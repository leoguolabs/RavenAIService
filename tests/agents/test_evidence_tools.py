"""Behavioral checks: complete paging, independent errors and workspace bounds."""
import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.agents.evidence_tools import MAX_READ_CHARS, read_page, run_batch
from app.agents.evidence_policy import prepare_snapshot, snapshot_prompt
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
