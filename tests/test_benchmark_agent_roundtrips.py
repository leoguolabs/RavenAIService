import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.benchmark_agent_roundtrips import prepare_inputs, run_one, summarize, sdk_response_evidence


class BenchmarkEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_response_count_includes_isolated_verifier_without_double_counting_frames(self):
        workspace = Path("/app/temp/code_repos/unique-run")
        primary = self.root / "-app-temp-code-repos-unique-run"
        verifier = self.root / "-app-temp-code-repos-unique-run--verification"
        unrelated = self.root / "-app-temp-code-repos-other-run"
        def save(folder, rows):
            folder.mkdir()
            (folder / "session.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        def response(identity, tokens):
            return {"type": "assistant", "message": {"id": identity, "usage": {"input_tokens": tokens},
                    "content": [{"text": "PRIVATE_CONTENT_NOT_METRICS"}]}}
        save(primary, [response("main1", 3), response("main1", 5), response("main2", 2),
                       {"type": "user", "message": {"id": "not-a-response"}}])
        save(verifier, [response("review1", 7), response("review1", 9)])
        save(unrelated, [response("unrelated", 1000)])
        evidence = sdk_response_evidence(workspace, self.root)
        self.assertEqual(evidence["observed_model_response_ids"], 2)
        self.assertEqual(evidence["verification_observed_model_response_ids"], 1)
        self.assertEqual(evidence["total_observed_model_response_ids"], 3)
        self.assertEqual(evidence["usage_by_response_id"]["main1"]["input_tokens"], 5)
        self.assertEqual(evidence["verification_usage_by_response_id"]["review1"]["input_tokens"], 9)
        self.assertNotIn("PRIVATE_CONTENT", json.dumps(evidence))
        self.assertNotIn("unrelated", json.dumps(evidence))

    def test_missing_verifier_keeps_original_response_count_semantics(self):
        workspace = Path("/app/temp/code_repos/unique-run")
        primary = self.root / "-app-temp-code-repos-unique-run"
        primary.mkdir()
        (primary / "session.jsonl").write_text(json.dumps({"type": "assistant", "message": {"id": "one"}}))
        evidence = sdk_response_evidence(workspace, self.root)
        self.assertEqual(evidence["observed_model_response_ids"], 1)
        self.assertEqual(evidence["verification_observed_model_response_ids"], 0)
        self.assertEqual(evidence["total_observed_model_response_ids"], 1)

    def test_original_duplicate_names_restored_without_reference_answer(self):
        path = self.root / "download.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("oam.log", b"first log")
            archive.writestr("oam_bbbbbbbb.log", b"second log")
            archive.writestr("batch_download_info.json", '{"answer":"historic answer must not enter model input"}')
        case = {"case_id": "case-01", "archive": path.name,
                "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "attachments": [
                    {"id": "aaaaaaaa-1", "filename": "oam.log", "file_size": 9},
                    {"id": "bbbbbbbb-2", "filename": "oam.log", "file_size": 10},
                ]}
        (self.root / "corpus.json").write_text(json.dumps({"cases": [case]}))
        prepare_inputs(self.root)
        inputs = json.loads((self.root / "corpus.json").read_text())["cases"][0]["inputs"]
        self.assertEqual([x["filename"] for x in inputs], ["oam.log", "oam.log"])
        self.assertEqual([(self.root / x["path"]).read_bytes() for x in inputs], [b"first log", b"second log"])
        path.write_bytes(b"modified")
        with self.assertRaisesRegex(RuntimeError, "Frozen archive changed"):
            prepare_inputs(self.root)

    def test_finished_run_is_recovered_without_resubmission(self):
        case = {"case_id": "case-01", "question": "q", "question_sha256": hashlib.sha256(b"q").hexdigest(), "inputs": []}
        directory = self.root / "runs/trial/case-01/baseline"
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"session_id": "existing"}))
        with patch("scripts.benchmark_agent_roundtrips.api_json", return_value={"status": "done"}) as status, \
             patch("urllib.request.urlopen") as submit:
            result = run_one(self.root, case, "baseline", "trial", {"url": "http://local", "token": "test"}, 1)
        status.assert_called_once()
        submit.assert_not_called()
        self.assertFalse(result["observation_timing_valid"])

    def test_failed_pair_is_reported_and_never_counted_as_speedup(self):
        for arm, server in (("baseline", {"status": "done", "result": {"status": "ok"}}),
                            ("candidate", {"status": "done", "error": "timeout"})):
            path = self.root / "runs/trial/case-01" / arm / "result.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"server": server, "observation_timing_valid": True, "end_to_end_seconds": 10}))
        summarize(self.root, "trial")
        result = json.loads((self.root / "runs/trial/summary.json").read_text())
        self.assertEqual(result["complete_timing_pairs"], 0)
        self.assertEqual(len(result["failures"]), 1)
        self.assertNotIn("median_reduction", result)
        self.assertEqual(result["all_terminal_timing"]["pairs"], 1)
        self.assertEqual(result["outcomes"]["candidate"]["failure_rate"], 1)
        self.assertFalse(result["timing_target_met"])
        self.assertFalse(result["timing_sample_complete"])
        self.assertFalse(result["eligible_for_acceptance"])

    def test_fast_failures_cannot_pass_target_among_twenty_faster_successes(self):
        for index in range(21):
            for arm in ("baseline", "candidate"):
                path = self.root / f"runs/trial/case-{index:02d}" / arm / "result.json"
                path.parent.mkdir(parents=True)
                failed = index == 20 and arm == "candidate"
                server = {"status": "done", "error": "max turns"} if failed else {
                    "status": "done", "result": {"status": "ok"},
                }
                path.write_text(json.dumps({"server": server, "observation_timing_valid": True,
                                            "end_to_end_seconds": 1 if failed else 100 if arm == "baseline" else 20}))
        summarize(self.root, "trial")
        result = json.loads((self.root / "runs/trial/summary.json").read_text())
        self.assertEqual(result["complete_timing_pairs"], 20)
        self.assertEqual(result["all_terminal_timing"]["pairs"], 21)
        self.assertGreater(result["median_reduction"], .30)
        self.assertEqual(result["outcomes"]["candidate"]["failure"], 1)
        self.assertFalse(result["timing_target_met"])
        self.assertFalse(result["timing_sample_complete"])

    def test_small_improvement_is_reported_without_an_implicit_percentage_gate(self):
        for index in range(20):
            for arm in ("baseline", "candidate"):
                path = self.root / f"runs/trial/case-{index:02d}" / arm / "result.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"server": {"status": "done", "result": {"status": "ok"}},
                                            "observation_timing_valid": True,
                                            "end_to_end_seconds": 100 if arm == "baseline" else 95}))
        summarize(self.root, "trial")
        result = json.loads((self.root / "runs/trial/summary.json").read_text())
        self.assertTrue(result["timing_sample_complete"])
        self.assertTrue(result["median_improved"])
        self.assertAlmostEqual(result["median_reduction"], .05)
        self.assertIsNone(result["target_reduction"])
        self.assertIsNone(result["timing_target_met"])
        self.assertFalse(result["eligible_for_acceptance"])
        summarize(self.root, "trial", target_reduction=.20)
        historical = json.loads((self.root / "runs/trial/summary.json").read_text())
        self.assertEqual(historical["target_reduction"], .20)
        self.assertFalse(historical["timing_target_met"])

    def test_invalid_explicit_threshold_does_not_write_a_summary(self):
        for value in (-.1, 1, float("nan")):
            with self.assertRaises(ValueError):
                summarize(self.root, "trial", target_reduction=value)
        self.assertFalse((self.root / "runs/trial/summary.json").exists())

    def test_failed_recovered_run_retains_both_failure_and_invalid_timing(self):
        path = self.root / "runs/trial/case-01/candidate/result.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"server": {"status": "done", "error": "timeout"},
                                    "observation_timing_valid": False}))
        summarize(self.root, "trial")
        result = json.loads((self.root / "runs/trial/summary.json").read_text())
        self.assertEqual(result["outcomes"]["baseline"]["missing"], 1)
        self.assertEqual(result["outcomes"]["candidate"]["failure"], 1)
        self.assertEqual(result["outcomes"]["candidate"]["invalid_timing"], 1)
        self.assertEqual(result["all_terminal_timing"]["pairs"], 0)


if __name__ == "__main__":
    unittest.main()
