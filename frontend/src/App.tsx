import { useEffect, useRef, useState } from "react"
import type { FormEvent } from "react"
import "./App.css"

const API = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000/api"
type MeetingType = "teaching_research" | "academic_operations" | "branch_management" | "generic"
type MeetingItem = { id: string; title: string; meeting_date: string | null; created_at: string; status: string; branch_name?: string | null; department?: string | null; meeting_type?: MeetingType }
type ActionItem = { id: string; task: string; owner: string; due_date: string | null; status: "unknown" | "todo" | "in_progress" | "done"; source_refs: string[] }
type Minutes = {
  summary: string
  decisions: { text: string; source_refs: string[] }[]
  action_items: ActionItem[]
  open_questions: string[]
  review_status?: "pending" | "confirmed"
  review_required_fields?: string[]
}
type ChatMessage = { id?: string; role: "user" | "assistant"; content: string }
type KnowledgeSource = { source_id: string; text: string; score: number; metadata: Record<string, unknown> }
type KnowledgeResponse = { answer: string; knowledge_refs: string[]; sources: KnowledgeSource[]; model_mode: string; cache_hit: boolean }
type Meeting = MeetingItem & {
  session_id: string
  source_type: string
  raw_text: string
  current_result: Minutes | null
  version_no: number | null
  model_mode: "fake" | "deepseek"
  needs_human_review?: boolean
  messages?: ChatMessage[]
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, options)
  const body = await response.json()
  if (!response.ok) throw new Error(body.message || "请求失败")
  return body
}

function App() {
  const [meetings, setMeetings] = useState<MeetingItem[]>([])
  const [active, setActive] = useState<Meeting | null>(null)
  const [tab, setTab] = useState<"result" | "source" | "weekly">("result")
  const [text, setText] = useState("")
  const [title, setTitle] = useState("")
  const [branchName, setBranchName] = useState("")
  const [department, setDepartment] = useState("")
  const [meetingType, setMeetingType] = useState<MeetingType>("generic")
  const [message, setMessage] = useState("")
  const [chatMode, setChatMode] = useState<"current" | "knowledge">("current")
  const [knowledgeAnswer, setKnowledgeAnswer] = useState<KnowledgeResponse | null>(null)
  const [weekly, setWeekly] = useState<Record<string, string[]> | null>(null)
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [notice, setNotice] = useState("")
  const [loading, setLoading] = useState(false)
  const chatEnd = useRef<HTMLDivElement | null>(null)

  const refreshList = async () => setMeetings(await request<MeetingItem[]>("/meetings"))
  useEffect(() => { refreshList().catch((error) => setNotice(error.message)) }, [])
  useEffect(() => { chatEnd.current?.scrollIntoView({ block: "end" }) }, [active?.messages, loading])

  const openMeeting = async (id: string, resetTab = true) => {
    try {
      setLoading(true)
      setActive(await request<Meeting>(`/meetings/${id}`))
      if (resetTab) setTab("result")
      setNotice("")
    } catch (error) { setNotice((error as Error).message) }
    finally { setLoading(false) }
  }

  const createFromText = async (event: FormEvent) => {
    event.preventDefault()
    try {
      setLoading(true)
      const created = await request<Meeting>("/meetings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, title: title || null, branch_name: branchName || null, department: department || null, meeting_type: meetingType }),
      })
      setText(""); setTitle(""); setActive(created); setTab("result")
      await refreshList(); setNotice("会议已保存。可先提问，或生成纪要后再修改。")
    } catch (error) { setNotice((error as Error).message) }
    finally { setLoading(false) }
  }

  const uploadFile = async (file: File) => {
    const form = new FormData()
    form.append("file", file)
    if (title) form.append("title", title)
    if (branchName) form.append("branch_name", branchName)
    if (department) form.append("department", department)
    form.append("meeting_type", meetingType)
    try {
      setLoading(true)
      const created = await request<Meeting>("/meetings/upload", { method: "POST", body: form })
      setActive(created); setTab("result"); await refreshList(); setNotice("文件已解析并保存。")
    } catch (error) { setNotice((error as Error).message) }
    finally { setLoading(false) }
  }

  const uploadAudio = async (file: File) => {
    const form = new FormData()
    form.append("file", file)
    if (title) form.append("title", title)
    if (branchName) form.append("branch_name", branchName)
    if (department) form.append("department", department)
    form.append("meeting_type", meetingType)
    try {
      setLoading(true)
      const created = await request<Meeting>("/meetings/audio", { method: "POST", body: form })
      setActive(created); setTab("result"); await refreshList()
      setNotice("音频已转写并生成纪要草稿，请检查待确认字段。")
    } catch (error) { setNotice((error as Error).message) }
    finally { setLoading(false) }
  }

  const generate = async () => {
    if (!active) return
    try {
      setLoading(true)
      await request(`/meetings/${active.id}/generate`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: "minutes" }),
      })
      await openMeeting(active.id, false)
      setNotice("已生成新的纪要草稿。")
    } catch (error) { setNotice((error as Error).message); setLoading(false) }
  }

  const sendMessage = async (event: FormEvent) => {
    event.preventDefault()
    if (!message.trim()) return
    if (chatMode === "knowledge") {
      const query = message.trim()
      try {
        setLoading(true)
        setMessage("")
        const result = await request<KnowledgeResponse>("/knowledge/query", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query, branch_name: null, department: null, meeting_type: null }),
        })
        setKnowledgeAnswer(result)
        setNotice(result.cache_hit ? "历史知识回答命中缓存。" : "历史知识检索完成。")
      } catch (error) { setNotice((error as Error).message) }
      finally { setLoading(false) }
      return
    }
    if (!active) return
    const content = message.trim()
    try {
      setLoading(true)
      setMessage("")
      const result = await request<{
        assistant_message: string
        intent: string
        result: Minutes | null
        version_no: number | null
      }>(`/sessions/${active.session_id}/messages`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, meeting_id: active.id }),
      })
      setActive((current) => current && {
        ...current,
        current_result: result.result ?? current.current_result,
        version_no: result.version_no ?? current.version_no,
        messages: [
          ...(current.messages || []),
          { role: "user", content },
          { role: "assistant", content: result.assistant_message },
        ],
      })
      setNotice(result.intent === "revision" ? result.assistant_message : "")
    } catch (error) { setNotice((error as Error).message) }
    finally { setLoading(false) }
  }

  const toggleTask = async (item: ActionItem, done: boolean) => {
    if (!active) return
    try {
      const response = await request<{ result: Minutes; version_no: number }>(
        `/meetings/${active.id}/action-items/${item.id}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ done }),
        },
      )
      setActive((current) => current && {
        ...current,
        current_result: response.result,
        version_no: response.version_no,
      })
    } catch (error) { setNotice((error as Error).message) }
  }

  const updateTaskStatus = async (item: ActionItem, status: ActionItem["status"]) => {
    if (!active) return
    try {
      const response = await request<{ result: Minutes; version_no: number }>(
        `/meetings/${active.id}/action-items/${item.id}/status`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }) },
      )
      setActive((current) => current && { ...current, current_result: response.result, version_no: response.version_no, needs_human_review: true })
    } catch (error) { setNotice((error as Error).message) }
  }

  const confirmReview = async () => {
    if (!active) return
    try {
      setLoading(true)
      const response = await request<{ result: Minutes; version_no: number; needs_human_review: boolean }>(
        `/meetings/${active.id}/review`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true, patches: [] }) },
      )
      setActive((current) => current && { ...current, current_result: response.result, version_no: response.version_no, needs_human_review: response.needs_human_review })
      setNotice("纪要已由人工确认，并保存为新版本。")
    } catch (error) { setNotice((error as Error).message) }
    finally { setLoading(false) }
  }

  const exportTasks = async () => {
    const items = active?.current_result?.action_items || []
    if (!items.length) return setNotice("当前没有可导出的待办。")
    const lines = [
      `# ${active?.title || "待办"}`,
      "",
      ...items.map((item) => {
        const mark = item.status === "done" ? "x" : " "
        return `- [${mark}] ${item.task}（${item.owner}，${item.due_date || "日期待确认"}）`
      }),
    ]
    const fileText = lines.join("\n")
    const blob = new Blob([fileText], { type: "text/markdown;charset=utf-8" })
    const url = URL.createObjectURL(blob)
    const link = document.createElement("a")
    link.href = url
    link.download = `${active?.title || "todos"}.md`
    link.click()
    URL.revokeObjectURL(url)
    try {
      await navigator.clipboard.writeText(fileText)
      setNotice("待办已导出，并复制到剪贴板。")
    } catch {
      setNotice("待办文件已下载。")
    }
  }

  const createWeekly = async () => {
    if (!selectedIds.length) return setNotice("请至少选择一场会议。")
    const today = new Date().toISOString().slice(0, 10)
    try {
      setLoading(true)
      const response = await request<{ result: Record<string, string[]> }>("/weekly-reports", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ meeting_ids: selectedIds, period_start: today, period_end: today }),
      })
      setWeekly(response.result); setTab("weekly"); setNotice("周报草稿已生成。")
    } catch (error) { setNotice((error as Error).message) }
    finally { setLoading(false) }
  }

  const minutes = active?.current_result
  const chatMessages = active?.messages || []
  return (
    <main className="app-shell">
      <header>
        <div><span className="eyebrow">MEETING EXECUTION WORKSPACE</span><h1>Meeting Execution Agent</h1></div>
        <div className="header-status"><span className="status-dot" /> {active?.model_mode === "deepseek" ? "DeepSeek" : "离线演示"}</div>
      </header>
      {notice && <div className="notice">{notice}</div>}
      <section className="workspace">
        <aside className="sidebar">
          <h2>新建会议</h2>
          <form onSubmit={createFromText} className="new-meeting">
            <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="会议标题（可选）" />
            <div className="context-fields">
              <input value={branchName} onChange={(e) => setBranchName(e.target.value)} placeholder="校区（可选）" />
              <input value={department} onChange={(e) => setDepartment(e.target.value)} placeholder="部门（可选）" />
            </div>
            <select value={meetingType} onChange={(e) => setMeetingType(e.target.value as MeetingType)}>
              <option value="generic">通用会议</option>
              <option value="teaching_research">教研会</option>
              <option value="academic_operations">教务会</option>
              <option value="branch_management">校区 / 管理例会</option>
            </select>
            <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="粘贴会议记录…" />
            <button disabled={loading || !text.trim()}>保存文本</button>
            <label className="file-button">上传 .txt / .md / .docx
              <input type="file" accept=".txt,.md,.docx" onChange={(e) => e.target.files?.[0] && uploadFile(e.target.files[0])} />
            </label>
            <label className="file-button audio-button">上传会议音频
              <input type="file" accept=".mp3,.wav,.m4a,.mp4,.webm,.ogg,audio/*" onChange={(e) => e.target.files?.[0] && uploadAudio(e.target.files[0])} />
            </label>
          </form>
          <div className="meeting-list">
            {meetings.map((meeting) => <button className={active?.id === meeting.id ? "meeting active" : "meeting"} key={meeting.id} onClick={() => openMeeting(meeting.id)}>
              <strong>{meeting.title}</strong><small>{[meeting.branch_name, meetingTypeLabels[meeting.meeting_type || "generic"], meeting.meeting_date || new Date(meeting.created_at).toLocaleDateString()].filter(Boolean).join(" · ")}</small>
            </button>)}
            {!meetings.length && <p className="empty">还没有会议材料</p>}
          </div>
        </aside>

        <section className="content">
          <div className="content-head"><div><span className="eyebrow">CURRENT MEETING</span><h2>{active?.title || "选择或新建会议"}</h2></div>
            {active && <button onClick={generate} disabled={loading}>生成纪要</button>}</div>
          <nav className="tabs">
            <button className={tab === "result" ? "active" : ""} onClick={() => setTab("result")}>结构化结果</button>
            <button className={tab === "source" ? "active" : ""} onClick={() => setTab("source")}>{active?.source_type === "audio" ? "Transcript" : "原始材料"}</button>
            <button className={tab === "weekly" ? "active" : ""} onClick={() => setTab("weekly")}>周报草稿</button>
          </nav>
          <div className="document">
            {!active && <Empty title="从左侧开始" detail="粘贴文字或上传会议文件" />}
            {active && tab === "source" && <pre className="source">{active.raw_text}</pre>}
            {active && tab === "result" && !minutes && <Empty title="尚未生成纪要" detail="原文已持久化。可先在右侧提问，或点击“生成纪要”" />}
            {active && tab === "result" && minutes && <>
              <div className="draft-line"><span>{minutes.review_status === "confirmed" ? "已确认" : "待人工确认"}</span><span>版本 v{active.version_no}</span></div>
              {minutes.review_status !== "confirmed" && <div className="review-panel">
                <div><b>人工确认</b><span>请核对决议、负责人、截止日期和状态。任何确认都会追加新版本。</span></div>
                {!!minutes.review_required_fields?.length && <small>尚待补充：{minutes.review_required_fields.join("、")}</small>}
                <button type="button" onClick={confirmReview} disabled={loading || !!minutes.review_required_fields?.length}>确认当前纪要</button>
              </div>}
              <article><h3>摘要</h3><p>{minutes.summary}</p></article>
              <article><h3>决议</h3>{minutes.decisions.map((item, i) => <p key={i}>• {item.text} <Ref ids={item.source_refs} /></p>)}{!minutes.decisions.length && <p className="muted">未发现明确决议</p>}</article>
              <article>
                <div className="task-toolbar">
                  <h3>待办事项</h3>
                  <button type="button" onClick={exportTasks} disabled={!minutes.action_items.length}>导出待办</button>
                </div>
                {minutes.action_items.map((item) => (
                  <label className={`${item.status === "done" ? "task done" : "task"} ${(item.owner === "待确认" || !item.due_date || item.status === "unknown") ? "needs-review" : ""}`} key={item.id}>
                    <input type="checkbox" checked={item.status === "done"} onChange={(e) => toggleTask(item, e.target.checked)} />
                    <div><b>{item.task}</b><small>{item.owner} · {item.due_date || "日期待确认"}</small>
                      <select value={item.status} onChange={(e) => updateTaskStatus(item, e.target.value as ActionItem["status"])}>
                        <option value="unknown">待确认</option><option value="todo">待办</option><option value="in_progress">进行中</option><option value="done">已完成</option>
                      </select>
                    </div>
                    <Ref ids={item.source_refs} />
                  </label>
                ))}
                {!minutes.action_items.length && <p className="muted">未提取到待办</p>}
              </article>
              {!!minutes.open_questions.length && <article><h3>待确认</h3>{minutes.open_questions.map((item) => <p key={item}>• {item}</p>)}</article>}
            </>}
            {tab === "weekly" && <div>
              <article className="weekly-select"><h3>选择来源会议</h3>{meetings.map((meeting) => <label key={meeting.id}>
                <input type="checkbox" checked={selectedIds.includes(meeting.id)} onChange={(e) => setSelectedIds(e.target.checked ? [...selectedIds, meeting.id] : selectedIds.filter((id) => id !== meeting.id))} />{meeting.title}
              </label>)}<button onClick={createWeekly} disabled={loading}>生成周报</button></article>
              {weekly && Object.entries(weekly).map(([key, items]) => Array.isArray(items) && key !== "source_meeting_ids" && <article key={key}><h3>{weeklyLabels[key] || key}</h3>{items.map((item) => <p key={item}>• {item}</p>)}</article>)}
            </div>}
          </div>
        </section>

        <aside className="chat">
          <span className="eyebrow">AGENT CHAT</span>
          <div className="chat-tabs">
            <button className={chatMode === "current" ? "active" : ""} onClick={() => setChatMode("current")}>当前会议</button>
            <button className={chatMode === "knowledge" ? "active" : ""} onClick={() => setChatMode("knowledge")}>历史知识</button>
          </div>
          <p className="chat-lead">{chatMode === "current" ? "仅依据当前会议提问或定向修改。" : "只读查询已确认会议与演示 SOP。"}</p>
          <div className="chat-log">
            {chatMode === "current" && !active && <p className="muted">先选择或新建一场会议。</p>}
            {chatMode === "current" && active && !chatMessages.length && <p className="muted">还没有对话。请在输入框中输入问题或修改请求。</p>}
            {chatMode === "current" && chatMessages.map((item, index) => (
              <div className={`bubble ${item.role}`} key={item.id || `${item.role}-${index}`}>{item.content}</div>
            ))}
            {chatMode === "knowledge" && !knowledgeAnswer && <p className="muted">例如：演示制度中调课最终由谁确认？</p>}
            {chatMode === "knowledge" && knowledgeAnswer && <>
              <div className="bubble assistant">{knowledgeAnswer.answer}</div>
              <div className="knowledge-sources">
                {knowledgeAnswer.sources.map((source) => <details key={source.source_id}>
                  <summary>{String(source.metadata.title || source.source_id)}</summary>
                  <p>{source.text}</p>
                </details>)}
              </div>
            </>}
            {loading && <div className="bubble assistant">正在处理…</div>}
            <div ref={chatEnd} />
          </div>
          <form onSubmit={sendMessage}>
            <textarea value={message} onChange={(e) => setMessage(e.target.value)} placeholder={chatMode === "current" ? "询问会议内容，或提出修改…" : "查询历史会议或演示 SOP…"} />
            <button disabled={loading || (chatMode === "current" && !active) || !message.trim()}>{loading ? "处理中…" : "发送"}</button>
          </form>
        </aside>
      </section>
    </main>
  )
}

function Ref({ ids }: { ids: string[] }) { return <span className="ref" title={ids.join(", ")}>来源 {ids.map((id) => id.slice(0, 4)).join(",")}</span> }
function Empty({ title, detail }: { title: string; detail: string }) { return <div className="empty-state"><b>{title}</b><span>{detail}</span></div> }
const weeklyLabels: Record<string, string> = { completed: "本周完成", in_progress: "进行中", risks_and_pending: "风险与待确认", next_week: "下周计划", overdue: "逾期事项" }
const meetingTypeLabels: Record<MeetingType, string> = { teaching_research: "教研会", academic_operations: "教务会", branch_management: "管理例会", generic: "通用会议" }
export default App
