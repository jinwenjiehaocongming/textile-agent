/**
 * 管理员「数据分析」视图（2026-09）
 * ============================================================
 * 一句话 → 分析规划 → 只读 SQL → 结论 → 图表，全链路流式展示。
 *
 * 设计要点（都是为了"让管理员能复核，而不是只能相信"）：
 *  - **每一步都摊开**：规划了什么、生成了哪条 SQL、返回多少行、校验是否要求重试；
 *  - **SQL 可展开**：结论旁边永远能看到出处（图表也带 source_step / sql）；
 *  - **数字有出处**：后端会把"结论里查不到出处的数字"列进「数据局限声明」，
 *    这里原样展示，不隐藏；
 *  - **失败如实说**：某步没查成、结果被截断，都标出来，而不是给一个漂亮的错误结论。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import {
  GridComponent, LegendComponent, TitleComponent, TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

import { fetchAnalyticsExamples, streamAnalytics } from '../api'
import { buildChartOption, formatNumber } from '../chartOption'

// 按需注册（比整包小很多；仍然零 CDN）
echarts.use([BarChart, LineChart, PieChart, GridComponent, LegendComponent,
  TitleComponent, TooltipComponent, CanvasRenderer])

const NODE_ICON = {
  planner: '🧭', coder: '🧾', executor: '🔎', critic: '🔍', reporter: '📝', charter: '📊',
}

function Panel({ title, children, tone = 'slate' }) {
  const tones = {
    slate: 'border-white/10 bg-white/[0.03]',
    warn: 'border-amber-500/25 bg-amber-500/[0.07]',
    error: 'border-rose-500/30 bg-rose-500/[0.08]',
  }
  return (
    <section className={`rounded-xl border ${tones[tone]} px-3.5 py-3 backdrop-blur-md`}>
      {title && <h3 className="mb-2 text-[12px] font-semibold tracking-tight text-slate-300">{title}</h3>}
      {children}
    </section>
  )
}

/** 结果表：最多展示 12 行（完整结果可由 SQL 复核） */
function ResultTable({ evt }) {
  if (!evt) return null
  if (!evt.ok) {
    return (
      <Panel tone="error" title={`❌ 这一步没查成：${evt.step}`}>
        <p className="whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-rose-200">
          {evt.error}
        </p>
      </Panel>
    )
  }
  const rows = (evt.rows || []).slice(0, 12)
  return (
    <Panel title={`🔎 ${evt.step}　·　${evt.row_count} 行　·　${evt.elapsed_ms}ms${evt.truncated ? '　·　已截断' : ''}`}>
      <div className="thin-scroll overflow-x-auto">
        <table className="w-full min-w-max border-collapse text-[11.5px]">
          <thead>
            <tr className="text-left text-slate-400">
              {(evt.columns || []).map((c) => (
                <th key={c} className="border-b border-white/10 px-2 py-1 font-medium whitespace-nowrap">{c}</th>
              ))}
            </tr>
          </thead>
          <tbody className="text-slate-200">
            {rows.map((row, i) => (
              <tr key={i} className="odd:bg-white/[0.02]">
                {row.map((cell, j) => (
                  <td key={j} className="px-2 py-1 whitespace-nowrap tabular-nums">
                    {cell === null ? '—' : (typeof cell === 'number' ? formatNumber(cell) : String(cell))}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {evt.row_count > rows.length && (
        <p className="mt-1.5 text-[10.5px] text-slate-500">仅展示前 {rows.length} 行，共 {evt.row_count} 行（可展开 SQL 复核）</p>
      )}
    </Panel>
  )
}

/** 单个 ECharts 实例：spec 变化时只 setOption，不重建 */
function ChartCard({ spec }) {
  const ref = useRef(null)
  const inst = useRef(null)

  useEffect(() => {
    if (!ref.current) return undefined
    inst.current = echarts.init(ref.current, null, { renderer: 'canvas' })
    const onResize = () => inst.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      inst.current?.dispose()
      inst.current = null
    }
  }, [])

  useEffect(() => {
    const option = buildChartOption(spec)
    if (inst.current && option) inst.current.setOption(option, true)
    setTimeout(() => inst.current?.resize(), 0)
  }, [spec])

  return (
    <Panel title={`📊 ${spec.title}　·　${spec.chart_type}`}>
      <div ref={ref} className="h-[240px] w-full" />
      <details className="mt-1.5">
        <summary className="cursor-pointer text-[10.5px] text-slate-500 hover:text-slate-300">
          出处：{spec.source_step}
        </summary>
        <pre className="thin-scroll mt-1 overflow-x-auto rounded-lg bg-slate-950/60 p-2 font-mono text-[10.5px] text-slate-400">
          {spec.sql}
        </pre>
      </details>
    </Panel>
  )
}

export default function AnalyticsPanel() {
  const [examples, setExamples] = useState([])
  const [question, setQuestion] = useState('')
  const [fast, setFast] = useState(false)         // 快速模式：只跑 1 步（每步 ≈ 2 次 LLM 调用）
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')

  const [plan, setPlan] = useState([])
  const [steps, setSteps] = useState([])          // 流水线步骤条（节点事件）
  const [results, setResults] = useState([])      // 每步结果
  const [pendingSql, setPendingSql] = useState('') // 当前步骤的 SQL（结果到来前先显示）
  const [critic, setCritic] = useState('')
  const [report, setReport] = useState('')
  const [notes, setNotes] = useState([])
  const [charts, setCharts] = useState([])
  const [elapsed, setElapsed] = useState(0)
  const abortRef = useRef(null)

  useEffect(() => {
    // 管理员才拿得到（客户 403）；失败就静默，不影响面板本身
    fetchAnalyticsExamples().then(setExamples).catch(() => setExamples([]))
  }, [])

  const reset = useCallback(() => {
    setPlan([]); setSteps([]); setResults([]); setPendingSql('')
    setCritic(''); setReport(''); setNotes([]); setCharts([]); setElapsed(0); setError('')
  }, [])

  const run = useCallback(async (q) => {
    const text = (q ?? question).trim()
    if (!text || running) return
    reset()
    setRunning(true)
    setQuestion(text)
    const controller = new AbortController()
    abortRef.current = controller
    try {
      await streamAnalytics(text, {
        maxSteps: fast ? 1 : 2,
        signal: controller.signal,
        onEvent: (evt) => {
          switch (evt.type) {
            case 'node': setSteps((prev) => [...prev, evt]); break
            case 'plan': setPlan(evt.steps || []); break
            case 'sql': setPendingSql(evt.sql || ''); break
            case 'rows':
              setResults((prev) => [...prev, evt])
              setPendingSql('')
              break
            case 'critic': setCritic(evt.retry ? `校验未通过 — ${evt.reason}` : ''); break
            case 'retry': setCritic((c) => `${c}（正在重写 SQL，第 ${evt.attempt} 次尝试）`); break
            case 'report': setReport(evt.content || ''); setNotes(evt.notes || []); break
            case 'chart': setCharts(evt.charts || []); break
            case 'done': setElapsed(evt.elapsed_ms || 0); break
            case 'error': setError(evt.content || '分析失败'); break
            default: break
          }
        },
      })
    } catch (e) {
      if (e.name !== 'AbortError') setError(String(e.message || e))
    } finally {
      setRunning(false)
      abortRef.current = null
    }
  }, [question, running, fast, reset])

  const hasOutput = steps.length > 0 || report || results.length > 0
  const progress = useMemo(() => (steps.length ? steps[steps.length - 1].label : ''), [steps])

  return (
    <div className="flex flex-col gap-3 p-3.5">
      {/* 命令面板 */}
      <Panel>
        <div className="flex flex-col gap-2.5">
          <div className="flex items-end gap-2">
            <textarea
              rows={2}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); run() } }}
              placeholder="问一句业务问题，例如：最近的质量投诉有什么规律？集中在哪些产品？"
              disabled={running}
              className="thin-scroll flex-1 resize-none rounded-xl border border-white/[0.12] bg-white/[0.05] px-3 py-2 text-[13px] text-slate-100 outline-none transition-colors placeholder:text-slate-500 focus:border-brand-400 focus:ring-2 focus:ring-brand-500/25 disabled:opacity-60"
            />
            <button
              onClick={() => run()}
              disabled={running || !question.trim()}
              className="flex h-[52px] shrink-0 items-center gap-2 rounded-xl border border-brand-200/60 bg-brand-300/35 px-4 text-[13px] font-semibold text-[#0c1830] transition-all hover:bg-brand-300/50 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
            >
              {running && <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" />}
              {running ? '分析中…' : '开始分析'}
            </button>
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            {examples.map((ex) => (
              <button
                key={ex}
                onClick={() => run(ex)}
                disabled={running}
                className="rounded-full border border-white/10 bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-300 transition-colors hover:border-brand-400/40 hover:text-brand-200 disabled:opacity-40"
              >
                {ex}
              </button>
            ))}
            <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-[11px] text-slate-400">
              <input type="checkbox" checked={fast} onChange={(e) => setFast(e.target.checked)}
                     className="h-3 w-3 accent-brand-400" />
              快速模式（1 步，约 30 秒；标准模式 2 步，约 2 分钟）
            </label>
          </div>
        </div>
      </Panel>

      {error && (
        <Panel tone="error">
          <p className="text-[12px] leading-relaxed text-rose-200">{error}</p>
        </Panel>
      )}

      {!hasOutput && !running && !error && (
        <Panel>
          <p className="text-[12px] leading-relaxed text-slate-400">
            问一句中文，Agent 会<b className="text-slate-300">自己规划步骤</b>、生成
            <b className="text-slate-300">只读 SQL</b>（四层防护：只读角色 / 事务只读 / 语句校验 /
            结果封顶）、校验结果、写结论并出图表。<br />
            每一步的 SQL 与结果都可展开复核；结论里的数字若查无出处，会列在「数据局限声明」里。
          </p>
        </Panel>
      )}

      {/* 流水线 + 规划 */}
      {(running || steps.length > 0) && (
        <Panel title={`流水线${progress ? `　·　当前：${progress}` : ''}${elapsed ? `　·　总耗时 ${(elapsed / 1000).toFixed(1)}s` : ''}`}>
          <div className="flex flex-wrap items-center gap-1.5">
            {steps.map((s, i) => (
              <span key={i} className="inline-flex items-center gap-1 rounded-lg border border-white/10 bg-white/[0.05] px-2 py-1 text-[11px] text-slate-300">
                <span>{NODE_ICON[s.node] || '•'}</span>{s.label}
                {i === steps.length - 1 && running
                  ? <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand-300" />
                  : <span className="text-emerald-400">✓</span>}
              </span>
            ))}
          </div>
          {plan.length > 0 && (
            <ol className="mt-2 flex flex-col gap-1">
              {plan.map((p, i) => (
                <li key={i} className="flex items-start gap-1.5 text-[11.5px] text-slate-400">
                  <span className={p.status === 'done' ? 'text-emerald-400' : p.status === 'failed' ? 'text-rose-400' : 'text-slate-500'}>
                    {p.status === 'done' ? '✓' : p.status === 'failed' ? '✕' : '·'}
                  </span>
                  <span>{p.step}</span>
                </li>
              ))}
            </ol>
          )}
          {pendingSql && (
            <pre className="thin-scroll mt-2 overflow-x-auto rounded-lg bg-slate-950/60 p-2 font-mono text-[10.5px] text-slate-400">
              {pendingSql}
            </pre>
          )}
          {critic && <p className="mt-2 text-[11px] text-amber-300">{critic}</p>}
        </Panel>
      )}

      {/* 结论 */}
      {report && (
        <Panel title="📝 结论">
          <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-slate-200">{report}</p>
          {notes.length > 0 && (
            <ul className="mt-2.5 flex flex-col gap-1 border-t border-white/10 pt-2">
              {notes.map((n, i) => (
                <li key={i} className={`text-[11px] leading-relaxed ${n.startsWith('⚠️') ? 'text-amber-300' : 'text-slate-500'}`}>{n}</li>
              ))}
            </ul>
          )}
        </Panel>
      )}

      {/* 图表 */}
      {charts.map((c, i) => <ChartCard key={i} spec={c} />)}

      {/* 每步结果（可复核） */}
      {results.map((r, i) => <ResultTable key={i} evt={r} />)}

      {running && (
        <button
          onClick={() => abortRef.current?.abort()}
          className="self-start rounded-lg border border-white/10 bg-white/[0.05] px-3 py-1.5 text-[11.5px] text-slate-300 transition-colors hover:border-rose-400/40 hover:text-rose-200"
        >
          中止本次分析
        </button>
      )}
    </div>
  )
}
