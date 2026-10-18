from __future__ import annotations

import json

from app.config import settings
from app.retrieval import get_retrieval_service, keyword_search, reset_retrieval_service, rrf_fuse
from evals.rag_eval import CASES


def main() -> None:
    reset_retrieval_service()
    service = get_retrieval_service()
    service.load_demo_knowledge()
    documents = service.store.list({})
    rows = []

    for case in [item for item in CASES if item["expected_ref"]]:
        inspection = service.local_model.inspect_input(case["query"])
        normalized = inspection.normalized_query

        keyword = keyword_search(normalized, documents, settings.rag_top_k * 2)
        if keyword:
            cutoff = max(
                settings.rag_min_keyword_score,
                keyword[0].score * settings.rag_keyword_relative_cutoff,
            )
            keyword = [item for item in keyword if item.score >= cutoff]

        hypothetical = service.local_model.generate_hypothetical(normalized)
        vector = service.embedding_client.embed(hypothetical)
        vector_results = [
            item
            for item in service.store.vector_search(vector, top_k=settings.rag_top_k * 2, filters={})
            if item.score >= settings.rag_min_vector_score
        ]
        final = rrf_fuse(
            [keyword, vector_results],
            k=settings.rrf_k,
            top_k=settings.rag_top_k,
        )
        ranked_ids = [item.document.source_id for item in final]
        rows.append(
            {
                "id": case["id"],
                "expected_ref": case["expected_ref"],
                "ranked_ids": ranked_ids,
                "relevant_rank": (
                    ranked_ids.index(case["expected_ref"]) + 1
                    if case["expected_ref"] in ranked_ids
                    else None
                ),
            }
        )

    recalls = {}
    for k in (1, 3, 5):
        per_query = [
            1.0 if row["relevant_rank"] is not None and row["relevant_rank"] <= k else 0.0
            for row in rows
        ]
        recalls[f"recall@{k}"] = sum(per_query) / len(per_query)

    print(json.dumps({"query_count": len(rows), "rows": rows, "macro_recall": recalls}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
