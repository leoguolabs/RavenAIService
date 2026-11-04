#!/usr/bin/env python3
"""Private, resumable corpus collection and evidence for Agent latency trials.

Artifacts contain real engineering logs and MUST live outside the repository.
Collection uses the same public download API as the log-list page. Reference
answers are retained separately and never supplied to the Agent under test.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import inspect
import json
from pathlib import Path
import random
import fcntl
import math
import os
import statistics
import subprocess
import time
import uuid
import urllib.request
import urllib.parse
import zipfile


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def collect(root: Path, base_url: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "corpus.json"
    if manifest_path.exists():
        cases = json.loads(manifest_path.read_text())["cases"]
    else:
        with urllib.request.urlopen(base_url + "/api/v1/logs?per_page=100&page=1", timeout=90) as response:
            listing = json.load(response)
        write_json(root / "production-list.json", listing)
        with urllib.request.urlopen(base_url + "/api/v1/project-repos", timeout=30) as response:
            write_json(root / "project-catalog.json", json.load(response))
        projects = {"oam_lx10": [], "oam_lx11ab": [], "oam_lx07a": []}
        seen = set()
        for log in listing["data"]["logs"]:
            code = log.get("project_code")
            result = log.get("ai_analysis_result") or {}
            question = log.get("ai_analysis_query") or log.get("issue_description")
            # Follow-ups requiring missing prior attachments are unsuitable for
            # independent replay, not failed/slow runs to be discarded later.
            if code not in projects or result.get("status") != "ok" or not question:
                continue
            if "两轨" in question or "另一轨" in question:
                continue
            identity = (log.get("checksum") or log["file_size"], question)
            if identity in seen:
                continue
            seen.add(identity)
            projects[code].append(log)
        rng = random.Random(20261023)
        for logs in projects.values():
            rng.shuffle(logs)
        selected = []
        while any(projects.values()) and len(selected) < 30:
            for logs in projects.values():
                if logs and len(selected) < 30:
                    selected.append(logs.pop())
        if len(selected) < 30:
            raise RuntimeError(f"Only {len(selected)} eligible independent cases; need 30")
        cases = []
        for index, log in enumerate(selected):
            question = log.get("ai_analysis_query") or log["issue_description"]
            filename = Path(log.get("download_filename") or log["original_filename"]).name
            case = {
                "case_id": f"case-{index + 1:02d}-{log['id'][:8]}",
                "split": "calibration" if index < 20 else "holdout",
                "log_id": log["id"], "project_code": log["project_code"],
                "question": question, "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
                "filename": filename, "attachments": log.get("attachments", []),
                "source_url": base_url + "/log/" + log["id"],
                "download_url": base_url + "/api/v1/logs/" + log["id"] + "/download",
                "archive": f"corpus/{log['id']}/{filename}",
                "reference": f"references/{log['id']}.json",
            }
            write_json(root / case["reference"], log)
            cases.append(case)
        write_json(manifest_path, {"selection_seed": 20261023, "cases": cases})

    def download(case: dict) -> dict:
        destination = root / case["archive"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_name(destination.name + ".part")
            with urllib.request.urlopen(case["download_url"], timeout=180) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            temporary.replace(destination)
        hasher = hashlib.sha256()
        with destination.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if case.get("archive_sha256") and case["archive_sha256"] != digest:
            raise RuntimeError(f"Frozen input changed: {case['case_id']}")
        return {**case, "archive_sha256": digest, "download_bytes": destination.stat().st_size}

    completed = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        pending = {pool.submit(download, case): case for case in cases}
        for future in concurrent.futures.as_completed(pending):
            case = future.result()
            completed[case["case_id"]] = case
            write_json(manifest_path, {"selection_seed": 20261023, "cases": [completed.get(c["case_id"], c) for c in cases]})
            print(json.dumps({"case": case["case_id"], "split": case["split"], "bytes": case["download_bytes"], "sha256": case["archive_sha256"]}), flush=True)


def prepare_inputs(root: Path) -> None:
    """Restore original multipart attachments, excluding download-wrapper metadata.

    The download API renames duplicate filenames in its ZIP. Upload the original
    names and order again, rather than feeding batch_download_info to the model.
    """
    manifest = json.loads((root / "corpus.json").read_text())
    for case in manifest["cases"]:
        archive = root / case["archive"]
        if hashlib.sha256(archive.read_bytes()).hexdigest() != case["archive_sha256"]:
            raise RuntimeError(f"Frozen archive changed: {case['case_id']}")
        attachments = case.get("attachments") or []
        inputs = []
        if len(attachments) > 1:
            with zipfile.ZipFile(archive) as bundle:
                used = set()
                for attachment in attachments:
                    filename = Path(attachment["filename"]).name
                    renamed = f"{Path(filename).stem}_{attachment['id'][:8]}{Path(filename).suffix}"
                    member = renamed if renamed in bundle.namelist() else filename
                    if member in used:
                        raise RuntimeError(f"Ambiguous duplicate attachment: {case['case_id']}")
                    used.add(member)
                    raw = bundle.read(member)
                    if len(raw) != attachment["file_size"]:
                        raise RuntimeError(f"Attachment byte count mismatch: {case['case_id']} {member}")
                    path = root / "inputs" / case["case_id"] / attachment["id"] / filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)
                    inputs.append({"path": str(path.relative_to(root)), "filename": filename,
                                   "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
                unexpected = set(bundle.namelist()) - used - {"batch_download_info.json"}
                if unexpected:
                    raise RuntimeError(f"Unmapped attachment members: {unexpected}")
        else:
            inputs.append({"path": case["archive"], "filename": case["filename"],
                           "sha256": case["archive_sha256"], "bytes": archive.stat().st_size})
        if case.get("inputs") and case["inputs"] != inputs:
            raise RuntimeError(f"Frozen multipart inputs changed: {case['case_id']}")
        case["inputs"] = inputs
    write_json(root / "corpus.json", manifest)
    print(json.dumps({"cases": len(manifest["cases"]), "attachments": sum(len(c['inputs']) for c in manifest['cases'])}), flush=True)


def api_json(url: str, token: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "X-App-Locale": "zh"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def multipart(fields: dict, files: list) -> tuple[bytes, str]:
    boundary = "raven-benchmark-" + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
    for filename, raw in files:
        if any(char in filename for char in ('"', '\r', '\n')):
            raise ValueError("Unsupported multipart filename")
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
        parts.extend([raw, b"\r\n"])
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"



def sdk_response_evidence(workspace: Path, projects: Path) -> dict:
    """Count primary and isolated-review SDK responses separately, never raw content."""
    key = str(workspace).replace("/", "-").replace("_", "-")
    by_stage = {}
    for stage, folder in (("primary", projects / key),
                          ("verification", projects / (key + "--verification"))):
        responses = {}
        for transcript in folder.glob("*.jsonl"):
            for line in transcript.read_text().splitlines():
                entry = json.loads(line)
                message = entry.get("message") or {}
                if entry.get("type") == "assistant" and isinstance(message, dict) and message.get("id"):
                    usage = responses.setdefault(message["id"], {})
                    for name, value in (message.get("usage") or {}).items():
                        if type(value) is int:
                            usage[name] = max(usage.get(name, 0), value)
        by_stage[stage] = responses
    return {
        "observed_model_response_ids": len(by_stage["primary"]),
        "usage_by_response_id": by_stage["primary"],
        "verification_observed_model_response_ids": len(by_stage["verification"]),
        "verification_usage_by_response_id": by_stage["verification"],
        "total_observed_model_response_ids": sum(len(v) for v in by_stage.values()),
        "model_count_note": "Distinct assistant IDs per primary/verification SDK transcript stage; total includes both stages, excludes auxiliary title requests and requests without an assistant response.",
    }


def checkout_evidence(endpoint: dict, status: dict, artifacts: Path | None = None) -> dict:
    """Read exact source identities without exposing credential-bearing remotes."""
    container = endpoint.get("container")
    task_id = next((e.get("task_id") for e in status.get("events", []) if e.get("type") == "run_start"), None)
    if not container or not task_id:
        return {"verified": False, "reason": "missing container or run_start task identity"}
    task_id = str(uuid.UUID(task_id))
    script = "import json,pathlib,subprocess,sys\nfrom pathlib import Path\n" + inspect.getsource(sdk_response_evidence) + """
root=pathlib.Path('/app/temp/code_repos').resolve()
workspace=(root/sys.argv[1]).resolve();workspace.relative_to(root)
rows=[]
for marker in workspace.rglob('.git'):
 p=marker.parent
 def git(*args): return subprocess.check_output(['git','-C',str(p),*args],text=True).strip()
 rows.append({'path':str(p.relative_to(workspace)),'commit':git('rev-parse','HEAD'),'changes':git('status','--porcelain')})
evidence=sdk_response_evidence(workspace,pathlib.Path('/home/appuser/.claude/projects'))
print(json.dumps({'verified':True,'task_id':sys.argv[1],'repositories':rows,**evidence}))
"""
    try:
        result = subprocess.run(["docker", "exec", container, "python", "-c", script, task_id],
                                capture_output=True, text=True, timeout=45, check=True)
        evidence = json.loads(result.stdout)
        if artifacts is not None:
            transcript_path = f"/home/appuser/.claude/projects/-app-temp-code-repos-{task_id}"
            copied = subprocess.run(["docker", "cp", f"{container}:{transcript_path}", str(artifacts / "sdk-session")],
                                    capture_output=True, timeout=45)
            evidence["sdk_transcript_saved"] = copied.returncode == 0
            verifier_path = transcript_path + "--verification"
            copied_verifier = subprocess.run(["docker", "cp", f"{container}:{verifier_path}", str(artifacts / "sdk-verification-session")],
                                             capture_output=True, timeout=45)
            evidence["sdk_verification_transcript_saved"] = copied_verifier.returncode == 0
        return evidence
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"verified": False, "reason": type(exc).__name__}


def run_one(root: Path, case: dict, arm: str, label: str, endpoint: dict, project_id: int) -> dict:
    directory = root / "runs" / label / case["case_id"] / arm
    directory.mkdir(parents=True, exist_ok=True)
    result_path = directory / "result.json"
    if result_path.exists():
        return json.loads(result_path.read_text())
    state_path = directory / "state.json"
    resumed = state_path.exists()
    files = []
    if hashlib.sha256(case["question"].encode()).hexdigest() != case["question_sha256"]:
        raise RuntimeError("Frozen question changed")
    for item in case["inputs"]:
        raw = (root / item["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise RuntimeError("Frozen attachment changed")
        files.append((item["filename"], raw))
    if resumed:
        state = json.loads(state_path.read_text())
        status = api_json(endpoint["url"] + "/api/v1/ai-chat/log-analysis/result?" + urllib.parse.urlencode({"session_id": state["session_id"]}), endpoint["token"])
        if status["status"] == "not_found":
            raise RuntimeError(f"Existing run {state['session_id']} is not cached; reconcile persisted DB/trace before retrying")
        if status["status"] == "done":
            write_json(result_path, {"state": state, "resumed": True, "observation_timing_valid": False,
                                     "server": status, "checkout_evidence": checkout_evidence(endpoint, status, directory)})
            return json.loads(result_path.read_text())
        fields = {"session_id": state["session_id"], "message": ""}
        files = []
    else:
        state = {"case_id": case["case_id"], "arm": arm, "label": label,
                 "session_id": str(uuid.uuid4()), "started_epoch": time.time(),
                 "pid": os.getpid(), "question_sha256": case["question_sha256"],
                 "inputs": case["inputs"], "project_code": case["project_code"],
                 "endpoint": endpoint["url"]}
        write_json(state_path, state)
        fields = {"session_id": state["session_id"], "message": case["question"],
                  "project_repo_id": project_id, "remember": "true"}
    body, content_type = multipart(fields, files)
    request = urllib.request.Request(endpoint["url"] + "/api/v1/ai-chat/log-analysis/stream", data=body,
                                     headers={"Authorization": "Bearer " + endpoint["token"],
                                              "Content-Type": content_type, "X-App-Locale": "zh"})
    print(json.dumps({"event": "start" if not resumed else "reattach", "case": case["case_id"], "arm": arm, "session_id": state["session_id"]}), flush=True)
    with urllib.request.urlopen(request, timeout=90) as response, (directory / "events.jsonl").open("a", encoding="utf-8") as output:
        for line in response:
            if line.startswith(b"data:"):
                event = json.loads(line[5:].decode().strip())
                output.write(json.dumps({"received_epoch": time.time(), "data": event}, ensure_ascii=False) + "\n")
                output.flush()
    finished = time.time()
    status = api_json(endpoint["url"] + "/api/v1/ai-chat/log-analysis/result?" + urllib.parse.urlencode({"session_id": state["session_id"]}), endpoint["token"])
    if status["status"] == "running":
        # A transport EOF is not a terminal Agent event. Reattach the SAME run.
        return run_one(root, case, arm, label, endpoint, project_id)
    result = {"state": state, "resumed": resumed, "observation_timing_valid": not resumed,
              "end_to_end_seconds": finished - state["started_epoch"], "server": status,
              "checkout_evidence": checkout_evidence(endpoint, status, directory)}
    write_json(result_path, result)
    print(json.dumps({"event": "terminal", "case": case["case_id"], "arm": arm,
                      "seconds": result["end_to_end_seconds"], "status": status.get("status"),
                      "analysis_status": (status.get("result") or {}).get("status"), "error": status.get("error")}), flush=True)
    return result


def run_trials(root: Path, args) -> None:
    cases = json.loads((root / "corpus.json").read_text())["cases"]
    cases = [c for c in cases if c["split"] == args.split][:args.limit]
    endpoints = json.loads((root / "endpoints.private.json").read_text())
    projects = {r["project_code"]: r["id"] for r in json.loads((root / "production-projects.json").read_text())}
    directory = root / "runs" / args.label
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "runner.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        environment = json.loads((root / "environment.json").read_text())
        saved_environment = directory / "environment.json"
        if saved_environment.exists():
            if json.loads(saved_environment.read_text()) != environment:
                raise RuntimeError("Runtime identity changed; use a new trial label and retain the previous trial")
        else:
            write_json(saved_environment, environment)
        for arm in args.arms.split(","):
            image = subprocess.check_output(["docker", "inspect", "--format", "{{.Image}}", endpoints[arm]["container"]], text=True).strip()
            if image != environment[arm]["image_id"]:
                raise RuntimeError(f"{arm} image no longer matches the frozen environment")
        for index, case in enumerate(cases):
            if (directory / "stop-after-pair").exists():
                print(json.dumps({"event": "stopped_between_pairs", "next_case": case["case_id"]}), flush=True)
                return
            arms = args.arms.split(",")
            if index % 2:
                arms.reverse()
            for arm in arms:
                run_one(root, case, arm, args.label, endpoints[arm], projects[case["project_code"]])


def summarize(root: Path, label: str, target_reduction: float | None = None) -> None:
    if target_reduction is not None and not (0 <= target_reduction < 1):
        raise ValueError("target reduction must be a fraction in [0, 1)")
    pairs = []
    terminal_times = []
    failures = []
    incomplete = []
    outcomes = {arm: {"success": 0, "failure": 0, "missing": 0, "invalid_timing": 0}
                for arm in ("baseline", "candidate")}
    for directory in sorted((root / "runs" / label).glob("case-*")):
        arms = {}
        all_arms = {}
        for arm in ("baseline", "candidate"):
            path = directory / arm / "result.json"
            if not path.exists():
                outcomes[arm]["missing"] += 1
                incomplete.append({"case": directory.name, "arm": arm, "reason": "no terminal result"})
                continue
            result = json.loads(path.read_text())
            server = result["server"]
            succeeded = (server.get("status") == "done" and not server.get("error")
                         and (server.get("result") or {}).get("status") == "ok")
            outcomes[arm]["success" if succeeded else "failure"] += 1
            if not succeeded:
                failures.append({"case": directory.name, "arm": arm, "status": server.get("status"), "error": server.get("error")})
            duration = result.get("end_to_end_seconds")
            if (result.get("observation_timing_valid") and type(duration) in (int, float)
                    and math.isfinite(duration) and duration >= 0):
                all_arms[arm] = duration
                if succeeded:
                    arms[arm] = duration
            else:
                outcomes[arm]["invalid_timing"] += 1
                incomplete.append({"case": directory.name, "arm": arm, "reason": "observation timing invalid after reconnect"})
        if len(all_arms) == 2:
            terminal_times.append({"case": directory.name, **all_arms})
        if len(arms) == 2:
            pairs.append({"case": directory.name, **arms})
    report = {"label": label, "complete_timing_pairs": len(pairs), "failures": failures, "incomplete": incomplete, "pairs": pairs,
              "metric_basis": "Successful paired analyses only; inspect all-terminal timing and failure rates before interpreting reduction.",
              "outcomes": outcomes, "quality_acceptance": "pending independent evidence review",
              "eligible_for_acceptance": False}
    for counts in outcomes.values():
        terminal_count = counts["success"] + counts["failure"]
        counts["failure_rate"] = counts["failure"] / terminal_count if terminal_count else None
    # A quick failure is not a faster answer. Show its time separately instead
    # of either hiding failed runs or allowing them to establish a speedup.
    report["all_terminal_timing"] = {
        "pairs": len(terminal_times), "includes_failures": True,
        "interpretation": "Time until termination, not time to a valid answer; never establishes optimization acceptance.",
    }
    if terminal_times:
        for arm in ("baseline", "candidate"):
            times = sorted(p[arm] for p in terminal_times)
            report["all_terminal_timing"][arm] = {
                "median": statistics.median(times), "p90": times[math.ceil(len(times) * .9) - 1],
            }
    if pairs:
        for arm in ("baseline", "candidate"):
            times = sorted(p[arm] for p in pairs)
            report[arm] = {"median": statistics.median(times), "p90": times[math.ceil(len(times) * .9) - 1]}
        if report["baseline"]["median"] > 0:
            report["median_reduction"] = 1 - report["candidate"]["median"] / report["baseline"]["median"]
    report["timing_sample_complete"] = len(pairs) >= 20 and not failures and not incomplete
    report["median_improved"] = report.get("median_reduction", 0) > 0
    report["target_reduction"] = target_reduction
    report["timing_target_met"] = (
        report["timing_sample_complete"] and "median_reduction" in report
        and report["median_reduction"] >= target_reduction
    ) if target_reduction is not None else None
    report["optimization_goal"] = "Minimize median latency subject to independent quality acceptance."
    write_json(root / "runs" / label / "summary.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "pairs"}, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--base-url", required=True)
    sub.add_parser("prepare-inputs")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--label", required=True)
    run_parser.add_argument("--split", choices=["calibration", "holdout"], required=True)
    run_parser.add_argument("--limit", type=int, default=30)
    run_parser.add_argument("--arms", default="baseline,candidate")
    summary_parser = sub.add_parser("summarize")
    summary_parser.add_argument("--label", required=True)
    summary_parser.add_argument("--target-reduction", type=float, default=None,
                                help="Optional explicit historical threshold as a fraction, e.g. 0.20; none by default")
    args = parser.parse_args()
    try:
        args.root.resolve().relative_to(Path(__file__).resolve().parents[1])
    except ValueError:
        pass
    else:
        parser.error("Production evidence must be stored outside the source repository")
    label = getattr(args, "label", None)
    if label is not None and (not label or Path(label).name != label or label in (".", "..")):
        parser.error("label must be a single directory name")
    if args.command == "collect":
        collect(args.root.resolve(), args.base_url.rstrip("/"))
    elif args.command == "prepare-inputs":
        prepare_inputs(args.root.resolve())
    elif args.command == "run":
        run_trials(args.root.resolve(), args)
    elif args.command == "summarize":
        if args.target_reduction is not None and not (0 <= args.target_reduction < 1):
            parser.error("target reduction must be a fraction in [0, 1)")
        summarize(args.root.resolve(), args.label, args.target_reduction)


if __name__ == "__main__":
    main()
