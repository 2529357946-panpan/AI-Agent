import copy
import os
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite:///./test_office_agent.db"
os.environ["USE_FAKE_MODEL"] = "true"
os.environ["USE_FAKE_TRANSCRIPTION"] = "true"
os.environ["OLLAMA_ENABLED"] = "false"
os.environ["SUPABASE_ENABLED"] = "false"

from fastapi.testclient import TestClient

from app.db import Base, engine
from app import main as main_module
from app.main import app
from app import model_client as model_client_module
from app.model_client import DeepSeekModelClient, ModelError
from app.schemas import MinutesResult, ReferencedText
from app.services import apply_revision
from app.services import validate_refs
from app.transcription import TranscriptionError


def setup_module():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def teardown_module():
    Base.metadata.drop_all(engine)
    engine.dispose()
    Path("test_office_agent.db").unlink(missing_ok=True)


def test_revision_only_changes_target_field():
    current = {
        "summary": "摘要不应改变",
        "decisions": [],
        "action_items": [
            {
                "id": "a1",
                "task": "任务一",
                "owner": "甲",
                "due_date": None,
                "status": "unknown",
                "source_refs": ["c1"],
            },
            {
                "id": "a2",
                "task": "任务二",
                "owner": "待确认",
                "due_date": None,
                "status": "unknown",
                "source_refs": ["c2"],
            },
        ],
        "open_questions": [],
        "source_refs": ["c1", "c2"],
    }
    original = copy.deepcopy(current)
    revised, changes, target = apply_revision(current, "第二项改成小王负责")
    assert target == "a2"
    assert revised["action_items"][1]["owner"] == "小王"
    assert revised["action_items"][0] == original["action_items"][0]
    assert revised["summary"] == original["summary"]
    assert changes[0]["field"] == "owner"


def test_invalid_date_rejected_without_mutation():
    current = {
        "summary": "摘要",
        "decisions": [],
        "action_items": [
            {
                "id": "a1",
                "task": "任务",
                "owner": "待确认",
                "due_date": None,
                "status": "unknown",
                "source_refs": ["c1"],
            }
        ],
        "open_questions": [],
        "source_refs": ["c1"],
    }
    original = copy.deepcopy(current)
    try:
        apply_revision(current, "第一项截止日期改成 2026-10-99")
        raise AssertionError("应拒绝非法日期")
    except ValueError:
        assert current == original


def test_full_api_flow_and_versions():
    with TestClient(app) as client:
        created = client.post(
            "/api/meetings",
            json={
                "title": "产品周会",
                "text": (
                    "会议决定周五演示新版。\n"
                    "负责人: 张三，整理客户反馈，截止日期 2026-10-09。\n"
                    "请完成演示环境准备，负责人和日期待确认。\n"
                    "这个议题可能下周再讨论。"
                ),
            },
        )
        assert created.status_code == 201
        data = created.json()

        generated = client.post(
            f"/api/meetings/{data['meeting_id']}/generate", json={"kind": "minutes"}
        )
        assert generated.status_code == 200
        result = generated.json()["result"]
        assert len(result["action_items"]) >= 2
        assert all(item["source_refs"] for item in result["action_items"])
        assert not any("可能" in item["text"] for item in result["decisions"])

        revised = client.post(
            f"/api/sessions/{data['session_id']}/messages",
            json={"content": "第二项改成小王负责"},
        )
        assert revised.status_code == 200
        assert revised.json()["result"]["action_items"][1]["owner"] == "小王"
        assert revised.json()["version_no"] == 2

        asked = client.post(
            f"/api/sessions/{data['session_id']}/messages",
            json={"content": "这次会议决定了什么？"},
        )
        assert asked.status_code == 200
        assert asked.json()["intent"] in {"question", "clarify"}
        assert asked.json()["version_no"] == 2

        item_id = revised.json()["result"]["action_items"][1]["id"]
        toggled = client.post(
            f"/api/meetings/{data['meeting_id']}/action-items/{item_id}",
            json={"done": True},
        )
        assert toggled.status_code == 200
        assert toggled.json()["result"]["action_items"][1]["status"] == "done"

        versions = client.get(f"/api/meetings/{data['meeting_id']}/versions")
        assert versions.status_code == 200
        assert len(versions.json()) == 3


def _create_and_generate(client: TestClient, title: str = "边界测试") -> dict:
    created = client.post(
        "/api/meetings",
        json={
            "title": title,
            "text": "会议决定发布演示版。\n请完成部署检查，负责人和日期待确认。",
        },
    )
    assert created.status_code == 201
    data = created.json()
    generated = client.post(
        f"/api/meetings/{data['meeting_id']}/generate", json={"kind": "minutes"}
    )
    assert generated.status_code == 200
    return data


def test_multiturn_revision_uses_previous_target():
    with TestClient(app) as client:
        data = _create_and_generate(client, "多轮修订")
        first = client.post(
            f"/api/sessions/{data['session_id']}/messages",
            json={"content": "第一项负责人改成小王"},
        )
        assert first.status_code == 200
        second = client.post(
            f"/api/sessions/{data['session_id']}/messages",
            json={"content": "刚才那项负责人其实是小李"},
        )
        assert second.status_code == 200
        assert second.json()["result"]["action_items"][0]["owner"] == "小李"
        assert second.json()["version_no"] == 3


def test_session_cannot_be_rebound_to_another_meeting():
    with TestClient(app) as client:
        first = _create_and_generate(client, "会议一")
        second = _create_and_generate(client, "会议二")
        response = client.post(
            f"/api/sessions/{first['session_id']}/messages",
            json={"content": "会议决定了什么？", "meeting_id": second["meeting_id"]},
        )
        assert response.status_code == 400
        assert response.json()["message"] == "会话与会议不匹配"


def test_invalid_inputs_have_consistent_errors_and_no_new_version():
    with TestClient(app) as client:
        blank = client.post("/api/meetings", json={"text": "   "})
        assert blank.status_code == 422
        assert set(blank.json()) == {"code", "message", "request_id"}

        unsupported = client.post(
            "/api/meetings/upload",
            files={"file": ("meeting.pdf", b"not a pdf", "application/pdf")},
        )
        assert unsupported.status_code == 400

        data = _create_and_generate(client, "非法日期")
        before = client.get(f"/api/meetings/{data['meeting_id']}/versions").json()
        invalid = client.post(
            f"/api/sessions/{data['session_id']}/messages",
            json={"content": "第一项截止日期改成 2026-10-99"},
        )
        after = client.get(f"/api/meetings/{data['meeting_id']}/versions").json()
        assert invalid.status_code == 400
        assert len(after) == len(before)


def test_model_failure_does_not_create_partial_version(monkeypatch):
    class FailingModel:
        def generate_minutes(self, _chunks, *_args, **_kwargs):
            raise ModelError("模拟上游超时")

    with TestClient(app) as client:
        created = client.post("/api/meetings", json={"text": "会议决定继续测试。"}).json()
        monkeypatch.setattr(main_module, "get_model_client", lambda: FailingModel())
        response = client.post(
            f"/api/meetings/{created['meeting_id']}/generate", json={"kind": "minutes"}
        )
        assert response.status_code == 502
        versions = client.get(f"/api/meetings/{created['meeting_id']}/versions").json()
        assert versions == []


def test_readiness_checks_database_and_weekly_unknown_is_not_completed():
    with TestClient(app) as client:
        assert client.get("/api/ready").json()["database"] == "ok"
        data = _create_and_generate(client, "周报来源")
        weekly = client.post(
            "/api/weekly-reports",
            json={
                "meeting_ids": [data["meeting_id"]],
                "period_start": "2026-10-01",
                "period_end": "2026-10-07",
            },
        )
        assert weekly.status_code == 200
        result = weekly.json()["result"]
        assert result["completed"] == []
        assert result["risks_and_pending"]


def test_invalid_source_reference_is_rejected():
    result = MinutesResult(
        summary="摘要",
        decisions=[ReferencedText(text="决定", source_refs=["不存在"])],
    )
    try:
        validate_refs(result, {"c1"})
        raise AssertionError("应拒绝无效引用")
    except ValueError as exc:
        assert "无效来源引用" in str(exc)


def test_deepseek_non_json_response_is_repaired_once(monkeypatch):
    contents = iter(["不是 JSON", '{"ok": true}'])
    call_count = 0

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            nonlocal call_count
            call_count += 1
            return {"choices": [{"message": {"content": next(contents)}}]}

    monkeypatch.setattr(model_client_module.settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(model_client_module.httpx, "post", lambda *args, **kwargs: FakeResponse())
    assert DeepSeekModelClient()._complete_json("测试") == '{"ok": true}'
    assert call_count == 2


def test_audio_transcript_creates_meeting_chunks_and_minutes():
    transcript = "会议决定发布新版。请完成上线检查，负责人和日期待确认。"
    with TestClient(app) as client:
        response = client.post(
            "/api/meetings/audio",
            files={"file": ("meeting.wav", transcript.encode("utf-8"), "audio/wav")},
            data={"title": "音频周会"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["source_type"] == "audio"
        assert body["raw_text"] == transcript
        assert body["source_chunks"]
        assert body["current_result"]["action_items"]
        assert body["current_result"]["review_status"] == "pending"
        assert body["transcription_mode"] == "fake"


def test_transcription_failure_is_atomic(monkeypatch):
    class FailingTranscription:
        def transcribe(self, *_args, **_kwargs):
            raise TranscriptionError("模拟转写失败")

    with TestClient(app) as client:
        before = len(client.get("/api/meetings").json())
        monkeypatch.setattr(
            main_module, "get_transcription_client", lambda: FailingTranscription()
        )
        response = client.post(
            "/api/meetings/audio",
            files={"file": ("meeting.wav", b"audio", "audio/wav")},
        )
        assert response.status_code == 502
        assert len(client.get("/api/meetings").json()) == before


def test_audio_model_failure_rolls_back_meeting_and_version(monkeypatch):
    class FailingModel:
        def generate_minutes(self, _chunks, *_args, **_kwargs):
            raise ModelError("模拟模型失败")

    with TestClient(app) as client:
        before = len(client.get("/api/meetings").json())
        monkeypatch.setattr(main_module, "get_model_client", lambda: FailingModel())
        response = client.post(
            "/api/meetings/audio",
            files={
                "file": (
                    "meeting.wav",
                    "会议决定继续测试。".encode("utf-8"),
                    "audio/wav",
                )
            },
        )
        assert response.status_code == 502
        assert len(client.get("/api/meetings").json()) == before


def test_human_review_creates_confirmed_version_and_preserves_model_version():
    with TestClient(app) as client:
        data = _create_and_generate(client, "人工确认")
        generated_versions = client.get(
            f"/api/meetings/{data['meeting_id']}/versions"
        ).json()
        item_id = generated_versions[0]["payload"]["action_items"][0]["id"]
        reviewed = client.post(
            f"/api/meetings/{data['meeting_id']}/review",
            json={
                "confirm": True,
                "patches": [
                    {"item_id": item_id, "field": "owner", "value": "小王"},
                    {"item_id": item_id, "field": "due_date", "value": "2026-10-20"},
                    {"item_id": item_id, "field": "status", "value": "todo"},
                ],
            },
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["result"]["review_status"] == "confirmed"
        versions = client.get(f"/api/meetings/{data['meeting_id']}/versions").json()
        assert len(versions) == 2
        assert versions[0]["payload"]["review_status"] == "confirmed"
        assert versions[1]["payload"].get("review_status", "pending") == "pending"


def test_incomplete_human_review_does_not_create_version():
    with TestClient(app) as client:
        data = _create_and_generate(client, "未完成确认")
        before = client.get(f"/api/meetings/{data['meeting_id']}/versions").json()
        response = client.post(
            f"/api/meetings/{data['meeting_id']}/review",
            json={"confirm": True, "patches": []},
        )
        after = client.get(f"/api/meetings/{data['meeting_id']}/versions").json()
        assert response.status_code == 400
        assert len(after) == len(before)


def test_action_item_lifecycle_and_overview():
    with TestClient(app) as client:
        data = _create_and_generate(client, "生命周期")
        meeting = client.get(f"/api/meetings/{data['meeting_id']}").json()
        item_id = meeting["current_result"]["action_items"][0]["id"]
        for expected_version, status in enumerate(("todo", "in_progress", "done"), start=2):
            response = client.post(
                f"/api/meetings/{data['meeting_id']}/action-items/{item_id}/status",
                json={"status": status},
            )
            assert response.status_code == 200
            assert response.json()["version_no"] == expected_version
            assert response.json()["result"]["action_items"][0]["status"] == status
        overview = client.get("/api/action-items/overview").json()
        assert any(item["meeting_id"] == data["meeting_id"] for item in overview["done"])
