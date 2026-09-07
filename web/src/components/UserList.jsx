import { useCallback, useEffect, useState } from 'react'

import { fetchAdminUsers } from '../api'

/**
 * 已注册用户（管理员视图，2026-09）
 * 只读列表：登录名 / 显示名 / 角色 / 状态 / 注册时间。
 * 接口 require_admin 兜底（普通客户 403）；响应绝不含 password_hash。
 */

function RoleBadge({ role }) {
  const admin = role === 'admin'
  return (
    <span
      className={`rounded-full border px-2 py-0.5 text-[10px] font-medium whitespace-nowrap ${
        admin
          ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
          : 'border-white/10 bg-white/[0.06] text-slate-400'
      }`}
    >
      {admin ? '管理员' : '客户'}
    </span>
  )
}

function StatusDot({ status }) {
  const active = status === 'active'
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-[11px] text-slate-400">
      <span className={`h-1.5 w-1.5 rounded-full ${active ? 'bg-emerald-400' : 'bg-rose-400'}`} />
      {active ? '正常' : '已禁用'}
    </span>
  )
}

function fmtTime(ts) {
  if (!ts) return '—'
  return String(ts).slice(0, 16).replace('T', ' ')
}

export default function UserList() {
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setUsers(await fetchAdminUsers())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-[15px] font-semibold tracking-tight text-slate-50">已注册用户</h2>
          <p className="mt-0.5 text-[11px] text-slate-400">
            共 {users.length} 个账号 · 密码仅存 bcrypt 哈希，列表永不回传
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

      {error && (
        <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-2.5 text-[12px] text-rose-300">
          {error}
        </div>
      )}

      {!loading && users.length === 0 && !error && (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-white/10 py-16 text-center">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl border border-brand-300/25 bg-brand-400/15 text-brand-200 backdrop-blur-md">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
              <circle cx="9" cy="7" r="4" />
              <path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" />
            </svg>
          </div>
          <p className="mt-3 text-[13px] font-medium text-slate-300">还没有注册用户</p>
          <p className="mt-1 text-[12px] text-slate-400">开放注册开启后，新账号会出现在这里</p>
        </div>
      )}

      {users.length > 0 && (
        <div className="overflow-hidden rounded-xl border border-white/10 bg-white/[0.04] shadow-panel-sm">
          <div className="thin-scroll overflow-x-auto">
            <table className="w-full min-w-[560px] text-[12px]">
              <thead>
                <tr className="border-b border-white/10 bg-white/[0.05] text-left text-[11px] uppercase tracking-wide text-slate-400">
                  <th className="px-3 py-2 font-medium">登录名</th>
                  <th className="px-3 py-2 font-medium">显示名</th>
                  <th className="px-3 py-2 font-medium">角色</th>
                  <th className="px-3 py-2 font-medium">状态</th>
                  <th className="px-3 py-2 font-medium">注册时间</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.username} className="border-b border-white/10 last:border-0 hover:bg-white/[0.04] transition-colors">
                    <td className="px-3 py-2.5 font-medium text-slate-100">{u.username}</td>
                    <td className="px-3 py-2.5 text-slate-300">{u.display_name || '—'}</td>
                    <td className="px-3 py-2.5"><RoleBadge role={u.role} /></td>
                    <td className="px-3 py-2.5"><StatusDot status={u.status} /></td>
                    <td className="px-3 py-2.5 text-slate-400">{fmtTime(u.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
