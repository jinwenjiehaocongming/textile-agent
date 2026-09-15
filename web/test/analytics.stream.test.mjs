/**
 * 分析流式解析测试（跑真实 api.js，不依赖浏览器）
 * ==============================================
 * 重点验 SSE 分帧缓冲：真实网络下**一个事件可能被拆到多个 chunk**，
 * 也可能一个 chunk 里挤多个事件。分帧写错的表现是"事件丢一半"或"JSON 解析失败"，
 * 而且只在网络抖动时偶发——所以必须用测试钉住。
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

const frames = [
  'data: {"type":"start","question":"退款率"}\n\n',
  'data: {"type":"plan","steps":[{"step":"按颜色统计","status":"pending"}]}\n\n',
  'data: {"type":"sql","sql":"SELECT color FROM orders"}\n\n',
  'data: {"type":"rows","ok":true,"columns":["color"],"rows":[["黑色"]],"row_count":1}\n\n',
  'data: {"type":"report","content":"黑色最多","notes":["数据说明：含演示数据"]}\n\n',
  'data: {"type":"done","elapsed_ms":1234,"steps":1,"sql_count":1}\n\n',
]
globalThis.fetch = async (url, opts = {}) => {
  calls.push({ url, body: opts.body })
  const encoder = new TextEncoder()
  // 故意把第 2 个事件切成两半（模拟 TCP 分片）+ 一次塞两个事件
  const parts = [
    frames[0] + frames[1].slice(0, 18),
    frames[1].slice(18) + frames[2] + frames[3],
    frames[4] + frames[5],
  ]
  const stream = new ReadableStream({
    start(controller) {
      for (const p of parts) controller.enqueue(encoder.encode(p))
      controller.close()
    },
  })
  return { ok: true, status: 200, body: stream, json: async () => ({}) }
}

const api = await import('../src/api.js')
const assert = (cond, msg) => { if (!cond) { console.error('❌', msg); process.exit(1) } console.log('✅', msg) }

store.set('hongrun_token', 'AT-x')
const got = []
await api.streamAnalytics('退款率最高的颜色', {
  maxSteps: 1,
  onEvent: (e) => got.push(e.type),
})

assert(got.join(',') === 'start,plan,sql,rows,report,done',
  `跨 chunk 的 SSE 分帧解析正确（收到 ${got.join(',')}）`)
assert(JSON.parse(calls[0].body).max_steps === 1, 'max_steps 按契约放进 body')
assert(calls[0].url.endsWith('/analytics/stream'), '打到 /analytics/stream')
console.log('\n分析流式解析：全部通过')
