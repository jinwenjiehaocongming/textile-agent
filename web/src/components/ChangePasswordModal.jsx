/**
 * 修改密码弹窗（2026-09）
 * ------------------------------------------------------------
 * 设计语言与登录页一致：圆角 2xl、白/6% 玻璃底、唯一 accent = brand 蓝、
 * label 在上错误在下、loading 态在按钮上。
 *
 * 交互要点（和后端语义对齐）：
 *  - 三个输入：旧密码 / 新密码 / 确认新密码；前端先做本地校验（少一次往返）
 *  - 成功后**不能跳登录页**：后端会全端下线，但给当前设备换发了一对新凭证，
 *    api.changePassword 已经写回 sessionStorage → 这里只提示"其他设备已下线"
 *  - 服务端错误原样展示：401 旧密码不正确 / 400 新密码不合规 / 429 失败次数过多
 */
import { useEffect, useRef, useState } from 'react'

import { changePassword } from '../api'

const MIN_PASSWORD_LEN = 6   // 与后端 src/users.py:MIN_PASSWORD_LEN 保持一致

function Field({ id, label, value, onChange, placeholder, error, autoComplete, disabled }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-[12.5px] font-medium text-slate-300">{label}</label>
      <input
        id={id}
        type="password"
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        autoComplete={autoComplete}
        disabled={disabled}
        className="w-full rounded-xl border border-white/[0.12] bg-white/[0.06] px-3 py-2.5 text-[13.5px] text-slate-100 outline-none transition-colors placeholder:text-slate-400/70 focus:border-brand-400 focus:ring-2 focus:ring-brand-500/25 disabled:opacity-50"
      />
      {error && <p className="text-[11px] text-rose-400">{error}</p>}
    </div>
  )
}

export default function ChangePasswordModal({ open, onClose }) {
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [errors, setErrors] = useState({})
  const [formError, setFormError] = useState('')
  const [done, setDone] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const firstFieldRef = useRef(null)

  // 每次打开都重置（避免上次的报错/输入残留）
  useEffect(() => {
    if (!open) return
    setOldPassword(''); setNewPassword(''); setConfirm('')
    setErrors({}); setFormError(''); setDone(false); setSubmitting(false)
    const t = setTimeout(() => firstFieldRef.current?.focus(), 30)
    return () => clearTimeout(t)
  }, [open])

  // Esc 关闭（提交中不关，避免用户以为没生效）
  useEffect(() => {
    if (!open) return
    const onKey = (e) => { if (e.key === 'Escape' && !submitting) onClose?.() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, submitting, onClose])

  if (!open) return null

  const validate = () => {
    const next = {}
    if (!oldPassword) next.oldPassword = '请输入当前密码'
    if (!newPassword) next.newPassword = '请输入新密码'
    else if (newPassword.length < MIN_PASSWORD_LEN) next.newPassword = `新密码至少 ${MIN_PASSWORD_LEN} 位`
    else if (newPassword === oldPassword) next.newPassword = '新密码不能与当前密码相同'
    if (confirm !== newPassword) next.confirm = '两次输入的新密码不一致'
    setErrors(next)
    return Object.keys(next).length === 0
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setFormError('')
    if (!validate()) return
    setSubmitting(true)
    try {
      await changePassword(oldPassword, newPassword)
      setDone(true)
    } catch (err) {
      setFormError(String(err.message || err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 px-4 backdrop-blur-sm"
      onMouseDown={(e) => { if (e.target === e.currentTarget && !submitting) onClose?.() }}
      role="dialog"
      aria-modal="true"
      aria-label="修改密码"
    >
      <div className="w-full max-w-[400px] animate-fade-up rounded-2xl border border-white/[0.14] bg-slate-900/85 p-6 shadow-panel backdrop-blur-2xl">
        <div className="mb-5 flex items-start justify-between gap-3">
          <div>
            <h2 className="text-[16px] font-semibold tracking-tight text-slate-50">修改密码</h2>
            <p className="mt-1 text-[11.5px] leading-relaxed text-slate-400">
              修改后其他设备会被登出，当前设备保持登录
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={submitting}
            title="关闭"
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-white/10 bg-white/[0.05] text-slate-400 transition-colors hover:text-slate-200 disabled:opacity-40"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
          </button>
        </div>

        {done ? (
          <div className="flex flex-col gap-4">
            <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-3.5 py-3 text-[12.5px] leading-relaxed text-emerald-200">
              密码已更新。<br />
              其他设备（含其他标签页）的登录已全部失效，当前设备不受影响。
            </div>
            <button
              onClick={onClose}
              className="flex h-11 w-full items-center justify-center rounded-full border border-brand-200/60 bg-brand-300/35 text-[13.5px] font-semibold text-[#0c1830] transition-all hover:bg-brand-300/50 active:scale-[0.98]"
            >
              知道了
            </button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="flex flex-col gap-4" noValidate>
            <div ref={firstFieldRef}>
              <Field
                id="oldPassword"
                label="当前密码"
                value={oldPassword}
                onChange={(e) => setOldPassword(e.target.value)}
                placeholder="用于确认是本人操作"
                autoComplete="current-password"
                error={errors.oldPassword}
                disabled={submitting}
              />
            </div>
            <Field
              id="newPassword"
              label="新密码"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder={`至少 ${MIN_PASSWORD_LEN} 位`}
              autoComplete="new-password"
              error={errors.newPassword}
              disabled={submitting}
            />
            <Field
              id="confirmPassword"
              label="确认新密码"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="再输入一次"
              autoComplete="new-password"
              error={errors.confirm}
              disabled={submitting}
            />

            {formError && (
              <div role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[12px] leading-relaxed text-rose-300">
                {formError}
              </div>
            )}

            <div className="mt-1 flex gap-2">
              <button
                type="button"
                onClick={onClose}
                disabled={submitting}
                className="h-11 flex-1 rounded-full border border-white/[0.12] bg-white/[0.05] text-[13.5px] font-medium text-slate-300 transition-colors hover:text-slate-100 disabled:opacity-40"
              >
                取消
              </button>
              <button
                type="submit"
                disabled={submitting || !oldPassword || !newPassword || !confirm}
                className="flex h-11 flex-[1.4] items-center justify-center gap-2 rounded-full border border-brand-200/60 bg-brand-300/35 text-[13.5px] font-semibold text-[#0c1830] shadow-panel-sm transition-all hover:bg-brand-300/50 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {submitting && (
                  <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" aria-hidden="true" />
                )}
                {submitting ? '提交中…' : '确认修改'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}
