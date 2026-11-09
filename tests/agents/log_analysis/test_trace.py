"""Tests for app/agents/log_analysis/trace.py."""

from __future__ import annotations

import json

import pytest

from app.agents.log_analysis.trace import (
    AgentTraceEvent,
    SeqCounter,
    STEP_END,
    STEP_START,
    THINKING_DELTA,
    THINKING_END,
    build_event,
    coerce_chunk,
    coerce_excerpt,
    derive_tool_trace,
    mask_input,
    mask_tokens,
    safe_emit,
    summarize,
)


class TestSeqCounter:
    def test_monotonic(self):
        counter = SeqCounter()
        seqs = [counter.next() for _ in range(5)]
        assert seqs == [1, 2, 3, 4, 5]

    def test_value_property(self):
        counter = SeqCounter()
        counter.next()
        counter.next()
        assert counter.value == 2


class TestMaskTokens:
    @pytest.mark.parametrize('scheme', ['http', 'https', 'HTTPS'])
    def test_git_remote_userinfo_and_bare_pat_are_hidden(self, scheme):
        token = 'glpat-fixture_secret_123'
        masked = mask_tokens(f'origin {scheme}://oauth2:{token}@git.example/repo.git; token={token}')
        assert token not in masked
        assert f'{scheme}://***@git.example/repo.git' in masked

    def test_strips_credential(self):
        url = "https://abc123@example.com/repo.git"
        assert mask_tokens(url) == "https://***@example.com/repo.git"

    def test_no_token_left_unchanged(self):
        url = "https://example.com/repo.git"
        assert mask_tokens(url) == url

    def test_multiple_in_one_string(self):
        text = "clone https://t1@a.com/x.git or https://t2@b.com/y.git"
        masked = mask_tokens(text)
        assert "t1" not in masked
        assert "t2" not in masked
        assert masked.count("***") == 2

    def test_mask_input_recurses_into_dict(self):
        payload = {
            "command": "git clone https://secret@github.com/owner/repo.git",
            "nested": {"url": "https://other@gitlab.com/x.git"},
            "extras": ["https://abc@host/y.git", 42],
        }
        masked = mask_input(payload)
        flat = json.dumps(masked)
        assert "secret" not in flat
        assert "other" not in flat
        assert "abc" not in flat
        assert "42" in flat  # numbers untouched

    def test_mask_input_preserves_primitives(self):
        assert mask_input(None) is None
        assert mask_input(True) is True
        assert mask_input(7) == 7
        assert mask_input(1.5) == 1.5


class TestCoerceChunk:
    def test_empty_returns_empty(self):
        assert coerce_chunk("") == []

    def test_short_returns_single(self):
        assert coerce_chunk("hello") == ["hello"]

    def test_splits_long_ascii(self):
        text = "A" * 10000
        chunks = coerce_chunk(text, max_bytes=4096)
        assert sum(len(c) for c in chunks) == 10000
        for chunk in chunks[:-1]:
            assert len(chunk.encode("utf-8")) <= 4096

    def test_handles_multibyte_boundary(self):
        # 4097 multi-byte chars (3 bytes each in UTF-8) — exceeds 4096.
        text = "中" * 2000
        chunks = coerce_chunk(text, max_bytes=4096)
        assert len(chunks) > 1
        rejoined = "".join(chunks)
        assert rejoined == text
        for chunk in chunks:
            # Each chunk must be valid UTF-8 (no boundary errors).
            chunk.encode("utf-8")

    def test_very_small_max_bytes(self):
        # max_bytes=1 forces at least one codepoint per chunk so we still
        # make progress on multibyte input.
        chunks = coerce_chunk("中国", max_bytes=1)
        assert "".join(chunks) == "中国"


class TestCoerceExcerpt:
    def test_short_unchanged(self):
        assert coerce_excerpt("abc", max_bytes=10) == "abc"

    def test_truncates(self):
        excerpt = coerce_excerpt("A" * 5000, max_bytes=100)
        assert len(excerpt.encode("utf-8")) <= 100

    def test_respects_codepoint_boundary(self):
        excerpt = coerce_excerpt("中" * 100, max_bytes=10)
        # Should not throw on decode and should not exceed 10 bytes.
        assert len(excerpt.encode("utf-8")) <= 10
        # Every char in the excerpt is a complete codepoint.
        excerpt.encode("utf-8").decode("utf-8")


class TestBuildEvent:
    def test_assigns_seq_and_timestamp(self):
        counter = SeqCounter()
        ev = build_event(STEP_START, task_id="t1", seq_counter=counter, tool_name="Bash")
        assert ev["type"] == STEP_START
        assert ev["task_id"] == "t1"
        assert ev["seq"] == 1
        assert isinstance(ev["timestamp"], float)
        assert ev["tool_name"] == "Bash"

    def test_seq_monotonic_across_events(self):
        counter = SeqCounter()
        e1 = build_event(STEP_START, task_id="t", seq_counter=counter)
        e2 = build_event(STEP_END, task_id="t", seq_counter=counter)
        assert e2["seq"] == e1["seq"] + 1

    def test_drops_none_fields(self):
        counter = SeqCounter()
        ev = build_event(
            "system_notice",
            task_id="t",
            seq_counter=counter,
            kind="cancel_requested",
            detail=None,
        )
        assert "detail" not in ev
        assert ev["kind"] == "cancel_requested"


class TestSafeEmit:
    def test_no_emitter_is_noop(self):
        counter = SeqCounter()
        ev = build_event(STEP_START, task_id="t", seq_counter=counter)
        safe_emit(None, ev)  # must not raise

    def test_exception_swallowed(self):
        def bad(event):
            raise RuntimeError("boom")

        counter = SeqCounter()
        ev = build_event(STEP_START, task_id="t", seq_counter=counter)
        safe_emit(bad, ev)  # must not raise

    def test_success_passes_event(self):
        captured = []
        counter = SeqCounter()
        ev = build_event(STEP_START, task_id="t", seq_counter=counter)
        safe_emit(captured.append, ev)
        assert captured == [ev]


class TestDeriveToolTrace:
    def test_pairs_start_end(self):
        counter = SeqCounter()
        events = [
            build_event(
                STEP_START,
                task_id="t",
                seq_counter=counter,
                step_id="s1",
                tool_name="Bash",
                tool_input={"command": "ls"},
            ),
            build_event(
                STEP_END,
                task_id="t",
                seq_counter=counter,
                step_id="s1",
                status="ok",
                output_excerpt="file1\nfile2",
            ),
        ]
        trace = derive_tool_trace(events)
        assert len(trace) == 1
        assert trace[0]["name"] == "Bash"
        assert trace[0]["output_excerpt"] == "file1\nfile2"
        # Input rendered as stringified JSON for backward compat.
        assert "command" in trace[0]["input"]

    def test_orphan_start_kept_empty_excerpt(self):
        counter = SeqCounter()
        events = [
            build_event(
                STEP_START,
                task_id="t",
                seq_counter=counter,
                step_id="s1",
                tool_name="Read",
                tool_input={"path": "/x"},
            ),
        ]
        trace = derive_tool_trace(events)
        assert len(trace) == 1
        assert trace[0]["output_excerpt"] == ""

    def test_preserves_order(self):
        counter = SeqCounter()
        events = []
        for idx, tool in enumerate(["Bash", "Read", "Grep"], start=1):
            sid = f"s{idx}"
            events.append(build_event(STEP_START, task_id="t", seq_counter=counter, step_id=sid, tool_name=tool, tool_input={}))
            events.append(build_event(STEP_END, task_id="t", seq_counter=counter, step_id=sid, status="ok", output_excerpt=f"out-{idx}"))
        trace = derive_tool_trace(events)
        assert [t["name"] for t in trace] == ["Bash", "Read", "Grep"]
        assert [t["output_excerpt"] for t in trace] == ["out-1", "out-2", "out-3"]

    def test_ignores_non_step_events(self):
        counter = SeqCounter()
        events = [
            build_event("run_start", task_id="t", seq_counter=counter, model="m"),
            build_event(THINKING_DELTA, task_id="t", seq_counter=counter, step_id="th1", text_chunk="thinking"),
            build_event("run_complete", task_id="t", seq_counter=counter),
        ]
        assert derive_tool_trace(events) == []


class TestSummarize:
    def test_counts_step_ends(self):
        counter = SeqCounter()
        events = [
            build_event("run_start", task_id="t", seq_counter=counter),
            build_event(STEP_START, task_id="t", seq_counter=counter, step_id="s1", tool_name="Bash", tool_input={}),
            build_event(STEP_END, task_id="t", seq_counter=counter, step_id="s1", status="ok", output_excerpt=""),
            build_event(STEP_START, task_id="t", seq_counter=counter, step_id="s2", tool_name="Read", tool_input={}),
            build_event(STEP_END, task_id="t", seq_counter=counter, step_id="s2", status="error", output_excerpt=""),
            build_event("run_complete", task_id="t", seq_counter=counter),
        ]
        summary = summarize(events)
        assert summary["tool_call_count"] == 2

    def test_thinking_chars_from_deltas(self):
        counter = SeqCounter()
        events = [
            build_event("thinking_start", task_id="t", seq_counter=counter, step_id="th1"),
            build_event(THINKING_DELTA, task_id="t", seq_counter=counter, step_id="th1", text_chunk="abc"),
            build_event(THINKING_DELTA, task_id="t", seq_counter=counter, step_id="th1", text_chunk="defg"),
            build_event(THINKING_END, task_id="t", seq_counter=counter, step_id="th1"),
        ]
        assert summarize(events)["thinking_chars"] == 7

    def test_thinking_chars_fallback_to_end_text(self):
        counter = SeqCounter()
        events = [
            build_event("thinking_start", task_id="t", seq_counter=counter, step_id="th1"),
            build_event(THINKING_END, task_id="t", seq_counter=counter, step_id="th1", text="hello"),
        ]
        assert summarize(events)["thinking_chars"] == 5

    def test_duration_from_first_last_timestamp(self):
        counter = SeqCounter()
        events = [
            {"type": "run_start", "task_id": "t", "seq": counter.next(), "timestamp": 100.0},
            {"type": "run_complete", "task_id": "t", "seq": counter.next(), "timestamp": 142.5},
        ]
        summary = summarize(events)
        assert summary["thought_duration_seconds"] == 42.5
