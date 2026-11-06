"""Import configured Raven endpoints without putting upstream keys in argv/logs.

Run inside the backend with the private Magpie web key mounted read-only.
Existing provider entries are retained. Enabling the gateway is explicit.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def bootstrap(admin_url, web_key, gateway_url, enable):
    from app.services.agent_runtime_service import get_config, save, models
    from app.services.model_router import _resolve
    from app.services.model_settings_service import PRIMARY_SLOT, BACKUP_SLOT, BUG_FIX_SLOT, api_keys_for_slot

    async with httpx.AsyncClient(base_url=admin_url, timeout=60, follow_redirects=True) as client:
        response = await client.post("/_magpie/sign-in", data={"key": web_key.strip(), "back": "/"})
        response.raise_for_status()
        response = await client.get("/api/providers")
        response.raise_for_status()
        catalog = response.json()
        existing = {row["id"] for row in (catalog if isinstance(catalog, list) else catalog.get("providers", []))}
        imported = []
        choices = []
        seen = {}
        for slot in (PRIMARY_SLOT, BACKUP_SLOT, BUG_FIX_SLOT):
            endpoint = _resolve(slot)
            if endpoint is None or not endpoint.api_key:
                continue
            keys = api_keys_for_slot(slot)
            fingerprint = (endpoint.provider, endpoint.base_url, tuple(keys))
            if fingerprint in seen:
                choices.append(seen[fingerprint] + "/" + endpoint.model)
                continue
            provider_id = "raven-" + endpoint.provider
            if provider_id in seen.values():
                provider_id += "-" + str(slot.name)
            seen[fingerprint] = provider_id
            choices.append(provider_id + "/" + endpoint.model)
            if provider_id in existing:
                continue
            protocol = "responses" if endpoint.profile.api_protocol == "openai_responses" else "anthropic"
            model_ids = list(dict.fromkeys(filter(None, [endpoint.model, endpoint.small_fast_model])))
            payload = {"id": provider_id, "name": "Raven · " + (endpoint.profile.label or endpoint.provider),
                "key": keys[0], "keys": [{"key": key} for key in keys[1:]],
                protocol: endpoint.base_url.rstrip("/"), "baseAPI": protocol,
                "models": model_ids}
            response = await client.post("/api/provider/save", json=payload)
            response.raise_for_status()
            if response.json().get("error"):
                raise RuntimeError("Magpie rejected provider configuration")
            imported.append(provider_id)
        current = get_config()
        key = current.api_key
        if not key:
            response = await client.post("/api/caller-keys/add-key", json={"name": "RavenAIService"})
            response.raise_for_status()
            key = response.json()["secret"]
        if not choices:
            raise RuntimeError("No configured Raven model endpoints")
        save({"base_url": gateway_url, "api_key": key, "model": current.model or choices[0],
              "enabled": enable or current.enabled})
        await models()
        return {"imported_providers": imported, "models": choices, "enabled": enable or current.enabled}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", default="http://magpie:3430")
    parser.add_argument("--gateway-url", default="http://magpie:3425")
    parser.add_argument("--web-key-file", default="/run/secrets/magpie_web_key")
    parser.add_argument("--enable", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(bootstrap(args.admin_url, Path(args.web_key_file).read_text(), args.gateway_url, args.enable))
    except Exception as exc:
        # Never render HTTP bodies or validation inputs that could contain keys.
        print(f"Magpie bootstrap failed ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1)
    import json
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
