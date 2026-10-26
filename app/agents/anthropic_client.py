"""
Anthropic 标准 LLM 配置层与 ClaudeAgentOptions 构建入口。

支持多个上游服务商（provider），包括 `anthropic`、`deepseek` 与
Anthropic-compatible `custom` profile。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


_UNEXPECTED_KWARG_RE = re.compile(r"unexpected keyword argument '([^']+)'")


def _instantiate_options(cls: Any, options_kwargs: Dict[str, Any]) -> Any:
    """Instantiate SDK options, dropping optional kwargs unsupported by older SDKs."""
    kwargs = dict(options_kwargs)
    dropped: List[str] = []
    while True:
        try:
            options = cls(**kwargs)
            if dropped:
                logger.warning(
                    "ClaudeAgentOptions does not support %s; parameter(s) dropped.",
                    ", ".join(dropped),
                )
            return options
        except TypeError as exc:
            match = _UNEXPECTED_KWARG_RE.search(str(exc))
            if not match:
                raise
            key = match.group(1)
            if key not in kwargs:
                raise
            dropped.append(key)
            kwargs.pop(key, None)


# ─────────────────────── Exceptions ────────────────────────────────

class AnthropicConfigurationError(Exception):
    """Anthropic 配置不完整或非法时抛出。"""


class ProviderCapabilityError(Exception):
    """调用方请求的特性在当前 provider 下不被支持时抛出。"""


# ─────────────────────── Provider Profile ──────────────────────────

@dataclass(frozen=True)
class ProviderProfile:
    name: str
    default_base_url: str
    default_model: str
    default_small_fast_model: Optional[str]
    supports_image_input: bool
    supports_document_input: bool
    supports_mcp_server_tools: bool
    thinking_budget_tokens_effective: bool
    disable_parallel_tool_use_effective: bool
    # Whether the upstream supports SDK chunked streaming
    # (``include_partial_messages`` → native ``content_block_delta`` events).
    # When False, callers requesting partial streaming are silently degraded:
    # no ``answer_delta`` is produced and the client falls back to
    # ``run_complete.final_text``.
    supports_partial_streaming: bool = False
    notes: str = ""
    # Human-readable vendor name for the Admin「模型设置」provider dropdown.
    label: str = ""
    # Known model ids for this upstream, offered as presets in the Admin form.
    # Never a whitelist — ``anthropic_model`` stays free-text so a newly shipped
    # model can be used before this table catches up.
    models: tuple = ()


# 除 anthropic 官方端点外，其余均为 Anthropic-compatible 第三方网关：它们实现
# Messages API（/v1/messages + SSE），因此 Claude Agent SDK 的工具循环、进程内
# MCP server 与增量流式均可用；但扩展能力（thinking budget、文档输入、并行工具
# 调用开关）通常不生效，故一律置 False，由 build_options 静默降级。
#
# supports_image_input 以该 provider 的**默认旗舰模型**为准（决定是否把原图物化
# 进 Agent 工作区，见 chat_image_store）。若在同一 provider 下改选纯文本模型
# （如 qwen3-coder-*），请依赖上方 OCR 链路而不是主力模型读图。
# 公网预设目录核对于 2026-10-20；按各端点的官方支持范围维护，而非厂商全部产品。
# 更新预设不迁移管理员已保存的模型；仅替换正在退出服务的默认候选。
# yhroot / yinhe 内部模型不参与公网目录更新；custom 仍由管理员填写。
PROVIDER_PROFILES: Dict[str, ProviderProfile] = {
    "anthropic": ProviderProfile(
        name="anthropic",
        label="Anthropic 官方",
        default_base_url="https://api.anthropic.com",
        default_model="claude-sonnet-4-6",
        default_small_fast_model="claude-haiku-4-5-20251001",
        # https://platform.claude.com/docs/en/about-claude/model-deprecations
        models=(
            "claude-fable-5-1",
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
            "claude-opus-5",
            "claude-sonnet-5",
            "claude-fable-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-opus-4-5-20251101",
            "claude-sonnet-4-6",
            "claude-sonnet-4-5-20250929",
            "claude-haiku-4-5-20251001",
        ),
        supports_image_input=True,
        supports_document_input=True,
        supports_mcp_server_tools=True,
        thinking_budget_tokens_effective=True,
        disable_parallel_tool_use_effective=True,
        supports_partial_streaming=True,
        notes="Anthropic 官方端点，能力全开",
    ),
    "deepseek": ProviderProfile(
        name="deepseek",
        label="DeepSeek 深度求索",
        default_base_url="https://api.deepseek.com/anthropic",
        default_model="deepseek-v4-pro",
        default_small_fast_model="deepseek-v4-flash",
        # https://api-docs.deepseek.com/quick_start/pricing/
        # Flash 旧名称仍被接受，实际路由至 DeepSeek-V4.1-Flash-1017。
        models=(
            "deepseek-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
        ),
        supports_image_input=False,
        supports_document_input=False,
        # SDK 进程内 MCP server（create_sdk_mcp_server）通过标准 tool_use 协议
        # 暴露给模型，与上游是否支持 API 端 mcp_servers 透传无关。
        # DeepSeek Anthropic 兼容端点支持标准 tool use，因此可启用。
        supports_mcp_server_tools=True,
        thinking_budget_tokens_effective=False,
        disable_parallel_tool_use_effective=False,
        # Anthropic 兼容端点支持标准 SSE 流式（content_block_delta），可发 answer_delta。
        supports_partial_streaming=True,
        notes="DeepSeek Anthropic 兼容端点；默认 V4 Pro 不支持图像/文档输入，"
        "不支持 thinking budget；"
        "支持标准 tool use（含 SDK 进程内 MCP server）；deepseek-flash 及 Flash 旧别名"
        "指向 V4.1 Flash 1017，deepseek-v4-pro 仍为 V4 Pro 0813",
    ),
    "aliyun": ProviderProfile(
        name="aliyun",
        label="阿里云百炼 / 通义千问",
        # {WorkspaceId} 必须由管理员替换为自己的百炼工作空间 ID，否则请求打到
        # 不存在的域名。model_settings_service.save 会拒绝残留占位符的地址。
        default_base_url="https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/apps/anthropic",
        default_model="qwen3.7-max",
        default_small_fast_model="qwen3.7-flash",
        # https://help.aliyun.com/zh/model-studio/anthropic-api-messages
        # 含百炼托管的第三方模型；其生命周期与原厂直连目录分别维护。
        models=(
            "qwen3.8-max",
            "qwen3.8-max-0902",
            "qwen3.8-flash",
            "qwen3.7-max",
            "qwen3.7-max-2026-05-20",
            "qwen3.7-max-2026-06-08",
            "qwen3.7-plus",
            "qwen3.7-plus-2026-05-26",
            "qwen3.6-plus",
            "qwen3.6-plus-2026-04-02",
            "qwen3.5-plus",
            "qwen3.5-plus-2026-04-20",
            "qwen3.5-plus-2026-02-15",
            "qwen-plus",
            "qwen-plus-latest",
            "qwen3.7-flash",
            "qwen3.7-flash-2026-07-15",
            "qwen3.6-flash",
            "qwen3.6-flash-2026-04-16",
            "qwen3.5-flash",
            "qwen3.5-flash-2026-02-23",
            "qwen-flash",
            "qwen-flash-2025-07-28",
            "qwen3-coder-flash",
            "qwen3-vl-plus",
            "qwen3.6-27b",
            "qwen3.5-397b-a17b",
            "qwen3.5-122b-a10b",
            "qwen3.5-27b",
            "qwen3.5-35b-a3b",
            "qwen3.8-2.4t-a95b",
            "qwen3.8-27b",
            "deepseek-v4.1-flash",
            "deepseek-v4.1-flash-1017",
            "deepseek-v4-pro",
            "deepseek-v4-pro-0813",
            "deepseek-v4-flash-0731",
            "kimi-k3",
            "kimi-k2.7-code",
            "kimi-k2.6",
            "kimi-k2.5",
            "glm-5.3",
            "glm-5.2",
            "glm-5.1",
            "glm-5",
            "MiniMax-M2.5",
        ),
        supports_image_input=True,
        supports_document_input=False,
        supports_mcp_server_tools=True,
        thinking_budget_tokens_effective=False,
        disable_parallel_tool_use_effective=False,
        supports_partial_streaming=True,
        notes="阿里云百炼 Anthropic 兼容端点（北京地域，按量付费）；Base URL 中的 "
        "{WorkspaceId} 需替换为百炼工作空间 ID；旗舰 qwen3.7-max 支持多模态输入，"
        "qwen3-coder-* 为纯文本编程模型",
    ),
    "zhipu": ProviderProfile(
        name="zhipu",
        label="智谱 AI / GLM",
        default_base_url="https://open.bigmodel.cn/api/anthropic",
        default_model="glm-5.2",
        default_small_fast_model="glm-5.2",
        # https://docs.bigmodel.cn/cn/guide/start/model-overview
        # https://docs.bigmodel.cn/cn/guide/develop/claude/introduction
        # https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash
        models=("glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.2"),
        supports_image_input=False,
        supports_document_input=False,
        supports_mcp_server_tools=True,
        thinking_budget_tokens_effective=False,
        disable_parallel_tool_use_effective=False,
        supports_partial_streaming=True,
        notes="智谱 GLM Anthropic 兼容端点；GLM-5.3 系列含 Flash / FlashX，保留 GLM-5.2；"
        "FlashX 为按量 API 模型，暂不包含在 Coding Plan 中",
    ),
    "moonshot": ProviderProfile(
        name="moonshot",
        label="月之暗面 / Kimi",
        default_base_url="https://api.moonshot.cn/anthropic",
        default_model="kimi-k3",
        default_small_fast_model="kimi-k2.7-code-highspeed",
        # https://platform.kimi.com/docs/models
        models=(
            "kimi-k3",
            "kimi-k2.7-code",
            "kimi-k2.7-code-highspeed",
            "kimi-k2.6",
        ),
        supports_image_input=False,
        supports_document_input=False,
        supports_mcp_server_tools=True,
        thinking_budget_tokens_effective=False,
        disable_parallel_tool_use_effective=False,
        supports_partial_streaming=True,
        notes="Kimi Anthropic 兼容端点；K3 为旗舰，K2.7 Code 面向编程，"
        "K2.6 支持可选思考模式",
    ),
    "minimax": ProviderProfile(
        name="minimax",
        label="MiniMax 稀宇科技",
        default_base_url="https://api.minimaxi.com/anthropic",
        default_model="MiniMax-M3",
        default_small_fast_model="MiniMax-M3",
        # https://platform.minimaxi.com/docs/api-reference/text-anthropic-api
        # https://platform.minimaxi.com/docs/faq/models
        # M2.5 / M2.7 已停止向新用户开放；存量按量用户迁移期至 2026-11-18。
        models=("MiniMax-M3.1-Flash", "MiniMax-M3"),
        supports_image_input=False,
        supports_document_input=False,
        supports_mcp_server_tools=True,
        thinking_budget_tokens_effective=False,
        disable_parallel_tool_use_effective=False,
        supports_partial_streaming=True,
        notes="MiniMax Anthropic 兼容端点；当前预设 M3.1 Flash / M3；M2.5 / M2.7 "
        "自 2026-10-19 停止新用户接入，存量按量 API 用户需在 2026-11-18 前迁移",
    ),
    "stepfun": ProviderProfile(
        name="stepfun",
        label="阶跃星辰 StepFun",
        default_base_url="https://api.stepfun.com",
        default_model="step-3.7-flash",
        default_small_fast_model="step-3.5-flash",
        # https://platform.stepfun.com/docs/zh/api-reference/chat/messages-create
        # step-router-v1 仅适用于 Step Plan，不属于本按量端点。
        models=("step-5-preview", "step-3.7-flash", "step-3.5-flash-2603", "step-3.5-flash"),
        supports_image_input=True,
        supports_document_input=False,
        supports_mcp_server_tools=True,
        thinking_budget_tokens_effective=False,
        disable_parallel_tool_use_effective=False,
        supports_partial_streaming=True,
        notes="阶跃星辰 Anthropic 兼容端点（按量付费）；Step 5 Preview 为新旗舰，"
        "3.7 Flash 支持多模态输入，"
        "3.5 Flash 系列偏 Agent / Coding / 工具调用",
    ),
    "mimo": ProviderProfile(
        name="mimo",
        label="小米 MiMo",
        default_base_url="https://api.xiaomimimo.com/anthropic",
        default_model="mimo-v2.6-pro",
        default_small_fast_model="mimo-v2.6-flash",
        # https://mimo.mi.com/docs/zh-CN/quick-start/summary/model
        # ultraspeed 为需联系厂商开通的定制服务，不作为通用 API 预设。
        # V2.5 在本次核对日仍可调用，保留至下线；已保存值仍可手工编辑。
        models=("mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.5-pro", "mimo-v2.5"),
        supports_image_input=True,
        supports_document_input=False,
        supports_mcp_server_tools=True,
        thinking_budget_tokens_effective=False,
        disable_parallel_tool_use_effective=False,
        supports_partial_streaming=True,
        notes="小米 MiMo Anthropic 兼容端点；V2.6 Pro / Flash 支持全模态理解；"
        "V2.5 Pro / V2.5 将于 2026-10-21 10:00（北京时间）下线，请迁移已保存配置",
    ),
    "hunyuan": ProviderProfile(
        name="hunyuan",
        label="腾讯混元",
        default_base_url="https://api.hunyuan.cloud.tencent.com/anthropic",
        default_model="hunyuan-2.0-thinking-20251109",
        default_small_fast_model="hunyuan-2.0-instruct-20251111",
        # https://cloud.tencent.com/document/product/1729/127293
        # TokenHub 的 hy4-preview / hy3 使用另一套端点及凭证，不混入旧接口。
        models=("hunyuan-2.0-thinking-20251109", "hunyuan-2.0-instruct-20251111"),
        supports_image_input=False,
        supports_document_input=False,
        supports_mcp_server_tools=True,
        thinking_budget_tokens_effective=False,
        disable_parallel_tool_use_effective=False,
        supports_partial_streaming=True,
        notes="腾讯混元直连平台 Anthropic 兼容端点；仅保留两款兼容模型，"
        "平台正迁移至 TokenHub，不再新增模型",
    ),
    "yinhe": ProviderProfile(
        name="yinhe",
        label="银河内部模型（OneAPI）",
        default_base_url="http://oneapi.yhroot.com",
        default_model="yinhe-thinking",
        default_small_fast_model="yinhe-chat",
        models=("yinhe-thinking", "yinhe-chat"),
        supports_image_input=False,
        supports_document_input=False,
        supports_mcp_server_tools=True,
        thinking_budget_tokens_effective=False,
        disable_parallel_tool_use_effective=False,
        supports_partial_streaming=True,
        notes="公司内部 OneAPI 网关（内网 HTTP，无需公网出口）；"
        "yinhe-thinking 带思考，yinhe-chat 为快速对话模型",
    ),
    "custom": ProviderProfile(
        name="custom",
        label="自定义 Anthropic 兼容端点",
        # custom 的地址、模型必须由 Settings 显式提供；这里的空值不会成为
        # assert_anthropic_configured 校验后的 effective 值。
        default_base_url="",
        default_model="",
        default_small_fast_model=None,
        supports_image_input=False,
        supports_document_input=False,
        # create_sdk_mcp_server 创建的是本进程内工具。它和 Read/Bash 等 SDK
        # 工具一样通过 Anthropic 标准 tool_use 协议暴露，不是把远端 MCP
        # 配置透传给上游。因此，能够运行 Claude Agent SDK 工具循环的
        # Anthropic-compatible custom 端点也能够使用这些工具。
        supports_mcp_server_tools=True,
        thinking_budget_tokens_effective=False,
        disable_parallel_tool_use_effective=False,
        supports_partial_streaming=False,
        notes="自定义 Anthropic 兼容端点；支持标准 tool use（含 SDK 进程内 MCP server）",
    ),
}


# ─────────────────────── Public API ────────────────────────────────

def _configured_primary_api_keys(settings_obj: Any) -> List[str]:
    """Read the typed pool defensively; legacy mocks/configs may lack the field."""
    raw = getattr(settings_obj, "anthropic_api_keys", [])
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(key).strip() for key in raw if str(key).strip()]


def assert_anthropic_configured() -> None:
    """校验 Anthropic 配置完整性，不完整时抛 AnthropicConfigurationError。"""
    from app.config import settings

    if not _configured_primary_api_keys(settings) and not settings.anthropic_api_key:
        raise AnthropicConfigurationError(
            "Neither anthropic_api_keys nor anthropic_api_key is configured. "
            "Set ANTHROPIC_API_KEYS or ANTHROPIC_API_KEY."
        )

    if settings.anthropic_provider == "custom":
        if not settings.anthropic_base_url:
            raise AnthropicConfigurationError(
                "anthropic_base_url is required when anthropic_provider='custom'. "
                "Set ANTHROPIC_BASE_URL environment variable."
            )
        if not settings.anthropic_model:
            raise AnthropicConfigurationError(
                "anthropic_model is required when anthropic_provider='custom'. "
                "Set ANTHROPIC_MODEL environment variable."
            )


def _sdk_model_for_context(provider: str, model: str, base_url: str) -> str:
    """Use only the verified official alias; gateways retain their own limits.

    DeepSeek's coding-agent guide and /models were checked on 2026-10-26.
    SDK 0.2.165 strips [1m] on the wire but uses it for local compaction.
    This does not change output-token or reasoning-effort settings.
    """
    if provider != "deepseek" or model != "deepseek-flash":
        return model
    try:
        url = urlsplit(base_url)
        official = (
            url.scheme == "https" and url.hostname == "api.deepseek.com"
            and url.port in (None, 443) and url.path in ("/anthropic", "/anthropic/")
            and url.username is None and url.password is None
            and not url.query and not url.fragment
        )
    except (TypeError, ValueError):
        return model
    return "deepseek-flash[1m]" if official else model


def build_options(
    *,
    system_prompt: "str | Dict[str, Any]",
    allowed_tools: List[str],
    cwd: str,
    disallowed_tools: Optional[List[str]] = None,
    max_turns: Optional[int] = None,
    permission_mode: Optional[str] = None,
    add_dirs: Optional[List[str]] = None,
    requires_image_input: bool = False,
    requires_document_input: bool = False,
    thinking_budget_tokens: Optional[int] = None,
    mcp_servers: Optional[Dict[str, Any]] = None,
    setting_sources: Optional[List[str]] = None,
    can_use_tool: Optional[Callable[..., Any]] = None,
    hooks: Optional[Dict[str, List[Any]]] = None,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    request_timeout_seconds: Optional[int] = None,
    include_partial_messages: bool = True,
    endpoint: Optional[Any] = None,
    use_extended_context: bool = False,
) -> Any:
    """构建 ClaudeAgentOptions，按 caller override → Settings → provider profile 优先级解析参数。

    ``endpoint`` 为 :class:`app.services.model_router.EndpointChoice` 时，provider /
    base_url / api_key / model 及**能力矩阵**全部取自该端点，而不是进程级全局
    ``settings``。故障转移必须走这条路：settings 是全进程共享的，路由器若靠改写它
    来切端点，会与并发运行中的其它 run 互相踩踏。未传时行为与之前完全一致。

    返回 ClaudeAgentOptions 实例。
    """
    from app.config import settings

    if endpoint is None:
        assert_anthropic_configured()

    if endpoint is not None:
        provider_name = endpoint.provider
        profile = endpoint.profile
    else:
        provider_name = settings.anthropic_provider
        profile = PROVIDER_PROFILES.get(provider_name)
    # 防御性兜底：Settings 正常会拒绝未知 provider。若测试或调用方绕过
    # Settings 校验，仍使用最严格的能力矩阵。
    if profile is None:
        profile = ProviderProfile(
            name="custom",
            default_base_url=settings.anthropic_base_url or "",
            default_model=settings.anthropic_model or "",
            default_small_fast_model=settings.anthropic_small_fast_model,
            supports_image_input=False,
            supports_document_input=False,
            supports_mcp_server_tools=False,
            thinking_budget_tokens_effective=False,
            disable_parallel_tool_use_effective=False,
        )

    # 能力检查
    if requires_image_input and not profile.supports_image_input:
        raise ProviderCapabilityError(
            f"Provider '{provider_name}' does not support image input "
            f"(supports_image_input=False). Switch to a capable provider."
        )
    if requires_document_input and not profile.supports_document_input:
        raise ProviderCapabilityError(
            f"Provider '{provider_name}' does not support document input "
            f"(supports_document_input=False). Switch to a capable provider."
        )

    # 解析 effective 值：caller override → endpoint / Settings → profile default
    if model is not None:
        effective_model = model
        if model != (settings.anthropic_model or ""):
            logger.info(
                "effective_model overridden by caller: %s (settings.anthropic_model=%s)",
                model,
                settings.anthropic_model,
            )
    elif endpoint is not None:
        effective_model = endpoint.model
    else:
        effective_model = (
            settings.anthropic_model
            or profile.default_model
        )
    # SDK background requests inherit process env unless pinned per run. A
    # runtime provider switch must not send the old provider's small model to
    # the new endpoint. EndpointChoice already resolves that slot's defaults.
    effective_small_fast_model = (
        (endpoint.small_fast_model or effective_model)
        if endpoint is not None else
        (settings.anthropic_small_fast_model or profile.default_small_fast_model or effective_model)
    )
    if endpoint is not None:
        effective_base_url = endpoint.base_url
        effective_api_key = endpoint.api_key
    else:
        effective_base_url = (
            settings.anthropic_base_url
            or profile.default_base_url
        )
        configured_api_keys = _configured_primary_api_keys(settings)
        effective_api_key = configured_api_keys[0] if configured_api_keys else settings.anthropic_api_key
    effective_max_turns = max_turns if max_turns is not None else settings.anthropic_max_turns
    effective_permission_mode = permission_mode or settings.anthropic_permission_mode
    sdk_model = (
        _sdk_model_for_context(provider_name, effective_model, effective_base_url)
        if use_extended_context is True else effective_model
    )

    # thinking_budget_tokens 在不支持的 provider 下静默丢弃
    effective_thinking: Optional[Dict[str, Any]] = None
    if thinking_budget_tokens is not None:
        if not profile.thinking_budget_tokens_effective:
            logger.warning(
                "thinking_budget_tokens=%d is ignored for provider='%s' "
                "(thinking_budget_tokens_effective=False). "
                "The parameter has been dropped.",
                thinking_budget_tokens,
                provider_name,
            )
        else:
            effective_thinking = {"budget_tokens": thinking_budget_tokens}

    effective_allowed_tools = list(allowed_tools)

    # MCP servers 能力检查（不支持的 provider 不注入）
    effective_mcp_servers: Dict[str, Any] = {}
    if mcp_servers:
        if not profile.supports_mcp_server_tools:
            logger.warning(
                "Provider '%s' does not support MCP server tools "
                "(supports_mcp_server_tools=False). "
                "MCP servers will not be registered.",
                provider_name,
            )
        else:
            effective_mcp_servers = mcp_servers

    if not profile.supports_mcp_server_tools:
        filtered_allowed_tools = [
            name for name in effective_allowed_tools
            if not str(name).startswith("mcp__")
        ]
        removed_count = len(effective_allowed_tools) - len(filtered_allowed_tools)
        if removed_count:
            logger.warning(
                "Provider '%s' does not support MCP server tools; removed %d MCP "
                "allowed tool(s).",
                provider_name,
                removed_count,
            )
        effective_allowed_tools = filtered_allowed_tools

    logger.info(
        "Building ClaudeAgentOptions: slot=%s provider=%s model=%s base_url=%s max_turns=%d",
        endpoint.slot if endpoint is not None else "settings",
        provider_name,
        effective_model,
        effective_base_url,
        effective_max_turns,
    )

    # 延迟导入以避免循环依赖，并在 SDK 未安装时给出清晰错误
    try:
        from claude_agent_sdk import ClaudeAgentOptions
    except ImportError as exc:
        raise AnthropicConfigurationError(
            "claude-agent-sdk is not installed. "
            "Run: pip install claude-agent-sdk>=0.1"
        ) from exc

    options_kwargs: Dict[str, Any] = {
        "model": sdk_model,
        "system_prompt": system_prompt,
        "allowed_tools": effective_allowed_tools,
        "cwd": cwd,
        "max_turns": effective_max_turns,
        "permission_mode": effective_permission_mode,
        "env": {
            "ANTHROPIC_API_KEY": effective_api_key,
            "ANTHROPIC_BASE_URL": effective_base_url,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": effective_small_fast_model,
            "ANTHROPIC_SMALL_FAST_MODEL": effective_small_fast_model,
        },
    }

    if sdk_model != effective_model:
        # Per-run only: never change administrator settings or auxiliary models.
        options_kwargs["env"]["ANTHROPIC_MODEL"] = sdk_model
        logger.info("SDK extended context enabled: model=%s sdk_model=%s", effective_model, sdk_model)

    if disallowed_tools:
        options_kwargs["disallowed_tools"] = list(disallowed_tools)

    if add_dirs:
        options_kwargs["add_dirs"] = add_dirs

    if effective_thinking:
        options_kwargs["thinking"] = effective_thinking

    if effective_mcp_servers:
        options_kwargs["mcp_servers"] = effective_mcp_servers

    if setting_sources:
        options_kwargs["setting_sources"] = list(setting_sources)

    if can_use_tool is not None:
        options_kwargs["can_use_tool"] = can_use_tool

    if hooks:
        options_kwargs["hooks"] = hooks

    if max_tokens is not None:
        options_kwargs["max_tokens"] = max_tokens
        # The bundled CLI consumes this even when the Python SDK version has
        # no max_tokens constructor field (and _instantiate_options drops it).
        options_kwargs["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)

    if request_timeout_seconds is not None:
        options_kwargs["request_timeout_seconds"] = request_timeout_seconds
        options_kwargs["env"]["API_TIMEOUT_MS"] = str(request_timeout_seconds * 1000)

    # Chunked streaming: enables native content_block_delta events so agents
    # can translate the assistant answer body into incremental answer_delta
    # trace events. Silently degrade on providers that don't support it.
    if include_partial_messages:
        if profile.supports_partial_streaming:
            options_kwargs["include_partial_messages"] = True
        else:
            logger.info(
                "Provider '%s' does not support partial streaming "
                "(supports_partial_streaming=False); include_partial_messages "
                "not set — answer_delta will not be emitted, clients fall back "
                "to run_complete.final_text.",
                provider_name,
            )

    return _instantiate_options(ClaudeAgentOptions, options_kwargs)
