"""Behavioral checks: complete paging, independent errors and workspace bounds."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.agents.evidence_tools import MAX_READ_CHARS, MAX_SEARCH_CONTEXT_CHARS, encode_evidence, read_page, run_batch
from app.agents.evidence_policy import prepare_snapshot, snapshot_prompt, use_snapshot_in_templates
from app.agents.log_analysis.mcp_tools import build_project_fit_guidance


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    async def test_read_pages_preserve_unicode_long_lines_and_empty_lines(self):
        original = ["天线" * MAX_READ_CHARS, "", "last"]
        (self.root / "log.txt").write_text("\n".join(original), encoding="utf-8")
        request = {"path": "log.txt", "line_count": 2}
        restored = {}
        while request:
            result = read_page(self.root, request)
            for line in result["lines"]:
                previous = restored.get(line["line"], "")
                self.assertEqual(line["column"], len(previous))
                restored[line["line"]] = previous + line["text"]
            request = result["next_request"]
        self.assertEqual(list(restored.values()), original)

    @unittest.skipUnless(shutil.which("rg"), "ripgrep required")
    async def test_search_and_read_agree_with_native_lf_lines_and_keep_control_characters(self):
        expected = ["first\rprogress", "second", "third\u2028still\rdata", "last\r"]
        (self.root / "terminal.log").write_bytes(
            "first\rprogress\nsecond\r\nthird\u2028still\rdata\nlast\r".encode()
        )
        result = await run_batch(str(self.root), [{"path": "terminal.log", "pattern": "progress|second|still|last"}], search=True)
        matches = result["results"][0]["matches"]
        self.assertEqual([m["line"] for m in matches], [1, 2, 3, 4])
        self.assertEqual([m["text"] for m in matches], expected)
        for match, text in zip(matches, expected):
            page = read_page(self.root, {**match["read_request"], "line_count": 1})
            self.assertEqual(page["lines"], [{"line": match["line"], "column": 0, "text": text}])

    async def test_long_line_continuation_preserves_embedded_carriage_returns(self):
        original = "进度\r" * MAX_READ_CHARS
        (self.root / "progress.log").write_bytes(original.encode())
        request = {"path": "progress.log", "line_count": 1}
        restored = ""
        while request:
            page = read_page(self.root, request)
            self.assertEqual(page["lines"][0]["line"], 1)
            self.assertEqual(page["lines"][0]["column"], len(restored))
            restored += page["lines"][0]["text"]
            request = page["next_request"]
        self.assertEqual(restored, original)

    async def test_tail_uses_lf_line_numbers_and_preserves_continuation(self):
        content = "banner\rprogress\n" + "天\u2028\r" * 4000 + "end\nlast"
        (self.root / "tail.log").write_bytes(content.encode())
        first = read_page(self.root, {"path": "tail.log", "tail_lines": 2})
        self.assertEqual(first["total_lines"], 3)
        self.assertEqual(first["lines"][0]["line"], 2)
        restored = {}
        page = first
        while True:
            for row in page["lines"]:
                self.assertEqual(row["column"], len(restored.get(row["line"], "")))
                restored[row["line"]] = restored.get(row["line"], "") + row["text"]
            if not page["next_request"]:
                break
            self.assertNotIn("tail_lines", page["next_request"])
            page = read_page(self.root, page["next_request"])
        self.assertEqual(restored, {2: "天\u2028\r" * 4000 + "end", 3: "last"})
        for raw, count in ((b"", 0), (b"\n", 1), (b"a\r\nb\n", 2)):
            (self.root / "tail.log").write_bytes(raw)
            result = read_page(self.root, {"path": "tail.log", "tail_lines": 10})
            self.assertEqual(result["total_lines"], count)

    async def test_tail_rejects_conflicting_coordinates_and_keeps_batch_errors(self):
        (self.root / "log").write_text("a\nb")
        requests = [{"path": "log", "tail_lines": 1, field: 1} for field in ("start_line", "column", "line_count")]
        requests += [{"path": "log", "tail_lines": True}, {"path": "log", "tail_lines": 0},
                     {"path": "log", "tail_lines": 1}]
        result = (await run_batch(str(self.root), requests, search=False))["results"]
        self.assertTrue(all("error" in row for row in result[:-1]))
        self.assertEqual(result[-1]["lines"], [{"line": 2, "column": 0, "text": "b"}])

    @unittest.skipUnless(shutil.which("rg"), "ripgrep required")
    async def test_search_context_merges_ranges_without_dropping_search_hits(self):
        lines = [f"line {n}" + (" target" if n in (3, 5, 8, 10) else "") for n in range(1, 13)]
        (self.root / "source.c").write_text("\n".join(lines))
        request = {"path": "source.c", "pattern": "target", "context_lines": 2}
        result = await run_batch(str(self.root), [request], search=True)
        row = result["results"][0]
        self.assertEqual([hit["line"] for hit in row["matches"]], [3, 5, 8, 10])
        self.assertEqual(row["matching_lines_total"], 4)
        self.assertEqual(row["context_matches_not_expanded"], 1)
        self.assertEqual(row["context_matches_requested"], 3)
        self.assertEqual(len(row["context_windows"]), 1)
        window = row["context_windows"][0]
        self.assertEqual([line["text"] for line in window["lines"]], lines[:10])
        self.assertTrue(window["range_complete"])
        self.assertIsNone(window["next_request"])
        # The selected context ends at line 10 although this file has 12 lines.
        wire = json.loads(encode_evidence(result))
        self.assertEqual(wire["results"][0]["context_windows"][0]["lines"][0], [1, 0, lines[0]])
        self.assertEqual(wire["line_columns"], ["line", "column", "text"])

    @unittest.skipUnless(shutil.which("rg"), "ripgrep required")
    async def test_search_context_budget_and_exact_long_line_continuation(self):
        lines = ["prefix" * 2000, "target\rprogress", "last"]
        (self.root / "source.c").write_bytes("\n".join(lines).encode())
        row = (await run_batch(str(self.root), [{"path": "source.c", "pattern": "target", "context_lines": 1}], search=True))["results"][0]
        self.assertEqual(row["matches"][0]["line"], 2)
        window = row["context_windows"][0]
        self.assertFalse(window["range_complete"])
        self.assertLessEqual(sum(len(line["text"]) for line in window["lines"]), MAX_SEARCH_CONTEXT_CHARS)
        restored = {line["line"]: line["text"] for line in window["lines"]}
        request = window["next_request"]
        while request:
            page = read_page(self.root, request)
            for line in page["lines"]:
                restored[line["line"]] = restored.get(line["line"], "") + line["text"]
            request = page["next_request"]
        self.assertEqual(list(restored.values()), lines)

    @unittest.skipUnless(shutil.which("rg"), "ripgrep required")
    async def test_context_errors_do_not_erase_search_evidence(self):
        (self.root / "log").write_text("target")
        with patch("app.agents.evidence_tools.read_page", side_effect=OSError("file changed")):
            row = (await run_batch(str(self.root), [{"path": "log", "pattern": "target", "context_lines": 2}], search=True))["results"][0]
        self.assertEqual(row["matches"][0]["text"], "target")
        self.assertEqual(row["context_windows"][0]["error"], "file changed")
        self.assertFalse(row["context_windows"][0]["range_complete"])
        for value in (-1, 41, True):
            row = (await run_batch(str(self.root), [{"pattern": "target", "context_lines": value}], search=True))["results"][0]
            self.assertIn("error", row)

    async def test_read_errors_do_not_suppress_valid_batch_items(self):
        (self.root / "good").write_text("evidence")
        result = await run_batch(str(self.root), [
            {"path": "missing"}, {"path": "good"}, {"path": "good", "start_line": 0},
        ], search=False)
        self.assertIn("error", result["results"][0])
        self.assertEqual(result["results"][1]["lines"][0]["text"], "evidence")
        self.assertIn("error", result["results"][2])

    async def test_traversal_and_external_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret"
            secret.write_text("must not be returned")
            (self.root / "escape").symlink_to(secret)
            relative = "../" + Path(outside).name + "/secret"
            for search in (False, True):
                result = await run_batch(str(self.root), [
                    {"path": relative, "pattern": "must"},
                    {"path": "escape", "pattern": "must"},
                ], search=search)
                self.assertTrue(all("error" in r for r in result["results"]))
                self.assertNotIn("must not be returned", json.dumps(result))

    @unittest.skipUnless(shutil.which("rg"), "ripgrep required")
    async def test_search_pagination_is_complete_and_stable(self):
        for filename in ("b.log", "a.log"):
            (self.root / filename).write_text("\n".join(f"hit {i}" for i in range(7)))
        request = {"pattern": "hit", "limit": 3}
        found = []
        while request:
            result = (await run_batch(str(self.root), [request], search=True))["results"][0]
            self.assertNotIn("error", result)
            found.extend((m["path"], m["line"]) for m in result["matches"])
            if not result["complete"]:
                self.assertIsNone(result["matching_lines_total"])
            request = result["next_request"]
        self.assertEqual(found, [(f, n) for f in ("a.log", "b.log") for n in range(1, 8)])
        self.assertEqual(result["matching_lines_total"], 14)

    @unittest.skipUnless(shutil.which("rg"), "ripgrep required")
    async def test_regex_failure_is_independent_and_symlink_not_followed(self):
        (self.root / "real").write_text("target")
        with tempfile.TemporaryDirectory() as outside:
            (Path(outside) / "external").write_text("target secret")
            (self.root / "link").symlink_to(outside, target_is_directory=True)
            result = await run_batch(str(self.root), [{"pattern": "["}, {"pattern": "target"}], search=True)
        self.assertIn("error", result["results"][0])
        self.assertEqual(len(result["results"][1]["matches"]), 1)
        self.assertEqual(result["results"][1]["matches"][0]["path"], "real")

    @unittest.skipUnless(shutil.which("rg"), "ripgrep required")
    async def test_lookaround_and_backreferences_work_without_model_retry(self):
        if subprocess.run(["rg", "--pcre2-version"], capture_output=True).returncode:
            self.skipTest("ripgrep requires PCRE2 support for advanced regex")
        (self.root / "events.log").write_text("error heartbeat\nerror driver\n波束 波束\nerror (?!heartbeat)")
        requests = [
            {"pattern": r"error (?!heartbeat)", "path": "events.log"},
            {"pattern": r"(波束) \1", "path": "events.log"},
            {"pattern": "(?!heartbeat)", "literal": True, "path": "events.log"},
        ]
        result = await run_batch(str(self.root), requests, search=True)
        self.assertTrue(all("error" not in item for item in result["results"]))
        self.assertEqual([[m["line"] for m in item["matches"]] for item in result["results"]], [[2, 4], [3], [4]])

    @unittest.skipUnless(shutil.which("rg"), "ripgrep required")
    async def test_compact_wire_preserves_all_evidence_and_continuations(self):
        path = self.root / "nested" / "source_with_a_long_filename.c"
        path.parent.mkdir()
        path.write_text("\n".join(f'行{i} \\"target\\"' for i in range(60)), encoding="utf-8")
        for search in (False, True):
            requests = ([{"path": "nested", "pattern": "target", "limit": 20}, {"path": "missing", "pattern": "target"}]
                        if search else [{"path": str(path.relative_to(self.root)), "line_count": 40}, {"path": "missing"}])
            original = await run_batch(str(self.root), requests, search=search)
            wire = encode_evidence(original)
            decoded = json.loads(wire)
            restored = {"results": []}
            for item in decoded["results"]:
                item = dict(item)
                if "lines" in item:
                    item["lines"] = [dict(zip(decoded["line_columns"], row)) for row in item["lines"]]
                if "matches" in item:
                    item["matches"] = [dict(zip(decoded["match_columns"], row)) for row in item["matches"]]
                    for match in item["matches"]:
                        match["read_request"] = {"path": match["path"], "start_line": match["line"],
                                                 "line_count": decoded["read_match"]["line_count"]}
                restored["results"].append(item)
            self.assertEqual(restored, original)
            self.assertLess(len(wire.encode()), len(json.dumps(original, ensure_ascii=False).encode()))

    def test_compact_wire_retains_nonstandard_read_request(self):
        payload = {"results": [{"matches": [{"path": "a", "line": 1, "read_request": {
            "path": "a", "start_line": 1, "line_count": 25, "column": 4,
        }}]}]}
        self.assertEqual(json.loads(encode_evidence(payload)), payload)
        payload = {"results": [{"lines": [{"line": 1, "column": 0, "text": "evidence", "annotation": "retain me"}]}]}
        self.assertEqual(json.loads(encode_evidence(payload)), payload)

    async def test_batch_parallelism_is_bounded_and_ordered(self):
        active = maximum = 0

        async def fake_search(root, request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"value": request["value"]}

        with patch("app.agents.evidence_tools.search_page", fake_search):
            result = await run_batch(str(self.root), [{"value": n} for n in range(8)], search=True)
        self.assertEqual(maximum, 4)
        self.assertEqual([r["value"] for r in result["results"]], list(range(8)))

    async def test_snapshot_removes_credentials_without_dropping_attachment_manifest(self):
        catalog = {"count": 1, "truncated": False, "projects": [
            {"project_code": "p", "project_card": "scope", "git_token": "catalog-secret"},
        ]}
        task = {"question": "q", "repo_info": {"project_code": "p", "clone_url": "credential"},
                "attachments": [{"filename": "a", "path": "logs/a", "kind": "text", "token": "secret"}]}
        with patch("app.agents.evidence_policy.discover_projects_payload", AsyncMock(return_value=catalog)):
            snapshot = await prepare_snapshot(task)
        serialized = json.dumps(snapshot)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("credential", serialized)
        self.assertEqual(snapshot["task"]["attachments"][0]["path"], "logs/a")
        self.assertIn("必需 Skill", snapshot_prompt(snapshot, "zh"))
        self.assertIn("required Skills", snapshot_prompt(snapshot, "en"))

    async def test_incomplete_catalog_and_discovery_errors_fall_back(self):
        for value in ({"count": 1, "projects": [], "truncated": False},
                      {"count": 0, "projects": [], "truncated": True}):
            with patch("app.agents.evidence_policy.discover_projects_payload", AsyncMock(return_value=value)):
                self.assertIsNone(await prepare_snapshot({"question": "q"}))
        with patch("app.agents.evidence_policy.discover_projects_payload", AsyncMock(side_effect=OSError)):
            self.assertIsNone(await prepare_snapshot({"question": "q"}))

    def test_project_fit_keeps_scope_rules_with_preloaded_catalog(self):
        args = dict(workflow_name="log", project_name="p", project_code="p", project_card="scope", catalog_available=True)
        for locale in ("zh", "en"):
            legacy = build_project_fit_guidance(**args, locale=locale)
            current = build_project_fit_guidance(**args, locale=locale, catalog_preloaded=True)
            self.assertIn("discover_projects", legacy)
            self.assertNotIn("discover_projects", current)
            self.assertIn("clone_project_repo", current)


if __name__ == "__main__":
    unittest.main()
