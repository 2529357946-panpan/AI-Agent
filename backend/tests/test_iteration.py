from app.cache import InMemoryCacheClient
from app.local_model import FakeEmbeddingClient, FakeLocalModelClient
from app.model_client import CachedModelClient, FakeModelClient, ModelError, ModelValidationError
from app.retrieval import (
    HybridRetrievalService,
    InMemoryRagStore,
    RankedDocument,
    RetrievalDocument,
    rrf_fuse,
)
from app.schemas import KnowledgeQuery, MinutesResult
from app.workflow import MeetingMinutesWorkflow, MeetingWorkflowState


def minutes(ref: str) -> MinutesResult:
    return MinutesResult(summary="测试纪要", source_refs=[ref])


class SequenceModel(FakeModelClient):
    def __init__(self, refs: list[str]):
        self.refs = refs
        self.calls = 0
        self.validation_errors: list[str | None] = []

    def generate_minutes(self, chunks, meeting_type="generic", validation_error=None):
        self.validation_errors.append(validation_error)
        ref = self.refs[min(self.calls, len(self.refs) - 1)]
        self.calls += 1
        return minutes(ref)


def workflow_state() -> MeetingWorkflowState:
    return MeetingWorkflowState(
        meeting_id="meeting-1", source_type="text", raw_text="当前会议", chunks=[{"id": "current", "text": "当前会议"}]
    )


def test_langgraph_valid_generation_does_not_retry():
    model = SequenceModel(["current"])
    state = MeetingMinutesWorkflow(model).generate_and_validate(workflow_state())
    assert model.calls == 1
    assert state.retry_count == 0


def test_langgraph_validation_failure_retries_once_with_error():
    model = SequenceModel(["history", "current"])
    state = MeetingMinutesWorkflow(model).generate_and_validate(workflow_state())
    assert model.calls == 2
    assert state.retry_count == 1
    assert model.validation_errors[1]


class SchemaErrorThenValidModel(SequenceModel):
    def generate_minutes(self, chunks, meeting_type="generic", validation_error=None):
        self.validation_errors.append(validation_error)
        self.calls += 1
        if self.calls == 1:
            raise ModelValidationError("status=pending 不属于允许枚举")
        return minutes("current")


def test_langgraph_model_schema_error_enters_business_retry():
    model = SchemaErrorThenValidModel(["current"])
    state = MeetingMinutesWorkflow(model).generate_and_validate(workflow_state())
    assert model.calls == 2
    assert state.retry_count == 1
    assert "status=pending" in (model.validation_errors[1] or "")


def test_langgraph_two_validation_failures_raise_without_result():
    model = SequenceModel(["history", "still-history"])
    try:
        MeetingMinutesWorkflow(model).generate_and_validate(workflow_state())
        raise AssertionError("expected ModelError")
    except ModelError:
        pass
    assert model.calls == 2


def test_rrf_uses_stable_ids_and_expected_order():
    a = RetrievalDocument("a", "A")
    b = RetrievalDocument("b", "B")
    c = RetrievalDocument("c", "C")
    result = rrf_fuse(
        [
            [RankedDocument(a, 9), RankedDocument(b, 8)],
            [RankedDocument(b, 9), RankedDocument(c, 8)],
        ],
        k=60,
        top_k=3,
    )
    assert [item.document.source_id for item in result] == ["b", "a", "c"]


class TrackingLocalModel(FakeLocalModelClient):
    def generate_hypothetical(self, query: str) -> str:
        return f"HYDE::{query}::教务负责人确认调课"


class CapturingStore(InMemoryRagStore):
    last_filters = None

    def vector_search(self, vector, *, top_k, filters=None):
        self.last_filters = filters
        return super().vector_search(vector, top_k=top_k, filters=filters)


def test_hyde_only_feeds_vector_route_and_sop_is_retrieved():
    store = CapturingStore()
    service = HybridRetrievalService(
        store=store,
        local_model=TrackingLocalModel(),
        embedding_client=FakeEmbeddingClient(),
        answer_model=FakeModelClient(),
    )
    result = service.query(KnowledgeQuery(query="调课最终由谁确认？"))
    assert service.last_keyword_query == "调课最终由谁确认？"
    assert service.last_vector_query.startswith("HYDE::")
    assert store.last_filters == {}
    assert any(source["source_id"] == "sop:排课调课流程" for source in result["sources"])


class CountingModel(FakeModelClient):
    def __init__(self):
        self.calls = 0

    def generate_minutes(self, chunks, meeting_type="generic", validation_error=None):
        self.calls += 1
        return minutes(chunks[0]["id"])


def test_cached_minutes_skip_second_model_call_and_validate_payload():
    delegate = CountingModel()
    cached = CachedModelClient(delegate, InMemoryCacheClient(), model_name="test-model")
    chunks = [{"id": "chunk-1", "text": "决定测试"}]
    first = cached.generate_minutes(chunks)
    second = cached.generate_minutes(chunks)
    assert delegate.calls == 1
    assert isinstance(second, MinutesResult)
    assert first == second
    assert cached.last_cache_hit is True


def test_historical_rag_does_not_contaminate_current_meeting_generation():
    store = InMemoryRagStore()
    store.upsert([RetrievalDocument("history", "历史会议决定采购新设备", embedding=[1.0])])
    current = [{"id": "current", "text": "本次会议只讨论课程资料整理，由教师A负责。"}]
    result = FakeModelClient().generate_minutes(current)
    serialized = result.model_dump_json()
    assert "采购新设备" not in serialized
    assert all(ref == "current" for ref in result.source_refs)
