"""Isolated device-link E2E server: real router/manager, no production DB or agents.

Run only on a local test port with DEVICE_LINK_STORE_FILE pointing at disposable storage:
    uvicorn tests.e2e.device_link_app:app --host 127.0.0.1 --port 18885
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.device_link import router
from app.models.device_link import PromptEnvelope
from app.services.device_link_service import device_link_manager

app = FastAPI()
app.include_router(router)


@app.post("/_test/prompt")
async def send_test_prompt(envelope: PromptEnvelope):
    return await device_link_manager.send_prompt(envelope.target_device_id, envelope)


# Serve the existing production-built Vue UI, including client-side routes, for browser verification.
dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if dist.exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{route:path}")
    async def frontend(route: str):
        return FileResponse(dist / "index.html")
