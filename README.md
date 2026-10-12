# Meeting Execution Agent

Meeting Execution Agent 面向机构规模扩大、跨校区协作增加后的会议执行管理场景。随着教研、教务和管理会议增加，微信群、人工纪要和零散文档难以持续追踪会议决议、负责人、截止日期及跨会议状态。本项目将会议输入规范化为可追溯的 `Meeting`、`SourceChunk` 和 Action Item，并通过人工确认、版本链、跨会议聚合和历史知识检索形成执行闭环。

项目业务背景是“大连某面向小学阶段的全科课后辅导/学习服务机构”的虚构演示场景。它不代表真实客户上线，不包含真实学生信息、经营数据或效果数据。

## Business Scenario

机构规模扩大和校区整合后，教研会、教务会与校区管理例会产生的决议分散在录音、文档和沟通记录里。系统聚焦一个窄而完整的工作流：

```text
会议录音/材料 -> 结构化纪要 -> 来源校验 -> 人工确认
-> Action Item -> 状态与版本 -> 跨会议追踪 -> 历史会议/SOP 查询
```

支持 `teaching_research`（教研会）、`academic_operations`（教务会）、`branch_management`（校区/管理例会）和 `generic`。不同类型会给模型不同提取重点，同时继续复用统一的 `MinutesResult`。

## Why not a general chatbot?

通用模型能够总结会议。本项目的差异不是“模型更聪明”，而是 `Workflow + State + Grounding + Audit`：自动标准化输入、校验当前来源、人工确认、确定性修改、追加版本、长期状态和只读历史知识检索。LLM 不能直接执行 SQL、绕过 Pydantic/source_refs 校验、直接覆盖旧版本或自行修改业务状态。

## Why not a full collaboration suite?

飞书等产品是完整的通用企业协作平台。本项目不替代整套办公体系，而是演示如何以较低操作复杂度收敛“跨校区会议执行管理”这一垂直流程。这里讨论的是实施范围与 TCO 取舍，不声称功能全面强于协作平台，也不提供未经验证的成本比例。

## 已实现

- FastAPI、统一错误响应、请求 ID、SQLite/PostgreSQL 适配
- 文本、Markdown、DOCX 与音频输入；10 MB 文档、25 MB 音频、50,000 字符限制
- OpenAI-compatible STT adapter 与明确标记的 FakeTranscription
- MeetingContext（校区、部门、会议类型）一对一扩展
- 真正的 LangGraph `generate -> validate -> retry once -> prepare_review` 工作流
- DeepSeek adapter、Pydantic/日期/status/source_refs 校验
- 人工确认、确定性 FieldPatch、多轮定向修订、append-only ResultVersion
- Action Item 聚合、逾期判断、跨会议周报
- 历史知识 RAG：轻量 BM25 + HyDE embedding/vector + RRF + knowledge_refs
- Ollama adapter：输入检查、HyDE、embedding；离线模式使用确定性 Fake
- Supabase sidecar：pgvector 与模型结果缓存；核心业务库不依赖它
- React + TypeScript 三栏界面，“当前会议”和“历史知识”严格分区
- pytest、前端 lint/build、Docker/Render、GitHub Actions

## Architecture

```text
Audio/TXT/MD/DOCX/paste
          |
   Meeting + MeetingContext + SourceChunk
          |
      LangGraph
 generate -> validate --failed--> regenerate once
          |                           |
          +------- valid ------------+
          |
   Pending Review -> Human Confirmation -> ResultVersion
          |                                  |
    current meeting only                confirmed only
          |                                  v
     MinutesResult          SOP + Meeting chunks -> RAG index
                                      |             |
                                original/BM25   HyDE/vector
                                      +------ RRF --+
                                              |
                                      answer + knowledge_refs
```

详细边界见 [docs/architecture.md](docs/architecture.md)。

## LangGraph Workflow

`MeetingGenerationState` 包含 `meeting_id`、`chunks`、`meeting_type`、`generation_result`、`validation_error`、`retry_count` 和 `model_mode`。节点与边为：

```text
START -> prepare_context -> generate -> validate
validate(success) -> prepare_review -> END
validate(failure, retry_count < 1) -> regenerate -> validate
validate(second failure) -> validation_failed -> END，随后抛出 ModelError
```

校验复用既有 Pydantic 和 `validate_refs()`。重试 prompt 会带上第一次校验错误。SQLAlchemy、commit/rollback、CRUD 与人工确认不放进 Graph。

## RAG Architecture

`POST /api/knowledge/query` 只读查询已确认历史会议和 synthetic SOP：

1. FakeLocalModel/Ollama 做输入检查；结果只是 preprocessing/risk signal，不是安全边界。
2. 原始规范化 Query 进入轻量 BM25 关键词路线。
3. Ollama/Fake 生成 hypothetical document，经 embedding 进入 Supabase pgvector 或内存向量路线。
4. 两路按稳定 source ID 使用 `Σ 1/(k+rank)` 做 RRF，默认 `k=60`，取 top 5。
5. DeepSeek/Fake 根据检索上下文生成答案并返回 `knowledge_refs`。

当前会议纪要永远只使用当前 Meeting 的 SourceChunk。RAG 不进入纪要主链，避免历史信息污染当前事实。会议 `source_refs` 和知识 `knowledge_refs` 不能混用。

## Model Routing

- Ollama：本地输入检查、HyDE、embedding，减少不必要的外部模型预处理调用。
- DeepSeek：核心纪要生成、复杂自然语言理解、RAG 最终答案。
- Fake adapters：无网络、无 Key 的可复现演示与测试，UI/API 明确标记为 `fake`。

该设计减少部分内容外发，但不等于完整隐私合规。项目尚无完整权限、租户隔离、脱敏体系或合规审计。

## Cache Strategy

Supabase `result_cache` 只缓存 `generate_minutes` 和 RAG final answer。Cache key 包含 operation、完整模型输入（含真实 chunk ID）、模型名和 prompt version。命中 JSON 会再次经过 Pydantic validation。缓存读写失败记录 warning 并回退到模型调用，不影响会议核心链；FieldPatch、确认、commit 与版本创建绝不缓存。

## 本地启动（FakeMode）

要求 Python 3.11+、Node.js 20+。在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python -m uvicorn app.main:app --app-dir backend --reload
```

另开终端：

```powershell
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173>；API 文档为 <http://127.0.0.1:8000/docs>。默认不需要 DeepSeek、Ollama、Supabase 或 STT Key。

## RealMode 配置

### DeepSeek

```dotenv
USE_FAKE_MODEL=false
DEEPSEEK_API_KEY=你的密钥
MODEL_NAME=deepseek-chat
BASE_URL=https://api.deepseek.com
```

### OpenAI-compatible STT

```dotenv
USE_FAKE_TRANSCRIPTION=false
STT_API_KEY=你的密钥
STT_BASE_URL=https://你的服务地址/v1
STT_MODEL=模型名
```

音频只在请求内存中处理，数据库保存 transcript，不永久保存原始音频。

### Ollama

先安装并启动 Ollama，再拉取一个文本模型和 embedding 模型（模型名以你的本地安装为准）：

```dotenv
OLLAMA_ENABLED=true
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=你的本地文本模型
OLLAMA_EMBED_MODEL=你的本地向量模型
```

### Supabase

1. 创建 Supabase 项目。
2. 在 SQL Editor 执行 `supabase/setup.sql`。
3. 后端环境变量填写：

```dotenv
SUPABASE_ENABLED=true
SUPABASE_URL=https://你的项目.supabase.co
SUPABASE_KEY=仅后端使用的密钥
RAG_ENABLED=true
RAG_TOP_K=5
RRF_K=60
```

不要把 Supabase key 暴露给 Vite 前端。SQL 使用无固定维度的 `vector`，以兼容不同 Ollama embedding 模型；生产环境确定模型后可固定维度并建立 HNSW/IVFFlat 索引。

## API

- `POST /api/meetings`：文本创建，支持校区、部门、会议类型
- `POST /api/meetings/upload`：TXT/MD/DOCX
- `POST /api/meetings/audio`：音频转写并生成纪要草稿
- `POST /api/meetings/{id}/generate`：运行 LangGraph
- `POST /api/meetings/{id}/review`：补丁、确认、追加版本；确认后幂等索引 SourceChunk
- `POST /api/sessions/{id}/messages`：当前会议问答/确定性修订
- `GET /api/action-items/overview`：多会议状态聚合
- `POST /api/weekly-reports`：周报草稿
- `POST /api/knowledge/query`：只读历史会议/SOP RAG
- `GET /api/health`、`GET /api/ready`：存活与就绪检查

## Demo

`fixtures/knowledge/` 与 `fixtures/historical_meetings/` 全部是 synthetic demo data。

1. 创建“校区A / 教研会”，上传 `fixtures/historical_meetings/校区A教研会.txt`，生成并补齐待确认字段后确认。
2. 创建“校区B / 教务会”，上传 `fixtures/historical_meetings/校区B教务会.txt`，生成并确认。
3. 在右侧“历史知识”问：“之前哪个校区讨论过调课？”
4. 再问：“演示制度中调课最终由谁确认？”；结果应引用 `排课调课流程`。

离线 FakeModel 的提取质量不代表真实模型；其作用是验证工作流、状态、索引和引用边界。

## 数据库与部署

默认核心数据库是项目根目录 `office_agent.db`。启动时自动新建表，并对 SQLite 启用外键、WAL 和 15 秒 busy timeout。旧库只新增 `meeting_contexts` 表。正式环境建议 PostgreSQL + Alembic；示例：

```dotenv
DATABASE_URL=postgresql+psycopg://office_agent:password@127.0.0.1:5432/office_agent
```

Docker：

```powershell
docker compose up --build -d
docker compose ps
docker compose logs -f backend
```

浏览器打开 <http://localhost:8080>。`docker compose down` 保留数据卷；只有明确要删除本地容器数据时才使用 `docker compose down -v`。

### Render Blueprint

仓库根目录的 `render.yaml` 会创建两个服务：Docker 后端 `office-agent-api` 和静态前端 `office-agent-web`。在 Render 选择 **New → Blueprint**，连接本仓库并应用 Blueprint。首次部署默认使用 Fake Model、Fake STT、进程内 RAG，因而不填写密钥也能验证页面和核心工作流。

部署成功后依次检查：

```text
https://office-agent-api.onrender.com/api/health
https://office-agent-api.onrender.com/api/ready
https://office-agent-web.onrender.com
```

要启用真实 DeepSeek，在 API 服务的 Environment 中填写 `DEEPSEEK_API_KEY`，并把 `USE_FAKE_MODEL` 改成 `false`。要启用真实 STT，填写 `STT_API_KEY`、`STT_BASE_URL`、`STT_MODEL`，并把 `USE_FAKE_TRANSCRIPTION` 改成 `false`。修改后执行一次 Manual Deploy。

Render 无法访问开发电脑上的 `127.0.0.1:11434`。只有另行部署了可由 Render 访问的 Ollama 服务时，才能填写 `OLLAMA_BASE_URL`、模型名并启用 `OLLAMA_ENABLED`。启用 Supabase 前先执行 `supabase/setup.sql`，再填写 `SUPABASE_URL`、后端专用 Key，并启用 `SUPABASE_ENABLED`；向量维度必须与实际 embedding 模型一致。

当前 Blueprint 使用本地 SQLite。Render 默认文件系统是临时的，免费实例重启、休眠或重新部署后数据可能丢失。正式使用应改用 PostgreSQL，或者在付费 Web Service 上把持久磁盘挂载到例如 `/app/data`，并将 `DATABASE_URL` 改为 `sqlite:////app/data/office_agent.db`。平台密钥只能通过 Environment 注入；`.env` 已被 `.gitignore` 和 `.dockerignore` 排除，不能提交或截图传播。

## 检查

```powershell
.\.venv\Scripts\python -m pytest backend\tests
cd frontend
npm run lint
npm run build
cd ..
docker compose config --quiet
```

不要粘贴普通 `docker compose config` 的完整输出，它会展开环境变量。

## Demo Data Disclaimer

所有校区、会议、人员和 SOP 都是虚构演示数据，不声称来自真实客户，不代表真实机构制度或实际使用效果。

## 已知限制

- 没有登录、租户隔离、细粒度权限、完整脱敏、合规审计或限流，不应直接作为公网多人生产系统。
- 数据库仍使用启动时自动建表；正式发布需要 Alembic、备份与恢复演练。
- 轻量中文 BM25 tokenizer 不等同于专业分词；没有高级 reranker。
- Supabase sidecar 的 RLS、固定向量维度与向量索引需按实际部署模型加固。
- 没有 speaker diarization、实时流式 STT、声纹识别、PDF、OCR 或知识图谱。
- Supabase/Ollama/DeepSeek 的真实链路需要用户自己的服务和密钥；pytest 不依赖网络。
