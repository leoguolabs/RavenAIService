"""Native search replay, exact evidence, independent cwd and bounded fallback."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from app.agents import log_topic_context as topic
from app.agents.evidence_policy import execution_hooks


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "sub").mkdir()
    (tmp_path / "logs/one.log").write_text("noise\n" * 25 + "rare first\ncontext\nrare last\nend\n")
    (tmp_path / "logs/two.log").write_text("noise\n" * 4 + "rare second file\n")
    return tmp_path


def native(workspace, command=r"grep -n 'noise\|rare' logs/one.log | head -2", cwd=None):
    cwd = cwd or workspace
    result = subprocess.run(["/bin/sh", "-c", command], cwd=cwd, capture_output=True, text=True, check=True)
    return {"hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": str(cwd),
            "tool_input": {"command": command}, "tool_response": {"stdout": result.stdout.removesuffix("\n"),
            "stderr": result.stderr, "interrupted": False}}


def payload(text):
    assert text and len(text.encode()) <= topic.MAX_OUTPUT_BYTES
    return json.loads(text.split("\n", 1)[1])


def verify_rows(workspace, entry):
    raw = (workspace / entry["path"]).read_bytes()
    assert entry["source_sha256"] == hashlib.sha256(raw).hexdigest()
    lines = raw.split(b"\n")
    for window in entry["windows"]:
        for n, text in window["rows"]:
            expected = lines[n - 1].removesuffix(b"\r") if n <= raw.count(b"\n") else lines[n - 1]
            assert text == expected.decode("utf-8", errors="backslashreplace")


@pytest.mark.parametrize("locale", ["zh", "en"])
@pytest.mark.asyncio
async def test_supplement_reaches_missing_topic_without_mutating_native_output(workspace, locale):
    data = native(workspace)
    before = copy.deepcopy(data)
    hook = topic.LogTopicContextEnricher(str(workspace), locale)
    result = await hook.after_tool(data, "first", {})
    body = payload(result["hookSpecificOutput"]["additionalContext"])
    assert data == before
    entry, = body["topic_context"]
    assert entry["literal"] == "rare" and entry["matching_lines"] == 2
    assert entry["original_preview_matches"] == 0 and entry["scan_complete"]
    verify_rows(workspace, entry)
    assert any(text == "rare last" for w in entry["windows"] for _, text in w["rows"])
    assert await hook.after_tool(data, "second", {}) == {}
    assert await hook.after_tool(data, "first", {}) == {}
    with (workspace / "logs/one.log").open("a") as f:
        f.write("rare new\n")
    again = await hook.after_tool(native(workspace), "third", {})
    assert payload(again["hookSpecificOutput"]["additionalContext"])["topic_context"][0]["matching_lines"] == 3


@pytest.mark.parametrize("command", [
    r'for f in logs/one.log logs/two.log; do echo "=== $f"; grep -n "noise\|rare" $f | head -2; done',
    r'for item in logs/one.log logs/two.log; do echo "===${item}"; grep -En "noise|rare" ${item} | head -n 2; done',
    r"grep -anE 'noise|rare' logs/one.log | head -n2",
])
def test_supported_native_grammars_match_actual_stdout(workspace, command):
    body = payload(topic.LogTopicContextEnricher(str(workspace)).collect(native(workspace, command)))
    assert all(e["literal"] == "rare" for e in body["topic_context"])
    for entry in body["topic_context"]:
        verify_rows(workspace, entry)


def test_cwd_is_actual_hook_cwd_and_absolute_cd_is_verified(workspace):
    command = r"grep -n 'noise\|rare' ../logs/one.log | head -2"
    assert topic.LogTopicContextEnricher(str(workspace)).collect(native(workspace, command, workspace / "sub"))
    command = f"cd {workspace} && grep -n 'noise\\|rare' logs/one.log | head -2"
    assert topic.LogTopicContextEnricher(str(workspace)).collect(native(workspace, command))
    data = native(workspace)
    data["cwd"] = str(workspace / "sub")
    # No root/repo fallback or guessed shell directory.
    with pytest.raises(FileNotFoundError):
        topic.LogTopicContextEnricher(str(workspace)).collect(data)


@pytest.mark.parametrize("command", [
    "grep -n 'noise.*|rare' logs/one.log | head -2", "grep -inE 'noise|rare' logs/one.log | head -2",
    "grep -n 'noise\\|rare' logs/one.log | tail -2", "grep -n 'noise\\|rare' logs/*.log | head -2",
    "grep -n 'noise\\|rare' logs/one.log | head -2; pwd", "grep -n 'noise\\|rare' $(echo logs/one.log) | head -2",
    "grep -n 'noise\\|rare' logs/one.log | head -0", "grep -n 'noise\\|rare' logs/one.log | head -201",
    "grep -n 'noise\\|noise' logs/one.log | head -2", "grep -n 'noise' logs/one.log | head -2",
    "cd sub && grep -n 'noise\\|rare' ../logs/one.log | head -2",
    'for f in logs/one.log; do echo "=== $f"; rm "$f"; grep -n "noise\\|rare" $f | head -2; done',
])
@pytest.mark.asyncio
async def test_unsupported_shell_and_regex_are_never_executed(workspace, command):
    data = native(workspace)
    original = (workspace / "logs/one.log").read_bytes()
    data["tool_input"]["command"] = command
    assert await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "unsafe", {}) == {}
    assert (workspace / "logs/one.log").read_bytes() == original


@pytest.mark.parametrize("change", ["prefix", "suffix", "truncated", "stderr", "cwd_missing", "cwd_relative", "interrupted", "error"])
@pytest.mark.asyncio
async def test_unverified_results_add_nothing(workspace, change):
    data = native(workspace)
    if change == "prefix":
        data["tool_response"]["stdout"] = "different\n" + data["tool_response"]["stdout"]
    elif change == "suffix":
        data["tool_response"]["stdout"] += "\nextra"
    elif change == "truncated":
        data["tool_response"]["stdout"] = "1:noise"
    elif change == "cwd_missing":
        data.pop("cwd")
    elif change == "cwd_relative":
        data["cwd"] = "."
    else:
        data["tool_response"][{"stderr": "stderr", "interrupted": "interrupted", "error": "is_error"}[change]] = True
    assert await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, change, {}) == {}


@pytest.mark.asyncio
async def test_outside_logs_symlink_binary_and_directory_fall_back(workspace):
    other = workspace / "other.log"
    other.write_text((workspace / "logs/one.log").read_text())
    (workspace / "logs/link.log").symlink_to(other)
    data = native(workspace)
    for target in ["other.log", "logs/link.log", "logs"]:
        data["tool_input"]["command"] = "grep -n 'noise\\|rare' " + target + " | head -2"
        assert await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, target, {}) == {}
    with (workspace / "logs/one.log").open("ab") as f:
        f.write(b"\0")
    data["tool_input"]["command"] = "grep -n 'noise\\|rare' logs/one.log | head -2"
    assert await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "binary", {}) == {}


def test_file_change_is_detected_after_read(workspace, monkeypatch):
    data = native(workspace)
    original = topic.os.fstat
    calls = 0

    def changed(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            with (workspace / "logs/one.log").open("a") as f:
                f.write("changed\n")
        return original(fd)

    monkeypatch.setattr(topic.os, "fstat", changed)
    assert topic.LogTopicContextEnricher(str(workspace)).collect(data) is None


@pytest.mark.parametrize("limit", ["MAX_FILE_BYTES", "MAX_TOTAL_BYTES", "MAX_LINES", "MAX_LINE_BYTES", "SCAN_SECONDS"])
def test_processing_limits_fail_back_without_false_completeness(workspace, monkeypatch, limit):
    data = native(workspace)
    monkeypatch.setattr(topic, limit, 0)
    assert topic.LogTopicContextEnricher(str(workspace)).collect(data) is None


def test_long_unicode_and_invalid_bytes_have_bounded_explicit_continuation(workspace):
    raw = b"noise\n" * 25 + ("rare " + "汉" * 4000 + "\n").encode() + b"rare \xff\n"
    (workspace / "logs/one.log").write_bytes(raw)
    data = {"cwd": str(workspace), "tool_input": {"command": r"grep -an 'noise\|rare' logs/one.log | head -2"},
            "tool_response": {"stdout": "1:noise\n2:noise"}}
    body = payload(topic.LogTopicContextEnricher(str(workspace)).collect(data))
    entry = body["topic_context"][0]
    assert entry["matching_lines"] == 2
    assert any(not w["range_complete"] and w["next_request"]["start_line"] == 26 for w in entry["windows"])
    verify_rows(workspace, entry)


def test_cr_and_final_partial_lines_keep_native_coordinates(workspace):
    (workspace / "logs/one.log").write_bytes(b"noise\nnoise\nrare first\rstill same line\ncontext\nrare last")
    body = payload(topic.LogTopicContextEnricher(str(workspace)).collect(native(workspace)))
    entry = body["topic_context"][0]
    assert entry["matching_lines"] == 2
    verify_rows(workspace, entry)
    assert any(n == 3 and "\r" in text for w in entry["windows"] for n, text in w["rows"])


def test_final_standalone_cr_is_evidence_not_a_crlf_terminator(workspace):
    (workspace / "logs/one.log").write_bytes(b"noise\nnoise\nrare final\r")
    body = payload(topic.LogTopicContextEnricher(str(workspace)).collect(native(workspace)))
    entry = body["topic_context"][0]
    verify_rows(workspace, entry)
    assert [3, "rare final\r"] in entry["windows"][0]["rows"]


def test_separate_hook_switch_preserves_existing_hook_registration(workspace):
    assert execution_hooks("en", workspace=str(workspace)) == {}
    hooks = execution_hooks("en", workspace=str(workspace), source_context=True, topic_context=True)
    assert len(hooks["PostToolUse"]) == 2
    assert hooks["PostToolUse"][0].hooks[0].__self__.__class__.__name__ == "SourceContextEnricher"
    assert isinstance(hooks["PostToolUse"][1].hooks[0].__self__, topic.LogTopicContextEnricher)


def test_already_represented_topics_do_not_add_context(workspace):
    data = native(workspace, r"grep -n 'noise\|rare' logs/one.log | head -30")
    assert topic.LogTopicContextEnricher(str(workspace)).collect(data) is None


def test_invalid_utf8_samples_are_explicitly_escaped(workspace):
    (workspace / "logs/one.log").write_bytes(b"noise\nnoise\nrare \xff\n")
    data = {"cwd": str(workspace), "tool_input": {"command": r"grep -an 'noise\|rare' logs/one.log | head -2"},
            "tool_response": {"stdout": "1:noise\n2:noise"}}
    body = payload(topic.LogTopicContextEnricher(str(workspace)).collect(data))
    entry = body["topic_context"][0]
    assert entry["matching_lines"] == 1
    assert [3, r"rare \xff"] in entry["windows"][0]["rows"]
    verify_rows(workspace, entry)


def test_topic_limit_reports_omissions_without_dropping_original_evidence(workspace):
    (workspace / "logs/one.log").write_text("noise\nnoise\nrareA\nrareB\nrareC\nrareD\nrareE\n")
    data = native(workspace, "grep -nE 'noise|rareA|rareB|rareC|rareD|rareE' logs/one.log | head -2")
    before = copy.deepcopy(data)
    body = payload(topic.LogTopicContextEnricher(str(workspace)).collect(data))
    assert len(body["topic_context"]) == 4 and body["omitted_topics"] == 1
    assert data == before
