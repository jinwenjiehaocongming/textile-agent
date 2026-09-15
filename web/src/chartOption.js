/**
 * 图表 spec → ECharts option（纯函数，零依赖，可 node 测试）
 * ========================================================
 * 后端（src/analytics/graph.py 的 charter 节点）产出的 spec 形状：
 *
 *   {
 *     chart_type: 'bar' | 'line' | 'pie' | 'hbar',
 *     title: '各颜色退款率排名',
 *     x: 'color',                    // 分类/时间轴列名
 *     series: ['refund_rate'],       // 数值列名（可多个）
 *     data: [{ x: '黑色', refund_rate: 0.097 }, ...],   // ← 数值来自真实查询结果
 *     source_step: '...', sql: 'SELECT ...'
 *   }
 *
 * 设计要点：**LLM 只决定图型与列名，数据由后端从查询结果里取**，所以这里
 * 只做"映射"，不做任何数据加工——图上不会出现模型编造的数字。
 */

// 与主界面一致的暗色调色板（唯一 accent = brand 蓝，其余为语义色）
const PALETTE = ['#7dd3fc', '#5eead4', '#fcd34d', '#fda4af', '#c4b5fd', '#a5b4fc', '#86efac', '#fdba74']

const AXIS_TEXT = '#94a3b8'
const SPLIT_LINE = 'rgba(255,255,255,0.08)'

export function formatNumber(v) {
  if (typeof v !== 'number' || Number.isNaN(v)) return String(v ?? '')
  const abs = Math.abs(v)
  if (abs >= 1e8) return (v / 1e8).toFixed(2) + ' 亿'
  if (abs >= 1e4) return (v / 1e4).toFixed(2) + ' 万'
  // 整数与小数量都带千分位（坐标轴/提示里更好读）
  return v.toLocaleString('zh-CN', {
    minimumFractionDigits: 0,
    maximumFractionDigits: Number.isInteger(v) ? 0 : (Math.abs(v) < 1 ? 4 : 2),
  })
}

const tooltip = {
  trigger: 'axis',
  backgroundColor: 'rgba(15,23,42,0.94)',
  borderColor: 'rgba(255,255,255,0.12)',
  textStyle: { color: '#e2e8f0', fontSize: 12 },
  valueFormatter: formatNumber,
}

const grid = { left: 8, right: 16, top: 34, bottom: 8, containLabel: true }

function baseOption(spec) {
  return {
    backgroundColor: 'transparent',
    color: PALETTE,
    title: {
      text: spec.title || '',
      left: 0, top: 0,
      textStyle: { color: '#e2e8f0', fontSize: 12.5, fontWeight: 600 },
    },
  }
}

/** 分类轴（bar / line / hbar 共用） */
function categoryAxisOption(spec, horizontal) {
  const cats = (spec.data || []).map((d) => String(d.x))
  const seriesNames = spec.series || []
  const option = {
    ...baseOption(spec),
    tooltip,
    legend: seriesNames.length > 1
      ? { top: 0, right: 0, textStyle: { color: AXIS_TEXT, fontSize: 11 }, itemWidth: 10, itemHeight: 8 }
      : undefined,
    grid,
    xAxis: {
      type: 'category',
      data: cats,
      axisLabel: { color: AXIS_TEXT, fontSize: 10.5, hideOverlap: true },
      axisLine: { lineStyle: { color: SPLIT_LINE } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: AXIS_TEXT, fontSize: 10.5, formatter: (v) => formatNumber(v) },
      splitLine: { lineStyle: { color: SPLIT_LINE } },
    },
    series: seriesNames.map((name) => ({
      name,
      type: spec.chart_type === 'line' ? 'line' : 'bar',
      smooth: spec.chart_type === 'line',
      barMaxWidth: 26,
      itemStyle: { borderRadius: spec.chart_type === 'line' ? 0 : [4, 4, 0, 0] },
      data: (spec.data || []).map((d) => d[name]),
    })),
  }
  if (horizontal) {
    // hbar：横向柱状图（排名类最好读）——交换 x/y 轴
    return {
      ...option,
      xAxis: { ...option.yAxis, splitLine: { lineStyle: { color: SPLIT_LINE } } },
      yAxis: { ...option.xAxis, inverse: true, splitLine: { show: false } },
      series: option.series.map((s) => ({ ...s, itemStyle: { borderRadius: [0, 4, 4, 0] } })),
    }
  }
  return option
}

/** 饼图：只取第一个数值列（占比类问题） */
function pieOption(spec) {
  const valueName = (spec.series || [])[0]
  return {
    ...baseOption(spec),
    tooltip: { ...tooltip, trigger: 'item', valueFormatter: formatNumber },
    legend: { bottom: 0, textStyle: { color: AXIS_TEXT, fontSize: 11 }, itemWidth: 10, itemHeight: 8 },
    series: [{
      type: 'pie',
      radius: ['38%', '62%'],
      center: ['50%', '48%'],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: 'rgba(15,23,42,0.6)', borderWidth: 2 },
      label: { color: AXIS_TEXT, fontSize: 10.5, formatter: '{b} {d}%' },
      data: (spec.data || []).map((d) => ({ name: String(d.x), value: d[valueName] })),
    }],
  }
}

export function buildChartOption(spec) {
  if (!spec || !Array.isArray(spec.data) || spec.data.length === 0) return null
  const type = spec.chart_type || 'bar'
  if (type === 'pie') return pieOption(spec)
  return categoryAxisOption(spec, type === 'hbar')
}
