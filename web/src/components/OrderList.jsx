import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchOrders } from '../api'

/**
 * 我的订单（2026-09）
 * 客户查看自己的历史订单（后端按 customer_id 行级隔离）。
 * 卡片式布局：每单一张玻璃卡片 = 订单号/时间 + 状态徽章 + 商品明细 + 收货信息。
 */

function fmtTime(ts) {
  if (!ts) return ''
  return String(ts).slice(0, 16).replace('T', ' ')
}

function fmtPrice(v) {
  return v === null || v === undefined || v === '' ? '—' : `¥${v}`
}

function StatusBadge({ status }) {
  const st = String(status || '')
  let cls = 'border-white/10 bg-white/10 text-slate-400'
  if (st.includes('待付款') || st.includes('处理中')) cls = 'border-amber-500/30 bg-amber-500/10 text-amber-300'
  else if (st.includes('已付款') || st.includes('已完成') || st.includes('通过')) cls = 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
  else if (st.includes('已发货')) cls = 'border-brand-400/30 bg-brand-400/10 text-brand-200'
  else if (st.includes('拒绝') || st.includes('失败')) cls = 'border-rose-500/30 bg-rose-500/10 text-rose-300'
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium whitespace-nowrap ${cls}`}>
      {st || '—'}
    </span>
  )
}

function OrderCard({ o }) {
  return (
    <div className="overflow-hidden rounded-xl border border-white/10 bg-white/[0.04] shadow-panel-sm backdrop-blur-xl">
      <div className="flex items-center justify-between gap-3 border-b border-white/[0.08] bg-white/[0.03] px-4 py-2.5">
        <div className="flex min-w-0 items-center gap-2">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-brand-300">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <path d="M14 2v6h6M16 13H8M16 17H8M10 9H8" />
          </svg>
          <span className="truncate font-mono text-[12px] font-semibold tracking-tight text-slate-100">{o.order_no || '—'}</span>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className="hidden text-[11px] text-slate-400 sm:inline">{fmtTime(o.created_at)}</span>
          <StatusBadge status={o.status} />
        </div>
      </div>

      <div className="px-4 py-3">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <div className="min-w-0">
            <div className="truncate text-[14px] font-semibold text-slate-50">{o.product_name || '—'}</div>
            {o.color && <div className="mt-0.5 text-[11px] text-slate-400">颜色：{o.color}</div>}
          </div>
          <div className="flex shrink-0 items-baseline gap-3 text-[12px]">
            <span className="text-slate-400">{o.quantity != null ? `${o.quantity} 米` : '—'}</span>
            <span className="text-slate-400">{fmtPrice(o.unit_price)}/米</span>
            <span className="text-[14px] font-bold text-brand-300">{fmtPrice(o.total)}</span>
          </div>
        </div>

        {(o.phone || o.address || o.delivery_date) && (
          <div className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 border-t border-white/[0.06] pt-2.5 text-[11px] text-slate-400">
            {o.phone && <span>📞 {o.phone}</span>}
            {o.address && <span className="min-w-0 truncate">📍 {o.address}</span>}
            {o.delivery_date && <span>🚚 交期 {fmtTime(o.delivery_date)}</span>}
          </div>
        )}
      </div>
    </div>
  )
}

export default function OrderList() {
  const [orders, setOrders] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const mountedRef = useRef(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setOrders(await fetchOrders())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  // 静默刷新：不闪 loading（审批在另一个端发生时，新订单能自动出现）
  const silentRefresh = useCallback(async () => {
    if (!mountedRef.current) return
    try {
      const rows = await fetchOrders()
      if (mountedRef.current) setOrders(rows)
    } catch { /* 网络抖动忽略，等下次轮询 */ }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    load()
    const timer = setInterval(silentRefresh, 15000) // 15s 轻轮询
    return () => { mountedRef.current = false; clearInterval(timer) }
  }, [load, silentRefresh])

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-[15px] font-semibold tracking-tight text-slate-50">我的订单</h2>
          <p className="mt-0.5 text-[11px] text-slate-400">历史下单记录 · 状态跟踪（审批通过的订单才会出现在这里）</p>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="shrink-0 whitespace-nowrap rounded-lg border border-white/10 bg-white/[0.05] px-3 py-1.5 text-[12px] font-medium text-slate-300 transition-colors hover:bg-white/[0.07] disabled:opacity-40 active:scale-95"
        >
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>

      {error && (
        <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-2.5 text-[12px] text-rose-300">
          {error}
        </div>
      )}

      {!loading && orders.length === 0 && !error && (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-white/10 py-16 text-center">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl border border-brand-300/25 bg-brand-400/15 text-brand-200 backdrop-blur-md">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z" />
              <path d="M3 6h18M16 10a4 4 0 0 1-8 0" />
            </svg>
          </div>
          <p className="mt-3 text-[13px] font-medium text-slate-300">还没有订单</p>
          <p className="mt-1 text-[12px] text-slate-400">回到对话，告诉 AI 您要下单，审批通过后订单会自动出现在这里</p>
        </div>
      )}

      {orders.length > 0 && (
        <div className="flex flex-col gap-3">
          {orders.map((o) => <OrderCard key={o.order_no || o.id} o={o} />)}
        </div>
      )}
    </div>
  )
}
