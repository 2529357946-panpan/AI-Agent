# 办公 Agent

本地运行的单智能体办公助手。它可以接收粘贴文字或 `.txt`、`.md`、`.docx` 文件，生成带来源引用的会议纪要和待办，通过对话定向修改某一项并保留历史版本，还能从多场会议生成周报草稿。

默认使用明确标记的离线演示模式，不需要 API Key。配置 DeepSeek 后可切换到真实模型。

## 已实现

- FastAPI API、统一错误响应与请求 ID
- SQLite 持久化会议原文、来源片段、会话、消息、结果版本和周报来源
- 文本、Markdown 和 DOCX 解析；10 MB 文件及 50,000 字符限制
- DeepSeek OpenAI 兼容适配器，JSON/Pydantic 结构校验和来源 ID 校验
- `FakeModelClient` 离线演示（界面和响应会标明 `fake` / “离线演示”）
- 只修改目标字段的待办补丁、非法日期拒绝、历史版本追加
- `unknown` 不计入“已完成”的跨会议周报
- React + TypeScript 三栏界面、加载/空态/错误提示、手机简化布局
- pytest 核心链路测试和两份演示材料

## 环境要求

- Python 3.11+
- Node.js 20+

## 1. 后端启动

在项目根目录 `office-agent` 执行（PowerShell）：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
Copy-Item .env.example .env
cd backend
uvicorn app.main:app --reload
```

后端地址为 <http://127.0.0.1:8000>，接口文档为 <http://127.0.0.1:8000/docs>。

如果 PowerShell 禁止激活脚本，可以直接执行：

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --app-dir backend --reload
```

## 2. 前端启动

另开一个 PowerShell：

```powershell
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173>。

## 需要你填写：DeepSeek API Key

离线演示不需要填写任何 Key。要接入真实模型：

1. 将 `.env.example` 复制为项目根目录的 `.env`。
2. 修改为：

```dotenv
USE_FAKE_MODEL=false
DEEPSEEK_API_KEY=在这里填写你的真实_Key
MODEL_NAME=deepseek-chat
BASE_URL=https://api.deepseek.com
```

3. 重启后端，在 `/api/health` 确认 `model_mode` 为 `deepseek` 且 `model_configured` 为 `true`。

`.env` 已加入 `.gitignore`，不要把真实 Key 粘贴到源码、聊天截图或提交记录中。若你的 DeepSeek 账户支持的模型名或接口地址不同，只修改 `MODEL_NAME`、`BASE_URL`，无需改业务代码。

前端通常无需配置。只有后端地址不是默认值时，复制 `frontend/.env.example` 为 `frontend/.env` 并修改 `VITE_API_BASE_URL`。

## 演示步骤

1. 启动前后端，打开网页。
2. 上传 `fixtures/meeting_a.txt`，点击“生成纪要”。
3. 查看摘要、决议、待办和每项来源标识。
4. 在右侧输入“第二项负责人改成小王”，确认只有第二项负责人变化且版本号增加。
5. 输入“第二项截止日期改成 2026-10-09”；再尝试 `2026-10-99`，后者会被拒绝且不产生版本。
6. 上传并生成 `fixtures/meeting_b.txt`。
7. 打开“周报草稿”，勾选两场会议并生成；未知状态只会进入“风险与待确认”。
8. 重启后端，再次打开会议，确认原文与当前版本仍存在。

## 检查命令

```powershell
# 项目根目录
.\.venv\Scripts\python -m pytest backend\tests

cd frontend
npm run lint
npm run build
```

也可以在项目根目录直接执行 `python -m pytest`；`pytest.ini` 已配置后端模块路径。

## 数据库连接与就绪检查

默认数据库是项目根目录的 SQLite 文件 `office_agent.db`。服务启动时自动建表，并启用外键、WAL 和 15 秒 busy timeout，适合本地单机演示。容器发布时数据库文件位于具名卷 `office-agent-data`，重建容器不会删除数据。

如需使用 PostgreSQL，在 `.env` 中设置：

```dotenv
DATABASE_URL=postgresql+psycopg://office_agent:password@127.0.0.1:5432/office_agent
```

`GET /api/health` 是进程存活检查；`GET /api/ready` 会实际执行数据库查询，并在真实模型模式下检查 Key 是否已配置。发布平台应使用 `/api/ready` 作为 readiness probe。

## Docker 发布

先复制配置，再构建并启动：

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
docker compose logs -f backend
```

浏览器打开 <http://localhost:8080>。停止服务使用 `docker compose down`；该命令保留数据库卷。只有明确需要连同本地容器数据一起删除时才使用 `docker compose down -v`。

真实模型发布前，把 `.env` 中的 `USE_FAKE_MODEL` 改为 `false` 并填写 `DEEPSEEK_API_KEY`。生产环境还应把 `CORS_ORIGINS` 设置为实际站点来源，反向代理启用 HTTPS，并由部署平台注入密钥，不要把 `.env` 打进镜像。

发布前检查清单：

```powershell
python -m pytest
cd frontend
npm.cmd run lint
npm.cmd run build
cd ..
docker compose config --quiet
```

不要把普通 `docker compose config` 的完整输出粘贴到日志或工单中，因为它会展开 `.env` 中的密钥。只做语法检查时使用上面的 `--quiet`。

## GitHub 与 Render 自动部署

项目已包含 `.github/workflows/ci.yml` 和 `render.yaml`。首次发布时：

```powershell
git init
git add .
git commit -m "Initial office agent release"
git branch -M main
git remote add origin https://github.com/<你的账号>/<你的仓库>.git
git push -u origin main
```

然后在 Render 控制台选择 **New → Blueprint**，连接这个 GitHub 仓库并应用 `render.yaml`。Blueprint 默认先使用 `USE_FAKE_MODEL=true`，不填写模型 Key 也能完成首次部署和链路验证。部署完成后先检查 `https://office-agent-api.onrender.com/api/ready`，再打开前端站点；确认正常后，在 API 服务环境变量中填写 `DEEPSEEK_API_KEY`，将 `USE_FAKE_MODEL` 改为 `false`，再手动 Deploy。不要把密钥写进 GitHub、`render.yaml` 或工作流文件。

当前 Blueprint 为兼容免费方案使用本地 SQLite 临时文件；Render 免费服务重启或重新部署时文件可能丢失。正式使用请把 `DATABASE_URL` 换成 Render Postgres，或升级 API 服务后再添加持久磁盘，并配置定期备份、登录鉴权和限流。

## 数据与安全说明

- API Key 只从后端环境变量读取，健康检查不会返回 Key。
- 上传文件在内存中解析，文件名不会拼接成本地路径。
- 模型结果保存前会经过 schema、状态枚举、日期和来源引用校验。
- 生成或修订失败时事务回滚，不写入半成品版本。
- 当前是单用户本地演示，不具备登录、权限或租户隔离，不应直接作为公网多人服务。

## 已知限制与后续工作

- 离线客户端使用确定性规则，只用于无网络演示，提取质量不代表真实模型。
- 当前修订支持待办的负责人、截止日期和状态；自由编辑摘要及更广泛的字段补丁可继续扩展。
- 当前周报时间段由 API 保存，但界面暂使用当天日期；可增加日期选择器和周报历史页。
- 尚未实现 PDF、音频转写、文档导出、全文搜索、真正的 RAG、登录和多人权限。
- 数据库当前采用自动建表；正式部署需要 Alembic 迁移、备份策略、队列、速率限制和可观测性。
