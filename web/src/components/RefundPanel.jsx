import { useCallback, useEffect, useRef, useState } from 'react'

import { decideRefund, fetchAdminRefunds } from '../api'
import StatusBadge from './StatusBadge'

/**
 * 退款审核（管理端，2026-09）
 * ==========================
 * 退款 Agent 建的工单（`refunds.status = 待审核`）在这里被处理 —— 在此之前
 * **没有任何接口能审它**，工单只能永远躺在库里。
 *
 * 交互要点：
 * 1. 审核必须**带备注**（驳回尤其：客户/客服要看到理由），所以通过/驳回都走一个小表单，
 *    而不是一个点了就走的按钮。
 * 2. 只能审一次：后端 CAS（`WHERE status='待审核'`），并发下第二个请求会被拒；
 *    前端失败后重新拉列表，让"已被别人处理"的真相立刻显示出来。
 */

const STATUS_FILTERS = ['待审核', '已通过', '已驳回', '']

function fmtTime(ts) {
  if (!ts) return '—'
  return String(ts).slice(0, 16).replace('T', ' ')
}

function fmtMoney(v) {
  const n = Number(v || 0)
  return Number.isFinite(n) ? `¥${n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}` : '—'
}

export default function RefundPanel({ initialStatus = '待审核' }) {
  const [status, setStatus] = useState(initialStatus)
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [openId, setOpenId] = useState(null)      // 正在填备注的工单
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(null)
  const mountedRef = useRef(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setItems(await fetchAdminRefunds({ status }))
      setError('')
    } catch (e) {
      if (mountedRef.current) setError(e.message)
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [status])

  useEffect(() => {
    mountedRef.current = true
    load()
    return () => { mountedRef.current = false }
  }, [load])

  const submit = async (r, approve) => {
    if (!approve && !note.trim()) {
      setError('驳回必须填写理由（客户会看到）')
      return
    }
    setBusy(r.id)
    setError('')
    setNotice('')
    try {
      const out = await decideRefund(r.id, approve, note.trim())
      // 订单联动结果一并提示：管理员必须看到"这张工单把订单改成了什么"
      setNotice(`工单 #${out.id}（订单 ${out.order_no}）→ ${out.status}`
        + (out.order_note ? `；订单：${out.order_note}` : ''))
      setOpenId(null)
      setNote('')
      await load()
    } catch (e) {
      setError(e.message)
      await load()   // 可能是"已被别人处理"，重新拉一次让状态对齐
    } finally {
      if (mountedRef.current) setBusy(null)
    }
  }

  const pendingCount = items.filter((r) => r.status === '待审核').length

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-4 px-4 py-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-[15px] font-semibold tracking-tight text-slate-50">退款审核</h2>
          <p className="mt-0.5 text-[11px] text-slate-400">
            客户提交的退款工单 · 审核结果会写审计日志
            {status === '待审核' && items.length > 0 ? ` · ${pendingCount} 单待处理` : ''}
          </p>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="shrink-0 whitespace-nowrap rounded-lg border border-white/10 bg-white/[0.05] px-3 py-1.5 text-[12px] font-medium text-slate-300 transition-colors hover:bg-white/[0.07] disabled:opacity-40 active:scale-95"
        >
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {STATUS_FILTERS.map((s) => (
          <button
            key={s || 'all'}
            onClick={() => { setStatus(s); setOpenId(null); setNote('') }}
            className={`rounded-full border px-2.5 py-1 text-[11px] font-medium transition-all active:scale-95 ${
              status === s
                ? 'border-brand-300/40 bg-brand-400/20 text-brand-100'
                : 'border-white/10 bg-white/[0.05] text-slate-300 hover:bg-white/[0.08]'
            }`}
          >
            {s || '全部'}
          </button>
        ))}
      </div>

      {notice && (
        <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-2.5 text-[12px] text-emerald-300">
          ✓ {notice}
        </div>
      )}
      {error && (
        <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-2.5 text-[12px] text-rose-300">
          {error}
        </div>
      )}

      {!loading && items.length === 0 && !error && (
        <div className="rounded-xl border border-dashed border-white/10 py-16 text-center">
          <p className="text-[13px] font-medium text-slate-300">
            {status === '待审核' ? '没有待审核的退款工单' : '没有符合条件的退款工单'}
          </p>
          <p className="mt-1 text-[12px] text-slate-400">客户在对话里申请退款后会出现在这里</p>
        </div>
      )}

      <div className="flex flex-col gap-3">
        {items.map((r) => {
          const pending = r.status === '待审核'
          const open = openId === r.id
          return (
            <div
              key={r.id}
              className="overflow-hidden rounded-2xl border border-white/10 bg-white/[0.04] shadow-panel-sm backdrop-blur-xl"
            >
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.08] bg-white/[0.03] px-4 py-2.5">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="font-mono text-[11px] text-slate-400">#{r.id}</span>
                  <span className="truncate font-mono text-[12px] font-semibold text-slate-100">
                    {r.order_no}
                  </span>
                  <StatusBadge status={r.status} />
                </div>
                <span className="text-[11px] text-slate-400">提交 {fmtTime(r.created_at)}</span>
              </div>

              <div className="px-4 py-3">
                <p className="text-[13px] text-slate-100">
                  <span className="text-slate-400">退款原因：</span>{r.reason || '—'}
                </p>

                <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-slate-400">
                  <span>客户 {r.customer_id || '—'}</span>
                  <span>{r.product_name || '—'}{r.quantity ? ` · ${r.quantity} 米` : ''}</span>
                  <span>订单金额 {fmtMoney(r.total)}</span>
                  <span>订单状态 {r.order_status || '—'}</span>
                  {r.order_status_before && (
                    <span title="发起退款时订单的状态；驳回会原样退回">
                      退款前 {r.order_status_before}
                    </span>
                  )}
                </div>

                {!pending && (
                  <div className="mt-2 border-t border-white/[0.06] pt-2 text-[11px] text-slate-400">
                    审核人 {r.decided_by || '—'} · {fmtTime(r.decided_at)}
                    {r.note ? ` · 备注：${r.note}` : ''}
                  </div>
                )}

                {pending && !open && (
                  <div className="mt-3 flex items-center gap-1.5">
                    <button
                      onClick={() => { setOpenId(r.id); setNote(''); setError('') }}
                      className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-[11px] font-medium text-emerald-300 transition-all hover:bg-emerald-500/20 active:scale-95"
                    >
                      审核处理
                    </button>
                  </div>
                )}

                {pending && open && (
                  <div className="mt-3 flex flex-col gap-2 border-t border-white/[0.06] pt-3">
                    <textarea
                      value={note}
                      onChange={(e) => setNote(e.target.value)}
                      rows={2}
                      placeholder="审核备注：通过可留空；驳回必须写理由（客户会看到）"
                      className="w-full resize-y rounded-lg border border-white/10 bg-white/[0.05] px-3 py-2 text-[12px] text-slate-100 outline-none transition-colors placeholder:text-slate-500 focus:border-brand-300/40"
                    />
                    <div className="flex items-center gap-1.5">
                      <button
                        disabled={busy === r.id}
                        onClick={() => submit(r, true)}
                        className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 text-[11px] font-medium text-emerald-300 transition-all hover:bg-emerald-500/20 disabled:opacity-40 active:scale-95"
                      >
                        {busy === r.id ? '处理中…' : '✓ 通过退款'}
                      </button>
                      <button
                        disabled={busy === r.id}
                        onClick={() => submit(r, false)}
                        className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-1 text-[11px] font-medium text-rose-300 transition-all hover:bg-rose-500/20 disabled:opacity-40 active:scale-95"
                      >
                        ✕ 驳回
                      </button>
                      <button
                        disabled={busy === r.id}
                        onClick={() => { setOpenId(null); setNote('') }}
                        className="rounded-lg border border-white/10 px-3 py-1 text-[11px] text-slate-400 transition-colors hover:text-slate-200 disabled:opacity-40 active:scale-95"
                      >
                        取消
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
