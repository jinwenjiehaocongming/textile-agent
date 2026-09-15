import { Fragment, useCallback, useEffect, useRef, useState } from 'react'

import { fetchAdminOrders, setOrderStatus } from '../api'
import StatusBadge from './StatusBadge'

/**
 * 订单管理（管理端，2026-09）
 * ==========================
 * 管理员看**全站**订单（`/orders` 只有自己的单），并能按状态机推进：
 *   待付款 →(付款) 已付款 →(发货) 已发货 →(收货) 已收货
 *   待付款/已付款 →(取消) 已取消       已收货 / 已取消 = 终态
 *
 * 两个刻意的设计：
 * 1. **前端按钮状态与后端状态机同源**（ALLOWED_TRANSITIONS 抄一份）：非法动作直接不显示，
 *    但后端仍会校验 —— 前端只是体验，不是防线（curl 一样能打接口）。
 * 2. 动作成功后**重新拉当前页**，而不是本地改状态：付款/发货会补 paid_at/shipped_at，
 *    以服务端返回为准才不会出现"界面说发货了、库里时间还是空"。
 */

// 与 src/order_flow.py 的 ALLOWED_TRANSITIONS 保持一致（**手工**可做的动作）
// 「退款中」和「已退款」刻意**没有手工动作**：它们由退款工单决定，
// 否则会出现"货也发了、款也退了"的双向损失（详见 src/order_flow.py 的说明）
const ALLOWED = {
  待付款: ['已付款', '已取消'],
  已付款: ['已发货', '已取消'],
  已发货: ['已收货'],
  已收货: [],
  退款中: [],
  已退款: [],
  已取消: [],
}
// 动作按钮文案（键是目标状态）
const ACTION_LABEL = { 已付款: '确认付款', 已发货: '发货', 已收货: '确认收货', 已取消: '取消订单' }
const ACTION_TONE = {
  已付款: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20',
  已发货: 'border-brand-300/30 bg-brand-400/10 text-brand-100 hover:bg-brand-400/20',
  已收货: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20',
  已取消: 'border-rose-500/30 bg-rose-500/10 text-rose-300 hover:bg-rose-500/20',
}

const STATUS_FILTERS = ['', '待付款', '已付款', '已发货', '已收货', '退款中', '已退款', '已取消']
const PAGE_SIZE = 20

function fmtMoney(v) {
  const n = Number(v || 0)
  return Number.isFinite(n) ? `¥${n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}` : '—'
}

function fmtTime(ts) {
  if (!ts) return '—'
  return String(ts).slice(0, 16).replace('T', ' ')
}

export default function OrderManager({ initialStatus = '', initialKeyword = '' }) {
  const [status, setStatus] = useState(initialStatus)
  const [keywordInput, setKeywordInput] = useState(initialKeyword)
  const [keyword, setKeyword] = useState(initialKeyword)   // 只有"回车/点搜索"才生效
  const [page, setPage] = useState(0)
  const [data, setData] = useState({ orders: [], total: 0 })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState('')            // 正在操作的订单号
  const [expanded, setExpanded] = useState('')    // 展开详情的订单号
  const mountedRef = useRef(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const body = await fetchAdminOrders({
        status, keyword, limit: PAGE_SIZE, offset: page * PAGE_SIZE,
      })
      if (!mountedRef.current) return
      setData(body)
      setError('')
    } catch (e) {
      if (mountedRef.current) setError(e.message)
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [status, keyword, page])

  useEffect(() => {
    mountedRef.current = true
    load()
    return () => { mountedRef.current = false }
  }, [load])

  const totalPages = Math.max(1, Math.ceil((data.total || 0) / PAGE_SIZE))

  const act = async (order, target) => {
    if (target === '已取消' && !window.confirm(`确认取消订单 ${order.order_no}？取消后不可恢复。`)) return
    setBusy(order.order_no)
    setNotice('')
    try {
      const r = await setOrderStatus(order.order_no, target, '')
      setNotice(`订单 ${r.order_no}：${r.from} → ${r.to}`)
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      if (mountedRef.current) setBusy('')
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-4 px-4 py-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-[15px] font-semibold tracking-tight text-slate-50">订单管理</h2>
          <p className="mt-0.5 text-[11px] text-slate-400">
            全站订单 · 共 {data.total ?? 0} 单 · 状态流转会写审计日志
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

      {/* ── 筛选：状态 chips + 关键词 ── */}
      <div className="flex flex-col gap-2.5 rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3 shadow-panel-sm backdrop-blur-xl">
        <div className="flex flex-wrap items-center gap-1.5">
          {STATUS_FILTERS.map((s) => (
            <button
              key={s || 'all'}
              onClick={() => { setStatus(s); setPage(0) }}
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

        <form
          onSubmit={(e) => { e.preventDefault(); setKeyword(keywordInput.trim()); setPage(0) }}
          className="flex items-center gap-2"
        >
          <input
            value={keywordInput}
            onChange={(e) => setKeywordInput(e.target.value)}
            placeholder="订单号 / 客户 ID 或昵称 / 产品编号或名称"
            className="min-w-0 flex-1 rounded-lg border border-white/10 bg-white/[0.05] px-3 py-1.5 text-[12px] text-slate-100 outline-none transition-colors placeholder:text-slate-500 focus:border-brand-300/40"
          />
          <button
            type="submit"
            className="shrink-0 rounded-lg border border-white/10 bg-white/[0.06] px-3 py-1.5 text-[12px] font-medium text-slate-200 transition-colors hover:bg-white/[0.09] active:scale-95"
          >
            搜索
          </button>
          {(keyword || status) && (
            <button
              type="button"
              onClick={() => { setKeyword(''); setKeywordInput(''); setStatus(''); setPage(0) }}
              className="shrink-0 rounded-lg border border-white/10 px-3 py-1.5 text-[12px] text-slate-400 transition-colors hover:text-slate-200 active:scale-95"
            >
              清空
            </button>
          )}
        </form>
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

      {!loading && data.orders.length === 0 && !error && (
        <div className="rounded-xl border border-dashed border-white/10 py-16 text-center">
          <p className="text-[13px] font-medium text-slate-300">没有符合条件的订单</p>
          <p className="mt-1 text-[12px] text-slate-400">换个状态筛选或清空关键词试试</p>
        </div>
      )}

      {/* ── 订单表 ── */}
      {data.orders.length > 0 && (
        <div className="overflow-hidden rounded-2xl border border-white/10 bg-white/[0.04] shadow-panel-sm backdrop-blur-xl">
          <div className="thin-scroll overflow-x-auto">
            <table className="w-full min-w-[760px] text-[12px]">
              <thead>
                <tr className="border-b border-white/[0.08] bg-white/[0.03] text-left text-[11px] text-slate-400">
                  <th className="px-4 py-2 font-medium">订单号</th>
                  <th className="px-3 py-2 font-medium">客户</th>
                  <th className="px-3 py-2 font-medium">产品</th>
                  <th className="px-3 py-2 text-right font-medium">数量</th>
                  <th className="px-3 py-2 text-right font-medium">金额</th>
                  <th className="px-3 py-2 font-medium">状态</th>
                  <th className="px-3 py-2 font-medium">下单时间</th>
                  <th className="px-4 py-2 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {data.orders.map((o) => {
                  const open = expanded === o.order_no
                  const actions = ALLOWED[o.status] || []
                  return (
                    // ⚠️ 必须用带 key 的 Fragment：React 的 Fragment 简写语法**不能带 key**，
                    // 列表里用它 React 会报 "unique key" 警告，展开行还可能被复用错位
                    <Fragment key={o.order_no}>
                      <tr
                        className="border-b border-white/[0.06] transition-colors last:border-0 hover:bg-white/[0.03]"
                      >
                        <td className="px-4 py-2.5">
                          <button
                            onClick={() => setExpanded(open ? '' : o.order_no)}
                            className="flex items-center gap-1.5 font-mono text-[11px] text-slate-200 hover:text-brand-200"
                            title="展开收货信息"
                          >
                            <span className={`text-[9px] text-slate-500 transition-transform ${open ? 'rotate-90' : ''}`}>▶</span>
                            {o.order_no}
                          </button>
                        </td>
                        <td className="px-3 py-2.5 text-slate-300">
                          <div className="max-w-[120px] truncate">{o.customer_name || o.customer_id}</div>
                          <div className="max-w-[120px] truncate text-[10px] text-slate-500">{o.customer_id}</div>
                        </td>
                        <td className="px-3 py-2.5 text-slate-300">
                          <div className="max-w-[170px] truncate">{o.product_name}</div>
                          <div className="text-[10px] text-slate-500">{o.color || '—'} · {o.product_id}</div>
                        </td>
                        <td className="px-3 py-2.5 text-right text-slate-300">{o.quantity} 米</td>
                        <td className="px-3 py-2.5 text-right font-semibold text-brand-300">{fmtMoney(o.total)}</td>
                        <td className="px-3 py-2.5"><StatusBadge status={o.status} /></td>
                        <td className="px-3 py-2.5 text-[11px] text-slate-400">{fmtTime(o.created_at)}</td>
                        <td className="px-4 py-2.5">
                          <div className="flex flex-wrap items-center justify-end gap-1.5">
                            {actions.length === 0 && (o.status === '退款中' ? (
                              <span className="text-[10px] text-violet-300/80"
                                    title="订单已进退款流程，只能通过退款审核推进（通过→已退款 / 驳回→退回原状态）">
                                待退款审核{o.pending_refund_id ? ` #${o.pending_refund_id}` : ''}
                              </span>
                            ) : (
                              <span className="text-[10px] text-slate-500">终态</span>
                            ))}
                            {actions.map((target) => (
                              <button
                                key={target}
                                disabled={busy === o.order_no}
                                onClick={() => act(o, target)}
                                className={`whitespace-nowrap rounded-lg border px-2 py-1 text-[11px] font-medium transition-all disabled:opacity-40 active:scale-95 ${ACTION_TONE[target]}`}
                              >
                                {busy === o.order_no ? '…' : ACTION_LABEL[target]}
                              </button>
                            ))}
                          </div>
                        </td>
                      </tr>
                      {open && (
                        <tr key={`${o.order_no}-detail`} className="border-b border-white/[0.06] bg-white/[0.02]">
                          <td colSpan={8} className="px-4 py-3">
                            <div className="flex flex-wrap gap-x-6 gap-y-1.5 text-[11px] text-slate-400">
                              <span>📞 {o.phone || '—'}</span>
                              <span className="min-w-0">📍 {o.address || '—'}</span>
                              <span>🚚 交期 {fmtTime(o.delivery_date)}</span>
                              <span>💰 单价 {fmtMoney(o.unit_price)}/米</span>
                              <span>✅ 付款时间 {fmtTime(o.paid_at)}</span>
                              <span>📦 发货时间 {fmtTime(o.shipped_at)}</span>
                              <span className="text-slate-500">
                                可选流转：{actions.length ? actions.join('、')
                                  : (o.status === '退款中' ? '（由退款工单决定）' : '（终态，不可再变更）')}
                              </span>
                              {o.status === '退款中' && (
                                <span className="text-violet-300/80">
                                  退款流程进行中{o.pending_refund_id ? `（工单 #${o.pending_refund_id}）` : ''}
                                </span>
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>

          {/* ── 分页 ── */}
          <div className="flex items-center justify-between gap-3 border-t border-white/[0.08] px-4 py-2.5 text-[11px] text-slate-400">
            <span>第 {page + 1} / {totalPages} 页 · 共 {data.total} 单</span>
            <div className="flex items-center gap-1.5">
              <button
                disabled={page === 0 || loading}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                className="rounded-lg border border-white/10 px-2.5 py-1 transition-colors hover:bg-white/[0.07] disabled:opacity-30 active:scale-95"
              >
                上一页
              </button>
              <button
                disabled={page + 1 >= totalPages || loading}
                onClick={() => setPage((p) => p + 1)}
                className="rounded-lg border border-white/10 px-2.5 py-1 transition-colors hover:bg-white/[0.07] disabled:opacity-30 active:scale-95"
              >
                下一页
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
