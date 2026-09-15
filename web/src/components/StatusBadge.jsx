/**
 * 订单/退款状态徽章（管理端 + 客户端共用）
 *
 * 之前 OrderList.jsx 和 DataTable.jsx 各写了一份同样的颜色映射，管理端又要用第三次 ——
 * 抽出来，避免"某个状态只在某一处显示成灰色"。
 * 深浅色类名保持与既有玻璃深色主题一致。
 */
export default function StatusBadge({ status, className = '' }) {
  const st = String(status || '')
  let cls = 'border-white/10 bg-white/10 text-slate-400'
  if (st.includes('退款中')) {
    // 退款中是"闸门状态"：已经进退款流程、钱还没退出去 —— 用紫色与其它状态区分开，
    // 免得被当成"正常的在途订单"（见 src/order_flow.py）
    cls = 'border-violet-500/30 bg-violet-500/10 text-violet-300'
  } else if (st.includes('已退款')) {
    cls = 'border-rose-500/30 bg-rose-500/10 text-rose-300'
  } else if (st.includes('待付款') || st.includes('待审核') || st.includes('处理中')) {
    cls = 'border-amber-500/30 bg-amber-500/10 text-amber-300'
  } else if (st.includes('已付款') || st.includes('已收货') || st.includes('已完成') || st.includes('已通过')) {
    cls = 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
  } else if (st.includes('已发货')) {
    cls = 'border-brand-400/30 bg-brand-400/10 text-brand-200'
  } else if (st.includes('取消') || st.includes('驳回') || st.includes('拒绝') || st.includes('失败')) {
    cls = 'border-rose-500/30 bg-rose-500/10 text-rose-300'
  }
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium whitespace-nowrap ${cls} ${className}`}>
      {st || '—'}
    </span>
  )
}
