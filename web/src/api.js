/**
 * 后端 API 封装。
 * 开发时走 Vite 代理 (/api -> http://127.0.0.1:8005)，避免 CORS。
 * 生产时可通过环境变量覆盖 VITE_API_BASE。
 *
 * 鉴权（2026-09 账号体系）：身份一律来自 JWT（Authorization: Bearer <token>）。
 * 无 token / token 失效 → 后端 401；前端由 App 统一跳回登录页。
 *
 * token 存 sessionStorage（每个标签页独立）：
 * 可开多个窗口分别登录不同账号（客户 + 管理员）互不覆盖。
 * localStorage 是同源所有标签共享的——后登录者会把别的窗口顶掉。
 */
const BASE = import.meta.env.VITE_API_BASE || '/api'

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
const store = window.sessionStorage

export function getToken() {
  return store.getItem(TOKEN_KEY) || ''
}

export function setToken(token) {
  if (token) store.setItem(TOKEN_KEY, token)
  else store.removeItem(TOKEN_KEY)
}

export function clearSession() {
  store.removeItem(TOKEN_KEY)
}

function authHeaders(extra = {}) {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}`, ...extra } : { ...extra }
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
  setToken(body.token)
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
  setToken(body.token)
  return body
}

/** 登出：清本地 token（服务端无状态） */
export function logout() {
  clearSession()
}

/** 探测当前身份 {user_id, role, username, display_name}；未登录/失效 → null */
export async function fetchMe() {
  const resp = await fetch(`${BASE}/me`, { headers: authHeaders() })
  if (!resp.ok) return null
  return resp.json()
}

/** 待审批订单列表（仅管理员，403 时抛错） */
export async function fetchPending() {
  const resp = await fetch(`${BASE}/approval/pending`, { headers: authHeaders() })
  if (!resp.ok) throw new Error(await readError(resp))
  const body = await resp.json()
  return body.pending || []
}

/** 审批：通过 / 拒绝（仅管理员） */
export async function decideApproval(action, threadId, reason = '') {
  const resp = await fetch(`${BASE}/approval/${action}`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ thread_id: threadId, reason }),
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
  let resp
  try {
    resp = await fetch(`${BASE}/chat/stream`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ message, session_id: sessionId }),
      signal: controller.signal,
    })
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
  const resp = await fetch(`${BASE}/history${q}`, { headers: authHeaders() })
  if (!resp.ok) return []
  return resp.json()
}

// ── 多会话（2026-09）───────────────────────────────────────
export async function fetchSessions() {
  const resp = await fetch(`${BASE}/sessions`, { headers: authHeaders() })
  if (!resp.ok) throw new Error(await readError(resp))
  const body = await resp.json()
  return body.sessions || []
}

export async function createSession(title = '') {
  const resp = await fetch(`${BASE}/sessions`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ title }),
  })
  if (!resp.ok) throw new Error(await readError(resp))
  return resp.json() // { session_id, title }
}

export async function renameSession(sessionId, title) {
  const resp = await fetch(`${BASE}/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ title }),
  })
  if (!resp.ok) throw new Error(await readError(resp))
  return resp.json()
}

export async function deleteSession(sessionId) {
  const resp = await fetch(`${BASE}/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
  if (!resp.ok) throw new Error(await readError(resp))
  return resp.json()
}

/** 已注册用户列表（仅管理员；服务端 require_admin 兜底） */
export async function fetchAdminUsers() {
  const resp = await fetch(`${BASE}/admin/users`, { headers: authHeaders() })
  if (!resp.ok) throw new Error(await readError(resp))
  const body = await resp.json()
  return body.users || []
}

/** 我的历史订单（行级隔离，只看自己的） */
export async function fetchOrders() {
  const resp = await fetch(`${BASE}/orders`, { headers: authHeaders() })
  if (!resp.ok) throw new Error(await readError(resp))
  const body = await resp.json()
  return body.orders || []
}
