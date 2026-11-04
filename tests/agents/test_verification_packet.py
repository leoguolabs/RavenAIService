import json
import pytest
from app.agents import verification_packet as p


def fixture(root):
    (root / "logs").mkdir()
    (root / "repo").mkdir()
    lines = [
        f"2026-10-01T00:00:{i:02}.000 INFO Tid:7 worker.c report 10 value={i}"
        for i in range(40)
    ]
    (root / "logs/input.log").write_text("\n".join(lines) + "\n")
    (root / "repo/worker.c").write_text("\n".join("line" + str(i) for i in range(100)))
    return {
        "status": "ok",
        "answer": "Inspect log:logs/input.log:10 and repo:worker.c:20",
        "summary": "summary",
        "root_cause_hypotheses": [
            {"hypothesis": "possible", "evidence": ["log:logs/input.log:20"]}
        ],
        "tool_trace": "SECRET_METADATA",
        "raw": "SECRET_RAW",
    }


def test_public_allowlist_citations_windows_and_unverified_runtime(tmp_path):
    d = p.build(tmp_path, fixture(tmp_path))
    assert "tool_trace" not in d["draft"] and "raw" not in d["draft"]
    assert "SECRET" not in json.dumps(d)
    assert d["evidence_limits"]["source_runtime_identity"] == "not_established"
    assert d["cited_windows"] and all(
        not f["semantic_whole_file_review"] for f in d["cited_files"]
    )
    w = next(w for w in d["cited_windows"] if w["path"] == "logs/input.log")
    assert w["start_line"] == 6 and w["end_line"] == 32
    assert len(w["rows"]) == 27
    assert all(
        w["rows"][i][0] + 1 == w["rows"][i + 1][0] for i in range(len(w["rows"]) - 1)
    )


@pytest.mark.parametrize(
    "path", ["repo/../secret.c", "repo/.git/config", "repo/.env", "/outside/file.c"]
)
def test_unsafe_reference_is_not_read(tmp_path, path):
    draft = fixture(tmp_path)
    draft["answer"] = "repo:" + path + ":1"
    draft.pop("root_cause_hypotheses")
    d = p.build(tmp_path, draft)
    assert not d["cited_windows"] and d["omitted_citations"]


def test_symlink_citation_is_rejected(tmp_path):
    draft = fixture(tmp_path)
    (tmp_path / "repo/link.c").symlink_to(tmp_path / "repo/worker.c")
    draft["answer"] = "repo:link.c:1"
    draft.pop("root_cause_hypotheses")
    assert p.build(tmp_path, draft)["omitted_citations"]


def test_missing_and_out_of_range_citations_are_explicit(tmp_path):
    draft = fixture(tmp_path)
    draft["answer"] = "log:logs/missing.log:1 log:logs/input.log:999"
    draft.pop("root_cause_hypotheses")
    d = p.build(tmp_path, draft)
    assert len(d["omitted_citations"]) == 2 and not d["cited_windows"]


def test_binary_data_has_metadata_not_text(tmp_path):
    draft = fixture(tmp_path)
    (tmp_path / "logs/data.bin").write_bytes(b"\0" * 55)
    draft["answer"] = "log:logs/data.bin:1"
    draft.pop("root_cause_hypotheses")
    d = p.build(tmp_path, draft)
    assert not d["cited_windows"]
    assert (
        d["cited_files"][0]["bytes"] == 55 and d["cited_files"][0]["binary_or_non_utf8"]
    )


def test_vertical_tab_does_not_shift_native_lines(tmp_path):
    draft = fixture(tmp_path)
    f = tmp_path / "logs/input.log"
    f.write_text(f.read_text().replace("value=0", "value=0\vcontinued"))
    d = p.build(tmp_path, draft)
    assert (
        next(f for f in d["cited_files"] if f["path"] == "logs/input.log")[
            "total_lf_lines"
        ]
        == 40
    )


@pytest.mark.parametrize(
    "limit,value",
    [("MAX_REFS", 0), ("MAX_ENTRIES", 0), ("MAX_SECONDS", -1), ("MAX_PACKET", 1)],
)
def test_global_limits_fail_without_a_complete_packet(
    tmp_path, monkeypatch, limit, value
):
    draft = fixture(tmp_path)
    monkeypatch.setattr(p, limit, value)
    with pytest.raises(ValueError):
        p.build(tmp_path, draft)


def test_packet_budget_marks_omissions_without_clipping_windows(tmp_path, monkeypatch):
    draft = fixture(tmp_path)
    d = p.build(tmp_path, draft)
    monkeypatch.setattr(
        p,
        "MAX_PACKET",
        len(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode()) - 10,
    )
    d = p.build(tmp_path, draft)
    assert (
        len(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode())
        <= p.MAX_PACKET
    )
    assert d["omitted_citations"] and all(
        w["range_complete"] for w in d["cited_windows"]
    )


def test_file_identity_change_fails_before_emission(tmp_path, monkeypatch):
    draft = fixture(tmp_path)
    original = p.producer.identity
    count = 0

    def changed(path):
        nonlocal count
        count += 1
        v = original(path)
        return v if count <= 1 else (*v[:-1], v[-1] + 1)

    monkeypatch.setattr(p.producer, "identity", changed)
    with pytest.raises(ValueError):
        p.build(tmp_path, draft)


def test_ref_without_evidence_has_explicit_empty_scope(tmp_path):
    draft = fixture(tmp_path)
    draft["answer"] = "No citations"
    draft.pop("root_cause_hypotheses")
    d = p.build(tmp_path, draft)
    assert not d["cited_windows"]
    assert d["evidence_limits"]["attachment_semantic_coverage"] == "not_established"


def test_small_c_function_is_not_cut_before_its_completion_operations(tmp_path):
    d = fixture(tmp_path)
    (tmp_path / "repo/worker.c").write_text(
        "void write_record(void) {\n"
        + "".join(f"  call_{i}();\n" for i in range(50))
        + "  flush_file();\n  close_file();\n}\n"
    )
    d["answer"] = "repo:worker.c:25"
    d.pop("root_cause_hypotheses")
    packet = p.build(tmp_path, d)
    w = packet["cited_windows"][0]
    assert w["start_line"] == 1 and w["end_line"] == 54
    assert w["complete_source_ranges"][0]["kind"] == "function"
    assert any("close_file" in row[1] for row in w["rows"])
    assert packet["evidence_limits"]["source_runtime_identity"] == "not_established"


def test_large_c_function_keeps_partial_window_label(tmp_path):
    d = fixture(tmp_path)
    (tmp_path / "repo/worker.c").write_text(
        "void big(void) {\n" + "".join(f"  call_{i}();\n" for i in range(200)) + "}\n"
    )
    d["answer"] = "repo:worker.c:25"
    d.pop("root_cause_hypotheses")
    packet = p.build(tmp_path, d)
    w = packet["cited_windows"][0]
    assert (
        w["start_line"] > 1 and w["end_line"] < 202 and not w["complete_source_ranges"]
    )


def test_omissions_reference_original_citations_not_synthetic_window_starts(
    tmp_path, monkeypatch
):
    draft = fixture(tmp_path)
    full = p.build(tmp_path, draft)
    monkeypatch.setattr(
        p,
        "MAX_PACKET",
        len(json.dumps(full, ensure_ascii=False, separators=(",", ":")).encode()) - 10,
    )
    packet = p.build(tmp_path, draft)
    refs = set(p.citations(packet["draft"]))
    assert all((x["path"], x["line"]) in refs for x in packet["omitted_citations"])
    coverage = packet["literal_citation_window_coverage"]
    assert (
        coverage["included"] + coverage["omitted"] == coverage["requested"] == len(refs)
    )


def test_short_log_citation_requires_unique_explicit_attachment(tmp_path):
    draft = fixture(tmp_path)
    draft["answer"] = "Inspect input.log (log:30)"
    draft.pop("root_cause_hypotheses")
    packet = p.build(tmp_path, draft)
    assert packet["resolved_short_citations"] == [
        {"reference": "log:30", "path": "logs/input.log", "line": 30}
    ]
    assert any(
        w["path"] == "logs/input.log" and w["start_line"] <= 30 <= w["end_line"]
        for w in packet["cited_windows"]
    )
    assert not packet["unresolved_short_citations"]
    draft["answer"] = "Inspect log:30"
    packet = p.build(tmp_path, draft)
    assert (
        not packet["resolved_short_citations"] and packet["unresolved_short_citations"]
    )


@pytest.mark.parametrize("duplicate_name", [False, True])
def test_ambiguous_short_citation_never_guesses_attachment(tmp_path, duplicate_name):
    draft = fixture(tmp_path)
    (tmp_path / "logs/sub").mkdir()
    name = "input.log" if duplicate_name else "other.log"
    (tmp_path / "logs/sub" / name).write_text("data\n")
    draft["answer"] = "Inspect input.log other.log (log:1)"
    draft.pop("root_cause_hypotheses")
    packet = p.build(tmp_path, draft)
    assert (
        not packet["resolved_short_citations"] and packet["unresolved_short_citations"]
    )


def test_explicit_paths_are_never_misread_as_short_references(tmp_path):
    draft = fixture(tmp_path)
    draft["answer"] = "log:logs/input.log:999 logs/input.log:2"
    packet = p.build(tmp_path, draft)
    assert (
        not packet["resolved_short_citations"]
        and not packet["unresolved_short_citations"]
    )


def typed_fixture(root):
    (root / "logs").mkdir()
    (root / "repo").mkdir()
    (root / "repo/state.h").write_text(
        "typedef struct { int mode; } MSG, *PMSG;\ntypedef enum { BAD=17, GOOD, UNKNOWN } Phase;\n"
    )
    (root / "repo/worker.c").write_text(
        '#include "state.h"\nvoid log_event(MSG *r) { log_info("mode=%d", r->mode); }\nvoid compare(PMSG r) { if (r->mode == BAD) return; }\n'
    )
    (root / "logs/input.log").write_text(
        "2026-10-01T00:00:00.000,Tick=1 INFO Tid:2 worker.c log_event 2 mode=18\n"
    )
    return {"status": "ok", "answer": "input.log (log:1)", "summary": "Mode observed"}


def test_verified_short_citation_links_typed_source_with_shared_read_cache(
    tmp_path, monkeypatch
):
    from collections import Counter

    draft = typed_fixture(tmp_path)
    reads = Counter()
    read = p.producer.read_regular

    def counted(path):
        reads[path.name] += 1
        return read(path)

    monkeypatch.setattr(p.producer, "read_regular", counted)
    monkeypatch.setattr(p.producer, "collect", lambda *a, **kw: None)
    packet = p.build(tmp_path, draft)
    relations = packet["source_field_relations"]["source_relations"]
    assert len(relations) == 1 and relations[0]["field"] == "mode"
    assert "BAD=17" in json.dumps(relations[0]["enum_body"])
    assert reads == {"input.log": 1, "worker.c": 1, "state.h": 1}
    assert packet["evidence_limits"]["source_runtime_identity"] == "not_established"


def test_typed_relations_honor_packet_shared_byte_budget(tmp_path, monkeypatch):
    draft = typed_fixture(tmp_path)
    monkeypatch.setattr(p, "MAX_BYTES", (tmp_path / "logs/input.log").stat().st_size)
    packet = p.build(tmp_path, draft)
    assert packet["source_field_relations"] is None
    assert "byte bound" in packet["source_relations_unavailable_reason"]


def test_source_mutation_during_typed_relation_never_passes(tmp_path, monkeypatch):
    from app.agents.source_field_relations import SourceFieldRelations

    draft = typed_fixture(tmp_path)
    original = SourceFieldRelations.collect

    def changed(self, rows):
        result = original(self, rows)
        with (tmp_path / "repo/worker.c").open("a") as stream:
            stream.write("// concurrent change\n")
        return result

    monkeypatch.setattr(SourceFieldRelations, "collect", changed)
    with pytest.raises(p.PacketChanged):
        p.build(tmp_path, draft)


def test_missing_optional_relation_parser_keeps_native_windows(tmp_path, monkeypatch):
    import sys

    draft = typed_fixture(tmp_path)
    monkeypatch.setitem(sys.modules, "tree_sitter", None)
    packet = p.build(tmp_path, draft)
    assert packet["cited_windows"] and packet["source_field_relations"] is None
    assert "ModuleNotFoundError" in packet["source_relations_unavailable_reason"]


def test_typed_relation_extra_files_keep_fixed_snapshot_bound(tmp_path, monkeypatch):
    draft = typed_fixture(tmp_path)
    monkeypatch.setattr(p, "MAX_FILES", 2)
    packet = p.build(tmp_path, draft)
    assert len(packet["cited_files"]) == 2
    assert packet["source_field_relations"] is None
    assert "file bound" in packet["source_relations_unavailable_reason"]
