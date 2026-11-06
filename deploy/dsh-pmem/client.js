/**
 * dsh-pmem 的浏览器半边。
 *
 * 设置页里的「可塑性记忆」只剩一行入口；真正的界面是**整页控制台**：
 * 侧边栏一行「记忆控制台」→ root main 的 keyed slot（key = pmem-console）。
 *
 * 控制台分区（左侧二级导航 + 右侧内容区）：
 *   总览 / 醒来卡 / 记忆 / 实体 / 睡眠 / 设置
 *
 * 约束：
 *   · 所有数据走同源 fetch：/dsh-pmem/api/console/* 与既有 /dsh-pmem/api/*；
 *   · 不新增依赖、不拼 HTML 字符串、不开新窗口、不整页导航；
 *   · 记忆正文一律「文本节点 + 行内元素」渲染（Markdown-lite 手写）。
 */
window.__ModuleLoader__.load({
  id: 'dsh-pmem',
  factory: require => {
    const module = { exports: {} }
    const exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    const React = require('react')
    const { createElement: h, useCallback, useEffect, useRef, useState } = React

    const API = '/dsh-pmem/api'
    const STARMAP = '/dsh-pmem/starmap'
    const CONSOLE = '/dsh-pmem/api/console'
    const NS = 'dsh-pmem'
    const PANEL_ID = 'pmem-console'

    // 左侧二级导航：分区要少、名字要短，桌面大屏优先。
    const CONSOLE_NAV = [
      ['overview', '总览'],
      ['wake', '醒来卡'],
      ['memory', '记忆'],
      ['entities', '实体'],
      ['sleep', '睡眠'],
      ['settings', '设置'],
    ]

    const MEMORY_MODES = [
      ['auto', '自动'],
      ['keyword', '关键词'],
      ['recent', '最近'],
    ]
    const MEMORY_GRADES = ['S', 'A', 'B', 'C', 'D']
    const MEMORY_STATUS_LABELS = {
      active: '活跃',
      dormant: '休眠',
      archived: '归档',
      pending: '待确认',
      deleted: '已删除',
    }
    const FEEDBACK_LABELS = {
      useful: '有用',
      irrelevant: '无关',
      wrong: '错了',
      confirm: '确认',
      deny: '否定',
      pin: '置顶',
      unpin: '取消置顶',
    }
    const FEEDBACK_SIGNALS = ['useful', 'irrelevant', 'wrong', 'confirm']
    const BATCH_FEEDBACK_SIGNALS = ['useful', 'confirm', 'irrelevant']
    const SENSITIVITY_OPTIONS = ['public', 'personal', 'secret']
    const KIND_LABELS = {
      episodic: '情景',
      semantic: '语义',
      person: '人物',
      relation: '关系',
      goal: '目标',
      experience: '经验',
      meta: '元',
    }
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

    // 前端偏好（检索默认值 / 大睡默认参数）：只存 localStorage，不制造第二份配置。
    const PREFS_KEY = 'dsh-pmem/console.prefs'
    const DEFAULT_PREFS = { k: 20, mode: 'auto', bigTemplate: false, bigLimit: 0 }
    const prefs = { ...DEFAULT_PREFS }
    let prefsLoaded = false

    function loadPrefs() {
      if (prefsLoaded) return prefs
      prefsLoaded = true
      try {
        const raw = typeof localStorage !== 'undefined' ? localStorage.getItem(PREFS_KEY) : null
        if (raw) {
          const parsed = JSON.parse(raw)
          if (parsed && typeof parsed === 'object') Object.assign(prefs, parsed)
        }
      } catch (ignored) {
        // 隐私模式/禁用存储时用默认值
      }
      if (!MEMORY_MODES.some(([value]) => value === prefs.mode)) prefs.mode = 'auto'
      prefs.k = Number(prefs.k) >= 1 && Number(prefs.k) <= 50 ? Number(prefs.k) : 20
      return prefs
    }

    function savePrefs(patch) {
      Object.assign(prefs, patch)
      try {
        if (typeof localStorage !== 'undefined') localStorage.setItem(PREFS_KEY, JSON.stringify(prefs))
      } catch (ignored) {
        // 存不了就算了
      }
      return prefs
    }

    // ── 样式：设置页一行入口（pmem-*）+ 整页控制台（pconsole-*）───────────────
    const CSS_TAG = 'dsh-pmem/settings.css'
    const CSS = `
.pmem-app{max-width:960px;margin:0 auto;color:var(--dsw-alias-label-primary,#1f2328);font-size:13px;line-height:20px;border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:14px;overflow:hidden;background:rgba(127,127,127,.03)}
.pmem-head{padding:16px 18px 14px;border-bottom:1px solid var(--dsw-alias-border-l2,#d0d5dd);background:linear-gradient(135deg,rgba(91,140,255,.12),rgba(120,90,255,.07))}
.pmem-kicker{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--dsw-alias-label-secondary,#5b6472);margin:0 0 4px}
.pmem-title{margin:0;font-size:18px;font-weight:600;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pmem-sub{margin:4px 0 0;color:var(--dsw-alias-label-secondary,#5b6472);font-size:12px}
.pmem-chip{display:inline-block;padding:0 6px;border-radius:99px;background:rgba(47,111,237,.14);color:var(--dsw-alias-brand-text,#2f6fed);font-size:10.5px;line-height:16px}
.pmem-row-sub{font-size:11.5px;color:var(--dsw-alias-label-secondary,#5b6472)}
.pmem-btn{font:inherit;font-size:12px;color:var(--dsw-alias-label-primary,#1f2328);background:rgba(127,127,127,.08);border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:8px;padding:5px 11px;cursor:pointer}
.pmem-btn:hover:not(:disabled){border-color:var(--dsw-alias-button-primary-fill,#2f6fed);background:rgba(47,111,237,.12)}
.pmem-btn:disabled{opacity:.5;cursor:default}
.pmem-btn:focus-visible{outline:2px solid var(--dsw-focus-ring-color,var(--dsw-alias-button-primary-fill,#2f6fed));outline-offset:2px}
.pmem-btn-primary{background:var(--dsw-alias-button-primary-fill,#2f6fed);border-color:transparent;color:var(--dsw-alias-label-primary-foreground,#fff)}.pmem-btn-primary:hover:not(:disabled){background:var(--dsw-alias-button-primary-hover,#2f6fed)}
`

    const CONSOLE_CSS_TAG = 'dsh-pmem/console.css'
    const CONSOLE_CSS = `
.pconsole{box-sizing:border-box;height:100%;min-height:0;display:flex;flex-direction:column;color:var(--dsw-alias-label-primary,#1f2328);font-size:13px;line-height:20px;background:var(--dsw-alias-bg-primary,transparent)}
.pconsole,.pconsole *{box-sizing:border-box}
.pconsole-head{flex:none;display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:18px 22px 12px;border-bottom:1px solid var(--dsw-alias-border-l2,#d0d5dd);background:linear-gradient(135deg,rgba(91,140,255,.12),rgba(120,90,255,.06))}
.pconsole-head-main{min-width:0}
.pconsole-kicker{margin:0 0 3px;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--dsw-alias-label-secondary,#5b6472)}
.pconsole-title{margin:0;font-size:19px;font-weight:600;line-height:26px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pconsole-sub{margin:4px 0 0;font-size:11.5px;color:var(--dsw-alias-label-secondary,#5b6472)}
.pconsole-head-actions{flex:none;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pconsole-shell{flex:1 1 auto;min-height:0;display:flex;align-items:stretch}
.pconsole-nav{flex:none;width:168px;padding:10px 8px;border-right:1px solid var(--dsw-alias-border-l2,#d0d5dd);background:rgba(127,127,127,.02);display:flex;flex-direction:column;gap:2px;overflow-y:auto}
.pconsole-nav-item{font:inherit;font-size:13px;text-align:left;color:var(--dsw-alias-label-secondary,#5b6472);background:transparent;border:none;border-radius:8px;padding:8px 10px;cursor:pointer;display:flex;align-items:center;gap:8px}
.pconsole-nav-item:hover{background:rgba(127,127,127,.09);color:var(--dsw-alias-label-primary,#1f2328)}
.pconsole-nav-item[aria-selected="true"]{background:rgba(47,111,237,.12);color:var(--dsw-alias-label-primary,#1f2328);font-weight:600;box-shadow:inset 2px 0 0 var(--dsw-alias-button-primary-fill,#2f6fed)}
.pconsole-nav-item:focus-visible{outline:2px solid var(--dsw-focus-ring-color,var(--dsw-alias-button-primary-fill,#2f6fed));outline-offset:1px}
.pconsole-nav-hint{margin-top:auto;padding:8px 10px 2px;font-size:10.5px;line-height:15px;color:var(--dsw-alias-label-secondary,#5b6472);opacity:.8}
.pconsole-main{flex:1 1 auto;min-width:0;min-height:0;overflow:auto;padding:16px 22px 28px}
.pconsole-section{margin:0 0 18px}
.pconsole-section-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:0 0 10px}
.pconsole-section-title{margin:0;font-size:13.5px;font-weight:600}
.pconsole-section-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pconsole-banner{margin:0 0 12px;padding:8px 12px;border-radius:8px;border:1px solid rgba(229,72,77,.35);background:rgba(229,72,77,.1);color:var(--dsw-alias-state-error-primary,#e5484d);font-size:12px}
.pconsole-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}
.pconsole-card{border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:10px;padding:10px 12px;background:rgba(127,127,127,.03);min-width:0}
.pconsole-card-title{margin:0 0 2px;font-size:11.5px;font-weight:600;color:var(--dsw-alias-label-secondary,#5b6472)}
.pconsole-metric{font-size:21px;font-weight:600;line-height:27px}
.pconsole-metric-sub{font-size:11px;color:var(--dsw-alias-label-secondary,#5b6472);overflow-wrap:anywhere}
.pconsole-kv{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-bottom:.5px solid var(--dsw-alias-border-l2,#d0d5dd);font-size:12px}
.pconsole-kv:last-child{border-bottom:none}
.pconsole-kv > span:first-child{color:var(--dsw-alias-label-secondary,#5b6472);flex:none}
.pconsole-kv > span:last-child{min-width:0;text-align:right;overflow-wrap:anywhere}
.pconsole-btn{font:inherit;font-size:12px;color:var(--dsw-alias-label-primary,#1f2328);background:rgba(127,127,127,.08);border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:8px;padding:4px 10px;cursor:pointer;white-space:nowrap}
.pconsole-btn:hover:not(:disabled){border-color:var(--dsw-alias-button-primary-fill,#2f6fed);background:rgba(47,111,237,.12)}
.pconsole-btn:disabled{opacity:.5;cursor:default}
.pconsole-btn:focus-visible{outline:2px solid var(--dsw-focus-ring-color,var(--dsw-alias-button-primary-fill,#2f6fed));outline-offset:2px}
.pconsole-btn[aria-pressed="true"]{border-color:var(--dsw-alias-button-primary-fill,#2f6fed);background:rgba(47,111,237,.14)}
.pconsole-btn-primary{background:var(--dsw-alias-button-primary-fill,#2f6fed);border-color:transparent;color:var(--dsw-alias-label-primary-foreground,#fff)}.pconsole-btn-primary:hover:not(:disabled){background:var(--dsw-alias-button-primary-hover,#2f6fed)}
.pconsole-btn-danger{color:var(--dsw-alias-state-error-primary,#e5484d);border-color:rgba(229,72,77,.4)}
.pconsole-btn-danger:hover:not(:disabled){border-color:var(--dsw-alias-state-error-primary,#e5484d);background:rgba(229,72,77,.12)}
.pconsole-input,.pconsole-select{font:inherit;font-size:12px;color:inherit;background:transparent;border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:8px;padding:4px 8px;min-width:0}
.pconsole-input:focus-visible,.pconsole-select:focus-visible{outline:2px solid var(--dsw-focus-ring-color,var(--dsw-alias-button-primary-fill,#2f6fed));outline-offset:1px}
.pconsole-select{max-width:160px}
.pconsole-check{appearance:auto;margin:0;cursor:pointer;accent-color:var(--dsw-alias-button-primary-fill,#2f6fed)}
.pconsole-pre{margin:0;padding:11px 12px;border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:10px;background:rgba(127,127,127,.06);font-family:ui-monospace,Consolas,monospace;font-size:11.5px;line-height:17px;white-space:pre-wrap;word-break:break-word;max-height:320px;overflow:auto}
.pconsole-note{font-size:11.5px;color:var(--dsw-alias-label-secondary,#5b6472);line-height:17px;margin:8px 0 0}
.pconsole-error{color:var(--dsw-alias-state-error-primary,#e5484d);font-size:12px;margin:8px 0 0}
.pconsole-ok{color:var(--dsw-alias-state-success-primary,#2fbf71);font-size:12px;margin:8px 0 0}
.pconsole-warn{color:var(--dsw-alias-state-warn-primary,#b97800);font-size:12px;margin:8px 0 0}
.pconsole-chip{display:inline-flex;align-items:center;gap:4px;padding:0 7px;border-radius:99px;background:rgba(47,111,237,.14);color:var(--dsw-alias-brand-text,#2f6fed);font-size:10.5px;line-height:17px;white-space:nowrap}
.pconsole-chip-muted{background:rgba(127,127,127,.14);color:var(--dsw-alias-label-secondary,#5b6472)}
.pconsole-chip-ok{background:rgba(47,191,113,.16);color:var(--dsw-alias-state-success-primary,#2fbf71)}
.pconsole-chip-warn{background:rgba(185,120,0,.16);color:var(--dsw-alias-state-warn-primary,#b97800)}
.pconsole-chip-bad{background:rgba(229,72,77,.16);color:var(--dsw-alias-state-error-primary,#e5484d)}
.pconsole-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 10px}
.pconsole-search{display:flex;align-items:center;gap:6px;flex:1 1 240px;min-width:0}
.pconsole-search-input{flex:1 1 auto}
.pconsole-field{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;color:var(--dsw-alias-label-secondary,#5b6472);white-space:nowrap}
.pconsole-two-col{display:grid;grid-template-columns:minmax(280px,380px) minmax(0,1fr);gap:14px;align-items:start}
.pconsole-col{min-width:0}
.pconsole-list-meta{font-size:11.5px;color:var(--dsw-alias-label-secondary,#5b6472);margin:0 0 6px;display:flex;align-items:center;gap:8px;justify-content:space-between;flex-wrap:wrap}
.pconsole-list{display:flex;flex-direction:column;gap:6px;margin:0;padding:0;list-style:none;max-height:calc(100vh - 300px);overflow:auto}
.pconsole-item-row{display:flex;align-items:flex-start;gap:6px}
.pconsole-item-row > input{margin-top:8px}
.pconsole-item{display:block;width:100%;text-align:left;font:inherit;color:inherit;background:rgba(127,127,127,.03);border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-left:3px solid transparent;border-radius:9px;padding:7px 9px;cursor:pointer}
.pconsole-item:hover{background:rgba(127,127,127,.08)}
.pconsole-item[aria-selected="true"]{border-color:var(--dsw-alias-button-primary-fill,#2f6fed);border-left-color:var(--dsw-alias-button-primary-fill,#2f6fed);background:rgba(47,111,237,.08)}
.pconsole-item:focus-visible{outline:2px solid var(--dsw-focus-ring-color,var(--dsw-alias-button-primary-fill,#2f6fed));outline-offset:1px}
.pconsole-item-meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:2px;font-size:10.5px;color:var(--dsw-alias-label-secondary,#5b6472)}
.pconsole-item-text{font-size:12.5px;line-height:19px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.pconsole-hl{background:rgba(255,196,0,.35);color:inherit;border-radius:3px;padding:0 1px}
.pconsole-score{height:3px;border-radius:99px;background:rgba(127,127,127,.16);margin-top:5px;overflow:hidden}
.pconsole-score > span{display:block;height:100%;background:var(--dsw-alias-button-primary-fill,#2f6fed);border-radius:99px}
.pconsole-detail{border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:10px;padding:13px 14px;background:rgba(127,127,127,.02);min-height:160px}
.pconsole-detail-title{margin:0 0 8px;font-size:13px;font-weight:600;line-height:19px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pconsole-body-text{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:21px;margin:0 0 10px}
.pconsole-subhead{margin:14px 0 6px;font-size:11.5px;font-weight:600;color:var(--dsw-alias-label-secondary,#5b6472);letter-spacing:.02em}
.pconsole-bullets{margin:0;padding-left:18px;font-size:12.5px;line-height:19px}
.pconsole-bullets li{margin:2px 0;overflow-wrap:anywhere}
.pconsole-actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:8px 0 0}
.pconsole-timeline{margin:4px 0 0;padding:0;list-style:none}
.pconsole-timeline li{position:relative;padding:0 0 7px 13px;font-size:11.5px;color:var(--dsw-alias-label-secondary,#5b6472);overflow-wrap:anywhere}
.pconsole-timeline li:before{content:"";position:absolute;left:0;top:6px;width:6px;height:6px;border-radius:50%;background:var(--dsw-alias-button-primary-fill,#2f6fed);opacity:.7}
.pconsole-evidence{margin:0 0 6px;padding:7px 9px;border-radius:8px;background:rgba(127,127,127,.05);font-size:12px;line-height:18px;overflow-wrap:anywhere}
.pconsole-table{width:100%;border-collapse:collapse;font-size:11.5px}
.pconsole-table th{text-align:left;font-weight:600;color:var(--dsw-alias-label-secondary,#5b6472);padding:4px 8px;border-bottom:1px solid var(--dsw-alias-border-l2,#d0d5dd);white-space:nowrap}
.pconsole-table td{padding:4px 8px;border-bottom:.5px solid var(--dsw-alias-border-l2,#d0d5dd);overflow-wrap:anywhere}
.pconsole-table tr:last-child td{border-bottom:none}
.pconsole-confirm{margin-top:6px;padding:9px 11px;border:1px solid rgba(229,72,77,.4);border-radius:9px;background:rgba(229,72,77,.06);font-size:12px}
.pconsole-confirm p{margin:0}
.pconsole-danger{margin-top:14px;padding:10px 12px;border:1px solid rgba(229,72,77,.4);border-radius:10px;background:rgba(229,72,77,.04)}
.pconsole-danger summary{cursor:pointer;font-size:12px;font-weight:600;color:var(--dsw-alias-state-error-primary,#e5484d)}
.pconsole-empty{margin:0;padding:18px 0;color:var(--dsw-alias-label-secondary,#5b6472);font-size:12.5px}
.pconsole-code{font-family:ui-monospace,Consolas,monospace;font-size:11px;overflow-wrap:anywhere}
.pconsole-stage-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:6px;margin-top:8px}
.pconsole-stage{border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:8px;padding:6px 8px;background:rgba(127,127,127,.04);min-width:0}
.pconsole-stage.done{border-left:3px solid var(--dsw-alias-state-success-primary,#2fbf71)}
.pconsole-stage.bad{border-left:3px solid var(--dsw-alias-state-error-primary,#e5484d)}
.pconsole-stage.skip{opacity:.55}
.pconsole-stage-name{font-size:11.5px;color:var(--dsw-alias-label-secondary,#5b6472);display:flex;justify-content:space-between;gap:6px}
.pconsole-stage-value{font-size:11.5px;margin-top:1px;word-break:break-word}
.pconsole-bar{height:3px;border-radius:99px;background:rgba(127,127,127,.16);margin-top:4px;overflow:hidden}
.pconsole-bar > span{display:block;height:100%;border-radius:99px;background:var(--dsw-alias-button-primary-fill,#2f6fed)}
.pconsole-stage.done .pconsole-bar > span{background:var(--dsw-alias-state-success-primary,#2fbf71)}
.pconsole-stage.bad .pconsole-bar > span{background:var(--dsw-alias-state-error-primary,#e5484d)}
.pconsole-run{border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:10px;padding:8px 11px;background:rgba(127,127,127,.03);margin-bottom:8px}
.pconsole-run-toggle{border:none;background:transparent;color:inherit;font:inherit;width:100%;text-align:left;padding:1px 0;cursor:pointer;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.pconsole-run-toggle:hover .pconsole-run-day{color:var(--dsw-alias-brand-text,#2f6fed)}
.pconsole-run-caret{color:var(--dsw-alias-label-secondary,#5b6472);font-size:10px;width:11px;flex:none}
.pconsole-run-day{font-weight:600}
.pconsole-run-meta{font-size:11px;color:var(--dsw-alias-label-secondary,#5b6472)}
.pconsole-tag{font-size:10.5px;line-height:16px;padding:0 7px;border-radius:99px;background:rgba(127,127,127,.14);color:var(--dsw-alias-label-secondary,#5b6472);white-space:nowrap}
.pconsole-tag-ok{background:rgba(47,191,113,.16);color:var(--dsw-alias-state-success-primary,#2fbf71)}
.pconsole-tag-bad{background:rgba(229,72,77,.16);color:var(--dsw-alias-state-error-primary,#e5484d)}
.pconsole-progress{height:5px;border-radius:99px;background:rgba(127,127,127,.16);margin-top:6px;overflow:hidden}
.pconsole-progress > span{display:block;height:100%;background:var(--dsw-alias-button-primary-fill,#2f6fed);border-radius:99px;transition:width .4s ease}
.pconsole-trend{width:100%;height:70px;display:block}
.pconsole-trend polyline{fill:none;stroke:var(--dsw-alias-button-primary-fill,#2f6fed);stroke-width:1.5}
.pconsole-trend circle{fill:var(--dsw-alias-button-primary-fill,#2f6fed)}
.pconsole-trend .axis{stroke:var(--dsw-alias-border-l2,#d0d5dd);stroke-width:.5}
.pconsole-toast{margin:0 0 10px;padding:6px 11px;border-radius:8px;background:rgba(47,191,113,.12);color:var(--dsw-alias-state-success-primary,#2fbf71);font-size:12px}
.pconsole-run-inline{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pconsole-big-form{margin:10px 0;padding:10px 12px;border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:10px;background:rgba(127,127,127,.03)}
.pconsole-big-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:6px}
.pconsole-sr{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
@media (max-width:1100px){.pconsole-two-col{grid-template-columns:minmax(240px,320px) minmax(0,1fr)}}
@media (max-width:900px){.pconsole-two-col{grid-template-columns:1fr}.pconsole-list{max-height:none}.pconsole-nav{width:120px}.pconsole-main{padding:14px 16px 22px}.pconsole-head{padding:16px 16px 10px}}
@media (max-width:768px){.pconsole-shell{flex-direction:column}.pconsole-nav{width:100%;flex-direction:row;overflow-x:auto;border-right:none;border-bottom:1px solid var(--dsw-alias-border-l2,#d0d5dd);padding:6px 8px;gap:4px}.pconsole-nav-hint{display:none}.pconsole-title{font-size:17px}.pconsole-head-actions{width:100%}}
@media (prefers-reduced-motion:reduce){.pconsole *{transition:none;animation:none}}
.pconsole-status{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.pconsole-light{display:inline-flex;align-items:center;gap:5px;font-size:11px;color:var(--dsw-alias-label-secondary,#5b6472);white-space:nowrap}
.pconsole-dot{width:8px;height:8px;border-radius:50%;background:var(--dsw-alias-label-tertiary,#9aa3af);flex:none}
.pconsole-dot.ok{background:var(--dsw-alias-state-success-primary,#2fbf71)}
.pconsole-dot.warn{background:var(--dsw-alias-state-warn-primary,#b97800)}
.pconsole-dot.bad{background:var(--dsw-alias-state-error-primary,#e5484d)}
.pconsole-dot.unknown{background:var(--dsw-alias-label-tertiary,#9aa3af);opacity:.7}
.pconsole-health{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:9px;padding:7px 10px;background:rgba(127,127,127,.03);margin:10px 0 0;font-size:12px;color:var(--dsw-alias-label-secondary,#5b6472)}
.pconsole-health-main{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
.pconsole-health-btn{border:none;background:transparent;color:var(--dsw-alias-label-secondary,#5b6472);font:inherit;font-size:11.5px;cursor:pointer;padding:2px 4px;border-radius:6px}
.pconsole-health-btn:hover{background:rgba(127,127,127,.1);color:var(--dsw-alias-label-primary,#1f2328)}
.pconsole-health-btn:focus-visible{outline:2px solid var(--dsw-focus-ring-color,var(--dsw-alias-button-primary-fill,#2f6fed));outline-offset:1px}
.pconsole-seg{display:inline-flex;border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:8px;overflow:hidden}
.pconsole-seg-btn{font:inherit;font-size:12px;padding:4px 9px;border:none;background:transparent;color:var(--dsw-alias-label-secondary,#5b6472);cursor:pointer;white-space:nowrap}
.pconsole-seg-btn + .pconsole-seg-btn{border-left:1px solid var(--dsw-alias-border-l2,#d0d5dd)}
.pconsole-seg-btn[aria-pressed="true"]{background:rgba(47,111,237,.14);color:var(--dsw-alias-brand-text,#2f6fed);font-weight:600}
.pconsole-seg-btn:focus-visible{outline:2px solid var(--dsw-focus-ring-color,var(--dsw-alias-button-primary-fill,#2f6fed));outline-offset:-2px}
.pconsole-filter-panel{border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:9px;padding:8px 10px;background:rgba(127,127,127,.02);margin:0 0 10px}
.pconsole-filter-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
.pconsole-filter-title{font-size:11.5px;font-weight:600;color:var(--dsw-alias-label-secondary,#5b6472);margin:0}
.pconsole-group-tree{max-height:220px;overflow:auto;margin-top:6px;border-top:.5px solid var(--dsw-alias-border-l2,#d0d5dd);padding-top:6px}
.pconsole-group-row{display:flex;align-items:center;gap:5px;font-size:12px;line-height:18px;padding:1px 0}
.pconsole-group-caret{border:none;background:transparent;color:var(--dsw-alias-label-secondary,#5b6472);font:inherit;font-size:10px;width:14px;padding:0;cursor:pointer;flex:none}
.pconsole-group-row label{display:flex;align-items:center;gap:5px;min-width:0;cursor:pointer}
.pconsole-group-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pconsole-group-count{font-size:10.5px;color:var(--dsw-alias-label-tertiary,#9aa3af);flex:none}
.pconsole-inline-form{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 8px}
.pconsole-relation{border:1px solid var(--dsw-alias-border-l2,#d0d5dd);border-radius:8px;padding:6px 9px;margin:0 0 6px;font-size:12px;line-height:18px;background:rgba(127,127,127,.03)}
.pconsole-relation-head{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.pconsole-relation-pred{color:var(--dsw-alias-brand-text,#2f6fed);font-weight:600}
.pconsole-backup-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:.5px solid var(--dsw-alias-border-l2,#d0d5dd);font-size:12px;flex-wrap:wrap}
.pconsole-backup-row:last-child{border-bottom:none}
.pconsole-backup-meta{display:flex;gap:8px;flex-wrap:wrap;font-size:11px;color:var(--dsw-alias-label-secondary,#5b6472);min-width:0}
.pconsole-action-result{margin:8px 0 0}
`

    function useStyleTag(tag, css) {
      useEffect(() => {
        if (typeof document === 'undefined') return
        if (document.querySelector(`style[data-tag="${tag}"]`)) return
        const style = document.createElement('style')
        style.dataset.tag = tag
        style.textContent = css
        document.head.appendChild(style)
      }, [])
    }

    function useStyleOnce() {
      useStyleTag(CSS_TAG, CSS)
    }

    function useConsoleStyleOnce() {
      useStyleTag(CONSOLE_CSS_TAG, CONSOLE_CSS)
    }

    // ── 数据层：同源 fetch，统一错误 = Error(message) ──────────────────────────
    async function request(url, options = {}) {
      const res = await fetch(url, {
        headers: {
          accept: 'application/json',
          ...(options.body ? { 'content-type': 'application/json' } : {}),
        },
        ...options,
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok || data?.ok === false) {
        throw new Error(data?.error || `HTTP ${res.status}`)
      }
      return data
    }

    const api = (path, options) => request(API + path, options)
    const apiConsole = (path, options) => request(CONSOLE + path, options)

    // ── 格式化 ────────────────────────────────────────────────────────────────
    const fmtBytes = n => {
      const value = Number(n) || 0
      if (value < 1024) return `${value} B`
      if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
      return `${(value / 1024 / 1024).toFixed(1)} MB`
    }

    const fmtTime = iso => {
      if (!iso) return '—'
      const date = new Date(iso)
      return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString()
    }

    const fmtWhen = item => {
      const raw = String(item?.valid_from || item?.day || '')
      const parsed = new Date(raw)
      if (!Number.isNaN(parsed.getTime())) {
        const pad = value => String(value).padStart(2, '0')
        return `${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`
      }
      return raw
    }

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

    const pretty = value => {
      try {
        return typeof value === 'string' ? value : JSON.stringify(value, null, 2)
      } catch (ignored) {
        return String(value)
      }
    }

    const pickNumber = (...values) => {
      for (const value of values) {
        if (typeof value === 'number' && Number.isFinite(value)) return value
      }
      return undefined
    }

    const countText = value =>
      (typeof value === 'number' && Number.isFinite(value) ? String(value) : '—')

    const clampText = (text, max) => {
      const value = String(text ?? '')
      return value.length > max ? `${value.slice(0, max)}…` : value
    }

    const labelOf = (map, key) => map?.[key] ?? (key || '—')

    const describeEntry = entry => {
      if (entry === null || entry === undefined) return '—'
      if (typeof entry === 'string' || typeof entry === 'number') return String(entry)
      const text = entry.text ?? entry.label ?? entry.name ?? entry.title ?? entry.goal
        ?? entry.summary ?? entry.path
      if (typeof text === 'string' && text) return text
      return pretty(entry)
    }

    const numberAt = (stats, key) =>
      (typeof stats?.[key] === 'number' ? stats[key] : undefined)

    const stageId = stage => stage?.name || stage?.stage || '?'
    const stageLabel = stage => STAGE_LABELS[stageId(stage)] || stageId(stage)
    const statusText = run => STATUS_LABELS[String(run?.status ?? '')] || String(run?.status ?? '—')
    const triggerText = run => TRIGGER_LABELS[String(run?.trigger ?? '')] || String(run?.trigger ?? '')

    /** 每个阶段一句人话；数值单位尽量说清（条 / 个 / 次）。 */
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
        case 's1_collect':
          add('记忆', n('memories'), ' 条')
          add('召回', n('recalls'), ' 次')
          add('待办', n('backlog'), ' 条')
          break
        case 's2_dedup':
          add('合并', n('merged'), ' 条')
          add('候选', n('candidates'), ' 组')
          break
        case 's3_relations':
          add('扫描', n('scanned'), ' 条')
          add('提及', n('mentions'), ' 次')
          add('新关系', n('relations_created'), ' 条')
          break
        case 's4_conflicts':
          add('发现', n('found'), ' 处')
          add('新建', n('created'), ' 条')
          break
        case 's5_patterns':
          add('规律', n('patterns_created'), ' 条')
          add('经验', n('experience_created'), ' 条')
          break
        case 's6_traits':
          add('新增', n('created'))
          add('活跃', n('active'))
          add('退役', n('retired'))
          break
        case 's7_reflect':
          add('质量分', n('quality'))
          add('缺陷', n('defects'))
          add('未决冲突', n('conflicts_open'))
          break
        case 's8_snapshot': {
          const size = n('size_bytes')
          if (size !== undefined) parts.push(`快照 ${(size / 1024).toFixed(0)} KB`)
          add('记忆', n('memory_count'), ' 条')
          break
        }
        default: {
          for (const [key, value] of Object.entries(stats)) {
            if (value === null || value === undefined || value === '' || typeof value === 'object') continue
            if (parts.length >= 4) break
            parts.push(`${key} ${typeof value === 'boolean' ? (value ? '是' : '否') : value}`)
          }
        }
      }
      return parts.join(' · ')
    }

    /** 阶段小条形图的量：优先 ms（耗时），没有就用阶段里最大的数值。 */
    function stageBar(stage) {
      if (!stage) return null
      const ms = numberAt(stage, 'ms')
      if (ms !== undefined) return { ratio: Math.min(1, ms / 30000), label: ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s` }
      const stats = stage.stats && typeof stage.stats === 'object' ? stage.stats : null
      if (!stats) return null
      let max = 0
      let label = ''
      let unit = ''
      if (numberAt(stats, 'memories') !== undefined) { max = stats.memories; label = `记忆 ${max}`; unit = '条' }
      else if (numberAt(stats, 'scanned') !== undefined) { max = stats.scanned; label = `扫描 ${max}` }
      else if (numberAt(stats, 'merged') !== undefined) { max = stats.merged; label = `合并 ${max}`; unit = '条' }
      if (!label) return null
      return { ratio: Math.min(1, max / 200), label: `${label}${unit}` }
    }

    function NightStage({ stage }) {
      const skipped = stage?.skipped === true
      const failed = !skipped && stage?.ok === false
      const ms = typeof stage?.ms === 'number' ? ` · ${stage.ms}ms` : ''
      const summary = stageSummary(stage)
      const bar = stageBar(stage)
      return h(
        'div',
        { className: 'pconsole-stage ' + (skipped ? 'skip' : failed ? 'bad' : 'done') },
        h('div', { className: 'pconsole-stage-name' },
          h('span', null, `${stageLabel(stage)}${ms}`),
          bar ? h('span', null, bar.label) : null),
        summary ? h('div', { className: 'pconsole-stage-value' }, summary) : null,
        bar
          ? h('div', { className: 'pconsole-bar', 'aria-hidden': 'true' },
              h('span', { style: { width: `${Math.max(3, Math.round(bar.ratio * 100))}%` } }))
          : null,
      )
    }

    function NightRun({ run, open, onToggle }) {
      if (!run) return null
      const status = String(run.status ?? '')
      const statusClass = status === 'ok' ? ' pconsole-tag-ok' : status === 'running' ? '' : ' pconsole-tag-bad'
      const stages = (run.stages ?? []).filter(Boolean)
      const okCount = stages.filter(stage => stage.skipped !== true && stage.ok !== false).length
      const clock = [fmtClock(run.started_at), fmtClock(run.finished_at)].filter(Boolean).join(' → ')
      const duration = fmtDuration(run)
      return h(
        'div',
        { className: 'pconsole-run' },
        h(
          'button',
          {
            type: 'button',
            className: 'pconsole-run-toggle',
            'aria-expanded': open ? 'true' : 'false',
            onClick: onToggle,
          },
          h('span', { className: 'pconsole-run-caret' }, open ? '▾' : '▸'),
          h('span', { className: 'pconsole-run-day' }, run.day ?? '—'),
          h('span', { className: 'pconsole-tag' + statusClass }, statusText(run)),
          run.trigger ? h('span', { className: 'pconsole-tag' }, triggerText(run)) : null,
          h('span', { className: 'pconsole-run-meta' },
            `${okCount}/${stages.length || 8} 阶段${duration ? ` · 耗时 ${duration}` : ''}${clock ? ` · ${clock}` : ''}`),
        ),
        run.error ? h('div', { className: 'pconsole-error' }, run.error) : null,
        open ? h('div', { className: 'pconsole-stage-grid' },
          ...stages.map((stage, index) =>
            h(NightStage, { key: `${stageId(stage)}-${index}`, stage }))) : null,
      )
    }

    /** 最近 14 次质量分折线（手写 SVG，不引图表库）。 */
    function QualityTrend({ runs }) {
      const points = []
      const runsList = Array.isArray(runs) ? runs.slice().reverse() : []
      for (const run of runsList) {
        const reflect = (run.stages ?? []).find(stage => stageId(stage) === 's7_reflect')
        const quality = numberAt(reflect?.stats, 'quality')
        if (quality !== undefined) points.push({ quality, day: run.day ?? '' })
      }
      if (points.length < 2) {
        return h('p', { className: 'pconsole-note', style: { margin: 0 } }, '质量分样本不足（至少两次睡眠）。')
      }
      const width = 420
      const height = 70
      const pad = 6
      const max = Math.max(...points.map(point => point.quality), 100)
      const min = Math.min(...points.map(point => point.quality), 0)
      const span = max - min || 1
      const coords = points.map((point, index) => {
        const x = pad + (index * (width - pad * 2)) / (points.length - 1)
        const y = height - pad - ((point.quality - min) / span) * (height - pad * 2)
        return `${x.toFixed(1)},${y.toFixed(1)}`
      })
      return h(
        'svg',
        {
          className: 'pconsole-trend',
          viewBox: `0 0 ${width} ${height}`,
          role: 'img',
          'aria-label': `最近 ${points.length} 次睡眠质量分趋势`,
        },
        h('line', { className: 'axis', x1: pad, y1: height - pad, x2: width - pad, y2: height - pad }),
        h('polyline', { points: coords.join(' ') }),
        ...points.map((point, index) =>
          h('circle', {
            key: `q-${index}`,
            cx: coords[index].split(',')[0],
            cy: coords[index].split(',')[1],
            r: 2.2,
            title: `${point.day} · ${point.quality}`,
          })),
      )
    }

    function MetricCard({ title, value, sub }) {
      return h(
        'div',
        { className: 'pconsole-card' },
        h('p', { className: 'pconsole-card-title' }, title),
        h('div', { className: 'pconsole-metric' }, value),
        sub ? h('div', { className: 'pconsole-metric-sub' }, sub) : null,
      )
    }

    function Panel({ title, actions, children, note }) {
      return h(
        'section',
        { className: 'pconsole-section' },
        (title || actions)
          ? h('div', { className: 'pconsole-section-head' },
              title ? h('h2', { className: 'pconsole-section-title' }, title) : h('span'),
              actions ? h('div', { className: 'pconsole-section-actions' }, actions) : null)
          : null,
        children,
        note ? h('p', { className: 'pconsole-note' }, note) : null,
      )
    }

    const kvRow = (label, value) =>
      (value === undefined || value === null || value === ''
        ? null
        : h('div', { className: 'pconsole-kv', key: `kv-${label}` },
            h('span', null, label),
            h('span', null, String(value))))

    function Switch({ checked, onChange, disabled, label }) {
      return h(
        'label',
        { className: 'pconsole-field' },
        h('input', {
          type: 'checkbox',
          className: 'pconsole-check',
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
        { style: { display: 'flex', alignItems: 'center', gap: '6px' } },
        h('input', {
          className: 'pconsole-input',
          type: 'number',
          min: spec?.min,
          max: spec?.max,
          step: spec?.step || 1,
          value: draft,
          disabled: !!disabled,
          style: { width: '104px' },
          'aria-label': spec?.label || '数值',
          onChange: event => setDraft(event.target.value),
        }),
        spec?.unit ? h('span', { className: 'pconsole-note', style: { margin: 0 } }, spec.unit) : null,
        h('button', {
          className: 'pconsole-btn',
          type: 'button',
          disabled: !!disabled || !dirty,
          onClick: () => onSave(Number(draft)),
        }, dirty ? '保存' : '已保存'),
      )
    }

    // ── 文本 / 高亮 / 复制 ─────────────────────────────────────────────────────
    function highlightParts(text, query) {
      const source = String(text ?? '')
      const tokens = String(query ?? '')
        .split(/\s+/)
        .map(token => token.trim())
        .filter(Boolean)
        .slice(0, 8)
      if (!tokens.length) return [source]
      const escaped = tokens.map(token => token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
      let pattern = null
      try {
        pattern = new RegExp(`(${escaped.join('|')})`, 'gi')
      } catch (ignored) {
        return [source]
      }
      return source.split(pattern)
    }

    function Highlighted({ text, query }) {
      const parts = highlightParts(text, query)
      if (parts.length === 1) return h('span', null, parts[0])
      return h('span', null, ...parts.map((part, index) =>
        (index % 2
          ? h('mark', { key: index, className: 'pconsole-hl' }, part)
          : h('span', { key: index }, part))))
    }

    async function copyText(text, notify, success = '已复制') {
      const value = String(text ?? '')
      if (!value) {
        notify?.('没有可复制的内容')
        return
      }
      try {
        if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(value)
          notify?.(success)
          return
        }
      } catch (ignored) {
        // 剪贴板被拒时走下面的提示
      }
      notify?.('这个环境不让写剪贴板，手动选中文本复制吧')
    }

    const formatDoctor = payload => {
      if (!payload) return ''
      const inner = payload.doctor ?? payload.report ?? payload.summary ?? payload
      const head = typeof inner === 'string' ? inner : pretty(inner)
      const statsPart = payload.stats && typeof payload.stats === 'object' ? pretty(payload.stats) : ''
      return statsPart ? `${head}\n\n${statsPart}` : head
    }

    const cliGet = id => `pmem get ${id} --json`

    // 409（同名多身份）和文件下载需要原始响应：request() 会「非 2xx 就抛」。
    async function requestRaw(url, options = {}) {
      const res = await fetch(url, {
        headers: {
          accept: 'application/json',
          ...(options.body ? { 'content-type': 'application/json' } : {}),
        },
        ...options,
      })
      const data = await res.json().catch(() => ({}))
      return { status: res.status, ok: res.ok, data, res }
    }

    /** 备份下载：fetch → blob → a[download]。不开新窗口、不整页导航。 */
    async function downloadBlob(url, filename, notify) {
      try {
        const res = await fetch(url, { headers: { accept: 'application/octet-stream' } })
        if (!res.ok) {
          const data = await res.json().catch(() => ({}))
          throw new Error(data?.error || `HTTP ${res.status}`)
        }
        const blob = await res.blob()
        const objectUrl = URL.createObjectURL(blob)
        const link = document.createElement('a')
        link.href = objectUrl
        link.download = filename
        link.rel = 'noreferrer'
        link.style.display = 'none'
        document.body.appendChild(link)
        link.click()
        document.body.removeChild(link)
        setTimeout(() => URL.revokeObjectURL(objectUrl), 4000)
        notify?.(`已开始下载 ${filename}`)
      } catch (cause) {
        notify?.(`下载失败：${cause.message}`)
      }
    }

    const timeAgo = iso => {
      const time = Date.parse(String(iso ?? ''))
      if (!Number.isFinite(time)) return '—'
      const seconds = Math.max(0, Math.round((Date.now() - time) / 1000))
      if (seconds < 60) return `${seconds} 秒前`
      const minutes = Math.round(seconds / 60)
      if (minutes < 60) return `${minutes} 分钟前`
      const hours = Math.round(minutes / 60)
      if (hours < 24) return `${hours} 小时前`
      return `${Math.round(hours / 24)} 天前`
    }

    const fmtTimeShort = value => {
      const date = value instanceof Date ? value : new Date(value)
      if (Number.isNaN(date.getTime())) return '—'
      const pad = n => String(n).padStart(2, '0')
      return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
    }

    /** 顶栏状态灯：能推断的给绿/红，推断不了的必须显示灰（unknown），不装懂。 */
    function deriveLights(boot, status) {
      const degraded = Array.isArray(boot?.degraded) ? boot.degraded : []
      const db = status?.db
      let dbLevel = 'unknown'
      if (db && typeof db === 'object') {
        dbLevel = db.integrity_ok === true
          && !degraded.includes('counts') && !degraded.includes('stats') ? 'ok' : 'bad'
      } else if (degraded.includes('stats')) {
        dbLevel = 'bad'
      }
      const mcpLevel = status?.mcp && typeof status.mcp.tools === 'number'
        ? (status.mcp.tools > 0 ? 'ok' : 'bad')
        : 'unknown'
      // 模型通道：宿主经 /console/bootstrap 的 model 块给出只读探测结果。
      // available=true → 绿；false → 红（宿主明确说没通道）；没拿到 → 灰，不装懂。
      // 注意：这只代表「配置里有」，不代表凭证一定有效（那要发请求才知道）。
      const modelInfo = boot?.model ?? status?.model ?? null
      const modelLevel = modelInfo && typeof modelInfo === 'object'
        ? (modelInfo.available === true ? 'ok' : 'bad')
        : 'unknown'
      const modelName = modelInfo && typeof modelInfo === 'object'
        ? [modelInfo.provider, modelInfo.model].filter(Boolean).join(' / ') || (modelInfo.providers?.length ? modelInfo.providers.join(' / ') : '')
        : ''
      return {
        db: {
          level: dbLevel,
          label: '数据库',
          hint: dbLevel === 'ok'
            ? '完整性 ok、计数可读'
            : dbLevel === 'bad' ? '完整性异常或计数降级' : '未拿到 /status 的数据库状态',
        },
        model: {
          level: modelLevel,
          label: '模型通道',
          hint: modelLevel === 'ok'
            ? `已配置：${modelName || '见宿主导出'}`
            : modelLevel === 'bad'
              ? `宿主没有可用的模型通道${modelInfo?.note ? `（${modelInfo.note}）` : ''}`
              : '宿主还没返回模型状态（灰）',
        },
        mcp: {
          level: mcpLevel,
          label: 'MCP',
          hint: mcpLevel === 'ok' ? `${status.mcp.tools} 个工具` : '未拿到 MCP 状态',
        },
      }
    }

    function StatusLights({ lights }) {
      return h(
        'div',
        { className: 'pconsole-status', 'aria-label': '系统状态灯' },
        ...['db', 'model', 'mcp'].map(key => {
          const light = lights?.[key] ?? { level: 'unknown', label: key, hint: '' }
          return h(
            'span',
            { key, className: 'pconsole-light', title: `${light.label}：${light.hint}` },
            h('span', { className: `pconsole-dot ${light.level}`, 'aria-hidden': 'true' }),
            h('span', null, light.label),
          )
        }),
      )
    }

    const MEMORY_SORTS = [
      ['relevance', '相关度'],
      ['time', '时间'],
      ['grade', '重要度'],
      ['uses', '使用次数'],
    ]

    // 排序分段按钮 → 后端 sort 参数（有词默认 relevance、无词 time；grade 对应 importance）。
    const MEMORY_SORT_API = {
      relevance: 'relevance',
      time: 'time',
      grade: 'importance',
      uses: 'uses',
    }

    /** /console/groups 的 tree → 过滤面板用的扁平树（保留层级、父路径）。 */
    function flattenGroupTree(nodes, depth = 0, parent = '', out = []) {
      for (const node of Array.isArray(nodes) ? nodes : []) {
        if (!node || typeof node !== 'object') {
          if (typeof node === 'string' && node) out.push({ path: node, count: undefined, depth, parent })
          continue
        }
        const path = String(node.path ?? node.name ?? node.id ?? node.key ?? '').trim()
        if (!path) continue
        out.push({
          path,
          count: pickNumber(node.count, node.total, node.memories, node.size),
          depth,
          parent,
        })
        const children = node.children ?? node.items ?? node.nodes
        if (Array.isArray(children) && children.length) flattenGroupTree(children, depth + 1, path, out)
      }
      return out
    }

    /** 选中的分组要「含子组」：条目 groups 里任一命中或位于选中路径之下。 */
    function matchesGroups(item, selected) {
      if (!selected.length) return true
      const groups = Array.isArray(item?.groups) ? item.groups.map(String) : []
      return groups.some(group =>
        selected.some(path =>
          group === path || group.startsWith(`${path}/`) || group.startsWith(`${path}.`)))
    }

    // ── 设置页里的入口：只剩一行 ─────────────────────────────────────────────
    function PMemSection({ onOpenConsole } = {}) {
      useStyleOnce()
      return h(
        'div',
        { className: 'pmem-app' },
        h(
          'div',
          { className: 'pmem-head' },
          h('p', { className: 'pmem-kicker' }, 'PMemories · 可塑性记忆'),
          h('h2', { className: 'pmem-title' }, '控制台已独立',
            h('span', { className: 'pmem-chip' }, 'M22')),
          h('p', { className: 'pmem-sub' },
            '七个标签已经搬进整页控制台：总览 / 记忆 / 实体 / 睡眠 / 设置。'),
          h(
            'div',
            { style: { display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', marginTop: '12px' } },
            h('button', {
              className: 'pmem-btn pmem-btn-primary',
              type: 'button',
              onClick: () => onOpenConsole?.(),
            }, '打开控制台'),
            h('span', { className: 'pmem-row-sub' },
              '左侧导航也有「记忆控制台」入口，两处进的是同一个整页面板'),
          ),
        ),
      )
    }

    /** 侧边栏图标：16px 内联 SVG（小鲸鱼轮廓），颜色继承 currentColor。 */
    function ConsolePanelIcon({ size = 16, active } = {}) {
      const px = typeof size === 'number' && size > 0 ? size : 16
      return h(
        'svg',
        {
          width: px,
          height: px,
          viewBox: '0 0 16 16',
          fill: 'none',
          stroke: 'currentColor',
          strokeWidth: 1.35,
          strokeLinecap: 'round',
          strokeLinejoin: 'round',
          'aria-hidden': 'true',
          focusable: 'false',
          'data-active': active ? 'true' : 'false',
        },
        // 海马轮廓：吻部朝左、卷尾在右下；16px 下也能认出来。
        h('path', { d: 'M2.1 5.6c.5-.8 1.4-1.2 2.3-1 .5.1.9.4 1.2.8' }),
        h('path', { d: 'M5.6 5.4c1.6-.7 3.5-.2 4.3 1.3.6 1.1.4 2.5-.5 3.4' }),
        h('path', { d: 'M9.4 10.1c-.8 1-2.2 1.4-3.3 1-1-.4-1.4-1.5-.9-2.3.4-.6 1.2-.8 1.8-.5.5.2.8.7.7 1.2' }),
        h('path', { d: 'M2.1 5.6c-.5.3-.9.8-1 1.4' }),
        h('path', { d: 'M6.3 3.9c.3-.5.9-.8 1.5-.8M9.6 6.6c.5-.2 1-.3 1.6-.2' }),
        h('circle', { cx: '4.4', cy: '5.3', r: '0.5', fill: 'currentColor', stroke: 'none' }),
      )
    }

    // ── 整页控制台外壳 ────────────────────────────────────────────────────────
    function ConsolePage() {
      useStyleOnce()
      useConsoleStyleOnce()
      const [nav, setNav] = useState('overview')
      const [boot, setBoot] = useState(null)
      const [status, setStatus] = useState(null)
      const [loading, setLoading] = useState(true)
      const [error, setError] = useState('')
      const [toast, setToast] = useState('')
      const [refreshedAt, setRefreshedAt] = useState(null)
      // 总览/实体页点记忆 → 记忆页直接选中这条。
      const [memoryTarget, setMemoryTarget] = useState('')

      const notify = useCallback(message => {
        setToast(message)
        if (typeof setTimeout === 'function') setTimeout(() => setToast(''), 3000)
      }, [])

      const goto = useCallback((next, options) => {
        if (next === 'memory' && options?.selectId) setMemoryTarget(String(options.selectId))
        setNav(next)
      }, [])

      const loadBootstrap = useCallback(async () => {
        setLoading(true)
        const [bootResult, statusResult] = await Promise.allSettled([
          apiConsole('/bootstrap'),
          api('/status'),
        ])
        if (statusResult.status === 'fulfilled') setStatus(statusResult.value)
        if (bootResult.status === 'fulfilled') {
          const data = bootResult.value
          setBoot(data && typeof data === 'object' ? data : {})
          setError('')
          setRefreshedAt(new Date())
        } else {
          setBoot(null)
          const message = bootResult.reason?.message ?? '未知错误'
          setError(`控制台数据没取到：${message}（点右上角「刷新」重试）`)
        }
        setLoading(false)
      }, [])

      useEffect(() => {
        void loadBootstrap()
      }, [loadBootstrap])

      // 总览每 30s 轮询一次；其他分区按需拉取（各自组件 mount 时取）。
      useEffect(() => {
        if (nav !== 'overview') return undefined
        const timer = setInterval(() => { void loadBootstrap() }, 30000)
        return () => clearInterval(timer)
      }, [nav, loadBootstrap])

      const version = boot?.version ?? status?.version ?? '—'
      const lights = deriveLights(boot, status)
      // 分区多了以后用 if 链别再套三元：嵌套 `? :` 一深，插一个分区就要重排缩进。
      let body
      if (nav === 'memory') {
        body = h(ConsoleMemory, {
          notify,
          initialId: memoryTarget,
          onConsumeInitial: () => setMemoryTarget(''),
        })
      } else if (nav === 'wake') {
        body = h(ConsoleWake, { boot, status, goto, notify })
      } else if (nav === 'entities') {
        body = h(ConsoleEntities, { notify, goto })
      } else if (nav === 'sleep') {
        body = h(ConsoleSleep, { notify })
      } else if (nav === 'settings') {
        body = h(ConsoleSettings, { boot, notify })
      } else {
        body = h(ConsoleOverview, { boot, status, loading, notify, goto, refreshedAt })
      }

      return h(
        'div',
        { className: 'pconsole', 'data-pmem-console': '1' },
        h(
          'header',
          { className: 'pconsole-head' },
          h(
            'div',
            { className: 'pconsole-head-main' },
            h('p', { className: 'pconsole-kicker' }, 'PMemories · 可塑性记忆'),
            h('h1', { className: 'pconsole-title' }, '记忆控制台',
              h('span', { className: 'pconsole-chip' }, `v${version}`)),
            h('p', { className: 'pconsole-sub' },
              '整页管理台：总览 / 醒来卡 / 记忆 / 实体 / 睡眠 / 设置。全部同源 fetch，不开新窗口。'),
          ),
          h(
            'div',
            {
              className: 'pconsole-head-actions',
              style: { flexDirection: 'column', alignItems: 'flex-end', gap: '6px' },
            },
            toast ? h('span', { className: 'pconsole-ok', role: 'status', style: { margin: 0 } }, toast) : null,
            h(StatusLights, { lights }),
            h(
              'div',
              { style: { display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', justifyContent: 'flex-end' } },
              h('span', { className: 'pconsole-note', style: { margin: 0 } },
                refreshedAt ? `最后刷新 ${fmtTimeShort(refreshedAt)}` : '还没刷新'),
              h('button', {
                className: 'pconsole-btn',
                type: 'button',
                'aria-label': '刷新控制台数据',
                disabled: loading,
                onClick: () => void loadBootstrap(),
              }, loading ? '刷新中…' : '刷新'),
            ),
          ),
        ),
        h(
          'div',
          { className: 'pconsole-shell' },
          h(
            'nav',
            {
              className: 'pconsole-nav',
              role: 'tablist',
              'aria-label': '控制台分区',
              'aria-orientation': 'vertical',
            },
            ...CONSOLE_NAV.map(([id, label]) =>
              h(
                'button',
                {
                  key: id,
                  type: 'button',
                  role: 'tab',
                  id: `pconsole-tab-${id}`,
                  'aria-selected': nav === id ? 'true' : 'false',
                  'aria-controls': 'pconsole-panel',
                  tabIndex: nav === id ? 0 : -1,
                  className: 'pconsole-nav-item',
                  onClick: () => setNav(id),
                },
                label,
              )),
            h('p', { className: 'pconsole-nav-hint' },
              '数据同源 fetch；危险操作都要二次确认。'),
          ),
          h(
            'div',
            {
              className: 'pconsole-main',
              role: 'tabpanel',
              id: 'pconsole-panel',
              'aria-labelledby': `pconsole-tab-${nav}`,
              tabIndex: -1,
            },
            error ? h('p', { className: 'pconsole-banner', role: 'alert' }, error) : null,
            body,
          ),
        ),
      )
    }

    // ===== 分区组件（下面依次实现）=============================================
    function ConsoleOverview({ boot, status, loading, notify, goto, refreshedAt }) {
      const counts = boot?.counts ?? {}
      const db = status?.db ?? {}
      const tokens = status?.tokens ?? null
      const recent = Array.isArray(boot?.recent) ? boot.recent.slice(0, 10) : []
      const latestRun = boot?.sleep?.latest ?? null

      const [health, setHealth] = useState(null)
      const [healthBusy, setHealthBusy] = useState(false)
      const [healthOpen, setHealthOpen] = useState(false)
      const [doctor, setDoctor] = useState(null)
      const [doctorBusy, setDoctorBusy] = useState(false)
      const [starmapUrl, setStarmapUrl] = useState('')
      const [starmapBusy, setStarmapBusy] = useState(false)
      const [sleeping, setSleeping] = useState(false)
      const [wakeInfo, setWakeInfo] = useState(status?.wake ?? null)
      const [err, setErr] = useState('')

      const loadHealth = useCallback(async () => {
        setHealthBusy(true)
        try {
          setHealth(await api('/debug'))
        } catch (cause) {
          setHealth(null)
          setErr(`请求日志没取到：${cause.message}`)
        } finally {
          setHealthBusy(false)
        }
      }, [])

      useEffect(() => {
        void loadHealth()
      }, [loadHealth])

      const refreshWake = async () => {
        setErr('')
        try {
          const data = await api('/wake?budget=300')
          setWakeInfo({ tokens: data?.tokens ?? null, chars: data?.chars ?? null, head: data?.head ?? '' })
          notify?.('醒来卡已重取（不落库）')
        } catch (cause) {
          setErr(`刷新醒来卡失败：${cause.message}`)
        }
      }

      const runDoctor = async () => {
        setDoctorBusy(true)
        setErr('')
        try {
          setDoctor(await apiConsole('/doctor'))
        } catch (cause) {
          setErr(`体检失败：${cause.message}`)
        } finally {
          setDoctorBusy(false)
        }
      }

      const openStarmap = async () => {
        setStarmapBusy(true)
        setErr('')
        try {
          const data = await api('/starmap/open', { method: 'POST', body: '{}' })
          if (data?.url) setStarmapUrl(data.url)
          if (data?.opened === false) setErr(data.warning || '浏览器没拉起来，地址在下面，自己点一下')
          else notify?.('已在浏览器打开星图')
        } catch (cause) {
          setErr(`打开星图失败：${cause.message}`)
        } finally {
          setStarmapBusy(false)
        }
      }

      const sleepNow = async () => {
        setSleeping(true)
        setErr('')
        try {
          const data = await api('/sleep/now', { method: 'POST', body: '{}' })
          if (!data?.ok) setErr(`入睡失败：${data?.error || '未知'}`)
          else notify?.(data?.data?.status === 'ok' ? '跑完了（去「睡眠」看流水）' : `跑完：${data?.data?.status ?? '未知'}`)
        } catch (cause) {
          setErr(`入睡失败：${cause.message}`)
        } finally {
          setSleeping(false)
        }
      }

      const requests = health && Array.isArray(health.requests) ? health.requests : null
      const badRequests = requests ? requests.filter(item => Number(item?.status) >= 400) : []
      const count4xx = badRequests.filter(item => Number(item?.status) < 500).length
      const count5xx = badRequests.filter(item => Number(item?.status) >= 500).length
      const anomalies = badRequests.slice(-5).reverse()
      const apiOk = requests ? count4xx + count5xx === 0 : null
      const tokenValue = pickNumber(tokens?.p95, tokens?.mean, tokens?.total, tokens?.tokens)
      const tokenSamples = pickNumber(tokens?.samples)
      const reflectStage = (latestRun?.stages ?? []).find(stage => stageId(stage) === 's7_reflect')
      const quality = numberAt(reflectStage?.stats, 'quality')
      // 今日召回：后端正在加；没有就退到「近 7 天新增」，都没有显示 —。
      const todayRecalls = pickNumber(counts.today_recalls, counts.today_recall, counts.recalls_today)
      const weekNew = pickNumber(counts.week_new, counts.last7d_new, counts.recent_new)

      if (!boot) {
        return h('p', { className: 'pconsole-empty' },
          loading ? '正在读控制台数据…' : '没有拿到控制台数据，点右上角「刷新」再试。')
      }

      // 请求健康：平时一行；点「详情」展开最近异常与日志路径（不再占一整块卡片）。
      const healthLine = h(
        'div',
        { className: 'pconsole-health' },
        h(
          'div',
          { className: 'pconsole-health-main' },
          h('span', {
            className: `pconsole-dot ${apiOk === null ? 'unknown' : apiOk ? 'ok' : 'bad'}`,
            'aria-hidden': 'true',
          }),
          h('span', null,
            apiOk === null ? 'API 状态未知（/debug 拿不到）'
              : apiOk ? 'API 正常' : 'API 有异常'),
          h('span', null, requests
            ? `最近 ${requests.length} 条请求 · 4xx ${count4xx} · 5xx ${count5xx}`
            : '请求日志 —'),
          h('span', null, `醒来卡 ${refreshedAt ? timeAgo(refreshedAt) : '—'}`),
        ),
        h('div', { className: 'pconsole-health-main' },
          h('button', {
            className: 'pconsole-health-btn',
            type: 'button',
            'aria-expanded': healthOpen ? 'true' : 'false',
            onClick: () => setHealthOpen(prev => !prev),
          }, healthOpen ? '收起详情' : '详情'),
          h('button', {
            className: 'pconsole-health-btn',
            type: 'button',
            disabled: healthBusy,
            onClick: () => void loadHealth(),
          }, healthBusy ? '刷新中…' : '刷新日志'),
        ),
      )

      return h(
        'div',
        { className: 'pconsole-overview' },
        err ? h('p', { className: 'pconsole-banner', role: 'alert' }, err) : null,
        (Array.isArray(boot.degraded) && boot.degraded.length)
          ? h('p', { className: 'pconsole-note' },
              `后端部分数据降级：${boot.degraded.join(' / ')}（其他分区不受影响）`)
          : null,

        // 空库引导：memory_total === 0 时不白屏，直接给「下一步做什么」。
        (pickNumber(counts.memory_total, db.memory_total) === 0)
          ? h(
              Panel,
              { title: '空库引导：先让库里有一条记忆' },
              h('p', { className: 'pconsole-note', style: { margin: 0 } },
                '库还是空的。按下面任意一条走，写完回这页点「刷新」就能看到指标和最近记忆。'),
              h('div', { className: 'pconsole-actions', style: { marginTop: '8px' } },
                h('button', {
                  className: 'pconsole-btn',
                  type: 'button',
                  onClick: () => void copyText('pmem init', notify, '已复制建库命令'),
                }, '复制建库命令（pmem init）'),
                h('button', {
                  className: 'pconsole-btn',
                  type: 'button',
                  onClick: () => void copyText('pmem remember "第一条记忆"', notify, '已复制写第一条命令'),
                }, '复制写第一条（pmem remember）'),
                h('button', {
                  className: 'pconsole-btn',
                  type: 'button',
                  onClick: () => void copyText('pmem import 路径/memories.jsonl', notify, '已复制导入命令'),
                }, '复制导入 JSONL'),
                goto
                  ? h('button', {
                      className: 'pconsole-btn',
                      type: 'button',
                      onClick: () => goto('settings'),
                    }, '去设置看排查')
                  : null),
              h('p', { className: 'pconsole-note', style: { margin: '6px 0 0' } },
                '文档：README.md / INSTALL-FOR-DSH.md（终端打开）；MCP 工具清单见 README。'),
            )
          : null,

        healthLine,
        healthOpen
          ? h('div', { style: { marginTop: '8px' } },
              anomalies.length
                ? h('table', { className: 'pconsole-table' },
                    h('thead', null, h('tr', null,
                      h('th', null, '时间'), h('th', null, '方法'), h('th', null, '路径'),
                      h('th', null, '状态'), h('th', null, '耗时'))),
                    h('tbody', null, ...anomalies.map((item, index) =>
                      h('tr', { key: `bad-${index}` },
                        h('td', null, fmtClock(item?.ts) || '—'),
                        h('td', null, item?.method || '—'),
                        h('td', null, clampText(item?.path, 60)),
                        h('td', {
                          className: Number(item?.status) >= 500 ? 'pconsole-error' : 'pconsole-warn',
                        }, String(item?.status ?? '—')),
                        h('td', null, item?.ms !== undefined ? `${item.ms}ms` : '—')))))
                : h('p', { className: 'pconsole-note', style: { margin: '4px 0 0' } }, '最近没有 4xx/5xx。'),
              health?.logFile
                ? h('p', { className: 'pconsole-note', style: { margin: '4px 0 0' } },
                    '日志：', h('code', { className: 'pconsole-code' }, health.logFile))
                : null)
          : null,

        // 指标条：数字优先来自 bootstrap.counts（后端算好），缺了退 /status.db。
        h(
          'div',
          { className: 'pconsole-grid', style: { marginTop: '12px' } },
          h(MetricCard, {
            title: '记忆总量',
            value: countText(pickNumber(counts.memory_total, db.memory_total)),
            sub: `活跃 ${countText(pickNumber(db.by_status?.active))}`,
          }),
          h(MetricCard, {
            title: '今日新增',
            value: countText(counts.today_new),
            sub: '今天写进库的记忆',
          }),
          h(MetricCard, {
            title: '今日召回',
            value: countText(todayRecalls ?? weekNew),
            sub: todayRecalls !== undefined
              ? '今天被召回（浮现 / 检索命中）的次数'
              : weekNew !== undefined
                ? `今日召回未提供，显示近 7 天新增 ${weekNew}`
                : '后端未提供该计数（—）',
          }),
          h(MetricCard, {
            title: '未决冲突',
            value: countText(counts.open_conflicts),
            sub: 'conflicts list --status open',
          }),
          h(MetricCard, {
            title: '使用次数',
            value: countText(counts.uses),
            sub: 'memory.use_count 总和',
          }),
        ),

        h('p', { className: 'pconsole-note', style: { margin: '10px 0 0' } },
          `token p95 ${countText(tokenValue)}${tokenSamples ? `（样本 ${tokenSamples} · 均值 ${countText(tokens?.mean)}）` : '（拿不到 sample）'}`
          + ` · 醒来卡 ${countText(wakeInfo?.chars)} 字`
          + ` · 数据库 ${fmtBytes(db.db_size_bytes)} · schema ${db.schema_version ?? '—'} · ${db.integrity_ok ? '完整性 ok' : '完整性异常'}`),

        // 睡眠状态：一行 + 操作。
        h(
          Panel,
          {
            title: '睡眠状态',
            actions: [
              h('button', {
                key: 'sleep',
                className: 'pconsole-btn',
                type: 'button',
                disabled: sleeping,
                onClick: () => void sleepNow(),
              }, sleeping ? '入睡中…' : '现在入睡'),
              h('button', {
                key: 'go',
                className: 'pconsole-btn',
                type: 'button',
                onClick: () => goto?.('sleep'),
              }, '看睡眠'),
            ],
          },
          latestRun
            ? h('div', { className: 'pconsole-kv', style: { borderBottom: 'none', padding: 0 } },
                h('span', null, '上次睡眠'),
                h('span', null,
                  `${latestRun.day ?? '—'} · ${statusText(latestRun)}`
                  + (quality !== undefined ? ` · 质量分 ${quality}` : '')
                  + (latestRun.started_at ? ` · ${fmtTime(latestRun.started_at)}` : '')))
            : h('p', { className: 'pconsole-empty', style: { padding: '8px 0' } },
                '还没有睡眠记录；点「现在入睡」或去「睡眠」页跑一次。'),
        ),

        // 最近 10 条：点一条直接跳到记忆页并选中。
        h(
          Panel,
          {
            title: '最近 10 条记忆',
            actions: h('button', {
              className: 'pconsole-btn',
              type: 'button',
              onClick: () => goto?.('memory'),
            }, '去记忆页'),
          },
          recent.length
            ? h('ul', { className: 'pconsole-list' },
                ...recent.map((item, index) =>
                  h('li', { key: item?.id ?? `recent-${index}` },
                    h(
                      'button',
                      {
                        type: 'button',
                        className: 'pconsole-item',
                        onClick: () => goto?.('memory', { selectId: item?.id }),
                      },
                      h('div', { className: 'pconsole-item-meta' },
                        h('span', null, fmtWhen(item)),
                        h('span', { className: 'pconsole-chip pconsole-chip-muted' }, labelOf(KIND_LABELS, item?.kind)),
                        item?.grade ? h('span', { className: 'pconsole-chip' }, item.grade) : null),
                      h('div', { className: 'pconsole-item-text' }, clampText(item?.text, 110)),
                    ))))
            : h('p', { className: 'pconsole-empty' }, '还没有最近变化。'),
        ),

        h(
          Panel,
          { title: '快捷动作' },
          h(
            'div',
            { className: 'pconsole-actions', style: { margin: 0 } },
            h('button', {
              className: 'pconsole-btn',
              type: 'button',
              disabled: starmapBusy,
              onClick: () => void openStarmap(),
            }, starmapBusy ? '打开中…' : '打开星图'),
            h('button', {
              className: 'pconsole-btn',
              type: 'button',
              disabled: doctorBusy,
              onClick: () => void runDoctor(),
            }, doctorBusy ? '体检中…' : '体检（doctor）'),
            h('button', {
              className: 'pconsole-btn',
              type: 'button',
              onClick: () => goto?.('wake'),
            }, '看醒来卡'),
            h('button', {
              className: 'pconsole-btn',
              type: 'button',
              onClick: () => void refreshWake(),
            }, '刷新醒来卡'),
          ),
          starmapUrl
            ? h('p', { className: 'pconsole-note' },
                '浏览器地址（可以存书签）：', h('code', { className: 'pconsole-code' }, starmapUrl))
            : null,
          doctor
            ? h('pre', { className: 'pconsole-pre', style: { marginTop: '8px' } }, formatDoctor(doctor))
            : null,
        ),
      )
    }

    // 醒来卡：每轮注入 system prompt 的那张卡，控制台里给一页能看见全文的地方。
    // 数据和注入走同一条 `pmem wake` 通道（index.js 的 systemPrompt.context）；
    // 这里的「重取」只是重算一遍，不落库、不改记忆。预算写的是 runtime.json 的 budget，
    // 和设置页那个数值是同一个键——改完下一轮生效。
    function ConsoleWake({ boot, status, notify, goto }) {
      const card = boot?.wake ?? null
      const runtime = status?.runtime ?? {}
      const [text, setText] = useState(card?.text ?? '')
      const [budget, setBudget] = useState(String(pickNumber(runtime.budget) ?? 300))
      const [busy, setBusy] = useState('')
      const [err, setErr] = useState('')
      const [takenAt, setTakenAt] = useState(card?.text ? new Date() : null)

      // 总览每 30s 轮询一次 bootstrap，卡内容变了就跟着换。
      useEffect(() => {
        if (!card?.text) return
        setText(card.text)
        setTakenAt(new Date())
      }, [card?.text])

      const budgetOf = () => Math.max(80, Math.min(1200, Math.trunc(Number(budget) || 300)))

      const fetchCard = async n => {
        const data = await api(`/wake?budget=${n}`)
        setText(String(data?.text ?? ''))
        setTakenAt(new Date())
      }

      const refresh = async () => {
        setBusy('refresh')
        setErr('')
        try {
          await fetchCard(budgetOf())
          notify?.('醒来卡已重取（不落库）')
        } catch (cause) {
          setErr(`重取醒来卡失败：${cause.message}`)
        } finally {
          setBusy('')
        }
      }

      const saveBudget = async () => {
        const n = budgetOf()
        setBusy('save')
        setErr('')
        try {
          const saved = await api('/config', { method: 'POST', body: JSON.stringify({ budget: n }) })
          setBudget(String(pickNumber(saved?.runtime?.budget) ?? n))
          notify?.(`每轮预算已存为 ${n}（下一轮生效）`)
          await fetchCard(n)
        } catch (cause) {
          setErr(`保存预算失败：${cause.message}`)
        } finally {
          setBusy('')
        }
      }

      const chars = text.length
      const tokens = Math.ceil(chars / 3)
      const firstLine = text.split('\n').find(line => line.trim()) ?? ''

      return h(
        'div',
        null,
        err ? h('p', { className: 'pconsole-banner', role: 'alert' }, err) : null,

        h(
          Panel,
          {
            title: '醒来卡',
            actions: [
              h('button', {
                key: 'refresh',
                className: 'pconsole-btn',
                type: 'button',
                disabled: busy === 'refresh',
                onClick: () => void refresh(),
              }, busy === 'refresh' ? '重取中…' : '重取'),
              h('button', {
                key: 'copy',
                className: 'pconsole-btn',
                type: 'button',
                disabled: !text,
                onClick: () => void copyText(text, notify, '醒来卡已复制'),
              }, '复制全文'),
              h('button', {
                key: 'settings',
                className: 'pconsole-btn',
                type: 'button',
                onClick: () => goto?.('settings'),
              }, '去设置'),
            ],
          },
          h('p', { className: 'pconsole-note', style: { margin: '0 0 8px' } },
            '这张卡每轮注入 system prompt：我是谁 / 我们为什么做这个 / 现在在做什么 / 下次从哪里接 / 相处里长出的句子。'
            + '这里看到的就是同一份 `pmem wake` 的输出；「重取」不落库，也不改记忆。'),
          h(
            'div',
            { className: 'pconsole-toolbar', style: { margin: 0 } },
            h('label', { className: 'pconsole-field' },
              h('span', null, '每轮预算'),
              h('input', {
                className: 'pconsole-input',
                type: 'number',
                min: 80,
                max: 1200,
                step: 20,
                style: { width: '90px' },
                'aria-label': '醒来卡预算',
                value: budget,
                onChange: event => setBudget(event.target.value),
              }),
              h('span', { className: 'pconsole-note', style: { margin: 0 } }, 'token')),
            h('button', {
              className: 'pconsole-btn',
              type: 'button',
              disabled: busy === 'save',
              onClick: () => void saveBudget(),
            }, busy === 'save' ? '保存中…' : '存为每轮预算'),
            h('span', { className: 'pconsole-note', style: { margin: 0 } },
              `${countText(chars)} 字 · 约 ${countText(tokens)} token`
              + (takenAt ? ` · 取于 ${fmtTimeShort(takenAt)}` : '')),
          ),
          text
            ? h('pre', {
                className: 'pconsole-pre',
                style: { marginTop: '8px', maxHeight: '62vh' },
                'aria-label': '醒来卡全文',
              }, text)
            : h('p', { className: 'pconsole-empty' },
                boot
                  ? '这张卡现在是空的：库里还没有可唤醒的记忆。去「记忆」页写一条，或 /pmem remember <内容>。'
                  : '正在读醒来卡…'),
          firstLine
            ? h('p', { className: 'pconsole-note' },
                '开头：', h('code', { className: 'pconsole-code' }, firstLine))
            : null,
        ),
      )
    }

    // 记忆管理：搜索 / 过滤 / 排序 / 真分页 / 多选批量 / 详情（元数据、证据、历史、冲突、相关记忆）。
    // task-8 起 /memories 支持 offset/limit/sort/since/until，返回 total/hasMore/capped：
    // 排序与翻页都交给后端，前端只保留「分组多选」这一条客户端过滤。
    function ConsoleMemory({ notify, initialId, onConsumeInitial }) {
      const defaults = loadPrefs()
      const [q, setQ] = useState('')
      const [mode, setMode] = useState(defaults.mode)
      const [kind, setKind] = useState('')
      const [grade, setGrade] = useState('')
      const [status, setStatus] = useState('')
      const [pinnedOnly, setPinnedOnly] = useState(false)
      const [fromDay, setFromDay] = useState('')
      const [toDay, setToDay] = useState('')
      const [sortMode, setSortMode] = useState('time')
      const [pageSize, setPageSize] = useState(defaults.k >= 50 ? 50 : 20)
      // 真分页：offset 是下一次请求的起点；total/hasMore/capped 都来自后端。
      const [offset, setOffset] = useState(0)
      const [total, setTotal] = useState(0)
      const [hasMore, setHasMore] = useState(false)
      const [capped, setCapped] = useState(false)

      const [items, setItems] = useState([])
      const [loading, setLoading] = useState(false)
      const [searched, setSearched] = useState(false)
      const [error, setError] = useState('')
      const [toast, setToast] = useState('')

      const [selectedId, setSelectedId] = useState('')
      const [detail, setDetail] = useState(null)
      const [detailLoading, setDetailLoading] = useState(false)
      const [detailError, setDetailError] = useState('')
      const [related, setRelated] = useState([])
      const [importanceDraft, setImportanceDraft] = useState('')
      // 单条元数据编辑：kind / 分组一次 POST；留空=不改。
      const [kindDraft, setKindDraft] = useState('')
      const [groupDraft, setGroupDraft] = useState('')

      const [batch, setBatch] = useState([])
      const [pending, setPending] = useState('')
      const [busy, setBusy] = useState('')

      // 分组过滤：树形、多选、含子组；数据来自 /console/groups。
      const [groupRows, setGroupRows] = useState([])
      const [groupSelected, setGroupSelected] = useState([])
      const [collapsedGroups, setCollapsedGroups] = useState([])
      const [groupOpen, setGroupOpen] = useState(false)
      const [groupLoading, setGroupLoading] = useState(false)

      // 批量改元数据（op=update，一次请求走 /console/memory/batch；后端逐条执行）。
      const [metaOpen, setMetaOpen] = useState(false)
      const [metaForm, setMetaForm] = useState({
        kind: '', grade: '', group: '', sensitivity: '', importance: '', status: '',
      })

      const say = useCallback(message => {
        setToast(message)
        notify?.(message)
      }, [notify])

      const loadDetail = useCallback(async id => {
        if (!id) {
          setDetail(null)
          return
        }
        setDetailLoading(true)
        setDetailError('')
        try {
          setDetail(await apiConsole(`/memory?id=${encodeURIComponent(id)}`))
        } catch (cause) {
          setDetail(null)
          setDetailError(`读详情失败：${cause.message}`)
        } finally {
          setDetailLoading(false)
        }
      }, [])

      // 真分页/真排序：offset/limit/sort/since/until 都交给后端（task-8）。
      // append=true 用于「加载更多」：offset += limit，不靠增大 limit 假装分页。
      const runSearch = useCallback(async overrides => {
        const next = overrides ?? {}
        const query = next.query ?? q
        const nextMode = next.mode ?? mode
        const nextSort = next.sort ?? sortMode
        const nextOffset = Math.max(0, Number(next.offset ?? 0) || 0)
        const nextPageSize = Math.min(50, Math.max(1, Number(next.pageSize ?? pageSize) || 20))
        const append = next.append === true
        const params = new URLSearchParams({
          limit: String(nextPageSize),
          offset: String(nextOffset),
          sort: MEMORY_SORT_API[nextSort] ?? 'time',
          mode: nextMode,
        })
        if (String(query).trim()) params.set('q', String(query).trim())
        const nextKind = next.kind ?? kind
        const nextGrade = next.grade ?? grade
        const nextStatus = next.status ?? status
        const nextPinned = next.pinnedOnly ?? pinnedOnly
        const nextFrom = next.fromDay ?? fromDay
        const nextTo = next.toDay ?? toDay
        if (nextKind) params.set('kind', nextKind)
        if (nextGrade) params.set('grade', nextGrade)
        if (nextStatus) params.set('status', nextStatus)
        if (nextPinned) params.set('pinned', 'true')
        if (nextFrom) params.set('since', nextFrom)
        if (nextTo) params.set('until', nextTo)
        setLoading(true)
        setError('')
        try {
          const data = await apiConsole(`/memories?${params.toString()}`)
          const list = Array.isArray(data.items) ? data.items : []
          let merged = list
          if (append) {
            const seen = new Set()
            merged = []
            for (const entry of items.concat(list)) {
              const key = entry?.id ?? `${entry?.day}-${entry?.text}`
              if (seen.has(key)) continue
              seen.add(key)
              merged.push(entry)
            }
          }
          setItems(merged)
          // 后端可能夹取 offset（0–1000）：用返回的 offset 作为起点，下一页才不会重复/跳条。
          const appliedOffset = Number.isFinite(Number(data.offset)) ? Number(data.offset) : nextOffset
          setOffset(appliedOffset + list.length)
          setTotal(pickNumber(data.total, merged.length) ?? merged.length)
          // 兼容旧后端：没有 hasMore 时退化成「这一页满了就可能还有」。
          setHasMore(typeof data.hasMore === 'boolean' ? data.hasMore : list.length >= nextPageSize)
          setCapped(data.capped === true)
          setSearched(true)
          if (!append && selectedId && !list.some(entry => entry?.id === selectedId)) {
            setSelectedId('')
            setDetail(null)
          }
        } catch (cause) {
          if (!append) setItems([])
          setError(`检索失败：${cause.message}`)
        } finally {
          setLoading(false)
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, [q, mode, kind, grade, status, pinnedOnly, sortMode, pageSize, fromDay, toDay, selectedId, items])

      // 第一次进来先给「最近」，别让人对着空白页发呆。
      useEffect(() => {
        void runSearch({})
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, [])

      // 分组树（只拉一次；失败不阻塞记忆页）。
      useEffect(() => {
        let alive = true
        setGroupLoading(true)
        apiConsole('/groups')
          .then(data => {
            if (!alive) return
            setGroupRows(flattenGroupTree(data?.tree))
          })
          .catch(() => { if (alive) setGroupRows([]) })
          .finally(() => { if (alive) setGroupLoading(false) })
        return () => { alive = false }
      }, [])

      // 从总览/实体页跳过来时，直接选中那条记忆。
      useEffect(() => {
        if (!initialId) return
        setSelectedId(String(initialId))
        void loadDetail(String(initialId))
        onConsumeInitial?.()
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, [initialId])

      // 相关记忆：拿正文首行做一次关键词检索（后端 /memory 不返回相关列表）。
      useEffect(() => {
        const item = detail?.item
        if (!item?.text) {
          setRelated([])
          return undefined
        }
        let alive = true
        const probe = String(item.text).split('\n').map(line => line.trim()).find(Boolean)
        if (!probe) {
          setRelated([])
          return undefined
        }
        apiConsole(`/memories?q=${encodeURIComponent(probe.slice(0, 40))}&k=6&mode=keyword`)
          .then(data => {
            if (!alive) return
            setRelated((data.items || []).filter(entry => entry?.id && entry.id !== item.id).slice(0, 5))
          })
          .catch(() => { if (alive) setRelated([]) })
        return () => { alive = false }
      }, [detail?.item?.id])

      useEffect(() => {
        const value = detail?.item?.importance
        setImportanceDraft(typeof value === 'number' && Number.isFinite(value) ? String(value) : '')
      }, [detail?.item?.id, detail?.item?.importance])

      // kind 默认显示当前值（与下拉一致）；分组输入默认留空 = 不改，避免误覆盖多分组。
      useEffect(() => {
        setKindDraft(String(detail?.item?.kind ?? ''))
        setGroupDraft('')
      }, [detail?.item?.id])

      const select = id => {
        setSelectedId(id)
        setPending('')
        void loadDetail(id)
      }

      const submitSearch = () => {
        const query = q.trim()
        // 默认排序：有搜索词 = 相关度，无词 = 时间倒序（都交给后端 sort）。
        const nextSort = query ? 'relevance' : 'time'
        setSortMode(nextSort)
        void runSearch({ sort: nextSort, offset: 0 })
      }

      // 加载更多：offset += limit（真分页），结果追加；不靠增大 limit。
      const loadMore = () => {
        if (!hasMore || loading) return
        void runSearch({ offset, append: true })
      }

      const changeSort = value => {
        setSortMode(value)
        void runSearch({ sort: value, offset: 0 })
      }

      const resetFilters = () => {
        setQ('')
        setKind('')
        setGrade('')
        setStatus('')
        setPinnedOnly(false)
        setFromDay('')
        setToDay('')
        setGroupSelected([])
        setSortMode('time')
        void runSearch({
          query: '', kind: '', grade: '', status: '', pinnedOnly: false,
          fromDay: '', toDay: '', sort: 'time', offset: 0,
        })
      }

      const toggleGroup = path => {
        setGroupSelected(prev =>
          (prev.includes(path) ? prev.filter(value => value !== path) : [...prev, path]))
      }

      const toggleCollapse = path => {
        setCollapsedGroups(prev =>
          (prev.includes(path) ? prev.filter(value => value !== path) : [...prev, path]))
      }

      // 后端 /memory/update 返回 { ok, applied, failed }；某个字段失败时 ok=false，
      // 所以这里用 requestRaw 拿原始响应，按 applied/failed 逐字段提示，不把部分成功当失败。
      const reportPatch = data => {
        const applied = data?.applied && typeof data.applied === 'object'
          ? Object.keys(data.applied)
          : []
        const failed = Array.isArray(data?.failed) ? data.failed : []
        if (applied.length) say(`已应用：${applied.join(' / ')}`)
        else if (!failed.length) say('已保存')
        if (failed.length) {
          const details = failed.map(entry => {
            const field = entry?.field ?? entry?.key ?? '?'
            const message = entry?.error?.message ?? entry?.message ?? ''
            return message ? `${field}（${message}）` : String(field)
          })
          setError(`部分字段没保存成功：${details.join('；')}`)
        }
      }

      const updateMemory = async patch => {
        if (!selectedId) return
        setBusy('update')
        setError('')
        try {
          const raw = await requestRaw(CONSOLE + '/memory/update', {
            method: 'POST',
            body: JSON.stringify({ id: selectedId, ...patch }),
          })
          const appliedCount = raw.data?.applied && typeof raw.data.applied === 'object'
            ? Object.keys(raw.data.applied).length
            : 0
          if (!raw.ok && appliedCount === 0) {
            const failed = Array.isArray(raw.data?.failed) ? raw.data.failed : []
            const detail = failed
              .map(entry => (entry?.field ?? '?')
                + (entry?.error?.message ? `（${entry.error.message}）` : ''))
              .join('；')
            throw new Error(detail || raw.data?.error || `HTTP ${raw.status}`)
          }
          reportPatch(raw.data)
          await loadDetail(selectedId)
          await runSearch({ offset: 0 })
        } catch (cause) {
          setError(`保存失败：${cause.message}`)
        } finally {
          setBusy('')
        }
      }

      // 单条元数据：kind + 分组合成一次 POST；分组留空=不改。
      const saveMetaEdits = () => {
        const patch = {}
        if (kindDraft && kindDraft !== (item?.kind ?? '')) patch.kind = kindDraft
        if (groupDraft.trim()) patch.group = groupDraft.trim()
        if (!Object.keys(patch).length) {
          say('kind 没变、分组也留空，没有要保存的字段')
          return
        }
        void updateMemory(patch)
      }

      const sendFeedback = async signal => {
        if (!selectedId) return
        setBusy(signal)
        setError('')
        try {
          await apiConsole('/memory/feedback', {
            method: 'POST',
            body: JSON.stringify({ id: selectedId, signal }),
          })
          say(`已记下：${labelOf(FEEDBACK_LABELS, signal)}`)
          await loadDetail(selectedId)
        } catch (cause) {
          setError(`反馈失败：${cause.message}`)
        } finally {
          setBusy('')
        }
      }

      // 删除默认软删；hard 才是真删。撤回 = status archived（可恢复）。
      const runDanger = async action => {
        if (!selectedId) return
        setBusy(action)
        setError('')
        try {
          if (action === 'archive') {
            await apiConsole('/memory/update', {
              method: 'POST',
              body: JSON.stringify({ id: selectedId, status: 'archived' }),
            })
            say('已撤回（归档，可恢复）')
          } else {
            await apiConsole('/memory/delete', {
              method: 'POST',
              body: JSON.stringify({ id: selectedId, hard: action === 'hard' }),
            })
            say(action === 'hard' ? '已彻底删除' : '已删除（软删，7 天内可恢复）')
          }
          setPending('')
          setSelectedId('')
          setDetail(null)
          await runSearch({ offset: 0 })
        } catch (cause) {
          setError(`操作失败：${cause.message}`)
        } finally {
          setBusy('')
        }
      }

      const toggleBatch = id => {
        setBatch(prev => (prev.includes(id) ? prev.filter(value => value !== id) : [...prev, id]))
        setPending('')
      }

      const selectedIds = batch.slice(0, 100)

      // 批量动作走 POST /console/memory/batch（1–100 条，顺序执行 10–20s）。
      const runBatchOp = async (op, extra) => {
        if (!selectedIds.length) return
        setBusy(`batch-${op}`)
        setError('')
        setPending('')
        try {
          const data = await apiConsole('/memory/batch', {
            method: 'POST',
            body: JSON.stringify({ ids: selectedIds, op, ...extra }),
          })
          const okCount = Number(data?.okCount) || 0
          const failCount = Number(data?.failCount) || 0
          say(`${op}：${okCount}/${data?.processed ?? selectedIds.length} 条完成`
            + (failCount ? `（失败 ${failCount}）` : ''))
          setBatch([])
          setSelectedId('')
          setDetail(null)
          await runSearch({ offset: 0 })
        } catch (cause) {
          setError(`批量操作失败：${cause.message}`)
        } finally {
          setBusy('')
        }
      }

      // 批量改元数据：走后端 /console/memory/batch 的 op=update（一次请求，服务端逐条执行），
      // patch 白名单 kind/grade/group/sensitivity/importance/status；非法字段/值后端直接 400。
      const runBatchUpdate = async () => {
        if (!selectedIds.length) return
        const patch = {}
        if (metaForm.kind) patch.kind = metaForm.kind
        if (metaForm.grade) patch.grade = metaForm.grade
        if (metaForm.group.trim()) patch.group = metaForm.group.trim()
        if (metaForm.sensitivity) patch.sensitivity = metaForm.sensitivity
        if (metaForm.status) patch.status = metaForm.status
        if (metaForm.importance !== '' && Number.isFinite(Number(metaForm.importance))) {
          patch.importance = Number(metaForm.importance)
        }
        if (!Object.keys(patch).length) {
          say('至少填一个要改的字段')
          return
        }
        setBusy('batch-update')
        setError('')
        setPending('')
        try {
          const data = await apiConsole('/memory/batch', {
            method: 'POST',
            body: JSON.stringify({ ids: selectedIds, op: 'update', patch }),
          })
          const okCount = Number(data?.okCount) || 0
          const failCount = Number(data?.failCount) || 0
          say(`已更新 ${okCount}/${data?.processed ?? selectedIds.length} 条`
            + (failCount ? `（失败 ${failCount}）` : ''))
          setBatch([])
          setSelectedId('')
          setDetail(null)
          setMetaOpen(false)
          await runSearch({ offset: 0 })
        } catch (cause) {
          setError(`批量改元数据失败：${cause.message}`)
        } finally {
          setBusy('')
        }
      }

      const item = detail?.item
        ?? items.find(entry => entry?.id === selectedId)
        ?? null
      const history = Array.isArray(detail?.history) ? detail.history : []
      const evidence = Array.isArray(detail?.evidence) ? detail.evidence : []
      const conflicts = Array.isArray(detail?.conflicts) ? detail.conflicts : []
      const itemPinned = item?.pinned === true
      const detailBusy = Boolean(busy)

      // 日期范围已交给后端 since/until；这里只剩「分组多选」是客户端过滤
      //（后端只支持单个 group），所以多选时结果可能被过滤，且受 200 条检索池上限影响。
      const visibleItems = groupSelected.length
        ? items.filter(entry => matchesGroups(entry, groupSelected))
        : items

      // 分组树：折叠的父节点下面的子节点不显示。
      const visibleGroups = groupRows.filter(row => {
        if (row.depth === 0) return true
        const parts = row.path.split('/')
        for (let index = 1; index < parts.length; index += 1) {
          const ancestor = parts.slice(0, index).join('/')
          if (collapsedGroups.includes(ancestor)) return false
        }
        return true
      })
      const hasChildren = path => groupRows.some(row => row.parent === path)

      const groupPanel = h(
        'div',
        { className: 'pconsole-filter-panel' },
        h(
          'div',
          { className: 'pconsole-filter-head' },
          h('p', { className: 'pconsole-filter-title' }, '分组过滤（含子组·多选）'),
          h(
            'div',
            { className: 'pconsole-actions', style: { margin: 0 } },
            groupSelected.length
              ? h('button', {
                  className: 'pconsole-btn',
                  type: 'button',
                  onClick: () => setGroupSelected([]),
                }, '清除')
              : null,
            h('button', {
              className: 'pconsole-health-btn',
              type: 'button',
              'aria-expanded': groupOpen ? 'true' : 'false',
              onClick: () => setGroupOpen(prev => !prev),
            }, groupOpen ? '收起' : '展开'),
          ),
        ),
        groupOpen
          ? h(
              'div',
              null,
              groupLoading
                ? h('p', { className: 'pconsole-note', style: { margin: '6px 0 0' } }, '读取分组树…')
                : visibleGroups.length
                  ? h(
                      'div',
                      { className: 'pconsole-group-tree' },
                      ...visibleGroups.map(row =>
                        h('div', {
                          key: row.path,
                          className: 'pconsole-group-row',
                          style: { paddingLeft: `${row.depth * 12}px` },
                        },
                          hasChildren(row.path)
                            ? h('button', {
                                className: 'pconsole-group-caret',
                                type: 'button',
                                'aria-label': collapsedGroups.includes(row.path) ? '展开子组' : '收起子组',
                                onClick: () => toggleCollapse(row.path),
                              }, collapsedGroups.includes(row.path) ? '▸' : '▾')
                            : h('span', { className: 'pconsole-group-caret' }),
                          h('label', null,
                            h('input', {
                              type: 'checkbox',
                              className: 'pconsole-check',
                              checked: groupSelected.includes(row.path),
                              onChange: () => toggleGroup(row.path),
                            }),
                            h('span', { className: 'pconsole-group-name' }, row.path),
                            row.count !== undefined
                              ? h('span', { className: 'pconsole-group-count' }, String(row.count))
                              : null),
                        )))
                  : h('p', { className: 'pconsole-note', style: { margin: '6px 0 0' } },
                      '没有分组数据（/console/groups 为空）。'),
              groupSelected.length
                ? h('p', { className: 'pconsole-note', style: { margin: '4px 0 0' } },
                    `已选 ${groupSelected.length} 个分组：客户端过滤（含子组）；后端只支持单个 group 参数，多选时受 200 条检索池上限影响。`)
                : null,
            )
          : null,
      )

      const toolbar = h(
        'div',
        { className: 'pconsole-toolbar' },
        h(
          'form',
          {
            className: 'pconsole-search',
            role: 'search',
            onSubmit: event => {
              event.preventDefault()
              submitSearch()
            },
          },
          h('input', {
            className: 'pconsole-input pconsole-search-input',
            type: 'search',
            placeholder: '搜关键词、路径、命令或错误码',
            'aria-label': '搜索记忆',
            value: q,
            onChange: event => setQ(event.target.value),
          }),
          h('button', {
            className: 'pconsole-btn pconsole-btn-primary',
            type: 'submit',
            disabled: loading,
          }, loading ? '检索中…' : '检索'),
        ),
        h('div', { className: 'pconsole-seg', role: 'group', 'aria-label': '排序方式' },
          ...MEMORY_SORTS.map(([value, label]) =>
            h('button', {
              key: value,
              className: 'pconsole-seg-btn',
              type: 'button',
              'aria-pressed': sortMode === value ? 'true' : 'false',
              onClick: () => changeSort(value),
            }, label))),
        h('label', { className: 'pconsole-field' },
          h('span', null, '模式'),
          h('select', {
            className: 'pconsole-select',
            value: mode,
            'aria-label': '检索模式',
            onChange: event => {
              const value = event.target.value
              setMode(value)
              void runSearch({ mode: value, offset: 0 })
            },
          }, ...MEMORY_MODES.map(([value, label]) => h('option', { key: value, value }, label)))),
        h('label', { className: 'pconsole-field' },
          h('span', null, '类型'),
          h('select', {
            className: 'pconsole-select',
            value: kind,
            'aria-label': '按类型过滤',
            onChange: event => {
              const value = event.target.value
              setKind(value)
              void runSearch({ kind: value, offset: 0 })
            },
          },
            h('option', { value: '' }, '全部'),
            ...Object.entries(KIND_LABELS).map(([value, label]) => h('option', { key: value, value }, label)))),
        h('label', { className: 'pconsole-field' },
          h('span', null, '重要度'),
          h('select', {
            className: 'pconsole-select',
            value: grade,
            'aria-label': '按重要度过滤',
            onChange: event => {
              const value = event.target.value
              setGrade(value)
              void runSearch({ grade: value, offset: 0 })
            },
          },
            h('option', { value: '' }, '全部'),
            ...MEMORY_GRADES.map(value => h('option', { key: value, value }, value)))),
        h('label', { className: 'pconsole-field' },
          h('span', null, '状态'),
          h('select', {
            className: 'pconsole-select',
            value: status,
            'aria-label': '按状态过滤',
            onChange: event => {
              const value = event.target.value
              setStatus(value)
              void runSearch({ status: value, offset: 0 })
            },
          },
            h('option', { value: '' }, '全部'),
            ...Object.entries(MEMORY_STATUS_LABELS)
              .filter(([value]) => value !== 'deleted')
              .map(([value, label]) => h('option', { key: value, value }, label)))),
        h('label', { className: 'pconsole-field' },
          h('input', {
            type: 'checkbox',
            className: 'pconsole-check',
            checked: pinnedOnly,
            onChange: event => {
              const value = event.target.checked
              setPinnedOnly(value)
              void runSearch({ pinnedOnly: value, offset: 0 })
            },
          }), '只看置顶'),
        h('label', { className: 'pconsole-field' },
          h('span', null, '从'),
          h('input', {
            className: 'pconsole-input',
            type: 'date',
            'aria-label': '起始日期（服务端 since）',
            value: fromDay,
            onChange: event => setFromDay(event.target.value),
          })),
        h('label', { className: 'pconsole-field' },
          h('span', null, '到'),
          h('input', {
            className: 'pconsole-input',
            type: 'date',
            'aria-label': '结束日期（服务端 until）',
            value: toDay,
            onChange: event => setToDay(event.target.value),
          })),
        h('button', { className: 'pconsole-btn', type: 'button', onClick: resetFilters }, '清空'),
        h('label', { className: 'pconsole-field' },
          h('span', null, '每页'),
          h('select', {
            className: 'pconsole-select',
            value: String(pageSize),
            'aria-label': '每页条数',
            onChange: event => {
              const value = Number(event.target.value) === 50 ? 50 : 20
              setPageSize(value)
              void runSearch({ offset: 0, pageSize: value })
            },
          },
            h('option', { value: '20' }, '20'),
            h('option', { value: '50' }, '50'))),
      )

      const batchBar = batch.length
        ? h(
            'div',
            { className: 'pconsole-confirm', role: 'region', 'aria-label': '批量操作' },
            h('p', null,
              `已选 ${batch.length} 条（最多 100）。批量动作都要二次确认，删除默认软删。`),
            busy === 'batch-update'
              ? h('p', { className: 'pconsole-note', role: 'status' },
                  '更新中：一次请求，服务端逐条执行（1–100 条）…')
              : null,
            h(
              'div',
              { className: 'pconsole-actions' },
              ...BATCH_FEEDBACK_SIGNALS.map(signal =>
                h('button', {
                  key: signal,
                  className: 'pconsole-btn',
                  type: 'button',
                  disabled: detailBusy,
                  onClick: () => setPending(`batch-feedback-${signal}`),
                }, `批量${FEEDBACK_LABELS[signal]}`)),
              h('button', {
                className: 'pconsole-btn',
                type: 'button',
                disabled: detailBusy,
                onClick: () => setPending('batch-retract'),
              }, '批量撤回'),
              h('button', {
                className: 'pconsole-btn pconsole-btn-danger',
                type: 'button',
                disabled: detailBusy,
                onClick: () => setPending('batch-delete'),
              }, '批量删除…'),
              h('button', {
                className: 'pconsole-btn',
                type: 'button',
                disabled: detailBusy,
                onClick: () => setMetaOpen(prev => !prev),
              }, metaOpen ? '收起批量改元数据' : '批量改元数据…'),
              h('button', {
                className: 'pconsole-btn',
                type: 'button',
                onClick: () => { setBatch([]); setPending(''); setMetaOpen(false) },
              }, '取消选择'),
            ),
            metaOpen
              ? h(
                  'div',
                  { style: { marginTop: '8px' } },
                  h('p', { className: 'pconsole-note', style: { margin: '0 0 6px' } },
                    '留空的字段不改。逐条调 /memory/update（<=100 条，显示进度）；kind/分组后端扩展后生效。'),
                  h(
                    'div',
                    { className: 'pconsole-actions', style: { margin: 0 } },
                    h('label', { className: 'pconsole-field' }, 'kind',
                      h('select', {
                        className: 'pconsole-select',
                        value: metaForm.kind,
                        'aria-label': '批量改 kind',
                        onChange: event => setMetaForm(prev => ({ ...prev, kind: event.target.value })),
                      },
                        h('option', { value: '' }, '不改'),
                        ...Object.entries(KIND_LABELS).map(([value, label]) =>
                          h('option', { key: value, value }, label)))),
                    h('label', { className: 'pconsole-field' }, 'grade',
                      h('select', {
                        className: 'pconsole-select',
                        value: metaForm.grade,
                        'aria-label': '批量改 grade',
                        onChange: event => setMetaForm(prev => ({ ...prev, grade: event.target.value })),
                      },
                        h('option', { value: '' }, '不改'),
                        ...MEMORY_GRADES.map(value => h('option', { key: value, value }, value)))),
                    h('label', { className: 'pconsole-field' }, '分组',
                      h('input', {
                        className: 'pconsole-input',
                        style: { width: '140px' },
                        placeholder: '我们/经历',
                        'aria-label': '批量改分组',
                        value: metaForm.group,
                        onChange: event => setMetaForm(prev => ({ ...prev, group: event.target.value })),
                      })),
                    h('label', { className: 'pconsole-field' }, '敏感级',
                      h('select', {
                        className: 'pconsole-select',
                        value: metaForm.sensitivity,
                        'aria-label': '批量改敏感级',
                        onChange: event => setMetaForm(prev => ({ ...prev, sensitivity: event.target.value })),
                      },
                        h('option', { value: '' }, '不改'),
                        ...SENSITIVITY_OPTIONS.map(value => h('option', { key: value, value }, value)))),
                    h('label', { className: 'pconsole-field' }, '重要度值',
                      h('input', {
                        className: 'pconsole-input',
                        type: 'number',
                        min: 0,
                        max: 1,
                        step: 0.05,
                        style: { width: '84px' },
                        placeholder: '不改',
                        'aria-label': '批量改重要度数值',
                        value: metaForm.importance,
                        onChange: event => setMetaForm(prev => ({ ...prev, importance: event.target.value })),
                      })),
                    h('label', { className: 'pconsole-field' }, '状态',
                      h('select', {
                        className: 'pconsole-select',
                        value: metaForm.status,
                        'aria-label': '批量改状态',
                        onChange: event => setMetaForm(prev => ({ ...prev, status: event.target.value })),
                      },
                        h('option', { value: '' }, '不改'),
                        h('option', { value: 'active' }, '活跃'),
                        h('option', { value: 'dormant' }, '休眠'),
                        h('option', { value: 'archived' }, '归档'))),
                  ),
                  h('div', { className: 'pconsole-actions' },
                    h('button', {
                      className: 'pconsole-btn pconsole-btn-danger',
                      type: 'button',
                      disabled: detailBusy,
                      onClick: () => setPending('batch-update'),
                    }, '确认批量改元数据…'),
                    h('button', {
                      className: 'pconsole-btn',
                      type: 'button',
                      onClick: () => setMetaForm({ kind: '', grade: '', group: '', sensitivity: '', importance: '', status: '' }),
                    }, '重置字段')),
                )
              : null,
            pending.startsWith('batch-')
              ? h(
                  'div',
                  { className: 'pconsole-actions' },
                  h('span', { className: 'pconsole-warn', style: { margin: 0 } },
                    pending === 'batch-delete'
                      ? `确认软删这 ${batch.length} 条？（7 天内可恢复）`
                      : pending === 'batch-retract'
                        ? `确认撤回这 ${batch.length} 条？（归档，可恢复）`
                        : pending === 'batch-update'
                          ? `确认修改这 ${batch.length} 条的元数据？`
                          : `确认给这 ${batch.length} 条打反馈？`),
                  h('button', {
                    className: 'pconsole-btn pconsole-btn-danger',
                    type: 'button',
                    disabled: detailBusy,
                    onClick: () => {
                      if (pending === 'batch-update') return void runBatchUpdate()
                      if (pending === 'batch-retract') return void runBatchOp('retract')
                      if (pending === 'batch-delete') return void runBatchOp('delete', { hard: false })
                      return void runBatchOp('feedback', { signal: pending.replace('batch-feedback-', '') })
                    },
                  }, '确认'),
                  h('button', {
                    className: 'pconsole-btn',
                    type: 'button',
                    onClick: () => setPending(''),
                  }, '取消'),
                )
              : null,
          )
        : null

      const list = h(
        'div',
        { className: 'pconsole-col' },
        h('div', { className: 'pconsole-list-meta' },
          h('span', null, loading
            ? '检索中…'
            : `显示 ${visibleItems.length} 条${total ? ` / 共 ${total} 条` : ''}${capped ? ' · 已封顶' : ''}${searched ? '' : ' · 还没检索'}`),
          h('span', null, batch.length ? `选中 ${batch.length}` : null)),
        error ? h('p', { className: 'pconsole-error', role: 'alert' }, error) : null,
        toast ? h('p', { className: 'pconsole-ok', role: 'status' }, toast) : null,
        groupPanel,
        batchBar,
        visibleItems.length
          ? h('div', { className: 'pconsole-list', role: 'listbox', 'aria-label': '记忆检索结果', 'aria-multiselectable': 'true' },
              ...visibleItems.map(entry =>
                h('div', { key: entry.id ?? `${entry.day}-${entry.text}`, className: 'pconsole-item-row' },
                  h('input', {
                    type: 'checkbox',
                    className: 'pconsole-check',
                    'aria-label': `选择记忆 ${entry.id ?? ''}`,
                    checked: batch.includes(entry.id),
                    onChange: () => toggleBatch(entry.id),
                  }),
                  h(
                    'button',
                    {
                      type: 'button',
                      role: 'option',
                      'aria-selected': entry.id === selectedId ? 'true' : 'false',
                      className: 'pconsole-item',
                      onClick: () => select(entry.id),
                    },
                    h('div', { className: 'pconsole-item-meta' },
                      h('span', null, fmtWhen(entry)),
                      h('span', { className: 'pconsole-chip pconsole-chip-muted' }, labelOf(KIND_LABELS, entry.kind)),
                      entry.grade ? h('span', { className: 'pconsole-chip' }, entry.grade) : null,
                      entry.pinned ? h('span', { className: 'pconsole-chip pconsole-chip-warn' }, '置顶') : null,
                      entry.provenance ? h('span', null, entry.provenance) : null),
                    h('div', { className: 'pconsole-item-text' }, h(Highlighted, { text: entry.text, query: q })),
                    typeof entry.score === 'number' && Number.isFinite(entry.score)
                      ? h('div', {
                          className: 'pconsole-score',
                          title: `相关度 ${entry.score.toFixed(2)}`,
                          'aria-hidden': 'true',
                        }, h('span', {
                          style: { width: `${Math.max(4, Math.min(100, Math.round(entry.score * 100)))}%` },
                        }))
                      : null,
                  ))))
          : h('p', { className: 'pconsole-empty' },
              searched
                ? (fromDay || toDay || groupSelected.length
                  ? '当前过滤条件下没有结果，清掉日期/分组过滤再试。'
                  : '没找到，换个说法，或者把模式切到「最近」看最新记忆。')
                : '还没检索。输入关键词，或者点「最近」。'),
        items.length
          ? h('div', null,
              h('div', { className: 'pconsole-actions' },
                hasMore
                  ? h('button', {
                      className: 'pconsole-btn',
                      type: 'button',
                      disabled: loading,
                      onClick: loadMore,
                    }, loading ? '加载中…' : `加载更多（+${pageSize}）`)
                  : h('span', { className: 'pconsole-note', style: { margin: 0 } },
                      `没有更多了${total ? `（共 ${total} 条）` : ''}`)),
              capped
                ? h('p', { className: 'pconsole-note', role: 'status' },
                    '仅显示最相关的 200 条，缩小搜索词可看更多。')
                : null)
          : null,
      )

      const metaRows = item
        ? [
            kvRow('id', item.id),
            kvRow('发生时间', item.valid_from ? fmtTime(item.valid_from) : (item.day || '')),
            kvRow('入库日', item.day),
            kvRow('类型', labelOf(KIND_LABELS, item.kind)),
            kvRow('重要度', item.grade),
            kvRow('状态', labelOf(MEMORY_STATUS_LABELS, item.status)),
            kvRow('来源', item.provenance),
            kvRow('来源类型', item.source_kind),
            kvRow('置信度', item.confidence),
            kvRow('分组', Array.isArray(item.groups) && item.groups.length ? item.groups.join(' · ') : ''),
            kvRow('命中原因', item.why),
            kvRow('创建时间', item.created_at ? fmtTime(item.created_at) : ''),
            kvRow('记录时间', item.recorded_at ? fmtTime(item.recorded_at) : ''),
            kvRow('最近使用', item.last_used_at ? fmtTime(item.last_used_at) : ''),
            kvRow('使用次数', item.use_count),
            kvRow('证据条数', item.evidence_count),
          ].filter(Boolean)
        : []

      const detailPane = h(
        'div',
        { className: 'pconsole-col pconsole-detail' },
        detailLoading
          ? h('p', { className: 'pconsole-empty' }, '读详情中…')
          : detailError
            ? h('p', { className: 'pconsole-error', role: 'alert' }, detailError)
            : !item
              ? h('p', { className: 'pconsole-empty' },
                  '从左边选一条记忆：正文、元数据编辑、证据原话、变更历史、冲突都在这里。')
              : h(
                  'div',
                  null,
                  h('h3', { className: 'pconsole-detail-title' }, '记忆正文',
                    item.grade ? h('span', { className: 'pconsole-chip' }, item.grade) : null,
                    item.pinned ? h('span', { className: 'pconsole-chip pconsole-chip-warn' }, '置顶') : null),
                  h('p', { className: 'pconsole-body-text' }, String(item.text ?? '')),
                  item.truncated ? h('p', { className: 'pconsole-note' }, '正文过长，接口返回的是截断版。') : null,
                  h('div', { className: 'pconsole-actions', style: { margin: '0 0 6px' } },
                    h('button', {
                      className: 'pconsole-btn',
                      type: 'button',
                      onClick: () => void copyText(item.text, notify, '已复制正文'),
                    }, '复制正文'),
                    h('button', {
                      className: 'pconsole-btn',
                      type: 'button',
                      onClick: () => void copyText(pretty(detail), notify, '已复制 JSON'),
                    }, '复制 JSON'),
                    h('button', {
                      className: 'pconsole-btn',
                      type: 'button',
                      onClick: () => void copyText(cliGet(item.id), notify, '已复制 CLI 命令'),
                    }, '复制 CLI 命令'),
                    h('button', {
                      className: 'pconsole-btn',
                      type: 'button',
                      onClick: () => void loadDetail(item.id),
                    }, '刷新')),
                  ...metaRows,

                  h('p', { className: 'pconsole-subhead' }, '元数据编辑'),
                  h('div', { className: 'pconsole-actions', style: { margin: 0 } },
                    h('label', { className: 'pconsole-field' },
                      h('span', null, 'kind'),
                      h('select', {
                        className: 'pconsole-select',
                        value: kindDraft,
                        disabled: detailBusy,
                        'aria-label': '修改类型 kind',
                        onChange: event => setKindDraft(event.target.value),
                      },
                        h('option', { value: '' }, '不改'),
                        ...Object.entries(KIND_LABELS).map(([value, label]) =>
                          h('option', { key: value, value }, `${label}（${value}）`)))),
                    h('label', { className: 'pconsole-field' },
                      h('span', null, '分组'),
                      h('input', {
                        className: 'pconsole-input',
                        style: { width: '170px' },
                        list: 'pconsole-group-options',
                        placeholder: '留空=不改（可输入新路径）',
                        'aria-label': '修改分组 group（留空不改）',
                        value: groupDraft,
                        disabled: detailBusy,
                        onChange: event => setGroupDraft(event.target.value),
                      })),
                    h('button', {
                      className: 'pconsole-btn pconsole-btn-primary',
                      type: 'button',
                      disabled: detailBusy,
                      onClick: () => saveMetaEdits(),
                    }, '保存 kind / 分组'),
                    h('label', { className: 'pconsole-field' },
                      h('span', null, 'grade'),
                      h('select', {
                        className: 'pconsole-select',
                        value: item.grade || '',
                        disabled: detailBusy,
                        'aria-label': '修改重要度等级',
                        onChange: event => void updateMemory({ grade: event.target.value }),
                      }, ...MEMORY_GRADES.map(value => h('option', { key: value, value }, value)))),
                    h('label', { className: 'pconsole-field' },
                      h('span', null, '敏感级'),
                      h('select', {
                        className: 'pconsole-select',
                        value: item.sensitivity || 'personal',
                        disabled: detailBusy,
                        'aria-label': '修改敏感级',
                        onChange: event => void updateMemory({ sensitivity: event.target.value }),
                      }, ...SENSITIVITY_OPTIONS.map(value => h('option', { key: value, value }, value)))),
                    h('label', { className: 'pconsole-field' },
                      h('span', null, '状态'),
                      h('select', {
                        className: 'pconsole-select',
                        value: item.status || 'active',
                        disabled: detailBusy,
                        'aria-label': '修改状态',
                        onChange: event => void updateMemory({ status: event.target.value }),
                      },
                        h('option', { value: 'active' }, '活跃'),
                        h('option', { value: 'dormant' }, '休眠'),
                        h('option', { value: 'archived' }, '归档'))),
                    h('label', { className: 'pconsole-field' },
                      h('span', null, '重要度值'),
                      h('input', {
                        className: 'pconsole-input',
                        type: 'number',
                        min: 0,
                        max: 1,
                        step: 0.05,
                        style: { width: '84px' },
                        'aria-label': '修改重要度数值（0–1）',
                        value: importanceDraft,
                        disabled: detailBusy,
                        onChange: event => setImportanceDraft(event.target.value),
                      }),
                      h('button', {
                        className: 'pconsole-btn',
                        type: 'button',
                        disabled: detailBusy || importanceDraft === ''
                          || Number(importanceDraft) === item.importance,
                        onClick: () => void updateMemory({ importance: Number(importanceDraft) }),
                      }, '保存')),
                  ),
                  // 分组输入建议：/console/groups 的树展平（含子组路径）。
                  h('datalist', { id: 'pconsole-group-options' },
                    ...groupRows.map(row => h('option', { key: row.path, value: row.path }))),

                  h('p', { className: 'pconsole-subhead' }, '反馈'),
                  h('div', { className: 'pconsole-actions', style: { margin: 0 } },
                    ...FEEDBACK_SIGNALS.map(signal =>
                      h('button', {
                        key: signal,
                        className: 'pconsole-btn',
                        type: 'button',
                        disabled: detailBusy,
                        onClick: () => void sendFeedback(signal),
                      }, FEEDBACK_LABELS[signal])),
                    h('button', {
                      key: 'pin',
                      className: 'pconsole-btn',
                      type: 'button',
                      disabled: detailBusy,
                      'aria-pressed': itemPinned ? 'true' : 'false',
                      onClick: () => void sendFeedback(itemPinned ? 'unpin' : 'pin'),
                    }, itemPinned ? '取消置顶' : '置顶'),
                  ),

                  evidence.length
                    ? h('div', null,
                        h('p', { className: 'pconsole-subhead' }, `证据原话 · ${evidence.length}`),
                        ...evidence.slice(0, 10).map((entry, index) =>
                          h('div', { key: `evidence-${index}`, className: 'pconsole-evidence' },
                            h('div', { className: 'pconsole-item-meta' },
                              h('span', null, entry?.role || '—'),
                              entry?.session ? h('span', null, `会话 ${entry.session}`) : null,
                              entry?.line !== undefined ? h('span', null, `第 ${entry.line} 行`) : null,
                              entry?.at ? h('span', null, fmtTime(entry.at)) : null),
                            h('div', null, clampText(entry?.text, 260)))))
                    : h('p', { className: 'pconsole-note' }, '没有可访问的原始话（trace 拿不到时为空，不是出错）。'),

                  history.length
                    ? h('div', null,
                        h('p', { className: 'pconsole-subhead' }, '变更历史'),
                        h('ul', { className: 'pconsole-timeline' },
                          ...history.map((entry, index) =>
                            h('li', { key: `history-${index}` },
                              `${fmtTime(entry?.at)} · ${entry?.action || '—'}`
                              + (entry?.actor ? ` · ${entry.actor}` : '')
                              + (entry?.note ? ` · ${entry.note}` : '')))))
                    : h('p', { className: 'pconsole-note' }, '没有变更历史。'),

                  conflicts.length
                    ? h('div', null,
                        h('p', { className: 'pconsole-subhead' }, `冲突 · ${conflicts.length}`),
                        h('table', { className: 'pconsole-table' },
                          h('thead', null, h('tr', null,
                            h('th', null, '对方'), h('th', null, 'kind'), h('th', null, 'slot'),
                            h('th', null, '严重度'), h('th', null, '状态'))),
                          h('tbody', null, ...conflicts.map((entry, index) =>
                            h('tr', { key: `conflict-${index}` },
                              h('td', null, entry?.other || '—'),
                              h('td', null, entry?.kind || '—'),
                              h('td', null, entry?.slot || '—'),
                              h('td', null, entry?.severity !== undefined ? String(entry.severity) : '—'),
                              h('td', null, entry?.status || '—'))))))
                    : null,

                  related.length
                    ? h('div', null,
                        h('p', { className: 'pconsole-subhead' }, `相关记忆 · ${related.length}`),
                        h('ul', { className: 'pconsole-bullets' },
                          ...related.map(entry =>
                            h('li', { key: `related-${entry.id}` },
                              h('button', {
                                className: 'pconsole-btn',
                                type: 'button',
                                style: { padding: '1px 8px' },
                                onClick: () => select(entry.id),
                              }, clampText(entry.text, 70))))))
                    : null,

                  h('p', { className: 'pconsole-subhead' }, '实体 / 关联网'),
                  h('p', { className: 'pconsole-note', style: { margin: 0 } },
                    '后端 /console/memory 不返回实体关系；去「实体」分区做 1/2 跳查询，或终端 ',
                    h('code', { className: 'pconsole-code' }, `pmem net query ${item.id} --hops 2`),
                    '。'),

                  h('div', { className: 'pconsole-danger' },
                    h('p', { className: 'pconsole-subhead', style: { margin: '0 0 6px' } }, '危险操作'),
                    pending === ''
                      ? h('div', { className: 'pconsole-actions', style: { margin: 0 } },
                          h('button', {
                            className: 'pconsole-btn',
                            type: 'button',
                            disabled: detailBusy,
                            onClick: () => setPending('archive'),
                          }, '撤回（归档）'),
                          h('button', {
                            className: 'pconsole-btn pconsole-btn-danger',
                            type: 'button',
                            disabled: detailBusy,
                            onClick: () => setPending('delete'),
                          }, '删除…'))
                      : h('div', { className: 'pconsole-confirm', role: 'alertdialog', 'aria-label': '确认危险操作' },
                          h('p', null,
                            pending === 'archive'
                              ? '确认撤回？记忆会降为 archived，之后可以恢复。'
                              : '删除后默认 7 天内可恢复；「彻底删除」不可恢复。'),
                          h('div', { className: 'pconsole-actions' },
                            h('button', {
                              className: 'pconsole-btn',
                              type: 'button',
                              disabled: detailBusy,
                              onClick: () => void runDanger('archive'),
                            }, pending === 'archive' ? '确认撤回' : '改为撤回'),
                            h('button', {
                              className: 'pconsole-btn pconsole-btn-danger',
                              type: 'button',
                              disabled: detailBusy,
                              onClick: () => void runDanger('delete'),
                            }, '确认软删'),
                            pending === 'delete'
                              ? h('button', {
                                  className: 'pconsole-btn pconsole-btn-danger',
                                  type: 'button',
                                  disabled: detailBusy,
                                  onClick: () => void runDanger('hard'),
                                }, '彻底删除')
                              : null,
                            h('button', {
                              className: 'pconsole-btn',
                              type: 'button',
                              onClick: () => setPending(''),
                            }, '取消'))),
                  ),
                ),
      )

      return h(
        'div',
        { className: 'pconsole-memory' },
        toolbar,
        h('div', { className: 'pconsole-two-col' }, list, detailPane),
      )
    }

    // 实体 / 关系：真路由（/console/entities + /console/entity + /console/net/query + /console/net/merge）。
    // 范围：列表、详情（别名/提及记忆/1 跳）、2 跳查询、合并（dry_run 预览→二次确认）、跳星图。
    // 不做新增/休眠关系（需求第 8 轮明确排除）。
    function ConsoleEntities({ notify, goto }) {
      const [query, setQuery] = useState('')
      const [typeFilter, setTypeFilter] = useState('')
      const [items, setItems] = useState([])
      const [total, setTotal] = useState(0)
      const [hasMore, setHasMore] = useState(false)
      const [offset, setOffset] = useState(0)
      const [listLoading, setListLoading] = useState(false)
      const [error, setError] = useState('')
      const [notice, setNotice] = useState('')

      const [selected, setSelected] = useState('')
      const [detail, setDetail] = useState(null)
      const [detailLoading, setDetailLoading] = useState(false)
      const [detailError, setDetailError] = useState('')
      const [candidates, setCandidates] = useState([])

      const [netSrc, setNetSrc] = useState('')
      const [netDst, setNetDst] = useState('')
      const [depth, setDepth] = useState('2')
      const [netResult, setNetResult] = useState(null)
      const [netBusy, setNetBusy] = useState(false)

      const [mergeRight, setMergeRight] = useState('')
      const [mergePreview, setMergePreview] = useState(null)
      const [mergeBusy, setMergeBusy] = useState(false)

      const [starmapUrl, setStarmapUrl] = useState('')
      const [starmapBusy, setStarmapBusy] = useState(false)

      const PAGE_SIZE = 20

      const say = useCallback(message => {
        setNotice(message)
        notify?.(message)
      }, [notify])

      const loadList = useCallback(async (options = {}) => {
        const nextQuery = options.query ?? query
        const nextType = options.type ?? typeFilter
        const nextOffset = options.offset ?? 0
        const append = options.append === true
        const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(nextOffset) })
        if (String(nextQuery).trim()) params.set('q', String(nextQuery).trim())
        if (nextType) params.set('type', nextType)
        setListLoading(true)
        setError('')
        try {
          const data = await apiConsole('/entities?' + params.toString())
          const list = Array.isArray(data.items) ? data.items : []
          setItems(previous => (append ? previous.concat(list) : list))
          setTotal(Number(data.total) || list.length)
          setHasMore(data.hasMore === true)
          setOffset(nextOffset + list.length)
          if (Array.isArray(data.degraded) && data.degraded.length) {
            setNotice('实体列表降级：' + data.degraded.join(' / ') + '（条数可能不全）')
          }
        } catch (cause) {
          if (!append) setItems([])
          setError('实体列表没取到：' + cause.message)
        } finally {
          setListLoading(false)
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, [query, typeFilter])

      useEffect(() => {
        void loadList({ offset: 0 })
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, [])

      const loadDetail = async ref => {
        if (!ref) return
        setSelected(ref)
        setCandidates([])
        setDetailError('')
        setDetailLoading(true)
        try {
          const lookup = String(ref).startsWith('id:')
            ? 'id=' + encodeURIComponent(String(ref).slice(3))
            : 'name=' + encodeURIComponent(ref)
          const raw = await requestRaw(CONSOLE + '/entity?' + lookup)
          if (raw.status === 409) {
            const list = Array.isArray(raw.data?.details?.candidates) ? raw.data.details.candidates : []
            setDetail(null)
            setCandidates(list)
            setDetailError(raw.data?.error || '这个实体名对应多个身份，请按 id 选择')
            return
          }
          if (!raw.data?.ok) throw new Error(raw.data?.error || 'HTTP ' + raw.status)
          setDetail(raw.data)
          if (Array.isArray(raw.data?.degraded) && raw.data.degraded.length) {
            setNotice('实体详情降级：' + raw.data.degraded.join(' / '))
          }
          setNetSrc(String(raw.data?.entity?.name ?? ''))
          setMergeRight('')
        } catch (cause) {
          setDetail(null)
          setDetailError('读实体详情失败：' + cause.message)
        } finally {
          setDetailLoading(false)
        }
      }

      const runNetQuery = async () => {
        const src = netSrc.trim()
        const dst = netDst.trim()
        if (!src && !dst) { setError('2 跳查询至少要给一个起点或终点'); return }
        setNetBusy(true)
        setError('')
        setNetResult(null)
        try {
          const body = { depth: depth === '1' ? 1 : 2, limit: 30 }
          if (src) body.src = src
          if (dst) body.dst = dst
          setNetResult(await apiConsole('/net/query', { method: 'POST', body: JSON.stringify(body) }))
        } catch (cause) {
          setError('2 跳查询失败：' + cause.message)
        } finally {
          setNetBusy(false)
        }
      }

      const runMergePreview = async () => {
        const left = selected ? (selected.startsWith('id:') ? selected.slice(3) : selected) : ''
        const right = mergeRight.trim()
        if (!left || !right) { setError('先在左边选一个实体，再填要被合并的实体'); return }
        setMergeBusy(true)
        setError('')
        setMergePreview(null)
        try {
          const raw = await requestRaw(CONSOLE + '/net/merge', {
            method: 'POST',
            body: JSON.stringify({ left, right, dry_run: true }),
          })
          if (raw.status === 409) {
            const list = Array.isArray(raw.data?.details?.candidates) ? raw.data.details.candidates : []
            setCandidates(list)
            setError(raw.data?.error || '合并对象里有同名多身份，请先按 id 选择')
            return
          }
          if (!raw.data?.ok) throw new Error(raw.data?.error || 'HTTP ' + raw.status)
          setMergePreview(raw.data)
        } catch (cause) {
          setError('合并预览失败：' + cause.message)
        } finally {
          setMergeBusy(false)
        }
      }

      const runMergeConfirm = async () => {
        if (!mergePreview) return
        const left = mergePreview?.keep?.id ?? mergePreview?.keep?.name
        const right = mergePreview?.drop?.id ?? mergePreview?.drop?.name
        setMergeBusy(true)
        setError('')
        try {
          const data = await apiConsole('/net/merge', {
            method: 'POST',
            body: JSON.stringify({ left, right, dry_run: false }),
          })
          say('合并完成：别名 ' + (data.aliases ?? 0) + ' · 提及 ' + (data.mentions ?? 0) + ' · 关系 ' + (data.relations ?? 0))
          setMergePreview(null)
          setMergeRight('')
          setSelected('')
          setDetail(null)
          await loadList({ offset: 0 })
        } catch (cause) {
          setError('合并失败：' + cause.message)
        } finally {
          setMergeBusy(false)
        }
      }

      const openStarmap = async () => {
        setStarmapBusy(true)
        setError('')
        try {
          const data = await api('/starmap/open', { method: 'POST', body: '{}' })
          if (data?.url) setStarmapUrl(data.url)
          if (data?.opened === false) setError(data.warning || '浏览器没拉起来，地址在下面，自己点一下')
          else say('已在浏览器打开星图')
        } catch (cause) {
          setError('打开星图失败：' + cause.message)
        } finally {
          setStarmapBusy(false)
        }
      }

      const entity = detail?.entity ?? null
      const aliases = Array.isArray(detail?.aliases) ? detail.aliases : []
      const mentions = Array.isArray(detail?.mentions) ? detail.mentions : []
      const relations = Array.isArray(detail?.relations) ? detail.relations : []
      const counts = detail?.counts ?? null
      const netItems = Array.isArray(netResult?.items) ? netResult.items : []

      const relationText = item => {
        const src = item?.src?.name ?? item?.src ?? '—'
        const dst = item?.dst?.name ?? item?.dst ?? '—'
        const arrow = item?.direction === 'in' ? '←' : item?.direction === 'self' ? '↔' : '→'
        return String(src) + ' ' + arrow + ' ' + String(item?.predicate ?? '—') + ' ' + arrow + ' ' + String(dst)
      }

      const listColumn = h(
        'div',
        { className: 'pconsole-col' },
        h(
          'form',
          {
            className: 'pconsole-search',
            role: 'search',
            onSubmit: event => { event.preventDefault(); void loadList({ offset: 0 }) },
          },
          h('input', {
            className: 'pconsole-input pconsole-search-input',
            type: 'search',
            placeholder: '搜实体名',
            'aria-label': '搜索实体名',
            value: query,
            onChange: event => setQuery(event.target.value),
          }),
          h('button', {
            className: 'pconsole-btn pconsole-btn-primary',
            type: 'submit',
            disabled: listLoading,
          }, listLoading ? '检索中…' : '检索'),
        ),
        h('div', { className: 'pconsole-toolbar', style: { margin: '8px 0 6px' } },
          h('label', { className: 'pconsole-field' },
            h('span', null, '类型'),
            h('input', {
              className: 'pconsole-input',
              style: { width: '110px' },
              placeholder: 'person / place',
              'aria-label': '按类型过滤',
              value: typeFilter,
              onChange: event => setTypeFilter(event.target.value),
            })),
          h('button', { className: 'pconsole-btn', type: 'button', onClick: () => { setTypeFilter(''); void loadList({ type: '', offset: 0 }) } }, '清空类型'),
          h('span', { className: 'pconsole-note', style: { margin: 0 } },
            items.length + (total ? ' / ' + total : '') + ' 个实体'),
        ),
        items.length
          ? h('ul', { className: 'pconsole-list', role: 'listbox', 'aria-label': '实体列表' },
              ...items.map(item => h('li', { key: item?.id ?? item?.name },
                h('button', {
                  type: 'button',
                  role: 'option',
                  'aria-selected': selected === (item?.id ? 'id:' + item.id : item?.name) ? 'true' : 'false',
                  className: 'pconsole-item',
                  onClick: () => void loadDetail(item?.id ? 'id:' + item.id : item?.name),
                },
                  h('div', { className: 'pconsole-item-meta' },
                    h('span', { className: 'pconsole-chip pconsole-chip-muted' }, item?.type || '实体'),
                    item?.grade ? h('span', { className: 'pconsole-chip' }, item.grade) : null,
                    item?.mention_count !== undefined ? h('span', null, '提及 ' + item.mention_count) : null),
                  h('div', { className: 'pconsole-item-text' }, item?.name || '—'),
                  Array.isArray(item?.groups) && item.groups.length
                    ? h('div', { className: 'pconsole-item-meta', style: { marginTop: '2px' } },
                        ...item.groups.slice(0, 3).map(group => h('span', { key: group, className: 'pconsole-chip pconsole-chip-muted' }, group)))
                    : null))))
          : h('p', { className: 'pconsole-empty' },
              listLoading ? '读取实体列表…' : '还没有实体。跑一次 pmem net extract 再回来。'),
        hasMore
          ? h('div', { className: 'pconsole-actions' },
              h('button', { className: 'pconsole-btn', type: 'button', disabled: listLoading, onClick: () => void loadList({ offset, append: true }) },
                listLoading ? '加载中…' : '加载更多'))
          : null,
      )

      const aliasChips = aliases.map((alias, index) =>
        h('span', { key: 'alias-' + index, className: 'pconsole-chip pconsole-chip-muted' },
          typeof alias === 'string' ? alias : describeEntry(alias)))

      const relationRows = relations.slice(0, 40).map((relation, index) =>
        h('div', { key: relation?.relation_id ?? 'rel-' + index, className: 'pconsole-relation' },
          h('div', { className: 'pconsole-relation-head' },
            h('span', { className: 'pconsole-relation-pred' }, relation?.predicate ?? '—'),
            h('span', null, relationText(relation)),
            typeof relation?.weight === 'number'
              ? h('span', { className: 'pconsole-note', style: { margin: 0 } }, '权重 ' + relation.weight)
              : null)))

      const mentionRows = mentions.slice(0, 20).map((mention, index) =>
        h('li', { key: mention?.id ?? 'mention-' + index },
          mention?.id && goto
            ? h('button', {
                className: 'pconsole-btn',
                type: 'button',
                style: { padding: '1px 8px' },
                onClick: () => goto('memory', { selectId: mention.id }),
              }, clampText(mention.text ?? describeEntry(mention), 70))
            : h('span', null, clampText(mention?.text ?? describeEntry(mention), 70))))

      const netRows = netItems.slice(0, 50).map((relation, index) =>
        h('details', { key: relation?.relation_id ?? 'net-' + index, className: 'pconsole-relation' },
          h('summary', null,
            h('span', { className: 'pconsole-relation-pred' }, relation?.predicate ?? '—'),
            ' ', relationText(relation)),
          h('pre', { className: 'pconsole-pre', style: { marginTop: '4px' } }, pretty(relation))))

      const detailColumn = h(
        'div',
        { className: 'pconsole-col pconsole-detail' },
        detailLoading
          ? h('p', { className: 'pconsole-empty' }, '读实体详情…')
          : detailError
            ? h('div', null,
                h('p', { className: 'pconsole-error', role: 'alert' }, detailError),
                candidates.length
                  ? h('div', null,
                      h('p', { className: 'pconsole-subhead' }, '同名候选（用 id 选一个）'),
                      h('div', { className: 'pconsole-actions', style: { margin: 0 } },
                        ...candidates.map(candidate => h('button', {
                          key: candidate?.id ?? candidate?.name,
                          className: 'pconsole-btn',
                          type: 'button',
                          onClick: () => void loadDetail('id:' + candidate?.id),
                        }, (candidate?.name ?? '—') + ' · ' + (candidate?.type ?? '?') + ' · ' + String(candidate?.id ?? '').slice(0, 8)))))
                  : null)
            : !detail
              ? h('p', { className: 'pconsole-empty' },
                  '从左边选一个实体：别名、提及它的记忆、1 跳关系、2 跳查询和合并都在这里。')
              : h('div', null,
                  h('h3', { className: 'pconsole-detail-title' },
                    entity?.name || '实体',
                    h('span', { className: 'pconsole-chip' }, entity?.type || '实体'),
                    entity?.grade ? h('span', { className: 'pconsole-chip pconsole-chip-muted' }, entity.grade) : null),
                  h('div', { className: 'pconsole-actions', style: { margin: '0 0 8px' } },
                    h('button', { className: 'pconsole-btn', type: 'button', disabled: starmapBusy, onClick: () => void openStarmap() },
                      starmapBusy ? '打开中…' : '在星图中查看'),
                    h('button', { className: 'pconsole-btn', type: 'button', onClick: () => void copyText(pretty(detail), notify, '已复制实体 JSON') }, '复制 JSON'),
                    h('button', { className: 'pconsole-btn', type: 'button', onClick: () => void copyText('pmem net query ' + String(entity?.name ?? '') + ' --depth 1 --json', notify, '已复制查询命令') }, '复制 CLI 查询'),
                    goto ? h('button', { className: 'pconsole-btn', type: 'button', onClick: () => goto('settings') }, '去设置') : null),
                  kvRow('id', entity?.id),
                  kvRow('类型', entity?.type),
                  kvRow('重要度', entity?.grade),
                  kvRow('提及次数', entity?.mention_count ?? entity?.mentions),
                  kvRow('置信度', entity?.confidence),
                  kvRow('分组', Array.isArray(entity?.groups) && entity.groups.length ? entity.groups.join(' · ') : ''),
                  kvRow('首次出现', entity?.first_seen ? fmtTime(entity.first_seen) : ''),
                  kvRow('最近出现', entity?.last_seen ? fmtTime(entity.last_seen) : ''),
                  kvRow('计数', counts ? pretty(counts).replace(/\s+/g, ' ') : ''),
                  aliases.length
                    ? h('div', null,
                        h('p', { className: 'pconsole-subhead' }, '别名 · ' + aliases.length),
                        h('div', { className: 'pconsole-actions', style: { margin: 0 } }, ...aliasChips))
                    : null,
                  h('p', { className: 'pconsole-subhead' }, '1 跳关系 · ' + relations.length),
                  relations.length ? h('div', null, ...relationRows) : h('p', { className: 'pconsole-note', style: { margin: 0 } }, '没有 1 跳关系。'),
                  h('p', { className: 'pconsole-subhead' }, '提及它的记忆 · ' + mentions.length),
                  mentions.length ? h('ul', { className: 'pconsole-bullets' }, ...mentionRows) : h('p', { className: 'pconsole-note', style: { margin: 0 } }, '没有提及记忆（或后端降级）。'),

                  h('p', { className: 'pconsole-subhead' }, '2 跳查询'),
                  h('div', { className: 'pconsole-inline-form' },
                    h('input', { className: 'pconsole-input', style: { flex: '1 1 120px' }, placeholder: 'src 实体', 'aria-label': '2 跳查询起点', value: netSrc, onChange: event => setNetSrc(event.target.value) }),
                    h('input', { className: 'pconsole-input', style: { flex: '1 1 120px' }, placeholder: 'dst（可空）', 'aria-label': '2 跳查询终点', value: netDst, onChange: event => setNetDst(event.target.value) }),
                    h('select', { className: 'pconsole-select', value: depth, 'aria-label': '跳数', onChange: event => setDepth(event.target.value) },
                      h('option', { value: '1' }, '1 跳'), h('option', { value: '2' }, '2 跳')),
                    h('button', { className: 'pconsole-btn', type: 'button', disabled: netBusy, onClick: () => void runNetQuery() }, netBusy ? '查询中…' : '查询')),
                  netResult
                    ? h('div', null,
                        h('p', { className: 'pconsole-note', style: { margin: '4px 0' } }, '扫描 ' + (netResult.scanned ?? 0) + ' 个，命中 ' + netItems.length + ' 条'),
                        netItems.length ? h('div', null, ...netRows) : h('p', { className: 'pconsole-note', style: { margin: 0 } }, '没有命中关系。'))
                    : null,

                  h('p', { className: 'pconsole-subhead' }, '合并实体（先预览，再确认）'),
                  h('div', { className: 'pconsole-inline-form' },
                    h('span', { className: 'pconsole-field' }, h('span', null, '保留 '), h('span', { className: 'pconsole-code' }, entity?.name ?? '—')),
                    h('input', { className: 'pconsole-input', style: { flex: '1 1 140px' }, placeholder: '要被合并的实体（id 或名字）', 'aria-label': '合并目标实体', value: mergeRight, onChange: event => setMergeRight(event.target.value) }),
                    h('button', { className: 'pconsole-btn', type: 'button', disabled: mergeBusy, onClick: () => void runMergePreview() }, mergeBusy ? '预览中…' : '预览合并')),
                  mergePreview
                    ? h('div', { className: 'pconsole-confirm', role: 'alertdialog', 'aria-label': '确认合并' },
                        h('p', null, '把「' + (mergePreview?.drop?.name ?? '—') + '」合并进「' + (mergePreview?.keep?.name ?? '—') + '」？'),
                        h('p', { className: 'pconsole-note', style: { margin: '2px 0 0' } },
                          '预览：keep 计数 ' + pretty(mergePreview?.keep_counts ?? {}).replace(/\s+/g, ' ')
                          + ' · drop 计数 ' + pretty(mergePreview?.drop_counts ?? {}).replace(/\s+/g, ' ')
                          + ' · 两者之间关系 ' + (mergePreview?.relations_between ?? []).length + ' 条'
                          + (mergePreview?.degraded?.length ? '（降级：' + mergePreview.degraded.join(' / ') + '）' : '')),
                        h('div', { className: 'pconsole-actions' },
                          h('button', { className: 'pconsole-btn pconsole-btn-danger', type: 'button', disabled: mergeBusy, onClick: () => void runMergeConfirm() }, '确认合并（不可撤销）'),
                          h('button', { className: 'pconsole-btn', type: 'button', onClick: () => setMergePreview(null) }, '取消')))
                    : null,
                  starmapUrl
                    ? h('p', { className: 'pconsole-note' }, '星图地址：', h('code', { className: 'pconsole-code' }, starmapUrl))
                    : null,
                ),
      )

      return h('div', { className: 'pconsole-entities' },
        error ? h('p', { className: 'pconsole-banner', role: 'alert' }, error) : null,
        notice ? h('p', { className: 'pconsole-note', role: 'status' }, notice) : null,
        h('div', { className: 'pconsole-two-col' }, listColumn, detailColumn),
      )
    }
    // 睡眠：run 列表 + 八阶段（每阶段右侧小条形图）+ 现在入睡 / 大睡一场 / 进行中轮询 + 质量趋势。
    function ConsoleSleep({ notify }) {
      const [night, setNight] = useState(null)
      const [loading, setLoading] = useState(true)
      const [error, setError] = useState('')
      const [sleeping, setSleeping] = useState(false)
      const [openRuns, setOpenRuns] = useState({})
      const [bigOpen, setBigOpen] = useState(false)
      const [bigBusy, setBigBusy] = useState(false)
      const [bigResult, setBigResult] = useState(null)
      const defaults = loadPrefs()
      const [bigForm, setBigForm] = useState({
        start: '',
        end: '',
        limit: defaults.bigLimit ? String(defaults.bigLimit) : '',
        dry: false,
        template: defaults.bigTemplate === true,
        skipDigested: false,
      })

      const loadNight = useCallback(async () => {
        setLoading(true)
        try {
          setNight(await api('/sleep'))
          setError('')
        } catch (cause) {
          setNight(null)
          setError(`读不到睡眠记录：${cause.message}`)
        } finally {
          setLoading(false)
        }
      }, [])

      useEffect(() => {
        void loadNight()
      }, [loadNight])

      const runs = night?.data?.runs ?? []
      const latest = runs[0] ?? null
      const latestStages = (latest?.stages ?? []).filter(Boolean)
      const running = latest ? String(latest.status) === 'running' : false
      const doneStages = latestStages.filter(stage => stage.skipped !== true && stage.ok !== false).length
      const currentStage = latestStages.find(stage => stage.skipped !== true && stage.ok === false)
        ?? latestStages.find(stage => stage.skipped !== true && stage.ok !== true)
        ?? null

      // 进行中：5 秒轮询一次 /sleep，进度条跟着走；跑完自动停。
      useEffect(() => {
        if (!running) return undefined
        const timer = setInterval(() => { void loadNight() }, 5000)
        return () => clearInterval(timer)
      }, [running, loadNight])

      const sleepNow = async () => {
        setSleeping(true)
        setError('')
        try {
          const data = await api('/sleep/now', { method: 'POST', body: '{}' })
          if (!data?.ok) setError(`入睡失败：${data?.error || '未知'}`)
          else notify?.(data?.data?.status === 'ok' ? '跑完了' : `跑完：${data?.data?.status ?? '未知'}`)
          await loadNight()
        } catch (cause) {
          setError(`入睡失败：${cause.message}`)
        } finally {
          setSleeping(false)
        }
      }

      // 大睡一场：默认 DSH（逐天开会话）；勾模板模式才本地跑（可选演练）。
      const sleepBig = async () => {
        setBigBusy(true)
        setError('')
        setBigResult(null)
        try {
          const shared = {
            start: bigForm.start || undefined,
            end: bigForm.end || undefined,
            limit: Number(bigForm.limit) || 0,
            skip_digested: bigForm.skipDigested === true,
          }
          if (bigForm.template) {
            const data = await api('/sleep/big', {
              method: 'POST',
              body: JSON.stringify({ ...shared, template: true, dry_run: bigForm.dry === true }),
            })
            if (!data?.ok) {
              setError(`大睡失败：${data?.error || data?.data?.error?.message || '未知'}`)
            } else {
              setBigResult({ mode: 'template', ...data.data })
              notify?.(data.data?.dry_run ? '演练完成，没有落库' : '大睡完成（模板模式）')
            }
            await loadNight()
          } else {
            const data = await api('/sleep/host-big', {
              method: 'POST',
              body: JSON.stringify(shared),
            })
            if (!data?.ok) {
              setError(`大睡失败：${data?.error || '未知'}`)
            } else {
              setBigResult({
                mode: 'host',
                count: data.count,
                sessions: data.sessions || [],
                days: data.days || [],
              })
              notify?.(data.count
                ? `已开 ${(data.sessions || []).length} 个鲸眠会话（${data.count} 天）`
                : '没有需要睡的日子')
              await loadNight()
            }
          }
        } catch (cause) {
          setError(`大睡失败：${cause.message}`)
        } finally {
          setBigBusy(false)
        }
      }

      // 大睡交给 DSH 之后，睡眠流水可能陆续更新，轮询一分钟。
      useEffect(() => {
        if (bigResult?.mode !== 'host' || !bigResult.count) return undefined
        let ticks = 0
        const timer = setInterval(() => {
          ticks += 1
          if (ticks > 12) clearInterval(timer)
          void loadNight()
        }, 5000)
        return () => clearInterval(timer)
      }, [bigResult, loadNight])

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

      return h(
        'div',
        { className: 'pconsole-sleep' },
        error ? h('p', { className: 'pconsole-banner', role: 'alert' }, error) : null,
        h(
          'div',
          { className: 'pconsole-toolbar' },
          h('button', {
            className: 'pconsole-btn pconsole-btn-primary',
            type: 'button',
            disabled: sleeping,
            onClick: () => void sleepNow(),
          }, sleeping ? '入睡中…' : '现在入睡'),
          h('button', {
            className: 'pconsole-btn',
            type: 'button',
            disabled: loading,
            onClick: () => void loadNight(),
          }, loading ? '刷新中…' : '刷新'),
          h('button', {
            className: 'pconsole-btn',
            type: 'button',
            onClick: () => setBigOpen(prev => !prev),
          }, bigOpen ? '收起大睡' : '大睡一场'),
          h('span', { className: 'pconsole-note', style: { margin: 0 } },
            '八阶段：收集 → 去重 → 关系 → 冲突 → 规律 → 特质 → 反思 → 快照'),
        ),

        // 进行中：进度条 + 当前阶段（5s 轮询）。
        running
          ? h(
              'div',
              { className: 'pconsole-run', role: 'status', 'aria-live': 'polite' },
              h('div', { className: 'pconsole-run-inline' },
                h('strong', null, `进行中 · ${latest?.day ?? '—'}`),
                h('span', { className: 'pconsole-tag' }, `${doneStages}/8 阶段`),
                currentStage ? h('span', { className: 'pconsole-run-meta' }, `当前 ${stageLabel(currentStage)}`) : null,
                h('span', { className: 'pconsole-run-meta' }, '每 5s 自动刷新')),
              h('div', { className: 'pconsole-progress', 'aria-hidden': 'true' },
                h('span', { style: { width: `${Math.max(3, Math.round((doneStages / 8) * 100))}%` } })),
            )
          : null,

        bigOpen
          ? h(
              'div',
              { className: 'pconsole-big-form' },
              h('p', { className: 'pconsole-note', style: { margin: 0 } },
                '大睡一场：把已有记忆按天从头睡一遍。默认由 DSH 来（每天开一个鲸眠会话，宿主模型逐天调 prep/finish）；'
                + '勾「模板模式」才在本地用模板跑（0 成本）。'),
              h(
                'div',
                { className: 'pconsole-big-row' },
                h('span', { className: 'pconsole-field' }, '从'),
                h('input', {
                  className: 'pconsole-input',
                  type: 'date',
                  value: bigForm.start,
                  'aria-label': '大睡开始日期',
                  onChange: event => setBigForm(prev => ({ ...prev, start: event.target.value })),
                }),
                h('span', { className: 'pconsole-field' }, '到'),
                h('input', {
                  className: 'pconsole-input',
                  type: 'date',
                  value: bigForm.end,
                  'aria-label': '大睡结束日期',
                  onChange: event => setBigForm(prev => ({ ...prev, end: event.target.value })),
                }),
                h('span', { className: 'pconsole-field' }, '最多'),
                h('input', {
                  className: 'pconsole-input',
                  type: 'number',
                  min: 0,
                  placeholder: '全部',
                  style: { width: '90px' },
                  'aria-label': '最多几天',
                  value: bigForm.limit,
                  onChange: event => setBigForm(prev => ({ ...prev, limit: event.target.value })),
                }),
                h('span', { className: 'pconsole-field' }, '天'),
              ),
              h(
                'div',
                { className: 'pconsole-big-row' },
                h('label', { className: 'pconsole-field' },
                  h('input', {
                    type: 'checkbox',
                    className: 'pconsole-check',
                    checked: bigForm.template,
                    onChange: event => {
                      const value = event.target.checked
                      setBigForm(prev => ({ ...prev, template: value }))
                      savePrefs({ bigTemplate: value })
                    },
                  }), ' 模板模式（0 成本，不经过 DSH）'),
                h('label', { className: 'pconsole-field' },
                  h('input', {
                    type: 'checkbox',
                    className: 'pconsole-check',
                    checked: bigForm.skipDigested,
                    onChange: event => setBigForm(prev => ({ ...prev, skipDigested: event.target.checked })),
                  }), ' 只睡没消化的天'),
                h('label', { className: 'pconsole-field' },
                  h('input', {
                    type: 'checkbox',
                    className: 'pconsole-check',
                    checked: bigForm.dry,
                    disabled: !bigForm.template,
                    onChange: event => setBigForm(prev => ({ ...prev, dry: event.target.checked })),
                  }), ' 演练（仅模板模式）'),
                h('button', {
                  className: 'pconsole-btn pconsole-btn-primary',
                  type: 'button',
                  disabled: bigBusy,
                  onClick: () => void sleepBig(),
                }, bigBusy ? '大睡中…' : '开始大睡'),
              ),
              bigResult
                ? h('p', { className: 'pconsole-note' },
                    bigResult.mode === 'host'
                      ? (bigResult.count
                          ? `已交给 DSH：${bigResult.count} 天，开了 ${(bigResult.sessions || []).length} 个鲸眠会话；`
                            + '进度到会话列表看，睡眠流水会在跑完后更新。'
                          : '没有需要睡的日子（这个范围里的流水都消化过了）。')
                      : `模板跑完：${bigResult.days ?? 0} 天（跳过 ${bigResult.skipped_days ?? 0} / `
                        + `失败 ${bigResult.failed_days ?? 0}），用时 ${bigResult.elapsed_s ?? 0}s`
                        + `${bigResult.dry_run ? '（演练，未落库）' : ''}`
                        + `${bigResult.note ? `；${bigResult.note}` : ''}`)
                : null,
            )
          : null,

        h(
          Panel,
          { title: '最近 14 次睡眠质量分趋势' },
          h(QualityTrend, { runs }),
        ),

        h(
          Panel,
          { title: `睡眠流水${runs.length ? ` · ${runs.length} 次` : ''}` },
          runs.length
            ? h('div', null,
                ...runs.map((run, index) =>
                  h(NightRun, {
                    key: runKey(run, index),
                    run,
                    open: isRunOpen(run, index),
                    onToggle: () => toggleRun(run, index),
                  })))
            : h('p', { className: 'pconsole-empty' },
                loading ? '加载中…' : '还没有睡眠记录。跑一次「现在入睡」就有。'),
          '每次睡眠一张卡，点标题展开八个阶段：绿条=跑完，红条=失败，淡一点=跳过；'
          + '每阶段右侧的小条是耗时/条数。数据来自 pmem sleep log，最多 14 次。',
        ),
      )
    }

    // 设置：运行时开关与数值 / 检索与夜间默认值 / 排查面板 / 危险区（折叠）。
    // 只有运行时开关会写 ~/.dsh/pmem/runtime.json；其余默认值存 localStorage（纯前端偏好）。
    function ConsoleSettings({ boot, notify }) {
      const [status, setStatus] = useState(boot?.status ?? null)
      const [debug, setDebug] = useState(null)
      const [doctor, setDoctor] = useState(null)
      const [wakePreview, setWakePreview] = useState('')
      const [busy, setBusy] = useState('')
      const [error, setError] = useState('')
      const [toast, setToast] = useState('')
      const defaults = loadPrefs()
      const [prefK, setPrefK] = useState(defaults.k)
      const [prefMode, setPrefMode] = useState(defaults.mode)
      const [bigTemplate, setBigTemplate] = useState(defaults.bigTemplate === true)
      const [bigLimit, setBigLimit] = useState(String(defaults.bigLimit ?? 0))
      // 夜间用真模型思考（鲸眠）：落 PMem 配置 sleep.whale.enabled，默认关。
      const [nightLlm, setNightLlm] = useState(false)
      const [nightBusy, setNightBusy] = useState(false)
      // 危险区（真路由）：备份列表/生成/下载、向量重建（可取消）、运维动作白名单。
      const [backups, setBackups] = useState(null)
      const [backupBusy, setBackupBusy] = useState('')
      const [backupKind, setBackupKind] = useState('manual')
      const [vectorStatus, setVectorStatus] = useState(null)
      const [vectorBusy, setVectorBusy] = useState('')
      const [vectorMessage, setVectorMessage] = useState('')
      const [actionResult, setActionResult] = useState(null)
      const [actionBusy, setActionBusy] = useState('')
      const [actionConfirm, setActionConfirm] = useState('')
      const vectorAbortRef = useRef(null)

      const say = useCallback(message => {
        setToast(message)
        notify?.(message)
      }, [notify])

      useEffect(() => {
        let alive = true
        api('/status')
          .then(data => { if (alive) setStatus(data) })
          .catch(() => {})
        return () => { alive = false }
      }, [])

      // 夜间开关：进设置页就回显后端当前值（默认关；读不到就保持关）。
      useEffect(() => {
        let alive = true
        api('/night-llm')
          .then(data => { if (alive) setNightLlm(data?.enabled === true) })
          .catch(() => {})
        return () => { alive = false }
      }, [])

      const runtime = status?.runtime ?? {}
      const locked = runtime.locked ?? {}
      const db = status?.db ?? {}
      const intSpecs = Object.entries(runtime.spec ?? {}).filter(([, spec]) => spec?.type === 'int')

      const saveRuntime = async patch => {
        setBusy('config')
        setError('')
        try {
          const data = await api('/config', { method: 'POST', body: JSON.stringify(patch) })
          setStatus(prev => (prev ? { ...prev, runtime: data.runtime } : prev))
          say('已保存（下一轮生效）')
        } catch (cause) {
          setError(`保存失败：${cause.message}`)
        } finally {
          setBusy('')
        }
      }

      // 夜间开关不写 runtime.json，走 PMem 配置；失败要退回原值，别让界面显示成已开。
      const saveNightLlm = async value => {
        const previous = nightLlm
        setNightLlm(value)
        setNightBusy(true)
        setError('')
        try {
          const data = await api('/night-llm', {
            method: 'POST',
            body: JSON.stringify({ enabled: value }),
          })
          setNightLlm(data?.enabled === true)
          say('已保存（下一晚生效）')
        } catch (cause) {
          setNightLlm(previous)
          setError(`夜间开关保存失败：${cause.message}`)
        } finally {
          setNightBusy(false)
        }
      }

      const loadDebug = async () => {
        setBusy('debug')
        setError('')
        try {
          setDebug(await api('/debug'))
        } catch (cause) {
          setError(`排查面板没取到：${cause.message}`)
        } finally {
          setBusy('')
        }
      }

      const loadDoctor = async () => {
        setBusy('doctor')
        setError('')
        try {
          setDoctor(await apiConsole('/doctor'))
        } catch (cause) {
          setError(`体检失败：${cause.message}`)
        } finally {
          setBusy('')
        }
      }

      const refreshWake = async () => {
        setBusy('wake')
        setError('')
        try {
          const data = await api('/wake?budget=300')
          setWakePreview(data?.text || '')
          say('醒来卡已刷新')
        } catch (cause) {
          setError(`刷新醒来卡失败：${cause.message}`)
        } finally {
          setBusy('')
        }
      }

      const copy = (command, message = '已复制命令') => void copyText(command, notify, message)

      const loadBackups = useCallback(async () => {
        setBackupBusy('list')
        try {
          setBackups(await apiConsole('/backup'))
        } catch (cause) {
          setBackups({ ok: false, error: cause.message, items: [] })
        } finally {
          setBackupBusy('')
        }
      }, [])

      useEffect(() => {
        void loadBackups()
      }, [loadBackups])

      // 离开页面/卸载时取消还没回来的向量重建请求（请求本身会被后端记录，但不阻塞界面）。
      useEffect(() => () => {
        try {
          vectorAbortRef.current?.abort()
        } catch (ignored) {
          // 已经结束
        }
      }, [])

      const createBackup = async () => {
        setBackupBusy('create')
        setError('')
        try {
          const data = await apiConsole('/backup', {
            method: 'POST',
            body: JSON.stringify({ kind: backupKind }),
          })
          const file = data?.file ?? null
          say('备份已生成' + (file?.name ? '：' + file.name : '') + (file?.verified ? '（校验通过）' : ''))
          await loadBackups()
        } catch (cause) {
          setError('生成备份失败：' + cause.message)
        } finally {
          setBackupBusy('')
        }
      }

      const previewVector = async () => {
        setVectorBusy('preview')
        setVectorMessage('')
        setError('')
        try {
          const data = await apiConsole('/vector/rebuild', {
            method: 'POST',
            body: JSON.stringify({ dry_run: true }),
          })
          setVectorStatus(data?.status ?? null)
          setVectorMessage('已读取向量通道状态（未重建）')
        } catch (cause) {
          setError('向量状态读取失败：' + cause.message)
        } finally {
          setVectorBusy('')
        }
      }

      const rebuildVector = async () => {
        setVectorBusy('rebuild')
        setVectorMessage('重建中：最长 10 分钟。可以离开页面，稍后回来点「刷新」看结果；也可以点「取消」。')
        setError('')
        try {
          const controller = new AbortController()
          vectorAbortRef.current = controller
          const data = await apiConsole('/vector/rebuild', {
            method: 'POST',
            body: JSON.stringify({ dry_run: false }),
            signal: controller.signal,
          })
          setVectorStatus(data?.result ?? data?.status ?? data)
          setVectorMessage('重建完成')
          say('向量索引重建完成')
        } catch (cause) {
          if (cause?.name === 'AbortError') setVectorMessage('已取消（服务端可能仍在跑，稍后刷新状态）')
          else setError('向量重建失败：' + cause.message)
        } finally {
          vectorAbortRef.current = null
          setVectorBusy('')
        }
      }

      const cancelVector = () => {
        try {
          vectorAbortRef.current?.abort()
        } catch (ignored) {
          // 已经结束
        }
      }

      // 运维动作白名单：doctor / vacuum / evolve / net-extract；先 dry_run 预览，再二次确认执行。
      const previewAction = async name => {
        setActionBusy(name)
        setError('')
        setActionResult(null)
        try {
          const data = await apiConsole('/action', {
            method: 'POST',
            body: JSON.stringify({ name, dry_run: name !== 'doctor' }),
          })
          setActionResult(data)
          setActionConfirm(name === 'doctor' ? '' : name)
        } catch (cause) {
          setError('运维动作预览失败：' + cause.message)
        } finally {
          setActionBusy('')
        }
      }

      const runAction = async (name, extra) => {
        setActionBusy(name)
        setError('')
        setActionConfirm('')
        try {
          setActionResult(await apiConsole('/action', {
            method: 'POST',
            body: JSON.stringify({ name, ...(extra ?? {}) }),
          }))
          say('运维动作完成：' + name)
          if (name === 'vacuum') await loadBackups()
        } catch (cause) {
          setError('运维动作失败：' + cause.message)
        } finally {
          setActionBusy('')
        }
      }

      // 配置差异：profile 侧显式值（/debug.config）vs 运行时覆盖值（/status.runtime）。
      const configKeys = ['surface', 'budget', 'surfaceCooldownMs', 'surfaceTimeoutMs', 'handoffTurns', 'handoffMinMs']
      const configDiff = debug?.config
        ? configKeys
            .filter(key => runtime[key] !== undefined && debug.config[key] !== undefined && runtime[key] !== debug.config[key])
            .map(key => ({ key, profile: debug.config[key], runtime: runtime[key] }))
        : []
      const recentRequests = debug && Array.isArray(debug.requests) ? debug.requests.slice(-20).reverse() : []
      const doctorText = formatDoctor(doctor)

      return h(
        'div',
        { className: 'pconsole-settings' },
        error ? h('p', { className: 'pconsole-banner', role: 'alert' }, error) : null,

        h(
          Panel,
          { title: '运行时开关', note: '写 ~/.dsh/pmem/runtime.json；保存后下一轮生效，不用重启。' },
          h('div', { className: 'pconsole-kv' },
            h('span', null, '每轮浮现'),
            h('span', null, h(Switch, {
              checked: runtime.surface !== false,
              disabled: locked.surface || busy === 'config',
              onChange: value => void saveRuntime({ surface: value }),
              label: runtime.surface === false ? '关' : '开',
            }))),
          h('div', { className: 'pconsole-kv' },
            h('span', null, '上下文自动交接'),
            h('span', null, h(Switch, {
              checked: runtime.contextAuto !== false,
              disabled: busy === 'config',
              onChange: value => void saveRuntime({ contextAuto: value }),
              label: runtime.contextAuto === false ? '关' : '开',
            }))),
          h('div', { className: 'pconsole-kv' },
            h('span', null, '星图'),
            h('span', null, h(Switch, {
              checked: runtime.starmap !== false,
              disabled: busy === 'config',
              onChange: value => void saveRuntime({ starmap: value }),
              label: runtime.starmap === false ? '关' : '开',
            }))),
          h('div', { className: 'pconsole-kv' },
            h('span', null, '星图实时'),
            h('span', null, h(Switch, {
              checked: runtime.starmapLive !== false,
              disabled: busy === 'config',
              onChange: value => void saveRuntime({ starmapLive: value }),
              label: runtime.starmapLive === false ? '实时关' : '实时开',
            }))),
        ),

        h(
          Panel,
          { title: '数值', note: '范围与单位来自后端 spec；保存即生效。' },
          intSpecs.length
            ? h('div', null,
                ...intSpecs.map(([key, spec]) =>
                  h('div', { key, className: 'pconsole-kv' },
                    h('span', null, spec.label || key),
                    h('span', null, h(NumberField, {
                      spec,
                      value: runtime[key],
                      disabled: busy === 'config',
                      onSave: value => void saveRuntime({ [key]: value }),
                    })))))
            : h('p', { className: 'pconsole-note' }, '后端没给 spec，暂时没有可调数值。'),
        ),

        h(
          Panel,
          {
            title: '检索与夜间默认值',
            note: '只存 localStorage（前端默认值），不写运行时配置；下次打开记忆页/大睡时生效。',
          },
          h('div', { className: 'pconsole-toolbar', style: { margin: 0 } },
            h('label', { className: 'pconsole-field' },
              h('span', null, '默认 k'),
              h('input', {
                className: 'pconsole-input',
                type: 'number',
                min: 1,
                max: 50,
                style: { width: '80px' },
                'aria-label': '默认检索 k',
                value: prefK,
                onChange: event => {
                  const value = Number(event.target.value) || 20
                  setPrefK(value)
                  savePrefs({ k: Math.min(50, Math.max(1, value)) })
                },
              })),
            h('label', { className: 'pconsole-field' },
              h('span', null, '默认 mode'),
              h('select', {
                className: 'pconsole-select',
                value: prefMode,
                'aria-label': '默认检索模式',
                onChange: event => {
                  setPrefMode(event.target.value)
                  savePrefs({ mode: event.target.value })
                },
              }, ...MEMORY_MODES.map(([value, label]) => h('option', { key: value, value }, label)))),
            h('label', { className: 'pconsole-field' },
              h('input', {
                type: 'checkbox',
                className: 'pconsole-check',
                checked: bigTemplate,
                onChange: event => {
                  setBigTemplate(event.target.checked)
                  savePrefs({ bigTemplate: event.target.checked })
                },
              }), '大睡默认模板模式'),
            h('label', { className: 'pconsole-field' },
              h('span', null, '大睡默认上限'),
              h('input', {
                className: 'pconsole-input',
                type: 'number',
                min: 0,
                style: { width: '80px' },
                'aria-label': '大睡默认最多几天',
                value: bigLimit,
                onChange: event => {
                  setBigLimit(event.target.value)
                  savePrefs({ bigLimit: Number(event.target.value) || 0 })
                },
              }),
              h('span', null, '天')),
          ),
        ),

        // ── 夜间：用真模型思考（鲸眠）。默认关；写 PMem 配置，改完下一晚生效。──
        h(
          Panel,
          {
            title: '夜间',
            note: '默认关。决定夜里谁把当天原文写成日记：本地模板，还是 DSH 会话（模型是我）。改完下一晚生效。',
          },
          h('div', { className: 'pconsole-kv' },
            h('span', null, '夜间用真模型思考（鲸眠）'),
            h('span', null, h(Switch, {
              checked: nightLlm,
              disabled: nightBusy,
              onChange: value => void saveNightLlm(value),
              label: nightLlm ? '开' : '关',
            }))),
          h('p', { className: 'pconsole-note', style: { margin: '4px 0 0' } },
            '关 = 夜间只跑本地模板日记 + 8 阶段（0 模型调用、0 成本）；'
            + '开 = 夜间由 DSH 会话（模型是我）把当天原文想成日记，用宿主额度，不需要 API key。'
            + '默认关，改完下一晚生效。'),
        ),

        // ── 排查面板 ──────────────────────────────────────────────────────────
        h(
          Panel,
          {
            title: '排查面板',
            actions: [
              h('button', {
                key: 'debug',
                className: 'pconsole-btn',
                type: 'button',
                disabled: busy === 'debug',
                onClick: () => void loadDebug(),
              }, busy === 'debug' ? '读取中…' : '拉取请求日志 / 配置'),
              h('button', {
                key: 'doctor',
                className: 'pconsole-btn',
                type: 'button',
                disabled: busy === 'doctor',
                onClick: () => void loadDoctor(),
              }, busy === 'doctor' ? '体检中…' : 'doctor 体检'),
              h('button', {
                key: 'wake',
                className: 'pconsole-btn',
                type: 'button',
                disabled: busy === 'wake',
                onClick: () => void refreshWake(),
              }, busy === 'wake' ? '刷新中…' : '刷新醒来卡缓存'),
            ],
          },

          h('p', { className: 'pconsole-subhead' }, 'MCP / 工具'),
          h('div', { className: 'pconsole-kv' },
            h('span', null, 'MCP server'),
            h('span', null, h('span', { className: 'pconsole-chip' }, status?.mcp?.serverName ?? 'pmem'))),
          h('div', { className: 'pconsole-kv' },
            h('span', null, 'MCP 工具数'),
            h('span', null, `${status?.mcp?.tools ?? 27} 个`)),
          h('div', { className: 'pconsole-kv' },
            h('span', null, '原生工具'),
            h('span', null, '5 个（pmem_wake / recall / remember / soul / stats）')),
          h('p', { className: 'pconsole-note', style: { margin: '4px 0 0' } },
            'MCP 探活由宿主管理；连接异常会体现在下面的 4xx/5xx 请求里（也可以在终端跑 /pmem mcp）。'),

          h('p', { className: 'pconsole-subhead' }, '数据库 / 路径 / 完整性'),
          h('div', { className: 'pconsole-kv' },
            h('span', null, '数据库'),
            h('span', null, debug?.config?.db || '~/.dsh/pmem/data/pmem.db')),
          h('div', { className: 'pconsole-kv' },
            h('span', null, '日志文件'),
            h('span', null, debug?.logFile || '—')),
          h('div', { className: 'pconsole-kv' },
            h('span', null, '体积'),
            h('span', null, fmtBytes(db.db_size_bytes))),
          h('div', { className: 'pconsole-kv' },
            h('span', null, 'schema / 完整性'),
            h('span', null, `${db.schema_version ?? '—'} · `,
              h('span', { className: db.integrity_ok ? 'pconsole-ok' : 'pconsole-error' },
                db.integrity_ok ? 'ok' : '异常'))),
          h('p', { className: 'pconsole-note', style: { margin: '4px 0 0' } },
            '「路径体检」：上面这些路径读不到时，/status 与 /console/bootstrap 会走降级（总览里会标 degraded）；'
            + '需要逐条确认可以跑 doctor。'),

          h('p', { className: 'pconsole-subhead' }, '配置差异（profile 默认 vs runtime 覆盖）'),
          configDiff.length
            ? h('table', { className: 'pconsole-table' },
                h('thead', null, h('tr', null, h('th', null, '键'), h('th', null, 'profile'), h('th', null, 'runtime'))),
                h('tbody', null, ...configDiff.map(entry =>
                  h('tr', { key: entry.key },
                    h('td', null, entry.key),
                    h('td', null, String(entry.profile)),
                    h('td', null, String(entry.runtime))))))
            : h('p', { className: 'pconsole-note', style: { margin: 0 } },
                debug ? '没有差异（或都还没显式覆盖）。' : '点「拉取请求日志 / 配置」后显示。'),

          h('p', { className: 'pconsole-subhead' }, '最近 20 条请求'),
          recentRequests.length
            ? h('table', { className: 'pconsole-table' },
                h('thead', null, h('tr', null,
                  h('th', null, '时间'), h('th', null, '方法'), h('th', null, '路径'),
                  h('th', null, '状态'), h('th', null, '耗时'))),
                h('tbody', null, ...recentRequests.map((item, index) =>
                  h('tr', { key: `req-${index}` },
                    h('td', null, fmtClock(item?.ts) || '—'),
                    h('td', null, item?.method || '—'),
                    h('td', null, clampText(item?.path, 70)),
                    h('td', {
                      className: Number(item?.status) >= 500
                        ? 'pconsole-error'
                        : Number(item?.status) >= 400 ? 'pconsole-warn' : null,
                    }, String(item?.status ?? '—')),
                    h('td', null, item?.ms !== undefined ? `${item.ms}ms` : '—')))))
            : h('p', { className: 'pconsole-note', style: { margin: 0 } },
                debug ? '还没有记录。' : '点「拉取请求日志 / 配置」后显示。'),

          wakePreview
            ? h('pre', { className: 'pconsole-pre', style: { marginTop: '8px' } }, wakePreview)
            : null,
          doctor
            ? h('pre', { className: 'pconsole-pre', style: { marginTop: '8px' } }, doctorText)
            : null,
        ),

        // ── 危险区：真路由（备份 / 向量重建 / 运维动作），折叠 + 二次确认 ──────
        h(
          'details',
          { className: 'pconsole-danger', style: { marginTop: '16px' } },
          h('summary', null, '危险区（折叠）· 备份 / 向量重建 / 运维动作'),
          h('p', { className: 'pconsole-note' },
            '这些是真写入动作：备份生成到 CLI 默认备份目录（后端返回的 dir），向量重建最长 10 分钟，'
            + '运维动作走白名单。全部二次确认；不提供新增/休眠关系。'),

          h('p', { className: 'pconsole-subhead' }, '数据库备份'),
          h('div', { className: 'pconsole-actions', style: { margin: 0 } },
            h('label', { className: 'pconsole-field' },
              h('span', null, 'kind'),
              h('select', {
                className: 'pconsole-select',
                value: backupKind,
                'aria-label': '备份类型',
                onChange: event => setBackupKind(event.target.value),
              },
                // 后端值就是 manual / daily / pre-migrate，别自己造 kind。
                h('option', { value: 'manual' }, 'manual（手动）'),
                h('option', { value: 'daily' }, 'daily（每日）'),
                h('option', { value: 'pre-migrate' }, 'pre-migrate（迁移前）'))),
            h('button', {
              className: 'pconsole-btn pconsole-btn-primary',
              type: 'button',
              disabled: backupBusy === 'create',
              onClick: () => void createBackup(),
            }, backupBusy === 'create' ? '生成中…' : '生成备份'),
            h('button', {
              className: 'pconsole-btn',
              type: 'button',
              disabled: backupBusy === 'list',
              onClick: () => void loadBackups(),
            }, backupBusy === 'list' ? '读取中…' : '刷新列表')),
          backups === null
            ? h('p', { className: 'pconsole-note', style: { margin: '4px 0 0' } }, '读取备份列表…')
            : backups?.ok === false
              ? h('p', { className: 'pconsole-error' }, '备份列表没取到：' + backups.error)
              : h('div', null,
                  h('p', { className: 'pconsole-note', style: { margin: '4px 0 0' } },
                    '备份目录：',
                    h('code', { className: 'pconsole-code' }, backups?.dir || '—'),
                    backups?.counts
                      ? ' · 共 ' + (backups.counts.total ?? 0) + ' 个 / ' + fmtBytes(backups.counts.total_bytes ?? 0)
                      : ''),
                  (backups?.items ?? []).length
                    ? h('div', { style: { marginTop: '4px' } },
                        ...(backups.items ?? []).map(file =>
                          h('div', { key: file?.name ?? Math.random(), className: 'pconsole-backup-row' },
                            h('div', { className: 'pconsole-backup-meta' },
                              h('span', { className: 'pconsole-code' }, file?.name ?? '—'),
                              h('span', null, fmtBytes(file?.size_bytes)),
                              h('span', null, file?.kind || '—'),
                              file?.memory_count !== undefined ? h('span', null, '记忆 ' + file.memory_count) : null,
                              file?.modified ? h('span', null, timeAgo(file.modified)) : null),
                            h('button', {
                              className: 'pconsole-btn',
                              type: 'button',
                              disabled: !file?.name,
                              onClick: () => void downloadBlob(
                                CONSOLE + '/backup/file?name=' + encodeURIComponent(file.name),
                                file.name,
                                notify,
                              ),
                            }, '下载'))))
                    : h('p', { className: 'pconsole-note', style: { margin: '4px 0 0' } },
                        '还没有备份。点「生成备份」会调 pmem backup。')),

          h('p', { className: 'pconsole-subhead' }, '重建向量索引（最长 10 分钟）'),
          h('div', { className: 'pconsole-actions', style: { margin: 0 } },
            h('button', {
              className: 'pconsole-btn',
              type: 'button',
              disabled: Boolean(vectorBusy),
              onClick: () => void previewVector(),
            }, vectorBusy === 'preview' ? '读取中…' : '查看向量状态'),
            h('button', {
              className: 'pconsole-btn pconsole-btn-danger',
              type: 'button',
              disabled: Boolean(vectorBusy),
              onClick: () => void rebuildVector(),
            }, vectorBusy === 'rebuild' ? '重建中…' : '重建索引'),
            vectorBusy === 'rebuild'
              ? h('button', {
                  className: 'pconsole-btn',
                  type: 'button',
                  onClick: cancelVector,
                }, '取消')
              : null),
          vectorMessage
            ? h('p', { className: 'pconsole-note', role: 'status' }, vectorMessage)
            : null,
          vectorStatus
            ? h('pre', { className: 'pconsole-pre', style: { marginTop: '6px' } }, pretty(vectorStatus))
            : null,

          h('p', { className: 'pconsole-subhead' }, '运维动作（白名单）'),
          h('div', { className: 'pconsole-actions', style: { margin: 0 } },
            ...['doctor', 'vacuum', 'evolve', 'net-extract'].map(name =>
              h('button', {
                key: name,
                className: 'pconsole-btn',
                type: 'button',
                disabled: Boolean(actionBusy),
                onClick: () => void previewAction(name),
              }, actionBusy === name ? name + ' 进行中…' : '预览 ' + name))),
          actionConfirm
            ? h('div', { className: 'pconsole-confirm', role: 'alertdialog', 'aria-label': '确认运维动作' },
                h('p', null, '确认执行 ' + actionConfirm + '？这是真写入/维护动作。'),
                h('div', { className: 'pconsole-actions' },
                  h('button', {
                    className: 'pconsole-btn pconsole-btn-danger',
                    type: 'button',
                    disabled: Boolean(actionBusy),
                    onClick: () => void runAction(actionConfirm),
                  }, '确认执行'),
                  h('button', {
                    className: 'pconsole-btn',
                    type: 'button',
                    onClick: () => setActionConfirm(''),
                  }, '取消')))
            : null,
          actionResult
            ? h('pre', { className: 'pconsole-pre', style: { marginTop: '6px' } }, pretty(actionResult))
            : null,
        ),

        h(
          Panel,
          {
            title: '关于',
            note: '控制台是只读 + 管理动作的集合；不引入第二份配置，不新增依赖。',
          },
          h('div', { className: 'pconsole-kv' },
            h('span', null, '版本'),
            h('span', null, status?.version ?? boot?.version ?? '—')),
          h('div', { className: 'pconsole-kv' },
            h('span', null, '数据规模'),
            h('span', null, `${countText(pickNumber(db.memory_total, boot?.counts?.memory_total))} 条 · ${fmtBytes(db.db_size_bytes)}`)),
          h('div', { className: 'pconsole-kv' },
            h('span', null, 'MCP 工具'),
            h('span', null, '27 个（含 memory_* / export / import / stats）')),
          h('p', { className: 'pconsole-subhead' }, '命令行速查'),
          h('div', { className: 'pconsole-actions', style: { margin: 0 } },
            h('button', { className: 'pconsole-btn', type: 'button', onClick: () => copy('pmem status --json') }, 'pmem status'),
            h('button', { className: 'pconsole-btn', type: 'button', onClick: () => copy('pmem doctor') }, 'pmem doctor'),
            h('button', { className: 'pconsole-btn', type: 'button', onClick: () => copy('pmem search <词> --json --k 10') }, 'pmem search'),
            h('button', { className: 'pconsole-btn', type: 'button', onClick: () => copy('pmem wake --budget 300') }, 'pmem wake'),
            h('button', { className: 'pconsole-btn', type: 'button', onClick: () => copy('pmem star --json') }, 'pmem star')),
        ),
      )
    }

    // ── 插件注册 ──────────────────────────────────────────────────────────────
    const inject = ['slots', 'layout']

    // 打开整页控制台：只切面板（桌面壳不为插件开新窗口；整页导航会丢登录态）。
    function openConsolePanel(ctx) {
      try {
        ctx?.layout?.selectPanel?.(PANEL_ID)
      } catch (ignored) {
        // layout 服务缺席时不炸，用户还能走侧边栏入口
      }
    }

    function apply(ctx) {
      const effect = typeof ctx.effect === 'function' ? ctx.effect.bind(ctx) : fn => fn()
      // 1) 设置页入口：旧七个标签已删，只剩一行「打开控制台」。
      effect(
        () =>
          ctx.slots.inject('settings.section', () =>
            ctx.slots.register(
              { name: 'settings.section', id: NS, order: 8, label: () => '可塑性记忆' },
              props => h(PMemSection, {
                ...props,
                onOpenConsole: () => {
                  openConsolePanel(ctx)
                  // 宿主把 settings.section 的 close 作为 prop 传进来：切面板后要关掉设置 dialog，
                  // 否则遮罩会压在新面板上（真实 GUI 验收发现的）。
                  try {
                    props?.close?.()
                  } catch (ignored) {
                    // close 缺席不阻塞切面板
                  }
                },
              }),
            ), 'dsh-pmem: settings section'),
        'dsh-pmem: settings section',
      )
      // 2) 左侧导航一行：「记忆控制台」+ 16px 内联 SVG。
      effect(
        () =>
          ctx.slots.inject('sidebar.panellist', () =>
            ctx.slots.register(
              { name: 'sidebar.panellist', id: PANEL_ID, order: 1, label: () => '记忆控制台', locale: NS },
              ConsolePanelIcon,
            ), 'dsh-pmem: sidebar panellist'),
        'dsh-pmem: sidebar panellist',
      )
      // 3) 整页面板：root 作用域 main 的 keyed slot，key 必须等于侧边栏 id。
      effect(
        () =>
          ctx.slots.inject('main', () =>
            ctx.slots.register(
              { name: 'main', key: PANEL_ID, locale: NS },
              ConsolePage,
            ), 'dsh-pmem: console panel'),
        'dsh-pmem: console panel',
      )
    }

    exports.apply = apply
    exports.inject = inject
    exports.NS = NS
    exports.PANEL_ID = PANEL_ID
    exports.CONSOLE = CONSOLE
    exports.CONSOLE_NAV = CONSOLE_NAV
    exports.PMemSection = PMemSection
    exports.ConsolePage = ConsolePage
    exports.ConsolePanelIcon = ConsolePanelIcon
    exports.ConsoleOverview = ConsoleOverview
    exports.ConsoleWake = ConsoleWake
    exports.ConsoleMemory = ConsoleMemory
    exports.ConsoleEntities = ConsoleEntities
    exports.ConsoleSleep = ConsoleSleep
    exports.ConsoleSettings = ConsoleSettings
    exports.openConsolePanel = openConsolePanel
    exports.highlightParts = highlightParts
    exports.stageSummary = stageSummary
    exports.API = API
    exports.STARMAP = STARMAP
    return module.exports
  },
})
