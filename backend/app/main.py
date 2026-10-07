import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession, selectinload

from .config import settings
from .db import (
    Meeting,
    Message,
    ResultVersion,
    Session,
    WeeklySelection,
    check_db,
    get_db,
    init_db,
)
from .model_client import ModelError, get_model_client
from .schemas import (
    ActionToggleRequest,
    FieldPatch,
    GenerateRequest,
    MeetingCreate,
    MessageRequest,
    WeeklyRequest,
)
from .services import (
    AmbiguousInstruction,
    InvalidDateError,
    apply_patches,
    apply_revision,
    build_chunks,
    draft_weekly,
    latest_version,
    looks_like_question,
    parse_document,
    save_version,
    validate_refs,
    validate_text,
)

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger("office-agent")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="办公 Agent API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_id=%s path=%s status=%s duration_ms=%.1f",
        request_id,
        request.url.path,
        response.status_code,
        (time.perf_counter() - started) * 1000,
    )
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": f"HTTP_{exc.status_code}",
            "message": str(exc.detail),
            "request_id": request.state.request_id,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": exc.errors()[0]["msg"],
            "request_id": request.state.request_id,
        },
    )


@app.exception_handler(Exception)
async def unknown_error(request: Request, exc: Exception):
    logger.exception("request_id=%s unhandled_error", request.state.request_id)
    return JSONResponse(
        status_code=500,
        content={
            "code": "INTERNAL_ERROR",
            "message": "服务内部错误，请稍后重试",
            "request_id": request.state.request_id,
        },
    )


def meeting_response(meeting: Meeting, session_id: str | None = None) -> dict:
    versions = [item for item in meeting.versions if item.kind == "minutes"]
    versions = sorted(versions, key=lambda item: item.version_no)
    latest = versions[-1] if versions else None
    session = meeting.sessions[0] if meeting.sessions else None
    active_session_id = session_id or (session.id if session else None)
    messages = []
    if session:
        messages = [
            {
                "id": item.id,
                "role": item.role,
                "content": item.content,
                "created_at": item.created_at.isoformat(),
            }
            for item in session.messages
        ]
    return {
        "id": meeting.id,
        "meeting_id": meeting.id,
        "session_id": active_session_id,
        "title": meeting.title,
        "meeting_date": str(meeting.meeting_date) if meeting.meeting_date else None,
        "created_at": meeting.created_at.isoformat(),
        "source_type": meeting.source_type,
        "raw_text": meeting.raw_text,
        "status": meeting.status,
        "messages": messages,
        "source_chunks": [
            {
                "id": chunk.id,
                "ordinal": chunk.ordinal,
                "text": chunk.text,
                "start_char": chunk.start_char,
                "end_char": chunk.end_char,
            }
            for chunk in meeting.chunks
        ],
        "current_result": latest.payload if latest else None,
        "version_no": latest.version_no if latest else None,
        "model_mode": "fake" if settings.use_fake_model else "deepseek",
    }


def create_meeting(
    db: DbSession,
    text: str,
    title: str | None,
    meeting_date,
    source_type: str,
) -> dict:
    meeting = Meeting(
        title=title or f"会议 {meeting_date or '未命名'}",
        meeting_date=meeting_date,
        source_type=source_type,
        raw_text=validate_text(text),
    )
    meeting.chunks = build_chunks(meeting.raw_text)
    session = Session(meeting_id=meeting.id)
    meeting.sessions.append(session)
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    return meeting_response(meeting, session.id)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "model_mode": "fake" if settings.use_fake_model else "deepseek",
        "model_configured": bool(settings.deepseek_api_key) if not settings.use_fake_model else True,
    }


@app.get("/api/ready")
def readiness():
    try:
        check_db()
    except SQLAlchemyError as exc:
        logger.exception("database readiness check failed")
        raise HTTPException(status_code=503, detail="数据库连接不可用") from exc
    if not settings.use_fake_model and not settings.deepseek_api_key:
        raise HTTPException(status_code=503, detail="真实模型模式尚未配置 DEEPSEEK_API_KEY")
    return {"status": "ready", "database": "ok", "model_configured": True}


@app.post("/api/meetings", status_code=201)
def post_meeting(payload: MeetingCreate, db: DbSession = Depends(get_db)):
    return create_meeting(
        db, payload.text, payload.title, payload.meeting_date, source_type="text"
    )


@app.post("/api/meetings/upload", status_code=201)
async def upload_meeting(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    meeting_date: str | None = Form(None),
    db: DbSession = Depends(get_db),
):
    try:
        # 只多读一个字节即可判断超限，避免把任意大文件全部读入内存。
        data = await file.read(settings.max_file_bytes + 1)
        text, source_type = parse_document(file.filename or "", data)
        parsed_date = None
        if meeting_date:
            from datetime import date

            parsed_date = date.fromisoformat(meeting_date)
        return create_meeting(db, text, title, parsed_date, source_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/meetings")
def list_meetings(db: DbSession = Depends(get_db)):
    meetings = db.scalars(select(Meeting).order_by(Meeting.created_at.desc())).all()
    return [
        {
            "id": item.id,
            "title": item.title,
            "meeting_date": str(item.meeting_date) if item.meeting_date else None,
            "created_at": item.created_at.isoformat(),
            "status": item.status,
        }
        for item in meetings
    ]


def load_meeting(db: DbSession, meeting_id: str) -> Meeting:
    meeting = db.scalar(
        select(Meeting)
        .options(
            selectinload(Meeting.chunks),
            selectinload(Meeting.sessions).selectinload(Session.messages),
            selectinload(Meeting.versions),
        )
        .where(Meeting.id == meeting_id)
    )
    if not meeting:
        raise HTTPException(status_code=404, detail="会议不存在")
    return meeting


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: str, db: DbSession = Depends(get_db)):
    return meeting_response(load_meeting(db, meeting_id))


@app.post("/api/meetings/{meeting_id}/generate")
def generate_minutes(
    meeting_id: str,
    _: GenerateRequest,
    db: DbSession = Depends(get_db),
):
    meeting = load_meeting(db, meeting_id)
    chunks = [{"id": item.id, "text": item.text} for item in meeting.chunks]
    try:
        result = get_model_client().generate_minutes(chunks)
        validate_refs(result, {item["id"] for item in chunks})
        parent = latest_version(db, meeting.id)
        version = save_version(
            db,
            meeting_id=meeting.id,
            kind="minutes",
            payload=result.model_dump(mode="json"),
            parent=parent,
        )
        db.commit()
        return {
            "result": result.model_dump(mode="json"),
            "version_no": version.version_no,
            "result_version_id": version.id,
            "model_mode": "fake" if settings.use_fake_model else "deepseek",
        }
    except ModelError as exc:
        db.rollback()
        logger.exception("model_error meeting_id=%s: %s", meeting_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/meetings/{meeting_id}/action-items/{item_id}")
def toggle_action_item(
    meeting_id: str,
    item_id: str,
    payload: ActionToggleRequest,
    db: DbSession = Depends(get_db),
):
    meeting = load_meeting(db, meeting_id)
    current = latest_version(db, meeting.id)
    if not current:
        raise HTTPException(status_code=400, detail="请先生成会议纪要")
    try:
        updated, changes, target_id = apply_patches(
            current.payload,
            [
                FieldPatch(
                    item_id=item_id,
                    field="status",
                    value="done" if payload.done else "todo",
                )
            ],
        )
        version = save_version(
            db,
            meeting_id=meeting.id,
            kind="minutes",
            payload=updated,
            parent=current,
        )
        session = meeting.sessions[0] if meeting.sessions else None
        if session:
            db.add(
                Message(
                    session_id=session.id,
                    role="assistant",
                    content=f"已将待办标记为{'完成' if payload.done else '未完成'}。目标：{target_id}。",
                    result_version_id=version.id,
                )
            )
        db.commit()
        return {
            "result": updated,
            "version_no": version.version_no,
            "changes": changes,
        }
    except (AmbiguousInstruction, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sessions/{session_id}/messages")
def post_message(
    session_id: str,
    payload: MessageRequest,
    db: DbSession = Depends(get_db),
):
    session = db.get(Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    meeting_id = payload.meeting_id or session.meeting_id
    if not meeting_id:
        raise HTTPException(status_code=400, detail="当前会话未关联会议")
    if session.meeting_id and meeting_id != session.meeting_id:
        raise HTTPException(status_code=400, detail="会话与会议不匹配")
    meeting = load_meeting(db, meeting_id)
    current = latest_version(db, meeting_id)
    history = [
        {"role": item.role, "content": item.content}
        for item in db.scalars(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at.desc())
            .limit(8)
        ).all()
    ][::-1]
    previous_target = None
    for item in reversed(history):
        match = __import__("re").search(r"目标：([^。\s]+)", item["content"])
        if item["role"] == "assistant" and match:
            previous_target = match.group(1)
            break

    chunks = [{"id": item.id, "text": item.text} for item in meeting.chunks]
    updated = current.payload if current else None
    changes = None
    version = None
    target_id = None
    intent = "question"
    assistant_text = ""

    try:
        if current and not looks_like_question(payload.content):
            try:
                updated, changes, target_id = apply_revision(
                    current.payload, payload.content, previous_target
                )
                intent = "revision"
            except InvalidDateError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except (AmbiguousInstruction, ValueError):
                intent = "clarify"
        if intent != "revision":
            interpretation = get_model_client().interpret(
                payload.content,
                chunks,
                current.payload if current else None,
                history,
                previous_target,
            )
            intent = interpretation.intent
            assistant_text = interpretation.answer
            if intent == "revision":
                if not current:
                    raise HTTPException(status_code=400, detail="请先生成会议纪要，再修改结构化结果")
                if interpretation.patches:
                    updated, changes, target_id = apply_patches(
                        current.payload, interpretation.patches
                    )
                else:
                    updated, changes, target_id = apply_revision(
                        current.payload, payload.content, previous_target
                    )
    except HTTPException:
        raise
    except ModelError as exc:
        db.rollback()
        logger.exception("model_error session_id=%s: %s", session_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except InvalidDateError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (AmbiguousInstruction, ValueError) as exc:
        intent = "clarify"
        assistant_text = str(exc)
        updated = current.payload if current else None
        changes = None

    if intent == "revision" and current and updated and changes:
        version = save_version(
            db,
            meeting_id=meeting_id,
            kind="minutes",
            payload=updated,
            parent=current,
        )
        assistant_text = assistant_text or f"已按要求更新，目标：{target_id}。这是草稿版本 {version.version_no}。"
        if target_id and f"目标：{target_id}" not in assistant_text:
            assistant_text = f"{assistant_text} 目标：{target_id}。"
    elif intent == "question" and not assistant_text:
        assistant_text = "我可以回答会议内容，或帮你修改待办。"
    elif intent == "clarify" and not assistant_text:
        assistant_text = "请补充你是想提问，还是要改某一项待办。"

    db.add(Message(session_id=session_id, role="user", content=payload.content))
    db.add(
        Message(
            session_id=session_id,
            role="assistant",
            content=assistant_text,
            result_version_id=version.id if version else None,
        )
    )
    db.commit()
    return {
        "assistant_message": assistant_text,
        "intent": intent,
        "result": updated,
        "result_version_id": version.id if version else None,
        "version_no": version.version_no if version else (current.version_no if current else None),
        "changes": changes,
        "messages": [
            {"role": "user", "content": payload.content},
            {"role": "assistant", "content": assistant_text},
        ],
    }


@app.get("/api/meetings/{meeting_id}/versions")
def list_versions(meeting_id: str, db: DbSession = Depends(get_db)):
    load_meeting(db, meeting_id)
    versions = db.scalars(
        select(ResultVersion)
        .where(ResultVersion.meeting_id == meeting_id)
        .order_by(ResultVersion.version_no.desc())
    ).all()
    return [
        {
            "id": item.id,
            "kind": item.kind,
            "version_no": item.version_no,
            "payload": item.payload,
            "parent_version_id": item.parent_version_id,
            "created_at": item.created_at.isoformat(),
        }
        for item in versions
    ]


@app.post("/api/weekly-reports")
def weekly_report(payload: WeeklyRequest, db: DbSession = Depends(get_db)):
    meetings = [load_meeting(db, meeting_id) for meeting_id in payload.meeting_ids]
    result = draft_weekly(meetings, payload.notes)
    version = save_version(
        db,
        meeting_id=None,
        kind="weekly",
        payload=result.model_dump(mode="json"),
    )
    db.add(
        WeeklySelection(
            result_version_id=version.id,
            meeting_ids_json=json.dumps(payload.meeting_ids),
            period_start=payload.period_start,
            period_end=payload.period_end,
        )
    )
    db.commit()
    return {
        "result": result.model_dump(mode="json"),
        "result_version_id": version.id,
        "version_no": version.version_no,
    }
