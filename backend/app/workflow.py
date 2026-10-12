from dataclasses import dataclass, field
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session as DbSession

from .db import Meeting, ResultVersion
from .model_client import ModelClient, ModelError, ModelValidationError
from .schemas import MinutesResult
from .services import latest_version, review_requirements, save_version, validate_refs


class MeetingGenerationState(TypedDict, total=False):
    meeting_id: str
    chunks: list[dict[str, str]]
    meeting_type: str
    generation_result: MinutesResult | None
    validation_error: str | None
    retry_count: int
    model_mode: str
    needs_human_review: bool


@dataclass
class MeetingWorkflowState:
    meeting_id: str
    source_type: str
    raw_text: str
    chunks: list[dict[str, str]]
    meeting_type: str = "generic"
    minutes: MinutesResult | None = None
    validation_errors: list[str] = field(default_factory=list)
    retry_count: int = 0
    result_version_id: str | None = None
    needs_human_review: bool = True


class MeetingMinutesWorkflow:
    """LangGraph orchestrates generation/validation/retry; SQLAlchemy stays outside the graph."""

    def __init__(self, model_client: ModelClient, model_mode: str = "fake"):
        self.model_client = model_client
        self.model_mode = model_mode
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(MeetingGenerationState)
        builder.add_node("prepare_context", self._prepare_context)
        builder.add_node("generate", self._generate)
        builder.add_node("validate", self._validate)
        builder.add_node("regenerate", self._regenerate)
        builder.add_node("prepare_review", self._prepare_review)
        builder.add_node("validation_failed", self._validation_failed)
        builder.add_edge(START, "prepare_context")
        builder.add_edge("prepare_context", "generate")
        builder.add_edge("generate", "validate")
        builder.add_conditional_edges(
            "validate",
            self._route_after_validation,
            {"success": "prepare_review", "retry": "regenerate", "failed": "validation_failed"},
        )
        builder.add_edge("regenerate", "validate")
        builder.add_edge("prepare_review", END)
        builder.add_edge("validation_failed", END)
        return builder.compile()

    def _prepare_context(self, state: MeetingGenerationState) -> dict:
        return {"validation_error": None, "retry_count": state.get("retry_count", 0)}

    def _generate(self, state: MeetingGenerationState) -> dict:
        try:
            result = self.model_client.generate_minutes(
                state["chunks"], state.get("meeting_type", "generic"), None
            )
            return {"generation_result": result, "validation_error": None}
        except ModelValidationError as exc:
            return {"generation_result": None, "validation_error": str(exc)}

    def _validate(self, state: MeetingGenerationState) -> dict:
        result = state.get("generation_result")
        if result is None:
            return {"validation_error": state.get("validation_error") or "模型没有返回纪要"}
        try:
            result = MinutesResult.model_validate(result)
            validate_refs(result, {item["id"] for item in state["chunks"]})
            return {"generation_result": result, "validation_error": None}
        except ValueError as exc:
            return {"validation_error": str(exc)}

    def _route_after_validation(self, state: MeetingGenerationState) -> str:
        if not state.get("validation_error"):
            return "success"
        if state.get("retry_count", 0) < 1:
            return "retry"
        return "failed"

    def _regenerate(self, state: MeetingGenerationState) -> dict:
        error = state.get("validation_error") or "未知校验错误"
        try:
            result = self.model_client.generate_minutes(
                state["chunks"], state.get("meeting_type", "generic"), error
            )
            return {
                "generation_result": result,
                "validation_error": None,
                "retry_count": state.get("retry_count", 0) + 1,
            }
        except ModelValidationError as exc:
            return {
                "generation_result": None,
                "validation_error": str(exc),
                "retry_count": state.get("retry_count", 0) + 1,
            }

    def _prepare_review(self, state: MeetingGenerationState) -> dict:
        result = state["generation_result"]
        if result is None:
            raise ModelError("工作流没有可复核的纪要")
        result.review_status = "pending"
        result.review_required_fields = review_requirements(result)
        return {"generation_result": result, "needs_human_review": True}

    def _validation_failed(self, state: MeetingGenerationState) -> dict:
        return state

    def initialize(self, meeting: Meeting) -> MeetingWorkflowState:
        return MeetingWorkflowState(
            meeting_id=meeting.id,
            source_type=meeting.source_type,
            raw_text=meeting.raw_text,
            chunks=[{"id": item.id, "text": item.text} for item in meeting.chunks],
            meeting_type=meeting.context.meeting_type if meeting.context else "generic",
        )

    def generate_and_validate(self, state: MeetingWorkflowState) -> MeetingWorkflowState:
        output = self.graph.invoke(
            {
                "meeting_id": state.meeting_id,
                "chunks": state.chunks,
                "meeting_type": state.meeting_type,
                "generation_result": None,
                "validation_error": None,
                "retry_count": 0,
                "model_mode": self.model_mode,
                "needs_human_review": True,
            }
        )
        if output.get("validation_error"):
            raise ModelError(f"模型结果连续两次未通过业务校验：{output['validation_error']}")
        state.minutes = output.get("generation_result")
        state.retry_count = output.get("retry_count", 0)
        state.needs_human_review = output.get("needs_human_review", True)
        return state

    def save_result(self, state: MeetingWorkflowState, db: DbSession) -> ResultVersion:
        if state.minutes is None:
            raise ValueError("工作流没有可保存的纪要")
        parent = latest_version(db, state.meeting_id)
        version = save_version(
            db, meeting_id=state.meeting_id, kind="minutes",
            payload=state.minutes.model_dump(mode="json"), parent=parent,
        )
        state.result_version_id = version.id
        return version

    def run(self, meeting: Meeting, db: DbSession) -> tuple[MeetingWorkflowState, ResultVersion]:
        state = self.generate_and_validate(self.initialize(meeting))
        version = self.save_result(state, db)
        return state, version
