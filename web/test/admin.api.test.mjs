/**
 * 管理端 API 契约测试（跑真实 api.js，不依赖浏览器）
 * ==================================================
 * 为什么值得钉住：**前后端路径/参数一旦不一致，表现是 404 或空列表**，
 * 而 UI 上只会看到"加载中…"或"没有符合条件的订单"——很难一眼看出是路径写错了。
 * 这里断言的是**请求契约**：URL、query 参数、HTTP 方法、body 字段。
 *
 * 覆盖：
 *   0. 路径前缀是 `/api`（生产前端打 `/api/xxx`，vite 开发代理剥掉前缀）——
 *      断言里带上它，前缀写错也逃不掉
 *   1. 全站订单列表：筛选/分页参数怎么拼（中文要编码、limit/offset 每页都要带）
 *   2. 工作台指标：GET 到正确的路径
 *   3. 订单状态流转：POST + body {status, note}，订单号要 encodeURIComponent
 *   4. 退款列表/审核：status 筛选 + decide 的 {approve, note}
 *   5. 非 2xx → 抛出后端 detail（前端要把"该工单已被处理"这类话原样展示给管理员）
 */
const calls = []
const store = new Map()
globalThis.window = {
  sessionStorage: {
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  },
  dispatchEvent: () => {},
}
globalThis.Event = class { constructor(t) { this.type = t } }

let nextResponse = { status: 200, body: {} }
globalThis.fetch = async (url, opts = {}) => {
  calls.push({ url, method: opts.method || 'GET', body: opts.body })
  const { status, body } = nextResponse
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  }
}

const {
  fetchAdminOrders, fetchAdminSummary, setOrderStatus,
  fetchAdminRefunds, decideRefund, setToken,
} = await import('../src/api.js')

setToken('test-token')
const last = () => calls[calls.length - 1]
let pass = 0
let fail = 0
function ok(cond, label) {
  if (cond) { pass += 1; console.log('✅', label) } else { fail += 1; console.log('❌', label) }
}

// ── 1. 全站订单：筛选 + 分页 ──
nextResponse = { status: 200, body: { orders: [], total: 0, limit: 20, offset: 0 } }
await fetchAdminOrders({ status: '已付款', keyword: '尼丝纺', limit: 20, offset: 40 })
{
  const u = last().url
  ok(u.startsWith('/api/admin/orders?'), '订单列表打到 /api/admin/orders')
  ok(last().method === 'GET', '订单列表用 GET')
  const q = new URLSearchParams(u.split('?')[1])
  ok(q.get('status') === '已付款', 'status 参数中文正确编码/解码')
  ok(q.get('keyword') === '尼丝纺', 'keyword 参数正确')
  ok(q.get('limit') === '20' && q.get('offset') === '40', '分页参数带上')
}

// 空筛选：不该出现 status= 空串（后端会当非法值 400）
await fetchAdminOrders({})
{
  const q = new URLSearchParams(last().url.split('?')[1])
  ok(!q.has('status') && !q.has('keyword'), '空筛选不发送空参数')
  ok(q.get('limit') === '50' && q.get('offset') === '0', '默认分页 50/0')
}

// ── 2. 工作台指标 ──
nextResponse = { status: 200, body: { orders_total: 602, trend_7d: [] } }
const summary = await fetchAdminSummary()
ok(last().url === '/api/admin/orders/summary', '指标打到 /api/admin/orders/summary')
ok(summary.orders_total === 602, '指标原样返回给调用方')

// ── 3. 订单状态流转 ──
nextResponse = { status: 200, body: { ok: true, order_no: 'ORD-1', from: '待付款', to: '已付款' } }
const r = await setOrderStatus('ORD-1', '已付款', '线下转账')
ok(last().url === '/api/admin/orders/ORD-1/status', '状态流转路径正确')
ok(last().method === 'POST', '状态流转用 POST')
ok(JSON.parse(last().body).status === '已付款', 'body 带 status')
ok(JSON.parse(last().body).note === '线下转账', 'body 带 note')
ok(r.to === '已付款', '返回结果透传')

// 订单号必须编码（否则带斜杠/空格就打到别的路由）
nextResponse = { status: 200, body: {} }
await setOrderStatus('ORD 1/2', '已取消')
ok(last().url === '/api/admin/orders/ORD%201%2F2/status', '订单号做 URL 编码')

// ── 4. 退款列表 + 审核 ──
nextResponse = { status: 200, body: { refunds: [{ id: 7, status: '待审核' }] } }
const refunds = await fetchAdminRefunds({ status: '待审核' })
ok(last().url.startsWith('/api/admin/refunds?'), '退款列表打到 /api/admin/refunds')
ok(new URLSearchParams(last().url.split('?')[1]).get('status') === '待审核', '退款状态筛选正确')
ok(refunds.length === 1 && refunds[0].id === 7, '退款列表取 body.refunds')

nextResponse = { status: 200, body: { ok: true, id: 7, status: '已驳回' } }
const decided = await decideRefund(7, false, '超过 7 天')
ok(last().url === '/api/admin/refunds/7/decide', '退款审核路径正确')
ok(last().method === 'POST', '退款审核用 POST')
ok(JSON.parse(last().body).approve === false, 'approve=false 正确序列化')
ok(JSON.parse(last().body).note === '超过 7 天', '审核备注带上')
ok(decided.status === '已驳回', '审核结果透传')

// ── 5. 错误处理：后端 detail 要能传到界面 ──
nextResponse = { status: 400, body: { detail: '该工单已被处理过（已通过）' } }
let msg = ''
try { await decideRefund(7, true, '') } catch (e) { msg = e.message }
ok(msg.includes('已被处理过'), '后端 detail 原样抛给调用方（管理员看得到原因）')

console.log(`\n管理端 API 契约：${pass} 通过${fail ? `，${fail} 失败` : '，全部通过'}`)
process.exit(fail ? 1 : 0)
