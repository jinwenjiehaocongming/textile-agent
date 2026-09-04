/**
 * 后端 API 封装。
 * 开发时走 Vite 代理 (/api -> http://127.0.0.1:8005)，避免 CORS。
 * 生产时可通过环境变量覆盖 VITE_API_BASE。
 */
const BASE = import.meta.env.VITE_API_BASE || '/api'

/**
 * 用户身份：浏览器本地持久化一个 user_id（对应企业微信 external_userid）。
 * 后端按请求头 X-User-Id 隔离历史/偏好/订单，互不串数据。
 */
const USER_ID_KEY = 'hongrun_user_id'
const TOKEN_KEY = 'hongrun_token'

export function getUserId() {
  let uid = localStorage.getItem(USER_ID_KEY)
  if (!uid || !/^[A-Za-z0-9_-]{1,64}$/.test(uid)) {
    uid = 'u_' + (crypto.randomUUID?.() || `u${Date.now()}_${Math.random().toString(36).slice(2, 10)}`).slice(0, 32)
    localStorage.setItem(USER_ID_KEY, uid)
  }
  return uid
}

// ── JWT 鉴权（2026-08）──────────────────────────────────────
// token 优先于 X-User-Id：有 token 后端认 token 里的身份，X-User-Id 仅作开发兜底
export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

function authHeaders(extra = {}) {
  const headers = { 'X-User-Id': getUserId(), ...extra }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

/** 开发/演示登录（后端 /dev/login，生产由微信 OAuth 取代）。切换身份即换 token。 */
export async function login({ role = 'customer', user_id = '' } = {}) {
  const resp = await fetch(`${BASE}/dev/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-User-Id': getUserId() },
    body: JSON.stringify({ role, user_id }),
  })
  if (!resp.ok) throw new Error(`登录失败 (${resp.status})`)
  const body = await resp.json()
  setToken(body.token)
  return body // { token, user_id, role }
}

/** 探测当前身份（前端据此显隐审批入口；授权仍以服务端为准） */
export async function fetchMe() {
  const resp = await fetch(`${BASE}/me`, { headers: authHeaders() })
  if (!resp.ok) return { user_id: 'guest', role: 'guest' }
  return resp.json()
}

/** 待审批订单列表（仅管理员，403 时抛错） */
export async function fetchPending() {
  const resp = await fetch(`${BASE}/approval/pending`, { headers: authHeaders() })
  if (!resp.ok) throw new Error(`加载待审批失败 (${resp.status})`)
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
  if (!resp.ok) throw new Error(`审批失败 (${resp.status})`)
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
export async function streamChat(message, { onStart, onReset, onToken, onNode, onDone, onError }) {
  // 超时保护：后端网关不稳定时，避免前端无限转圈
  const controller = new AbortController()
  const timeoutTimer = setTimeout(() => controller.abort(), 60000) // 60s 硬超时
  let resp
  try {
    resp = await fetch(`${BASE}/chat/stream`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ message }),
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

/** 拉取历史聊天记录 */
export async function fetchHistory() {
  const resp = await fetch(`${BASE}/history`, { headers: authHeaders() })
  if (!resp.ok) return []
  return resp.json()
}
