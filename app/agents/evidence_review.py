"""One final evidence review in the existing SDK session, never a review loop."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_REVIEW_RESERVE = 5

_REVIEW = {
    "en": (
        "Review the final result you just drafted against the evidence already collected. "
        "Preserve supported findings and citations. Check that every causal, completion, and exclusion claim "
        "is justified; distinguish observations from hypotheses and different times/objects from one operation. "
        "Retain relevant independent blockers and unresolved anomalies, including counterevidence. "
        "Missing messages, default values, possible source branches, or a computed hash alone do not establish "
        "a root cause or successful completion. Keep the certainty of the answer, summary, hypotheses, and "
        "recommendations consistent. Do not promise recovery while an independent blocker remains. "
        "Only investigate an actual evidence gap, batching independent checks; do not restart the investigation. "
        "If evidence is unavailable, state the uncertainty where the claim appears. "
        "Return the complete final result in the original fenced JSON schema, without review commentary. "
        "This is a single review; no further review is required."
    ),
    "zh": (
        "请依据本轮已经收集的证据，复核刚才拟定的最终结果，保留有依据的结论和引用。"
        "逐项检查原因、完成状态和排除判断是否有证据，区分观察与假设、不同时间或对象与同一次操作。"
        "保留与问题有关的独立阻塞条件、尚未解释的异常和反证。"
        "消息缺失、默认值、源码可能分支或仅计算出哈希值，都不能单独证明根因或执行成功。"
        "正文、摘要、根因假设和建议的确定程度必须一致；仍有独立阻塞条件时不要承诺某一步即可恢复。"
        "只针对实际证据缺口补查，独立检查合并执行，不重新开始整套调查；证据无法取得时就在相关判断处说明不确定性。"
        "返回原格式的完整 JSON 围栏结果，保留所有要求的字段，不添加复核过程说明。这里只复核一次，无须继续复核。"
    ),
}


class FinalEvidenceReview:
    def __init__(self, locale: str, task_id: str, max_turns: int | None):
        self.locale = locale
        self.task_id = task_id
        self.max_turns = max_turns
        self._response_ids: set[str] = set()
        self._unidentified_responses = 0
        self._reviewed = False

    def observe(self, message) -> None:
        """Conservatively count parent responses, not tool calls or HTTP requests.

        Old SDKs may omit IDs; counting each such message can skip a review
        early, but cannot create extra reviews. The CLI retains its turn cap.
        """
        # Assistant messages have model + content; user/tool result messages
        # and streaming envelopes do not. Avoid coupling to an SDK class copy.
        if (getattr(message, "model", None) is None
                or not isinstance(getattr(message, "content", None), list)
                or getattr(message, "parent_tool_use_id", None)):
            return
        message_id = getattr(message, "message_id", None)
        if isinstance(message_id, str) and message_id:
            self._response_ids.add(message_id)
        else:
            self._unidentified_responses += 1

    async def before_stop(self, data: dict, _tool_use_id, _context) -> dict:
        if data.get("hook_event_name") != "Stop" or self._reviewed:
            return {}
        # Set before returning: duplicate callbacks and a later natural Stop
        # must not force the model into repeated self-review.
        self._reviewed = True
        if data.get("stop_hook_active"):
            return {}
        responses = len(self._response_ids) + self._unidentified_responses
        if self.max_turns and responses + _REVIEW_RESERVE >= self.max_turns:
            logger.info("Final evidence review skipped near turn cap task_id=%s responses=%s", self.task_id, responses)
            return {}
        logger.info("Final evidence review requested task_id=%s responses=%s", self.task_id, responses)
        return {"decision": "block", "reason": _REVIEW.get(self.locale, _REVIEW["zh"])}
