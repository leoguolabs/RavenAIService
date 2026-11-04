from pathlib import Path
import json
import pytest
from app.agents import source_field_relations as mod
from app.agents.source_field_relations import SourceFieldRelations as Prototype


def fixture(
    root,
    *,
    argument="r->mode",
    comparison_type="PMSG",
    operator="==",
    format="mode=%d",
    decl=None,
):
    (root / "repo").mkdir()
    (root / "logs").mkdir()
    (root / "repo/state.h").write_text(
        decl
        or "typedef struct { int mode; int other; } MSG, *PMSG;\ntypedef struct { int mode; } OTHER, *POTHER;\ntypedef enum { BAD = 17, GOOD, UNKNOWN } Phase;\n"
    )
    (root / "repo/worker.c").write_text(
        '#include "state.h"\nvoid log_event(MSG *r) { log_info("'
        + format
        + '", '
        + argument
        + "); }\nvoid compare("
        + comparison_type
        + " r) { if (r->mode "
        + operator
        + " BAD) return; }\n"
    )
    line = "2026-10-01T00:00:00.000,Tick=1 INFO Tid:2 worker.c log_event 2 mode=18"
    (root / "logs/input.log").write_text(line + "\n")
    return [("logs/input.log", 1, line)]


def test_pointer_alias_format_argument_and_reversed_comparison_are_source_backed(
    tmp_path,
):
    rows = fixture(tmp_path)
    source = tmp_path / "repo/worker.c"
    source.write_text(source.read_text().replace("r->mode == BAD", "BAD == r->mode"))
    result = Prototype(tmp_path).collect(rows)
    assert len(result["source_relations"]) == 1
    rel = result["source_relations"][0]
    assert rel["field"] == "mode" and rel["compared_symbol"] == "BAD"
    assert rel["format_binding"][0]["argument"] == "r->mode"
    assert "BAD = 17, GOOD, UNKNOWN" in rel["enum_body"]["lines"][0][2]
    assert "value" not in rel and len(json.dumps(result).encode()) <= 8000


@pytest.mark.parametrize(
    "argument,typ,operator,format",
    [
        ("r->other", "PMSG", "==", "mode=%d"),
        ("r->mode + 1", "PMSG", "==", "mode=%d"),
        ("r->mode", "POTHER", "==", "mode=%d"),
        ("r->mode", "MSG", "==", "mode=%d"),
        ("r->mode", "PMSG", "<", "mode=%d"),
        ("r->mode", "PMSG", "==", "mode=%*d"),
        ("r->mode", "PMSG", "==", "other=%d"),
    ],
)
def test_unproved_field_binding_or_type_is_not_a_mapping(
    tmp_path, argument, typ, operator, format
):
    assert (
        Prototype(tmp_path).collect(
            fixture(
                tmp_path,
                argument=argument,
                comparison_type=typ,
                operator=operator,
                format=format,
            )
        )["source_relations"]
        == []
    )


def test_wrong_logged_function_does_not_select_other_code(tmp_path):
    rows = fixture(tmp_path)
    line = rows[0][2].replace(" log_event ", " unrelated ")
    (tmp_path / "logs/input.log").write_text(line + "\n")
    assert (
        Prototype(tmp_path).collect([("logs/input.log", 1, line)])["source_relations"]
        == []
    )


def test_log_text_is_verified_not_trusted(tmp_path):
    rows = fixture(tmp_path)
    assert (
        Prototype(tmp_path).collect([(rows[0][0], 1, rows[0][2] + " forged")])[
            "source_relations"
        ]
        == []
    )


def test_ambiguous_source_basename_is_not_resolved_by_first_hit(tmp_path):
    rows = fixture(tmp_path)
    (tmp_path / "repo/other").mkdir()
    (tmp_path / "repo/other/worker.c").write_text(
        (tmp_path / "repo/worker.c").read_text()
    )
    assert Prototype(tmp_path).collect(rows)["source_relations"] == []


def test_ambiguous_enum_definitions_are_not_resolved_by_first_hit(tmp_path):
    rows = fixture(tmp_path)
    p = tmp_path / "repo/state.h"
    p.write_text(p.read_text() + "\nenum OtherPhase { BAD = 40, OTHER_GOOD };\n")
    assert Prototype(tmp_path).collect(rows)["source_relations"] == []


def test_alias_is_resolved_from_source_not_pointer_name_prefix(tmp_path):
    rows = fixture(
        tmp_path,
        decl="typedef struct { int mode; int other; } MSG;\ntypedef enum { BAD=17, GOOD } Phase;\n",
    )
    assert Prototype(tmp_path).collect(rows)["source_relations"] == []


def test_source_mutation_aborts_without_stale_evidence(tmp_path):
    rows = fixture(tmp_path)
    probe = Prototype(tmp_path)
    old = probe.definitions

    def mutate(*a):
        result = old(*a)
        p = tmp_path / "repo/state.h"
        p.write_text(p.read_text() + "\n// changed")
        return result

    probe.definitions = mutate
    with pytest.raises(ValueError, match="mutation"):
        probe.collect(rows)


@pytest.mark.parametrize(
    "limit,value,message",
    [
        ("MAX_ENTRIES", 1, "enumeration"),
        ("MAX_BYTES", 8, "byte bound"),
        ("MAX_SECONDS", -1, "time bound"),
    ],
)
def test_limits_prevent_partial_uniqueness_or_unbounded_scan(
    tmp_path, monkeypatch, limit, value, message
):
    rows = fixture(tmp_path)
    monkeypatch.setattr(mod, limit, value)
    with pytest.raises(ValueError, match=message):
        Prototype(tmp_path).collect(rows)


def test_source_symlink_is_not_followed(tmp_path):
    rows = fixture(tmp_path)
    p = tmp_path / "repo/worker.c"
    text = p.read_text()
    p.unlink()
    outside = tmp_path / "outside.c"
    outside.write_text(text)
    p.symlink_to(outside)
    assert Prototype(tmp_path).collect(rows)["source_relations"] == []


@pytest.mark.parametrize(
    "change",
    [
        ("void compare(PMSG r)", "void compare(PMSG r, int BAD)"),
        ("if (r->mode == BAD)", "int BAD = 4; if (r->mode == BAD)"),
        ("if (r->mode == BAD)", "enum Local { BAD=7 }; if (r->mode == BAD)"),
    ],
)
def test_shadowed_names_are_not_global_enum_references(tmp_path, change):
    rows = fixture(tmp_path)
    p = tmp_path / "repo/worker.c"
    p.write_text(p.read_text().replace(*change))
    assert Prototype(tmp_path).collect(rows)["source_relations"] == []


def test_macro_override_does_not_become_an_enum_value_mapping(tmp_path):
    rows = fixture(tmp_path)
    p = tmp_path / "repo/state.h"
    p.write_text(p.read_text() + "\n#define BAD 99\n")
    assert Prototype(tmp_path).collect(rows)["source_relations"] == []


def test_log_mutation_aborts_without_stale_evidence(tmp_path):
    rows = fixture(tmp_path)
    probe = Prototype(tmp_path)
    old = probe.definitions

    def mutate(*a):
        result = old(*a)
        p = tmp_path / "logs/input.log"
        p.write_text(p.read_text() + "changed\n")
        return result

    probe.definitions = mutate
    with pytest.raises(ValueError, match="log mutation"):
        probe.collect(rows)


def test_external_include_symlink_does_not_fall_back_to_another_header(tmp_path):
    rows = fixture(tmp_path)
    h = tmp_path / "repo/state.h"
    text = h.read_text()
    h.unlink()
    outside = tmp_path / "outside.h"
    outside.write_text(text)
    h.symlink_to(outside)
    (tmp_path / "repo/other").mkdir()
    (tmp_path / "repo/other/state.h").write_text(text)
    assert Prototype(tmp_path).collect(rows)["source_relations"] == []


def test_metadata_budget_does_not_emit_oversized_payload(tmp_path, monkeypatch):
    rows = fixture(tmp_path)
    monkeypatch.setattr(mod, "MAX_OUTPUT", 10)
    with pytest.raises(ValueError, match="metadata budget"):
        Prototype(tmp_path).collect(rows)


def test_full_message_prefix_must_match_current_format(tmp_path):
    rows = fixture(tmp_path, format="other_message mode=%d")
    assert Prototype(tmp_path).collect(rows)["source_relations"] == []


def test_matching_field_with_two_incompatible_print_arguments_is_ambiguous(tmp_path):
    rows = fixture(tmp_path)
    p = tmp_path / "repo/worker.c"
    p.write_text(
        p.read_text().replace(
            'log_info("mode=%d", r->mode);',
            'log_info("mode=%d", r->mode); log_info("mode=%d", r->other);',
        )
    )
    assert Prototype(tmp_path).collect(rows)["source_relations"] == []


def test_repeated_string_separator_is_rejected_without_unbounded_backtracking(tmp_path):
    rows = fixture(tmp_path)
    p = tmp_path / "repo/worker.c"
    p.write_text(
        p.read_text().replace(
            '"mode=%d", r->mode', '"name=%s mode=%d", "value", r->mode'
        )
    )
    line = rows[0][2].replace("mode=18", "name=a mode=18 mode=18")
    (tmp_path / "logs/input.log").write_text(line + "\n")
    assert (
        Prototype(tmp_path).collect([("logs/input.log", 1, line)])["source_relations"]
        == []
    )


def test_format_length_bound_is_not_a_partial_binding(tmp_path, monkeypatch):
    rows = fixture(tmp_path)
    monkeypatch.setattr(mod, "MAX_FORMAT_BYTES", 5)
    assert Prototype(tmp_path).collect(rows)["source_relations"] == []


def test_format_specifier_count_bound_is_not_a_partial_binding(tmp_path, monkeypatch):
    rows = fixture(tmp_path)
    p = tmp_path / "repo/worker.c"
    p.write_text(
        p.read_text().replace('"mode=%d", r->mode', '"name=%s mode=%d", "x", r->mode')
    )
    line = rows[0][2].replace("mode=18", "name=x mode=18")
    (tmp_path / "logs/input.log").write_text(line + "\n")
    monkeypatch.setattr(mod, "MAX_SPECIFIERS", 1)
    assert (
        Prototype(tmp_path).collect([("logs/input.log", 1, line)])["source_relations"]
        == []
    )


def search_event(rows, *, column=0, excerpted=False):
    from app.agents.evidence_tools import encode_evidence

    matches = [
        {
            "path": path,
            "line": number,
            "column": column,
            "text": text,
            "line_excerpted": excerpted,
            "read_request": {"path": path, "start_line": number, "line_count": 20},
        }
        for path, number, text in rows
    ]
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__evidence__search_evidence",
        "tool_input": {"requests": [{"path": "logs/input.log", "pattern": "mode"}]},
        "tool_response": [
            {
                "type": "text",
                "text": encode_evidence({"results": [{"matches": matches}]}),
            }
        ],
    }


@pytest.mark.asyncio
async def test_registered_hook_preserves_result_supplies_complete_relation_and_deduplicates(
    tmp_path,
):
    import copy
    from app.agents.evidence_context import SourceContextEnricher, MAX_CONTEXT_BYTES

    data = search_event(fixture(tmp_path))
    before = copy.deepcopy(data)
    hook = SourceContextEnricher(str(tmp_path))
    result = await hook.after_tool(data, "first", {})
    text = result["hookSpecificOutput"]["additionalContext"]
    payload = json.loads(text.split("\n", 1)[1])
    assert payload["source_context"] == []
    assert len(payload["source_relations"]) == 1
    assert payload["source_relations"][0]["field"] == "mode"
    assert len(text.encode()) <= MAX_CONTEXT_BYTES
    assert data == before
    assert await hook.after_tool(data, "new-call-same-fact", {}) == {}


@pytest.mark.asyncio
async def test_relation_never_clips_enum_or_consumes_dedup_before_shared_budget_fits(
    tmp_path, monkeypatch
):
    from app.agents import evidence_context

    data = search_event(fixture(tmp_path))
    hook = evidence_context.SourceContextEnricher(str(tmp_path))
    maximum = evidence_context.MAX_CONTEXT_BYTES
    monkeypatch.setattr(evidence_context, "MAX_CONTEXT_BYTES", 600)
    assert await hook.after_tool(data, "too-small", {}) == {}
    assert not hook.related_seen
    monkeypatch.setattr(evidence_context, "MAX_CONTEXT_BYTES", maximum)
    assert await hook.after_tool(data, "fits", {})


@pytest.mark.asyncio
async def test_existing_source_window_has_priority_over_two_window_relation(
    tmp_path, monkeypatch
):
    from app.agents.evidence_context import SourceContextEnricher

    rows = fixture(tmp_path)
    code = (tmp_path / "repo/worker.c").read_text().splitlines()[1]
    data = search_event([*rows, ("repo/worker.c", 2, code)])
    data["tool_input"]["requests"].append(
        {"path": "repo/worker.c", "pattern": "log_event"}
    )
    monkeypatch.setattr(
        mod.SourceFieldRelations, "collect", lambda *_: pytest.fail("No two free slots")
    )
    text = (await SourceContextEnricher(str(tmp_path)).after_tool(data, "mixed", {}))[
        "hookSpecificOutput"
    ]["additionalContext"]
    payload = json.loads(text.split("\n", 1)[1])
    assert payload["source_context"] and "source_relations" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_page", ["column", "excerpt", "text"])
async def test_unverified_log_page_adds_no_relation(tmp_path, bad_page):
    from app.agents.evidence_context import SourceContextEnricher

    rows = fixture(tmp_path)
    if bad_page == "text":
        rows = [(rows[0][0], 1, rows[0][2] + " forged")]
    data = search_event(
        rows, column=1 if bad_page == "column" else 0, excerpted=bad_page == "excerpt"
    )
    assert (
        await SourceContextEnricher(str(tmp_path)).after_tool(data, bad_page, {}) == {}
    )


@pytest.mark.asyncio
async def test_missing_optional_parser_is_not_an_agent_tool_failure(
    tmp_path, monkeypatch
):
    import sys
    from app.agents.evidence_context import SourceContextEnricher

    data = search_event(fixture(tmp_path))
    monkeypatch.setitem(sys.modules, "tree_sitter", None)
    assert (
        await SourceContextEnricher(str(tmp_path)).after_tool(data, "no-parser", {})
        == {}
    )


@pytest.mark.asyncio
async def test_log_first_clone_later_revalidates_references_without_another_search(
    tmp_path,
):
    import hashlib
    from app.agents.evidence_context import SourceContextEnricher, MCP_CLONE_TOOL

    rows = fixture(tmp_path)
    (tmp_path / "repo").rename(tmp_path / "staged-repo")
    hook = SourceContextEnricher(str(tmp_path))
    assert await hook.after_tool(search_event(rows), "log-before-clone", {}) == {}
    assert hook.related_references == {
        (rows[0][0], rows[0][1]): hashlib.sha256(rows[0][2].encode()).hexdigest()
    }
    (tmp_path / "staged-repo").rename(tmp_path / "repo")
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": MCP_CLONE_TOOL,
        "tool_input": {},
        "tool_response": [],
    }
    result = await hook.after_tool(event, "clone-complete", {})
    payload = json.loads(
        result["hookSpecificOutput"]["additionalContext"].split("\n", 1)[1]
    )
    assert payload["source_relations"][0]["field"] == "mode"
    assert await hook.after_tool(event, "same-clone-fact", {}) == {}


@pytest.mark.asyncio
async def test_pending_log_reference_hash_rejects_changed_or_forged_text(tmp_path):
    from app.agents.evidence_context import SourceContextEnricher, MCP_CLONE_TOOL

    rows = fixture(tmp_path)
    (tmp_path / "repo").rename(tmp_path / "staged-repo")
    hook = SourceContextEnricher(str(tmp_path))
    assert await hook.after_tool(search_event(rows), "before-clone", {}) == {}
    p = tmp_path / rows[0][0]
    p.write_text(p.read_text().replace("mode=18", "mode=19"))
    (tmp_path / "staged-repo").rename(tmp_path / "repo")
    event = {"hook_event_name": "PostToolUse", "tool_name": MCP_CLONE_TOOL}
    assert await hook.after_tool(event, "clone", {}) == {}


@pytest.mark.asyncio
async def test_pending_references_are_bounded_and_hold_no_log_text(
    tmp_path, monkeypatch
):
    from app.agents import evidence_context

    rows = fixture(tmp_path)
    monkeypatch.setattr(evidence_context, "MAX_RELATED_REFERENCES", 2)
    hook = evidence_context.SourceContextEnricher(str(tmp_path))
    await hook.after_tool(
        search_event([(rows[0][0], n, rows[0][2]) for n in range(1, 6)]), "bounded", {}
    )
    assert len(hook.related_references) == 2
    assert all(len(digest) == 64 for digest in hook.related_references.values())


def test_registered_matcher_includes_repository_ready_event(tmp_path):
    import re
    from app.agents.evidence_context import MCP_CLONE_TOOL
    from app.agents.evidence_policy import execution_hooks
    hooks = execution_hooks("zh", workspace=str(tmp_path), source_context=True)
    assert re.fullmatch(hooks["PostToolUse"][0].matcher, MCP_CLONE_TOOL)
