"""Opt-in soft completion checkpoints; never remove tools or lower turn limits."""


class EvidenceProgress:
    """Per-run, bounded counter with at most two evidence-preserving notices."""

    def __init__(self, locale="zh"):
        self.locale = locale
        self.calls = 0
        self.seen = set()

    async def after_tool(self, data, tool_id, context):
        if (
            not isinstance(data, dict)
            or data.get("hook_event_name") != "PostToolUse"
            or self.calls >= 120
        ):
            return {}
        identity = data.get("tool_use_id") or tool_id
        if isinstance(identity, str) and identity:
            if identity in self.seen:
                return {}
            self.seen.add(identity)
        self.calls += 1
        if self.calls not in (80, 120):
            return {}
        if self.locale == "en":
            stage = (
                "Review the remaining decisive gaps"
                if self.calls == 80
                else "Move toward a supported final answer"
            )
            text = (
                f"Evidence progress checkpoint: {self.calls} tool results observed (not model turns). {stage}. "
                "Preserve confirmed findings, counterevidence, native citations, required Skills, source checks and attachment coverage. "
                "Continue only checks of specific unresolved evidence that could change the requested conclusions; batch independent checks. "
                "Do not repeat searches to prove an unknown actor or globally absent event. State what is unknown in the inspected scope; "
                "missing logs or current source possibilities cannot establish an operation, exclude actors, identify a deployed branch or prove completion. "
                "Do not claim uninspected attachments were checked or drop decisive evidence to finish. "
                "Once the material findings are supported, return the required final schema with calibrated uncertainty. "
                "This is a soft checkpoint: all original tools and the configured turn limit remain available."
            )
        else:
            stage = (
                "核对剩余的关键证据缺口"
                if self.calls == 80
                else "进入有依据的最终答案收尾"
            )
            text = (
                f"证据进度检查：已观察到 {self.calls} 次工具结果（不是模型轮数）。请{stage}。"
                "保留已证实发现、反证、原始引用、必需 Skill、源码核验和附件覆盖。"
                "只继续核查会改变用户所问结论的具体未决证据，独立核查合并批次；"
                "不要为确定未知执行方或证明事件全局不存在而反复变换关键词。"
                "对已检查范围内无法确定的事项明确写出未知；缺日志或当前源码可能性不能证明现场动作、排除执行方、确定部署分支或证明完成。"
                "不得假装已检查未读附件，也不得为赶收尾丢弃决定性证据。"
                "重要发现有依据后，按要求的最终 schema 回答，并保持相称置信度。"
                "这是软检查点，全部原工具及原配置轮数上限仍保留。"
            )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": text,
            }
        }
