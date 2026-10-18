from __future__ import annotations

import json

from app.retrieval import get_retrieval_service, keyword_search, reset_retrieval_service


QUERIES = [
    "演示制度中调课最终由谁确认？",
    "临时改变课程时间或教室时，最后需要哪个角色拍板？",
    "校车维修费用最终由谁审批？",
]


def serialize(items):
    return [
        {"source_id": item.document.source_id, "score": round(item.score, 6)}
        for item in items
    ]


def main() -> None:
    reset_retrieval_service()
    service = get_retrieval_service()
    service.load_demo_knowledge()
    documents = service.store.list({})
    rows = []
    for query in QUERIES:
        inspection = service.local_model.inspect_input(query)
        keyword = keyword_search(inspection.normalized_query, documents, 10)
        hypothetical = service.local_model.generate_hypothetical(inspection.normalized_query)
        vector = service.embedding_client.embed(hypothetical)
        vector_results = service.store.vector_search(vector, top_k=10, filters={})
        rows.append(
            {
                "query": query,
                "normalized_query": inspection.normalized_query,
                "hyde": hypothetical,
                "keyword": serialize(keyword),
                "vector": serialize(vector_results),
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
