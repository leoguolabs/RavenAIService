"""
定期清理任务
用于清理临时文件和过期数据
"""

import asyncio
import logging

from app.celery_app import celery_app
from app.config import settings
from app.models.database import DatabaseManager, db_manager
from app.services.chat_workspace_cleanup import cleanup_idle_chat_workspaces as remove_idle_workspaces
from app.services.log_service import log_service
from app.utils.temp_directory_cleaner import temp_directory_cleaner

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name='app.tasks.cleanup_tasks.cleanup_temp_directories')
def cleanup_temp_directories(self, processing_max_age: int = 24, extracted_max_age: int = 48):
    """
    清理临时目录的定时任务
    
    Args:
        processing_max_age: 临时处理目录最大保留时间（小时）
        extracted_max_age: 解压文件最大保留时间（小时）
    
    Returns:
        dict: 清理统计信息
    """
    try:
        logger.info(
            f"开始定时清理临时目录: "
            f"processing_max_age={processing_max_age}h, "
            f"extracted_max_age={extracted_max_age}h"
        )
        
        # 执行清理
        stats = temp_directory_cleaner.cleanup_all(
            processing_max_age=processing_max_age,
            extracted_max_age=extracted_max_age
        )
        
        logger.info(
            f"定时清理完成: "
            f"删除 {stats['total_deleted']} 个目录/文件, "
            f"释放空间 {temp_directory_cleaner._format_size(stats['total_freed_space_bytes'])}, "
            f"失败 {stats['total_failed']} 个"
        )
        
        return {
            "status": "success",
            "stats": stats
        }
        
    except Exception as e:
        error_msg = f"定时清理任务失败: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {
            "status": "failed",
            "error": error_msg
        }


@celery_app.task(bind=True, name='app.tasks.cleanup_tasks.cleanup_expired_logs')
def cleanup_expired_logs(self):
    """
    清理超过30天的日志（物理文件 + 数据库记录），固定周期执行。
    """
    try:
        logger.info("开始定时清理超过30天的日志...")

        # 确保数据库初始化
        if db_manager.engine is None or db_manager.session_factory is None:
            db_manager.initialize()

        async def _run_cleanup():
            deleted = 0
            async for session in db_manager.get_session():
                deleted = await log_service.cleanup_expired_logs(session)
            return deleted

        deleted_count = asyncio.run(_run_cleanup())

        logger.info(f"定时清理完成，删除 {deleted_count} 条过期日志")
        return {
            "status": "success",
            "deleted": deleted_count
        }
    except Exception as e:
        logger.error(f"定时清理过期日志失败: {e}", exc_info=True)
        return {
            "status": "failed",
            "error": str(e)
        }


@celery_app.task(name="app.tasks.cleanup_tasks.cleanup_idle_chat_workspaces")
def cleanup_idle_chat_workspaces():
    """每天回收超过 30 天未活动的持久会话工作区。"""

    async def run_cleanup():
        # Celery 每次通过 asyncio.run 创建新事件循环，连接池随任务创建和关闭。
        database = DatabaseManager()
        database.initialize()
        try:
            async with database.session_factory() as session:
                return await remove_idle_workspaces(session, settings.code_repo_clone_base_dir)
        finally:
            await database.engine.dispose()

    stats = asyncio.run(run_cleanup())
    logger.info(
        "会话工作区清理完成，删除 %d 个，跳过 %d 个，失败 %d 个",
        stats["deleted"],
        stats["skipped"],
        stats["failed"],
    )
    return {
        "status": "partial" if stats["failed"] else "success",
        "stats": stats,
    }
