"""
日志服务模块
处理所有日志相关的业务逻辑，使用SQLAlchemy数据库操作
"""

import os
import shutil
import zipfile
import tempfile
import uuid
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple
from pathlib import Path
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, case, func
from sqlalchemy.orm import load_only

from app.config import settings
from app.agents.log_analysis.result_normalization import normalize_saved_analysis_result
from app.models.log import (
    LogRecord, LogFileInfo, LogUploadRequest, LogListRequest, BatchDeleteRequest,
    BatchDownloadRequest, LogStatus, LogLevel, LogMetadata,
    BatchOperationResult, SortField, SortOrder, LogListData, PaginationInfo,
    LogAttachmentInfo,
)
from app.models.project_repo import ProjectRepo
from app.models.database import get_db
from app.services.base import BaseCRUDService
from app.exceptions import (
    FileNotFoundError, FileUploadError, StorageError,
    FileProcessingError, BatchOperationError
)
from app.utils.validation import file_validator
from app.utils.storage_utils import ensure_free_space
import glob

logger = logging.getLogger(__name__)

# 每条日志最多保留的 AI 分析对话轮数，避免 metadata_json 无限膨胀。
_MAX_ANALYSIS_TURNS = 50


def append_analysis_conversation_turn(
    extra_fields: Dict[str, Any],
    analysis_data: Dict[str, Any],
    *,
    query: Optional[str] = None,
) -> None:
    """将一轮 AI 分析结果追加到 ``extra_fields['ai_analysis_conversation']``。

    日志详情页需要展示完整的多轮问答历史，而不仅是最近一次结果
    （最近一次结果仍保存在 ``ai_analysis_result`` 中以保持向后兼容）。

    每轮副本会剔除 ``trace_events``（仅最近一次结果的实时 trace 才需要它），
    避免 ``metadata_json`` 因历史轮次而急剧膨胀。
    """
    if not isinstance(extra_fields, dict) or not isinstance(analysis_data, dict):
        return

    turn = {k: v for k, v in analysis_data.items() if k != "trace_events"}
    if query and not turn.get("query"):
        turn["query"] = query
    turn.setdefault("created_at", datetime.utcnow().isoformat())

    conversation = extra_fields.get("ai_analysis_conversation")
    if not isinstance(conversation, list):
        conversation = []
    conversation.append(turn)
    if len(conversation) > _MAX_ANALYSIS_TURNS:
        conversation = conversation[-_MAX_ANALYSIS_TURNS:]
    extra_fields["ai_analysis_conversation"] = conversation


def seed_conversation_from_legacy_result(extra_fields: Dict[str, Any]) -> None:
    """升级兼容：把已存在但未进入历史的上一轮结果补种为第一轮。

    旧版本只在 ``ai_analysis_result`` 保存最近一次结果，不维护多轮历史。
    升级到多轮历史后，若某条日志在旧版本下已分析过、本次是升级后的首次
    再分析，则历史列表为空但 ``ai_analysis_result`` 仍是上一轮结果。此时若
    直接覆盖并仅追加本轮，上一轮问答会被永久丢失。

    本函数应在覆盖 ``ai_analysis_result`` 之前调用：仅当历史为空且存在旧结果时，
    把旧结果作为第一轮补入，使升级后的首次再分析得到 ``[上一轮, 本轮]``。
    对全新日志（无旧结果）或已有历史的日志均为无操作，因此每条日志至多触发一次，
    不会产生重复。
    """
    if not isinstance(extra_fields, dict):
        return
    conversation = extra_fields.get("ai_analysis_conversation")
    if isinstance(conversation, list) and conversation:
        return  # 已有历史，无需补种
    legacy = extra_fields.get("ai_analysis_result")
    if isinstance(legacy, dict) and legacy:
        append_analysis_conversation_turn(extra_fields, legacy, query=legacy.get("query"))


class LogService(BaseCRUDService[LogRecord]):
    """日志服务类"""
    
    def __init__(self):
        super().__init__(LogRecord)
        self.storage_path = Path(settings.temp_dir)
        self.logs_storage_path = self.storage_path / "logs"
        self.downloads_storage_path = self.storage_path / "downloads"
        
        # 确保存储目录存在
        self.logs_storage_path.mkdir(parents=True, exist_ok=True)
        self.downloads_storage_path.mkdir(parents=True, exist_ok=True)

    async def upload_log(
        self, 
        db: AsyncSession, 
        file: UploadFile, 
        request: LogUploadRequest
    ) -> LogFileInfo:
        """
        上传日志文件
        
        Args:
            db: 数据库会话
            file: 上传的文件
            request: 上传请求参数
            
        Returns:
            LogFileInfo: 上传后的文件信息
        """
        logger.info(f"LogService - 开始处理日志上传: {file.filename}")
        try:
            # 验证文件
            logger.info(f"LogService - 开始验证文件: {file.filename}")
            is_valid, error_msg = await file_validator.validate_upload_file(file)
            if not is_valid:
                logger.error(f"LogService - 文件验证失败: {file.filename}, 错误: {error_msg}")
                raise FileUploadError(error_msg)
            logger.info(f"LogService - 文件验证通过: {file.filename}")
            
            # 生成文件ID和存储路径
            file_id = str(uuid.uuid4())
            original_filename = file.filename
            sanitized_filename = file_validator.sanitize_filename(original_filename)
            stored_filename = f"{file_id}_{sanitized_filename}"
            file_path = self.logs_storage_path / stored_filename
            logger.info(f"LogService - 生成文件路径: {file_path}")
            
            # 计算文件校验和
            logger.info(f"LogService - 开始计算文件校验和: {file.filename}")
            checksum = await file_validator.calculate_file_checksum(file)
            logger.info(f"LogService - 文件校验和计算完成: {checksum[:16]}...")
            
            # 保存文件
            logger.info(f"LogService - 开始保存文件: {file_path}")
            await self._save_file(file, file_path)
            logger.info(f"LogService - 文件保存完成")
            
            # 获取文件信息
            file_size = file_path.stat().st_size
            mime_type = file.content_type or "application/octet-stream"
            logger.info(f"LogService - 文件信息: 大小={file_size} bytes, MIME类型={mime_type}")
            
            # 将元数据转换为JSON字符串
            metadata_json = None
            if request.metadata:
                metadata_json = request.metadata.model_dump_json()
            
            # 根据项目确定初始状态和进度
            # OAM天线日志上传后直接标记为已完成，其他类型保持待处理状态
            is_oam = (request.project_code or "").lower() == "oam_antenna"
            initial_status = LogStatus.COMPLETED if is_oam else LogStatus.PENDING
            initial_progress = 100.0 if is_oam else 0.0
            processed_at = datetime.utcnow() if is_oam else None

            logger.info(f"LogService - 项目: code={request.project_code} id={request.project_id}, 初始状态: {initial_status.value}, 初始进度: {initial_progress}")

            # 创建数据库记录
            logger.info(f"LogService - 开始创建数据库记录: ID={file_id}")
            create_data = {
                "id": file_id,
                "filename": stored_filename,
                "original_filename": original_filename,
                "file_size": file_size,
                "file_path": str(file_path),
                "archive_path": str(file_path),
                "project_id": request.project_id,
                "status": initial_status,
                "progress": initial_progress,
                "checksum": checksum,
                "mime_type": mime_type,
                "log_level": request.log_level,
                "metadata_json": metadata_json,
                "issue_description": request.issue_description
            }
            
            # 如果是OAM天线日志，设置处理完成时间
            if processed_at:
                create_data["processed_at"] = processed_at
            
            log_record = await self.create(db=db, **create_data)
            logger.info(f"LogService - 数据库记录创建成功: ID={file_id}")

            # 转换为Pydantic模型
            project = await self._get_project(db, log_record.project_id)
            result = await self._db_to_pydantic(log_record, request.metadata, project)
            
            # 立即提交事务，确保数据在触发Celery任务前已完全写入数据库
            # 这样可以避免Celery worker无法找到记录的竞态条件
            await db.commit()
            logger.info(f"LogService - 数据库记录已提交: ID={file_id}")

            # 记录日志上传业务事件（best-effort，绝不影响上传流程）。
            # 文件内容/路径不入库；仅记录低敏的 log_type/status，字节数走 Prometheus。
            try:
                from app.services import metrics_service
                from app.utils import metrics as prom

                project_label = request.project_code or "unclassified"
                status_value = getattr(initial_status, "value", str(initial_status))
                await metrics_service.record_business_event(
                    event_type="log_activity",
                    source="log_upload",
                    idempotency_key=f"log_activity:upload:{file_id}",
                    status=status_value,
                    log_id=file_id,
                    metadata={"project_code": project_label},
                )
                prom.record_log_upload(
                    log_type=project_label,
                    status=status_value,
                    uploaded_bytes=file_size,
                )
            except Exception as metrics_exc:  # noqa: BLE001
                logger.debug(f"LogService - 上传指标记录失败（已忽略）: {metrics_exc}")
            
            # 检查是否为协议栈日志，如果是则自动触发处理
            # OAM天线日志已经标记为完成，无需额外处理
            if not is_oam:
                logger.info(f"LogService - 检查是否需要触发协议栈处理: {original_filename}")
                await self._check_and_trigger_protocol_stack_processing(log_record)
            else:
                logger.info(f"LogService - OAM天线日志无需额外处理，已标记为完成: {original_filename}")
            
            logger.info(f"LogService - 日志上传完成: ID={file_id}, 文件名={original_filename}")
            return result
            
        except Exception as e:
            logger.error(f"LogService - 日志上传失败: {file.filename}, 错误: {str(e)}")
            # 清理已创建的文件
            if 'file_path' in locals() and file_path.exists():
                file_path.unlink()
                logger.info(f"LogService - 清理失败文件: {file_path}")
            
            if isinstance(e, (FileUploadError, StorageError)):
                raise e
            else:
                raise FileUploadError(f"上传失败: {str(e)}")

    @staticmethod
    def _record_metadata(record: LogRecord) -> Tuple[Dict[str, Any], LogMetadata]:
        """Parse one record's metadata without letting malformed legacy data
        break list/group operations.
        """
        metadata_dict: Dict[str, Any] = {}
        if record.metadata_json:
            try:
                raw = json.loads(record.metadata_json) or {}
                if isinstance(raw, dict):
                    metadata_dict = raw
            except Exception:
                metadata_dict = {}
        if not metadata_dict:
            return {}, LogMetadata()
        try:
            return metadata_dict, LogMetadata(**metadata_dict)
        except Exception as exc:
            logger.warning(
                "Log metadata is invalid, recovering extra_fields only "
                "record_id=%s: %s",
                record.id,
                exc,
            )
            extra_fields = metadata_dict.get("extra_fields")
            try:
                return metadata_dict, LogMetadata(
                    extra_fields=extra_fields
                    if isinstance(extra_fields, dict)
                    else {}
                )
            except Exception:
                return metadata_dict, LogMetadata()

    @classmethod
    def _legacy_analysis_group_identity(
        cls, record: LogRecord
    ) -> Optional[Tuple[str, str, Optional[int]]]:
        """Infer groups written before ``analysis_group_id`` existed.

        Old AI-chat uploads already persisted ``chat_session_id`` on every
        attachment. The issue description is the upload turn's question, so
        including it prevents unrelated upload turns in the same conversation
        from normally being collapsed together.
        """
        metadata_dict, _ = cls._record_metadata(record)
        if metadata_dict.get("source") != "ai_chat":
            return None
        extra_fields = metadata_dict.get("extra_fields")
        if not isinstance(extra_fields, dict):
            return None
        session_id = str(extra_fields.get("chat_session_id") or "").strip()
        if not session_id:
            return None
        return (
            session_id,
            str(record.issue_description or "").strip(),
            record.project_id,
        )

    @classmethod
    def _analysis_group_key(cls, record: LogRecord) -> Tuple[Any, ...]:
        if record.analysis_group_id:
            return ("analysis_group", record.analysis_group_id)
        legacy_identity = cls._legacy_analysis_group_identity(record)
        if legacy_identity is not None:
            return ("legacy_ai_chat", *legacy_identity)
        return ("log_record", record.id)

    @staticmethod
    def _group_status(records: List[LogRecord]) -> LogStatus:
        statuses = {record.status for record in records}
        if LogStatus.PROCESSING in statuses:
            return LogStatus.PROCESSING
        if LogStatus.FAILED in statuses:
            return LogStatus.FAILED
        if LogStatus.PENDING in statuses:
            return LogStatus.PENDING
        return LogStatus.COMPLETED

    @classmethod
    def _group_primary_record(
        cls, records: List[LogRecord]
    ) -> LogRecord:
        def _sort_key(record: LogRecord) -> Tuple[Any, ...]:
            metadata_dict, _ = cls._record_metadata(record)
            extra_fields = metadata_dict.get("extra_fields")
            has_analysis = (
                isinstance(extra_fields, dict)
                and isinstance(extra_fields.get("ai_analysis_result"), dict)
            )
            return (
                0 if has_analysis else 1,
                record.created_at,
                record.id,
            )

        return min(records, key=_sort_key)

    async def get_analysis_group_records(
        self,
        db: AsyncSession,
        log_id: str,
        *,
        include_deleted: bool = False,
    ) -> List[LogRecord]:
        """Return all original attachments represented by one list row."""
        target = await self.get_by_id(db, log_id)
        if target is None or (target.is_deleted and not include_deleted):
            raise FileNotFoundError(file_id=log_id)

        conditions = []
        if not include_deleted:
            conditions.append(LogRecord.is_deleted == False)

        if target.analysis_group_id:
            stmt = select(LogRecord).where(
                LogRecord.analysis_group_id == target.analysis_group_id,
                *conditions,
            )
            result = await db.execute(stmt)
            records = list(result.scalars().all())
        else:
            legacy_identity = self._legacy_analysis_group_identity(target)
            if legacy_identity is None:
                records = [target]
            else:
                stmt = select(LogRecord)
                if conditions:
                    stmt = stmt.where(*conditions)
                result = await db.execute(stmt)
                records = [
                    record
                    for record in result.scalars().all()
                    if self._legacy_analysis_group_identity(record)
                    == legacy_identity
                ]

        return sorted(
            records,
            key=lambda record: (record.created_at, record.id),
        )

    async def expand_analysis_group_ids(
        self, db: AsyncSession, log_ids: List[str]
    ) -> List[str]:
        """Expand representative IDs while preserving request/group order."""
        expanded: List[str] = []
        seen: set[str] = set()
        for log_id in log_ids:
            records = await self.get_analysis_group_records(db, log_id)
            for record in records:
                if record.id not in seen:
                    seen.add(record.id)
                    expanded.append(record.id)
        return expanded

    async def _group_to_pydantic(
        self,
        db: AsyncSession,
        records: List[LogRecord],
        project_map: Dict[int, ProjectRepo],
        *,
        enrich_analysis_trigger: bool = False,
        primary: Optional[LogRecord] = None,
    ) -> LogFileInfo:
        primary = primary if primary is not None else self._group_primary_record(records)
        _, metadata = self._record_metadata(primary)
        info = await self._db_to_pydantic(
            primary,
            metadata,
            project_map.get(primary.project_id),
            db=db if enrich_analysis_trigger else None,
        )
        ordered = sorted(
            records,
            key=lambda record: (record.created_at, record.id),
        )
        info.analysis_group_id = primary.analysis_group_id
        info.attachment_count = len(ordered)
        info.attachments = [
            LogAttachmentInfo(
                id=record.id,
                filename=record.original_filename or record.filename,
                file_size=record.file_size,
            )
            for record in ordered
        ]
        info.file_size = sum(record.file_size for record in ordered)
        info.status = self._group_status(ordered)
        info.progress = sum(record.progress for record in ordered) / len(ordered)
        info.created_at = min(record.created_at for record in ordered)
        info.updated_at = max(record.updated_at for record in ordered)
        # Every grouped attachment is incremented together by ZIP downloads.
        # max therefore represents the number of group downloads, not N times
        # that number.
        info.download_count = max(
            (record.download_count for record in ordered),
            default=0,
        )
        if len(ordered) > 1:
            timestamp = info.created_at.strftime("%Y%m%d_%H%M%S")
            info.download_filename = f"log_analysis_{timestamp}.zip"
        else:
            info.download_filename = (
                primary.original_filename or primary.filename
            )
        return info

    async def _list_group_headers(self, db: AsyncSession) -> List[LogRecord]:
        """SQLite 只读取分组/筛选所需字段，避免把所有分析正文载入 Python。

        临时 LogRecord 不加入 session；分页后才加载真实记录。其他数据库
        保留原有路径，避免对损坏的旧 JSON 做不兼容的强制类型转换。
        """
        if db.get_bind().dialect.name != "sqlite":
            result = await db.execute(
                select(LogRecord).where(LogRecord.is_deleted == False)
            )
            return list(result.scalars().all())

        metadata = case(
            (func.json_valid(LogRecord.metadata_json) == 1, LogRecord.metadata_json),
            else_="{}",
        )
        result = await db.execute(
            select(
                LogRecord.id, LogRecord.filename, LogRecord.original_filename,
                LogRecord.file_size, LogRecord.analysis_group_id,
                LogRecord.project_id, LogRecord.status, LogRecord.log_level,
                LogRecord.created_at, LogRecord.updated_at,
                LogRecord.issue_description,
                func.json_extract(metadata, "$.source").label("source"),
                func.json_extract(metadata, "$.extra_fields.chat_session_id").label("session_id"),
                func.json_type(metadata, "$.extra_fields.ai_analysis_result").label("analysis_type"),
            ).where(LogRecord.is_deleted == False)
        )
        headers = []
        for row in result.mappings():
            values = dict(row)
            source = values.pop("source")
            session_id = values.pop("session_id")
            has_analysis = values.pop("analysis_type") == "object"
            extra = {"chat_session_id": session_id}
            if has_analysis:
                extra["ai_analysis_result"] = {}
            headers.append(LogRecord(
                **values,
                metadata_json=json.dumps({"source": source, "extra_fields": extra}),
            ))
        return headers

    @staticmethod
    def _compact_list_info(info: LogFileInfo) -> LogFileInfo:
        """列表仅保留展示需要的摘要，正文继续由详情接口提供。"""
        info.ai_analysis_result = None
        info.ai_analysis_conversation = None
        info.manual_analysis = None
        info.issue_description = None
        info.error_message = None
        info.ai_analysis_query = None
        info.ai_analysis_error = None
        if info.metadata:
            info.metadata = LogMetadata(
                source=info.metadata.source,
                environment=info.metadata.environment,
                service_name=info.metadata.service_name,
                version=info.metadata.version,
                tags=info.metadata.tags,
            )
        return info

    async def get_log_list(
        self, 
        db: AsyncSession, 
        request: LogListRequest
    ) -> LogListData:
        """
        获取日志列表
        
        Args:
            db: 数据库会话
            request: 查询请求参数
            
        Returns:
            LogListData: 包含日志列表和分页信息的数据
        """
        try:
            # Grouping has to happen before pagination: one AI analysis can own
            # several LogRecord rows but represents exactly one list item.
            all_records = await self._list_group_headers(db)

            groups: Dict[Tuple[Any, ...], List[LogRecord]] = {}
            for record in all_records:
                groups.setdefault(
                    self._analysis_group_key(record), []
                ).append(record)

            filtered_groups: List[List[LogRecord]] = []
            primaries = {
                id(records): self._group_primary_record(records)
                for records in groups.values()
            }
            normalized_search = (request.search or "").strip().casefold()
            for records in groups.values():
                primary = primaries[id(records)]
                created_at = min(record.created_at for record in records)

                if request.project_id is not None:
                    if request.project_id <= 0:
                        if primary.project_id is not None:
                            continue
                    elif primary.project_id != request.project_id:
                        continue
                if request.log_level and primary.log_level != request.log_level:
                    continue
                if (
                    request.status
                    and self._group_status(records) != request.status
                ):
                    continue
                if request.start_time and created_at < request.start_time:
                    continue
                if request.end_time and created_at > request.end_time:
                    continue
                if normalized_search and not any(
                    normalized_search
                    in (record.original_filename or record.filename).casefold()
                    or normalized_search in record.filename.casefold()
                    for record in records
                ):
                    continue
                filtered_groups.append(records)

            def _group_sort_key(records: List[LogRecord]) -> Any:
                primary = primaries[id(records)]
                if request.sort_by == SortField.FILE_SIZE:
                    return sum(record.file_size for record in records)
                if request.sort_by == SortField.UPDATED_AT:
                    return max(record.updated_at for record in records)
                if request.sort_by == SortField.FILENAME:
                    return (
                        primary.original_filename or primary.filename
                    ).casefold()
                return min(record.created_at for record in records)

            filtered_groups.sort(
                key=_group_sort_key,
                reverse=request.sort_order == SortOrder.DESC,
            )

            total = len(filtered_groups)
            offset = (request.page - 1) * request.per_page
            page_groups = filtered_groups[
                offset:offset + request.per_page
            ]
            page_ids = [record.id for records in page_groups for record in records]
            page_records = []
            if page_ids:
                result = await db.execute(
                    select(LogRecord).where(LogRecord.id.in_(page_ids))
                )
                page_records = list(result.scalars().all())
            record_map = {record.id: record for record in page_records}
            project_map = await self._get_project_map(
                db, [record.project_id for record in page_records]
            )
            log_infos = [
                await self._group_to_pydantic(
                    db,
                    [record_map[record.id] for record in records],
                    project_map,
                    enrich_analysis_trigger=True,
                    primary=record_map[primaries[id(records)].id],
                )
                for records in page_groups
            ]
            if request.compact:
                log_infos = [self._compact_list_info(info) for info in log_infos]

            pages = (total + request.per_page - 1) // request.per_page if total > 0 else 0
            pagination = PaginationInfo(
                page=request.page,
                per_page=request.per_page,
                total=total,
                pages=pages
            )
            
            return LogListData(
                logs=log_infos,
                pagination=pagination
            )
            
        except Exception as e:
            raise StorageError(f"获取日志列表失败: {str(e)}")

    async def get_log_detail(self, db: AsyncSession, log_id: str) -> LogFileInfo:
        """
        获取日志详情

        Args:
            db: 数据库会话
            log_id: 日志ID

        Returns:
            LogFileInfo: 日志详情
        """
        log_record = await self.get_by_id(db, log_id)

        if not log_record or log_record.is_deleted:
            raise FileNotFoundError(file_id=log_id)

        # 检查文件是否存在
        if not Path(log_record.file_path).exists():
            # 更新状态为失败
            await self.update(db, log_id, status=LogStatus.FAILED, error_message="文件不存在")
            raise FileNotFoundError(file_id=log_id)

        group_records = await self.get_analysis_group_records(db, log_id)
        if len(group_records) > 1:
            project_map = await self._get_project_map(
                db, [record.project_id for record in group_records]
            )
            return await self._group_to_pydantic(
                db,
                group_records,
                project_map,
                enrich_analysis_trigger=True,
            )

        # 解析元数据
        metadata = LogMetadata()
        if log_record.metadata_json:
            metadata_dict: Dict[str, Any] = {}
            try:
                metadata_dict = json.loads(log_record.metadata_json)
            except json.JSONDecodeError as je:
                logger.error("get_log_detail: metadata_json 解析失败 log_id=%s: %s", log_id, je)

            if metadata_dict:
                try:
                    metadata = LogMetadata(**metadata_dict)
                except Exception as meta_exc:
                    # Pydantic 校验失败时记录警告并降级恢复 extra_fields，
                    # 以保证 ai_analysis_result 等关键字段不丢失
                    logger.warning(
                        "get_log_detail: LogMetadata 构建失败 log_id=%s: %s — 尝试仅恢复 extra_fields",
                        log_id, meta_exc,
                    )
                    try:
                        ef = metadata_dict.get("extra_fields")
                        metadata = LogMetadata(extra_fields=ef if isinstance(ef, dict) else {})
                    except Exception as ef_exc:
                        logger.error(
                            "get_log_detail: extra_fields 恢复也失败 log_id=%s: %s",
                            log_id, ef_exc,
                        )

        return await self._db_to_pydantic(log_record, metadata, db=db)

    async def save_ai_analysis_result(
        self,
        db: AsyncSession,
        log_id: str,
        analysis_data: Dict[str, Any],
        query: Optional[str] = None,
    ) -> LogFileInfo:
        """
        保存AI分析结果到日志元数据中，便于后续读取

        除覆盖 ``ai_analysis_result``（最近一次结果）外，还会把本轮结果追加到
        ``ai_analysis_conversation`` 历史列表，使日志详情页能展示完整的多轮问答。
        ``query`` 会写入结果，确保详情页在前端切换时仍显示正确的本轮提问。
        """
        log_record = await self.get_by_id(db, log_id)

        if not log_record or log_record.is_deleted:
            raise FileNotFoundError(file_id=log_id)

        metadata_dict: Dict[str, Any] = {}
        try:
            if log_record.metadata_json:
                metadata_dict = json.loads(log_record.metadata_json) or {}
        except Exception:
            metadata_dict = {}

        # 确保 extra_fields 可用
        extra_fields = metadata_dict.get("extra_fields")
        if not isinstance(extra_fields, dict):
            extra_fields = {}

        # 把本轮提问写入结果，便于详情页直接读取（避免依赖前端临时输入框）
        if query and isinstance(analysis_data, dict) and not analysis_data.get("query"):
            analysis_data = {**analysis_data, "query": query}

        # 升级兼容：覆盖前先把旧版本遗留的上一轮结果补种进历史，避免丢失上一轮问答
        seed_conversation_from_legacy_result(extra_fields)
        # 更新并写回元数据：覆盖最近一次结果，并追加到多轮对话历史
        extra_fields["ai_analysis_result"] = analysis_data
        append_analysis_conversation_turn(extra_fields, analysis_data, query=query)
        metadata_dict["extra_fields"] = extra_fields
        log_record.metadata_json = json.dumps(metadata_dict, ensure_ascii=False, default=str)
        log_record.updated_at = datetime.utcnow()

        db.add(log_record)
        await db.commit()
        await db.refresh(log_record)

        metadata = LogMetadata(**metadata_dict) if metadata_dict else LogMetadata()
        return await self._db_to_pydantic(log_record, metadata, db=db)

    async def save_manual_analysis(
        self,
        db: AsyncSession,
        log_id: str,
        content: str,
        author: Optional[Dict[str, Any]] = None
    ) -> LogFileInfo:
        """
        保存人工分析结果到日志元数据中
        """
        log_record = await self.get_by_id(db, log_id)

        if not log_record or log_record.is_deleted:
            raise FileNotFoundError(file_id=log_id)

        metadata_dict: Dict[str, Any] = {}
        try:
            if log_record.metadata_json:
                metadata_dict = json.loads(log_record.metadata_json) or {}
        except Exception:
            metadata_dict = {}

        if not isinstance(metadata_dict, dict):
            metadata_dict = {}

        extra_fields = metadata_dict.get("extra_fields")
        if not isinstance(extra_fields, dict):
            extra_fields = {}

        manual_analysis_payload: Dict[str, Any] = {
            "content": content,
            "updated_at": datetime.utcnow().isoformat()
        }
        if author:
            manual_analysis_payload["author"] = {
                k: v for k, v in author.items() if v is not None
            }
        extra_fields["manual_analysis"] = manual_analysis_payload
        metadata_dict["extra_fields"] = extra_fields
        log_record.metadata_json = json.dumps(metadata_dict, ensure_ascii=False, default=str)
        log_record.updated_at = datetime.utcnow()

        db.add(log_record)
        await db.commit()
        await db.refresh(log_record)

        metadata = LogMetadata(**metadata_dict) if metadata_dict else LogMetadata()
        return await self._db_to_pydantic(log_record, metadata, db=db)

    async def update_issue_description(
        self,
        db: AsyncSession,
        log_id: str,
        issue_description: Optional[str]
    ) -> LogFileInfo:
        """
        更新问题描述
        """
        log_record = await self.get_by_id(db, log_id)

        if not log_record or log_record.is_deleted:
            raise FileNotFoundError(file_id=log_id)

        log_record.issue_description = issue_description
        log_record.updated_at = datetime.utcnow()

        metadata = None
        try:
            if log_record.metadata_json:
                metadata_dict = json.loads(log_record.metadata_json)
                metadata = LogMetadata(**metadata_dict)
        except Exception:
            metadata = LogMetadata()

        db.add(log_record)
        await db.commit()
        await db.refresh(log_record)

        return await self._db_to_pydantic(log_record, metadata, db=db)

    async def update_ai_analysis_task(
        self,
        db: AsyncSession,
        log_id: str,
        *,
        task_id: Optional[str] = None,
        status: Optional[str] = None,
        progress: Optional[float] = None,
        query: Optional[str] = None,
        error: Optional[str] = None,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
        triggered_by: Optional[Dict[str, Any]] = None,
    ) -> LogFileInfo:
        """
        更新AI分析任务的元数据（状态/进度/错误）
        """
        log_record = await self.get_by_id(db, log_id)

        if not log_record or log_record.is_deleted:
            raise FileNotFoundError(file_id=log_id)

        metadata_dict: Dict[str, Any] = {}
        try:
            if log_record.metadata_json:
                metadata_dict = json.loads(log_record.metadata_json) or {}
        except Exception:
            metadata_dict = {}

        extra_fields = metadata_dict.get("extra_fields")
        if not isinstance(extra_fields, dict):
            extra_fields = {}

        task_info = extra_fields.get("ai_analysis_task")
        if not isinstance(task_info, dict):
            task_info = {}

        if task_id:
            task_info["task_id"] = task_id
        if status:
            task_info["status"] = status
        if progress is not None:
            task_info["progress"] = float(progress)
        if query:
            task_info["query"] = query
        if error is not None:
            task_info["error"] = error
        if started_at:
            task_info["started_at"] = started_at.isoformat()
        if finished_at:
            task_info["finished_at"] = finished_at.isoformat()
        if isinstance(triggered_by, dict):
            task_info["triggered_by"] = triggered_by

        extra_fields["ai_analysis_task"] = task_info
        metadata_dict["extra_fields"] = extra_fields
        log_record.metadata_json = json.dumps(metadata_dict, ensure_ascii=False, default=str)
        log_record.updated_at = datetime.utcnow()

        db.add(log_record)
        await db.commit()
        await db.refresh(log_record)

        metadata = LogMetadata(**metadata_dict) if metadata_dict else LogMetadata()
        return await self._db_to_pydantic(log_record, metadata, db=db)

    async def delete_log(self, db: AsyncSession, log_id: str, hard_delete: bool = False) -> bool:
        """
        删除日志文件（支持软删除和硬删除）
        
        Args:
            db: 数据库会话
            log_id: 日志ID
            hard_delete: 是否硬删除（物理删除文件和数据库记录）
            
        Returns:
            bool: 是否删除成功
        """
        log_record = await self.get_by_id(db, log_id)
        
        if not log_record:
            raise FileNotFoundError(file_id=log_id)
        
        # 检查是否已经被软删除
        if log_record.is_deleted and not hard_delete:
            return True
        
        try:
            if hard_delete:
                # 硬删除：删除文件和数据库记录
                file_path = Path(log_record.file_path)
                if file_path.exists():
                    file_path.unlink()
                    logger.info(f"删除主文件: {file_path}")
                
                # 清理关联的临时处理目录
                self._cleanup_processing_directories(log_record)
                
                # 从数据库删除记录
                await self.delete(db, log_id)
            else:
                # 软删除：只标记为已删除，同时清理物理文件
                # 删除主文件
                file_path = Path(log_record.file_path)
                if file_path.exists():
                    file_path.unlink()
                    logger.info(f"软删除-删除主文件: {file_path}")
                
                # 清理关联的临时处理目录
                self._cleanup_processing_directories(log_record)
                
                # 更新数据库标记
                log_record.is_deleted = True
                log_record.deleted_at = datetime.utcnow()
                db.add(log_record)
                await db.commit()
            
            return True
            
        except Exception as e:
            await db.rollback()
            raise StorageError(f"删除日志失败: {str(e)}")

    async def update_log_status(
        self, 
        db: AsyncSession, 
        log_id: str, 
        status: LogStatus,
        progress: Optional[float] = None,
        error_message: Optional[str] = None
    ) -> LogFileInfo:
        """
        更新日志状态
        
        Args:
            db: 数据库会话
            log_id: 日志ID
            status: 新状态
            progress: 处理进度
            error_message: 错误信息
            
        Returns:
            LogFileInfo: 更新后的日志信息
        """
        logger.info(f"LogService - 更新日志状态: ID={log_id}, 状态={status.value}, 进度={progress}")
        update_data = {"status": status}
        
        if progress is not None:
            update_data["progress"] = progress
        
        if error_message is not None:
            update_data["error_message"] = error_message
        
        if status == LogStatus.COMPLETED:
            update_data["processed_at"] = datetime.utcnow()
            update_data["progress"] = 100.0
        
        log_record = await self.update(db, log_id, **update_data)
        
        if not log_record:
            logger.error(f"LogService - 日志记录不存在: ID={log_id}")
            raise FileNotFoundError(file_id=log_id)
        
        logger.info(f"LogService - 日志状态更新成功: ID={log_id}, 新状态={log_record.status.value}")
        
        # 解析元数据
        metadata = LogMetadata()
        if log_record.metadata_json:
            try:
                metadata_dict = json.loads(log_record.metadata_json)
                metadata = LogMetadata(**metadata_dict)
            except:
                pass
        
        return await self._db_to_pydantic(log_record, metadata, db=db)

    async def get_download_path(self, db: AsyncSession, log_id: str) -> str:
        """
        获取文件下载路径
        
        Args:
            db: 数据库会话
            log_id: 日志ID
            
        Returns:
            str: 文件路径
        """
        # 获取日志记录
        record = await self.get_by_id(db, log_id)
        if not record:
            raise FileNotFoundError(filename=log_id)
        
        # 检查文件是否存在
        file_path = Path(record.file_path)
        if not file_path.exists():
            raise FileNotFoundError(filename=record.original_filename)
        
        return str(file_path)

    async def increment_download_count(self, db: AsyncSession, log_id: str) -> LogFileInfo:
        """
        增加下载次数
        
        Args:
            db: 数据库会话
            log_id: 日志ID
            
        Returns:
            LogFileInfo: 更新后的日志信息
        """
        # 获取日志记录
        record = await self.get_by_id(db, log_id)
        if not record:
            raise FileNotFoundError(filename=log_id)
        
        # 增加下载次数
        record.download_count += 1
        record.updated_at = datetime.utcnow()
        
        # 保存到数据库
        await db.commit()
        await db.refresh(record)
        
        # 解析元数据
        metadata = None
        if record.metadata_json:
            try:
                metadata_dict = json.loads(record.metadata_json)
                metadata = LogMetadata(**metadata_dict)
            except (json.JSONDecodeError, TypeError):
                metadata = LogMetadata()
        
        return await self._db_to_pydantic(record, metadata, db=db)

    async def batch_delete(
        self, 
        db: AsyncSession, 
        request: BatchDeleteRequest
    ) -> BatchOperationResult:
        """
        批量删除日志 - 改进版本，支持事务处理和详细错误报告
        
        Args:
            db: 数据库会话
            request: 批量删除请求
            
        Returns:
            BatchOperationResult: 操作结果
        """
        result = BatchOperationResult()
        
        # 首先批量查询所有日志记录
        try:
            stmt = select(LogRecord).where(
                LogRecord.id.in_(request.log_ids),
                LogRecord.is_deleted == False
            )
            db_result = await db.execute(stmt)
            log_records = db_result.scalars().all()
            
            # 创建ID到记录的映射
            records_map = {record.id: record for record in log_records}
            
            # 检查哪些ID不存在
            found_ids = set(records_map.keys())
            requested_ids = set(request.log_ids)
            missing_ids = requested_ids - found_ids
            
            # 为不存在的ID添加错误信息
            for missing_id in missing_ids:
                result.failed_count += 1
                result.failed_ids.append(missing_id)
                result.errors[missing_id] = "日志记录不存在"
                result.failed_logs.append({
                    "log_id": missing_id,
                    "reason": "日志记录不存在"
                })
            
        except Exception as e:
            # 如果查询失败，所有操作都失败
            for log_id in request.log_ids:
                result.failed_count += 1
                result.failed_ids.append(log_id)
                result.errors[log_id] = f"数据库查询失败: {str(e)}"
                result.failed_logs.append({
                    "log_id": log_id,
                    "reason": f"数据库查询失败: {str(e)}"
                })
            return result
        
        # 使用事务处理批量删除
        try:
            # 批量处理文件删除和数据库更新
            for log_id in found_ids:
                try:
                    record = records_map[log_id]
                    
                    # 删除物理文件
                    if request.force:
                        # 硬删除：删除物理文件和数据库记录
                        file_path = Path(record.file_path)
                        if file_path.exists():
                            file_path.unlink()
                            logger.info(f"批量删除-硬删除主文件: {file_path}")
                        
                        # 清理关联的临时处理目录
                        self._cleanup_processing_directories(record)
                        
                        # 删除数据库记录
                        await db.delete(record)
                    else:
                        # 软删除：只标记为已删除，同时清理物理文件
                        # 删除主文件
                        file_path = Path(record.file_path)
                        if file_path.exists():
                            file_path.unlink()
                            logger.info(f"批量删除-软删除主文件: {file_path}")
                        
                        # 清理关联的临时处理目录
                        self._cleanup_processing_directories(record)
                        
                        # 更新数据库标记
                        record.is_deleted = True
                        record.deleted_at = datetime.utcnow()
                    
                    result.success_count += 1
                    result.deleted_count += 1
                    result.success_ids.append(log_id)
                    
                except Exception as e:
                    result.failed_count += 1
                    result.failed_ids.append(log_id)
                    error_msg = f"删除失败: {str(e)}"
                    result.errors[log_id] = error_msg
                    result.failed_logs.append({
                        "log_id": log_id,
                        "reason": error_msg
                    })
                    
                    # 如果是硬删除模式下的文件删除失败，回滚事务
                    if request.force:
                        await db.rollback()
                        raise BatchOperationError(f"批量删除失败: {error_msg}")
            
            # 提交事务
            await db.commit()
            
        except Exception as e:
            # 事务回滚
            await db.rollback()
            
            # 如果是事务级别的错误，将所有成功的操作标记为失败
            for log_id in result.success_ids:
                result.failed_count += 1
                result.failed_ids.append(log_id)
                result.errors[log_id] = f"事务回滚: {str(e)}"
                result.failed_logs.append({
                    "log_id": log_id,
                    "reason": f"事务回滚: {str(e)}"
                })
            
            # 重置成功计数
            result.success_count = 0
            result.deleted_count = 0
            result.success_ids = []
        
        return result

    async def batch_download(
        self, 
        db: AsyncSession, 
        request: BatchDownloadRequest
    ) -> str:
        """
        批量下载日志 - 改进版本，支持流式zip文件生成
        
        Args:
            db: 数据库会话
            request: 批量下载请求
            
        Returns:
            str: 压缩文件路径
        """
        try:
            # 批量查询所有日志记录
            stmt = select(LogRecord).where(
                LogRecord.id.in_(request.log_ids),
                LogRecord.is_deleted == False
            )
            db_result = await db.execute(stmt)
            records_by_id = {
                record.id: record
                for record in db_result.scalars().all()
            }
            log_records = [
                records_by_id[log_id]
                for log_id in request.log_ids
                if log_id in records_by_id
            ]
            
            if not log_records:
                raise FileNotFoundError("没有找到有效的日志文件")
            
            # 创建临时压缩文件
            download_id = str(uuid.uuid4())
            zip_filename = f"logs_batch_{download_id}.zip"
            zip_path = self.downloads_storage_path / zip_filename

            # 预估压缩前所需空间：原始文件总大小 + 10% 冗余
            try:
                estimated_total = sum(Path(record.file_path).stat().st_size for record in log_records if Path(record.file_path).exists())
            except Exception:
                estimated_total = 0
            estimated_zip_size = int(estimated_total * 0.5)  # 粗略估计压缩后占比
            ensure_free_space(
                self.downloads_storage_path,
                required_bytes=estimated_zip_size,
                reserve_bytes=settings.disk_reserve_bytes,
            )
            
            # 使用流式压缩避免内存溢出
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zipf:
                processed_count = 0
                used_filenames: set[str] = set()
                
                for log_record in log_records:
                    try:
                        file_path = Path(log_record.file_path)
                        
                        if not file_path.exists():
                            # 如果文件不存在，创建一个错误信息文件
                            error_filename = f"{log_record.original_filename}.error.txt"
                            error_content = f"错误: 文件 {log_record.original_filename} 不存在\n"
                            error_content += f"原始路径: {log_record.file_path}\n"
                            error_content += f"日志ID: {log_record.id}\n"
                            zipf.writestr(error_filename, error_content)
                            continue
                        
                        # Preserve original names. Only add a short ID when two
                        # attachments genuinely have the same filename.
                        base_name = (
                            log_record.original_filename
                            or log_record.filename
                        )
                        name, ext = os.path.splitext(base_name)
                        unique_filename = base_name
                        if unique_filename in used_filenames:
                            unique_filename = (
                                f"{name}_{log_record.id[:8]}{ext}"
                            )
                        used_filenames.add(unique_filename)
                        
                        # 流式添加文件到压缩包
                        with open(file_path, 'rb') as f:
                            # 分块读取文件，避免大文件占用过多内存
                            with zipf.open(unique_filename, 'w') as zf:
                                while True:
                                    chunk = f.read(8192)  # 8KB chunks
                                    if not chunk:
                                        break
                                    zf.write(chunk)
                        
                        # 如果需要包含元数据
                        if request.include_metadata:
                            metadata_content = await self._create_metadata_content(log_record)
                            metadata_filename = f"{name}_{log_record.id[:8]}.metadata.json"
                            zipf.writestr(metadata_filename, metadata_content)
                        
                        processed_count += 1
                        
                        # 更新下载计数
                        log_record.download_count += 1
                        
                    except Exception as e:
                        # 为单个文件错误创建错误信息文件
                        error_filename = f"{log_record.original_filename}.error.txt"
                        error_content = f"处理文件时发生错误: {str(e)}\n"
                        error_content += f"文件: {log_record.original_filename}\n"
                        error_content += f"日志ID: {log_record.id}\n"
                        zipf.writestr(error_filename, error_content)
                        continue
                
                # 添加批量下载信息文件
                batch_info = {
                    "download_id": download_id,
                    "requested_files": len(request.log_ids),
                    "processed_files": processed_count,
                    "download_time": datetime.utcnow().isoformat(),
                    "include_metadata": request.include_metadata,
                    "compress": request.compress
                }
                zipf.writestr("batch_download_info.json", json.dumps(batch_info, indent=2))
            
            # 提交数据库更改（下载计数）
            await db.commit()
            
            return str(zip_path)
            
        except Exception as e:
            await db.rollback()
            raise FileProcessingError(f"批量下载失败: {str(e)}")

    async def batch_download_stream(
        self, 
        db: AsyncSession, 
        request: BatchDownloadRequest
    ):
        """
        流式批量下载 - 生成器函数，用于API流式响应
        
        Args:
            db: 数据库会话
            request: 批量下载请求
            
        Yields:
            bytes: zip文件数据块
        """
        import io
        
        try:
            # 批量查询所有日志记录
            stmt = select(LogRecord).where(
                LogRecord.id.in_(request.log_ids),
                LogRecord.is_deleted == False
            )
            db_result = await db.execute(stmt)
            records_by_id = {
                record.id: record
                for record in db_result.scalars().all()
            }
            log_records = [
                records_by_id[log_id]
                for log_id in request.log_ids
                if log_id in records_by_id
            ]
            
            if not log_records:
                raise FileNotFoundError("没有找到有效的日志文件")
            
            # 创建内存中的zip文件
            zip_buffer = io.BytesIO()
            
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zipf:
                used_filenames: set[str] = set()
                for log_record in log_records:
                    try:
                        file_path = Path(log_record.file_path)
                        
                        if not file_path.exists():
                            continue
                        
                        base_name = (
                            log_record.original_filename
                            or log_record.filename
                        )
                        name, ext = os.path.splitext(base_name)
                        unique_filename = base_name
                        if unique_filename in used_filenames:
                            unique_filename = (
                                f"{name}_{log_record.id[:8]}{ext}"
                            )
                        used_filenames.add(unique_filename)
                        
                        # 添加文件到zip
                        zipf.write(file_path, unique_filename)
                        
                        # 如果需要包含元数据
                        if request.include_metadata:
                            metadata_content = await self._create_metadata_content(log_record)
                            metadata_filename = f"{name}_{log_record.id[:8]}.metadata.json"
                            zipf.writestr(metadata_filename, metadata_content)
                        
                        # 更新下载计数
                        log_record.download_count += 1
                        
                    except Exception:
                        continue
            
            # 提交数据库更改
            await db.commit()
            
            # 返回zip文件内容
            zip_buffer.seek(0)
            return zip_buffer.getvalue()
            
        except Exception as e:
            await db.rollback()
            raise FileProcessingError(f"流式批量下载失败: {str(e)}")

    async def cleanup_expired_logs(self, db: AsyncSession) -> int:
        """
        清理过期日志（可以基于创建时间或其他条件）
        
        Args:
            db: 数据库会话
            
        Returns:
            int: 清理的文件数量
        """
        # 这里可以实现清理逻辑，比如删除超过30天的日志
        cutoff_date = datetime.utcnow() - timedelta(days=30)
        
        # 查找过期的日志
        query = select(LogRecord).where(
            LogRecord.created_at < cutoff_date,
            LogRecord.is_deleted == False  # 仅清理未被标记删除的记录
        )
        result = await db.execute(query)
        expired_logs = result.scalars().all()
        
        cleaned_count = 0
        skipped_manual = 0
        for log_record in expired_logs:
            if self._has_manual_analysis(log_record):
                skipped_manual += 1
                logger.info(
                    f"跳过清理已人工分析的日志: 日志ID={log_record.id}, 原始文件名={log_record.original_filename}"
                )
                continue
            try:
                # 删除文件
                file_path = Path(log_record.file_path)
                if file_path.exists():
                    file_path.unlink()
                
                # 删除数据库记录
                await self.delete(db, log_record.id)
                cleaned_count += 1
            except Exception:
                # 忽略删除错误，继续处理其他文件
                pass

        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise StorageError(f"清理过期日志失败: {str(e)}")

        if skipped_manual > 0:
            logger.info(f"清理过期日志时跳过 {skipped_manual} 条已人工分析的日志记录")
        
        return cleaned_count
    
    async def retry_failed_protocol_stack_logs(self, db: AsyncSession, limit: int = 50) -> int:
        """
        在服务启动时重试失败的协议栈日志处理任务
        
        Args:
            db: 数据库会话
            limit: 本次重试的最大条数，避免启动时一次拉起过多任务
        
        Returns:
            int: 成功重新触发的任务数量
        """
        try:
            # OAM 项目无需解压处理；通过 project_repo 关联排除 oam_antenna
            oam_subquery = (
                select(ProjectRepo.id)
                .where(ProjectRepo.project_code == "oam_antenna")
                .scalar_subquery()
            )
            query = select(LogRecord).where(
                LogRecord.status == LogStatus.FAILED,
                LogRecord.is_deleted == False,
                or_(
                    LogRecord.project_id.is_(None),
                    LogRecord.project_id != oam_subquery,
                ),
            ).order_by(LogRecord.updated_at.desc())
            
            if limit and limit > 0:
                query = query.limit(limit)
            
            result = await db.execute(query)
            failed_logs = result.scalars().all()
            
            if not failed_logs:
                logger.info("LogService - 启动检查: 未发现需要重试的失败协议栈日志")
                return 0
            
            # 动态导入，避免循环依赖
            from app.tasks.log_processing import process_protocol_stack_log
            
            retriggered = 0
            for log_record in failed_logs:
                if not os.path.exists(log_record.file_path):
                    logger.warning(
                        f"LogService - 启动重试跳过: 文件不存在, 日志ID={log_record.id}, 路径={log_record.file_path}"
                    )
                    continue
                
                log_record.status = LogStatus.PENDING
                log_record.progress = 0.0
                log_record.error_message = None
                log_record.task_id = None
                log_record.processing_started_at = None
                
                task_result = process_protocol_stack_log.apply_async(
                    args=[log_record.id],
                    countdown=1
                )
                log_record.task_id = task_result.id
                retriggered += 1
                
                logger.info(
                    f"LogService - 启动重试已提交: 日志ID={log_record.id}, 任务ID={task_result.id}, "
                    f"文件={log_record.original_filename}"
                )
            
            await db.commit()
            return retriggered
        
        except Exception as e:
            await db.rollback()
            logger.error(f"LogService - 启动重试失败协议栈日志时出错: {str(e)}", exc_info=True)
            return 0

    # 私有方法
    async def _save_file(self, file: UploadFile, file_path: Path):
        """保存上传的文件"""
        try:
            await file.seek(0)
            content = await file.read()
            file_size = len(content)

            # 磁盘空间校验，避免写满容器文件系统
            ensure_free_space(
                file_path.parent,
                required_bytes=file_size,
                reserve_bytes=settings.disk_reserve_bytes,
            )

            with open(file_path, "wb") as buffer:
                buffer.write(content)
        except Exception as e:
            raise StorageError(f"保存文件失败: {str(e)}")

    async def _get_project(self, db: AsyncSession, project_id: Optional[int]) -> Optional[ProjectRepo]:
        """根据 project_id 获取项目，None / 不存在时返回 None。"""
        if not project_id:
            return None
        result = await db.execute(select(ProjectRepo).where(ProjectRepo.id == project_id))
        return result.scalar_one_or_none()

    async def _get_project_map(
        self, db: AsyncSession, project_ids: List[Optional[int]]
    ) -> Dict[int, ProjectRepo]:
        """批量获取项目，返回 {project_id: ProjectRepo}。"""
        ids = {pid for pid in project_ids if pid}
        if not ids:
            return {}
        result = await db.execute(select(ProjectRepo).where(ProjectRepo.id.in_(ids)))
        return {repo.id: repo for repo in result.scalars().all()}

    async def _db_to_pydantic(
        self,
        record: LogRecord,
        metadata: Optional[LogMetadata] = None,
        project: Optional[ProjectRepo] = None,
        db: Optional[AsyncSession] = None,
    ) -> LogFileInfo:
        """将数据库记录转换为Pydantic模型"""
        # 未显式传入 project 时，按 project_id 即时查询（单条场景）
        if project is None and db is not None and record.project_id:
            project = await self._get_project(db, record.project_id)
        ai_analysis_result = None
        ai_analysis_conversation: Optional[List[Dict[str, Any]]] = None
        ai_analysis_task: Dict[str, Any] = {}
        ai_analysis_triggered_by: Optional[Dict[str, Any]] = None
        manual_analysis_content: Optional[str] = None
        manual_analysis_updated_at: Optional[datetime] = None
        manual_analysis_author: Optional[Dict[str, Any]] = None
        try:
            ef = metadata.extra_fields if metadata else {}
            logger.debug(
                "_db_to_pydantic: record_id=%s extra_fields_keys=%s has_ai_result=%s",
                record.id,
                list(ef.keys()) if isinstance(ef, dict) else type(ef).__name__,
                isinstance(ef, dict) and "ai_analysis_result" in ef,
            )
            if metadata and metadata.extra_fields:
                ai_analysis_result = metadata.extra_fields.get("ai_analysis_result")
                if isinstance(ai_analysis_result, dict):
                    ai_analysis_result = normalize_saved_analysis_result(ai_analysis_result)
                if ai_analysis_result is not None:
                    logger.debug(
                        "_db_to_pydantic: record_id=%s ai_analysis_result found status=%s model=%s",
                        record.id,
                        ai_analysis_result.get("status") if isinstance(ai_analysis_result, dict) else type(ai_analysis_result).__name__,
                        ai_analysis_result.get("model") if isinstance(ai_analysis_result, dict) else "n/a",
                    )
                else:
                    logger.debug(
                        "_db_to_pydantic: record_id=%s ai_analysis_result is None; extra_fields keys=%s",
                        record.id,
                        list(metadata.extra_fields.keys()),
                    )
                raw_conversation = metadata.extra_fields.get("ai_analysis_conversation")
                if isinstance(raw_conversation, list) and raw_conversation:
                    ai_analysis_conversation = [
                        normalize_saved_analysis_result(turn)
                        for turn in raw_conversation if isinstance(turn, dict)
                    ]
                raw_task = metadata.extra_fields.get("ai_analysis_task")
                if isinstance(raw_task, dict):
                    ai_analysis_task = raw_task
                manual_analysis = metadata.extra_fields.get("manual_analysis")
                if isinstance(manual_analysis, dict):
                    manual_analysis_content = manual_analysis.get("content") or manual_analysis.get("text")
                    author = manual_analysis.get("author")
                    if isinstance(author, dict) and author:
                        manual_analysis_author = author
                    updated_at = manual_analysis.get("updated_at")
                    if isinstance(updated_at, str):
                        try:
                            manual_analysis_updated_at = datetime.fromisoformat(updated_at)
                        except Exception:
                            manual_analysis_updated_at = None
                elif isinstance(manual_analysis, str):
                    manual_analysis_content = manual_analysis
        except Exception as e:
            logger.error("_db_to_pydantic: extra_fields 提取异常 record_id=%s: %s", record.id, e, exc_info=True)
            ai_analysis_result = None

        result_trigger = (
            ai_analysis_result.get("triggered_by")
            if isinstance(ai_analysis_result, dict)
            and isinstance(ai_analysis_result.get("triggered_by"), dict)
            else None
        )
        task_trigger = (
            ai_analysis_task.get("triggered_by")
            if isinstance(ai_analysis_task.get("triggered_by"), dict)
            else None
        )
        task_status = str(ai_analysis_task.get("status") or "").lower()
        if task_status in {"queued", "running", "processing"} and task_trigger:
            ai_analysis_triggered_by = task_trigger
        elif result_trigger:
            ai_analysis_triggered_by = result_trigger
        elif task_trigger:
            ai_analysis_triggered_by = task_trigger

        should_backfill_trigger = (
            db is not None
            and ai_analysis_triggered_by is None
            and (
                isinstance(ai_analysis_result, dict)
                or getattr(metadata, "source", None) == "ai_chat"
            )
        )
        if should_backfill_trigger:
            enriched_result = await self._enrich_ai_analysis_trigger(
                db,
                record,
                ai_analysis_result if isinstance(ai_analysis_result, dict) else {},
            )
            enriched_trigger = enriched_result.get("triggered_by")
            if isinstance(enriched_trigger, dict):
                ai_analysis_triggered_by = enriched_trigger
                if isinstance(ai_analysis_result, dict):
                    ai_analysis_result = enriched_result

        metadata_payload = metadata or LogMetadata()
        try:
            if getattr(metadata_payload, "extra_fields", None) and isinstance(metadata_payload.extra_fields, dict):
                metadata_payload.extra_fields = {
                    k: v for k, v in metadata_payload.extra_fields.items()
                    if k not in {"ai_analysis_result", "ai_analysis_conversation", "manual_analysis"}
                }
        except Exception:
            pass

        return LogFileInfo(
            id=record.id,
            filename=record.filename,
            original_filename=record.original_filename,
            file_size=record.file_size,
            file_path=record.file_path,
            analysis_group_id=record.analysis_group_id,
            attachment_count=1,
            attachments=[
                LogAttachmentInfo(
                    id=record.id,
                    filename=record.original_filename or record.filename,
                    file_size=record.file_size,
                )
            ],
            download_filename=record.original_filename or record.filename,
            project_id=record.project_id,
            project_code=project.project_code if project else None,
            project_name=project.project_name if project else None,
            status=record.status,
            progress=record.progress,
            created_at=record.created_at,
            updated_at=record.updated_at,
            processed_at=record.processed_at,
            checksum=record.checksum,
            mime_type=record.mime_type,
            log_level=record.log_level,
            metadata=metadata_payload,
            error_message=record.error_message,
            download_count=record.download_count,
            issue_description=record.issue_description,
            has_ai_analysis=ai_analysis_result is not None,
            has_manual_analysis=bool(manual_analysis_content and manual_analysis_content.strip()),
            ai_analysis_result=ai_analysis_result,
            ai_analysis_conversation=ai_analysis_conversation,
            ai_analysis_triggered_by=ai_analysis_triggered_by,
            ai_analysis_task_id=ai_analysis_task.get("task_id"),
            ai_analysis_status=ai_analysis_task.get("status"),
            ai_analysis_progress=ai_analysis_task.get("progress"),
            ai_analysis_error=ai_analysis_task.get("error"),
            ai_analysis_query=ai_analysis_task.get("query"),
            ai_analysis_started_at=ai_analysis_task.get("started_at"),
            ai_analysis_finished_at=ai_analysis_task.get("finished_at"),
            manual_analysis=manual_analysis_content,
            manual_analysis_updated_at=manual_analysis_updated_at,
            manual_analysis_author=manual_analysis_author,
        )

    async def _enrich_ai_analysis_trigger(
        self,
        db: AsyncSession,
        record: LogRecord,
        ai_analysis_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Best-effort backfill for old ai_chat analysis results.

        New ai_chat results persist ``triggered_by`` directly. Older rows can
        still be identified through ``chat_agent_runs.request_json.log_id``.
        """
        try:
            from app.models.user import ChatAgentRun, User

            result = await db.execute(
                select(ChatAgentRun)
                .options(load_only(
                    ChatAgentRun.id, ChatAgentRun.request_json, ChatAgentRun.user_id,
                    ChatAgentRun.session_id, ChatAgentRun.started_at, ChatAgentRun.finished_at,
                    raiseload=True,
                ))
                .where(
                    ChatAgentRun.agent_kind == "log_analysis",
                    ChatAgentRun.request_json.ilike(f"%{record.id}%"),
                    # Fork 的 run 只描述个人追问，不能回填为原共享分析的触发人。
                    ~ChatAgentRun.request_json.ilike('%"forked_from_log_id"%'),
                )
                .order_by(ChatAgentRun.started_at.desc())
                .limit(20)
            )
            matched_run = None
            for run in result.scalars().all():
                try:
                    payload = json.loads(run.request_json or "{}")
                except Exception:
                    payload = {}
                if isinstance(payload, dict) and str(payload.get("log_id") or "") == str(record.id):
                    matched_run = run
                    break

            if matched_run is None:
                return ai_analysis_result

            user_payload: Dict[str, Any] = {}
            if matched_run.user_id:
                user_result = await db.execute(
                    select(User).options(load_only(
                        User.id, User.username, User.display_name, User.email,
                        raiseload=True,
                    )).where(User.id == matched_run.user_id)
                )
                user = user_result.scalar_one_or_none()
                if user is not None:
                    user_payload = {
                        "id": user.id,
                        "username": user.username,
                        "display_name": user.display_name,
                        "email": user.email,
                    }
                else:
                    user_payload = {"id": matched_run.user_id}

            enriched = dict(ai_analysis_result)
            enriched["triggered_by"] = {
                "source": "ai_chat",
                "run_id": matched_run.id,
                "session_id": matched_run.session_id,
                "user": {k: v for k, v in user_payload.items() if v is not None},
                "started_at": (
                    matched_run.started_at.isoformat()
                    if matched_run.started_at
                    else None
                ),
                "finished_at": (
                    matched_run.finished_at.isoformat()
                    if matched_run.finished_at
                    else None
                ),
            }
            return enriched
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "_enrich_ai_analysis_trigger skipped record_id=%s: %s",
                record.id,
                exc,
            )
            return ai_analysis_result

    async def _create_metadata_content(self, log_record: LogRecord) -> str:
        """创建元数据内容"""
        metadata = {
            "id": log_record.id,
            "original_filename": log_record.original_filename,
            "file_size": log_record.file_size,
            "mime_type": log_record.mime_type,
            "checksum": log_record.checksum,
            "status": log_record.status.value,
            "project_id": log_record.project_id,
            "log_level": log_record.log_level.value if log_record.log_level else None,
            "progress": log_record.progress,
            "created_at": log_record.created_at.isoformat(),
            "updated_at": log_record.updated_at.isoformat(),
            "processed_at": log_record.processed_at.isoformat() if log_record.processed_at else None,
            "error_message": log_record.error_message
        }
        
        # 添加解析后的元数据
        if log_record.metadata_json:
            try:
                parsed_metadata = json.loads(log_record.metadata_json)
                metadata["metadata"] = parsed_metadata
            except:
                pass
        
        return json.dumps(metadata, indent=2, ensure_ascii=False)

    def _cleanup_processing_directories(self, log_record: LogRecord):
        """
        清理与日志记录相关的所有临时处理目录
        
        Args:
            log_record: 日志记录
        """
        try:
            # 1. 清理基于 task_id 的临时处理目录
            if log_record.task_id:
                processing_dir = Path(settings.temp_dir) / f"processing_{log_record.task_id}"
                if processing_dir.exists():
                    shutil.rmtree(processing_dir, ignore_errors=True)
                    logger.info(f"清理临时处理目录: {processing_dir}")
            
            # 2. 清理所有可能的 processing_* 目录（针对 task_id 可能变更的情况）
            # 使用 glob 查找所有匹配的目录
            temp_base = Path(settings.temp_dir)
            for processing_dir in temp_base.glob("processing_*"):
                try:
                    # 检查目录中是否有与当前日志ID相关的文件
                    # 这样可以避免误删其他正在处理的日志的临时目录
                    if processing_dir.is_dir():
                        # 简单策略：如果目录存在时间超过24小时，或者为空，则删除
                        dir_mtime = processing_dir.stat().st_mtime
                        age_hours = (datetime.utcnow().timestamp() - dir_mtime) / 3600
                        
                        # 检查目录是否为空或过期
                        is_empty = not any(processing_dir.iterdir())
                        is_old = age_hours > 24
                        
                        if is_empty or is_old:
                            shutil.rmtree(processing_dir, ignore_errors=True)
                            logger.info(f"清理过期/空的临时处理目录: {processing_dir} (年龄: {age_hours:.1f}小时, 空目录: {is_empty})")
                except Exception as e:
                    logger.warning(f"清理临时处理目录失败: {processing_dir}, 错误: {e}")
            
        except Exception as e:
            logger.warning(f"清理临时处理目录时发生错误: {e}")

    async def _check_and_trigger_protocol_stack_processing(self, log_record: LogRecord):
        """
        检查是否为协议栈日志，如果是则自动触发处理
        
        Args:
            log_record: 日志记录
        """
        try:
            # 检查文件名是否包含"stack"关键字（全量日志的文件名同样包含 stack）
            if "stack" in log_record.original_filename.lower():
                logger.info(f"LogService - 检测到协议栈日志，准备启动处理任务: {log_record.original_filename}")
                # 动态导入避免循环导入
                from app.tasks.log_processing import process_protocol_stack_log
                
                # 启动异步任务，由于数据库事务已经立即提交，只需要很短的延迟
                task_result = process_protocol_stack_log.apply_async(
                    args=[log_record.id],
                    countdown=1  # 延迟1秒执行，给数据库一点时间确保在所有连接中可见
                )
                logger.info(f"LogService - 协议栈处理任务已启动: 任务ID={task_result.id}, 日志ID={log_record.id}, 延迟执行=1秒")
                    
        except Exception as e:
            # 记录错误但不影响文件上传流程
            logger.error(f"LogService - 触发协议栈处理失败: 日志ID={log_record.id}, 错误: {str(e)}")

    def _has_manual_analysis(self, log_record: LogRecord) -> bool:
        """
        判断日志是否包含人工分析结果，如果有则视为需要长期保留
        """
        try:
            if not log_record.metadata_json:
                return False

            metadata = json.loads(log_record.metadata_json) or {}
            extra_fields = metadata.get("extra_fields")
            if not isinstance(extra_fields, dict):
                return False

            manual_analysis = extra_fields.get("manual_analysis")
            content = None
            if isinstance(manual_analysis, dict):
                content = manual_analysis.get("content") or manual_analysis.get("text")
            elif isinstance(manual_analysis, str):
                content = manual_analysis

            return bool(content and str(content).strip())
        except Exception:
            return False


# 创建全局服务实例
log_service = LogService()
