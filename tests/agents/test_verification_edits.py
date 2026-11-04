import copy
import pytest
from app.agents.verification_edits import apply


def fixture():
    return {
        "status": "ok",
        "answer": "observed A. actor definitely B. future always C.",
        "summary": "summary S",
        "root_cause_hypotheses": [
            {"hypothesis": "hypothesis H", "evidence": ["log:logs/input.log:1"]}
        ],
        "recommended_actions": [
            "check X",
            {"action": "check Y", "evidence": ["repo:code.c:1"]},
        ],
    }


def edit(path, before, after):
    return {"path": path, "before": before, "after": after}


def test_atomic_multiple_literal_spans_leave_evidence_and_other_bytes_unchanged():
    d = fixture()
    before = copy.deepcopy(d)
    r = apply(
        d,
        {
            "edits": [
                edit(["answer"], "actor definitely B", "actor unknown"),
                edit(["answer"], "future always C", "future unobserved"),
                edit(
                    ["recommended_actions", 1, "action"],
                    "check Y",
                    "check Y conditionally",
                ),
            ]
        },
    )
    assert (
        d == before and r["answer"] == "observed A. actor unknown. future unobserved."
    )
    assert (
        r["root_cause_hypotheses"] == d["root_cause_hypotheses"]
        and r["status"] == d["status"]
    )
    assert (
        r["recommended_actions"][1]["evidence"]
        == d["recommended_actions"][1]["evidence"]
    )


@pytest.mark.parametrize(
    "path",
    [
        ["status"],
        ["root_cause_hypotheses", 0, "evidence", 0],
        ["missing"],
        ["recommended_actions", True],
        ["recommended_actions", 99],
        "/answer",
    ],
)
def test_schema_metadata_evidence_and_invalid_paths_are_rejected(path):
    with pytest.raises(ValueError):
        apply(fixture(), {"edits": [edit(path, "ok", "new")]})


@pytest.mark.parametrize(
    "before,after",
    [
        ("", "new"),
        ("missing", "new"),
        ("observed", ""),
        ("observed", 3),
        ("x" * 1501, "new"),
    ],
)
def test_no_fuzzy_or_empty_or_oversized_spans(before, after):
    with pytest.raises(ValueError):
        apply(fixture(), {"edits": [edit(["answer"], before, after)]})


def test_ambiguous_and_overlapping_spans_fail_without_partial_changes():
    d = fixture()
    before = copy.deepcopy(d)
    with pytest.raises(ValueError):
        apply(
            d,
            {
                "edits": [
                    edit(["answer"], "observed A", "changed"),
                    edit(["answer"], "A. actor", "another"),
                ]
            },
        )
    assert d == before
    d["answer"] = "same same"
    with pytest.raises(ValueError):
        apply(d, {"edits": [edit(["answer"], "same", "different")]})


def test_empty_patch_preserves_every_field_and_value():
    d = fixture()
    assert apply(d, {"edits": []}) == d


def test_inline_native_citations_cannot_be_removed_or_changed():
    d = fixture()
    d["answer"] = "claim (log:logs/input.log:1)"
    with pytest.raises(ValueError):
        apply(d, {"edits": [edit(["answer"], d["answer"], "qualified claim")]})
    r = apply(d, {"edits": [edit(["answer"], "claim", "unverified claim")]})
    assert r["answer"] == "unverified claim (log:logs/input.log:1)"


@pytest.mark.parametrize("reference", ["log:55774", "repo:src/input.cc:3", "logs/input.txt:4"])
def test_short_and_alternate_native_citations_remain_intact(reference):
    draft = fixture()
    draft["answer"] = "claim " + reference
    with pytest.raises(ValueError):
        apply(draft, {"edits": [edit(["answer"], draft["answer"], "qualified claim")]})
