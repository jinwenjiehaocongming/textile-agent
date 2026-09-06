import { useCallback, useEffect, useRef, useState } from 'react'
import {
  streamChat, fetchHistory, fetchMe, logout,
  fetchSessions, createSession, deleteSession, uuid,
} from './api'
import LoginPage from './components/LoginPage'
import { MessageBubble } from './components/MessageBubble'
import { TypingIndicator } from './components/TypingIndicator'
import ApprovalPanel from './components/ApprovalPanel'
import OrderList from './components/OrderList'

// 快捷建议（空状态展示）
const SUGGESTIONS = [
  'T400 黑色多少钱',
  '羽绒服推荐什么面料',
  '你们有哪些涤塔夫现货',
  '我要确认一下下单',
]

// 打字机：每次 tick 揭示的字符数（用随机 1-3 更自然）
const TYPE_TICK_MS = 24

function BrandMark({ size = 'md' }) {
  const box = size === 'lg' ? 'h-12 w-12 rounded-2xl' : 'h-10 w-10 rounded-xl'
  const icon = size === 'lg' ? 26 : 20
  return (
    <div className={`flex items-center justify-center border border-brand-300/25 bg-gradient-to-b from-brand-400/25 to-brand-500/15 text-brand-50 shadow-panel-sm backdrop-blur-md ${box}`}>
      <svg width={icon} height={icon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 2a10 10 0 0 1 10 10c0 5-4 8-10 8-1.2 0-2.4-.2-3.4-.5L4 21l1.2-3.2A9.6 9.6 0 0 1 2 12 10 10 0 0 1 12 2Z" />
      </svg>
    </div>
  )
}

/** 会话列表相对时间：刚刚 / N 分钟前 / N 小时前 / 昨天 / M月D日 */
function fmtAgo(iso) {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const diff = Date.now() - t
  const min = 60 * 1000
  const hour = 60 * min
  if (diff < min) return '刚刚'
  if (diff < hour) return `${Math.floor(diff / min)} 分钟前`
  if (diff < 24 * hour) return `${Math.floor(diff / hour)} 小时前`
  const d = new Date(t)
  return `${d.getMonth() + 1}月${d.getDate()}日`
}

const VIEW_META = {
  chat: { title: 'AI 客服助手', sub: '纺织产品 · 下单 · 售后' },
  orders: { title: '我的订单', sub: '历史下单记录 · 状态跟踪' },
  approval: { title: '订单审批', sub: '待人工确认的下单请求' },
}

export default function App() {
  // ── 会话门禁（2026-09）：无 token/失效 → /me 401 → 登录页 ──
  const [user, setUser] = useState(null) // { user_id, role, display_name, username }
  const [booting, setBooting] = useState(true)

  // ── 多会话 ──
  const [sessions, setSessions] = useState([])
  const [sessionId, setSessionId] = useState('')
  const [sessionsReady, setSessionsReady] = useState(false)

  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState('')
  const [steps, setSteps] = useState([]) // 图节点执行过程（节点流式）
  const [view, setView] = useState('chat') // chat | orders | approval(仅 admin)
  const messagesEndRef = useRef(null)
  const typeTimerRef = useRef(null)

  // 启动：探测登录态（fetchMe 401 → null → 登录页）
  useEffect(() => {
    fetchMe()
      .then((u) => setUser(u))
      .catch(() => setUser(null))
      .finally(() => setBooting(false))
  }, [])

  // 登录成功（LoginPage 回调）
  const handleAuthed = useCallback((u) => {
    setUser(u)
    setView('chat')
    setMessages([])
    setSteps([])
    setError('')
  }, [])

  // 退出登录：清 token → 回登录页（服务端无状态，无需调接口）
  const handleLogout = useCallback(() => {
    logout()
    clearInterval(typeTimerRef.current)
    setUser(null)
    setSessions([])
    setSessionId('')
    setSessionsReady(false)
    setView('chat')
    setMessages([])
    setSteps([])
    setError('')
    setInput('')
  }, [])

  // 登录后加载会话列表；无会话自动新建一个
  useEffect(() => {
    if (!user) {
      setSessionsReady(false)
      return
    }
    let alive = true
    ;(async () => {
      try {
        let list = await fetchSessions()
        let active = list[0]?.session_id || ''
        if (!active) {
          const created = await createSession()
          list = [created]
          active = created.session_id
        }
        if (!alive) return
        setSessions(list)
        setSessionId(active)
      } catch {
        /* 会话加载失败：保持空，等发送时重试 */
      } finally {
        if (alive) setSessionsReady(true)
      }
    })()
    return () => { alive = false }
  }, [user?.user_id])

  // 静默刷新会话列表（发送后标题自动更新时用）
  const refreshSessions = useCallback(() => {
    fetchSessions().then(setSessions).catch(() => {})
  }, [])

  // 新建对话：后端建会话 → 顶部插入并切换
  const handleNewSession = useCallback(async () => {
    setError('')
    try {
      const created = await createSession()
      setSessions((prev) => [created, ...prev.filter((x) => x.session_id !== created.session_id)])
      setSessionId(created.session_id)
      setView('chat')
      setMessages([])
      setSteps([])
    } catch (e) {
      setError(e.message)
    }
  }, [])

  const handleSelectSession = useCallback((sid) => {
    setView('chat')
    setSessionId(sid)
    setMessages([])
    setSteps([])
    setError('')
  }, [])

  const handleDeleteSession = useCallback(async (sid) => {
    if (!window.confirm('删除该对话？其中的消息会一并清除。')) return
    try {
      await deleteSession(sid)
      const rest = sessions.filter((s) => s.session_id !== sid)
      if (sid === sessionId) {
        setMessages([])
        if (rest.length > 0) {
          setSessionId(rest[0].session_id)
        } else {
          const created = await createSession()
          rest.unshift(created)
          setSessionId(created.session_id)
        }
      }
      setSessions(rest)
    } catch (e) {
      setError(e.message)
    }
  }, [sessions, sessionId])

  // 加载当前会话的历史（登录/换会话时重载，互不串数据）
  useEffect(() => {
    if (!user || !sessionId) return () => clearInterval(typeTimerRef.current)
    let alive = true
    fetchHistory(sessionId).then((rows) => {
      if (!alive) return
      setMessages((rows || []).map((r) => ({
        id: uuid(),
        role: r.role === 'human' ? 'user' : 'ai',
        content: r.content,
        time: new Date(),
      })))
    })
    return () => {
      alive = false
      clearInterval(typeTimerRef.current)
    }
  }, [user?.user_id, sessionId])

  // 自动滚到底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const appendMessage = useCallback((msg) => {
    setMessages((prev) => [...prev, msg])
  }, [])

  /** 更新指定消息的 content（按 id） */
  const patchMessage = useCallback((id, updater) => {
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, ...updater(m) } : m))
    )
  }, [])

  /** 启动本地打字机：逐字揭示 content，完成回调 */
  const startTyping = useCallback((msgId, fullText, onDone) => {
    clearInterval(typeTimerRef.current)
    let idx = 0
    patchMessage(msgId, () => ({ content: '' }))
    typeTimerRef.current = setInterval(() => {
      idx += 1 + Math.floor(Math.random() * 2) // 每次 1-2 字符
      const shown = fullText.slice(0, idx)
      patchMessage(msgId, () => ({ content: shown, streaming: idx < fullText.length }))
      if (idx >= fullText.length) {
        clearInterval(typeTimerRef.current)
        patchMessage(msgId, () => ({ content: fullText, streaming: false }))
        onDone?.()
      }
    }, TYPE_TICK_MS)
  }, [patchMessage])

  const send = useCallback(
    async (text) => {
      const content = (text ?? input).trim()
      if (!content || streaming) return

      // 确保有活跃会话（发送时兜底创建）
      let sid = sessionId
      if (!sid) {
        try {
          const created = await createSession()
          sid = created.session_id
          setSessions((prev) => [created, ...prev])
          setSessionId(sid)
        } catch (e) {
          setError(e.message)
          return
        }
      }

      setInput('')
      setError('')
      setSteps([]) // 新一轮对话重置执行步骤

      const userMsg = { id: uuid(), role: 'user', content, time: new Date() }
      const aiMsg = { id: uuid(), role: 'ai', content: '', time: new Date(), streaming: true }
      appendMessage(userMsg)
      appendMessage(aiMsg)
      setStreaming(true)
      let receivedTokens = false // 是否收到真 token 流

      await streamChat(content, {
        sessionId: sid,
        onStart: () => {},
        onReset: () => {}, // 后端已不使用 reset，保留兼容
        onToken: (token) => {
          // 真流式：token 逐字实时渲染
          receivedTokens = true
          patchMessage(aiMsg.id, (m) => ({ content: (m.content || '') + token }))
        },
        // 节点过程：改写查询 → 检索 → 路由 → 应答 → 审核
        onNode: (evt) => {
          setSteps((prev) => [...prev, evt])
        },
        onDone: (full, data) => {
          const text = full || '（无回复）'
          if (receivedTokens) {
            // 真 token 流已渲染 → 用服务端权威最终文本收尾
            patchMessage(aiMsg.id, () => ({ content: text, streaming: false }))
            setStreaming(false)
            if (data) patchMessage(aiMsg.id, () => ({ data }))
          } else {
            // 无 token（挂起/兜底路径）→ 本地打字机补偿
            startTyping(aiMsg.id, text, () => {
              setStreaming(false)
              if (data) patchMessage(aiMsg.id, () => ({ data }))
            })
          }
        },
        onError: (err) => {
          // token 过期：后端 401 → 清理会话回登录页
          if (String(err).includes('401')) {
            patchMessage(aiMsg.id, () => ({ content: '登录已过期，请重新登录', streaming: false }))
            setStreaming(false)
            setTimeout(handleLogout, 600)
            return
          }
          patchMessage(aiMsg.id, () => ({ content: err, streaming: false }))
          setError(err)
          setStreaming(false)
        },
      })

      // 会话标题可能被后端自动更新 → 静默刷新列表
      refreshSessions()
    },
    [input, streaming, sessionId, appendMessage, patchMessage, startTyping, handleLogout, refreshSessions]
  )

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  // ── 启动闪屏 / 登录页 / 主界面 ──
  if (booting) {
    return (
      <div className="flex min-h-[100dvh] w-full items-center justify-center bg-transparent">
        <div className="flex animate-fade-up flex-col items-center gap-4">
          <BrandMark size="lg" />
          <p className="text-[12px] text-slate-400">正在进入交易智能体…</p>
        </div>
      </div>
    )
  }

  if (!user) {
    return <LoginPage onSuccess={handleAuthed} />
  }

  const displayName = user.display_name || user.user_id
  const isAdmin = user.role === 'admin'
  const meta = VIEW_META[view] || VIEW_META.chat
  const showChat = view === 'chat'
  const showOrders = view === 'orders'
  const showApproval = view === 'approval' && isAdmin

  const goView = (v) => {
    setView(view === v ? 'chat' : v)
    if (v !== 'chat') setSteps([])
  }

  return (
    <div className="flex h-[100dvh] w-full gap-3 overflow-hidden bg-transparent p-3">
      {/* ── 侧栏：品牌 + 新建对话 + 会话列表 ── */}
      <aside className="hidden w-64 shrink-0 flex-col overflow-hidden rounded-2xl border border-white/10 bg-white/[0.04] backdrop-blur-2xl shadow-panel md:flex">
        <div className="flex items-center gap-3 px-5 py-4">
          <BrandMark />
          <div className="min-w-0">
            <div className="text-sm font-semibold tracking-tight text-slate-50">交易智能体</div>
            <div className="truncate text-[11px] text-slate-400">纺织面料 B2B 交易助手</div>
          </div>
        </div>

        <div className="px-3 pb-2 pt-1">
          <button
            onClick={handleNewSession}
            className="flex w-full items-center justify-center gap-2 rounded-xl border border-brand-300/30 bg-brand-400/15 px-3 py-2 text-[13px] font-semibold text-brand-100 shadow-panel-sm backdrop-blur-xl transition-all hover:bg-brand-400/25 active:scale-[0.98]"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M12 5v14M5 12h14" />
            </svg>
            新建对话
          </button>
        </div>

        <div className="mx-4 h-px bg-white/[0.07]" />

        <div className="flex min-h-0 flex-1 flex-col px-2 py-3">
          <div className="mb-1.5 px-2 text-[11px] font-medium text-slate-400">对话</div>
          <div className="thin-scroll min-h-0 flex-1 space-y-0.5 overflow-y-auto">
            {sessions.length === 0 && !sessionsReady && (
              <div className="px-2 py-1 text-[12px] text-slate-400">加载中…</div>
            )}
            {sessions.length === 0 && sessionsReady && (
              <div className="px-2 py-1 text-[12px] text-slate-400">暂无对话</div>
            )}
            {sessions.map((s) => {
              const active = s.session_id === sessionId
              return (
                <div
                  key={s.session_id}
                  role="button"
                  tabIndex={0}
                  onClick={() => handleSelectSession(s.session_id)}
                  onKeyDown={(e) => { if (e.key === 'Enter') handleSelectSession(s.session_id) }}
                  className={`group flex cursor-pointer items-center gap-2 rounded-xl px-2.5 py-2 text-left transition-colors ${
                    active
                      ? 'border border-brand-300/25 bg-brand-400/15 text-slate-50'
                      : 'border border-transparent text-slate-300 hover:bg-white/[0.05]'
                  }`}
                >
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${active ? 'bg-brand-300/90' : 'bg-white/15'}`} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] leading-tight">{s.title || '新对话'}</span>
                    <span className="block text-[10px] leading-tight text-slate-400">{fmtAgo(s.updated_at)}</span>
                  </span>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleDeleteSession(s.session_id) }}
                    title="删除对话"
                    className="shrink-0 rounded-md p-1 text-slate-400 opacity-0 transition-all hover:bg-rose-500/15 hover:text-rose-300 focus:opacity-100 group-hover:opacity-100 active:scale-90"
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
                    </svg>
                  </button>
                </div>
              )
            })}
          </div>
        </div>

        <div className="border-t border-white/[0.07] px-5 py-3.5">
          <div className="flex items-center gap-2 text-[12px] text-slate-400">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
            </span>
            在线 · 即时响应
          </div>
        </div>
      </aside>

      {/* ── 主区：上(顶栏) / 中(内容面板) / 下(输入，仅对话) 三个圆角浮层 ── */}
      <div className="flex min-w-0 flex-1 flex-col gap-2.5">
        {/* 上：顶栏 */}
        <header className="flex shrink-0 items-center justify-between gap-2 rounded-2xl border border-white/10 bg-white/[0.04] px-3 py-2.5 shadow-panel-sm backdrop-blur-2xl sm:gap-3 sm:px-4">
          <div className="flex min-w-0 items-center gap-2.5 sm:gap-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-brand-300/25 bg-brand-400/15 text-brand-100 backdrop-blur-md">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><path d="M12 2a10 10 0 0 1 10 10c0 5-4 8-10 8-1.2 0-2.4-.2-3.4-.5L4 21l1.2-3.2A9.6 9.6 0 0 1 2 12 10 10 0 0 1 12 2Z" /></svg>
            </div>
            <div className="min-w-0">
              <h1 className="truncate text-[15px] font-semibold tracking-tight text-slate-50">{meta.title}</h1>
              <p className="hidden truncate text-[11px] text-slate-400 sm:block">{meta.sub}</p>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2 sm:gap-2.5">
            {/* 当前用户 */}
            <div className="flex min-w-0 items-center gap-2 rounded-lg border border-white/10 bg-white/[0.05] py-1.5 pl-2.5 pr-1.5">
              <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${isAdmin ? 'bg-emerald-400' : 'bg-slate-400'}`} />
              <span className="max-w-[40px] truncate text-[12px] font-medium text-slate-200 sm:max-w-[100px]">{displayName}</span>
              <span
                className={`hidden rounded-md border px-1.5 py-0.5 text-[10px] font-medium leading-none sm:inline-flex ${
                  isAdmin
                    ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
                    : 'border-white/[0.12] bg-white/[0.07] text-slate-400'
                }`}
              >
                {isAdmin ? '管理员' : '客户'}
              </span>
            </div>

            {/* 我的订单（所有人） */}
            <button
              onClick={() => goView('orders')}
              className={`shrink-0 whitespace-nowrap rounded-lg border px-2.5 py-1.5 text-[11px] font-medium transition-colors active:scale-95 sm:px-3 sm:text-[12px] ${
                showOrders
                  ? 'border-brand-300/40 bg-brand-400/20 text-brand-100'
                  : 'border-white/10 bg-white/[0.05] text-slate-300 hover:bg-white/[0.07]'
              }`}
            >
              {showOrders ? '返回聊天' : '我的订单'}
            </button>

            {/* 订单审批（仅管理员） */}
            {isAdmin && (
              <button
                onClick={() => goView('approval')}
                className={`shrink-0 whitespace-nowrap rounded-lg border px-2.5 py-1.5 text-[11px] font-medium transition-colors active:scale-95 sm:px-3 sm:text-[12px] ${
                  showApproval
                    ? 'border-brand-300/40 bg-brand-400/20 text-brand-100'
                    : 'border-white/10 bg-white/[0.05] text-slate-300 hover:bg-white/[0.07]'
                }`}
              >
                {showApproval ? '返回聊天' : '订单审批'}
              </button>
            )}

            <button
              onClick={handleNewSession}
              className="hidden shrink-0 items-center gap-1 whitespace-nowrap rounded-lg border border-white/10 bg-white/[0.05] px-3 py-1.5 text-[12px] font-medium text-slate-300 transition-colors hover:bg-white/[0.07] active:scale-95 sm:flex"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
              新对话
            </button>

            <button
              onClick={handleLogout}
              title="退出登录"
              className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-lg border border-white/10 bg-white/[0.05] text-slate-400 transition-colors hover:border-rose-500/40 hover:text-rose-300 active:scale-95"
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
                <path d="m16 17 5-5-5-5M21 12H9" />
              </svg>
            </button>
          </div>
        </header>

        {/* 中：审批 / 订单 / 对话 面板 */}
        {showApproval ? (
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl border border-white/10 bg-white/[0.02] shadow-panel backdrop-blur-xl">
            <main className="thin-scroll flex-1 overflow-y-auto">
              <ApprovalPanel />
            </main>
          </div>
        ) : showOrders ? (
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl border border-white/10 bg-white/[0.02] shadow-panel backdrop-blur-xl">
            <main className="thin-scroll flex-1 overflow-y-auto">
              <OrderList />
            </main>
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl border border-white/10 bg-white/[0.02] shadow-panel backdrop-blur-xl">
            {/* 执行步骤丝带（节点流式，嵌在消息面板顶部，不随消息滚动） */}
            {steps.length > 0 && (
              <div className="thin-scroll shrink-0 border-b border-white/[0.08] bg-white/[0.04] px-4 py-2">
                <div className="mx-auto flex max-w-3xl items-center gap-2 overflow-x-auto">
                  {steps.map((s, i) => (
                    <div
                      key={`${s.node}-${i}`}
                      className="flex shrink-0 items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.06] px-3 py-1 text-[11px] text-slate-300 shadow-soft"
                    >
                      {i === steps.length - 1 && streaming ? (
                        <span className="relative flex h-1.5 w-1.5">
                          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand-400 opacity-75" />
                          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-brand-300/90" />
                        </span>
                      ) : (
                        <span className="font-semibold leading-none text-emerald-400">✓</span>
                      )}
                      <span className="font-medium text-slate-100">{s.label}</span>
                      {s.detail && (
                        <span className="max-w-[180px] truncate text-slate-400">{s.detail}</span>
                      )}
                    </div>
                  ))}
                  {streaming && (
                    <span className="shrink-0 text-[11px] text-slate-400">处理中…</span>
                  )}
                </div>
              </div>
            )}

            {!sessionsReady && !sessionId ? (
              <div className="flex flex-1 items-center justify-center text-[13px] text-slate-400">加载会话…</div>
            ) : (
              <main className="thin-scroll flex-1 overflow-y-auto">
                <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
                  {messages.length === 0 && !streaming ? (
                    <EmptyState onPick={(s) => send(s)} />
                  ) : (
                    messages.map((m) => (
                      <MessageBubble key={m.id} message={m} last={m === messages[messages.length - 1]} />
                    ))
                  )}
                  {streaming && messages[messages.length - 1]?.content === '' && <TypingIndicator />}
                  {error && (
                    <div className="mx-auto text-[12px] text-rose-400">{error}</div>
                  )}
                  <div ref={messagesEndRef} />
                </div>
              </main>
            )}
          </div>
        )}

        {/* 下：输入面板（仅对话视图） */}
        {showChat && (
          <footer className="shrink-0 rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3 shadow-panel-sm backdrop-blur-2xl">
            <div className="mx-auto flex max-w-3xl items-end gap-3">
              <div className="flex-1 rounded-2xl border border-white/[0.12] bg-white/[0.06] px-4 py-2.5 shadow-panel-sm transition-colors focus-within:border-brand-400 focus-within:ring-2 focus-within:ring-brand-500/25">
                <textarea
                  rows={1}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder="请输入您的问题…"
                  className="max-h-32 w-full resize-none bg-transparent text-[14px] leading-relaxed text-slate-100 outline-none placeholder:text-slate-400/80"
                />
              </div>
              <button
                onClick={() => send()}
                disabled={streaming || !input.trim()}
                className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border border-brand-200/60 bg-brand-300/40 text-[#0d1a33] shadow-panel-sm backdrop-blur-xl transition-all hover:bg-brand-300/55 active:scale-95 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="m5 12 14 0M13 5l7 7-7 7" />
                </svg>
              </button>
            </div>
            <p className="mx-auto mt-2 max-w-3xl px-1 text-center text-[11px] text-slate-400">
              报价仅展示正常售价，AI 不会透露成本与内部信息
            </p>
          </footer>
        )}
      </div>
    </div>
  )
}

function EmptyState({ onPick }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center py-16 text-center">
      <div className="mb-5 flex h-14 w-14 items-center justify-center rounded-2xl border border-brand-300/30 bg-gradient-to-b from-brand-400/30 to-brand-500/15 text-brand-100 shadow-panel backdrop-blur-lg">
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 2a10 10 0 0 1 10 10c0 5-4 8-10 8-1.2 0-2.4-.2-3.4-.5L4 21l1.2-3.2A9.6 9.6 0 0 1 2 12 10 10 0 0 1 12 2Z" />
          <path d="M8 12h8M8 8h5M8 16h3" />
        </svg>
      </div>
      <h2 className="font-display text-xl font-semibold tracking-tight text-slate-50">
        您好，我是交易智能体
      </h2>
      <p className="mt-2 max-w-sm text-sm leading-relaxed text-slate-400">
        我可以帮您查询面料价格、规格与库存，推荐合适的产品，并协助完成下单与售后。
      </p>
      <div className="mt-7 flex flex-wrap items-center justify-center gap-2">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            onClick={() => onPick(s)}
            className="rounded-full border border-white/10 bg-white/[0.05] px-4 py-2 text-[13px] font-medium text-slate-300 shadow-soft transition-all hover:border-brand-500/50 hover:text-brand-300 active:scale-95"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}
