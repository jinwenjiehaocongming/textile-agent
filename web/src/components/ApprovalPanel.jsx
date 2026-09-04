import { Fragment, useCallback, useEffect, useState } from 'react'
import { decideApproval, fetchPending } from '../api'

/**
 * 订单审批面板（销售经理使用，仅 role=admin 可见）。
 * 设计语言与 DataTable.jsx 保持一致：rounded-xl 卡片 + border-slate-800 分隔
 * + slate-800/70 表头 + brand 蓝 accent + 语义色按钮（emerald 通过 / rose 拒绝）。
 */

function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(Number(ts) * 1000)
  return d.toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  })
}

function fmtPrice(v) {
  return v === undefined || v === null || v === '' ? '—' : `¥${v}`
}

export default function ApprovalPanel() {
  const [pending, setPending] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState('')       // 正在审批的 thread_id
  const [rejectingId, setRejectingId] = useState('') // 展开拒绝理由输入的 thread_id
  const [rejectReason, setRejectReason] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setPending(await fetchPending())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const onDecide = useCallback(async (threadId, approved) => {
    setBusyId(threadId)
    setError('')
    try {
      const res = await decideApproval(approved ? 'approve' : 'reject', threadId, approved ? '' : rejectReason.trim())
      if (!res.ok) throw new Error(res.error || '审批失败')
      setPending((prev) => prev.filter((p) => p.thread_id !== threadId))
      setRejectingId('')
      setRejectReason('')
    } catch (e) {
      setError(e.message)
    } finally {
      setBusyId('')
    }
  }, [rejectReason])

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
      {/* 标题栏：与侧栏品牌风格一致 */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-[15px] font-semibold tracking-tight text-slate-50">订单审批</h2>
          <p className="mt-0.5 text-[11px] text-slate-500">待人工确认的下单请求，审批通过后订单落库</p>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="rounded-lg border border-slate-700 bg-slate-800/60 px-3 py-1.5 text-[12px] font-medium text-slate-300 transition-colors hover:bg-slate-800 disabled:opacity-40"
        >
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>

      {error && (
        <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-2.5 text-[12px] text-rose-300">
          {error}
        </div>
      )}

      {!loading && pending.length === 0 && !error && (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-slate-800 py-14 text-center">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-slate-800/80 text-slate-400">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 11l3 3L22 4" />
              <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
            </svg>
          </div>
          <p className="mt-3 text-[13px] font-medium text-slate-300">暂无待审批订单</p>
          <p className="mt-1 text-[12px] text-slate-500">客户发起下单并挂起后，会出现在这里</p>
        </div>
      )}

      {pending.length > 0 && (
        <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900">
          <div className="thin-scroll overflow-x-auto">
            <table className="w-full min-w-[640px] text-[12px]">
              <thead>
                <tr className="border-b border-slate-800 bg-slate-800/70 text-left text-[11px] uppercase tracking-wide text-slate-400">
                  <th className="px-3 py-2 font-medium">提交时间</th>
                  <th className="px-3 py-2 font-medium">产品</th>
                  <th className="px-3 py-2 font-medium">颜色</th>
                  <th className="px-3 py-2 text-right font-medium">数量</th>
                  <th className="px-3 py-2 text-right font-medium">总价</th>
                  <th className="px-3 py-2 font-medium">联系方式</th>
                  <th className="px-3 py-2 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {pending.map((p) => {
                  const d = p.draft || {}
                  const busy = busyId === p.thread_id
                  return (
                    <Fragment key={p.thread_id}>
                      <tr className="border-b border-slate-800 last:border-0 hover:bg-brand-500/10 transition-colors">
                        <td className="whitespace-nowrap px-3 py-2.5 text-slate-400">{fmtTime(p.created_at)}</td>
                        <td className="px-3 py-2.5 font-medium text-slate-100">{d.product_name || '—'}</td>
                        <td className="px-3 py-2.5 text-slate-300">{d.color || '—'}</td>
                        <td className="px-3 py-2.5 text-right text-slate-300">{d.quantity ?? '—'} 米</td>
                        <td className="px-3 py-2.5 text-right font-semibold text-brand-300">{fmtPrice(d.total)}</td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-slate-400">
                          {[d.phone, d.address].filter(Boolean).join(' · ') || '—'}
                        </td>
                        <td className="px-3 py-2.5">
                          <div className="flex items-center justify-end gap-1.5">
                            <button
                              onClick={() => onDecide(p.thread_id, true)}
                              disabled={!!busyId}
                              className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-[11px] font-medium text-emerald-300 transition-colors hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-40 active:scale-95"
                            >
                              {busy ? '处理中…' : '通过'}
                            </button>
                            <button
                              onClick={() => { setRejectingId(rejectingId === p.thread_id ? '' : p.thread_id); setRejectReason('') }}
                              disabled={!!busyId}
                              className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-2.5 py-1 text-[11px] font-medium text-rose-300 transition-colors hover:bg-rose-500/20 disabled:cursor-not-allowed disabled:opacity-40 active:scale-95"
                            >
                              拒绝
                            </button>
                          </div>
                        </td>
                      </tr>
                      {rejectingId === p.thread_id && (
                        <tr className="border-b border-slate-800 bg-slate-800/40 last:border-0">
                          <td colSpan={7} className="px-3 py-2.5">
                            <div className="flex items-center gap-2">
                              <input
                                value={rejectReason}
                                onChange={(e) => setRejectReason(e.target.value)}
                                placeholder="填写拒绝原因（可选），将反馈给客户"
                                className="flex-1 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-[12px] text-slate-100 outline-none transition-colors placeholder:text-slate-500 focus:border-brand-400 focus:ring-2 focus:ring-brand-500/20"
                              />
                              <button
                                onClick={() => onDecide(p.thread_id, false)}
                                disabled={!!busyId}
                                className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-2.5 py-1.5 text-[11px] font-medium text-rose-300 transition-colors hover:bg-rose-500/20 disabled:opacity-40 active:scale-95"
                              >
                                确认拒绝
                              </button>
                              <button
                                onClick={() => setRejectingId('')}
                                className="rounded-lg border border-slate-700 bg-slate-800/60 px-2.5 py-1.5 text-[11px] font-medium text-slate-300 transition-colors hover:bg-slate-800"
                              >
                                取消
                              </button>
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
        </div>
      )}
    </div>
  )
}
