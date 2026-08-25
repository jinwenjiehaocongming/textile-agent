import { memo } from 'react'
import { DataTable } from './DataTable'

/**
 * 轻量文本渲染：支持 **加粗**、换行。
 * 保持克制，不引入重型 markdown 库。
 */
function renderContent(text) {
  return text.split('\n').map((line, i) => {
    if (line.trim() === '') return <div key={i} className="h-2" />
    // 简单处理 **bold**
    const parts = line.split(/(\*\*[^*]+\*\*)/g).filter(Boolean)
    return (
      <div key={i} className={i > 0 ? 'mt-1' : ''}>
        {parts.map((p, j) => {
          if (p.startsWith('**') && p.endsWith('**')) {
            return (
              <strong key={j} className="font-semibold text-slate-50">
                {p.slice(2, -2)}
              </strong>
            )
          }
          if (p.startsWith('# ')) {
            return (
              <span key={j} className="block text-[15px] font-semibold text-slate-50">
                {p.slice(2)}
              </span>
            )
          }
          if (p.startsWith('- ') || p.startsWith('• ')) {
            return (
              <span key={j} className="block">
                <span className="mr-2 text-brand-400">•</span>
                {p.replace(/^[-•]\s*/, '')}
              </span>
            )
          }
          return <span key={j}>{p}</span>
        })}
      </div>
    )
  })
}

export const MessageBubble = memo(function MessageBubble({ message, last }) {
  const isUser = message.role === 'user'
  const typing = !isUser && message.streaming === true

  const time = message.time
    ? new Date(message.time).toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
      })
    : ''

  if (typing && message.content === '') return null // 由 TypingIndicator 接管

  return (
    <div className={`flex w-full gap-3 animate-fade-up ${isUser ? 'justify-end' : 'justify-start'}`}>
      {!isUser && (
        <div className="mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-600 text-white">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 2a10 10 0 0 1 10 10c0 5-4 8-10 8-1.2 0-2.4-.2-3.4-.5L4 21l1.2-3.2A9.6 9.6 0 0 1 2 12 10 10 0 0 1 12 2Z" />
          </svg>
        </div>
      )}

      <div
        className={`rounded-2xl px-4 py-2.5 text-[14px] leading-relaxed shadow-soft ${
          isUser
            ? 'max-w-[80%] rounded-br-md bg-brand-600 text-white'
            : 'rounded-bl-md border border-slate-700/60 bg-slate-900 text-slate-200'
        } ${message.data ? 'w-full max-w-[92%]' : 'max-w-[80%]'} ${typing ? 'typing-caret' : ''}`}
      >
        {renderContent(message.content)}
        {message.data && <DataTable data={message.data} />}
        <div
          className={`mt-1 text-[10px] ${
            isUser ? 'text-brand-100/80' : 'text-slate-500'
          }`}
        >
          {time}
        </div>
      </div>
    </div>
  )
})
