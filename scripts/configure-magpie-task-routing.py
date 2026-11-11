"""Configure Yinhe-first routing with fast, sticky fallback and no classifier.

Run inside the authorized backend. No provider credentials are copied or printed.
Existing providers, executor overrides and unrelated groups are preserved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.magpie_routing_policy import (
    TITLE_DEEPSEEK_GROUP,
    TITLE_DEEPSEEK_MODEL,
    TITLE_YINHE_GROUP,
    TITLE_YINHE_MODEL,
    TaskRoutingPolicy,
)


def title_groups() -> list[dict]:
    return [
        {"id": group_id, "name": name, "members": [model], "routing": "order",
         "affinity": "off", "patience": -1, "classifier": "", "rules": []}
        for group_id, name, model in (
            (TITLE_YINHE_GROUP, "Raven · 标题 · 银河", TITLE_YINHE_MODEL),
            (TITLE_DEEPSEEK_GROUP, "Raven · 标题 · DeepSeek", TITLE_DEEPSEEK_MODEL),
        )
    ]


def routing_groups(local: str, strong: str, light: str) -> list[dict]:
    local_ref = "group/raven-task-yinhe"
    # Magpie's native timer is integer seconds. Raven enforces the tighter
    # dispatch-to-output budget before this group's first response is delivered.
    return [{"id": "raven-task-yinhe", "name": "Raven · 银河优先 · Raven 慢速保护",
        "members": [local], "routing": "order", "affinity": "session", "firstToken": 1,
        "classifier": "", "rules": []},
        *[{"id": group_id, "name": name,
           "members": [local_ref, strong, light], "routing": "order", "affinity": "session",
           "classifier": "", "rules": []}
          for group_id, name in (("raven-task-local", "Raven · 银河优先 · 顺序备用"),
                                 ("raven-task-intent", "Raven · 银河优先 · 旧入口兼容"))],
        {"id": "raven-task-cloud", "name": "Raven · 拥堵期间云端顺序备用",
         "members": [strong, light], "routing": "order", "affinity": "session",
         "classifier": "", "rules": []}]


async def configure(args):
    from app.services import agent_runtime_service as runtime

    policy = None
    if args.titles_only:
        groups = title_groups()
        local_model, cloud_models = TITLE_YINHE_MODEL, {TITLE_DEEPSEEK_MODEL}
    else:
        local_model, cloud_models = args.local_model, {args.strong_model, args.light_model}
        groups = routing_groups(local_model, args.strong_model, args.light_model)
        if (local_model, args.light_model) == (TITLE_YINHE_MODEL, TITLE_DEEPSEEK_MODEL):
            groups.extend(title_groups())
        policy = TaskRoutingPolicy(enabled=True, entry_group="group/raven-task-yinhe",
            degraded_group="group/raven-task-cloud", local_model=local_model,
            local_group="group/raven-task-yinhe", backup_model=args.strong_model)

    async with httpx.AsyncClient(base_url=args.admin_url, timeout=30, follow_redirects=True) as client:
        response = await client.post("/_magpie/sign-in", data={
            "key": Path(args.web_key_file).read_text().strip(), "back": "/"})
        response.raise_for_status()
        response = await client.get("/api/groups")
        response.raise_for_status()
        before = response.json()
        offered = {row["id"] for row in before.get("models", [])}
        if not cloud_models <= offered:
            raise RuntimeError("Cloud backup is unavailable in Magpie")
        if local_model not in offered:
            response = await client.get("/api/providers")
            response.raise_for_status()
            rows = response.json()
            rows = rows if isinstance(rows, list) else rows.get("providers", [])
            known = {row['id'] + '/' + model['id'] for row in rows for model in row.get('models', [])}
            if local_model not in known:
                raise RuntimeError("Local model is not configured in Magpie")
            # An intentionally disabled local provider remains disabled. The new
            # groups can be enabled first, before an operator switches it on.
        if not args.apply:
            preview = {"apply": False, "groups": groups}
            if policy:
                preview["task_routing"] = policy.model_dump()
            return preview
        directory = Path(args.backup_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        backup = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        group_ids = [group["id"] for group in groups]
        previous_groups = before["groups"]
        if args.titles_only:
            previous_groups = [group for group in previous_groups if group["id"] in group_ids]
        snapshot = {"groups": previous_groups}
        if policy:
            # The runtime contains a gateway key: private rollback evidence only.
            snapshot["agent_runtime"] = runtime.get_config().model_dump()
        with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
            json.dump(snapshot, out)
        for group in groups:
            response = await client.post("/api/groups/save", json=group)
            response.raise_for_status()
            if response.json().get("error"):
                raise RuntimeError("Magpie rejected a routing group")
        result = {"apply": True, "groups": group_ids, "private_backup": str(backup)}
        if policy:
            # Per-agent selections are preserved. Empty models inherit the entry group.
            runtime.save({"model": policy.entry_group, "task_routing": policy.model_dump()})
            result.update(model=policy.entry_group, task_routing=policy.model_dump())
            await runtime.models()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", default="http://magpie:3430")
    parser.add_argument("--web-key-file", default="/run/secrets/magpie_web_key")
    parser.add_argument("--backup-dir", default="/app/data/magpie-routing-backups")
    parser.add_argument("--local-model", default=TITLE_YINHE_MODEL)
    parser.add_argument("--strong-model", default="raven-openai/gpt-6.1-sol")
    parser.add_argument("--light-model", default=TITLE_DEEPSEEK_MODEL)
    parser.add_argument("--titles-only", action="store_true", help="Configure only the two title routing groups.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(configure(args)), ensure_ascii=False, indent=2))
    except Exception as exc:
        # HTTP bodies and validation errors can include submitted credentials.
        print(f"Task routing configuration failed ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
