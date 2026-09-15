/**
 * 后端 API 封装。
 * 开发时走 Vite 代理 (/api -> http://127.0.0.1:8005)，避免 CORS。
 * 生产时可通过环境变量覆盖 VITE_API_BASE。
 *
 * 凭证体系（2026-09 路线②：短期 access + 可轮换 refresh）
 * ------------------------------------------------------
 * - **access token**：JWT，默认 15 分钟，无状态。放 sessionStorage，随请求带
 *   `Authorization: Bearer`；过期后无法续命，只能靠刷新换新的。
 * - **refresh token**：不透明串，服务端存 Redis，可撤销、每次刷新轮换。
 * - 过期处理：任何请求 401 → 单飞刷新 → 重放一次；刷新失败 → 清本地 + 通知
 *   App 回登录页（`auth:expired` 事件）。
 * - **单飞（single-flight）很重要**：并发 401 若各打一次 /auth/refresh，第二个
 *   请求会拿已被轮换作废的旧 refresh → 服务端判定"重用"→ 整个会话被注销。
 *
 * token 存 sessionStorage（每个标签页独立）：
 * 可开多个窗口分别登录不同账号（客户 + 管理员）互不覆盖。
 * localStorage 是同源所有标签共享的——后登录者会把别的窗口顶掉。
 * （取舍：sessionStorage 对 XSS 不设防。要更强的方案需把 refresh 放进
 *   HttpOnly Cookie + HTTPS，代价是多标签共享同一会话、并需额外做 CSRF 防护。）
 */
// 可选链：让本文件能被 node 直接 import 跑测试（node 里 import.meta.env 不存在；
// vite 构建时仍会把 import.meta.env 注入进来）
const BASE = import.meta.env?.VITE_API_BASE || '/api'

/**
 * 生成唯一 id（消息/会话用）。
 * crypto.randomUUID 只在 HTTPS 或 localhost（安全上下文）可用；
 * 公网 http://IP 部署时它不存在 → 用 getRandomValues 手工拼 uuid v4 兜底。
 */
export function uuid() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

const TOKEN_KEY = 'hongrun_token'
const REFRESH_KEY = 'hongrun_refresh'
const store = window.sessionStorage

/** 登录态彻底失效时派发（App 监听后回登录页） */
export const AUTH_EXPIRED_EVENT = 'auth:expired'

export function getToken() {
  return store.getItem(TOKEN_KEY) || ''
}

export function getRefreshToken() {
  return store.getItem(REFRESH_KEY) || ''
}

/** 保存服务端下发的双凭证（兼容只返回 token 的旧响应） */
export function setSession(body) {
  const access = (body && (body.access_token || body.token)) || ''
  if (access) store.setItem(TOKEN_KEY, access)
  if (body && body.refresh_token) store.setItem(REFRESH_KEY, body.refresh_token)
}

/** @deprecated 用 setSession；保留给旧调用点 */
export function setToken(token) {
  if (token) store.setItem(TOKEN_KEY, token)
  else store.removeItem(TOKEN_KEY)
}

export function clearSession() {
  store.removeItem(TOKEN_KEY)
  store.removeItem(REFRESH_KEY)
}

function authHeaders(extra = {}) {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}`, ...extra } : { ...extra }
}

// ── 刷新（单飞）─────────────────────────────────────────────
let refreshInFlight = null

async function doRefresh() {
  const refresh = getRefreshToken()
  if (!refresh) return false
  let resp
  try {
    resp = await fetch(`${BASE}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refresh }),
    })
  } catch {
    return false            // 网络异常：保留本地凭证，下次再试
  }
  if (!resp.ok) {
    // 401（被撤销/过期/重用）/ 503（会话存储不可用）：前者清本地；后者保留凭证等恢复
    if (resp.status === 401) clearSession()
    return false
  }
  setSession(await resp.json())
  return true
}

/** 单飞刷新：并发调用共享同一个请求 */
export function refreshSession() {
  if (!refreshInFlight) {
    refreshInFlight = doRefresh().finally(() => { refreshInFlight = null })
  }
  return refreshInFlight
}

/**
 * 带自动刷新的 fetch：401 → 刷新 → 重放一次。
 * 刷新失败会清空本地凭证并派发 `auth:expired`（由 App 统一回登录页）。
 */
export async function authFetch(path, opts = {}) {
  const { retryOn401 = true, headers, ...rest } = opts
  const send = () => fetch(`${BASE}${path}`, { ...rest, headers: authHeaders(headers || {}) })

  let resp = await send()
  if (resp.status === 401 && retryOn401) {
    const rotated = await refreshSession()
    if (rotated) {
      resp = await send()                      // 用新 access 重放
    } else {
      clearSession()
      window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT))
    }
  }
  return resp
}

/** 统一取后端错误文案（FastAPI detail 可能是字符串/数组） */
async function readError(resp) {
  try {
    const body = await resp.json()
    if (typeof body?.detail === 'string') return body.detail
    if (Array.isArray(body?.detail) && body.detail[0]?.msg) return body.detail[0].msg
  } catch { /* ignore */ }
  return `请求失败 (${resp.status})`
}

/** 登录：用户名 + 密码 → {token, user_id, role, display_name} */
export async function login(username, password) {
  const resp = await fetch(`${BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!resp.ok) throw new Error(await readError(resp))
  const body = await resp.json()
  setSession(body)
  return body
}

/** 注册（后端强制 customer 角色）：成功即自动登录 */
export async function register({ username, password, display_name = '' }) {
  const resp = await fetch(`${BASE}/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password, display_name }),
  })
  if (!resp.ok) throw new Error(await readError(resp))
  const body = await resp.json()
  setSession(body)
  return body
}

/**
 * 登出：**先清本地再撤销服务端会话**。
 * 服务端那一半不能省：否则被拷走的 refresh 在空闲期内仍是活凭证
 * （旧实现"服务端无状态，无需调接口"的注释，在引入 refresh 后就不成立了）。
 */
export async function logout() {
  const refresh = getRefreshToken()
  clearSession()
  if (!refresh) return
  try {
    await fetch(`${BASE}/auth/logout`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refresh }),
    })
  } catch { /* 网络不可用：本地已登出，服务端会话会随空闲期过期 */ }
}

/** 探测当前身份 {user_id, role, username, display_name}；未登录/失效 → null。
 *  带自动刷新：access 过期但 refresh 还在 → 静默换新，用户无感。 */
export async function fetchMe() {
  const resp = await authFetch('/me')
  if (!resp.ok) return null
  return resp.json()
}

// ── 管理员数据分析（2026-09）────────────────────────────────

/** 快捷问题（这个 Agent 能回答什么） */
export async function fetchAnalyticsExamples() {
  const resp = await authFetch('/analytics/examples')
  if (!resp.ok) throw new Error(await readError(resp))
  return (await resp.json()).examples || []
}

/**
 * 流式分析：一句话 → 规划 → 只读 SQL → 结论 → 图表。
 *
 * SSE 事件类型（后端 src/analytics/graph.py 定义）：
 *   start / node / plan / sql / rows / critic / retry / report / chart / done / error
 * 与 chat 的流不同，这里**不重放**：分析很贵（一次多轮 LLM），401 只刷新并提示重试。
 */
export async function streamAnalytics(question, { maxSteps = 0, onEvent, signal } = {}) {
  let resp = await fetch(`${BASE}/analytics/stream`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ question, max_steps: maxSteps }),
    signal,
  })
  if (resp.status === 401 && await refreshSession()) {
    resp = await fetch(`${BASE}/analytics/stream`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ question, max_steps: maxSteps }),
      signal,
    })
  }
  if (!resp.ok) {
    let detail = ''
    try { detail = (await resp.json()).detail || '' } catch { /* ignore */ }
    throw new Error(detail || `请求失败 (${resp.status})`)
  }
  if (!resp.body) throw new Error('浏览器不支持流式读取')

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const handle = (raw) => {
    for (const line of raw.split('\n')) {
      if (!line.startsWith('data:')) continue
      const payload = line.slice(5).trim()
      if (!payload) continue
      try { onEvent?.(JSON.parse(payload)) } catch { /* 忽略坏事件 */ }
    }
  }
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        handle(buffer.slice(0, idx))
        buffer = buffer.slice(idx + 2)
      }
    }
    if (buffer.trim()) handle(buffer)
  } finally {
    reader.releaseLock()
  }
}

/** 我的登录会话列表（会话管理 / "我在哪些设备登着"） */
export async function fetchAuthSessions() {
  const resp = await authFetch('/auth/sessions')
  if (!resp.ok) throw new Error(await readError(resp))
  const body = await resp.json()
  return body.sessions || []
}

/**
 * 修改密码。
 *
 * 服务端行为：验旧密码 → 改哈希 → **全端下线**（其他设备的 refresh 立刻失效，
 * 已签发的 access 也因 token_version 自增而失效）→ 给**当前设备**换发一对新凭证。
 * 所以这里必须把新凭证写回 sessionStorage，否则当前标签页自己也会被登出。
 */
export async function changePassword(oldPassword, newPassword) {
  const resp = await authFetch('/auth/change-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  })
  if (!resp.ok) throw new Error(await readError(resp))
  const body = await resp.json()
  setSession(body)          // ★ 不写回 = 自己把自己踢下线
  return body
}

/** 所有设备一起下线（怀疑账号被盗时的第一反应） */
export async function logoutAll() {
  const resp = await authFetch('/auth/logout-all', { method: 'POST' })
  clearSession()
  if (!resp.ok) throw new Error(await readError(resp))
  return resp.json()
}

/** 踢掉某个登录会话（其他设备/标签） */
export async function revokeAuthSession(sid) {
  const resp = await authFetch(`/auth/sessions/${encodeURIComponent(sid)}`, { method: 'DELETE' })
  if (!resp.ok) throw new Error(await readError(resp))
  return resp.json()
}

/** 待审批订单列表（仅管理员，403 时抛错） */
export async function fetchPending() {
  const resp = await authFetch('/approval/pending')
  if (!resp.ok) throw new Error(await readError(resp))
  const body = await resp.json()
  return body.pending || []
}

/**
 * 审批：通过 / 拒绝（仅管理员）。
 * 优先传 approvalId（精确到那一张审批单）；threadId 作为兼容/兜底。
 */
export async function decideApproval(action, threadId, reason = '', approvalId = '') {
  const resp = await authFetch(`/approval/${action}`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ thread_id: threadId, approval_id: approvalId, reason }),
  })
  if (!resp.ok) throw new Error(await readError(resp))
  return resp.json()
}

/**
 * 流式发送消息，逐 token 回调。
 * @param {string} message
 * @param {() => void} onStart
 * @param {() => void} onReset         新回复开始，前端应清空当前气泡
 * @param {(token: string) => void} onToken
 * @param {(evt: {node: string, label: string, detail: string}) => void} onNode  图节点执行过程
 * @param {(full: string, data?: object) => void} onDone
 * @param {(err: string) => void} onError
 */
export async function streamChat(message, { sessionId = '', onStart, onReset, onToken, onNode, onDone, onError }) {
  // 超时保护：后端网关不稳定时，避免前端无限转圈
  const controller = new AbortController()
  const timeoutTimer = setTimeout(() => controller.abort(), 60000) // 60s 硬超时
  const openStream = () => fetch(`${BASE}/chat/stream`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ message, session_id: sessionId }),
    signal: controller.signal,
  })
  let resp
  try {
    resp = await openStream()
    if (resp.status === 401 && await refreshSession()) {
      resp = await openStream()   // access 过期（SSE 无法中途续传）→ 静默刷新后重发一次
    }
  } catch (e) {
    clearTimeout(timeoutTimer)
    onError?.(e.name === 'AbortError' ? '请求超时（60秒），请稍后重试' : `网络异常: ${e.message}`)
    return
  }
  clearTimeout(timeoutTimer)

  if (!resp.ok) {
    const text = await resp.text().catch(() => '')
    onError?.(`请求失败 (${resp.status}): ${text || '服务不可用'}`)
    return
  }
  if (!resp.body) {
    onError?.('浏览器不支持流式读取')
    return
  }

  onStart?.()

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // SSE 事件以空行分隔
      let idx
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const rawEvent = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        for (const line of rawEvent.split('\n')) {
          if (!line.startsWith('data:')) continue
          const data = line.slice(5).trim()
          if (!data) continue
          try {
            const evt = JSON.parse(data)
            if (evt.type === 'start') continue
            if (evt.type === 'reset') {
              onReset?.()
            } else if (evt.type === 'token' && evt.content) {
              onToken?.(evt.content)
            } else if (evt.type === 'node') {
              onNode?.(evt)
            } else if (evt.type === 'done' || evt.type === 'pending') {
              onDone?.(evt.content, evt.data)
            } else if (evt.type === 'error') {
              onError?.(evt.content)
            }
          } catch {
            // 忽略无法解析的事件
          }
        }
      }
    }
    // 流结束时若缓冲里还有残余事件，处理最后一段
    if (buffer.trim()) {
      const line = buffer.trim().replace(/^data:\s*/, '')
      try {
        const evt = JSON.parse(line)
        if (evt.type === 'reset') onReset?.()
        else if (evt.type === 'token' && evt.content) onToken?.(evt.content)
        else if (evt.type === 'node') onNode?.(evt)
        else if (evt.type === 'done' || evt.type === 'pending') onDone?.(evt.content, evt.data)
        else if (evt.type === 'error') onError?.(evt.content)
      } catch { /* ignore */ }
    }
  } finally {
    reader.releaseLock()
  }
}

/** 拉取某会话的历史聊天记录 */
export async function fetchHistory(sessionId = '') {
  const q = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  const resp = await authFetch(`/history${q}`)
  if (!resp.ok) return []
  return resp.json()
}

// ── 多会话（2026-09）───────────────────────────────────────
export async function fetchSessions() {
  const resp = await authFetch('/sessions')
  if (!resp.ok) throw new Error(await readError(resp))
  const body = await resp.json()
  return body.sessions || []
}

export async function createSession(title = '') {
  const resp = await authFetch('/sessions', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ title }),
  })
  if (!resp.ok) throw new Error(await readError(resp))
  return resp.json() // { session_id, title }
}

export async function renameSession(sessionId, title) {
  const resp = await authFetch(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ title }),
  })
  if (!resp.ok) throw new Error(await readError(resp))
  return resp.json()
}

export async function deleteSession(sessionId) {
  const resp = await authFetch(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
  if (!resp.ok) throw new Error(await readError(resp))
  return resp.json()
}

/** 已注册用户列表（仅管理员；服务端 require_admin 兜底） */
export async function fetchAdminUsers() {
  const resp = await authFetch('/admin/users')
  if (!resp.ok) throw new Error(await readError(resp))
  const body = await resp.json()
  return body.users || []
}

/** 我的历史订单（行级隔离，只看自己的） */
export async function fetchOrders() {
  const resp = await authFetch('/orders')
  if (!resp.ok) throw new Error(await readError(resp))
  const body = await resp.json()
  return body.orders || []
}

// ── 管理端工作台（2026-09）：全站订单 / 状态流转 / 退款审核 ────
// 全部 require_admin（服务端兜底），前端只是入口。

/** 全站订单（管理端）：状态/关键词筛选 + 分页。返回 { orders, total, limit, offset } */
export async function fetchAdminOrders({ status = '', keyword = '', limit = 50, offset = 0 } = {}) {
  const q = new URLSearchParams()
  if (status) q.set('status', status)
  if (keyword) q.set('keyword', keyword)
  q.set('limit', String(limit))
  q.set('offset', String(offset))
  const resp = await authFetch(`/admin/orders?${q}`)
  if (!resp.ok) throw new Error(await readError(resp))
  return resp.json()
}

/** 工作台指标：待审批 / 待发货 / 待付款 / 退款待审 / 本月 GMV + 近 7 天趋势 */
export async function fetchAdminSummary() {
  const resp = await authFetch('/admin/orders/summary')
  if (!resp.ok) throw new Error(await readError(resp))
  return resp.json()
}

/** 订单状态流转（受限状态机：非法转换后端 400，前端按钮也按同一规则禁用） */
export async function setOrderStatus(orderNo, status, note = '') {
  const resp = await authFetch(`/admin/orders/${encodeURIComponent(orderNo)}/status`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ status, note }),
  })
  if (!resp.ok) throw new Error(await readError(resp))
  return resp.json()
}

/** 退款工单列表（管理端）；status 常用 '待审核' */
export async function fetchAdminRefunds({ status = '', limit = 50 } = {}) {
  const q = new URLSearchParams()
  if (status) q.set('status', status)
  q.set('limit', String(limit))
  const resp = await authFetch(`/admin/refunds?${q}`)
  if (!resp.ok) throw new Error(await readError(resp))
  const body = await resp.json()
  return body.refunds || []
}

/** 退款审核：通过 / 驳回（只能审一次，并发下 CAS 保证只有一个成功） */
export async function decideRefund(refundId, approve, note = '') {
  const resp = await authFetch(`/admin/refunds/${refundId}/decide`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ approve, note }),
  })
  if (!resp.ok) throw new Error(await readError(resp))
  return resp.json()
}
