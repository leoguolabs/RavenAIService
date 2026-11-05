"""
应用配置模块
支持环境变量配置和开发/生产环境切换
"""

import hmac
import os
from pathlib import Path
from typing import List, Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# 加载环境变量（优先使用容器内挂载的 .env）
load_dotenv(dotenv_path=".env", override=True)


# 运行期可由后台「模型设置」覆盖的键。.env / 下面的字段默认值仅作为
# 引导默认值（bootstrap default）；一旦 Admin 在后台保存了同名覆盖，读取
# 这些属性时会透明地返回后台持久化的值（见 Settings.__getattribute__），
# 无需重启即可生效。未被覆盖的键仍回退到 .env / 字段默认值，保证向后兼容。
# 该集合是「唯一真源」在 app.services.model_settings_service 定义处的镜像；
# 两处必须保持一致（model_settings_service 在导入时会断言校验）。
OVERRIDABLE_MODEL_KEYS: frozenset = frozenset(
    {
        # 主力 Anthropic 兼容模型
        "anthropic_provider",
        "anthropic_api_key",
        "anthropic_api_keys",
        "anthropic_base_url",
        "anthropic_model",
        "anthropic_small_fast_model",
        "anthropic_max_tokens",
        # 备用 Anthropic 兼容模型（主力降级时的故障转移目标）
        "anthropic_backup_enabled",
        "anthropic_backup_provider",
        "anthropic_backup_api_key",
        "anthropic_backup_base_url",
        "anthropic_backup_model",
        "anthropic_backup_small_fast_model",
        # 路由策略阈值。与主备端点同store：runtime_settings.json 落在 app_data 卷上，
        # backend / worker / worker-bugfix / beat 四个服务共享，且读取按 mtime 失效，
        # 因此各进程看到的阈值天然一致——和 API Key 用的是同一条通路。
        # 危险在于取值本身（填太低会把 100% 流量打到付费端点），由
        # model_settings_service 的取值范围 + 跨字段校验兜住。
        "model_router_enabled",
        "model_router_slow_ttft_ms",
        "model_router_first_token_deadline_ms",
        "model_router_window_size",
        "model_router_trip_threshold",
        "model_router_min_samples",
        "model_router_hard_failure_trip",
        "model_router_cooldown_seconds",
        "model_router_sample_ttl_seconds",
        # BugFix 独立端点和执行预算
        "bug_fix_agent_provider",
        "bug_fix_agent_api_key",
        "bug_fix_agent_base_url",
        "bug_fix_agent_model",
        "bug_fix_agent_small_fast_model",
        "bug_fix_agent_max_tokens",
        "bug_fix_agent_max_turns",
        "bug_fix_agent_request_timeout_seconds",
        # OCR / 视觉模型
        "ocr_enabled",
        "ocr_api_key",
        "ocr_base_url",
        "ocr_model",
        "ocr_provider",
    }
)


class Settings(BaseSettings):
    """应用配置类"""

    def __getattribute__(self, name: str):
        """Overlay Admin runtime overrides on top of the .env-loaded value.

        Only the whitelisted model keys are intercepted; every other attribute
        access (including all pydantic internals) falls straight through to the
        normal lookup. When the backing runtime store holds an override for the
        requested key, that value wins; otherwise the .env / field default is
        returned unchanged — so behaviour is identical to before when Admin has
        not configured anything.
        """
        if name in OVERRIDABLE_MODEL_KEYS:
            # 延迟导入打破循环依赖：model_settings_service → runtime_settings_service
            # → app.config。此分支只在运行期读取受控键时触发，此时模块已完成加载。
            try:
                from app.services.model_settings_service import get_override

                found, value = get_override(name)
                if found:
                    return value
            except Exception:  # noqa: BLE001 — 覆盖层永不能让配置读取失败
                pass
        return super().__getattribute__(name)

    # 环境配置
    environment: str = "development"
    base_dir: str = str(Path(__file__).resolve().parent.parent)

    # 服务配置
    host: str = "0.0.0.0"
    port: int = 8085
    serve_frontend: bool = False
    frontend_dist_dir: Optional[str] = None

    # 对话分享公开页（系统首个未鉴权读取面）
    # 公开分享链接 share_url 的站点根地址，如 https://ravenai.example.com。
    # 留空时由 API 回退请求 Origin / Host 拼接，便于本地与多域名部署。
    public_base_url: Optional[str] = None
    # 公开 GET /share/{token} 端点的按 IP 基础限流（抑制 token 空间扫描枚举）。
    share_public_rate_limit: int = 60  # 时间窗内允许的最大请求数
    share_public_rate_window_seconds: int = 60  # 限流时间窗（秒）
    # 设备长链接配置（仅暴露常量，后续实现 WebSocket 心跳与超时逻辑）
    device_link_heartbeat_sec: int = 30  # 环境变量: DEVICE_LINK_HEARTBEAT_SEC
    device_link_timeout_sec: int = 120  # 环境变量: DEVICE_LINK_TIMEOUT_SEC
    device_link_store_file: str = "data/device_links.json"

    # 日志配置
    log_level: str = "INFO"
    log_file_path: str = "logs/app.log"
    debug_log_file_path: str = "logs/debug.log"
    console_log_level: str = "INFO"
    log_file_max_bytes: int = 50 * 1024 * 1024  # 50MB 默认单个日志文件上限
    log_file_backup_count: int = 5  # 保留的滚动日志文件数量
    enable_debug_file_log: bool = False  # 默认关闭单独的debug文件

    # 文件配置
    max_file_size: int = 1073741824  # 1GB
    temp_dir: str = "temp"
    logs_dir: str = "logs"
    disk_reserve_bytes: int = 512 * 1024 * 1024  # 默认预留512MB空间，防止磁盘写满

    # Raven 包管理配置（已统一到 FastAPI 后端）
    raven_base_path: str = "/raven"
    raven_enable_legacy_paths: bool = True
    raven_data_dir: str = "data/raven"
    raven_metadata_file: str = "data/raven/package-metadata.json"
    upload_dir: str = "data/raven/uploads"
    upload_max_size_mb: int = 500

    # Package search Agent 配置（Claude Agent SDK 驱动的重构包智能检索）
    package_search_max_turns: int = 8
    package_search_default_limit: int = 5
    package_search_max_limit: int = 50

    # Agent配置（日志分析智能体）
    agent_enabled: bool = True
    agent_root_dir: str = "."  # 允许Agent访问的日志根目录
    agent_max_snippet_bytes: int = 512 * 1024  # 每次提取的最大字节数
    agent_max_matches: int = 50  # grep最大匹配数
    agent_search_backend: str = "regex"  # 可选：regex | elasticsearch
    elasticsearch_url: Optional[str] = None

    # 代码仓库配置（用于代码分析智能体克隆源码）
    code_repo_clone_base_dir: str = "temp/code_repos"
    code_repo_git_token: Optional[str] = (
        None  # 全局 Git Token（私有仓库认证，可被 project_repo 单独 token 覆盖）
    )
    # Local experiment: omit unused SDK session titles/nonessential traffic.
    agent_sdk_disable_nonessential_traffic: bool = False
    agent_analysis_confidence_bounds_enabled: bool = False
    agent_analysis_batch_prompt_enabled: bool = False
    agent_analysis_prepare_primary_repo: bool = False
    agent_analysis_stage_checkpoint_enabled: bool = False
    agent_related_repo_max_count: int = 4
    agent_repo_clone_timeout_seconds: int = 300
    # Opt-in until paired latency and evidence-equivalence validation passes.
    agent_evidence_efficiency_enabled: bool = False
    # Local prompt experiment: dispatch independent text searches in one MCP batch.
    agent_evidence_batch_plan_enabled: bool = False
    # Separate local prompt experiment: small batches and gap-led follow-up.
    agent_evidence_batch_plan_v3_enabled: bool = False
    # Separate local prompt experiment: front-load independent checks per search turn.
    agent_evidence_batch_plan_v4_enabled: bool = False
    agent_evidence_regex_endpoints_enabled: bool = False
    agent_evidence_compare_enabled: bool = False
    # Independently opt in: added context/review failed the combined trial.
    agent_evidence_source_context_enabled: bool = False
    agent_evidence_final_review_enabled: bool = False
    agent_evidence_progress_budget_enabled: bool = False
    agent_evidence_final_review_mode: str = "session"  # session | isolated; review gate remains default off
    agent_evidence_log_overview_enabled: bool = False
    agent_evidence_statistics_enabled: bool = False
    agent_analysis_standalone_statistics_enabled: bool = False
    agent_analysis_native_search_counts_enabled: bool = False
    agent_evidence_topic_context_enabled: bool = False
    agent_evidence_search_endpoints_enabled: bool = False
    agent_evidence_read_window_enabled: bool = False
    # Independent SDK capacity experiment; does not require an evidence snapshot.
    agent_evidence_extended_context_enabled: bool = False
    # Local experiment: same-session collection/finalization, verified SDK only.
    agent_evidence_staged_effort_enabled: bool = False

    # 上下文压缩/记忆配置
    agent_compression_strategy: str = (
        "map_reduce_summarize"  # 可选：map_reduce_summarize | extractive | hybrid
    )
    agent_short_term_window: int = 5  # 短时记忆窗口消息条数

    # Anthropic 标准 LLM 配置（供 Claude Agent SDK 使用）
    anthropic_provider: str = "deepseek"  # anthropic | deepseek | custom
    anthropic_api_key: Optional[str] = None
    # 主力 API Key 池。非空时优先于上面的兼容单 Key；环境变量使用 JSON 数组，
    # 例如 ANTHROPIC_API_KEYS=["sk-a","sk-b"]。后台运行期配置同样保存原生数组。
    anthropic_api_keys: List[str] = []
    anthropic_base_url: Optional[str] = None  # None 时由 provider profile 提供
    anthropic_model: Optional[str] = None  # None 时由 provider profile 提供
    anthropic_small_fast_model: Optional[str] = None
    anthropic_max_tokens: int = 8192
    anthropic_max_turns: int = 30
    anthropic_permission_mode: str = "acceptEdits"
    anthropic_request_timeout_seconds: int = 3600
    anthropic_max_history_turns: int = 10
    anthropic_small_fast_max_tokens: int = 1024
    anthropic_small_fast_request_timeout_seconds: int = 30
    general_agent_max_turns: int = 6
    ai_analysis_max_extract_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB

    # 备用 Anthropic 兼容端点（故障转移目标）。字段结构与主力一组完全对称，
    # 由 Admin「模型设置」独立配置。anthropic_backup_enabled 为 False（默认）时
    # model_router 只返回主力一个候选，行为与未引入路由前完全一致。
    # 注意：max_turns / permission_mode / 各类超时等调参项不分主备，两个端点共用。
    anthropic_backup_enabled: bool = False
    anthropic_backup_provider: Optional[str] = None  # None/空 视为「未配置」
    anthropic_backup_api_key: Optional[str] = None
    anthropic_backup_base_url: Optional[str] = None  # None 时由 provider profile 提供
    anthropic_backup_model: Optional[str] = None  # None 时由 provider profile 提供
    anthropic_backup_small_fast_model: Optional[str] = None

    # 模型端点路由（延迟感知预选 + 熔断器）。全部可在 Admin「模型设置」运行时调整
    # （见 OVERRIDABLE_MODEL_KEYS），.env 只是启动兜底；跨字段约束在
    # model_settings_service._validate_router 中强制。
    # 总开关（kill switch）。开启后仍需在 Admin 配置并启用备用端点才会真正转移：
    # 未配置备用时 candidates 只返回主力、熔断器不跳闸，等价于「只观测 TTFT」。
    model_router_enabled: bool = True
    model_router_slow_ttft_ms: int = 6000  # 首 token 超过此值即计一次「慢」
    # 首 token 硬性抢占死线：等待超过此值即放弃当前端点、立刻改用下一个候选。
    # 与 slow_ttft_ms 的区别：后者是**事后**给样本打「慢」标记，救不了当前这一条
    # 请求；只有本项能给用户可感知的等待时间设上限。仅在「尚未提交」且「还有下一个
    # 候选」时生效——最后一个候选不抢占，否则用户什么都拿不到。设为 0 关闭抢占。
    model_router_first_token_deadline_ms: int = 20000
    model_router_window_size: int = 8  # 滚动窗口样本数
    model_router_trip_threshold: int = 4  # 窗口内「慢或失败」达到此数即跳闸
    model_router_min_samples: int = 4  # 样本不足此数永不跳闸
    model_router_hard_failure_trip: int = 2  # 连接/鉴权类硬失败的快速跳闸阈值
    model_router_cooldown_seconds: int = 120  # 熔断冷却 = 半开探测令牌 TTL
    model_router_sample_ttl_seconds: int = 900  # 窗口 TTL，避免闲置整夜后残留陈旧状态

    # OCR / 视觉理解模型（独立于主力 Anthropic 模型，走 OpenAI 兼容端点，默认
    # 对接阿里云百炼 DashScope Qwen-VL）。用户粘贴的图片先由此模型转成文字，再
    # 以 <user_image_ocr> 段合并进用户提示，下游各 Agent 零改动。全部可选、带
    # 安全默认；未配置 OCR_API_KEY 时对图片自动降级。
    ocr_enabled: bool = True  # 总开关；False 时无条件降级
    ocr_provider: str = "dashscope"  # 计量/日志标签
    ocr_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ocr_api_key: Optional[str] = None  # 未设置即视为「未配置」→ 降级
    ocr_model: str = "qwen3.5-ocr"  # 可设 qwen-vl-ocr-latest / qwen-vl-ocr
    ocr_max_tokens: int = 2048  # 单次输出上限
    ocr_request_timeout_seconds: int = 30  # 单次请求超时
    ocr_max_images: int = 6  # 单轮图片数上限
    ocr_max_image_mb: int = 5  # 单图大小上限（MB）

    # 用户随消息附带图片的原图存储。图片按 <session_id>/<image_id>.<ext> 落盘，
    # chat_messages.images_json 只存元数据，历史回显经鉴权端点回图。目录随会话
    # 删除而清理（见 chat_image_store）。相对路径按 base_dir 解析。
    chat_image_store_dir: str = "temp/chat_images"
    # 是否把原图物化到 Agent 工作区 <workspace>/images/（为后续多模态铺路）。
    # 仅在 provider 的 supports_image_input 为真时生效——非视觉上游若 Read 到
    # 图片文件会导致整个 run 报错。
    chat_image_workspace_materialize: bool = True

    # Bug Fix Coding Agent（分析判定需要代码修复时自动派发的写入型 Agent）
    bug_fix_auto_dispatch: bool = False  # 自动派发总开关，默认关闭，灰度可控
    bug_fix_agent_provider: Optional[str] = 'deepseek'
    bug_fix_agent_api_key: Optional[str] = None
    bug_fix_agent_base_url: Optional[str] = None
    bug_fix_agent_model: Optional[str] = 'deepseek-v4-pro'
    bug_fix_agent_small_fast_model: Optional[str] = None
    bug_fix_agent_max_tokens: int = 16384
    bug_fix_agent_max_turns: int = 150
    bug_fix_agent_request_timeout_seconds: int = 3600
    bug_fix_context_dir: str = "data/bug_fix_context"
    bug_fix_git_provider: Optional[str] = (
        None  # None 时由 repo_url host 推断（gitlab|github）
    )
    bug_fix_git_api_base: Optional[str] = None  # None 时由 repo_url host 推断

    # DeviceAgent 专属（Claude Agent SDK 设备联动对话）
    device_agent_permission_timeout_seconds: int = 120
    device_agent_result_excerpt_bytes: int = 16 * 1024
    device_agent_result_max_bytes: int = 256 * 1024
    device_agent_max_remote_tools: int = 64
    # Deprecated alias for agent_clarification_timeout_seconds below; kept so
    # deployments whose .env still sets it keep working.
    device_agent_clarification_timeout_seconds: int = 300

    # AskUserQuestion 澄清提问等待时长：代码常量、非用户可改，默认 5 分钟。
    # 超时后的行为（取消本轮 / 基于已知信息继续）由用户偏好决定。
    # 适用于全部对话型 Agent（device / log_analysis / project_expert /
    # package_search），与用户的全局「Agent 澄清提问」偏好配套。
    agent_clarification_timeout_seconds: int = 300

    # Agent Skills 数据目录（Claude Agent SDK Skill 包按 agent 隔离存储）
    skills_data_dir: str = "data/agent_skills"

    # Project Skills 数据目录（按 project_code 隔离存储，与 Agent Skills 平行）
    project_skills_data_dir: str = "data/project_skills"

    # Project 系统提示词数据目录（按 project_code 隔离存储；项目级追加提示词，
    # 与 Project Skills 平行，让系统提示词也能像 Skill 一样分级处理）
    project_prompts_data_dir: str = "data/project_prompts"

    # 运行期可由 Admin 调整的轻量级模型设置持久化文件
    runtime_settings_path: str = "data/runtime_settings.json"

    # Metrics 成本估算价格配置（可选）。默认空表示不估价，成本字段返回 null。
    # 格式：{"<provider>": {"<model>": {"input_per_million": 3.0,
    #        "output_per_million": 15.0, "cache_read_per_million": 0.3,
    #        "cache_write_per_million": 3.75}}}
    # 单价单位为“每 100 万 token 的美元价格”。
    ai_metrics_pricing_json: Optional[str] = None

    def get_ai_metrics_pricing(self) -> dict:
        """解析 ai_metrics_pricing_json，返回 provider->model->token_type 价格映射。

        解析失败或未配置时返回空 dict（即不估价）。永不抛出。
        """
        import json

        raw = self.ai_metrics_pricing_json
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:  # noqa: BLE001
            pass
        return {}

    @field_validator("anthropic_provider")
    @classmethod
    def validate_anthropic_provider(cls, v: str) -> str:
        """校验 .env 引导值落在已知 provider profile 内。

        名单从 PROVIDER_PROFILES 惰性读取而非硬编码：此前写死的
        {anthropic, deepseek, custom} 早已与 profile 表脱节，导致想在 .env 里直接
        写 yinhe / moonshot 等 provider 的部署只能退而求其次填 custom —— 而 custom
        的 supports_partial_streaming=False，会静默关掉增量流式。

        仅作用于 .env / 构造函数路径；Admin 运行期覆盖绕过 pydantic，由
        model_settings_service.save 按 ANTHROPIC_PROVIDERS 校验。
        导入失败时放行，避免让配置层反过来依赖 agents 层的可用性。
        """
        try:
            from app.agents.anthropic_client import PROVIDER_PROFILES
        except Exception:  # noqa: BLE001 — 校验永不能让应用无法启动
            return v
        if v not in PROVIDER_PROFILES:
            raise ValueError(
                f"anthropic_provider must be one of {sorted(PROVIDER_PROFILES)}, got '{v}'"
            )
        return v

    @field_validator("anthropic_backup_provider")
    @classmethod
    def validate_anthropic_backup_provider(cls, v: Optional[str]) -> Optional[str]:
        """备用 provider 同样只接受已知 profile；空值表示「未配置」。"""
        if not v:
            return v
        return cls.validate_anthropic_provider(v)

    # Prompt配置（外部化模板路径，可通过环境变量覆盖）
    prompts_config_path: str = "app/prompts/prompts_config.yaml"

    # 后台管理配置
    admin_auth_config_path: str = "app/admin_auth.yaml"
    admin_token_ttl_minutes: int = 120
    user_token_ttl_minutes: int = 60 * 24 * 7

    # CORS配置
    cors_origins: List[str] = ["*"]
    cors_credentials: bool = True
    cors_methods: List[str] = ["*"]
    cors_headers: List[str] = ["*"]

    # 安全配置
    secret_key: str = "your-secret-key-here"
    # 整包确认签名是发布权限边界，生产环境必须使用与登录
    # token 分离的随机密钥。开发环境为了现有测试兼容可回退 secret_key。
    package_confirmation_secret: Optional[str] = None

    # 数据库配置
    database_url: Optional[str] = None
    database_echo: bool = False
    database_pool_size: int = 5
    database_max_overflow: int = 10
    database_pool_timeout: int = 30
    database_pool_recycle: int = 3600

    # Celery配置
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/0"
    celery_task_serializer: str = "json"
    celery_result_serializer: str = "json"
    celery_accept_content: List[str] = ["json"]
    celery_timezone: str = "UTC"
    celery_enable_utc: bool = True

    # Redis配置
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None

    # 协议栈日志处理配置
    log_processing_speed_mb_per_sec: int = 100  # 假设处理速度100MB/s
    max_retry_attempts: int = 3
    task_timeout: int = 3600  # 1小时超时
    thread_num_for_decompress: int = 4  # 默认线程数
    repackage_use_pigz: bool = True  # 优先使用pigz并行压缩
    repackage_pigz_threads: int = 0  # 0表示自动按CPU核心数选择
    repackage_compress_level: int = 6  # pigz缺失时tarfile的压缩等级(1-9)

    # SQLite配置（开发环境）
    # 默认放在 data 目录，避免与代码目录冲突，方便卷持久化
    sqlite_file: str = "data/logs.db"

    # PostgreSQL配置（生产环境）
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "log_staging"
    postgres_user: str = "postgres"
    postgres_password: str = "password"

    def get_database_url(self) -> str:
        """获取数据库连接URL"""
        if self.database_url:
            return self.database_url

        if self.environment == "production":
            return f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        else:
            return f"sqlite+aiosqlite:///{self.sqlite_file}"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"  # 忽略额外的环境变量


class DevelopmentSettings(Settings):
    """开发环境配置"""

    environment: str = "development"
    log_level: str = "DEBUG"


class ProductionSettings(Settings):
    """生产环境配置"""

    environment: str = "production"
    log_level: str = "WARNING"
    cors_origins: List[str] = []  # 生产环境需要配置具体的域名


def get_settings() -> Settings:
    """获取配置实例"""
    env = os.getenv("ENVIRONMENT", "development").lower()

    if env == "production":
        configured = ProductionSettings()
        confirmation_secret = str(configured.package_confirmation_secret or "").strip()
        if (
            len(confirmation_secret.encode("utf-8")) < 32
            or confirmation_secret == "your-secret-key-here"
            or hmac.compare_digest(
                confirmation_secret.encode("utf-8"),
                str(configured.secret_key).encode("utf-8"),
            )
        ):
            raise RuntimeError(
                "PACKAGE_CONFIRMATION_SECRET must be an independent random "
                "secret of at least 32 bytes in production"
            )
        return configured
    else:
        return DevelopmentSettings()


# 全局配置实例
settings = get_settings()
