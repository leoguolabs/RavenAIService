"""Backend message catalog for user-facing, server-generated text.

User-facing strings (API success/error/validation messages) are stored here
keyed by ``locale`` then by a stable ``message id`` and looked up through
:func:`t`. Internal logs and developer diagnostics stay inline in code and do
*not* belong in this catalog.

Design notes:

* The catalog is keyed by locale first so a whole-language parity check is a
  simple key-set comparison (see :func:`missing_keys`).
* Every id present in any locale SHALL exist in :data:`DEFAULT` (``zh``); the
  parity test enforces this. :func:`t` still degrades gracefully (falls back to
  ``zh`` and then to the raw key) so a missing variant never raises to a user.
* Values may contain ``str.format`` placeholders; callers pass the values as
  keyword arguments to :func:`t`.
"""

from __future__ import annotations

from typing import Dict

from app.i18n import DEFAULT, normalize

# Catalog: locale -> message id -> template string.
# Keep ids stable; they are referenced from code. Group by area with a prefix.
MESSAGES: Dict[str, Dict[str, str]] = {
    "zh": {
        # ----- file upload validation (app/utils/file_upload_validator.py) -----
        "upload.no_file_selected": "没有选择文件",
        "upload.file_invalid": "文件 {index} ({filename}): {error}",
        "upload.filename_empty": "文件名不能为空",
        "upload.filename_too_long": "文件名长度不能超过255个字符",
        "upload.filename_path_separator": "文件名不能包含路径分隔符",
        "upload.filename_hidden": "不支持隐藏文件",
        "upload.filename_unsafe": "文件名包含不安全的字符或格式不正确，支持的格式：{supported}",
        "upload.file_empty": "文件不能为空",
        "upload.unsupported_type": "不支持的文件类型: {file_type}，支持的类型: {supported}",
        "upload.size_exceeded": "文件大小超出限制: {size}MB > {max}MB",
        "upload.file_too_small": "文件损坏：文件太小",
        "upload.magic_mismatch": "文件损坏：magic number 与扩展名 {ext} 不匹配",
        "upload.unsupported_hash": "不支持的哈希算法: {algorithm}",
        # ----- generic API / auth messages -----
        "error.internal": "服务器内部错误",
        "auth.not_logged_in": "未登录",
        "auth.user_invalid": "用户无效或已禁用",
        "auth.authentication_failed": "身份验证失败",
        "auth.forbidden": "权限不足",
        # ----- users & chat sessions (app/api/users.py) -----
        "auth.login_success": "登录成功",
        "auth.register_success": "注册成功",
        "auth.email_invalid": "请输入有效的邮箱地址",
        "auth.account_disabled": "该账号已被管理员停用，请联系管理员处理。",
        "user.not_found": "用户不存在",
        "user.created": "用户创建成功",
        "user.updated": "用户已更新",
        "user.disabled": "用户已禁用",
        "user.profile_updated": "个人资料已更新",
        "chat.message_saved": "消息已保存",
        # ----- AI chat runs (app/api/ai_chat.py) -----
        "chat.empty_message_no_run": "message 为空且该会话无运行中的 run，无法订阅",
        "chat.user_content_empty": "user_content 不能为空",
        "chat.invalid_decision": "decision 必须为 'allow' 或 'deny'",
        "chat.updated_args_not_object": "updated_args 必须是 JSON object",
        "chat.permission_not_found": "权限请求不存在或已被处理: {request_id}",
        "chat.clarification_not_found": "澄清请求不存在或已被处理: {request_id}",
        "chat.clarification_answers_invalid": "answers 必须是数组，且每个必答问题需至少选择一项或填写自定义内容",
        "chat.no_active_run": "无运行中的 run",
        "chat.run_not_found": "未找到该 run",
        "chat.run_not_found_evicted": "未找到该 run（可能已超出内存保留期，请改用 GET /chat/runs/{run_id}）",
        "chat.run_not_found_or_terminal": "未找到运行中的 run 或已终态",
        "chat.no_running_task": "未找到进行中的任务",
        "project_expert.project_required": "请先选择一个关联项目，再开始向项目专家提问。",
        "session.not_found": "会话不存在",
        "session.deleted": "会话已删除",
        "session.pinned": "已置顶",
        "session.unpinned": "已取消置顶",
        "session.renamed": "已重命名",
        "session.title_empty": "名称不能为空",
        # ----- conversation sharing (app/api/users.py, app/api/share.py) -----
        "share.created": "已生成公开分享链接",
        "share.revoked": "已取消分享",
        # ----- bug fixes (app/api/bug_fixes.py) -----
        "task.not_found": "任务不存在",
        "task.retry_only_failed": "只有失败的 Bug 修复任务可以重试",
        "task.retry_in_progress": "任务已被重试或正在执行，请刷新后查看",
        "task.retry_enqueue_failed": "重试提交失败，请稍后再试",
        "task.retry_queued": "已重新提交 Bug 修复任务",
        # ----- app releases (app/api/releases.py) -----
        "release.invalid_platform": "platform 必须是以下之一: {platforms}",
        "release.version_empty": "version 不能为空",
        "release.save_failed": "文件保存失败: {error}",
        "release.upload_success": "上传成功",
        "release.not_found": "Release 不存在",
        "release.deleted": "已删除",
        "release.file_missing": "文件不存在，可能已被删除",
        # ----- packages (app/api/packages.py) -----
        "package.invalid_package_info_json": "无效的 packageInfo JSON: {error}",
        "package.package_info_not_object": "packageInfo 必须是 JSON object",
        "package.scan_complete": "扫描完成",
        "package.not_found": "包不存在",
        "package.not_found_or_delete_failed": "包不存在或删除失败",
        "package.delete_success": "包删除成功",
        "package.delete_forbidden": "只有管理员可以删除重构包",
        "package.upload_success": "包上传成功",
        "package.no_files_uploaded": "没有上传文件",
        "package.batch_upload_success": "成功上传 {count} 个包",
        "package.ids_required": "缺少包 ID",
        "package.no_valid_packages": "未找到有效的包",
        "package.none_for_criteria": "未找到符合条件的包",
        "package.file_not_found": "包文件不存在",
        "package.query_required": "query 为必填项且必须是字符串",
        "package.query_empty": "query 不能为空",
        "package.query_too_long": "query 超过 {max} 个字符的上限",
        "package.body_not_object": "请求体必须是 JSON object",
        "package.session_id_not_string": "session_id 必须是字符串",
        "package.project_code_required": "projectCode 为必填项，请选择包所属的项目",
        "package.project_invalid": "项目不存在或已停用: {code}",
        "package.project_repo_required": "请先选择项目后再使用配置管理员进行包检索",
        "package.metadata_patch_empty": "请求体需至少包含 description、tags 或 components 之一",
        "package.metadata_description_invalid": "description 必须是字符串或 null",
        "package.metadata_components_invalid": "components 必须是对象数组，组件名为非空字符串，版本为可选字符串",
        "package.metadata_components_too_many": "组件数量不能超过 {max} 个",
        "package.metadata_component_too_long": "组件名或版本不能超过 {max} 个字符",
        "package.metadata_description_too_long": "description 不能超过 {max} 个字符",
        "package.metadata_tags_invalid": "tags 必须是字符串数组",
        "package.metadata_tag_too_long": "单个标签不能超过 {max} 个字符",
        "package.metadata_tags_too_many": "标签数量不能超过 {max} 个",
        "package.metadata_forbidden": "无权编辑该重构包的元数据",
        "package.metadata_update_success": "重构包元数据已更新",
        # ----- logs (app/api/logs.py) -----
        "log.project_not_found_id": "项目不存在或未启用: project_id={project_id}",
        "log.project_not_found_code": "项目不存在或未启用: project_code={project_code}",
        "log.upload_success": "日志上传成功",
        "log.file_validation_failed": "文件验证失败",
        "log.file_size_exceeded": "文件大小超限",
        "log.file_format_error": "文件格式错误",
        "log.file_corrupted": "文件损坏",
        "log.storage_insufficient": "存储空间不足",
        "log.server_error": "服务器错误",
        "log.t04_upload_success": "成功上传 {count} 个文件",
        "log.t04_upload_partial": "成功上传 {uploaded} 个文件，{failed} 个文件失败",
        "log.t04_upload_all_failed": "所有文件上传失败",
        "log.list_success": "获取日志列表成功",
        "log.detail_success": "获取日志详情成功",
        "log.detail_failed": "获取日志详情失败",
        "log.delete_success": "日志删除成功",
        "log.not_ready_for_download": "文件尚未处理完成，无法下载",
        "log.download_failed": "文件下载失败",
        "log.download_count_updated": "下载次数已更新",
        "log.download_count_failed": "下载次数更新失败",
        "log.batch_delete_complete": "批量删除完成: 成功删除 {deleted} 个，失败 {failed} 个",
        "log.batch_download_limit": "批量下载的文件数量不能超过50个",
        "log.batch_download_ready": "批量下载准备完成",
        "log.stream_download_limit": "流式批量下载的文件数量不能超过20个",
        "log.ai_analysis_queued": "AI分析任务已提交，后台将继续运行",
        "log.ai_module_unavailable": "AI分析模块不可用",
        "log.ai_analysis_failed": "AI分析执行失败",
        "log.ai_analysis_error": "AI分析失败",
        "log.ai_status_success": "AI分析状态获取成功",
        "log.ai_status_failed": "查询AI分析状态失败",
        "log.no_ai_task": "该日志暂未发起 AI 分析任务",
        "log.archive_path_missing": "日志归档文件（archive_path）未设置，无法执行 AI 分析。请先上传归档文件。",
        "log.invalid_project_repo": "所选项目仓库不存在或已禁用",
        "log.issue_description_updated": "问题描述已更新",
        "log.issue_description_failed": "问题描述更新失败",
        "log.manual_analysis_saved": "人工分析已保存",
        "log.manual_analysis_failed": "保存人工分析失败",
        "log.error_kind.missing_archive": "日志归档文件缺失，请联系管理员上传归档",
        "log.error_kind.missing_metadata_json": "归档中缺少 metadata.json，无法识别项目信息",
        "log.error_kind.missing_project_identity": "metadata.json 中缺少项目代号字段，请补全上报数据",
        "log.error_kind.project_repo_not_registered": "项目仓库未在系统注册，请管理员在「项目仓库管理」页面添加",
        "log.error_kind.timeout": "AI 分析超时，请联系管理员检查配置或增大超时限制",
    },
    "en": {
        # ----- file upload validation (app/utils/file_upload_validator.py) -----
        "upload.no_file_selected": "No file selected",
        "upload.file_invalid": "File {index} ({filename}): {error}",
        "upload.filename_empty": "Filename cannot be empty",
        "upload.filename_too_long": "Filename cannot exceed 255 characters",
        "upload.filename_path_separator": "Filename cannot contain path separators",
        "upload.filename_hidden": "Hidden files are not supported",
        "upload.filename_unsafe": "Filename contains unsafe characters or has an invalid format; supported formats: {supported}",
        "upload.file_empty": "File cannot be empty",
        "upload.unsupported_type": "Unsupported file type: {file_type}; supported types: {supported}",
        "upload.size_exceeded": "File size exceeds limit: {size}MB > {max}MB",
        "upload.file_too_small": "Corrupted file: file is too small",
        "upload.magic_mismatch": "Corrupted file: magic number does not match extension {ext}",
        "upload.unsupported_hash": "Unsupported hash algorithm: {algorithm}",
        # ----- generic API / auth messages -----
        "error.internal": "Internal server error",
        "auth.not_logged_in": "Not logged in",
        "auth.user_invalid": "Invalid or disabled user",
        "auth.authentication_failed": "Authentication failed",
        "auth.forbidden": "Insufficient permissions",
        # ----- users & chat sessions (app/api/users.py) -----
        "auth.login_success": "Login successful",
        "auth.register_success": "Registration successful",
        "auth.email_invalid": "Please enter a valid email address",
        "auth.account_disabled": "This account has been disabled by an administrator. Please contact them for help.",
        "user.not_found": "User not found",
        "user.created": "User created",
        "user.updated": "User updated",
        "user.disabled": "User disabled",
        "user.profile_updated": "Profile updated",
        "chat.message_saved": "Message saved",
        # ----- AI chat runs (app/api/ai_chat.py) -----
        "chat.empty_message_no_run": "message is empty and the session has no active run to subscribe to",
        "chat.user_content_empty": "user_content cannot be empty",
        "chat.invalid_decision": "decision must be 'allow' or 'deny'",
        "chat.updated_args_not_object": "updated_args must be a JSON object",
        "chat.permission_not_found": "Permission request not found or already resolved: {request_id}",
        "chat.clarification_not_found": "Clarification request not found or already resolved: {request_id}",
        "chat.clarification_answers_invalid": "answers must be an array; each required question needs at least one selected option or custom text",
        "chat.no_active_run": "No active run",
        "chat.run_not_found": "Run not found",
        "chat.run_not_found_evicted": "Run not found (it may have aged out of memory; use GET /chat/runs/{run_id} instead)",
        "chat.run_not_found_or_terminal": "No active run found, or it has already finished",
        "chat.no_running_task": "No running task found",
        "project_expert.project_required": "Please select an associated project before asking the project expert.",
        "session.not_found": "Session not found",
        "session.deleted": "Session deleted",
        "session.pinned": "Pinned",
        "session.unpinned": "Unpinned",
        "session.renamed": "Renamed",
        "session.title_empty": "Name cannot be empty",
        # ----- conversation sharing (app/api/users.py, app/api/share.py) -----
        "share.created": "Public share link generated",
        "share.revoked": "Sharing revoked",
        # ----- bug fixes (app/api/bug_fixes.py) -----
        "task.not_found": "Task not found",
        "task.retry_only_failed": "Only failed bug fix tasks can be retried",
        "task.retry_in_progress": "The task has already been retried or is running; refresh to view its status",
        "task.retry_enqueue_failed": "Failed to submit the retry; please try again later",
        "task.retry_queued": "Bug fix task resubmitted",
        # ----- app releases (app/api/releases.py) -----
        "release.invalid_platform": "platform must be one of: {platforms}",
        "release.version_empty": "version cannot be empty",
        "release.save_failed": "Failed to save file: {error}",
        "release.upload_success": "Upload successful",
        "release.not_found": "Release not found",
        "release.deleted": "Deleted",
        "release.file_missing": "File not found; it may have been deleted",
        # ----- packages (app/api/packages.py) -----
        "package.invalid_package_info_json": "Invalid packageInfo JSON: {error}",
        "package.package_info_not_object": "packageInfo must be a JSON object",
        "package.scan_complete": "Scan complete",
        "package.not_found": "Package not found",
        "package.not_found_or_delete_failed": "Package not found or could not be deleted",
        "package.delete_success": "Package deleted",
        "package.delete_forbidden": "Only administrators can delete packages",
        "package.upload_success": "Package uploaded",
        "package.no_files_uploaded": "No files uploaded",
        "package.batch_upload_success": "Successfully uploaded {count} package(s)",
        "package.ids_required": "Package IDs are required",
        "package.no_valid_packages": "No valid packages found",
        "package.none_for_criteria": "No packages found for the specified criteria",
        "package.file_not_found": "Package file not found",
        "package.query_required": "query is required and must be a string",
        "package.query_empty": "query must not be empty",
        "package.query_too_long": "query exceeds {max}-character limit",
        "package.body_not_object": "request body must be a JSON object",
        "package.session_id_not_string": "session_id must be a string",
        "package.project_code_required": "projectCode is required; choose the project this package belongs to",
        "package.project_invalid": "Project not found or disabled: {code}",
        "package.project_repo_required": "Select a project before using Configuration Manager for package search",
        "package.metadata_patch_empty": "Provide at least one of description, tags or components",
        "package.metadata_components_invalid": "components must be an array of objects with a non-empty string name and optional string version",
        "package.metadata_components_too_many": "Component count cannot exceed {max}",
        "package.metadata_component_too_long": "Component name or version cannot exceed {max} characters",
        "package.metadata_description_invalid": "description must be a string or null",
        "package.metadata_description_too_long": "description must not exceed {max} characters",
        "package.metadata_tags_invalid": "tags must be an array of strings",
        "package.metadata_tag_too_long": "each tag must not exceed {max} characters",
        "package.metadata_tags_too_many": "no more than {max} tags are allowed",
        "package.metadata_forbidden": "You are not allowed to edit this package's metadata",
        "package.metadata_update_success": "Package metadata updated",
        # ----- logs (app/api/logs.py) -----
        "log.project_not_found_id": "Project not found or disabled: project_id={project_id}",
        "log.project_not_found_code": "Project not found or disabled: project_code={project_code}",
        "log.upload_success": "Log uploaded successfully",
        "log.file_validation_failed": "File validation failed",
        "log.file_size_exceeded": "File size exceeded",
        "log.file_format_error": "Invalid file format",
        "log.file_corrupted": "File corrupted",
        "log.storage_insufficient": "Insufficient storage space",
        "log.server_error": "Server error",
        "log.t04_upload_success": "Successfully uploaded {count} file(s)",
        "log.t04_upload_partial": "Successfully uploaded {uploaded} file(s); {failed} file(s) failed",
        "log.t04_upload_all_failed": "All files failed to upload",
        "log.list_success": "Log list retrieved",
        "log.detail_success": "Log detail retrieved",
        "log.detail_failed": "Failed to retrieve log detail",
        "log.delete_success": "Log deleted",
        "log.not_ready_for_download": "File is not ready for download yet",
        "log.download_failed": "File download failed",
        "log.download_count_updated": "Download count updated",
        "log.download_count_failed": "Failed to update download count",
        "log.batch_delete_complete": "Batch delete complete: {deleted} deleted, {failed} failed",
        "log.batch_download_limit": "Batch download is limited to 50 files",
        "log.batch_download_ready": "Batch download ready",
        "log.stream_download_limit": "Streaming batch download is limited to 20 files",
        "log.ai_analysis_queued": "AI analysis task submitted; it will continue running in the background",
        "log.ai_module_unavailable": "AI analysis module unavailable",
        "log.ai_analysis_failed": "AI analysis execution failed",
        "log.ai_analysis_error": "AI analysis failed",
        "log.ai_status_success": "AI analysis status retrieved",
        "log.ai_status_failed": "Failed to retrieve AI analysis status",
        "log.no_ai_task": "No AI analysis task has been started for this log",
        "log.archive_path_missing": "Log archive path (archive_path) is not set; AI analysis cannot run. Please upload the archive first.",
        "log.invalid_project_repo": "The selected project repository does not exist or is disabled",
        "log.issue_description_updated": "Issue description updated",
        "log.issue_description_failed": "Failed to update issue description",
        "log.manual_analysis_saved": "Manual analysis saved",
        "log.manual_analysis_failed": "Failed to save manual analysis",
        "log.error_kind.missing_archive": "Log archive is missing; please ask an admin to upload the archive",
        "log.error_kind.missing_metadata_json": "metadata.json is missing from the archive; cannot identify project information",
        "log.error_kind.missing_project_identity": "Project code field is missing from metadata.json; please complete the reported data",
        "log.error_kind.project_repo_not_registered": "Project repository is not registered in the system; please ask an admin to add it in Project Repository Management",
        "log.error_kind.timeout": "AI analysis timed out; please ask an admin to check the configuration or increase the timeout limit",
    },
}


def t(key: str, locale: str | None = None, **fmt: object) -> str:
    """Translate a message ``key`` into ``locale``.

    Resolution order: requested locale → :data:`DEFAULT` (``zh``) → the raw
    ``key`` itself. ``locale`` is normalized, so loose inputs like ``"en-US"``
    are accepted. Any ``**fmt`` values are applied with :meth:`str.format`;
    formatting failures degrade to the unformatted template rather than raising.
    """
    code = normalize(locale)
    template = MESSAGES.get(code, {}).get(key)
    if template is None and code != DEFAULT:
        template = MESSAGES.get(DEFAULT, {}).get(key)
    if template is None:
        return key
    if not fmt:
        return template
    try:
        return template.format(**fmt)
    except (KeyError, IndexError, ValueError):
        return template


def missing_keys() -> Dict[str, set[str]]:
    """Return, per locale, the message ids that locale is missing.

    The union of all ids across locales is the expected key set; any locale not
    containing an id appears in the result. An empty dict means full parity.
    Used by the catalog-parity test.
    """
    all_ids: set[str] = set()
    for catalog in MESSAGES.values():
        all_ids |= catalog.keys()
    gaps: Dict[str, set[str]] = {}
    for code, catalog in MESSAGES.items():
        missing = all_ids - catalog.keys()
        if missing:
            gaps[code] = missing
    return gaps
