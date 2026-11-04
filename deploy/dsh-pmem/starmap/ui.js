/*!
 * PMem · 星图 · 界面层  (M9.7b)
 *
 * 只做三件事：把渲染器挂上去、把库里的东西讲清楚、把镜头交给人。
 * 依赖 space.js 暴露的 window.PMemStar。
 */
(function () {
  'use strict';

  var API = '/dsh-pmem/api';
  var R = window.PMemStar;
  if (!R) return;

  var EMBEDDED = window.__PMEM_DATA__ || null;
  // 独立浏览器通道：插件自带的小服务会在页头注入这份配置。
  // 三种形态共用同一份渲染器与界面层，只是数据从哪来不一样。
  var EMBED = window.__PMEM_EMBED__ || null;
  var STANDALONE = Boolean(EMBED && EMBED.standalone);
  var DATA_URL = EMBEDDED ? null : ((EMBED && EMBED.dataUrl) || (API + '/starmap'));
  var EVENTS_URL = EMBEDED_EVENTS();
  function EMBEDED_EVENTS() {
    if (EMBEDDED) return null;
    if (EMBED && Object.prototype.hasOwnProperty.call(EMBED, 'eventsUrl')) return EMBED.eventsUrl;
    return API + '/starmap/events';
  }
  var IN_FRAME = false;
  try { IN_FRAME = window.self !== window.top; } catch (e) { IN_FRAME = true; }

  // ------------------------------------------------------------------ 工具

  function h(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function fmtTime(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d)) return '—';
    var p = function (v) { return v < 10 ? '0' + v : String(v); };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function fmtDateShort(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d)) return '—';
    var p = function (v) { return v < 10 ? '0' + v : String(v); };
    return p(d.getMonth() + 1) + '-' + p(d.getDate());
  }
  function num(v) {
    if (v === null || v === undefined) return '—';
    return String(v).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }
  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
  // 颜色表是 0~1 的三元组（和 WebGL 共用），DOM 里要的是 RGB 字符串。
  function rgbOf(c) {
    return 'rgb(' + Math.round(c[0] * 255) + ',' + Math.round(c[1] * 255) + ',' + Math.round(c[2] * 255) + ')';
  }

  var GRADE_TEXT = { S: '核心', A: '长期', B: '有用', C: '情境', D: '琐碎' };
  var KIND_TEXT = {
    episodic: '经历', semantic: '事实', person: '人', relation: '关系',
    experience: '经验', goal: '目标', meta: '元'
  };
  // 来源是独立维度：journal = 由当天日记派生的记忆（琥珀色）。
  var SOURCE_TEXT = { journal: '日记派生' };
  var TYPE_TEXT = { memory: '记忆', entity: '实体', group: '分组', trait: '特质', core: '核心' };
  var DUST_TEXT = { event: '事件', recall: '回想', metric: '指标', reflect: '反思', sleep: '睡眠' };
  var STATUS_TEXT = {
    active: '活跃', dormant: '休眠', archived: '归档', deleted: '已删除',
    hypothesis: '假设', established: '确立', contested: '争议'
  };

  // ------------------------------------------------------------------ 外壳

  var SHELL = [
    '<canvas class="sm-canvas" id="sm-canvas"></canvas>',
    '<div class="sm-labels" id="sm-labels"></div>',
    '<div class="sm-pulses" id="sm-pulses"></div>',
    '<header class="sm-top">',
    '  <div class="sm-brand">',
    '    <div class="sm-kicker">PMEM · 海马体星图</div>',
    '    <h1 class="sm-title" id="sm-title">记忆星图</h1>',
    '  </div>',
    '  <nav class="sm-actions" id="sm-actions"></nav>',
    '</header>',
    '<div class="sm-searchbox" id="sm-searchbox">',
    '  <input id="sm-q" type="search" placeholder="搜一条记忆、一个实体、一个分组" autocomplete="off" spellcheck="false">',
    '  <div class="sm-hits" id="sm-hits"></div>',
    '</div>',
    '<div class="sm-panel sm-filters" id="sm-filters"></div>',
    '<aside class="sm-panel sm-detail" id="sm-detail"></aside>',
    '<div class="sm-telemetry" id="sm-telemetry"></div>',
    '<footer class="sm-bottom">',
    '  <div class="sm-timeline" id="sm-timeline">',
    '    <button class="sm-play" id="sm-play" title="时间回放：看记忆一天天长出来">',
    '      <svg viewBox="0 0 12 12" width="10" height="10" aria-hidden="true"><path id="sm-play-icon" d="M2 1l9 5-9 5z" fill="currentColor"/></svg>',
    '    </button>',
    '    <div class="sm-track" id="sm-track">',
    '      <div class="sm-track-rail"></div>',
    '      <div class="sm-track-fill" id="sm-fill"></div>',
    '      <div class="sm-track-glow" id="sm-glow"></div>',
    '      <div class="sm-track-dot" id="sm-dot"></div>',
    '    </div>',
    '    <div class="sm-clock" id="sm-clock">全部</div>',
    '  </div>',
    '</footer>',
    '<div class="sm-tip" id="sm-tip"></div>',
    '<div class="sm-boot" id="sm-boot">',
    '  <div class="sm-boot-ring"><i></i><i></i><i></i></div>',
    '  <div class="sm-boot-text">正在点亮星图</div>',
    '</div>',
    '<div class="sm-error" id="sm-error"></div>'
  ].join('');

  var APP = null;
  var DATA = null;
  var frameHook = null;
  var els = {};
  var labels = [];
  var hits = [];
  var pulseItems = [];
  var gridBuilt = false;

  function $(id) { return document.getElementById(id); }

  function buildShell() {
    var host = document.getElementById('sm-root') || document.body;
    host.classList.add('sm-shell');
    host.innerHTML = SHELL;
    ['canvas', 'labels', 'pulses', 'title', 'actions', 'searchbox', 'q', 'hits',
      'filters', 'detail', 'telemetry', 'timeline', 'play', 'track', 'fill', 'glow',
      'dot', 'clock', 'tip', 'boot', 'error'].forEach(function (key) {
      els[key] = $('sm-' + key);
    });
    els.root = host;
  }

  function showError(text) {
    if (!els.error) return;
    els.error.textContent = text || '';
    els.error.classList.toggle('is-on', Boolean(text));
  }

  function bootDone() {
    if (!els.boot) return;
    els.boot.classList.add('is-done');
    els.root.classList.add('is-ready');
  }

  // ------------------------------------------------------------------ 顶栏

  function icon(name) {
    var paths = {
      search: '<circle cx="7" cy="7" r="4.4" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M10.4 10.4L14 14" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
      filter: '<path d="M2 3.6h12M4 7.6h8M6 11.6h4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
      refresh: '<path d="M12.6 7a5.6 5.6 0 1 1-1.7-4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/><path d="M13.4 1.6v3.6H9.8" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>',
      download: '<path d="M7 2.2v8.2M3.8 7.4L7 10.6l3.2-3.2M2.4 12.4h9.2" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>',
      full: '<path d="M2.6 5.4V2.6h2.8M11.4 2.6h2.8v2.8M14.2 11.4v2.8h-2.8M5.4 14.2H2.6v-2.8" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
      close: '<path d="M3.4 3.4l7.2 7.2M10.6 3.4l-7.2 7.2" stroke="currentColor" stroke-width="1.35" stroke-linecap="round"/>',
      reset: '<circle cx="7" cy="7" r="4.6" fill="none" stroke="currentColor" stroke-width="1.3"/><circle cx="7" cy="7" r="1.3" fill="currentColor"/>',
      eye: '<path d="M1 7s2.2-4 6-4 6 4 6 4-2.2 4-6 4-6-4-6-4z" fill="none" stroke="currentColor" stroke-width="1.25"/><circle cx="7" cy="7" r="1.7" fill="currentColor"/>',
      eyeOff: '<path d="M1 7s2.2-4 6-4 6 4 6 4-2.2 4-6 4-6-4-6-4z" fill="none" stroke="currentColor" stroke-width="1.25" opacity="0.45"/><path d="M2.2 2.2l9.6 9.6" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>'
    };
    return '<svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true">' + (paths[name] || '') + '</svg>';
  }

  function btn(name, label, title) {
    var b = h('button', 'sm-btn sm-btn-' + name);
    b.type = 'button';
    if (title) b.title = title;
    b.innerHTML = icon(name) + (label ? '<span>' + label + '</span>' : '');
    return b;
  }

  function buildActions() {
    var box = els.actions;
    box.innerHTML = '';

    if (!EMBEDDED && !STANDALONE) {
      var live = h('button', 'sm-btn sm-live');
      live.type = 'button';
      live.title = '新记忆写进库就重绘，不刷新页面';
      live.appendChild(h('i', 'sm-live-dot'));
      live.appendChild(h('span', null, '实时'));
      live.addEventListener('click', toggleLive);
      els.live = live;
      box.appendChild(live);
    }

    var search = btn('search', '搜索', '在星图里找到一条记忆');
    search.addEventListener('click', function () { togglePanel(els.searchbox, search); });
    box.appendChild(search);

    var names = btn('eye', '名称·简', '记忆名：点击切换 简 / 全 / 关');
    names.addEventListener('click', cycleLabels);
    els.labelBtn = names;
    syncLabelButton(labelMode());
    box.appendChild(names);

    var filter = btn('filter', '筛选', '按等级 / 类型 / 分组看骨架');
    filter.addEventListener('click', function () { togglePanel(els.filters, filter); });
    box.appendChild(filter);

    var reset = btn('reset', null, '回到全景 (R)');
    reset.addEventListener('click', function () { APP && APP.resetView(); closeAll(); });
    box.appendChild(reset);

    var full = btn('full', null, '全屏 (F)');
    full.addEventListener('click', toggleFullscreen);
    box.appendChild(full);

    if (!EMBEDDED && !STANDALONE) {
      var regen = btn('refresh', null, '按当前库重新算一次星图');
      regen.addEventListener('click', regenerate);
      box.appendChild(regen);
    } else if (STANDALONE) {
      // 独立页不负责重算（那是 DSH 那边的事），但可以立刻拉一次最新数据。
      var pull = btn('refresh', null, '立刻拉一次最新星图');
      pull.addEventListener('click', function () {
        loadData().catch(function (e) { showError('拉取失败：' + e.message); });
      });
      box.appendChild(pull);
    }

    var down = btn('download', null, '导出一张星图');
    down.addEventListener('click', exportImage);
    box.appendChild(down);

    if (IN_FRAME) {
      var close = btn('close', null, '收起星图 (Esc)');
      close.addEventListener('click', closeOverlay);
      box.appendChild(close);
    }
  }

  function togglePanel(panel, trigger) {
    var open = !panel.classList.contains('is-open');
    closeAll();
    if (open) {
      panel.classList.add('is-open');
      trigger.classList.add('is-active');
      if (panel === els.searchbox) setTimeout(function () { els.q.focus(); }, 30);
    }
  }

  function closeAll() {
    [els.searchbox, els.filters].forEach(function (p) {
      if (p) p.classList.remove('is-open');
    });
    var act = els.actions.querySelectorAll('.sm-btn');
    for (var i = 0; i < act.length; i++) act[i].classList.remove('is-active');
  }

  function toggleFullscreen() {
    var root = els.root;
    var doc = document;
    var full = doc.fullscreenElement || doc.webkitFullscreenElement;
    if (full) {
      (doc.exitFullscreen || doc.webkitExitFullscreen || function () {}).call(doc);
    } else {
      var req = root.requestFullscreen || root.webkitRequestFullscreen;
      if (req) {
        var p = req.call(root);
        if (p && p.catch) p.catch(function () { showError('这个窗口不允许全屏：试着在独立页面里打开星图'); });
      } else {
        showError('浏览器不支持全屏 API');
      }
    }
  }

  function closeOverlay() {
    try {
      window.parent.postMessage({ type: 'pmem-starmap-close' }, '*');
    } catch (e) { /* 跨源就算了 */ }
  }

  // ------------------------------------------------------------------ 搜索

  function buildHits(query) {
    var box = els.hits;
    box.innerHTML = '';
    hits = [];
    if (!DATA || !query) return;
    var q = query.toLowerCase();
    var scored = [];
    (DATA.nodes || []).forEach(function (n) {
      if (!n.label || n.type === 'core') return;
      var hay = (n.label + ' ' + (n.text || '') + ' ' + (n.path || '') + ' ' + (n.groups || []).join(' ')).toLowerCase();
      var idx = hay.indexOf(q);
      if (idx < 0) return;
      var score = idx === 0 ? 0 : (idx < 8 ? 1 : 2);
      if (n.type === 'group') score -= 1.4;
      if (n.grade === 'S') score -= 0.5;
      if (n.grade === 'A') score -= 0.3;
      scored.push({ n: n, s: score + idx * 0.004 });
    });
    scored.sort(function (a, b) { return a.s - b.s; });
    hits = scored.slice(0, 9);
    if (!hits.length) {
      box.appendChild(h('div', 'sm-hit sm-hit-empty', '没有匹配的星'));
      return;
    }
    hits.forEach(function (item) {
      var n = item.n;
      var row = h('button', 'sm-hit');
      row.type = 'button';
      var dot = h('i', 'sm-hit-dot');
      var c = n.color || [0.7, 0.8, 1];
      dot.style.background = 'rgb(' + Math.round(c[0] * 255) + ',' + Math.round(c[1] * 255) + ',' + Math.round(c[2] * 255) + ')';
      row.appendChild(dot);
      var main = h('div', 'sm-hit-main');
      main.appendChild(h('div', 'sm-hit-label', n.label));
      var meta = TYPE_TEXT[n.type] || n.type;
      if (n.type === 'memory') {
        meta = (n.grade ? (GRADE_TEXT[n.grade] || n.grade) + ' · ' : '') + (KIND_TEXT[n.kind] || n.kind || '');
      }
      if (n.day) meta += ' · ' + n.day;
      main.appendChild(h('div', 'sm-hit-meta', meta));
      row.appendChild(main);
      row.addEventListener('click', function () {
        select(n.id);
        APP.flyTo(n.id);
        closeAll();
      });
      box.appendChild(row);
    });
  }

  // ------------------------------------------------------------------ 名称

  // 全景下满屏文字是最快变廉价的方法，所以默认只给分组和实体；
  // 想全看、或者想全关，都在这一颗按钮上。
  var LABEL_KEY = 'pmem.starmap.labels';
  var LABEL_MODES = ['lite', 'full', 'off'];
  var LABEL_LABEL = { lite: '名称·简', full: '名称·全', off: '名称·关' };

  function labelMode() {
    try {
      var stored = window.localStorage.getItem(LABEL_KEY);
      if (LABEL_MODES.indexOf(stored) >= 0) return stored;
    } catch (e) { /* 隐私模式下会抛 */ }
    return 'lite';
  }

  function cycleLabels() {
    var next = LABEL_MODES[(LABEL_MODES.indexOf(labelMode()) + 1) % LABEL_MODES.length];
    try { window.localStorage.setItem(LABEL_KEY, next); } catch (e) { /* 存不了就算了 */ }
    syncLabelButton(next);
    buildLabels();
  }

  function syncLabelButton(mode) {
    if (!els.labelBtn) return;
    els.labelBtn.innerHTML = icon(mode === 'off' ? 'eyeOff' : 'eye') +
      '<span>' + LABEL_LABEL[mode] + '</span>';
    els.labelBtn.classList.toggle('is-active', mode === 'full');
    els.labelBtn.classList.toggle('is-muted', mode === 'off');
    els.labelBtn.title = '记忆名：' + (mode === 'off' ? '全关' : mode === 'full' ? '全部显示' : '只显示分组与实体') +
      '（点击切换）';
  }

  function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

  function animateNumber(el, target) {
    if (el._raf) cancelAnimationFrame(el._raf);
    el.textContent = num(target);
    if (target <= 0) return;
    var start = performance.now();
    function step(now) {
      var k = clamp((now - start) / 900, 0, 1);
      el.textContent = num(Math.round(target * easeOutCubic(k)));
      if (k < 1) el._raf = requestAnimationFrame(step);
      else el._raf = 0;
    }
    el._raf = requestAnimationFrame(step);
  }

  // ------------------------------------------------------------------ 筛选

  function buildFilters() {
    var box = els.filters;
    box.innerHTML = '';
    if (!DATA) return;

    var state = {
      grade: [], kind: [], type: [], arm: []
    };
    els.filterState = state;

    function section(title, items, key, colors) {
      if (!items.length) return;
      var wrap = h('div', 'sm-fgroup');
      wrap.appendChild(h('div', 'sm-fgroup-title', title));
      var chips = h('div', 'sm-fchips');
      items.forEach(function (item) {
        var chip = h('button', 'sm-chip');
        chip.type = 'button';
        var dot = h('i', 'sm-chip-dot');
        if (colors && colors[item.value]) {
          var c = colors[item.value];
          dot.style.background = 'rgb(' + Math.round(c[0] * 255) + ',' + Math.round(c[1] * 255) + ',' + Math.round(c[2] * 255) + ')';
        } else {
          dot.style.display = 'none';
        }
        chip.appendChild(dot);
        chip.appendChild(h('span', null, item.label));
        if (item.count !== undefined) chip.appendChild(h('em', null, item.count));
        chip.addEventListener('click', function () {
          var list = state[key];
          var at = list.indexOf(item.value);
          if (at >= 0) list.splice(at, 1); else list.push(item.value);
          chip.classList.toggle('is-on');
          applyFilters();
        });
        chips.appendChild(chip);
      });
      wrap.appendChild(chips);
      box.appendChild(wrap);
    }

    var counts = { grade: {}, kind: {}, type: {}, arm: {}, source: {} };
    (DATA.nodes || []).forEach(function (n) {
      if (n.type === 'core') return;
      counts.type[n.type] = (counts.type[n.type] || 0) + 1;
      if (n.type === 'memory') {
        var g = n.grade || 'none';
        counts.grade[g] = (counts.grade[g] || 0) + 1;
        if (n.kind) counts.kind[n.kind] = (counts.kind[n.kind] || 0) + 1;
        if (n.arm) counts.arm[n.arm] = (counts.arm[n.arm] || 0) + 1;
        // 老 JSON 没有 source_kind，当 undefined 跳过，不影响筛选和计数。
        if (n.source_kind) counts.source[n.source_kind] = (counts.source[n.source_kind] || 0) + 1;
      }
    });

    var gradeOrder = ['S', 'A', 'B', 'C', 'D', 'none'];
    section('等级', gradeOrder.filter(function (g) { return counts.grade[g]; }).map(function (g) {
      return { value: g, label: GRADE_TEXT[g] || '未分级', count: counts.grade[g] };
    }), 'grade', {
      S: R.GRADE_COLOR.S, A: R.GRADE_COLOR.A, B: R.GRADE_COLOR.B,
      C: R.GRADE_COLOR.C, D: R.GRADE_COLOR.D, none: [0.68, 0.76, 0.90]
    });

    var typeOrder = ['memory', 'entity', 'group', 'trait'];
    section('形态', typeOrder.filter(function (t) { return counts.type[t]; }).map(function (t) {
      return { value: t, label: TYPE_TEXT[t] || t, count: counts.type[t] };
    }), 'type', null);

    section('类型', Object.keys(counts.kind).map(function (k) {
      return { value: k, label: KIND_TEXT[k] || k, count: counts.kind[k] };
    }), 'kind', R.KIND_COLOR);

    var arms = Object.keys(counts.arm).sort();
    section('分组', arms.map(function (a) {
      return { value: a, label: a, count: counts.arm[a] };
    }), 'arm', null);

    // 图例：日记派生是「来源」维度，既不是等级也不是类型，
    // 所以单独一行、单独一枚琥珀色块，避免和等级色混为一谈。
    var legend = h('div', 'sm-flegend');
    var journalRow = h('div', 'sm-flegend-item');
    var journalDot = h('i', 'sm-flegend-dot');
    journalDot.style.background = rgbOf(R.JOURNAL_COLOR || [0.9137, 0.7216, 0.4471]);
    journalRow.appendChild(journalDot);
    journalRow.appendChild(h('span', null, SOURCE_TEXT.journal));
    if (counts.source.journal) journalRow.appendChild(h('em', null, counts.source.journal));
    legend.appendChild(journalRow);
    legend.appendChild(h('div', 'sm-flegend-hint',
      '半径 = 时间（早的在中心） · 角度 = 分组 · 亮度 = 等级与浮现'));
    box.appendChild(legend);
  }

  function applyFilters() {
    var s = els.filterState;
    if (!s) return;
    var any = s.grade.length || s.kind.length || s.type.length || s.arm.length;
    APP.setFilter(any ? {
      grade: s.grade, kind: s.kind, type: s.type, arm: s.arm
    } : null);
  }

  // ------------------------------------------------------------------ 详情

  function select(id) {
    APP.setSelect(id);
    var n = DATA && (DATA.nodes || []).filter(function (x) { return x.id === id; })[0];
    if (n) {
      renderDetailNode(n);
    }
  }

  function clearDetail() {
    APP.setSelect(null);
    els.detail.classList.remove('is-open');
  }

  function metaRow(label, value) {
    var row = h('div', 'sm-meta-row');
    row.appendChild(h('span', 'sm-meta-key', label));
    row.appendChild(h('span', 'sm-meta-val', value));
    return row;
  }

  function renderDetailNode(n) {
    var box = els.detail;
    box.innerHTML = '';
    box.classList.add('is-open');

    var close = h('button', 'sm-close');
    close.type = 'button';
    close.innerHTML = icon('close');
    close.addEventListener('click', clearDetail);
    box.appendChild(close);

    var kicker = h('div', 'sm-dkicker');
    var c = n.color || [0.8, 0.85, 1];
    kicker.style.color = 'rgb(' + Math.round(Math.min(1, c[0] * 1.05) * 255) + ',' +
      Math.round(Math.min(1, c[1] * 1.05) * 255) + ',' +
      Math.round(Math.min(1, c[2] * 1.05) * 255) + ')';
    var tag = TYPE_TEXT[n.type] || n.type;
    if (n.type === 'memory' && n.grade) tag += ' · ' + (GRADE_TEXT[n.grade] || n.grade);
    if (n.type === 'memory' && n.kind) tag += ' · ' + (KIND_TEXT[n.kind] || n.kind);
    if (n.type === 'memory' && n.source_kind === 'journal') tag += ' · ' + SOURCE_TEXT.journal;
    if (n.type === 'entity' && n.entity_type) tag += ' · ' + n.entity_type;
    kicker.textContent = tag;
    box.appendChild(kicker);

    box.appendChild(h('h2', 'sm-dtitle', n.label || '（无标题）'));

    var text = n.text || '';
    if (text && text !== n.label) {
      box.appendChild(h('p', 'sm-dtext', text));
    }

    var grid = h('div', 'sm-meta');
    if (n.day) grid.appendChild(metaRow('日期', n.day));
    if (n.created_at) grid.appendChild(metaRow('写入', fmtTime(n.created_at)));
    if (n.status) grid.appendChild(metaRow('状态', STATUS_TEXT[n.status] || n.status));
    if (n.type === 'memory') {
      grid.appendChild(metaRow('重要', Number(n.importance || 0).toFixed(2)));
      grid.appendChild(metaRow('置信', Number(n.confidence || 0).toFixed(2)));
      grid.appendChild(metaRow('浮现', (n.use_count || 0) + ' 次'));
      if (n.source_kind === 'journal') grid.appendChild(metaRow('来源', SOURCE_TEXT.journal));
    }
    if (n.type === 'entity' && n.mention_count) grid.appendChild(metaRow('提及', n.mention_count + ' 次'));
    if (n.type === 'trait' && n.strength) grid.appendChild(metaRow('强度', Number(n.strength).toFixed(2)));
    if (n.groups && n.groups.length) grid.appendChild(metaRow('分组', n.groups.join(' / ')));
    if (n.path) grid.appendChild(metaRow('路径', n.path));
    if (n.members) grid.appendChild(metaRow('条目', n.members + ' 项'));
    box.appendChild(grid);

    if (n.pinned) {
      var pin = h('div', 'sm-flag', '置顶');
      box.appendChild(pin);
    }

    var actions = h('div', 'sm-dactions');
    var locate = h('button', 'sm-dbtn', '镜头拉近');
    locate.type = 'button';
    locate.addEventListener('click', function () { APP.flyTo(n.id); });
    actions.appendChild(locate);
    if (n.type === 'group') {
      var isolate = h('button', 'sm-dbtn', '只看这一组');
      isolate.type = 'button';
      isolate.addEventListener('click', function () {
        APP.setFilter({ arm: [n.path || ''] });
        if (els.filterState) {
          els.filterState.arm = [n.path || ''];
        }
        select(n.id);
      });
      actions.appendChild(isolate);
    }
    box.appendChild(actions);

    var adjs = APP.scene.adj && APP.scene.adj.get(n.id);
    if (adjs && adjs.size) {
      var wrap = h('div', 'sm-dnbr');
      wrap.appendChild(h('div', 'sm-dnbr-title', '相连的星'));
      var chips = h('div', 'sm-dnbr-chips');
      var shown = 0;
      adjs.forEach(function (otherId) {
        if (shown >= 8) return;
        var other = DATA && (DATA.nodes || []).filter(function (x) { return x.id === otherId; })[0];
        if (!other) return;
        shown += 1;
        var chip = h('button', 'sm-dnbr-chip', other.label || otherId);
        chip.type = 'button';
        chip.addEventListener('click', function () {
          select(other.id);
          APP.flyTo(other.id);
        });
        chips.appendChild(chip);
      });
      wrap.appendChild(chips);
      box.appendChild(wrap);
    }
  }

  function renderDetailDust(d) {
    var box = els.detail;
    box.innerHTML = '';
    box.classList.add('is-open');
    var close = h('button', 'sm-close');
    close.type = 'button';
    close.innerHTML = icon('close');
    close.addEventListener('click', function () {
      APP.setSelect(null);
      box.classList.remove('is-open');
    });
    box.appendChild(close);

    var c = R.DUST_COLOR[d.k] || [0.6, 0.7, 0.9];
    var kicker = h('div', 'sm-dkicker');
    kicker.style.color = 'rgb(' + Math.round(c[0] * 255) + ',' + Math.round(c[1] * 255) + ',' + Math.round(c[2] * 255) + ')';
    kicker.textContent = '星尘 · ' + (DUST_TEXT[d.k] || d.k);
    box.appendChild(kicker);
    box.appendChild(h('h2', 'sm-dtitle', d.l || '一次活动'));
    var grid = h('div', 'sm-meta');
    grid.appendChild(metaRow('时间', fmtTime(d.t)));
    if (d.m) grid.appendChild(metaRow('落在', d.m));
    box.appendChild(grid);
    box.appendChild(h('p', 'sm-dtext',
      '星尘不是记忆，是记忆被写下、被回想、被整理时留下的痕迹。它比记忆暗，也消失得更快。'));
    APP.setSelect(null);
  }

  // ------------------------------------------------------------------ 标签

  function buildLabels() {
    var box = els.labels;
    box.innerHTML = '';
    labels = [];
    if (!DATA) return;
    var mode = labelMode();
    if (mode === 'off') {
      box.innerHTML = '';
      labels = [];
      return;
    }
    var picked = (DATA.nodes || []).filter(function (n) {
      if (n.type === 'core') return false;
      if (n.type === 'group') return true;
      // 实体名很多是从句子里抽出来的短语（"只能由插件侧"），太长的直接不给标签，
      // 否则全景会被一串半截句子糊住。
      if (n.type === 'entity') return (n.mention_count || 1) >= 2 && (n.label || '').length <= 10;
      if (n.type === 'trait') return true;
      if (mode === 'full') return n.type === 'memory';
      // 精简模式下，记忆名只在拉近时才给：全景下满屏文字会立刻变廉价。
      return n.grade === 'S' && APP && APP.cam.dist < 2.6;
    });
    picked.slice(0, mode === 'full' ? 90 : 32).forEach(function (n) {
      var el = h('div', 'sm-label sm-label-' + n.type);
      el.appendChild(h('i', 'sm-label-tick'));
      var text = n.label || '';
      var limit = mode === 'full' ? 18 : 12;
      if (n.type === 'memory' && text.length > limit) text = text.slice(0, limit - 1) + '…';
      if (n.type === 'entity' && text.length > 10) text = text.slice(0, 9) + '…';
      el.appendChild(h('span', null, text));
      box.appendChild(el);
      labels.push({ id: n.id, el: el, w: 0, h: 0 });
    });
  }

  function updateLabels() {
    if (!APP || !labels.length) return;
    var used = [];
    var cw = els.labels.clientWidth;
    var ch = els.labels.clientHeight;
    var base = els.root.classList.contains('is-ready') ? 1 : 0;
    for (var i = 0; i < labels.length; i++) {
      var item = labels[i];
      var pos = APP.screenOf(item.id);
      if (!pos || pos.x < -200 || pos.x > cw + 200 || pos.y < -200 || pos.y > ch + 200) {
        item.el.style.opacity = '0';
        continue;
      }
      var w = item.w || (item.w = item.el.offsetWidth || 60);
      var hgt = item.h || (item.h = item.el.offsetHeight || 14);
      var x = pos.x + 9;
      var y = pos.y - hgt * 0.5;
      var overlap = false;
      for (var j = 0; j < used.length; j++) {
        var u = used[j];
        if (x < u.x + u.w && x + w > u.x && y < u.y + u.h && y + hgt > u.y) { overlap = true; break; }
      }
      if (overlap) {
        item.el.style.opacity = '0';
        continue;
      }
      used.push({ x: x, y: y, w: w, h: hgt });
      var fade = clamp(1.6 - pos.depth * 0.28, 0.28, 1) * base;
      item.el.style.transform = 'translate3d(' + x.toFixed(1) + 'px,' + y.toFixed(1) + 'px,0)';
      item.el.style.opacity = String(fade);
      if (used.length > 34) { item.el.style.opacity = '0'; }
    }
  }

  // ------------------------------------------------------------------ 提示

  function showTip(item, x, y) {
    var tip = els.tip;
    if (!item) { tip.classList.remove('is-on'); return; }
    var ref = item.ref;
    tip.innerHTML = '';
    if (item.kind === 'node') {
      var kicker = h('div', 'sm-tip-kicker');
      var tag = TYPE_TEXT[ref.type] || ref.type;
      if (ref.type === 'memory' && ref.grade) tag += ' · ' + (GRADE_TEXT[ref.grade] || ref.grade);
      kicker.textContent = tag;
      tip.appendChild(kicker);
      tip.appendChild(h('div', 'sm-tip-title', ref.label || ''));
      if (ref.text && ref.text !== ref.label) {
        tip.appendChild(h('div', 'sm-tip-text', ref.text.slice(0, 120)));
      }
      if (ref.day) tip.appendChild(h('div', 'sm-tip-meta', ref.day));
    } else {
      tip.appendChild(h('div', 'sm-tip-kicker', '星尘 · ' + (DUST_TEXT[ref.k] || ref.k)));
      tip.appendChild(h('div', 'sm-tip-title', ref.l || ''));
      tip.appendChild(h('div', 'sm-tip-meta', fmtTime(ref.t)));
    }
    tip.classList.add('is-on');
    var w = tip.offsetWidth, hh = tip.offsetHeight;
    var left = x + 16, top = y + 16;
    if (left + w > els.root.clientWidth - 12) left = x - w - 16;
    if (top + hh > els.root.clientHeight - 12) top = y - hh - 16;
    tip.style.transform = 'translate3d(' + Math.max(8, left) + 'px,' + Math.max(8, top) + 'px,0)';
  }

  // ------------------------------------------------------------------ 时间轴

  function timeBounds() {
    if (!DATA) return { min: 0, max: 1, minIso: null, maxIso: null };
    var min = Infinity, max = -Infinity, minIso = null, maxIso = null;
    (DATA.nodes || []).forEach(function (n) {
      if (n.type !== 'memory' || !n.created_at) return;
      var t = Date.parse(n.created_at);
      if (isNaN(t)) return;
      if (t < min) { min = t; minIso = n.created_at; }
      if (t > max) { max = t; maxIso = n.created_at; }
    });
    if (!isFinite(min)) return { min: 0, max: 1, minIso: null, maxIso: null };
    return { min: min, max: max, minIso: minIso, maxIso: maxIso };
  }

  function cursorLabel(t) {
    var b = els.bounds;
    if (!b || !b.minIso) return '全部';
    if (t >= 0.999) return '全部 · ' + fmtDateShort(b.maxIso);
    var ms = b.min + (b.max - b.min) * t;
    var count = 0;
    (DATA.nodes || []).forEach(function (n) {
      if (n.type === 'memory' && typeof n.t === 'number' && n.t <= t) count += 1;
    });
    return fmtDateShort(new Date(ms).toISOString()) + ' · ' + count + ' 条';
  }

  function setCursor(t) {
    t = clamp(t, 0, 1);
    APP.setCursor(t);
    els.fill.style.width = (t * 100).toFixed(2) + '%';
    els.glow.style.width = (t * 100).toFixed(2) + '%';
    els.dot.style.left = (t * 100).toFixed(2) + '%';
    els.timeline.classList.toggle('is-scrubbed', t < 0.999);
    els.clock.textContent = cursorLabel(t);
  }

  function bindTimeline() {
    var track = els.track;
    var dragging = false;

    function fromEvent(ev) {
      var rect = track.getBoundingClientRect();
      var x = (ev.clientX !== undefined ? ev.clientX : ev.touches[0].clientX) - rect.left;
      return clamp(x / Math.max(1, rect.width), 0, 1);
    }
    function down(ev) {
      dragging = true;
      track.classList.add('is-dragging');
      setCursor(fromEvent(ev));
      ev.preventDefault();
    }
    function move(ev) {
      if (!dragging) return;
      setCursor(fromEvent(ev));
    }
    function up() {
      dragging = false;
      track.classList.remove('is-dragging');
    }
    track.addEventListener('pointerdown', down);
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);

    els.play.addEventListener('click', function () {
      if (APP.isPlaying()) {
        setCursor(APP.mode.cursor);
      } else {
        els.timeline.classList.add('is-playing');
        APP.play();
      }
    });

  }

  function onCursor(t, playing) {
    els.fill.style.width = (t * 100).toFixed(2) + '%';
    els.glow.style.width = (t * 100).toFixed(2) + '%';
    els.dot.style.left = (t * 100).toFixed(2) + '%';
    els.timeline.classList.toggle('is-playing', playing);
    els.timeline.classList.toggle('is-scrubbed', t < 0.999);
    els.clock.textContent = cursorLabel(t);
    var icon = document.getElementById('sm-play-icon');
    if (icon) {
      icon.setAttribute('d', playing ? 'M2 1h3v10H2zM7 1h3v10H7z' : 'M2 1l9 5-9 5z');
    }
  }

  // ------------------------------------------------------------------ 遥测

  function renderTelemetry() {
    var box = els.telemetry;
    box.innerHTML = '';
    if (!DATA) return;
    var m = DATA.meta || {};
    var counts = m.counts || {};
    var stats = APP.stats();

    var rows = [
      ['星体', num(stats.nodes)],
      ['连线', num(stats.links)],
      ['星尘', num(stats.dust)]
    ];
    rows.forEach(function (row) {
      var item = h('div', 'sm-tm');
      var value = h('b', null, '0');
      animateNumber(value, Number(String(row[1]).replace(/,/g, '')) || 0);
      item.appendChild(value);
      item.appendChild(h('span', null, row[0]));
      box.appendChild(item);
    });

    var note = h('div', 'sm-tm-note');
    note.textContent = fmtTime(m.generated_at) + 'Z';
    box.appendChild(note);
  }

  // ------------------------------------------------------------------ 脉冲

  function pulseNew(ids) {
    if (!ids || !ids.length || !DATA) return;
    ids.slice(0, 6).forEach(function (id) {
      var el = h('div', 'sm-pulse');
      els.pulses.appendChild(el);
      pulseItems.push({ id: id, el: el, born: performance.now() });
    });
  }

  function updatePulses() {
    if (!pulseItems.length) return;
    var now = performance.now();
    pulseItems = pulseItems.filter(function (item) {
      var age = now - item.born;
      if (age > 3200) {
        if (item.el.parentNode) item.el.parentNode.removeChild(item.el);
        return false;
      }
      var pos = APP.screenOf(item.id);
      if (!pos) { item.el.style.opacity = '0'; return true; }
      var t = age / 3200;
      var size = 10 + t * 90;
      item.el.style.transform = 'translate3d(' + (pos.x - size / 2) + 'px,' + (pos.y - size / 2) + 'px,0)';
      item.el.style.width = size + 'px';
      item.el.style.height = size + 'px';
      item.el.style.opacity = String((1 - t) * 0.85);
      return true;
    });
  }

  // ------------------------------------------------------------------ 导出

  function download(url, name) {
    var a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { document.body.removeChild(a); }, 60);
  }

  function stamp() {
    var d = new Date();
    var p = function (v) { return v < 10 ? '0' + v : String(v); };
    return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '-' + p(d.getHours()) + p(d.getMinutes());
  }

  function exportImage() {
    var url;
    try { url = APP.snapshot(); } catch (e) { showError('导出失败：' + e.message); return; }
    // 顺手给一张「可分享的长图」：星图 + 标题 + 数字。
    var img = new Image();
    img.onload = function () {
      var W = 1600;
      var H = Math.round(W * 0.72);
      var pad = 64;
      var board = document.createElement('canvas');
      board.width = W;
      board.height = H;
      var ctx = board.getContext('2d');
      ctx.fillStyle = '#04060b';
      ctx.fillRect(0, 0, W, H);

      var cover = Math.max(W / img.width, H / img.height);
      var dw = img.width * cover, dh = img.height * cover;
      ctx.globalAlpha = 0.96;
      ctx.drawImage(img, (W - dw) / 2, (H - dh) / 2, dw, dh);
      ctx.globalAlpha = 1;

      var grad = ctx.createLinearGradient(0, H * 0.55, 0, H);
      grad.addColorStop(0, 'rgba(4,6,11,0)');
      grad.addColorStop(1, 'rgba(4,6,11,0.92)');
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, W, H);

      ctx.fillStyle = 'rgba(255,255,255,0.42)';
      ctx.font = '500 13px "Segoe UI", "Microsoft YaHei", system-ui, sans-serif';
      ctx.fillText('P M E M  ·  H I P P O C A M P U S   S T A R   M A P', pad, H - 168);

      ctx.fillStyle = '#f2f6ff';
      ctx.font = '600 44px "Segoe UI", "Microsoft YaHei", system-ui, sans-serif';
      ctx.fillText((DATA.meta && DATA.meta.title) || '记忆星图', pad, H - 112);

      ctx.fillStyle = 'rgba(226,236,250,0.52)';
      ctx.font = '400 17px "Segoe UI", "Microsoft YaHei", system-ui, sans-serif';
      ctx.fillText((DATA.meta && DATA.meta.subtitle) || '', pad, H - 80);

      var stats = APP.stats();
      ctx.fillStyle = 'rgba(226,236,250,0.72)';
      ctx.font = '500 16px "Segoe UI", "Microsoft YaHei", system-ui, sans-serif';
      var line = stats.nodes + ' 星体   ·   ' + stats.links + ' 连线   ·   ' + stats.dust + ' 星尘   ·   ' +
        ((DATA.meta && DATA.meta.timeline && DATA.meta.timeline.days) || 0) + ' 天';
      ctx.fillText(line, pad, H - 42);

      ctx.textAlign = 'right';
      ctx.fillStyle = 'rgba(226,236,250,0.36)';
      ctx.font = '400 14px "Segoe UI", "Microsoft YaHei", system-ui, sans-serif';
      ctx.fillText('半径 = 时间 · 角度 = 分组', W - pad, H - 42);

      board.toBlob(function (blob) {
        if (!blob) return;
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'pmem-starmap-' + stamp() + '.png';
        document.body.appendChild(a);
        a.click();
        setTimeout(function () {
          URL.revokeObjectURL(a.href);
          document.body.removeChild(a);
        }, 4000);
      }, 'image/png');
    };
    img.onerror = function () { download(url, 'pmem-starmap-' + stamp() + '.png'); };
    img.src = url;
  }

  // ------------------------------------------------------------------ 数据

  function applyData(data, isFirst) {
    DATA = data;
    if (!DATA || !DATA.nodes || !DATA.nodes.length) {
      showError('这个库里还没有可画的记忆：先写一条，再回来。');
      bootDone();
      return;
    }
    APP.setData(DATA);
    if (isFirst) {
      buildLabels();
      buildFilters();
      renderTelemetry();
      els.bounds = timeBounds();
      els.timeline.classList.remove('is-disabled');
      els.clock.textContent = '全部 · ' + fmtDateShort(els.bounds.maxIso);
      els.fill.style.width = '100%';
      els.glow.style.width = '100%';
      els.dot.style.left = '100%';
      var title = (DATA.meta && DATA.meta.title) || '记忆星图';
      els.title.textContent = title.replace(/^PMem\s*·\s*/, '');
      bootDone();
    } else {
      buildLabels();
      renderTelemetry();
      els.bounds = timeBounds();
    }
  }

  function loadData() {
    if (EMBEDDED) {
      applyData(EMBEDDED, true);
      return Promise.resolve(EMBEDDED);
    }
    return fetch(DATA_URL, { headers: { accept: 'application/json' } })
      .then(function (r) {
        if (r.status === 401) throw new Error('没有登录凭证：回主界面刷新一次再进来');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (payload) {
        // 两种信封都吃：插件接口是 {ok:true, ...}，独立服务的兜底也是。
        // 只认 ok 会让「裸图」这种半对的响应整页报错，没必要。
        if (!payload || (!payload.ok && !payload.nodes)) {
          throw new Error((payload && payload.error) || '接口返回异常');
        }
        applyData(payload, true);
        return payload;
      });
  }

  function regenerate() {
    if (EMBEDDED || STANDALONE) return;
    var btn = els.actions.querySelector('.sm-btn-refresh');
    if (btn) btn.classList.add('is-spinning');
    fetch(API + '/starmap/regen', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}'
    }).then(function (r) { return r.json(); })
      .then(function () { return loadData(); })
      .catch(function (e) { showError('重绘失败：' + e.message); })
      .finally(function () {
        if (btn) btn.classList.remove('is-spinning');
      });
  }

  var liveSource = null;
  // 独立页没有 SSE，用轮询：只有 generated_at 变了才重建，省得每 4 秒重画一次。
  function pollData() {
    if (!STANDALONE || !DATA_URL) return;
    var stamp = DATA && DATA.meta ? DATA.meta.generated_at : null;
    setInterval(function () {
      fetch(DATA_URL, { headers: { accept: 'application/json' } })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(function (payload) {
          if (!payload || !payload.ok) return;
          var next = payload.meta && payload.meta.generated_at;
          if (stamp && next === stamp) return;
          stamp = next;
          applyData(payload, false);
        })
        .catch(function () { /* 下一轮再试 */ });
    }, 4000);
  }

  function connectLive() {
    if (STANDALONE) { pollData(); return; }
    if (EMBEDDED || typeof EventSource === 'undefined') return;
    var dot = els.live;
    try {
      liveSource = new EventSource(EVENTS_URL);
    } catch (e) {
      return;
    }
    liveSource.addEventListener('hello', function () {
      if (dot) dot.classList.add('is-on');
    });
    liveSource.addEventListener('refresh', function () {
      loadData().then(function () {
        pulseNew(lastNewIds);
      }).catch(function () {});
    });
    liveSource.onerror = function () {
      if (dot) dot.classList.remove('is-on');
    };
    liveSource.onopen = function () {
      if (dot) dot.classList.add('is-on');
    };
  }

  var liveWanted = true;
  function toggleLive() {
    if (!els.live) return;
    liveWanted = !liveWanted;
    els.live.classList.toggle('is-on', liveWanted);
    els.live.classList.toggle('is-off', !liveWanted);
    fetch(API + '/config', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ starmapLive: liveWanted })
    }).catch(function (e) { showError('设置实时失败：' + e.message); });
    if (liveWanted && !liveSource) connectLive();
  }

  var lastNewIds = [];
  var lastZoomed = null;

  // ------------------------------------------------------------------ 指针

  function bindPointer() {
    var canvas = els.canvas;
    var dragging = false;
    var panning = false;
    var moved = 0;
    var last = { x: 0, y: 0 };
    var downAt = { x: 0, y: 0 };

    canvas.addEventListener('pointerdown', function (ev) {
      canvas.setPointerCapture(ev.pointerId);
      dragging = true;
      moved = 0;
      panning = ev.button === 2 || ev.button === 1 || ev.shiftKey;
      last.x = ev.clientX;
      last.y = ev.clientY;
      downAt.x = ev.clientX;
      downAt.y = ev.clientY;
      canvas.classList.add(panning ? 'is-panning' : 'is-dragging');
      ev.preventDefault();
    });

    canvas.addEventListener('pointermove', function (ev) {
      if (dragging) {
        var dx = ev.clientX - last.x;
        var dy = ev.clientY - last.y;
        last.x = ev.clientX;
        last.y = ev.clientY;
        moved += Math.abs(dx) + Math.abs(dy);
        if (panning) {
          APP.pan(dx, dy);
        } else {
          APP.orbitTo(APP.cam.goalTheta + dx * 0.0055, APP.cam.goalPhi - dy * 0.0045);
        }
        return;
      }
      var rect = canvas.getBoundingClientRect();
      var item = APP.pick(ev.clientX - rect.left, ev.clientY - rect.top);
      APP.setHover(item ? (item.kind === 'node' ? item.ref.id : null) : null);
      showTip(item, ev.clientX - rect.left, ev.clientY - rect.top);
      canvas.classList.toggle('is-hot', Boolean(item));
    });

    function end(ev) {
      if (!dragging) return;
      dragging = false;
      canvas.classList.remove('is-dragging', 'is-panning');
      var dist = Math.abs(ev.clientX - downAt.x) + Math.abs(ev.clientY - downAt.y);
      if (dist < 5) {
        var rect = canvas.getBoundingClientRect();
        var item = APP.pick(ev.clientX - rect.left, ev.clientY - rect.top);
        if (item) {
          if (item.kind === 'node') select(item.ref.id);
          else renderDetailDust(item.ref);
        } else {
          clearDetail();
        }
      }
    }
    canvas.addEventListener('pointerup', end);
    canvas.addEventListener('pointercancel', end);
    canvas.addEventListener('pointerleave', function () {
      APP.setHover(null);
      showTip(null);
    });

    canvas.addEventListener('wheel', function (ev) {
      ev.preventDefault();
      var f = Math.exp(clamp(ev.deltaY, -220, 220) * 0.0012);
      APP.dolly(f);
    }, { passive: false });

    canvas.addEventListener('dblclick', function (ev) {
      var rect = canvas.getBoundingClientRect();
      var item = APP.pick(ev.clientX - rect.left, ev.clientY - rect.top);
      if (item && item.kind === 'node') APP.flyTo(item.ref.id);
      else APP.resetView();
    });

    canvas.addEventListener('contextmenu', function (ev) { ev.preventDefault(); });

    window.addEventListener('keydown', function (ev) {
      if (ev.target && /INPUT|TEXTAREA/.test(ev.target.tagName)) return;
      var k = ev.key.toLowerCase();
      if (k === 'escape') {
        if (els.searchbox.classList.contains('is-open') || els.filters.classList.contains('is-open')) {
          closeAll();
        } else if (els.detail.classList.contains('is-open')) {
          clearDetail();
        } else if (IN_FRAME) {
          closeOverlay();
        }
      } else if (k === 'f') {
        toggleFullscreen();
      } else if (k === 'r') {
        APP.resetView();
      } else if (k === ' ') {
        ev.preventDefault();
        if (APP.isPlaying()) setCursor(APP.mode.cursor);
        else APP.play();
      } else if (k === '/') {
        ev.preventDefault();
        togglePanel(els.searchbox, els.actions.querySelector('.sm-btn-search'));
      }
    });

    document.addEventListener('fullscreenchange', function () {
      setTimeout(function () { APP.resize(); }, 80);
    });
  }

  // ------------------------------------------------------------------ 启动

  function fail(kind, message) {
    bootDone();
    showError(message);
    els.boot.classList.add('is-failed');
    if (kind === 'webgl') {
      els.boot.querySelector('.sm-boot-text').textContent = '这个窗口没有 WebGL2';
    }
  }

  // 地址参数：?t=0.55&labels=off&cruise=0
  // 让"这一张星图"可以被分享/存书签，也方便截图对着看。
  function readUrlOptions() {
    try {
      var params = new URLSearchParams(window.location.search);
      return {
        t: params.has('t') ? clamp(Number(params.get('t')), 0, 1) : null,
        labels: params.get('labels'),
        cruise: params.get('cruise') !== '0',
      };
    } catch (e) {
      return { t: null, labels: null, cruise: true };
    }
  }

  function boot() {
    var opts = readUrlOptions();
    if (opts.labels && LABEL_MODES.indexOf(opts.labels) >= 0) {
      try { window.localStorage.setItem(LABEL_KEY, opts.labels); } catch (e) { /* 存不了 */ }
    }
    buildShell();
    buildActions();
    bindTimeline();
    bindPointer();

    els.q.addEventListener('input', function () {
      buildHits(els.q.value.trim());
    });
    els.q.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && hits.length) {
        var n = hits[0].n;
        select(n.id);
        APP.flyTo(n.id);
        closeAll();
      }
      if (ev.key === 'Escape') closeAll();
    });
    document.addEventListener('pointerdown', function (ev) {
      if (els.searchbox.classList.contains('is-open') &&
        !els.searchbox.contains(ev.target) &&
        !ev.target.closest('.sm-btn-search')) {
        closeAll();
      }
      if (els.filters.classList.contains('is-open') &&
        !els.filters.contains(ev.target) &&
        !ev.target.closest('.sm-btn-filter')) {
        closeAll();
      }
    });

    try {
      APP = R.create(els.canvas, {
        autoCruise: opts.cruise,
        onData: function (data, newIds) {
          lastNewIds = newIds || [];
          if (newIds && newIds.length) {
            setTimeout(function () { pulseNew(newIds); }, 120);
          }
        },
        onCursor: onCursor,
        onFrame: function (now) { if (frameHook) frameHook(now); }
      });
    } catch (e) {
      if (e && e.code === 'no-webgl2') {
        fail('webgl', '这个窗口不支持 WebGL2。用最新版 Chrome / Edge 打开，或者升级显卡驱动。');
      } else {
        fail('gl', '星图渲染器启动失败：' + (e && e.message ? e.message : e));
      }
      return;
    }

    APP.start();
    APP.mode.lastInput = performance.now();

    var frameTick = 0;
    frameHook = function () {
      frameTick += 1;
      if (frameTick % 2) return;   // 标签半个刷新率，省点
      // 精简模式里记忆名是"拉近才出现"，所以要盯着距离的跨越点重建一次。
      var zoomed = APP.cam.dist < 2.6;
      if (zoomed !== lastZoomed) { lastZoomed = zoomed; buildLabels(); }
      updateLabels();
      updatePulses();
    };

    loadData().catch(function (e) {
      fail('data', '取不到星图数据：' + e.message);
    }).then(function () {
      renderTelemetry();
      connectLive();
      var start = opts.t === null ? 1 : opts.t;
      if (start < 0.995) setCursor(start);
      else onCursor(1, false);
      // 先把标签摆一次：只靠 rAF 的话，第一帧没跑到它们就还是透明的。
      updateLabels();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
