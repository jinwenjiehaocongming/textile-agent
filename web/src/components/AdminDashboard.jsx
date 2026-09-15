import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchAdminOrders, fetchAdminSummary } from '../api'
import StatusBadge from './StatusBadge'

/**
 * 管理员工作台（2026-09）
 * ======================
 * 管理员登录后的**默认首页**：管理端不需要"下单/聊天"，需要的是"今天该处理什么"。
 *
 * 内容按"要动作"到"要了解"排序：
 *   1. 待处理指标（待审批 / 待发货 / 退款待审 / 待付款）—— 点了直接跳对应页面
 *   2. 近 7 天订单趋势 —— 一眼看业务量
 *   3. 最近订单 —— 出问题时最近的单在哪
 *
 * 数据全部来自 /admin/orders/summary + /admin/orders（服务端 require_admin）。
 * 15s 轻轮询：其他端（客户下单、审批）产生的新数据能自己浮上来。
 */

function fmtMoney(v) {
  const n = Number(v || 0)
  if (!Number.isFinite(n)) return '—'
  if (Math.abs(n) >= 10000) return `¥${(n / 10000).toFixed(1)}万`
  return `¥${n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
}

function fmtTime(ts) {
  if (!ts) return '—'
  return String(ts).slice(0, 16).replace('T', ' ')
}

/** 指标卡：数字 + 标签 + 可选"去处理"箭头（有 jump 才可点） */
function MetricCard({ label, value, hint, tone = 'slate', jump, onClick }) {
  const tones = {
    amber: 'border-amber-500/25 bg-amber-500/[0.07] text-amber-200',
    brand: 'border-brand-300/25 bg-brand-400/[0.07] text-brand-100',
    emerald: 'border-emerald-500/25 bg-emerald-500/[0.07] text-emerald-200',
    rose: 'border-rose-500/25 bg-rose-500/[0.07] text-rose-200',
    slate: 'border-white/10 bg-white/[0.04] text-slate-200',
  }
  const clickable = typeof onClick === 'function' && Number(value) > 0
  return (
    <button
      type="button"
      onClick={clickable ? onClick : undefined}
      disabled={!clickable}
      className={`flex flex-col items-start gap-1 rounded-2xl border px-4 py-3.5 text-left shadow-panel-sm backdrop-blur-xl transition-all ${tones[tone]} ${
        clickable ? 'cursor-pointer hover:brightness-125 active:scale-[0.98]' : 'cursor-default opacity-90'
      }`}
    >
      <span className="text-[11px] font-medium text-slate-400">{label}</span>
      <span className="text-[22px] font-bold leading-none tracking-tight">{value ?? '—'}</span>
      {hint && <span className="text-[10px] text-slate-400">{hint}</span>}
      {jump && clickable && (
        <span className="mt-0.5 text-[10px] font-medium opacity-80">{jump} →</span>
      )}
    </button>
  )
}

/** 近 7 天柱状趋势（纯 div 高度，不引图表库 —— 首页要秒开） */
function TrendBars({ trend, days = 7 }) {
  // 后端只返回"有单的日期"，这里补齐 7 天骨架：缺口必须看得见，不能把 5 天画成 7 天
  const map = new Map((trend || []).map((t) => [String(t.day), t]))
  const today = new Date()
  const buckets = []
  for (let i = days - 1; i >= 0; i -= 1) {
    const d = new Date(today.getTime() - i * 86400000)
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
    const hit = map.get(key)
    buckets.push({
      key,
      label: `${d.getMonth() + 1}/${d.getDate()}`,
      orders: Number(hit?.orders || 0),
      gmv: Number(hit?.gmv || 0),
    })
  }
  const max = Math.max(1, ...buckets.map((b) => b.orders))
  const totalOrders = buckets.reduce((s, b) => s + b.orders, 0)
  const totalGmv = buckets.reduce((s, b) => s + b.gmv, 0)

  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3.5 shadow-panel-sm backdrop-blur-xl">
      <div className="flex items-baseline justify-between gap-3">
        <div className="text-[12px] font-medium text-slate-200">近 7 天订单</div>
        <div className="text-[11px] text-slate-400">
          {totalOrders} 单 · {fmtMoney(totalGmv)}
        </div>
      </div>

      <div className="mt-3 flex h-24 items-end gap-1.5">
        {buckets.map((b) => (
          <div key={b.key} className="group flex min-w-0 flex-1 flex-col items-center justify-end gap-1">
            <span className="text-[10px] font-medium text-slate-300 opacity-0 transition-opacity group-hover:opacity-100">
              {b.orders}
            </span>
            <div
              title={`${b.key}：${b.orders} 单 / ${fmtMoney(b.gmv)}`}
              className={`w-full rounded-t-md transition-all ${
                b.orders > 0
                  ? 'bg-gradient-to-t from-brand-500/40 to-brand-300/80'
                  : 'bg-white/[0.06]'
              }`}
              style={{ height: `${Math.max(3, (b.orders / max) * 100)}%` }}
            />
            <span className="text-[10px] text-slate-500">{b.label}</span>
          </div>
        ))}
      </div>

      {totalOrders === 0 && (
        <p className="mt-2 text-[11px] text-slate-400">近 7 天没有新订单</p>
      )}
    </div>
  )
}

export default function AdminDashboard({ onNavigate }) {
  const [summary, setSummary] = useState(null)
  const [recent, setRecent] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const mountedRef = useRef(true)

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const [s, r] = await Promise.all([
        fetchAdminSummary(),
        fetchAdminOrders({ limit: 8 }),
      ])
      if (!mountedRef.current) return
      setSummary(s)
      setRecent(r.orders || [])
      setError('')
    } catch (e) {
      if (mountedRef.current) setError(e.message)
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    load()
    const timer = setInterval(() => load(true), 15000)
    return () => { mountedRef.current = false; clearInterval(timer) }
  }, [load])

  const go = (view, params) => onNavigate?.(view, params)

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-4 px-4 py-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-[15px] font-semibold tracking-tight text-slate-50">经营工作台</h2>
          <p className="mt-0.5 text-[11px] text-slate-400">
            待处理事项 → 点击即可进入对应页面；数据 15 秒自动刷新
          </p>
        </div>
        <button
          onClick={() => load()}
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

      {/* ── 待处理（要动作的四项）── */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <MetricCard
          label="待审批下单" value={summary?.pending_approvals} tone="amber"
          hint="客户下的单等人工确认" jump="去审批"
          onClick={() => go('approval')}
        />
        <MetricCard
          label="待发货" value={summary?.to_ship} tone="brand"
          hint="已付款，等安排发货" jump="去发货"
          onClick={() => go('adminOrders', { status: '已付款' })}
        />
        <MetricCard
          label="退款待审核" value={summary?.refunds_to_review} tone="rose"
          hint={summary?.refunding
            ? `另有 ${summary.refunding} 单退款中（${fmtMoney(summary.refunding_amount)} 在途）`
            : '客户提交的退款工单'}
          jump="去审核"
          onClick={() => go('refunds', { status: '待审核' })}
        />
        <MetricCard
          label="未付款订单" value={summary?.unpaid} tone="slate"
          hint="已下单但未付款" jump="查看"
          onClick={() => go('adminOrders', { status: '待付款' })}
        />
      </div>

      {/* ── 经营概况 ── */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <MetricCard label="本月成交额" value={fmtMoney(summary?.gmv_this_month)} tone="emerald"
                    hint="不含已取消订单" />
        <MetricCard label="本月订单" value={summary?.orders_this_month} tone="slate" hint="下单量" />
        <MetricCard label="累计订单" value={summary?.orders_total} tone="slate" hint="全部历史订单" />
        <MetricCard label="活跃账号" value={summary?.active_users} tone="slate" hint="状态正常" />
      </div>

      <TrendBars trend={summary?.trend_7d} />

      {/* ── 最近订单 ── */}
      <div className="overflow-hidden rounded-2xl border border-white/10 bg-white/[0.04] shadow-panel-sm backdrop-blur-xl">
        <div className="flex items-center justify-between gap-3 border-b border-white/[0.08] px-4 py-2.5">
          <span className="text-[12px] font-medium text-slate-200">最近订单</span>
          <button
            onClick={() => go('adminOrders')}
            className="rounded-lg border border-white/10 bg-white/[0.05] px-2.5 py-1 text-[11px] font-medium text-slate-300 transition-colors hover:bg-white/[0.08] active:scale-95"
          >
            全部订单 →
          </button>
        </div>

        {recent.length === 0 ? (
          <p className="px-4 py-6 text-center text-[12px] text-slate-400">
            {loading ? '加载中…' : '暂无订单'}
          </p>
        ) : (
          <div className="thin-scroll overflow-x-auto">
            <table className="w-full min-w-[600px] text-[12px]">
              <thead>
                <tr className="border-b border-white/[0.08] bg-white/[0.03] text-left text-[11px] text-slate-400">
                  <th className="px-4 py-2 font-medium">订单号</th>
                  <th className="px-3 py-2 font-medium">客户</th>
                  <th className="px-3 py-2 font-medium">产品</th>
                  <th className="px-3 py-2 text-right font-medium">金额</th>
                  <th className="px-3 py-2 font-medium">状态</th>
                  <th className="px-4 py-2 font-medium">下单时间</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((o) => (
                  <tr
                    key={o.order_no}
                    onClick={() => go('adminOrders', { keyword: o.order_no })}
                    className="cursor-pointer border-b border-white/[0.06] transition-colors last:border-0 hover:bg-brand-500/10"
                  >
                    <td className="px-4 py-2.5 font-mono text-[11px] text-slate-200">{o.order_no}</td>
                    <td className="px-3 py-2.5 text-slate-300">
                      {o.customer_name || o.customer_id}
                    </td>
                    <td className="max-w-[180px] truncate px-3 py-2.5 text-slate-300">
                      {o.product_name}{o.color ? ` · ${o.color}` : ''}
                    </td>
                    <td className="px-3 py-2.5 text-right font-semibold text-brand-300">
                      {fmtMoney(o.total)}
                    </td>
                    <td className="px-3 py-2.5"><StatusBadge status={o.status} /></td>
                    <td className="px-4 py-2.5 text-slate-400">{fmtTime(o.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
