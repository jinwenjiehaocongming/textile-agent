/**
 * 前端刷新逻辑测试（零依赖，node 直接跑：npm test）
 * ==============================================
 * 测的是真实实现（import '../src/api.js'），不是复刻品 —— 这段逻辑最容易写错，
 * 又最难靠肉眼在浏览器里看出来：
 *
 * 1. 并发 3 个请求同时 401 → **只打一次** /auth/refresh（单飞）。
 *    若各打一次，第二个请求会拿已被轮换作废的旧 refresh → 服务端判"重用"
 *    → 整个会话被注销（用户莫名被登出）。
 * 2. 刷新成功后：新 access/refresh 写回 sessionStorage，3 个请求都用新 access 重放。
 * 3. 刷新失败（被撤销/过期）：清空本地凭证 + 派发 auth:expired（App 据此回登录页）。
 * 4. 改密码：服务端会全端下线但给当前设备换发新凭证 → 前端**必须写回**，
 *    否则当前标签页会立刻被自己踢下线。
 */
const calls = []
let refreshCount = 0, tokenSeq = 0
const store = new Map()
globalThis.window = {
  sessionStorage: {
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  },
  dispatchEvent: (e) => calls.push({ type: 'event', name: e.type }),
}
globalThis.Event = class { constructor(type) { this.type = type } }

globalThis.fetch = async (url, opts = {}) => {
  const auth = (opts.headers || {}).Authorization || ''
  calls.push({ url, auth, body: opts.body })
  if (url.endsWith('/auth/refresh')) {
    refreshCount++
    await new Promise((r) => setTimeout(r, 30))          // 模拟网络延迟，制造并发窗口
    const rt = JSON.parse(opts.body).refresh_token
    if (rt !== 'RT-valid') return { ok: false, status: 401, json: async () => ({}) }
    tokenSeq++
    return { ok: true, status: 200, json: async () => ({ access_token: `AT-${tokenSeq}`, refresh_token: `RT-new` }) }
  }
  if (url.endsWith('/auth/change-password')) {
    tokenSeq++
    return { ok: true, status: 200, json: async () => ({ access_token: `AT-${tokenSeq}`, refresh_token: `RT-after-pw` }) }
  }
  // 受保护接口：旧 access 一律 401，新 access 放行
  if (auth === 'Bearer AT-0') return { ok: false, status: 401, text: async () => 'expired' }
  return { ok: true, status: 200, json: async () => ({ ok: true, auth }) }
}

const api = await import('../src/api.js')
const assert = (cond, msg) => { if (!cond) { console.error('❌', msg); process.exit(1) } console.log('✅', msg) }
// calls 里既有 HTTP 调用也有 window 事件，筛 HTTP 时统一走这个（事件没有 url）
const httpCalls = (suffix) => calls.filter((x) => !x.type && x.url && x.url.endsWith(suffix))

// ── 1. 并发 3 个 401 请求 → 只打一次刷新（单飞）──
store.set('hongrun_token', 'AT-0'); store.set('hongrun_refresh', 'RT-valid')
const [a, b, c] = await Promise.all([
  api.fetchMe(), api.fetchOrders(), api.fetchSessions(),
])
assert(refreshCount === 1, `并发 3 个 401 只触发 1 次 /auth/refresh（实际 ${refreshCount} 次）`)
assert(store.get('hongrun_token') === 'AT-1', '新 access 已写回 sessionStorage')
assert(store.get('hongrun_refresh') === 'RT-new', '轮换后的 refresh 已写回（否则下次刷新就是重用）')
const retried = [...httpCalls('/orders'), ...httpCalls('/sessions'), ...httpCalls('/me')]
assert(retried.filter((x) => x.auth === 'Bearer AT-1').length === 3, '3 个请求都用新 access 重放过')

// ── 2. refresh 失效 → 清本地 + 派发 auth:expired ──
store.set('hongrun_token', 'AT-0'); store.set('hongrun_refresh', 'RT-broken')
const before = refreshCount
await api.fetchMe()
assert(refreshCount === before + 1, '失效时又尝试了一次刷新')
assert(!store.get('hongrun_token') && !store.get('hongrun_refresh'), '刷新失败 → 本地凭证被清空')
assert(calls.some((x) => x.type === 'event' && x.name === 'auth:expired'), '派发了 auth:expired（App 据此回登录页）')
console.log('\n前端单飞刷新：全部通过')

// ── 3. 改密码：新凭证必须写回（否则当前标签页把自己登出）──
store.set('hongrun_token', 'AT-9'); store.set('hongrun_refresh', 'RT-live')
await api.changePassword('old-pass', 'new-pass-123')
const pwCall = httpCalls('/auth/change-password').pop()
assert(JSON.parse(pwCall.body).old_password === 'old-pass', '旧密码按契约放进 body（不走 URL，避免进 access log）')
assert(pwCall.auth === 'Bearer AT-9', '改密码请求带上了当前 access')
assert(store.get('hongrun_refresh') === 'RT-after-pw', '改密后新 refresh 已写回（否则当前设备也被下线）')
assert(store.get('hongrun_token') !== 'AT-9', '改密后新 access 已写回')

console.log('\n前端单飞刷新 + 改密码凭证写回：全部通过')
