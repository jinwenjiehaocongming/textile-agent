import { memo } from 'react'

/**
 * 结构化数据可视化组件（深色主题）。
 * 根据 data.type 渲染对应组件：
 *   - products: 产品价格/规格表格（多行）
 *   - order:   订单信息卡片布局
 *   - refund:  退款单信息卡片
 */

export const DataTable = memo(function DataTable({ data }) {
  if (!data || !data.type) return null
  if (data.type === 'products') return <ProductsTable products={data.data} />
  if (data.type === 'order') return <OrderCard order={data.data} />
  if (data.type === 'refund') return <RefundCard refund={data.data} />
  return null
})

function fmt(v, suffix = '') {
  if (v === null || v === undefined || v === '') return '—'
  return `${v}${suffix}`
}

// ── 产品表格 ──────────────────────────────────────────────
function ProductsTable({ products }) {
  const list = Array.isArray(products) ? products : [products]
  return (
    <div className="my-2 overflow-hidden rounded-xl border border-slate-800 bg-slate-900">
      <div className="thin-scroll overflow-x-auto">
        <table className="w-full min-w-[520px] text-[12px]">
          <thead>
            <tr className="border-b border-slate-800 bg-slate-800/70 text-left text-[11px] uppercase tracking-wide text-slate-400">
              <th className="px-3 py-2 font-medium">产品</th>
              <th className="px-3 py-2 font-medium">颜色</th>
              <th className="px-3 py-2 font-medium">规格</th>
              <th className="px-3 py-2 font-medium">门幅</th>
              <th className="px-3 py-2 text-right font-medium">价格</th>
              <th className="px-3 py-2 text-right font-medium">MOQ</th>
              <th className="px-3 py-2 text-right font-medium">交期</th>
            </tr>
          </thead>
          <tbody>
            {list.map((p, i) => (
              <tr
                key={i}
                className="border-b border-slate-800 last:border-0 hover:bg-brand-500/10 transition-colors"
              >
                <td className="px-3 py-2.5 font-medium text-slate-100">{fmt(p.name)}</td>
                <td className="px-3 py-2.5 text-slate-300">{fmt(p.color)}</td>
                <td className="px-3 py-2.5 text-slate-300">{fmt(p.weight)}</td>
                <td className="px-3 py-2.5 text-slate-300">{fmt(p.width)}</td>
                <td className="px-3 py-2.5 text-right font-semibold text-brand-300">
                  <span className="text-[10px] text-slate-500">¥</span>
                  {fmt(p.price, '/米')}
                </td>
                <td className="px-3 py-2.5 text-right text-slate-300">{fmt(p.moq, '米')}</td>
                <td className="px-3 py-2.5 text-right text-slate-300">{fmt(p.delivery_days, '天')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── 订单卡片 ──────────────────────────────────────────────
function OrderCard({ order }) {
  const rows = [
    ['订单号', order.order_no],
    ['产品', order.product_name && order.color ? `${order.product_name} · ${order.color}` : (order.product_name || order.color || '')],
    ['数量', order.quantity ? `${order.quantity} 米` : ''],
    ['单价', order.unit_price ? `¥${order.unit_price}/米` : ''],
    ['总价', order.total ? `¥${order.total}` : ''],
    ['状态', order.status],
    ['下单时间', order.created_at ? String(order.created_at).slice(0, 16) : ''],
    ['电话', order.phone || ''],
    ['地址', order.address || ''],
    ['交期', order.delivery_date || ''],
  ].filter(([, v]) => v)

  return (
    <div className="my-2 overflow-hidden rounded-xl border border-slate-800 bg-slate-900">
      <div className="flex items-center gap-2 border-b border-slate-800 bg-slate-800/70 px-4 py-2.5">
        <span className="text-[12px] font-semibold text-slate-100">📋 订单信息</span>
        {order.status && (
          <StatusBadge status={order.status} />
        )}
      </div>
      <dl className="divide-y divide-slate-800 px-4 text-[12px]">
        {rows.map(([k, v]) => (
          <div key={k} className="flex justify-between gap-4 py-2">
            <dt className="shrink-0 text-slate-500">{k}</dt>
            <dd className="text-right font-medium text-slate-100">{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

// ── 退款单卡片 ────────────────────────────────────────────
function RefundCard({ refund }) {
  const rows = [
    ['关联订单', refund.order_no],
    ['退款原因', refund.reason],
    ['状态', refund.status],
    ['创建时间', refund.created_at ? String(refund.created_at).slice(0, 16) : ''],
  ].filter(([, v]) => v)

  return (
    <div className="my-2 overflow-hidden rounded-xl border border-slate-800 bg-slate-900">
      <div className="flex items-center gap-2 border-b border-slate-800 bg-slate-800/70 px-4 py-2.5">
        <span className="text-[12px] font-semibold text-slate-100">↩️ 退款单</span>
        {refund.status && <StatusBadge status={refund.status} />}
      </div>
      <dl className="divide-y divide-slate-800 px-4 text-[12px]">
        {rows.map(([k, v]) => (
          <div key={k} className="flex justify-between gap-4 py-2">
            <dt className="shrink-0 text-slate-500">{k}</dt>
            <dd className="text-right font-medium text-slate-100">{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

// ── 状态徽章（深色） ───────────────────────────────────────
function StatusBadge({ status }) {
  const s = String(status || '')
  let cls = 'bg-slate-800 text-slate-300 border-slate-700'
  if (s.includes('待付款') || s.includes('处理中')) cls = 'bg-amber-500/10 text-amber-300 border-amber-500/30'
  else if (s.includes('已付款') || s.includes('已完成') || s.includes('通过')) cls = 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30'
  else if (s.includes('已发货')) cls = 'bg-brand-500/10 text-brand-300 border-brand-500/30'
  else if (s.includes('拒绝') || s.includes('失败')) cls = 'bg-rose-500/10 text-rose-300 border-rose-500/30'
  return (
    <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium border ${cls}`}>
      {s}
    </span>
  )
}