from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

import httpx

from .config import ROOT_DIR, settings
from .db import Meeting, ResultVersion
from .local_model import (
    EmbeddingClient,
    EmbeddingError,
    LocalModelClient,
    LocalModelError,
    get_embedding_client,
    get_local_model_client,
)
from .model_client import ModelClient, ModelError, get_model_client
from .schemas import KnowledgeQuery, KnowledgeSource

logger = logging.getLogger(__name__)


class RetrievalError(RuntimeError):
    pass


@dataclass
class RetrievalDocument:
    source_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)


@dataclass
class RankedDocument:
    document: RetrievalDocument
    score: float


def tokenize(text: str) -> list[str]:
    """Lightweight Chinese tokenizer: ASCII words plus Chinese unigram/bigram tokens."""
    lowered = text.lower()
    ascii_words = re.findall(r"[a-z0-9_]+", lowered)
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", lowered)
    tokens = list(ascii_words)
    for run in chinese_runs:
        tokens.extend(run)
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def keyword_search(
    query: str, documents: list[RetrievalDocument], top_k: int
) -> list[RankedDocument]:
    query_tokens = tokenize(query)
    if not query_tokens or not documents:
        return []
    corpus_tokens = [tokenize(document.text) for document in documents]
    document_frequency = Counter(
        token for tokens in corpus_tokens for token in set(tokens)
    )
    average_length = sum(map(len, corpus_tokens)) / len(corpus_tokens) or 1.0
    query_counts = Counter(query_tokens)
    ranked: list[RankedDocument] = []
    for document, tokens in zip(documents, corpus_tokens):
        frequencies = Counter(tokens)
        score = 0.0
        for token, query_frequency in query_counts.items():
            frequency = frequencies[token]
            if not frequency:
                continue
            idf = math.log(1 + (len(documents) - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))
            denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * len(tokens) / average_length)
            score += idf * frequency * 2.5 / denominator * query_frequency
        if score > 0:
            ranked.append(RankedDocument(document, score))
    return sorted(ranked, key=lambda item: (-item.score, item.document.source_id))[:top_k]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def rrf_fuse(
    rankings: list[list[RankedDocument]], *, k: int = 60, top_k: int = 5
) -> list[RankedDocument]:
    scores: dict[str, float] = {}
    documents: dict[str, RetrievalDocument] = {}
    for ranking in rankings:
        seen: set[str] = set()
        for rank, item in enumerate(ranking, start=1):
            source_id = item.document.source_id
            if source_id in seen:
                continue
            seen.add(source_id)
            documents[source_id] = item.document
            scores[source_id] = scores.get(source_id, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores, key=lambda source_id: (-scores[source_id], source_id))[:top_k]
    return [RankedDocument(documents[source_id], scores[source_id]) for source_id in ordered]


class InMemoryRagStore:
    def __init__(self):
        self._documents: dict[str, RetrievalDocument] = {}
        self._lock = Lock()

    def upsert(self, documents: list[RetrievalDocument]) -> None:
        with self._lock:
            for document in documents:
                self._documents[document.source_id] = document

    def list(self, filters: dict[str, str | None] | None = None) -> list[RetrievalDocument]:
        documents = list(self._documents.values())
        if filters:
            documents = [
                document
                for document in documents
                if all(not value or document.metadata.get(key) == value for key, value in filters.items())
            ]
        return documents

    def vector_search(
        self, vector: list[float], *, top_k: int, filters: dict[str, str | None] | None = None
    ) -> list[RankedDocument]:
        ranked = [
            RankedDocument(document, cosine_similarity(vector, document.embedding))
            for document in self.list(filters)
        ]
        return sorted(ranked, key=lambda item: (-item.score, item.document.source_id))[:top_k]


class SupabaseRagStore(InMemoryRagStore):
    """Supabase pgvector sidecar. A local mirror supports BM25 for the current process."""

    def __init__(self):
        super().__init__()
        if not settings.supabase_url or not settings.supabase_key:
            raise RetrievalError("Supabase RAG 未配置 URL 或 Key")
        self.base_url = settings.supabase_url.rstrip("/")
        self.headers = {
            "apikey": settings.supabase_key,
            "Authorization": f"Bearer {settings.supabase_key}",
            "Content-Type": "application/json",
        }

    def upsert(self, documents: list[RetrievalDocument]) -> None:
        payload = [
            {
                "id": document.source_id,
                "content": document.text,
                "embedding": document.embedding,
                "metadata": document.metadata,
            }
            for document in documents
        ]
        try:
            response = httpx.post(
                f"{self.base_url}/rest/v1/rag_chunks",
                headers={**self.headers, "Prefer": "resolution=merge-duplicates"},
                params={"on_conflict": "id"},
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RetrievalError(f"Supabase RAG upsert 失败：{exc}") from exc
        super().upsert(documents)

    def list(self, filters: dict[str, str | None] | None = None) -> list[RetrievalDocument]:
        params = {"select": "id,content,metadata"}
        try:
            response = httpx.get(
                f"{self.base_url}/rest/v1/rag_chunks",
                headers=self.headers,
                params=params,
                timeout=10,
            )
            response.raise_for_status()
            documents = [
                RetrievalDocument(
                    source_id=row["id"], text=row["content"], metadata=row.get("metadata") or {}
                )
                for row in response.json()
            ]
            if filters:
                documents = [
                    document for document in documents
                    if all(not value or document.metadata.get(key) == value for key, value in filters.items())
                ]
            return documents
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise RetrievalError(f"Supabase keyword corpus 读取失败：{exc}") from exc

    def vector_search(self, vector: list[float], *, top_k: int, filters=None) -> list[RankedDocument]:
        try:
            response = httpx.post(
                f"{self.base_url}/rest/v1/rpc/match_rag_chunks",
                headers=self.headers,
                json={"query_embedding": vector, "match_count": top_k, "filter": filters or {}},
                timeout=10,
            )
            response.raise_for_status()
            return [
                RankedDocument(
                    RetrievalDocument(
                        source_id=row["id"],
                        text=row["content"],
                        metadata=row.get("metadata") or {},
                        embedding=[],
                    ),
                    float(row.get("similarity", 0)),
                )
                for row in response.json()
            ]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise RetrievalError(f"Supabase vector search 失败：{exc}") from exc


class HybridRetrievalService:
    def __init__(
        self,
        *,
        store: InMemoryRagStore | None = None,
        local_model: LocalModelClient | None = None,
        embedding_client: EmbeddingClient | None = None,
        answer_model: ModelClient | None = None,
    ):
        self.store = store or (SupabaseRagStore() if settings.supabase_enabled else InMemoryRagStore())
        self.local_model = local_model or get_local_model_client()
        self.embedding_client = embedding_client or get_embedding_client()
        self.answer_model = answer_model or get_model_client()
        self.last_keyword_query = ""
        self.last_vector_query = ""
        self.demo_loaded = False

    def load_demo_knowledge(self) -> None:
        if self.demo_loaded:
            return
        knowledge_dir = ROOT_DIR / "fixtures" / "knowledge"
        documents = []
        for path in sorted(knowledge_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            documents.append(
                RetrievalDocument(
                    source_id=f"sop:{path.stem}",
                    text=text,
                    metadata={
                        "source_kind": "sop",
                        "title": path.stem,
                        "synthetic": True,
                    },
                    embedding=self.embedding_client.embed(text),
                )
            )
        if documents:
            self.store.upsert(documents)
        self.demo_loaded = True

    def query(self, payload: KnowledgeQuery) -> dict:
        if not settings.rag_enabled:
            raise RetrievalError("RAG 未启用")
        try:
            self.load_demo_knowledge()
            inspection = self.local_model.inspect_input(payload.query)
            if not inspection.valid:
                raise RetrievalError("查询内容为空或无效")
            normalized = inspection.normalized_query
            filters = {
                key: value
                for key, value in {
                    "branch_name": payload.branch_name,
                    "department": payload.department,
                    "meeting_type": payload.meeting_type,
                }.items()
                if value is not None
            }
            candidates = self.store.list(filters)
            self.last_keyword_query = normalized
            keyword_results = keyword_search(normalized, candidates, settings.rag_top_k * 2)
            hypothetical = self.local_model.generate_hypothetical(normalized)
            self.last_vector_query = hypothetical
            vector = self.embedding_client.embed(hypothetical)
            vector_results = self.store.vector_search(
                vector, top_k=settings.rag_top_k * 2, filters=filters
            )
            final = rrf_fuse(
                [keyword_results, vector_results], k=settings.rrf_k, top_k=settings.rag_top_k
            )
            sources = [
                KnowledgeSource(
                    source_id=item.document.source_id,
                    text=item.document.text,
                    score=item.score,
                    metadata=item.document.metadata,
                )
                for item in final
            ]
            answer = self.answer_model.answer_knowledge(
                normalized, [source.model_dump() for source in sources]
            )
            valid_ids = {source.source_id for source in sources}
            if not set(answer.knowledge_refs).issubset(valid_ids):
                raise RetrievalError("知识回答引用了未检索到的来源")
            cache_hit = bool(getattr(self.answer_model, "last_cache_hit", False))
            logger.info(
                "rag_keyword_count=%d rag_vector_count=%d rag_final_count=%d",
                len(keyword_results), len(vector_results), len(final),
            )
            return {
                "answer": answer.answer,
                "knowledge_refs": answer.knowledge_refs,
                "sources": [source.model_dump() for source in sources],
                "retrieval": {
                    "keyword_count": len(keyword_results),
                    "vector_count": len(vector_results),
                    "final_count": len(final),
                },
                "inspection": inspection.model_dump(),
                "model_mode": "fake" if settings.use_fake_model else "deepseek",
                "cache_hit": cache_hit,
            }
        except (EmbeddingError, LocalModelError, ModelError) as exc:
            raise RetrievalError(str(exc)) from exc

    def index_confirmed_meeting(self, meeting: Meeting, version: ResultVersion) -> int:
        payload = version.payload
        if payload.get("review_status") != "confirmed":
            return 0
        context = meeting.context
        documents = []
        for chunk in meeting.chunks:
            source_id = f"meeting:{meeting.id}:chunk:{chunk.id}"
            metadata = {
                "source_kind": "meeting",
                "meeting_id": meeting.id,
                "result_version_id": version.id,
                "title": meeting.title,
                "branch_name": context.branch_name if context else None,
                "department": context.department if context else None,
                "meeting_type": context.meeting_type if context else "generic",
                "created_at": meeting.created_at.isoformat(),
                "chunk_id": chunk.id,
            }
            documents.append(
                RetrievalDocument(
                    source_id=source_id,
                    text=chunk.text,
                    metadata=metadata,
                    embedding=self.embedding_client.embed(chunk.text),
                )
            )
        self.store.upsert(documents)
        return len(documents)


_service: HybridRetrievalService | None = None
_service_lock = Lock()


def get_retrieval_service() -> HybridRetrievalService:
    global _service
    with _service_lock:
        if _service is None:
            _service = HybridRetrievalService()
        return _service


def reset_retrieval_service() -> None:
    global _service
    with _service_lock:
        _service = None
