"""Atomic literal edits to public narrative fields; never fuzzy-match or rewrite schema."""

import copy
import re

CITE = re.compile(
    r"(?:log:|repo:)?[A-Za-z0-9_./\-\[\]]+\.(?:log|txt|c|h|cc|hpp|cpp|py):[0-9]+"
    r"|(?<![A-Za-z0-9_])log:[0-9]+"
)


def apply(draft, patch):
    if (
        not isinstance(patch, dict)
        or set(patch) != {"edits"}
        or not isinstance(patch["edits"], list)
        or len(patch["edits"]) > 16
    ):
        raise ValueError("edit schema")
    allowed = [["answer"], ["summary"]]
    for i, h in enumerate(draft.get("root_cause_hypotheses", [])):
        if isinstance(h, dict) and isinstance(h.get("hypothesis"), str):
            allowed.append(["root_cause_hypotheses", i, "hypothesis"])
    for i, a in enumerate(draft.get("recommended_actions", [])):
        if isinstance(a, str):
            allowed.append(["recommended_actions", i])
        elif isinstance(a, dict) and isinstance(a.get("action"), str):
            allowed.append(["recommended_actions", i, "action"])
    groups = {}
    for edit in patch["edits"]:
        if not isinstance(edit, dict) or set(edit) != {"path", "before", "after"}:
            raise ValueError("edit fields")
        path = edit["path"]
        before = edit["before"]
        after = edit["after"]
        if (
            not isinstance(path, list)
            or any(type(x) not in (str, int) for x in path)
            or path not in allowed
            or not isinstance(before, str)
            or not before
            or len(before.encode()) > 1500
            or not isinstance(after, str)
            or not after.strip()
            or len(after.encode()) > 2000
        ):
            raise ValueError("invalid narrative edit")
        value = draft
        for part in path:
            value = value[part]
        if not isinstance(value, str) or value.count(before) != 1:
            raise ValueError("nonunique original span")
        start = value.index(before)
        end = start + len(before)
        key = tuple(path)
        for a, b, _ in groups.get(key, []):
            if start < b and a < end:
                raise ValueError("overlapping original spans")
        groups.setdefault(key, []).append((start, end, after))
    result = copy.deepcopy(draft)
    for path, replacements in groups.items():
        parent = result
        for part in path[:-1]:
            parent = parent[part]
        value = parent[path[-1]]
        for a, b, after in sorted(replacements, reverse=True):
            value = value[:a] + after + value[b:]
        if any(
            value.count(ref) < parent[path[-1]].count(ref)
            for ref in set(CITE.findall(parent[path[-1]]))
        ):
            raise ValueError("citation removed or altered")
        parent[path[-1]] = value
    return result
