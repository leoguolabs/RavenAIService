"""Upload contracts: real multipart/SSE rejection and accepted workspace files."""

import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from app.api import ai_chat
from app.agents.log_analysis.workspace import populate_logs_dir
from app.services.log_analysis_chat_service import LogAnalysisChatService
from app.utils.validation import FileValidator, file_validator


@pytest.mark.asyncio
@pytest.mark.parametrize("extension,mime", [
    ("md", "text/markdown"), ("markdown", "text/x-markdown"),
    ("ini", "application/octet-stream"), ("cfg", "text/plain"),
    ("conf", "text/plain"), ("yaml", "application/yaml"),
    ("yml", "text/yaml"), ("toml", "application/toml"),
    ("properties", "text/plain"), ("MD", "text/markdown"),
])
async def test_text_attachment_passes_validation_and_reaches_workspace(tmp_path, extension, mime):
    payload = "# 测试要点\nmode = test\n".encode()
    filename = f"测试配置.{extension}"
    upload = UploadFile(filename=filename, file=io.BytesIO(payload), headers=Headers({"content-type": mime}))
    assert await FileValidator().validate_upload_file(upload) == (True, "")
    # Validation must rewind the stream for the actual save operation.
    source = tmp_path / filename
    source.write_bytes(await upload.read())
    logs = tmp_path / "logs"
    logs.mkdir()
    kind, _ = populate_logs_dir(source, logs, preferred_name=filename)
    assert kind == "text"
    assert (logs / filename).read_bytes() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("extension,mime,payload", [
    ("xls", "application/vnd.ms-excel", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1binary spreadsheet"),
    ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04sheet"),
    ("xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12", b"PK\x03\x04sheet"),
])
async def test_excel_attachment_is_preserved_as_binary(tmp_path, extension, mime, payload):
    filename = f"report.{extension}"
    upload = UploadFile(filename=filename, file=io.BytesIO(payload), headers=Headers({"content-type": mime}))
    assert await FileValidator().validate_upload_file(upload) == (True, "")
    source = tmp_path / filename
    source.write_bytes(await upload.read())
    logs = tmp_path / "logs"
    logs.mkdir()
    kind, _ = populate_logs_dir(source, logs, preferred_name=filename)
    assert kind == "spreadsheet"
    assert (logs / filename).read_bytes() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("extension,mime", [
    ("pcap", "application/vnd.tcpdump.pcap"),
    ("pcap0", "application/octet-stream"),
    ("PCAP0", "application/x-pcap"),
])
async def test_capture_attachment_is_preserved_as_binary(tmp_path, extension, mime):
    payload = bytes.fromhex("d4c3b2a1 02000400 00000000 00000000 ffff0000 01000000")
    filename = f"capture.segment.{extension}"
    upload = UploadFile(
        filename=filename, size=len(payload), file=io.BytesIO(payload),
        headers=Headers({"content-type": mime}),
    )
    assert await FileValidator().validate_upload_file(upload) == (True, "")
    source = tmp_path / f"stored-{filename}"
    source.write_bytes(await upload.read())

    logs = tmp_path / "logs"
    kind, attachment = populate_logs_dir(source, logs, preferred_name=filename)
    assert kind == "pcap"
    assert attachment == logs / filename
    assert attachment.read_bytes() == payload


@pytest.fixture
def upload_client(monkeypatch, tmp_path):
    service = LogAnalysisChatService()
    service.registry_dir = tmp_path
    create_context = AsyncMock(side_effect=AssertionError("invalid batch must never be saved"))
    monkeypatch.setattr(service, "_create_context_from_upload", create_context)
    monkeypatch.setattr(ai_chat, "log_analysis_chat_service", service)
    app = FastAPI()
    app.include_router(ai_chat.router, prefix="/ai-chat")
    app.dependency_overrides[ai_chat.get_db] = lambda: None
    app.dependency_overrides[ai_chat.get_current_user] = lambda: SimpleNamespace(id="upload-test", language="zh")
    with TestClient(app) as client:
        yield client, create_context, service


@pytest.mark.parametrize("locale", ["zh", "en"])
@pytest.mark.parametrize("filename,mime", [("report.pdf", "application/pdf"), ("capture.pcap1", "application/octet-stream"), ("README", "text/plain")])
def test_multipart_unknown_format_returns_actionable_sse_before_any_save(upload_client, locale, filename, mime):
    client, create_context, service = upload_client
    response = client.post(
        "/ai-chat/log-analysis/stream",
        headers={"X-App-Locale": locale},
        data={"message": "检查覆盖率", "remember": "false"},
        files=[("files", ("test.md", b"# checklist", "text/markdown")), ("files", (filename, b"unsupported", mime))],
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    error = events[-1]
    assert error["event"] == "error"
    assert error["reason"] == "unsupported_format"
    assert error["filename"] == filename
    assert filename in error["message"]
    assert all(ext in error["message"] for ext in (".md", ".xls", ".pcap", ".pcap0"))
    assert ("尚未保存" if locale == "zh" else "No files in this batch were saved") in error["message"]
    assert "准备日志分析工作区时遇到了问题" not in error["message"]
    create_context.assert_not_awaited()
    assert not service._jobs


@pytest.mark.parametrize("filename,payload,limit,reason,detail", [
    ("large.md", b"123456", 4, "file_too_large", "单文件上限"),
    ("bad..md", b"ok", 100, "invalid_filename", "路径分隔符"),
])
def test_known_validation_failures_remain_specific(upload_client, monkeypatch, filename, payload, limit, reason, detail):
    client, create_context, _ = upload_client
    monkeypatch.setattr(file_validator, "max_file_size", limit)
    response = client.post("/ai-chat/log-analysis/stream", files={"files": (filename, payload, "text/markdown")})
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[-1]["reason"] == reason
    assert filename in events[-1]["message"]
    assert detail in events[-1]["message"]
    create_context.assert_not_awaited()
