import { useCallback, useEffect, useRef, useState } from 'react'
import { streamChat, fetchHistory } from './api'
import { MessageBubble } from './components/MessageBubble'
import { TypingIndicator } from './components/TypingIndicator'

// 快捷建议（空状态展示）
const SUGGESTIONS = [
  'T400 黑色多少钱',
  '羽绒服推荐什么面料',
  '你们有哪些涤塔夫现货',
  '我要确认一下下单',
]

// 打字机：每次 tick 揭示的字符数（用随机 1-3 更自然）
const TYPE_TICK_MS = 24

export default function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState('')
  const [steps, setSteps] = useState([]) // 图节点执行过程（节点流式）
  const messagesEndRef = useRef(null)
  const typeTimerRef = useRef(null)

  // 加载历史
  useEffect(() => {
    fetchHistory().then((rows) => {
      const mapped = (rows || []).map((r) => ({
        id: crypto.randomUUID(),
        role: r.role === 'human' ? 'user' : 'ai',
        content: r.content,
        time: new Date(),
      }))
      setMessages(mapped)
    })
    return () => clearInterval(typeTimerRef.current)
  }, [])

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
      setInput('')
      setError('')
      setSteps([]) // 新一轮对话重置执行步骤

      const userMsg = { id: crypto.randomUUID(), role: 'user', content, time: new Date() }
      const aiMsg = { id: crypto.randomUUID(), role: 'ai', content: '', time: new Date(), streaming: true }
      appendMessage(userMsg)
      appendMessage(aiMsg)
      setStreaming(true)
      let receivedTokens = false // 是否收到真 token 流

      await streamChat(content, {
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
          patchMessage(aiMsg.id, () => ({ content: err, streaming: false }))
          setError(err)
          setStreaming(false)
        },
      })
    },
    [input, streaming, appendMessage, patchMessage, startTyping]
  )

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="flex h-[100dvh] w-full overflow-hidden bg-slate-950">
      {/* ── 侧栏 ── */}
      <aside className="hidden w-64 flex-col border-r border-slate-800 bg-slate-900 md:flex">
        <div className="flex items-center gap-3 px-5 py-5">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-600 text-white shadow-soft">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2a10 10 0 0 1 10 10c0 5-4 8-10 8-1.2 0-2.4-.2-3.4-.5L4 21l1.2-3.2A9.6 9.6 0 0 1 2 12 10 10 0 0 1 12 2Z" />
            </svg>
          </div>
          <div>
            <div className="text-sm font-semibold tracking-tight text-slate-50">宏润纺织</div>
            <div className="text-[11px] text-slate-400">AI 智能客服</div>
          </div>
        </div>

        <div className="mx-5 h-px bg-slate-800" />

        <div className="flex-1 overflow-y-auto px-5 py-4">
          <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-slate-500">
            我可以帮你
          </div>
          <ul className="space-y-2.5 text-[13px] text-slate-300">
            {[
              ['产品询价', '价格 · 规格 · 库存'],
              ['面料咨询', '用途 · 成分 · 工艺'],
              ['在线下单', '确认单 → 生成订单'],
              ['售后处理', '退货 · 退款 · 投诉'],
            ].map(([t, d]) => (
              <li key={t} className="flex items-start gap-2.5">
                <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-brand-500" />
                <span>
                  <span className="font-medium text-slate-100">{t}</span>
                  <span className="block text-[12px] text-slate-500">{d}</span>
                </span>
              </li>
            ))}
          </ul>
        </div>

        <div className="border-t border-slate-800 px-5 py-4">
          <div className="flex items-center gap-2 text-[12px] text-slate-400">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
            </span>
            在线 · 即时响应
          </div>
        </div>
      </aside>

      {/* ── 主聊天区 ── */}
      <div className="flex flex-1 flex-col">
        {/* 顶栏 */}
        <header className="flex items-center justify-between border-b border-slate-800 bg-slate-900/80 px-5 py-3 backdrop-blur">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-600 text-white md:hidden">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><path d="M12 2a10 10 0 0 1 10 10c0 5-4 8-10 8-1.2 0-2.4-.2-3.4-.5L4 21l1.2-3.2A9.6 9.6 0 0 1 2 12 10 10 0 0 1 12 2Z" /></svg>
            </div>
            <div>
              <h1 className="text-[15px] font-semibold tracking-tight text-slate-50">AI 客服助手</h1>
              <p className="text-[11px] text-slate-400">纺织产品 · 下单 · 售后</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={() => window.location.reload()}
              className="rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-1.5 text-[12px] font-medium text-slate-300 transition-colors hover:bg-slate-800"
            >
              新会话
            </button>
          </div>
        </header>

        {/* 执行步骤丝带（节点流式） */}
        {steps.length > 0 && (
          <div className="thin-scroll border-b border-slate-800/70 bg-slate-900/70 px-4 py-2 backdrop-blur">
            <div className="mx-auto flex max-w-3xl items-center gap-2 overflow-x-auto">
              {steps.map((s, i) => (
                <div
                  key={`${s.node}-${i}`}
                  className="flex shrink-0 items-center gap-1.5 rounded-full border border-slate-700 bg-slate-800/80 px-3 py-1 text-[11px] text-slate-300 shadow-soft"
                >
                  {i === steps.length - 1 && streaming ? (
                    <span className="relative flex h-1.5 w-1.5">
                      <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand-400 opacity-75" />
                      <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-brand-500" />
                    </span>
                  ) : (
                    <span className="font-semibold leading-none text-emerald-400">✓</span>
                  )}
                  <span className="font-medium text-slate-100">{s.label}</span>
                  {s.detail && (
                    <span className="max-w-[180px] truncate text-slate-500">{s.detail}</span>
                  )}
                </div>
              ))}
              {streaming && (
                <span className="shrink-0 text-[11px] text-slate-500">处理中…</span>
              )}
            </div>
          </div>
        )}

        {/* 消息区 */}
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

        {/* 输入区 */}
        <footer className="border-t border-slate-800 bg-slate-900/80 px-4 py-3 backdrop-blur">
          <div className="mx-auto flex max-w-3xl items-end gap-3">
            <div className="flex-1 rounded-2xl border border-slate-700 bg-slate-800/60 px-4 py-2.5 shadow-soft transition-colors focus-within:border-brand-400 focus-within:ring-2 focus-within:ring-brand-500/20">
              <textarea
                rows={1}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="请输入您的问题…"
                className="max-h-32 w-full resize-none bg-transparent text-[14px] leading-relaxed text-slate-100 outline-none placeholder:text-slate-500"
              />
            </div>
            <button
              onClick={() => send()}
              disabled={streaming || !input.trim()}
              className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-brand-600 text-white shadow-soft transition-all hover:bg-brand-700 active:scale-95 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="m5 12 14 0M13 5l7 7-7 7" />
              </svg>
            </button>
          </div>
          <p className="mx-auto mt-2 max-w-3xl px-1 text-center text-[11px] text-slate-500">
            报价仅展示正常售价，AI 不会透露成本与内部信息
          </p>
        </footer>
      </div>
    </div>
  )
}

function EmptyState({ onPick }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center py-16 text-center">
      <div className="mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-brand-600 text-white shadow-lift">
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 2a10 10 0 0 1 10 10c0 5-4 8-10 8-1.2 0-2.4-.2-3.4-.5L4 21l1.2-3.2A9.6 9.6 0 0 1 2 12 10 10 0 0 1 12 2Z" />
          <path d="M8 12h8M8 8h5M8 16h3" />
        </svg>
      </div>
      <h2 className="font-display text-xl font-semibold tracking-tight text-slate-50">
        您好，我是宏润纺织智能客服
      </h2>
      <p className="mt-2 max-w-sm text-sm leading-relaxed text-slate-400">
        我可以帮您查询面料价格、规格与库存，推荐合适的产品，并协助完成下单与售后。
      </p>
      <div className="mt-7 flex flex-wrap items-center justify-center gap-2">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            onClick={() => onPick(s)}
            className="rounded-full border border-slate-700 bg-slate-800/60 px-4 py-2 text-[13px] font-medium text-slate-300 shadow-soft transition-all hover:border-brand-500/50 hover:text-brand-300"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}