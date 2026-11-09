"""Configure official intent rules and opt in to stable Raven task routing.

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


def routing_groups(local: str, strong: str, light: str) -> list[dict]:
    simple = ("简单任务：问候、闲聊、明确的知识问答、翻译、格式整理、字段提取；"
              "不需要调查未知原因、设计方案或修改代码和设备配置")
    routine = ("常规任务：按照明确步骤读取文件或日志、查询项目与设备信息、常规状态排查；"
               "不涉及根因未知的故障、跨模块改动、架构设计或有风险的写操作")
    complex_task = ("复杂任务：根因未知的故障定位、多文件代码修改、架构与算法设计、性能与并发问题、"
                    "网络或设备配置变更、需要多步推理的方案；不确定难度时也归此类")
    local_ref = "group/raven-task-yinhe"
    groups = [{"id": "raven-task-yinhe", "name": "Raven · 仅银河首 token 1 秒",
        "members": [local], "routing": "order", "affinity": "session", "firstToken": 1},
        {"id": "raven-task-local", "name": "Raven · 银河慢速转 GPT · 任务内保持",
         "members": [local_ref, strong], "routing": "order", "affinity": "session"}]
    for degraded in (False, True):
        groups.append({"id": "raven-task-cloud" if degraded else "raven-task-intent",
            "name": "Raven · 拥堵期间云端意图路由" if degraded else "Raven · 任务意图与复杂度",
            "members": [strong, light] if degraded else [strong, local_ref, light],
            "routing": "order", "affinity": "session", "classifier": light,
            "rules": [
                {"use": strong, "images": True},
                {"use": strong, "intent": complex_task},
                {"use": light, "intent": simple},
                {"use": strong if degraded else local_ref, "intent": routine},
            ]})
    return groups


async def configure(args):
    from app.services import agent_runtime_service as runtime
    from app.services.magpie_routing_policy import TaskRoutingPolicy

    config = runtime.get_config()
    groups = routing_groups(args.local_model, args.strong_model, args.light_model)
    policy = TaskRoutingPolicy(enabled=True, entry_group="group/raven-task-intent",
        degraded_group="group/raven-task-cloud", local_model=args.local_model,
        local_group="group/raven-task-local", backup_model=args.strong_model)
    async with httpx.AsyncClient(base_url=args.admin_url, timeout=30, follow_redirects=True) as client:
        response = await client.post("/_magpie/sign-in", data={
            "key": Path(args.web_key_file).read_text().strip(), "back": "/"})
        response.raise_for_status()
        response = await client.get("/api/groups")
        response.raise_for_status()
        before = response.json()
        offered = {row["id"] for row in before.get("models", [])}
        if not {args.strong_model, args.light_model} <= offered:
            raise RuntimeError("Cloud backup or classifier is unavailable in Magpie")
        if args.local_model not in offered:
            response = await client.get("/api/providers")
            response.raise_for_status()
            rows = response.json()
            rows = rows if isinstance(rows, list) else rows.get("providers", [])
            known = {row['id'] + '/' + model['id'] for row in rows for model in row.get('models', [])}
            if args.local_model not in known:
                raise RuntimeError("Local model is not configured in Magpie")
            # An intentionally disabled local provider remains disabled. The new
            # groups can be enabled first, before an operator switches it on.
        if not args.apply:
            return {"apply": False, "groups": groups, "task_routing": policy.model_dump()}
        directory = Path(args.backup_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        backup = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        # The runtime contains a gateway key: private rollback evidence only.
        with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
            json.dump({"agent_runtime": config.model_dump(), "groups": before["groups"]}, out)
        for group in groups:
            response = await client.post("/api/groups/save", json=group)
            response.raise_for_status()
            if response.json().get("error"):
                raise RuntimeError("Magpie rejected a routing group")
        # Leave per-agent selections as the administrator chose them. An explicit
        # selection intentionally opts out; empty models inherit this entry group.
        runtime.save({"model": policy.entry_group, "task_routing": policy.model_dump()})
        await runtime.models()
        return {"apply": True, "model": policy.entry_group, "groups": [g["id"] for g in groups],
                "task_routing": policy.model_dump(), "private_backup": str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", default="http://magpie:3430")
    parser.add_argument("--web-key-file", default="/run/secrets/magpie_web_key")
    parser.add_argument("--backup-dir", default="/app/data/magpie-routing-backups")
    parser.add_argument("--local-model", default="raven-yinhe/yinhe-thinking")
    parser.add_argument("--strong-model", default="raven-openai/gpt-6.1-sol")
    parser.add_argument("--light-model", default="raven-deepseek/deepseek-v4-flash")
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
