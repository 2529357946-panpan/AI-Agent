import { useEffect, useRef, useState } from "react"
import type { FormEvent } from "react"
import "./App.css"

const API = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000/api"
type MeetingItem = { id: string; title: string; meeting_date: string | null; created_at: string; status: string }
type ActionItem = { id: string; task: string; owner: string; due_date: string | null; status: string; source_refs: string[] }
type Minutes = { summary: string; decisions: { text: string; source_refs: string[] }[]; action_items: ActionItem[]; open_questions: string[] }
type ChatMessage = { id?: string; role: "user" | "assistant"; content: string }
type Meeting = MeetingItem & {
  session_id: string
  raw_text: string
  current_result: Minutes | null
  version_no: number | null
  model_mode: "fake" | "deepseek"
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
  const [message, setMessage] = useState("")
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
        body: JSON.stringify({ text, title: title || null }),
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
    try {
      setLoading(true)
      const created = await request<Meeting>("/meetings/upload", { method: "POST", body: form })
      setActive(created); setTab("result"); await refreshList(); setNotice("文件已解析并保存。")
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
    if (!active || !message.trim()) return
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
        <div><span className="eyebrow">LOCAL WORKSPACE</span><h1>办公 Agent</h1></div>
        <div className="header-status"><span className="status-dot" /> {active?.model_mode === "deepseek" ? "DeepSeek" : "离线演示"}</div>
      </header>
      {notice && <div className="notice">{notice}</div>}
      <section className="workspace">
        <aside className="sidebar">
          <h2>会议</h2>
          <form onSubmit={createFromText} className="new-meeting">
            <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="会议标题（可选）" />
            <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="粘贴会议记录…" />
            <button disabled={loading || !text.trim()}>保存文本</button>
            <label className="file-button">上传 .txt / .md / .docx
              <input type="file" accept=".txt,.md,.docx" onChange={(e) => e.target.files?.[0] && uploadFile(e.target.files[0])} />
            </label>
          </form>
          <div className="meeting-list">
            {meetings.map((meeting) => <button className={active?.id === meeting.id ? "meeting active" : "meeting"} key={meeting.id} onClick={() => openMeeting(meeting.id)}>
              <strong>{meeting.title}</strong><small>{meeting.meeting_date || new Date(meeting.created_at).toLocaleDateString()}</small>
            </button>)}
            {!meetings.length && <p className="empty">还没有会议材料</p>}
          </div>
        </aside>

        <section className="content">
          <div className="content-head"><div><span className="eyebrow">CURRENT MEETING</span><h2>{active?.title || "选择或新建会议"}</h2></div>
            {active && <button onClick={generate} disabled={loading}>生成纪要</button>}</div>
          <nav className="tabs">
            <button className={tab === "result" ? "active" : ""} onClick={() => setTab("result")}>结构化结果</button>
            <button className={tab === "source" ? "active" : ""} onClick={() => setTab("source")}>原始材料</button>
            <button className={tab === "weekly" ? "active" : ""} onClick={() => setTab("weekly")}>周报草稿</button>
          </nav>
          <div className="document">
            {!active && <Empty title="从左侧开始" detail="粘贴文字或上传会议文件" />}
            {active && tab === "source" && <pre className="source">{active.raw_text}</pre>}
            {active && tab === "result" && !minutes && <Empty title="尚未生成纪要" detail="原文已持久化。可先在右侧提问，或点击“生成纪要”" />}
            {active && tab === "result" && minutes && <>
              <div className="draft-line"><span>草稿</span><span>版本 v{active.version_no}</span></div>
              <article><h3>摘要</h3><p>{minutes.summary}</p></article>
              <article><h3>决议</h3>{minutes.decisions.map((item, i) => <p key={i}>• {item.text} <Ref ids={item.source_refs} /></p>)}{!minutes.decisions.length && <p className="muted">未发现明确决议</p>}</article>
              <article>
                <div className="task-toolbar">
                  <h3>待办事项</h3>
                  <button type="button" onClick={exportTasks} disabled={!minutes.action_items.length}>导出待办</button>
                </div>
                {minutes.action_items.map((item) => (
                  <label className={item.status === "done" ? "task done" : "task"} key={item.id}>
                    <input type="checkbox" checked={item.status === "done"} onChange={(e) => toggleTask(item, e.target.checked)} />
                    <div><b>{item.task}</b><small>{item.owner} · {item.due_date || "日期待确认"} · {item.status === "done" ? "已完成" : "未完成"}</small></div>
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
          <span className="eyebrow">MEETING CHAT</span>
          <h2>提问</h2>
          <p className="chat-lead">对会议内容进行提问，或提出修改。</p>
          <div className="chat-log">
            {!active && <p className="muted">先选择或新建一场会议。</p>}
            {active && !chatMessages.length && <p className="muted">还没有对话。请在输入框中输入问题或修改请求。</p>}
            {chatMessages.map((item, index) => (
              <div className={`bubble ${item.role}`} key={item.id || `${item.role}-${index}`}>{item.content}</div>
            ))}
            {loading && <div className="bubble assistant">正在处理…</div>}
            <div ref={chatEnd} />
          </div>
          <form onSubmit={sendMessage}>
            <textarea value={message} onChange={(e) => setMessage(e.target.value)} placeholder="询问会议内容，或提出修改…" />
            <button disabled={loading || !active || !message.trim()}>{loading ? "处理中…" : "发送"}</button>
          </form>
        </aside>
      </section>
    </main>
  )
}

function Ref({ ids }: { ids: string[] }) { return <span className="ref" title={ids.join(", ")}>来源 {ids.map((id) => id.slice(0, 4)).join(",")}</span> }
function Empty({ title, detail }: { title: string; detail: string }) { return <div className="empty-state"><b>{title}</b><span>{detail}</span></div> }
const weeklyLabels: Record<string, string> = { completed: "本周完成", in_progress: "进行中", risks_and_pending: "风险与待确认", next_week: "下周计划" }
export default App
