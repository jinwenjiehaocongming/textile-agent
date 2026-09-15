/**
 * 图表映射测试（纯函数，node 直接跑：npm test）
 * ============================================
 * 为什么值得测：**图上的数字必须来自真实查询结果**。后端只让 LLM 选"图型 + 列名"，
 * 数据由程序填；这里验证"填进去的数据被一比一映射到 ECharts"，
 * 并且图型/边界（缺列、空数据、hbar 轴交换、饼图取值）都按预期处理。
 */
import assert from 'node:assert'

const { buildChartOption, formatNumber } = await import('../src/chartOption.js')
const ok = (cond, msg) => { if (!cond) { console.error('❌', msg); process.exit(1) } console.log('✅', msg) }

// ── 1. 数值格式化 ──
ok(formatNumber(8386080) === '838.61 万', `千万级 → 万（${formatNumber(8386080)}）`)
ok(formatNumber(1234.5678) === '1,234.57', `千分位 + 两位小数（${formatNumber(1234.5678)}）`)
ok(formatNumber(0.0978) === '0.0978', `小于 1 保留 4 位（${formatNumber(0.0978)}）`)
ok(formatNumber(42) === '42', `整数不带小数（${formatNumber(42)}）`)

// ── 2. 柱状图：分类 + 多个数值列 ──
const spec = {
  chart_type: 'bar', title: '各颜色退款率', x: 'color', series: ['orders', 'refunds'],
  data: [{ x: '黑色', orders: 276, refunds: 27 }, { x: '白色', orders: 66, refunds: 3 }],
}
let opt = buildChartOption(spec)
ok(opt.series.length === 2, '两个数值列 → 两个系列')
ok(opt.xAxis.data.join(',') === '黑色,白色', 'x 轴按数据顺序取分类')
ok(opt.series[0].data.join(',') === '276,66', '系列 0（orders）按行顺序映射')
ok(opt.series[1].data.join(',') === '27,3', '系列 1（refunds）按行顺序映射 —— 多系列对齐是最容易错的地方')
ok(opt.series[0].type === 'bar' && opt.title.text === '各颜色退款率', '图型与标题正确')

// ── 3. 趋势线 ──
opt = buildChartOption({ chart_type: 'line', title: 'GMV 趋势', x: 'month', series: ['gmv'],
                         data: [{ x: '2026-01', gmv: 346490 }, { x: '2026-02', gmv: 535340 }] })
ok(opt.series[0].type === 'line' && opt.series[0].smooth === true, 'line → 折线且平滑')

// ── 4. 横向柱状图：轴要交换（排名类问题） ──
opt = buildChartOption({ chart_type: 'hbar', title: '排名', x: 'pid', series: ['n'],
                         data: [{ x: 'P1', n: 20 }, { x: 'P2', n: 9 }] })
ok(opt.yAxis.type === 'category' && opt.xAxis.type === 'value', 'hbar → 分类轴落到 y（交换成功）')
ok(opt.yAxis.inverse === true, 'hbar 顺序自上而下 = 排名顺序')

// ── 5. 饼图：取第一个数值列，name 用 x ──
opt = buildChartOption({ chart_type: 'pie', title: '占比', x: 'category', series: ['gmv'],
                         data: [{ x: '化纤面料', gmv: 2776110 }, { x: '尼龙面料', gmv: 1636270 }] })
ok(opt.series[0].type === 'pie', 'pie → 饼图')
ok(opt.series[0].data[0].name === '化纤面料' && opt.series[0].data[0].value === 2776110, '饼图 name/value 映射正确')

// ── 6. 边界：空数据 / 缺字段一律返回 null（宁可不出图，也不出错图）──
ok(buildChartOption(null) === null, '空 spec → null')
ok(buildChartOption({ chart_type: 'bar', x: 'a', series: ['b'], data: [] }) === null, '空数据 → null')

console.log('\n图表映射：全部通过')
