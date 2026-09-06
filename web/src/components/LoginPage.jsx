/**
 * 登录 / 注册页（2026-09 账号体系）
 * ------------------------------------------------------------
 * 设计语言与主界面保持一致（深色控制台）：
 *  - 底色 slate-950，卡片 slate-900/80 + border-white/[0.10] + rounded-2xl
 *  - 唯一 accent = brand 蓝（现有 tailwind brand 色板）
 *  - 圆角体系：按钮/输入 rounded-xl，卡片 rounded-2xl（沿用聊天区）
 *  - 表单规范：label 在上、错误在下、loading 态在按钮上（WCAG AA 对比）
 * 交互：登录/注册双态切换；注册后端强制 customer 角色（admin 由种子脚本建）。
 */
import { useState } from 'react'

import { login, register } from '../api'

const USERNAME_RE = /^[A-Za-z0-9_-]{3,32}$/

function BrandMark({ size = 'md' }) {
  const box = size === 'lg' ? 'h-12 w-12 rounded-2xl' : 'h-10 w-10 rounded-xl'
  const icon = size === 'lg' ? 26 : 20
  return (
    <div className={`flex items-center justify-center border border-brand-300/25 bg-gradient-to-b from-brand-400/25 to-brand-500/15 text-brand-50 shadow-panel-sm backdrop-blur-md ${box}`}>
      <svg width={icon} height={icon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 2a10 10 0 0 1 10 10c0 5-4 8-10 8-1.2 0-2.4-.2-3.4-.5L4 21l1.2-3.2A9.6 9.6 0 0 1 2 12 10 10 0 0 1 12 2Z" />
      </svg>
    </div>
  )
}

function Field({ id, label, type = 'text', value, onChange, placeholder, error, autoComplete, disabled }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-[13px] font-medium text-slate-300">{label}</label>
      <input
        id={id}
        type={type}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        autoComplete={autoComplete}
        disabled={disabled}
        className="w-full rounded-2xl border border-white/[0.12] bg-white/[0.06] px-3.5 py-2.5 shadow-panel-sm text-[14px] text-slate-100 outline-none transition-colors placeholder:text-slate-400/80 focus:border-brand-400 focus:ring-2 focus:ring-brand-500/25 disabled:opacity-50"
      />
      {error && <p className="text-[11px] text-rose-400">{error}</p>}
    </div>
  )
}

export default function LoginPage({ onSuccess }) {
  const [mode, setMode] = useState('login') // login | register
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState('')

  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')

  const [usernameError, setUsernameError] = useState('')
  const [passwordError, setPasswordError] = useState('')

  const switchMode = (next) => {
    if (next === mode || submitting) return
    setMode(next)
    setFormError('')
    setUsernameError('')
    setPasswordError('')
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setFormError('')
    setUsernameError('')
    setPasswordError('')

    // 客户端预校验（后端为最终权威）
    if (!USERNAME_RE.test(username)) {
      setUsernameError('用户名需为 3-32 位字母、数字、下划线或连字符')
      return
    }
    if (mode === 'register' && password.length < 6) {
      setPasswordError('密码至少 6 位')
      return
    }

    setSubmitting(true)
    try {
      const body = mode === 'login'
        ? await login(username, password)
        : await register({ username, password, display_name: displayName })
      onSuccess({ user_id: body.user_id, role: body.role, display_name: body.display_name, username: body.username })
    } catch (err) {
      // 401 用户名或密码错误 / 409 用户名已存在 / 400 格式问题
      setFormError(err.message || '操作失败，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  const isLogin = mode === 'login'

  return (
    <div className="relative flex min-h-[100dvh] w-full items-center justify-center overflow-hidden bg-transparent px-4">
      {/* 环境光：单一 brand 蓝，克制（无花哨渐变） */}
      <div aria-hidden="true" className="pointer-events-none absolute inset-0">
        <div className="absolute left-1/2 top-[-220px] h-[480px] w-[720px] -translate-x-1/2 rounded-full bg-brand-500/15 blur-[140px]" />
        <div className="absolute bottom-[-180px] left-1/2 h-[380px] w-[560px] -translate-x-1/2 rounded-full bg-brand-400/20 blur-[140px]" />
      </div>

      <div className="relative w-full max-w-[420px] animate-fade-up">
        {/* 品牌头 */}
        <div className="mb-6 flex flex-col items-center text-center">
          <BrandMark size="lg" />
          <h1 className="mt-4 font-display text-[22px] font-semibold tracking-tight text-slate-50">
            交易智能体
          </h1>
          <p className="mt-1.5 text-[13px] leading-relaxed text-slate-400">
            纺织面料 · 询价 下单 售后
          </p>
        </div>

        {/* 表单卡片 */}
        <div className="rounded-3xl border border-white/[0.14] bg-white/[0.06] p-7 shadow-panel backdrop-blur-2xl sm:p-8">
          {/* 登录 / 注册切换 */}
          <div role="tablist" aria-label="登录或注册" className="mb-6 grid grid-cols-2 gap-1 rounded-full border border-white/[0.12] bg-white/[0.05] p-1 shadow-panel-sm">
            {(['login', 'register']).map((m) => (
              <button
                key={m}
                role="tab"
                aria-selected={mode === m}
                onClick={() => switchMode(m)}
                disabled={submitting}
                className={`rounded-full py-2 text-[13px] font-medium transition-colors ${
                  mode === m
                    ? 'border border-brand-200/50 bg-brand-300/40 text-[#0d1a33] shadow-panel-sm'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {m === 'login' ? '登录' : '注册'}
              </button>
            ))}
          </div>

          <form onSubmit={handleSubmit} className="flex flex-col gap-4" noValidate>
            <Field
              id="username"
              label="用户名"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="3-32 位字母、数字、下划线或连字符"
              autoComplete="username"
              error={usernameError}
              disabled={submitting}
            />

            {!isLogin && (
              <Field
                id="displayName"
                label="昵称（选填）"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="展示在界面上的称呼"
                autoComplete="name"
                disabled={submitting}
              />
            )}

            <Field
              id="password"
              label="密码"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={isLogin ? '请输入密码' : '至少 6 位'}
              autoComplete={isLogin ? 'current-password' : 'new-password'}
              error={passwordError}
              disabled={submitting}
            />

            {formError && (
              <div role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[12px] leading-relaxed text-rose-300">
                {formError}
              </div>
            )}

            <button
              type="submit"
              disabled={submitting || !username || !password}
              className="mt-1 flex h-12 w-full items-center justify-center gap-2 rounded-full border border-brand-200/60 bg-brand-300/35 text-[14px] font-semibold text-[#0c1830] shadow-panel-sm backdrop-blur-xl transition-all hover:bg-brand-300/50 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
            >
              {submitting && (
                <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" aria-hidden="true" />
              )}
              {submitting
                ? (isLogin ? '登录中…' : '注册中…')
                : (isLogin ? '登录' : '注册并登录')}
            </button>
          </form>
        </div>

        {/* 底部说明 */}
        <p className="mt-5 text-center text-[11px] leading-relaxed text-slate-400">
          开放注册普通客户账号；管理员账号由部署方创建
        </p>
      </div>
    </div>
  )
}
