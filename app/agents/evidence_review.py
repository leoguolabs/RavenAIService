"""One final evidence review in the existing SDK session, never a review loop."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_REVIEW_RESERVE = 5

_REVIEW = {
    "en": (
        "Use your complete final JSON just drafted as the editing base. Perform one consistency edit "
        "using only evidence already collected; do not restart analysis or search again. "
        "Compare every example, timeline, table row, summary, hypothesis and recommendation with the "
        "definitions, enum meanings and state conditions you cited. Apply each field's meaning consistently "
        "to its examples; different fields or objects are not interchangeable. Correct contradictions in "
        "every affected place, including examples that conflict with a correct definition elsewhere. "
        "An attempted operation, debug print or possible source branch does not by itself prove successful "
        "execution, durable output or remote receipt. Match the certainty of each claim to its evidence. "
        "Apart from correcting contradictions or unsupported certainty, preserve every supported independent "
        "finding, record, citation, coverage count, blocker and unresolved anomaly. Do not drop evidence "
        "to make the answer consistent, invent missing facts, or turn a partial sample into an exhaustive claim. "
        "Uninspected, unknown, and absent from one file are not evidence that something is absent from all "
        "attachments or outside their time coverage. Check the claimed scope against the files actually inspected; "
        "otherwise state what remains unverified. Current source describes possible behavior, not the deployed "
        "binary identity. A generic failure return or a missing log does not identify a specific device node, "
        "failure branch or deployed version. Qualify such inferences where they appear. "
        "If the evidence cannot resolve a statement, qualify it where it appears. "
        "Return the complete original JSON schema in a fenced block, without editing commentary. "
        "This is one edit only; no further review is required."
    ),
    "zh": (
        "以刚才完整的最终JSON为底稿，只依据已经收集的证据，做一次前后一致性校对，不重做分析或重新检索。"
        "把每个实例、时间线、表格行、摘要、假设和建议，与文中引用的定义、枚举含义、状态条件逐项对照。"
        "同一字段的含义应一致地用于所有实例，不同字段或对象不可混用；尤其检查定义写对、后面实例却用了相反含义的情况，修正所有受影响位置。"
        "操作尝试、调试打印或源码可能分支，本身不证明执行成功、持久化完成或远端收到；每项判断的确定程度须与已有证据相符。"
        "除纠正矛盾和无依据的确定判断外，保留所有有依据的独立发现、记录、引用、覆盖数量、阻塞条件和未决异常。"
        "不能为消除矛盾而删除证据、补造缺失事实，或把局部样本说成完整范围；证据不能判定时就在该判断处说明不确定性。"
        "未检查、未知、某份文件没查到，均不能证明所有附件都没有或时间在附件覆盖之外；核对所声称的范围与实际检查的文件，不足时写明尚未核验。"
        "当前源码描述可能行为，不等于现场二进制身份；通用失败返回或缺少某条日志不能确定具体设备节点、失败分支或部署版本，这类推断须就地说明依据和限制。"
        "按原有全部字段返回完整JSON围栏结果，不输出校对过程。只校对这一次，无须继续复核。"
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
