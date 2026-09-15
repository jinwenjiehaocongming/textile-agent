/**
 * 端到端验证（需服务在 8005 跑）：真实分析请求 → 图表 spec → ECharts 服务端渲染 SVG。
 * 这不是回归测试（要 LLM + 服务），是"人肉验收"脚本：
 *   node test/render_chart_ssr.mjs [问题]
 * 输出：/tmp/analytics_chart.svg（可直接用浏览器打开看）
 */
import { writeFileSync } from 'node:fs'

const B = process.env.ANALYTICS_BASE || 'http://127.0.0.1:8005'
const question = process.argv[2] || '退款率最高的颜色是哪些？'
const token = process.env.ANALYTICS_TOKEN
if (!token) { console.error('需要 ANALYTICS_TOKEN（管理员 JWT）'); process.exit(1) }

const resp = await fetch(`${B}/analytics/stream`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
  body: JSON.stringify({ question, max_steps: 1 }),
})
if (!resp.ok) { console.error('请求失败', resp.status, await resp.text()); process.exit(1) }

let spec = null, report = ''
const reader = resp.body.getReader()
const dec = new TextDecoder()
let buf = ''
for (;;) {
  const { done, value } = await reader.read()
  if (done) break
  buf += dec.decode(value, { stream: true })
  let i
  while ((i = buf.indexOf('\n\n')) !== -1) {
    const raw = buf.slice(0, i); buf = buf.slice(i + 2)
    for (const line of raw.split('\n')) {
      if (!line.startsWith('data:')) continue
      const evt = JSON.parse(line.slice(5).trim())
      if (evt.type === 'chart' && evt.charts?.length) spec = evt.charts[0]
      if (evt.type === 'report') report = evt.content
    }
  }
}
if (!spec) { console.error('这次没产出图表'); process.exit(1) }
console.log(`问题：${question}`)
console.log(`结论：${report.slice(0, 120)}…`)
console.log(`图表：${spec.chart_type} 《${spec.title}》 x=${spec.x} series=${spec.series} 数据 ${spec.data.length} 行`)

// ECharts 服务端渲染（同一份 buildChartOption，不经过浏览器）
const echarts = await import('echarts')
const { buildChartOption } = await import('../src/chartOption.js')
const option = buildChartOption(spec)
const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width: 720, height: 300 })
chart.setOption(option)
const svg = chart.renderToSVGString()
chart.dispose()
const out = '/tmp/analytics_chart.svg'
writeFileSync(out, svg)
console.log(`✅ 已渲染 SVG：${out}（${svg.length} 字节，含 ${(svg.match(/<path|<rect|<text/g) || []).length} 个图形元素）`)
console.log('   首行数据点：', JSON.stringify(spec.data.slice(0, 3)))
