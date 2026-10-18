# Architecture

本项目是面向教培演示场景的 Meeting Execution Agent，不代表真实客户上线。

## 核心边界

```text
会议输入 -> Meeting / SourceChunk -> LangGraph 生成与校验
        -> pending review -> 人工确认 -> append-only ResultVersion

已确认会议 SourceChunk + synthetic SOP -> keyword / vector -> RRF
                                           -> 只读知识回答
```

- 当前纪要只接收当前 Meeting 的 SourceChunk，历史 RAG 不参与事实提取。
- LLM 只负责理解与生成；Pydantic、`validate_refs`、`FieldPatch` 和事务控制负责业务约束。
- SQLite/PostgreSQL 保存核心业务状态。Supabase 只承载 pgvector 和结果缓存，故障不影响会议主链。
- Ollama 负责输入风险信号、HyDE 和 embedding；DeepSeek 负责纪要与最终知识回答。

## LangGraph

```text
START -> prepare_context -> generate -> validate
                                    success -> prepare_review -> END
                                    failure -> regenerate -> validate
                                    second failure -> validation_failed -> END/Error
```

数据库写入在 Graph 成功退出后由 API/SQLAlchemy 完成；人工复核是独立 HTTP 请求。

## RAG

```text
query -> local inspection / route -- injection --> blocked structured response
                |
                +-> original query -> BM25 + score cutoff --------+
                |                                                  |
                +-> constrained HyDE -> embed -> vector + cutoff --+-> RRF
                                                                      -> answer
                                                                      -> supported/schema/ref validation
                                                                      -> cited sources only
```

默认向量阈值为 `0.60`，BM25 使用中文 bigram，并同时采用绝对分数 `2.50` 与最高分 `20%` 的动态截断。这些参数来自当前 synthetic corpus 的小样本验收，只是可解释基线，不视为跨语料通用最优值。

会议 `source_refs` 与 RAG `knowledge_refs` 是两个不同引用域。只有 `review_status=confirmed` 的会议会被幂等索引，稳定 ID 为 `meeting:{meeting_id}:chunk:{chunk_id}`。

## Storage

- `MeetingContext`：Meeting 的一对一扩展，保存校区、部门和会议类型。
- `ResultVersion`：纪要及状态的 append-only source of truth。
- `rag_chunks`：可选 Supabase sidecar 表。
- `result_cache`：只缓存 `generate_minutes` 与 RAG 最终答案；命中结果仍做 Pydantic 校验。
