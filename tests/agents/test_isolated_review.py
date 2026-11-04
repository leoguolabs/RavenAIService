import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from app.agents import isolated_review as review


def fixture(root):
    (root / "logs").mkdir()
    (root / "logs/a.log").write_text("data\n")
    return {
        "status": "ok",
        "answer": "actor confirmed",
        "summary": "summary",
        "recommended_actions": [],
    }


def options():
    return SimpleNamespace(
        model="yinhe-thinking",
        env={"ANTHROPIC_AUTH_TOKEN": "private"},
        max_turns=150,
        system_prompt="original",
        allowed_tools=["Bash"],
        tools=None,
        mcp_servers={"original": {}},
        hooks={"Stop": []},
        setting_sources=["project"],
        cwd="original",
        resume="old",
        continue_conversation=True,
        fork_session=True,
        session_id="old",
        agents={},
        add_dirs=["original"],
        include_partial_messages=True,
        can_use_tool="old",
    )


def test_options_pin_effective_primary_and_keep_budget_without_mutating_original(
    tmp_path,
):
    primary = options()
    before = copy.deepcopy(primary)
    o = review.review_options(primary, tmp_path, "REQUIRED", "zh")
    assert primary.__dict__ == before.__dict__
    assert o.max_turns == 150 and o.model == "yinhe-thinking"
    assert all(
        o.env[k] == "yinhe-thinking"
        for k in [
            "ANTHROPIC_MODEL",
            "ANTHROPIC_SMALL_FAST_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        ]
    )
    assert o.env["ANTHROPIC_AUTH_TOKEN"] == "private" and o.env is not primary.env
    assert (
        o.system_prompt.count("REQUIRED") == 1
        and not o.hooks
        and o.tools == []
        and o.allowed_tools == []
    )
    assert (
        not o.mcp_servers
        and not o.setting_sources
        and not o.resume
        and o.session_id is None
    )


def test_gate_requires_snapshot_and_explicit_existing_defaultoff_review():
    s = SimpleNamespace(
        agent_evidence_final_review_enabled=False,
        agent_evidence_final_review_mode="isolated",
    )
    assert not review.isolated_mode(s, {})
    s.agent_evidence_final_review_enabled = True
    assert not review.isolated_mode(s, None) and review.isolated_mode(s, {})
    s.agent_evidence_final_review_mode = "session"
    assert not review.isolated_mode(s, {})


@pytest.mark.parametrize(
    "text", ['{"edits":[],"edits":[]}', 'comment {"edits":[]}', "```json\n{}\n```more"]
)
def test_ambiguous_or_non_json_verification_fails(text):
    with pytest.raises(review.VerificationError):
        review.parse_edits(text)


@pytest.mark.asyncio
async def test_success_applies_only_literal_public_edits_without_raw_input_or_stream_output(
    tmp_path,
):
    draft = fixture(tmp_path)
    draft["tool_trace"] = "PRIVATE_TRACE"
    draft["raw"] = "PRIVATE_RAW"
    before = copy.deepcopy(draft)
    seen = []

    async def query(**kw):
        seen.append(kw)
        yield SimpleNamespace(
            is_error=False,
            result=json.dumps(
                {
                    "edits": [
                        {
                            "path": ["answer"],
                            "before": "actor confirmed",
                            "after": "actor unconfirmed",
                        }
                    ]
                }
            ),
            usage={"input_tokens": 5},
        )

    result, metadata = await review.verify(
        workspace=tmp_path,
        draft=draft,
        primary_options=options(),
        required_prompt="REQUIRED",
        locale="en",
        check_cancel=lambda: None,
        query=query,
    )
    assert (
        draft == before
        and result["answer"] == "actor unconfirmed"
        and result["raw"] == "PRIVATE_RAW"
    )
    assert (
        metadata["edits"] == 1
        and metadata["usage"] == {"input_tokens": 5}
        and metadata["seconds"] >= 0
    )
    assert (
        "PRIVATE" not in seen[0]["prompt"]
        and seen[0]["agent_kind"] == "log_analysis_verifier"
    )
    assert (
        seen[0]["candidates"] == [] and seen[0]["make_options"](None).max_turns == 150
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        None,
        {"edits": [{"path": ["answer"], "before": "missing", "after": "x"}]},
        {"edits": [{"path": ["status"], "before": "ok", "after": "bad"}]},
    ],
)
async def test_invalid_or_failed_response_never_returns_original_as_verified(
    tmp_path, value
):
    draft = fixture(tmp_path)

    async def query(**kw):
        yield SimpleNamespace(
            is_error=value is None, result=json.dumps(value), usage=None
        )

    with pytest.raises(review.VerificationError):
        await review.verify(
            workspace=tmp_path,
            draft=draft,
            primary_options=options(),
            required_prompt="",
            locale="zh",
            check_cancel=lambda: None,
            query=query,
        )
    assert draft["answer"] == "actor confirmed"


@pytest.mark.asyncio
async def test_cancellation_interrupts_blocked_verifier_and_closes_generator(tmp_path):
    draft = fixture(tmp_path)
    started = asyncio.Event()
    closed = asyncio.Event()
    cancel = False

    class Cancelled(Exception):
        pass

    def check():
        if cancel:
            raise Cancelled()

    async def query(**kw):
        started.set()
        try:
            await asyncio.Event().wait()
            if False:
                yield None
        finally:
            closed.set()

    task = asyncio.create_task(
        review.verify(
            workspace=tmp_path,
            draft=draft,
            primary_options=options(),
            required_prompt="",
            locale="zh",
            check_cancel=check,
            query=query,
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    cancel = True
    with pytest.raises(Cancelled):
        await asyncio.wait_for(task, 2)
    assert closed.is_set()


@pytest.mark.asyncio
async def test_changed_public_draft_is_not_overwritten_by_stale_edits(tmp_path):
    draft = fixture(tmp_path)

    async def query(**kw):
        draft["summary"] = "newer summary"
        yield SimpleNamespace(is_error=False, result='{"edits":[]}', usage=None)

    with pytest.raises(review.VerificationError, match="changed"):
        await review.verify(
            workspace=tmp_path,
            draft=draft,
            primary_options=options(),
            required_prompt="",
            locale="zh",
            check_cancel=lambda: None,
            query=query,
        )
