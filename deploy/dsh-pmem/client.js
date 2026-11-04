/**
 * dsh-pmem 的客户端半边：DSH 设置里的「可塑性记忆」窗口。
 *
 * 七个标签页：概览 / 醒来卡 / 记忆 / 自动 handoff / MCP / 高级 / 夜间。
 *
 * 星图**不占标签页**：只在概览里留一行开关 + 「打开星图 ↗」，
 * 主体是独立页面 `/dsh-pmem/starmap`（整页重绘 + SSE 实时）。
 *
 * 数据只来自 `/dsh-pmem/api/*`，不制造第二份配置。
 */
window.__ModuleLoader__.load({
  id: 'dsh-pmem',
  factory: require => {
    const module = { exports: {} }
    const exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    const React = require('react')
    const { createElement: h, useCallback, useEffect, useState } = React

    const API = '/dsh-pmem/api'
    const STARMAP = '/dsh-pmem/starmap'
    const TABS = [
      ['overview', '概览'],
      ['wake', '醒来卡'],
      ['memory', '记忆'],
      ['handoff', '自动 handoff'],
      ['mcp', 'MCP'],
      ['advanced', '高级'],
      ['night', '夜间'],
    ]

    const CSS_TAG = 'dsh-pmem/settings.css'
    const CSS = `
.pmem-app{max-width:960px;margin:0 auto;color:var(--dsw-alias-label-primary,#1f2328);font-size:13px;line-height:20px;border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:14px;overflow:hidden;display:flex;flex-direction:column;max-height:min(700px,calc(100vh - 160px));background:rgba(127,127,127,.03)}
.pmem-head{padding:16px 18px 12px;border-bottom:1px solid var(--dsw-alias-border-l2,#d0d5dd);background:linear-gradient(135deg,rgba(91,140,255,.12),rgba(120,90,255,.07))}
.pmem-kicker{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--dsw-alias-label-secondary,#5b6472);margin:0 0 4px}
.pmem-title{margin:0;font-size:18px;font-weight:600;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pmem-sub{margin:4px 0 0;color:var(--dsw-alias-label-secondary,#5b6472);font-size:12px}
.pmem-tabs{display:flex;gap:2px;padding:8px 12px 0;border-bottom:1px solid var(--dsw-alias-border-l2,#d0d5dd);overflow-x:auto;background:rgba(127,127,127,.02)}
.pmem-tab{border:none;background:transparent;color:var(--dsw-alias-label-secondary,#5b6472);font:inherit;font-size:12.5px;padding:8px 10px 9px;border-bottom:2px solid transparent;cursor:pointer;white-space:nowrap;border-radius:6px 6px 0 0}
.pmem-tab:hover{background:rgba(127,127,127,.09);color:var(--dsw-alias-label-primary,#1f2328)}
.pmem-tab.on{color:var(--dsw-alias-label-primary,#1f2328);border-bottom-color:var(--dsw-alias-brand-primary,#2f6fed);font-weight:600}
.pmem-body{padding:14px 18px 18px;overflow:auto;min-height:0}
.pmem-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.pmem-card{border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:10px;padding:11px 12px;background:rgba(127,127,127,.03)}
.pmem-card h4{margin:0 0 4px;font-size:12px;font-weight:600;color:var(--dsw-alias-label-secondary,#5b6472);letter-spacing:.02em}
.pmem-big{font-size:20px;font-weight:600;line-height:26px}
.pmem-kv{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:.5px solid var(--dsw-alias-border-l2,#d0d5dd);font-size:12px}
.pmem-kv:last-child{border-bottom:none}
.pmem-kv span:first-child{color:var(--dsw-alias-label-secondary,#5b6472)}
.pmem-row{display:flex;align-items:center;gap:10px;justify-content:space-between;padding:10px 0;border-bottom:.5px solid var(--dsw-alias-border-l2,#d0d5dd)}
.pmem-row:last-child{border-bottom:none}
.pmem-row-main{min-width:0}
.pmem-row-title{font-size:13px;font-weight:500}
.pmem-row-sub{font-size:11.5px;color:var(--dsw-alias-label-secondary,#5b6472)}
.pmem-btn{font:inherit;font-size:12px;color:var(--dsw-alias-label-primary,#1f2328);background:rgba(127,127,127,.08);border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:8px;padding:5px 11px;cursor:pointer}
.pmem-btn:hover:not(:disabled){border-color:var(--dsw-alias-brand-primary,#2f6fed);background:rgba(47,111,237,.12)}
.pmem-btn:disabled{opacity:.5;cursor:default}
.pmem-btn:focus-visible{outline:2px solid var(--dsw-alias-brand-primary,#2f6fed);outline-offset:2px}
.pmem-link{color:var(--dsw-alias-brand-primary,#2f6fed);text-decoration:none;font-size:12px}
.pmem-link:hover{text-decoration:underline}
.pmem-pre{margin:0;padding:12px;border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:10px;background:rgba(127,127,127,.06);font-family:ui-monospace,Consolas,monospace;font-size:11.5px;line-height:17px;white-space:pre-wrap;word-break:break-word;max-height:340px;overflow:auto}
.pmem-input{font:inherit;font-size:12px;padding:6px 9px;border-radius:8px;border:1px solid var(--dsw-alias-border-l2,#d0d5dd);background:transparent;color:inherit;min-width:0}
.pmem-input:focus-visible{outline:2px solid var(--dsw-alias-brand-primary,#2f6fed);outline-offset:1px}
.pmem-list{display:flex;flex-direction:column;gap:6px;margin-top:10px}
.pmem-item{border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:9px;padding:9px 10px;background:rgba(127,127,127,.03)}
.pmem-item-meta{font-size:11px;color:var(--dsw-alias-label-secondary,#5b6472);margin-bottom:3px;display:flex;gap:8px;flex-wrap:wrap}
.pmem-chip{display:inline-block;padding:0 6px;border-radius:99px;background:rgba(47,111,237,.14);color:var(--dsw-alias-brand-primary,#2f6fed);font-size:10.5px;line-height:16px}
.pmem-ok{color:var(--dsw-alias-state-success-primary,#2fbf71)}
.pmem-warn{color:var(--dsw-alias-state-warn-primary,#b97800)}
.pmem-err{color:var(--dsw-alias-state-error-primary,#e5484d)}
.pmem-note{font-size:11.5px;color:var(--dsw-alias-label-secondary,#5b6472);margin:8px 0 0;line-height:17px}
.pmem-switch{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--dsw-alias-label-secondary,#5b6472);cursor:pointer;user-select:none}
.pmem-switch input{appearance:none;width:32px;height:18px;border-radius:99px;background:rgba(127,127,127,.28);position:relative;cursor:pointer;margin:0;transition:background .15s}
.pmem-switch input::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.25);transition:transform .15s}
.pmem-switch input:checked{background:var(--dsw-alias-brand-primary,#2f6fed)}
.pmem-switch input:checked::after{transform:translateX(14px)}
.pmem-switch input:disabled{opacity:.45;cursor:default}
.pmem-switch input:focus-visible{outline:2px solid var(--dsw-alias-brand-primary,#2f6fed);outline-offset:2px}
.pmem-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.pmem-runs{display:flex;flex-direction:column;gap:8px}
.pmem-run{border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:10px;padding:8px 12px;background:rgba(127,127,127,.03)}
.pmem-run-toggle{border:none;background:transparent;color:inherit;font:inherit;width:100%;text-align:left;padding:2px 0;cursor:pointer;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.pmem-run-toggle:hover .pmem-run-day{color:var(--dsw-alias-brand-primary,#2f6fed)}
.pmem-run-caret{color:var(--dsw-alias-label-secondary,#5b6472);font-size:10px;width:10px;flex:none}
.pmem-run-day{font-weight:600}
.pmem-run-meta{font-size:11.5px;color:var(--dsw-alias-label-secondary,#5b6472)}
.pmem-tag{font-size:11px;line-height:16px;padding:1px 8px;border-radius:99px;background:rgba(127,127,127,.14);color:var(--dsw-alias-label-secondary,#5b6472);white-space:nowrap}
.pmem-tag-ok{background:rgba(47,191,113,.16);color:var(--dsw-alias-state-success-primary,#2fbf71)}
.pmem-tag-bad{background:rgba(229,72,77,.16);color:var(--dsw-alias-state-error-primary,#e5484d)}
.pmem-run-err{margin-top:6px;font-size:12.5px}
.pmem-stages{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px;margin-top:10px}
.pmem-stage{border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:9px;padding:7px 9px;background:rgba(127,127,127,.04);min-width:0}
.pmem-stage.done{border-left:3px solid var(--dsw-alias-state-success-primary,#2fbf71)}
.pmem-stage.bad{border-left:3px solid var(--dsw-alias-state-error-primary,#e5484d)}
.pmem-stage.bad .pmem-stage-name{color:var(--dsw-alias-state-error-primary,#e5484d)}
.pmem-stage.skip{opacity:.55}
.pmem-stage-name{font-size:12px;color:var(--dsw-alias-label-secondary,#5b6472)}
.pmem-stage-stats{font-size:12.5px;margin-top:2px;word-break:break-word}
.pmem-empty{margin:0;padding:24px 0;color:var(--dsw-alias-label-secondary,#5b6472)}
`

    function useStyleOnce() {
      useEffect(() => {
        if (typeof document === 'undefined') return
        if (document.querySelector(`style[data-tag="${CSS_TAG}"]`)) return
        const style = document.createElement('style')
        style.dataset.tag = CSS_TAG
        style.textContent = CSS
        document.head.appendChild(style)
      }, [])
    }

    async function api(path, options = {}) {
      const res = await fetch(API + path, {
        headers: { accept: 'application/json', ...(options.body ? { 'content-type': 'application/json' } : {}) },
        ...options,
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok || data?.ok === false) {
        throw new Error(data?.error || `HTTP ${res.status}`)
      }
      return data
    }

    const fmtBytes = n => {
      const v = Number(n) || 0
      if (v < 1024) return `${v} B`
      if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`
      return `${(v / 1024 / 1024).toFixed(1)} MB`
    }

    const fmtTime = iso => {
      if (!iso) return '—'
      const d = new Date(iso)
      return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
    }

    function Switch({ checked, onChange, disabled, label }) {
      return h(
        'label',
        { className: 'pmem-switch' },
        h('input', {
          type: 'checkbox',
          checked: !!checked,
          disabled: !!disabled,
          onChange: event => onChange?.(event.target.checked),
        }),
        label,
      )
    }

    function NumberField({ spec, value, onSave, disabled }) {
      const [draft, setDraft] = useState(value)
      useEffect(() => setDraft(value), [value])
      const dirty = Number(draft) !== Number(value) && draft !== ''
      return h(
        'div',
        { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
        h('input', {
          className: 'pmem-input',
          type: 'number',
          min: spec?.min,
          max: spec?.max,
          step: spec?.step || 1,
          value: draft,
          disabled: !!disabled,
          style: { width: '104px' },
          onChange: event => setDraft(event.target.value),
        }),
        spec?.unit ? h('span', { className: 'pmem-row-sub' }, spec.unit) : null,
        h('button', {
          className: 'pmem-btn',
          disabled: !!disabled || !dirty,
          onClick: () => onSave(Number(draft)),
        }, dirty ? '保存' : '已保存'),
      )
    }

    function Row({ title, sub, children }) {
      return h(
        'div',
        { className: 'pmem-row' },
        h('div', { className: 'pmem-row-main' }, h('div', { className: 'pmem-row-title' }, title),
          sub ? h('div', { className: 'pmem-row-sub' }, sub) : null),
        children,
      )
    }

    // 睡眠流水（`pmem sleep log --json`）：一次 run 一张可折叠的卡，展开是八个阶段。
    // 数据里各阶段 stats 是嵌套 JSON（原始是一堆花括号数字，人看不懂），
    // 所以每个阶段给一句人话摘要；不认识的阶段/字段退回"键=值"列表，绝不直接甩 JSON。
    const STAGE_LABELS = {
      s1_collect: '1 · 收集',
      s2_dedup: '2 · 去重',
      s3_relations: '3 · 关系',
      s4_conflicts: '4 · 冲突',
      s5_patterns: '5 · 规律',
      s6_traits: '6 · 特质',
      s7_reflect: '7 · 反思',
      s8_snapshot: '8 · 快照',
    }

    const STATUS_LABELS = { ok: '完成', partial: '部分完成', failed: '失败', error: '出错', running: '进行中' }
    const TRIGGER_LABELS = { manual: '手动', schedule: '定时', sched: '定时', catchup: '补跑', dry_run: '演练' }
    const KIND_LABELS = { episodic: '情景', semantic: '语义', person: '人物', relation: '关系', goal: '目标', experience: '经验', meta: '元' }

    const stageId = stage => stage?.name || stage?.stage || '?'
    const stageLabel = stage => STAGE_LABELS[stageId(stage)] || stageId(stage)
    const statusText = run => STATUS_LABELS[String(run?.status ?? '')] || String(run?.status ?? '—')
    const triggerText = run => TRIGGER_LABELS[String(run?.trigger ?? '')] || String(run?.trigger ?? '')

    function fmtClock(iso) {
      if (!iso) return ''
      const date = new Date(iso)
      return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString()
    }

    function fmtDuration(run) {
      const start = Date.parse(run?.started_at ?? '')
      const end = Date.parse(run?.finished_at ?? '')
      if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return ''
      const ms = end - start
      return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`
    }

    const numberAt = (stats, key) => (typeof stats?.[key] === 'number' ? stats[key] : undefined)

    /** 不认识的阶段：把 stats 摊平成"键 值"，跳掉空对象/空串，最多 6 项。 */
    function genericPairs(stats) {
      const pairs = []
      for (const [key, value] of Object.entries(stats ?? {})) {
        if (value === null || value === undefined || value === '' || typeof value === 'object') continue
        if (pairs.length >= 6) break
        const text = typeof value === 'boolean' ? (value ? '是' : '否') : String(value)
        pairs.push(`${key} ${text}`)
      }
      return pairs
    }

    /** 每个阶段一句人话；数值单位尽量说清（条 / 个 / KB）。 */
    function stageSummary(stage) {
      if (!stage || stage.skipped === true) return '本次跳过'
      const stats = stage.stats && typeof stage.stats === 'object' ? stage.stats : null
      if (!stats) return typeof stage.stats === 'string' ? stage.stats.slice(0, 120) : ''
      const parts = []
      const add = (label, value, unit = '') => {
        if (value === undefined || value === null) return
        parts.push(`${label} ${value}${unit}`)
      }
      const n = key => numberAt(stats, key)
      switch (stageId(stage)) {
        case 's1_collect': {
          add('记忆', n('memories'), ' 条')
          add('召回', n('recalls'), ' 次')
          add('待办', n('backlog'), ' 条')
          const kinds = Object.entries(stats.by_kind ?? {})
            .filter(([, count]) => typeof count === 'number' && count > 0)
            .map(([kind, count]) => `${KIND_LABELS[kind] || kind} ${count}`)
          if (kinds.length) parts.push(kinds.join(' · '))
          break
        }
        case 's2_dedup':
          add('合并', n('merged'), ' 条')
          add('候选', n('candidates'), ' 组')
          add('归档分组', n('groups_archived'))
          break
        case 's3_relations':
          add('扫描记忆', n('scanned'), ' 条')
          add('提及', n('mentions'), ' 次')
          add('新关系', n('relations_created'), ' 条')
          add('强化', n('relations_reinforced'), ' 条')
          break
        case 's4_conflicts':
          add('发现冲突', n('found'), ' 处')
          add('新建', n('created'), ' 条')
          break
        case 's5_patterns':
          add('新规律', n('patterns_created'), ' 条')
          add('更新规律', n('patterns_updated'), ' 条')
          add('新经验', n('experience_created'), ' 条')
          add('未完成经验', n('experience_incomplete'), ' 条')
          break
        case 's6_traits':
          add('新增特质', n('created'))
          add('更新', n('updated_in_place'))
          add('建立', n('established'))
          add('退役', n('retired'))
          add('当前活跃', n('active'))
          break
        case 's7_reflect':
          add('质量分', n('quality'))
          add('缺陷', n('defects'))
          add('未决冲突', n('conflicts_open'))
          break
        case 's8_snapshot': {
          const size = n('size_bytes')
          if (size !== undefined) parts.push(`快照 ${(size / 1024).toFixed(0)} KB`)
          if (typeof stats.verified === 'boolean') parts.push(stats.verified ? '校验通过' : '校验未过')
          add('记忆', n('memory_count'), ' 条')
          break
        }
        default:
          parts.push(...genericPairs(stats))
      }
      if (!parts.length) parts.push(...genericPairs(stats))
      return parts.join(' · ')
    }

    function NightStage({ stage }) {
      const skipped = stage?.skipped === true
      // 阶段失败（ok === false 且不是跳过）要和成功分开：旧夜间页把失败也画成绿的，
      // 看起来像"跑完了"，实际上那一阶段什么都没做成。
      const failed = !skipped && stage?.ok === false
      const ms = typeof stage?.ms === 'number' ? ` · ${stage.ms}ms` : ''
      const summary = stageSummary(stage)
      return h(
        'div',
        { className: 'pmem-stage ' + (skipped ? 'skip' : failed ? 'bad' : 'done') },
        h('div', { className: 'pmem-stage-name' }, `${stageLabel(stage)}${ms}`),
        summary ? h('div', { className: 'pmem-stage-stats' }, summary) : null,
      )
    }

    /** 一次睡眠：默认展开最近一次，其余折叠——不折叠的话 14 次会把面板撑到看不见标签栏。 */
    function NightRun({ run, open, onToggle }) {
      if (!run) return null
      const status = String(run.status ?? '')
      const statusClass = status === 'ok' ? ' pmem-tag-ok' : status === 'running' ? '' : ' pmem-tag-bad'
      const stages = (run.stages ?? []).filter(Boolean)
      const okCount = stages.filter(stage => stage.skipped !== true && stage.ok !== false).length
      const clock = [fmtClock(run.started_at), fmtClock(run.finished_at)].filter(Boolean).join(' → ')
      const duration = fmtDuration(run)
      return h(
        'div',
        { className: 'pmem-run' },
        h(
          'button',
          {
            type: 'button',
            className: 'pmem-run-toggle',
            'aria-expanded': open ? 'true' : 'false',
            onClick: onToggle,
          },
          h('span', { className: 'pmem-run-caret' }, open ? '▾' : '▸'),
          h('span', { className: 'pmem-run-day' }, run.day ?? '—'),
          h('span', { className: 'pmem-tag' + statusClass }, statusText(run)),
          run.trigger ? h('span', { className: 'pmem-tag' }, triggerText(run)) : null,
          h(
            'span',
            { className: 'pmem-run-meta' },
            `${okCount}/${stages.length || 8} 阶段${duration ? ` · 耗时 ${duration}` : ''}${clock ? ` · ${clock}` : ''}`,
          ),
        ),
        run.error ? h('div', { className: 'pmem-err pmem-run-err' }, run.error) : null,
        open
          ? h('div', { className: 'pmem-stages' },
              ...stages.map((stage, index) =>
                h(NightStage, { key: `${stageId(stage)}-${index}`, stage })))
          : null,
      )
    }

    function PMemSection() {
      useStyleOnce()
      const [tab, setTab] = useState('overview')
      const [status, setStatus] = useState(null)
      const [card, setCard] = useState('')
      const [budget, setBudget] = useState(300)
      const [items, setItems] = useState([])
      const [query, setQuery] = useState('')
      const [busy, setBusy] = useState('')
      const [night, setNight] = useState(null)
      const [nightLoading, setNightLoading] = useState(true)
      const [nightError, setNightError] = useState('')
      const [openRuns, setOpenRuns] = useState({})
      const [sleeping, setSleeping] = useState(false)
      const [err, setErr] = useState('')
      const [toast, setToast] = useState('')
      const [debug, setDebug] = useState(null)
      const [browserUrl, setBrowserUrl] = useState('')

      const notify = message => {
        setToast(message)
        setTimeout(() => setToast(''), 3500)
      }

      const loadStatus = useCallback(async () => {
        try {
          const data = await api('/status')
          setStatus(data)
          setErr('')
        } catch (error) {
          setErr(`读状态失败：${error.message}`)
        }
      }, [])

      const loadCard = useCallback(async () => {
        try {
          const data = await api(`/wake?budget=${encodeURIComponent(budget)}`)
          setCard(data.text || '')
        } catch (error) {
          setErr(`读醒来卡失败：${error.message}`)
        }
      }, [budget])

      const search = useCallback(async keyword => {
        try {
          const data = await api(`/memories?q=${encodeURIComponent(keyword)}&k=20`)
          setItems(data.items || [])
        } catch (error) {
          setErr(`检索失败：${error.message}`)
        }
      }, [])

      useEffect(() => { void loadStatus() }, [loadStatus])
      useEffect(() => {
        if (tab === 'wake' && !card) void loadCard()
      }, [tab, card, loadCard])
      useEffect(() => {
        if (tab === 'memory' && !items.length) void search(query)
      }, [tab, items.length, query, search])
      // 设置页开着的时候 30 秒刷一次概览：库是一直在长的。
      useEffect(() => {
        const timer = setInterval(() => { void loadStatus() }, 30000)
        return () => clearInterval(timer)
      }, [loadStatus])

      const openStarMap = async () => {
        setBusy('starmap')
        setErr('')
        try {
          const data = await api('/starmap/open', { method: 'POST', body: '{}' })
          if (data?.url) setBrowserUrl(data.url)
          if (data?.opened === false) setErr(data.warning || '浏览器没拉起来，地址在下面，自己点一下')
          else notify('已在浏览器打开')
        } catch (error) {
          setErr(`打开星图失败：${error.message}`)
        } finally {
          setBusy('')
        }
      }

      // 已经起过的独立服务直接把地址显示出来，省得再点一次。
      useEffect(() => {
        if (tab !== 'overview') return
        api('/starmap/url')
          .then(data => { if (data?.url) setBrowserUrl(data.url) })
          .catch(() => {})
      }, [tab])

      const loadNight = async () => {
        setNightLoading(true)
        try {
          setNight(await api('/sleep'))
          setNightError('')
        } catch (error) {
          setNight(null)
          setNightError(`读不到睡眠记录：${error.message}`)
        } finally {
          setNightLoading(false)
        }
      }

      // 主动入睡：跑一轮 sleep（默认处理昨天，自动续跑未完成的 run）。可能几十秒。
      const sleepNow = async () => {
        setSleeping(true)
        setErr('')
        try {
          const data = await api('/sleep/now', { method: 'POST', body: '{}' })
          if (!data?.ok) setErr(`入睡失败：${data?.error || '未知'}`)
          else notify(data?.data?.status === 'ok' ? '跑完了' : `跑完：${data?.data?.status ?? '未知'}`)
          await loadNight()
        } catch (error) {
          setErr(`入睡失败：${error.message}`)
        } finally {
          setSleeping(false)
        }
      }

      useEffect(() => {
        if (tab !== 'overview' && tab !== 'night') return
        void loadNight()
      }, [tab])

      const saveRuntime = async patch => {
        setBusy('config')
        try {
          const data = await api('/config', { method: 'POST', body: JSON.stringify(patch) })
          setStatus(prev => (prev ? { ...prev, runtime: data.runtime } : prev))
          notify('已保存')
        } catch (error) {
          setErr(`保存失败：${error.message}`)
        } finally {
          setBusy('')
        }
      }

      const db = status?.db ?? {}
      const runtime = status?.runtime ?? {}
      const locked = runtime.locked ?? {}
      const surface = status?.surface ?? {}
      const tokens = status?.tokens ?? {}
      const starmap = status?.starmap ?? {}

      const tabsBar = h(
        'div',
        { className: 'pmem-tabs', role: 'tablist' },
        ...TABS.map(([id, label]) =>
          h(
            'button',
            {
              key: id,
              role: 'tab',
              'aria-selected': tab === id,
              className: 'pmem-tab' + (tab === id ? ' on' : ''),
              onClick: () => setTab(id),
            },
            label,
          ),
        ),
      )

      const overview = h(
        'div',
        null,
        h(
          'div',
          { className: 'pmem-grid' },
          h('div', { className: 'pmem-card' },
            h('h4', null, '记忆'),
            h('div', { className: 'pmem-big' }, db.memory_total ?? '—'),
            h('div', { className: 'pmem-row-sub' }, `活跃 ${db.by_status?.active ?? '—'} · 事件 ${db.event_count ?? '—'}`)),
          h('div', { className: 'pmem-card' },
            h('h4', null, '浮现命中率'),
            h('div', { className: 'pmem-big' },
              surface.hit_rate === null || surface.hit_rate === undefined
                ? '—'
                : `${Math.round(surface.hit_rate * 100)}%`),
            h('div', { className: 'pmem-row-sub' }, `浮现 ${surface.surfaced ?? 0} · 用到 ${surface.used ?? 0}`)),
          h('div', { className: 'pmem-card' },
            h('h4', null, '注入 tokens'),
            h('div', { className: 'pmem-big ' + (tokens.samples && tokens.p95 <= tokens.budget_p95 ? 'pmem-ok' : '') },
              tokens.samples ? tokens.p95 : '—'),
            h('div', { className: 'pmem-row-sub' },
              tokens.samples
                ? `均值 ${tokens.mean} · 门 ≤${tokens.budget_mean}/≤${tokens.budget_p95}`
                : '还没样本：插件每轮报一次')),
          h('div', { className: 'pmem-card' },
            h('h4', null, '完整性'),
            h('div', { className: 'pmem-big ' + (db.integrity_ok ? 'pmem-ok' : 'pmem-err') }, db.integrity_ok ? 'ok' : '异常'),
            h('div', { className: 'pmem-row-sub' }, `${fmtBytes(db.db_size_bytes)} · schema ${db.schema_version ?? '—'}`)),
          h('div', { className: 'pmem-card' },
            h('h4', null, '星图'),
            h('div', { className: 'pmem-big' }, starmap.ready ? `${starmap.nodes ?? 0}` : '未生成'),
            h('div', { className: 'pmem-row-sub' }, starmap.ready ? `连接 ${starmap.links ?? 0} · ${fmtTime(starmap.generated_at)}` : '跑一次 pmem star')),
        ),
        h(
          'div',
          { style: { marginTop: '14px' } },
          h(Row, {
            title: '每轮浮现',
            sub: '按线索 / 情境 / 节律想起最多一条记忆；关掉后不再注入。',
          }, h(Switch, {
            checked: runtime.surface !== false,
            disabled: locked.surface || busy === 'config',
            onChange: value => void saveRuntime({ surface: value }),
            label: runtime.surface === false ? '关' : '开',
          })),
          h(Row, {
            title: '上下文自动交接',
            sub: '到上下文 80%（可配）时自动写好「停在哪」并压缩历史，不用你手动换会话；关掉就只提醒。',
          }, h(Switch, {
            checked: runtime.contextAuto !== false,
            disabled: busy === 'config',
            onChange: value => void saveRuntime({ contextAuto: value }),
            label: runtime.contextAuto === false ? '关' : '开',
          })),
          h(Row, {
            title: '星图',
            sub: '用系统浏览器打开，不占 DSH 的界面；写进新记忆后几秒内自己长出来。',
          }, h(
            'div',
            { style: { display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' } },
            h(Switch, {
              checked: runtime.starmap !== false,
              disabled: busy === 'config',
              onChange: value => void saveRuntime({ starmap: value }),
              label: runtime.starmap === false ? '关' : '开',
            }),
            // 浏览器这条不走 DSH 的登录闸：插件自己起一个只绑回环的小服务，
            // 再由 Node 侧拉起系统浏览器。所以不会占住 DSH 的窗口。
            h('button', {
              className: 'pmem-btn',
              disabled: busy === 'starmap' || runtime.starmap === false,
              onClick: () => void openStarMap(),
            }, busy === 'starmap' ? '打开中…' : '在浏览器打开'),
            // 只留浏览器这一条路：页内浮层有概率被窗口边缘压住关闭按钮，
            // 而浏览器自己管自己的窗口，没有这个问题。
            h('span', { className: 'pmem-row-sub', style: { opacity: .7 } }, '关掉开关就不起服务'),
          )),
          browserUrl
            ? h('p', { className: 'pmem-note' },
                '浏览器地址（可以存书签）：',
                h('code', { style: { fontSize: '11px' } }, browserUrl))
            : null,
          h(Row, {
            title: '醒来卡',
            sub: status?.wake ? `${status.wake.chars} 字 · ${status.wake.head || ''}` : '—',
          }, h('button', { className: 'pmem-btn', onClick: () => setTab('wake') }, '看看')),
          // 夜间：八阶段睡眠的「看得见」入口。最近一次的状态直接写在这行，
          // 想看每一步细节就切到「夜间」标签（同源 fetch，不开新窗口）；
          // 不想等夜里，也可以现在手动入睡。
          h(Row, {
            title: '夜间',
            sub: (() => {
              const runs = night?.data?.runs || []
              if (!runs.length) return '还没有睡眠记录（夜里会自动跑；也可以现在入睡）'
              const run = runs[0]
              const done = (run.stages || []).filter(stage => stage && stage.skipped !== true).length
              return `${run.day} · ${statusText(run)} · ${done}/8 阶段 · ${fmtClock(run.started_at)}`
            })(),
          }, h(
            'div',
            { style: { display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' } },
            h('button', {
              className: 'pmem-btn',
              onClick: () => setTab('night'),
            }, '夜间页'),
            h('button', {
              className: 'pmem-btn',
              disabled: sleeping,
              onClick: () => void sleepNow(),
            }, sleeping ? '入睡中…' : '现在入睡'),
            h('span', { className: 'pmem-row-sub', style: { opacity: .7 } }, '八阶段：收集→去重→关系→冲突→规律→特质→反思→快照'),
          )),
        ),
      )

      const wakeTab = h(
        'div',
        null,
        h(
          'div',
          { style: { display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '10px', flexWrap: 'wrap' } },
          h('span', { className: 'pmem-row-sub' }, '预算'),
          h('input', {
            className: 'pmem-input',
            type: 'number',
            min: 120,
            max: 1200,
            step: 20,
            value: budget,
            style: { width: '96px' },
            onChange: event => setBudget(Number(event.target.value) || 300),
          }),
          h('button', {
            className: 'pmem-btn',
            disabled: busy === 'wake',
            onClick: async () => {
              setBusy('wake')
              try { await loadCard() } finally { setBusy('') }
            },
          }, busy === 'wake' ? '刷新中…' : '刷新'),
          h('button', {
            className: 'pmem-btn',
            disabled: busy === 'config',
            onClick: () => void saveRuntime({ budget: Math.round(budget) }),
          }, '存为默认'),
        ),
        h('pre', { className: 'pmem-pre' }, card || '— 还没有可唤醒的记忆'),
        h('p', { className: 'pmem-note' },
          '这张卡每轮注入 system prompt（order 941），只读、不写库。改了预算只影响这一页的预览，' +
          '真正生效的预算在 profile 的 cordis.patch.yml 里。'),
      )

      const memoryTab = h(
        'div',
        null,
        h(
          'form',
          {
            style: { display: 'flex', gap: '8px' },
            onSubmit: event => { event.preventDefault(); void search(query) },
          },
          h('input', {
            className: 'pmem-input',
            style: { flex: '1 1 auto' },
            placeholder: '搜关键词、路径、命令或错误码',
            value: query,
            onChange: event => setQuery(event.target.value),
          }),
          h('button', { className: 'pmem-btn', type: 'submit' }, '检索'),
        ),
        h('div', { className: 'pmem-list' },
          ...(items.length
            ? items.map(item => h('div', { className: 'pmem-item', key: item.id },
                h('div', { className: 'pmem-item-meta' },
                  h('span', null, item.day || ''),
                  item.grade ? h('span', { className: 'pmem-chip' }, item.grade) : null,
                  ...(item.groups ?? []).map(group => h('span', { key: group }, group))),
                h('div', null, String(item.text ?? '').slice(0, 320))))
            : [h('p', { className: 'pmem-note', key: 'empty' }, '没有结果。搜索是 BM25 + 三元组双通道，很短的词也能命中。')]),
        ),
      )

      const intSpecs = Object.entries(runtime.spec ?? {}).filter(([, spec]) => spec.type === 'int')
      const specOf = key => runtime.spec?.[key]

      const handoffTab = h(
        'div',
        null,
        h(Row, { title: '自动 handoff', sub: status?.handoff?.enabled ? '已开启（profile 里 handoff: true）' : '已关闭' },
          h('span', { className: 'pmem-row-sub' }, status?.handoff?.enabled ? '✓' : '—')),
        h(Row, {
          title: '长会话落盘间隔',
          sub: `${specOf('handoffTurns')?.min ?? 2}–${specOf('handoffTurns')?.max ?? 100} 轮，改完下一轮生效`,
        }, h(NumberField, {
          spec: specOf('handoffTurns'),
          value: runtime.handoffTurns,
          disabled: busy === 'config',
          onSave: value => void saveRuntime({ handoffTurns: value }),
        })),
        h(Row, {
          title: '落盘最小间隔',
          sub: '距上一次落盘不足这个时间就跳过，避免刷屏',
        }, h(NumberField, {
          spec: specOf('handoffMinMs'),
          value: runtime.handoffMinMs,
          disabled: busy === 'config',
          onSave: value => void saveRuntime({ handoffMinMs: value }),
        })),
        h(Row, { title: '退出时写入', sub: '会话结束写一条「上次停在哪」到 我们/停在哪（B 级，幂等）' },
          h('span', { className: 'pmem-row-sub' }, 'agent/disposed')),
        h('p', { className: 'pmem-note' },
          '这两个数是运行时开关（~/.dsh/pmem/runtime.json），保存后下一次 turn/end 就用新值，不用重启。' +
          'handoff 总开关仍是装配期的，改 profile 并重启才生效。'),
      )

      const mcpTab = h(
        'div',
        null,
        h(Row, { title: 'MCP server', sub: '官方 @deepseek-ai/dsh-mcp-client，stdio 拉起 pmem.gate.mcp_server' },
          h('span', { className: 'pmem-chip' }, status?.mcp?.serverName ?? 'pmem')),
        h(Row, { title: '工具数', sub: 'memory_add / search / get / update / delete / history / export / import / stats …' },
          h('span', { className: 'pmem-row-sub' }, `${status?.mcp?.tools ?? 20} 个`)),
        h(Row, { title: '原生工具', sub: 'pmem_wake / pmem_recall / pmem_remember / pmem_stats' },
          h('span', { className: 'pmem-row-sub' }, '4 个')),
        h('p', { className: 'pmem-note' },
          'MCP 工具和原生工具并存、不重名。连接状态由宿主管理；想手工探活可以在终端跑 /pmem mcp。'),
      )

      const advancedTab = h(
        'div',
        null,
        h(Row, { title: '数据库', sub: status?.db?.subject ? `subject=${status.db.subject}` : '' },
          h('code', { style: { fontSize: '11.5px' } }, status ? 'data/pmem.db' : '—')),
        h(Row, { title: '星图实时', sub: '关掉只是停止自动重绘，手动「重绘」照常可用' },
          h(Switch, {
            checked: runtime.starmapLive !== false,
            disabled: busy === 'config',
            onChange: value => void saveRuntime({ starmapLive: value }),
            label: runtime.starmapLive === false ? '实时关' : '实时开',
          })),
        ...intSpecs.map(([key, spec]) => h(Row, {
          key,
          title: spec.label,
          sub: `${spec.min}–${spec.max} ${spec.unit || ''} · 保存即生效`,
        }, h(NumberField, {
          spec,
          value: runtime[key],
          disabled: busy === 'config',
          onSave: value => void saveRuntime({ [key]: value }),
        }))),
        h('p', { className: 'pmem-note' },
          '淡忘不是删除：状态只降到 dormant/archived，原文与事件链不动；被回执用到的会浮回 active。' +
          '真正的删除只有 memory_delete --hard。'),
        h('div', { style: { marginTop: '14px' } },
          h(Row, {
            title: '自检',
            sub: '星图打不开时先点这里：列出最近请求和它们的状态码（404 路由没注册 / 401 被登录闸拦 / 500 处理炸了）',
          }, h('button', {
            className: 'pmem-btn',
            disabled: busy === 'debug',
            onClick: async () => {
              setBusy('debug')
              try {
                setDebug(await api('/debug'))
              } catch (error) {
                setErr(`自检失败：${error.message}`)
              } finally {
                setBusy('')
              }
            },
          }, busy === 'debug' ? '读取中…' : '运行')),
          debug ? h('pre', { className: 'pmem-pre', style: { marginTop: '8px' } },
            JSON.stringify({
              logFile: debug.logFile,
              starmap: debug.starmap,
              config: debug.config,
              requests: (debug.requests ?? []).slice(-12),
            }, null, 2)) : null,
        ),
      )

      // 「夜间」标签：把夜间流水的信息密度搬进设置面板。数据走同源 /sleep。
      // 不开新窗口：桌面壳只把 http/https 转给系统浏览器（那边没有登录 cookie，401），
      // dsh-app:// 相对路径则静默 deny、0 请求——用户看到的就是「点了没反应」。
      // 整页导航能过（主进程会补 cookie），但会离开设置面板，所以也不走。
      const nightRuns = night?.data?.runs ?? []
      // 默认只展开最近一次；其余点开再看。14 张全展开会把面板撑到标签栏都滚没。
      const runKey = (run, index) => String(run?.id ?? `${run?.day ?? 'run'}-${index}`)
      const isRunOpen = (run, index) => {
        const key = runKey(run, index)
        return Object.prototype.hasOwnProperty.call(openRuns, key) ? Boolean(openRuns[key]) : index === 0
      }
      const toggleRun = (run, index) => {
        const key = runKey(run, index)
        const next = !isRunOpen(run, index)
        setOpenRuns(prev => ({ ...prev, [key]: next }))
      }

      const nightTab = h(
        'div',
        null,
        h(
          'div',
          { className: 'pmem-toolbar' },
          h('button', {
            className: 'pmem-btn',
            disabled: sleeping,
            onClick: () => void sleepNow(),
          }, sleeping ? '入睡中…' : '现在入睡'),
          h('button', {
            className: 'pmem-btn',
            disabled: nightLoading,
            onClick: () => void loadNight(),
          }, nightLoading ? '刷新中…' : '刷新'),
          h('span', { className: 'pmem-row-sub' }, '八阶段：收集→去重→关系→冲突→规律→特质→反思→快照'),
        ),
        nightError
          ? h('p', { className: 'pmem-err', role: 'alert' }, nightError)
          : nightRuns.length
            ? h('div', { className: 'pmem-runs' },
                ...nightRuns.map((run, index) =>
                  h(NightRun, {
                    key: runKey(run, index),
                    run,
                    open: isRunOpen(run, index),
                    onToggle: () => toggleRun(run, index),
                  })))
            : h('p', { className: 'pmem-empty' },
                nightLoading ? '加载中…' : '还没有睡眠记录。跑一次「现在入睡」就有。'),
        h('p', { className: 'pmem-note' },
          '每次睡眠是一张卡片，点标题展开八个阶段：左边绿条=这一步跑完了，红条=失败了，淡一点=这次跳过。' +
          '阶段下面那句是这一步到底做了什么（记忆几条、合并几条、新关系几条……）。默认只展开最近一次，' +
          '数据来自 pmem sleep log，最多显示最近 14 次。'),
      )

      const body = tab === 'wake' ? wakeTab
        : tab === 'memory' ? memoryTab
        : tab === 'handoff' ? handoffTab
        : tab === 'mcp' ? mcpTab
        : tab === 'advanced' ? advancedTab
        : tab === 'night' ? nightTab
        : overview

      const app = h(
        'div',
        { className: 'pmem-app' },
        h('div', { className: 'pmem-head' },
          h('p', { className: 'pmem-kicker' }, 'PMemories · 可塑性记忆'),
          h('h2', { className: 'pmem-title' }, '海马体',
            h('span', { className: 'pmem-chip' }, `v${status?.version ?? '—'}`)),
          h('p', { className: 'pmem-sub' }, '记忆不是被存下来的，是被不断重塑的。上下文清空之后，先想起「我们」。')),
        tabsBar,
        h('div', { className: 'pmem-body' },
          err ? h('div', { className: 'pmem-err', style: { marginBottom: '10px' } }, err) : null,
          toast ? h('div', { className: 'pmem-ok', style: { marginBottom: '10px' } }, toast) : null,
          body),
      )

      return app
    }

    const inject = ['slots']
    const NS = 'dsh-pmem'

    function apply(ctx) {
      const effect = typeof ctx.effect === 'function' ? ctx.effect.bind(ctx) : fn => fn()
      effect(
        () =>
          ctx.slots.inject('settings.section', () =>
            ctx.slots.register(
              { name: 'settings.section', id: NS, order: 8, label: () => '可塑性记忆' },
              PMemSection,
            ), 'dsh-pmem: settings section'),
        'dsh-pmem: settings section',
      )
    }

    exports.apply = apply
    exports.inject = inject
    exports.PMemSection = PMemSection
    exports.TABS = TABS
    exports.API = API
    exports.STARMAP = STARMAP
    return module.exports
  },
})
