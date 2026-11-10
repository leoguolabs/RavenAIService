"""Carry one OpenAI admission decision through a Raven model task."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar

import httpx

# Magpie redacts *-Token headers in captured requests.
ADMISSION_HEADER = "X-Raven-OpenAI-Admission-Token"
ADMISSION_URL = "http://openai-balance-guard:8099/task-admission"


class TaskAdmission:
    def __init__(self):
        self._proof: str | None = None
        self._lock = asyncio.Lock()

    async def prepare(self, api_key: str, model: str, agent_kind: str) -> str:
        if os.environ.get("RAVEN_OPENAI_TASK_BALANCE") != "true":
            return ""
        async with self._lock:
            if self._proof is not None:
                return self._proof
            if agent_kind == "title_generator":
                return ""
            self._proof = ""
            if not model.startswith(("group/", "auto/", "raven-openai/")):
                return ""
            try:
                async with asyncio.timeout(3), httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post(ADMISSION_URL, headers={"Authorization": f"Bearer {api_key}"})
                    if response.is_success:
                        proof = response.json().get("admission")
                        if isinstance(proof, str) and proof.isascii() and len(proof) <= 160:
                            self._proof = proof
            except (httpx.HTTPError, TimeoutError, ValueError, AttributeError):
                pass  # With no proof, the guard rejects and Magpie can fall back.
            return self._proof


current_admission: ContextVar[TaskAdmission | None] = ContextVar("openai_task_admission", default=None)


@asynccontextmanager
async def admission_scope(admission: TaskAdmission | None = None):
    token = current_admission.set(admission or current_admission.get() or TaskAdmission())
    try:
        yield
    finally:
        current_admission.reset(token)
