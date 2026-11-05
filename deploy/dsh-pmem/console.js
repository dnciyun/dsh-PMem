/**
 * M22：PMem 控制台后端。
 *
 * 整页控制台（侧边栏「记忆控制台」）要的数据与动作都在这里，前缀
 * `/dsh-pmem/api/console`。设计上只有三件事：
 *
 *   1. **只读聚合**：bootstrap / memories / memory / profile / groups /
 *      conflicts / stats / doctor —— 全部调用既有 `callPmem`（Python CLI），
 *      不在 JS 里重写检索、分级、冲突判定。
 *   2. **管理动作**：feedback / update / delete —— 写路由只接受 POST，
 *      同源 + application/json + body ≤ 256 KB 的守卫在 `web.js` 的
 *      `guarded()` 里，这里不重复发明。
 *   3. **最近列表**：CLI 没有 `recent` 模式，`/memories?mode=recent` 用
 *      node:sqlite 只读连接查 memory 表（拿不到就退回 `stats.recent`）。
 *      这是唯一一处直接读库，且只读、失败即降级，不改任何数据。
 *
 * 契约见 `docs/console-design.md` §5。2026-11-05 需求纠偏后，问诊/聊天
 * （chats / SSE / 模型 / 50 轮）整段作废，本文件不含任何 LLM 调用。
 */

import { createRequire } from 'node:module'
import {
  createReadStream,
  existsSync,
  readFileSync,
  readdirSync,
  statSync,
} from 'node:fs'
import { basename, dirname, join, resolve, sep } from 'node:path'

const require = createRequire(import.meta.url)

/** 控制台 API 前缀：和 web.js 里注册的路由一字不差。 */
export const CONSOLE_API = '/dsh-pmem/api/console'

/** 写请求 body 上限，和 web.js 的 MAX_BODY 对齐。 */
export const MAX_BODY = 256 * 1024

export const MEMORY_MODES = Object.freeze(['auto', 'keyword', 'recent'])
/** /memories 的排序方式（无 q 时 relevance 退化为 time）。 */
export const MEMORY_SORTS = Object.freeze(['relevance', 'time', 'importance', 'uses'])
export const FEEDBACK_SIGNALS = Object.freeze([
  'useful',
  'irrelevant',
  'wrong',
  'confirm',
  'deny',
  'pin',
  'unpin',
])
export const GRADES = Object.freeze(['S', 'A', 'B', 'C', 'D'])
export const GROUP_TABLES = Object.freeze(['memory', 'entity', 'trait', 'experience'])
/** 记忆 kind 枚举（和 Python `gate/schemas.py::_KIND` 一致）。 */
export const MEMORY_KINDS = Object.freeze([
  'episodic',
  'semantic',
  'person',
  'relation',
  'experience',
  'goal',
  'meta',
])
/** 控制台允许改的 status：比 store 的集合窄，避免误把记忆标成 deleted/discarded。 */
export const MEMORY_STATUSES = Object.freeze(['active', 'contested', 'dormant', 'archived'])
/** 单条 update 可接受的字段（reason 不是状态字段，只作为 CLI 备注）。 */
export const UPDATE_FIELDS = Object.freeze([
  'kind',
  'status',
  'sensitivity',
  'importance',
  'pinned',
  'grade',
  'group',
  'reason',
])
/** 批量 update 的 patch 白名单：第 8 轮拍板只有这六个元数据字段。 */
export const BATCH_UPDATE_FIELDS = Object.freeze([
  'kind',
  'grade',
  'group',
  'sensitivity',
  'importance',
  'status',
])
/** P1 运维动作白名单：name 只能是这几个，参数由服务端按白名单拼。 */
export const ACTION_NAMES = Object.freeze(['doctor', 'vacuum', 'evolve', 'net-extract'])
export const EVOLVE_LAYERS = Object.freeze(['patterns', 'conflicts', 'experience', 'tuning'])
export const BATCH_OPS = Object.freeze(['feedback', 'retract', 'delete', 'update'])
export const BATCH_MAX_IDS = 100
export const BACKUP_KINDS = Object.freeze(['manual', 'daily', 'pre-migrate'])

const ID_RE = /^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$/
const TOKEN_RE = /^[a-z][a-z_]{0,31}$/
const TYPE_RE = /^[a-z][a-z0-9_]{0,31}$/
const PATH_MAX = 200
/** CLI 错误码 → HTTP 语义（和 Python 侧 base/errors.py 的 HTTP_STATUS 对齐）。 */
const HTTP_STATUS = Object.freeze({
  E_VALIDATION: 400,
  E_NOT_FOUND: 404,
  E_CONFLICT: 409,
  E_DENIED: 403,
  E_DEGRADED_READONLY: 503,
  E_BUDGET: 429,
  E_RATE_LIMIT: 429,
  E_INTERNAL: 500,
})


// ---------------------------------------------------------------------------
// 小工具：HTTP / 参数 / 数字
// ---------------------------------------------------------------------------

function json(res, status, body) {
  const text = JSON.stringify(body)
  res.statusCode = status
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('cache-control', 'no-store')
  res.end(text)
}

function methodOf(req) {
  return String(req?.method ?? 'GET').toUpperCase()
}

/** 只读路由：GET 之外一律 405（写接口是另一批路径）。 */
function requireGet(req, res) {
  if (methodOf(req) === 'GET') return true
  json(res, 405, { ok: false, error: '只接受 GET' })
  return false
}

/** 写路由：POST 之外一律 405。 */
function requirePost(req, res) {
  if (methodOf(req) === 'POST') return true
  json(res, 405, { ok: false, error: '只接受 POST' })
  return false
}

/**
 * 读 JSON body。上限 256 KB；超限返回 413，坏了返回 400。
 * `guarded()` 已经校验过 content-type，这里只负责语义。
 */
function readJsonBody(req, limit = MAX_BODY) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    const failWith = (message, code, statusCode) => {
      const error = new Error(message)
      error.code = code
      error.statusCode = statusCode
      reject(error)
      try { req.destroy() } catch { /* 已经断了 */ }
    }
    req.on('data', chunk => {
      size += chunk?.length ?? 0
      if (size > limit) {
        failWith('body 超过 256 KB', 'E_TOO_LARGE', 413)
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8')
      if (!raw.trim()) return resolve({})
      let parsed
      try {
        parsed = JSON.parse(raw)
      } catch {
        return failWith('body 不是 JSON', 'E_BAD_JSON', 400)
      }
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        return failWith('body 必须是 JSON 对象', 'E_BAD_JSON', 400)
      }
      resolve(parsed)
    })
    req.on('error', reject)
  })
}

function urlOf(req) {
  try {
    return new URL(String(req?.url ?? '/'), 'http://localhost')
  } catch {
    return new URL('/', 'http://localhost')
  }
}

function numberOrNull(value) {
  if (value === null || value === undefined || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function textOrNull(value) {
  if (value === null || value === undefined) return null
  const body = String(value)
  return body ? body : null
}

/** 夹取整数：空值用 fallback，越界夹到 [min, max]。 */
export function intIn(value, fallback, min, max) {
  const number = Number(value)
  if (value === null || value === undefined || value === '' || !Number.isFinite(number)) {
    return Math.min(max, Math.max(min, Math.trunc(Number(fallback))))
  }
  return Math.min(max, Math.max(min, Math.trunc(number)))
}

/** `a,b` / 数组 / 多值查询都收成一个去重数组。 */
export function parseList(value) {
  if (value === null || value === undefined || value === '') return []
  const flat = []
  const push = item => {
    if (item === null || item === undefined) return
    const body = String(item).trim()
    if (!body) return
    for (const piece of body.split(',')) {
      const clean = piece.trim()
      if (clean) flat.push(clean)
    }
  }
  if (Array.isArray(value)) for (const item of value) push(item)
  else push(value)
  return [...new Set(flat)]
}

function validateId(raw) {
  const id = String(raw ?? '').trim()
  return ID_RE.test(id) ? id : null
}

function safe(fn, fallback = null) {
  try {
    const value = fn()
    return value === undefined ? fallback : value
  } catch {
    return fallback
  }
}

/** 本机时区的 YYYY-MM-DD：和 Python 侧 `day_key` 同一个口径。 */
export function localDayKey(date = new Date()) {
  const pad = value => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/**
 * 通用参数校验：实体名 / 关系谓词 / as_of / group path 这类「会进 argv 的值」。
 * 禁止控制字符、禁止以 `-` 开头（argparse 会把它当选项）、长度上限。
 */
export function safeValue(raw, max = PATH_MAX) {
  const body = String(raw ?? '').trim()
  if (!body || body.length > max) return null
  if (body.startsWith('-')) return null
  if (/[\u0000-\u001f\u007f]/.test(body)) return null
  return body
}

function safeRegexValue(raw, re, max = PATH_MAX) {
  const body = safeValue(raw, max)
  return body && re.test(body) ? body : null
}

/** 实体/关系的 brief：CLI 与只读 SQL 都用这一套字段名。 */
export function normalizeEntity(item) {
  const raw = item ?? {}
  let disambig = []
  if (Array.isArray(raw.disambig)) disambig = raw.disambig.map(String)
  else if (typeof raw.extra_json === 'string' && raw.extra_json) {
    try {
      const extra = JSON.parse(raw.extra_json)
      if (Array.isArray(extra?.disambig)) disambig = extra.disambig.map(String)
    } catch { /* extra_json 坏了就当没有 */ }
  }
  const mentionCount = numberOrNull(raw.mention_count)
  const groups = Array.isArray(raw.groups)
    ? raw.groups.map(String)
    : (typeof raw.group_paths === 'string' && raw.group_paths
      ? raw.group_paths.split('||').filter(Boolean)
      : [])
  return {
    id: String(raw.id ?? ''),
    name: String(raw.name ?? ''),
    type: textOrNull(raw.type),
    name_norm: textOrNull(raw.name_norm),
    confidence: numberOrNull(raw.confidence),
    mention_count: mentionCount,
    // 需求里前端叫 mentions；两个都给，避免歧义。
    mentions: mentionCount,
    grade: textOrNull(raw.grade),
    groups,
    first_seen: textOrNull(raw.first_seen),
    last_seen: textOrNull(raw.last_seen),
    disambig,
  }
}

/**
 * 备份目录：和 Python 侧 `ops/backup.py::_dir()` 同一个默认口径——
 * 数据库文件所在目录下的 `backups/`。`config.backupDir` 可覆盖（仅影响
 * 列表/下载；生成仍走 CLI，CLI 默认也落在同一个目录）。
 */
export function resolveBackupDir(config = {}) {
  if (config.backupDir && typeof config.backupDir === 'string') {
    return resolve(config.backupDir)
  }
  const dbPath = typeof config.db === 'string' ? config.db.trim() : ''
  if (!dbPath || dbPath === ':memory:') return null
  return join(dirname(resolve(dbPath)), 'backups')
}

/** 下载时只认备份目录里的 basename：不拼路径、不认分隔符、只认 .db。 */
export function safeBackupName(raw) {
  const name = String(raw ?? '').trim()
  if (!name || name.length > 200) return null
  if (name.includes('..')) return null
  if (!/^[A-Za-z0-9._-]+\.db$/.test(name)) return null
  if (name.startsWith('.')) return null
  return name
}

/** 把备份目录里的文件列出来（不跟随符号链接、只收 .db）。 */
function listBackupFiles(dir) {
  if (!dir) return null
  let entries
  try {
    entries = readdirSync(dir, { withFileTypes: true })
  } catch {
    return null
  }
  const items = []
  for (const entry of entries) {
    try {
      if (!entry.isFile() || !entry.name.endsWith('.db') || entry.name.startsWith('.')) continue
      if (!safeBackupName(entry.name)) continue
      const info = statSync(join(dir, entry.name))
      items.push({
        name: entry.name,
        size_bytes: info.size,
        modified_at: info.mtime.toISOString(),
        mtime_ms: info.mtimeMs,
      })
    } catch {
      // 单个文件 stat 失败不影响整份列表
    }
  }
  items.sort((a, b) => b.mtime_ms - a.mtime_ms || a.name.localeCompare(b.name))
  return items.map(({ mtime_ms, ...rest }) => rest)
}

/** 实体关系的方向：详情页要能区分「它指向谁」和「谁指向它」。 */
function relationsWithDirection(items, entityId) {
  const out = []
  for (const item of items ?? []) {
    const srcId = String(item?.src?.id ?? '')
    const dstId = String(item?.dst?.id ?? '')
    const direction = srcId === entityId && dstId === entityId
      ? 'self'
      : srcId === entityId ? 'out' : dstId === entityId ? 'in' : null
    out.push({ ...item, direction })
  }
  return out
}


// ---------------------------------------------------------------------------
// 记忆归一化：前端只依赖这组字段（docs/console-design.md §5.2）
// ---------------------------------------------------------------------------

const WHY_LABEL = Object.freeze({
  tok: '词法命中',
  tri: '字面命中',
  vec: '语义相近',
  entity: '实体关联',
  graph: '关系跳转',
  fts: '全文命中',
})

function whyOf(raw, channels) {
  if (typeof raw?.why === 'string' && raw.why.trim()) return raw.why.trim()
  if (!channels.length) return null
  return channels.map(name => WHY_LABEL[name] ?? String(name)).join(' + ')
}

/**
 * 把 `search` / `get` / 只读 SQL 三种来源统一成 NormalizedMemory。
 * 字段名是前端契约的一部分，不要改；新增字段向后兼容。
 */
export function normalizeMemory(item) {
  const raw = item ?? {}
  const channels = Array.isArray(raw.channels)
    ? raw.channels.filter(channel => typeof channel === 'string')
    : []
  const groups = Array.isArray(raw.groups)
    ? raw.groups.map(String)
    : (typeof raw.group_paths === 'string' && raw.group_paths
      ? raw.group_paths.split('||').filter(Boolean)
      : [])
  const pinned = raw.pinned === true || raw.pinned === 1
  return {
    id: String(raw.id ?? ''),
    text: String(raw.text ?? raw.text_raw ?? ''),
    score: numberOrNull(raw.score),
    day: textOrNull(raw.day ?? raw.day_key),
    valid_from: textOrNull(raw.valid_from),
    kind: textOrNull(raw.kind),
    grade: textOrNull(raw.grade),
    status: textOrNull(raw.status),
    provenance: textOrNull(raw.provenance),
    confidence: numberOrNull(raw.confidence),
    importance: numberOrNull(raw.importance),
    groups,
    source_kind: textOrNull(raw.source_kind ?? raw.source?.kind),
    why: whyOf(raw, channels),
    truncated: Boolean(raw.truncated),
    // 以下为附加字段（前端可忽略）：详情页/列表做徽标时用得到。
    pinned,
    sensitivity: textOrNull(raw.sensitivity),
    created_at: textOrNull(raw.created_at),
    use_count: numberOrNull(raw.use_count),
  }
}

// ---------------------------------------------------------------------------
// 只读 SQL 通道：CLI 没有 recent 模式，只补这一条
// ---------------------------------------------------------------------------

/**
 * 打开一个只读的 memory 读取器。拿不到 node:sqlite 或库文件不可读时返回
 * null —— 调用方必须能降级，不能因为「最近列表」把整页打白。
 *
 * 每个方法都现开现关：控制台是低频请求，省掉生命周期管理比省掉几毫秒重要。
 */
export function openMemoryReader(dbPath) {
  if (!dbPath || typeof dbPath !== 'string') return null
  let DatabaseSync = null
  try {
    // 延迟 require：老 Node / 无 node:sqlite 的宿主必须能降级。
    ;({ DatabaseSync } = require('node:sqlite'))
  } catch {
    return null
  }
  if (typeof DatabaseSync !== 'function') return null

  const withDb = fn => {
    let db = null
    try {
      db = new DatabaseSync(dbPath, { readOnly: true })
      return fn(db)
    } catch {
      return null
    } finally {
      try { db?.close?.() } catch { /* 已经关了 */ }
    }
  }

  // 等级与分组不在 memory 主表里，用两个标量子查询带上；表不存在时整条查询
  // 会抛错 → withDb 返回 null → 调用方退回 stats.recent。
  const GRADE_SQL = "(SELECT grade FROM target_grade WHERE target_tbl='memory' AND target_id=m.id)"
  const GROUPS_SQL = "(SELECT group_concat(g.path, '||') FROM grp_member gm JOIN grp g ON g.id=gm.grp_id WHERE gm.target_tbl='memory' AND gm.target_id=m.id)"
  const COLUMNS = [
    'm.id',
    'm.text_raw',
    'm.day_key',
    'm.valid_from',
    'm.kind',
    'm.status',
    'm.provenance',
    'm.confidence',
    'm.importance',
    'm.source_kind',
    'm.pinned',
    'm.sensitivity',
    'm.created_at',
    'm.use_count',
    `${GRADE_SQL} AS grade`,
    `${GROUPS_SQL} AS group_paths`,
  ].join(', ')

  // 实体表的等级/分组也不在主表：同一套标量子查询。
  const ENTITY_GRADE_SQL = "(SELECT grade FROM target_grade WHERE target_tbl='entity' AND target_id=e.id)"
  const ENTITY_GROUPS_SQL = "(SELECT group_concat(g.path, '||') FROM grp_member gm JOIN grp g ON g.id=gm.grp_id WHERE gm.target_tbl='entity' AND gm.target_id=e.id)"
  const ENTITY_COLUMNS = [
    'e.id',
    'e.type',
    'e.name',
    'e.name_norm',
    'e.confidence',
    'e.mention_count',
    'e.first_seen',
    'e.last_seen',
    'e.extra_json',
    `${ENTITY_GRADE_SQL} AS grade`,
    `${ENTITY_GROUPS_SQL} AS group_paths`,
  ].join(', ')

  // 只读分页的排序口径：都不带 OFFSET 歧义，统一用 seq 做稳定次键。
  const ORDER_BY = Object.freeze({
    time: 'm.seq DESC',
    relevance: 'm.seq DESC', // 没有检索词时「相关度」退化成最近
    importance: 'm.importance DESC, m.seq DESC',
    uses: 'COALESCE(m.use_count,0) DESC, m.seq DESC',
  })

  /** 把过滤条件拼成 WHERE；分页/最近列表/计数共用同一套。返回参数是位置化的。 */
  const buildMemoryWhere = (filters = {}) => {
    const kinds = parseList(filters.kind)
    const grades = parseList(filters.grade)
    const statuses = parseList(filters.status)
    const where = []
    const params = []
    if (statuses.length) {
      where.push(`m.status IN (${statuses.map(() => '?').join(',')})`)
      params.push(...statuses)
    } else {
      where.push("m.status <> 'deleted'")
    }
    if (kinds.length) {
      where.push(`m.kind IN (${kinds.map(() => '?').join(',')})`)
      params.push(...kinds)
    }
    if (grades.length) {
      where.push(`${GRADE_SQL} IN (${grades.map(() => '?').join(',')})`)
      params.push(...grades)
    }
    const since = textOrNull(filters.since)
    const until = textOrNull(filters.until)
    if (since) {
      where.push('m.day_key >= ?')
      params.push(since)
    }
    if (until) {
      where.push('m.day_key <= ?')
      params.push(until)
    }
    if (filters.pinned) where.push('m.pinned = 1')
    const group = textOrNull(filters.group)
    if (group) {
      // 含子组：group 本身或其子路径。
      where.push(
        "EXISTS (SELECT 1 FROM grp_member gm JOIN grp g ON g.id=gm.grp_id "
        + "WHERE gm.target_tbl='memory' AND gm.target_id=m.id AND (g.path=? OR g.path LIKE ?))",
      )
      params.push(group, `${group}/%`)
    }
    return { where, params }
  }

  return {
    /**
     * 按写入顺序取最近 N 条（不是相关性排序——「最近」要有确定答案）。
     * filters: { kind, grade, status, since, until, pinned, group }
     */
    recent(limit, filters = {}) {
      const { where, params } = buildMemoryWhere(filters)
      params.push(intIn(limit, 20, 1, 50))
      const sql = `SELECT ${COLUMNS} FROM memory m WHERE ${where.join(' AND ')} ORDER BY m.seq DESC LIMIT ?`
      return withDb(db => {
        const rows = db.prepare(sql).all(...params)
        return rows.map(row => ({ ...row, score: null, truncated: false }))
      })
    },

    /**
     * 真分页：无检索词时用 SQL ORDER BY + LIMIT/OFFSET，返回 total/hasMore。
     * sort: time | importance | uses | relevance（无检索词时 relevance 同 time）。
     */
    page({ limit = 20, offset = 0, sort = 'time', filters = {} } = {}) {
      const { where, params } = buildMemoryWhere(filters)
      const safeLimit = intIn(limit, 20, 1, 50)
      const safeOffset = intIn(offset, 0, 0, 1000)
      const orderBy = ORDER_BY[sort] ?? ORDER_BY.time
      return withDb(db => {
        const total = numberOrNull(
          db.prepare(`SELECT COUNT(*) c FROM memory m WHERE ${where.join(' AND ')}`).get(...params)?.c,
        ) ?? 0
        const rows = db
          .prepare(`SELECT ${COLUMNS} FROM memory m WHERE ${where.join(' AND ')} `
            + `ORDER BY ${orderBy} LIMIT ? OFFSET ?`)
          .all(...params, safeLimit, safeOffset)
        return {
          total,
          items: rows.map(row => ({ ...row, score: null, truncated: false })),
        }
      })
    },

    /** 总览计数：总量 / 今日新增 / 使用次数 / 今日召回 / 近 7 天新增。 */
    counts(dayKey = localDayKey()) {
      return withDb(db => {
        // 单列失败只让那一列为 null，不把整份总览打空。
        const safeCount = (sql, params = []) => {
          try {
            return numberOrNull(db.prepare(sql).get(...params)?.c)
          } catch {
            return null
          }
        }
        const now = new Date()
        const dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0, 0)
        const dayEnd = new Date(dayStart.getTime() + 24 * 60 * 60 * 1000)
        const weekStart = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6)
        return {
          total: safeCount("SELECT COUNT(*) c FROM memory WHERE status<>'deleted'"),
          today_new: safeCount(
            "SELECT COUNT(*) c FROM memory WHERE day_key=? AND status<>'deleted'",
            [String(dayKey)],
          ),
          uses: safeCount('SELECT COALESCE(SUM(use_count),0) c FROM memory'),
          // recall_log.ts 是 UTC ISO；按本机时区的今天切上下界。
          today_recalls: safeCount(
            'SELECT COUNT(*) c FROM recall_log WHERE ts>=? AND ts<?',
            [dayStart.toISOString(), dayEnd.toISOString()],
          ),
          week_new: safeCount(
            "SELECT COUNT(*) c FROM memory WHERE status<>'deleted' AND day_key>=? AND day_key<=?",
            [localDayKey(weekStart), String(dayKey)],
          ),
          pinned: safeCount("SELECT COUNT(*) c FROM memory WHERE pinned=1 AND status<>'deleted'"),
        }
      })
    },

    /**
     * 实体列表：按提及次数/最近出现排序，带 grade/groups（CLI 的
     * `net entities` 没有这两个字段，也没有 offset）。
     */
    entities({ query = '', type = null, limit = 20, offset = 0 } = {}) {
      const where = ["e.status='active'", "e.subject_id='self'"]
      const params = []
      const q = String(query ?? '').trim()
      if (q) {
        where.push('(e.name_norm LIKE ? OR e.name LIKE ?)')
        params.push(`%${q.toLowerCase()}%`, `%${q}%`)
      }
      if (type) {
        where.push('e.type=?')
        params.push(String(type))
      }
      const safeLimit = intIn(limit, 20, 1, 100)
      const safeOffset = intIn(offset, 0, 0, 5000)
      return withDb(db => {
        const total = numberOrNull(
          db.prepare(`SELECT COUNT(*) c FROM entity e WHERE ${where.join(' AND ')}`).get(...params)?.c,
        ) ?? 0
        const rows = db
          .prepare(`SELECT ${ENTITY_COLUMNS} FROM entity e WHERE ${where.join(' AND ')} `
            + 'ORDER BY e.mention_count DESC, e.last_seen DESC LIMIT ? OFFSET ?')
          .all(...params, safeLimit, safeOffset)
        return { total, items: rows.map(row => normalizeEntity(row)) }
      })
    },

    /**
     * 实体详情：基本信息 + 别名 + 提及它的记忆（最近 20 条）+ 计数。
     * 同名多身份时返回 `{conflict:[...]}`，让调用方 409 给前端选，不猜。
     */
    entityByIdOrName(value) {
      const ref = String(value ?? '').trim()
      if (!ref) return null
      return withDb(db => {
        const select = `SELECT ${ENTITY_COLUMNS} FROM entity e`
        let row = ID_RE.test(ref)
          ? db.prepare(`${select} WHERE e.id=? AND e.status='active'`).get(ref)
          : null
        if (!row) {
          const rows = db
            .prepare(`${select} WHERE e.status='active' AND (e.name_norm=? OR e.name=?) `
              + 'ORDER BY e.mention_count DESC, e.last_seen DESC')
            .all(ref, ref)
          if (rows.length > 1) return { conflict: rows.map(item => normalizeEntity(item)) }
          row = rows[0] ?? null
        }
        if (!row) return null

        const aliases = db
          .prepare('SELECT alias,lang,source,confidence,created_at FROM entity_alias WHERE entity_id=? ORDER BY created_at')
          .all(row.id)
        const mentions = db
          .prepare('SELECT m.id, m.text_raw, m.day_key, m.kind, m.status, '
            + 'em.surface, em.confidence, em.extractor FROM entity_mention em '
            + 'JOIN memory m ON m.seq=em.memory_seq '
            + "WHERE em.entity_id=? AND m.status<>'deleted' ORDER BY m.seq DESC LIMIT 20")
          .all(row.id)
        const countOf = sql => numberOrNull(db.prepare(sql).get(row.id)?.c) ?? 0
        return {
          entity: normalizeEntity(row),
          aliases: aliases.map(item => ({
            alias: String(item.alias ?? ''),
            lang: textOrNull(item.lang),
            source: textOrNull(item.source),
            confidence: numberOrNull(item.confidence),
            created_at: textOrNull(item.created_at),
          })),
          mentions: mentions.map(item => ({
            id: String(item.id ?? ''),
            text: String(item.text_raw ?? ''),
            day: textOrNull(item.day_key),
            kind: textOrNull(item.kind),
            status: textOrNull(item.status),
            surface: textOrNull(item.surface),
            confidence: numberOrNull(item.confidence),
            extractor: textOrNull(item.extractor),
          })),
          counts: {
            aliases: countOf('SELECT COUNT(*) c FROM entity_alias WHERE entity_id=?'),
            mentions: countOf('SELECT COUNT(*) c FROM entity_mention WHERE entity_id=?'),
            relations_out: countOf("SELECT COUNT(*) c FROM relation WHERE src_id=? AND status IN ('active','contested')"),
            relations_in: countOf("SELECT COUNT(*) c FROM relation WHERE dst_id=? AND status IN ('active','contested')"),
          },
        }
      })
    },
  }
}

// ---------------------------------------------------------------------------
// 主入口
// ---------------------------------------------------------------------------

/**
 * @param {object} options
 * @param {object} options.config        插件配置（至少 db / budget / cwd）
 * @param {Function} options.callPmem   index.js 里那个带超时/PYTHONPATH 的 CLI 调用器
 * @param {string} [options.version]    插件版本号
 * @param {Function} [options.runtime]  返回公开运行时快照（web.js 的 publicRuntime）
 * @param {Function} [options.wake]     async (budget) => 醒来卡文本
 * @param {Function} [options.stats]    async () => 统计 JSON
 * @param {Function} [options.starmap]  () => 星图摘要
 * @param {Function} [options.snapshot] async ({card}) => web.js 的 /status 快照
 * @param {object}   [options.reader]   覆盖只读读取器（测试用）
 * @param {object}   [options.logger]   { warn(message) }
 */
export function createConsole(options = {}) {
  const {
    config = {},
    callPmem,
    version = '',
    runtime = null,
    wake = null,
    stats = null,
    starmap = null,
    snapshot = null,
    logger = null,
  } = options

  if (typeof callPmem !== 'function') {
    throw new Error('createConsole 需要 callPmem(config, args, opts)')
  }

  // reader === undefined 才自动建；显式传 null 表示「测试里不要碰真库」。
  const reader = options.reader !== undefined ? options.reader : openMemoryReader(config.db)

  const routes = []
  const route = (kind, path, handler) => routes.push({ kind, path, handler })

  function note(message) {
    try {
      if (logger && typeof logger.warn === 'function') logger.warn(`[pmem-console] ${message}`)
    } catch { /* 日志不能影响请求 */ }
  }

  // ---- CLI 调用的统一出口 ---------------------------------------------------
  // 成功返回 { ok:true, data }；失败（含 CLI 以非 0 退出但 stdout 仍是 JSON）
  // 返回 { ok:false, error:{code,message,hint,field,details} }。业务层再决定 HTTP 码。
  function normalizeError(value) {
    if (value && typeof value === 'object') {
      return {
        code: textOrNull(value.code) ?? 'E_PMEM',
        message: String(value.message ?? value.error ?? 'pmem 调用失败'),
        ...(value.hint ? { hint: String(value.hint) } : {}),
        ...(value.field ? { field: String(value.field) } : {}),
        ...(value.details && typeof value.details === 'object' ? { details: value.details } : {}),
      }
    }
    return { code: 'E_PMEM', message: String(value ?? 'pmem 调用失败') }
  }

  function outcome(payload) {
    if (payload && payload.ok === false) {
      return { ok: false, error: normalizeError(payload.error), payload }
    }
    return { ok: true, data: payload?.data ?? payload, payload }
  }

  function parseFailurePayload(error) {
    const raw = String(error?.stdout ?? '').trim()
    if (!raw) return null
    try {
      const parsed = JSON.parse(raw)
      return parsed && typeof parsed === 'object' ? parsed : null
    } catch {
      return null
    }
  }

  async function invoke(args, timeout = 15000) {
    try {
      const payload = await callPmem(config, args, { json: true, timeout })
      return outcome(payload)
    } catch (error) {
      const parsed = parseFailurePayload(error)
      if (parsed) return outcome(parsed)
      return {
        ok: false,
        error: { code: 'E_SPAWN', message: String(error?.message ?? error) },
      }
    }
  }

  function statusFor(error) {
    const code = String(error?.code ?? '')
    if (code === 'E_BAD_JSON' || code === 'E_TOO_LARGE') return 400
    return HTTP_STATUS[code] ?? 500
  }

  /** 统一错误响应体：把 CLI 的 code/hint/field/details 一并带给前端。 */
  function errorBody(error, extra = {}) {
    const body = { ok: false, error: String(error?.message ?? error), ...extra }
    if (error?.code) body.code = String(error.code)
    if (error?.hint) body.hint = String(error.hint)
    if (error?.field) body.field = String(error.field)
    if (error?.details && typeof error.details === 'object') body.details = error.details
    return body
  }

  function fail(res, error) {
    const status = Number(error?.statusCode) || statusFor(error)
    return json(res, status, errorBody(error))
  }

  /** 读 body；失败时已经把响应写完了，返回 null 让调用方直接 return。 */
  async function bodyOrRespond(req, res) {
    try {
      return await readJsonBody(req)
    } catch (error) {
      fail(res, error)
      return null
    }
  }

  function matchesFilters(item, { kind, grade, status, pinned }) {
    if (kind.length && !kind.includes(String(item?.kind ?? ''))) return false
    if (grade.length && !grade.includes(String(item?.grade ?? ''))) return false
    if (status.length && !status.includes(String(item?.status ?? ''))) return false
    if (pinned && !(item?.pinned === true || item?.pinned === 1)) return false
    return true
  }

  /** YYYY-MM-DD 校验：非法返回 400，空值返回 null。 */
  function normalizeDayParam(value, field) {
    const raw = String(value ?? '').trim()
    if (!raw) return null
    if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
      throw validationError(`${field} 必须是 YYYY-MM-DD`, field)
    }
    const parsed = new Date(`${raw}T00:00:00Z`)
    if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== raw) {
      throw validationError(`${field} 不是合法日期`, field)
    }
    return raw
  }

  /** 检索结果的后端排序：相关度/时间/重要度/使用次数，seq 不可得时用 id 做稳定次键。 */
  function sortMemoryItems(items, sort) {
    const scoreOf = item => numberOrNull(item?.score) ?? 0
    const dayOf = item => String(item?.day ?? item?.valid_from ?? '')
    const importanceOf = item => numberOrNull(item?.importance) ?? 0
    const usesOf = item => numberOrNull(item?.use_count) ?? 0
    const idOf = item => String(item?.id ?? '')
    const byScore = (a, b) => scoreOf(b) - scoreOf(a)
    const byDay = (a, b) => dayOf(b).localeCompare(dayOf(a))
    const byImportance = (a, b) => importanceOf(b) - importanceOf(a)
    const byUses = (a, b) => usesOf(b) - usesOf(a)
    const list = [...items]
    if (sort === 'time') {
      return list.sort((a, b) => byDay(a, b) || byScore(a, b) || idOf(b).localeCompare(idOf(a)))
    }
    if (sort === 'importance') {
      return list.sort((a, b) => byImportance(a, b) || byScore(a, b) || idOf(b).localeCompare(idOf(a)))
    }
    if (sort === 'uses') {
      return list.sort((a, b) => byUses(a, b) || byScore(a, b) || idOf(b).localeCompare(idOf(a)))
    }
    return list.sort((a, b) => byScore(a, b) || byDay(a, b) || idOf(b).localeCompare(idOf(a)))
  }

  /** 有 q 时 CLI search 不带日期过滤，这里按 day/valid_from 回补。 */
  function inDayRange(item, since, until) {
    if (!since && !until) return true
    const day = String(item?.day ?? item?.valid_from ?? '').slice(0, 10)
    if (!day) return false
    if (since && day < since) return false
    if (until && day > until) return false
    return true
  }

  function profileOf(out) {
    if (!out?.ok) {
      return {
        traits: [],
        goals: [],
        open_goals: [],
        rendered: '',
        tokens: 0,
        budget: null,
        truncated: false,
      }
    }
    const data = out.data ?? {}
    const traits = Array.isArray(data.traits) ? data.traits : []
    const goals = Array.isArray(data.goals) ? data.goals : []
    return {
      traits,
      goals,
      // 契约 §5.1 里叫 open_goals；CLI 里叫 goals，两个都给，前端都能用。
      open_goals: goals,
      rendered: String(data.rendered ?? ''),
      tokens: numberOrNull(data.tokens) ?? 0,
      budget: numberOrNull(data.budget),
      truncated: Boolean(data.truncated),
    }
  }

  // ---- 最近列表的降级路径 ---------------------------------------------------
  async function recentFallback(limit, filters) {
    if ((filters?.kind?.length ?? 0) || (filters?.grade?.length ?? 0) || (filters?.status?.length ?? 0)) {
      return []
    }
    const out = await invoke(['stats'], 15000)
    const recent = out.ok && Array.isArray(out.data?.recent) ? out.data.recent : []
    return recent.slice(0, limit).map(row => ({ ...row, score: null, truncated: false }))
  }

  // ---- 记忆元数据修改（单条 update 与 batch update 共用） -------------------
  const SENSITIVITIES = ['public', 'personal', 'secret']

  function validationError(message, field = null) {
    const error = new Error(message)
    error.code = 'E_VALIDATION'
    error.statusCode = 400
    if (field) error.field = field
    return error
  }

  /**
   * 校验并归一化 update patch。字段白名单由调用方给：
   *   · 单条 update：UPDATE_FIELDS（含 pinned / reason）
   *   · batch update：BATCH_UPDATE_FIELDS（六个元数据字段）
   * 非法字段/值一律 400，不做部分应用——先校验完再动手。
   */
  function normalizeUpdatePatch(input, allowed) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) {
      throw validationError('patch 必须是 JSON 对象')
    }
    const patch = {}
    for (const key of Object.keys(input)) {
      if (!allowed.includes(key)) {
        throw validationError(`不支持更新字段 ${key}`, key)
      }
      const value = input[key]
      if (value === undefined || value === null) continue
      if (key === 'kind') {
        const kind = String(value).trim()
        if (!MEMORY_KINDS.includes(kind)) {
          throw validationError(`kind 只能是 ${MEMORY_KINDS.join(' / ')}`, 'kind')
        }
        patch.kind = kind
      } else if (key === 'status') {
        const status = String(value).trim()
        if (!MEMORY_STATUSES.includes(status)) {
          throw validationError(`status 只能是 ${MEMORY_STATUSES.join(' / ')}`, 'status')
        }
        patch.status = status
      } else if (key === 'sensitivity') {
        const sensitivity = String(value).trim()
        if (!SENSITIVITIES.includes(sensitivity)) {
          throw validationError(`sensitivity 只能是 ${SENSITIVITIES.join(' / ')}`, 'sensitivity')
        }
        patch.sensitivity = sensitivity
      } else if (key === 'importance') {
        const importance = Number(value)
        if (!Number.isFinite(importance)) {
          throw validationError('importance 必须是数字', 'importance')
        }
        patch.importance = Math.min(1, Math.max(0, importance))
      } else if (key === 'pinned') {
        if (typeof value !== 'boolean') {
          throw validationError('pinned 必须是布尔值', 'pinned')
        }
        patch.pinned = value
      } else if (key === 'grade') {
        const grade = String(value).trim()
        if (!GRADES.includes(grade)) {
          throw validationError(`grade 只能是 ${GRADES.join(' / ')}`, 'grade')
        }
        patch.grade = grade
      } else if (key === 'group') {
        const group = safeValue(value, PATH_MAX)
        if (!group) {
          throw validationError('group 不合法（不得为空、不得以 - 开头）', 'group')
        }
        patch.group = group
      } else if (key === 'reason') {
        patch.reason = String(value).replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, 500)
      }
    }
    return patch
  }

  function failureOf(out) {
    return {
      code: out.error.code,
      message: out.error.message,
      ...(out.error.hint ? { hint: out.error.hint } : {}),
    }
  }

  /**
   * 应用一份已校验的 patch，返回 { applied, failed }。
   * 标量字段一次 `pmem update`；group/grade 一次 `pmem group assign`。
   * 两组互不连坐：kind 成功但 group 失败也如实记。
   */
  async function applyMemoryPatch(id, patch) {
    const applied = {}
    const failed = []

    const scalarKeys = ['kind', 'status', 'sensitivity', 'importance', 'pinned']
      .filter(key => patch[key] !== undefined)
    if (scalarKeys.length) {
      const argv = ['update', id]
      if (patch.kind !== undefined) argv.push('--kind', patch.kind)
      if (patch.status !== undefined) argv.push('--status', patch.status)
      if (patch.sensitivity !== undefined) argv.push('--sensitivity', patch.sensitivity)
      if (patch.importance !== undefined) argv.push('--importance', String(patch.importance))
      if (patch.pinned !== undefined) argv.push(patch.pinned ? '--pin' : '--unpin')
      if (patch.reason) argv.push('--reason', patch.reason)
      const out = await invoke(argv, 20000)
      if (out.ok) {
        for (const key of scalarKeys) applied[key] = patch[key]
      } else {
        for (const key of scalarKeys) failed.push({ field: key, error: failureOf(out) })
      }
    }

    const groupKeys = ['group', 'grade'].filter(key => patch[key] !== undefined)
    if (groupKeys.length) {
      const argv = ['group', 'assign', 'memory', id]
      if (patch.group !== undefined) argv.push('--path', patch.group)
      if (patch.grade !== undefined) argv.push('--grade', patch.grade)
      argv.push('--rationale', patch.reason || '控制台手动调整')
      const out = await invoke(argv, 20000)
      if (out.ok) {
        for (const key of groupKeys) applied[key] = patch[key]
      } else {
        for (const key of groupKeys) failed.push({ field: key, error: failureOf(out) })
      }
    }

    return { applied, failed }
  }

  // ==========================================================================
  // GET /bootstrap
  // ==========================================================================
  route('exact', `${CONSOLE_API}/bootstrap`, async (req, res) => {
    if (!requireGet(req, res)) return
    try {
      const budget = intIn(config.budget, 300, 80, 1200)
      const card = typeof wake === 'function'
        ? await Promise.resolve().then(() => wake(budget)).catch(() => '')
        : ''
      const [snap, profileOut, sleepOut, conflictOut] = await Promise.all([
        typeof snapshot === 'function'
          ? Promise.resolve().then(() => snapshot({ card })).catch(() => null)
          : null,
        invoke(['profile', '--budget', String(budget)], 20000),
        invoke(['sleep', 'log', '--limit', '5'], 30000),
        invoke(['conflicts', 'list', '--status', 'open', '--limit', '100'], 20000),
      ])

      const degraded = []
      let status = snap
      if (!status || typeof status !== 'object') {
        const dbOut = await invoke(['stats'], 15000)
        if (!dbOut.ok) degraded.push('stats')
        status = {
          ok: true,
          version,
          runtime: typeof runtime === 'function' ? safe(runtime) : null,
          db: dbOut.ok ? dbOut.data : null,
          wake: {
            tokens: Math.ceil(String(card).length / 3),
            chars: String(card).length,
            head: String(card).split('\n')[1] ?? '',
          },
          starmap: typeof starmap === 'function' ? safe(starmap, { ready: false }) : { ready: false },
        }
      }

      const counts = reader?.counts ? reader.counts(localDayKey()) : null
      let recentRows = null
      try {
        recentRows = reader?.recent ? reader.recent(10) : null
      } catch {
        recentRows = null
      }
      if (!Array.isArray(recentRows)) {
        recentRows = Array.isArray(status?.db?.recent) ? status.db.recent : []
        recentRows = recentRows.map(row => ({ ...row, score: null, truncated: false }))
      }

      if (!profileOut.ok) degraded.push('profile')
      if (!sleepOut.ok) degraded.push('sleep')
      if (!conflictOut.ok) degraded.push('conflicts')
      if (!counts) degraded.push('counts')

      const runs = sleepOut.ok && Array.isArray(sleepOut.data?.runs) ? sleepOut.data.runs : []
      const conflicts = conflictOut.ok && Array.isArray(conflictOut.data?.items)
        ? conflictOut.data.items
        : []
      const db = status?.db ?? null

      return json(res, 200, {
        ok: true,
        version,
        status,
        wake: {
          text: String(card),
          tokens: Math.ceil(String(card).length / 3),
          chars: String(card).length,
          head: String(card).split('\n')[1] ?? '',
        },
        profile: profileOf(profileOut),
        recent: recentRows.slice(0, 10).map(normalizeMemory),
        counts: {
          memory_total: numberOrNull(db?.memory_total) ?? numberOrNull(counts?.total),
          today_new: numberOrNull(counts?.today_new),
          open_conflicts: conflictOut.ok ? conflicts.length : null,
          uses: numberOrNull(counts?.uses),
          event_count: numberOrNull(db?.event_count),
          today_recalls: numberOrNull(counts?.today_recalls),
          week_new: numberOrNull(counts?.week_new),
          pinned: numberOrNull(counts?.pinned),
        },
        starmap: status?.starmap ?? (typeof starmap === 'function' ? safe(starmap, { ready: false }) : { ready: false }),
        sleep: {
          runs: runs.length,
          latest: runs[0] ?? null,
          truncated: sleepOut.ok ? false : null,
        },
        degraded,
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // ==========================================================================
  // GET /memories?q=&k=&limit=&offset=&sort=&mode=&kind=&grade=&status=
  //              &group=&pinned=&since=&until=
  // ==========================================================================
  route('exact', `${CONSOLE_API}/memories`, async (req, res) => {
    if (!requireGet(req, res)) return
    try {
      const url = urlOf(req)
      const query = String(url.searchParams.get('q') ?? '').slice(0, 200).trim()
      const limit = intIn(url.searchParams.get('limit') ?? url.searchParams.get('k'), 20, 1, 50)
      const offset = intIn(url.searchParams.get('offset'), 0, 0, 1000)
      const mode = String(url.searchParams.get('mode') ?? 'auto').trim() || 'auto'
      if (!MEMORY_MODES.includes(mode)) {
        return json(res, 400, { ok: false, error: `mode 只能是 ${MEMORY_MODES.join(' / ')}` })
      }
      const sortParam = String(url.searchParams.get('sort') ?? '').trim()
      const sort = sortParam || (query ? 'relevance' : 'time')
      if (!MEMORY_SORTS.includes(sort)) {
        return json(res, 400, { ok: false, error: `sort 只能是 ${MEMORY_SORTS.join(' / ')}` })
      }
      const kind = parseList(url.searchParams.getAll('kind').length
        ? url.searchParams.getAll('kind')
        : url.searchParams.get('kind'))
      const grade = parseList(url.searchParams.getAll('grade').length
        ? url.searchParams.getAll('grade')
        : url.searchParams.get('grade'))
      const status = parseList(url.searchParams.getAll('status').length
        ? url.searchParams.getAll('status')
        : url.searchParams.get('status'))
      const group = String(url.searchParams.get('group') ?? '').trim().slice(0, PATH_MAX) || null
      const pinned = ['1', 'true', 'yes', 'on'].includes(
        String(url.searchParams.get('pinned') ?? '').toLowerCase(),
      )
      const badGrade = grade.find(value => !GRADES.includes(value))
      if (badGrade) {
        return json(res, 400, { ok: false, error: `grade 只能是 ${GRADES.join(' / ')}：${badGrade}` })
      }
      if (group && group.startsWith('-')) {
        return json(res, 400, { ok: false, error: 'group 不能以 - 开头' })
      }
      const since = normalizeDayParam(url.searchParams.get('since'), 'since')
      const until = normalizeDayParam(url.searchParams.get('until'), 'until')
      if (since && until && since > until) {
        return json(res, 400, { ok: false, error: 'since 不能晚于 until' })
      }

      const filters = { kind, grade, status, pinned, group, since, until }
      // 空查询 = 「看最近」；显式 mode=recent 也是同一条路。
      const effective = mode === 'recent' || !query ? 'recent' : mode
      // 无检索词时「相关度」退化成时间倒序；显式 sort 仍按请求值展示。
      const effectiveSort = effective === 'recent' && sort === 'relevance' ? 'time' : sort

      if (effective === 'recent') {
        let page = null
        if (reader?.page) {
          try {
            page = reader.page({ limit, offset, sort: effectiveSort, filters })
          } catch {
            page = null
          }
        }
        if (page && Array.isArray(page.items)) {
          const items = page.items.map(normalizeMemory)
          const total = numberOrNull(page.total) ?? items.length
          return json(res, 200, {
            ok: true,
            query,
            mode,
            sort: effectiveSort,
            offset,
            limit,
            k: limit,
            effective: 'recent',
            items,
            total,
            hasMore: offset + items.length < total,
          })
        }
        // 旧 reader / 统计降级：没有 page 能力时用 recent(offset+limit) 再切。
        let rows = null
        if (reader?.recent) {
          try {
            rows = reader.recent(Math.min(50, offset + limit), filters)
          } catch {
            rows = null
          }
        }
        if (!Array.isArray(rows)) rows = await recentFallback(limit, filters)
        const sorted = sortMemoryItems(rows, effectiveSort)
        const total = sorted.length
        const items = sorted.slice(offset, offset + limit).map(normalizeMemory)
        return json(res, 200, {
          ok: true,
          query,
          mode,
          sort: effectiveSort,
          offset,
          limit,
          k: limit,
          effective: 'recent',
          items,
          total,
          hasMore: offset + items.length < total,
        })
      }

      // 有 q：先按 min(200, offset+limit) 取一个池子，再后端排序/切片。
      const fetchK = Math.min(200, Math.max(limit, offset + limit))
      const argv = ['search', query, '--k', String(fetchK), '--mode', effective]
      for (const value of kind) argv.push('--kind', value)
      for (const value of grade) argv.push('--grade', value)
      if (group) argv.push('--group', group)
      if (pinned) argv.push('--pinned-only')
      const out = await invoke(argv, 20000)
      if (!out.ok) {
        return json(res, statusFor(out.error), {
          ok: false,
          error: out.error.message,
          code: out.error.code,
        })
      }
      const raw = Array.isArray(out.data?.items) ? out.data.items : []
      const sorted = sortMemoryItems(
        raw
          .filter(item => matchesFilters(item, filters))
          .filter(item => inDayRange(item, since, until)),
        effectiveSort,
      )
      const total = sorted.length
      const items = sorted.slice(offset, offset + limit).map(normalizeMemory)
      // 池子被 --k 填满就认为可能还有下一页（否则前端第一页永远拿不到 hasMore）。
      const windowFilled = fetchK < 200 && raw.length >= fetchK
      const capped = fetchK >= 200 && raw.length >= 200
      return json(res, 200, {
        ok: true,
        query,
        mode,
        sort: effectiveSort,
        offset,
        limit,
        k: limit,
        effective,
        items,
        total,
        hasMore: offset + items.length < total || windowFilled,
        ...(capped ? { capped: true } : {}),
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // ==========================================================================
  // GET /memory?id=
  // ==========================================================================
  route('exact', `${CONSOLE_API}/memory`, async (req, res) => {
    if (!requireGet(req, res)) return
    try {
      const id = validateId(urlOf(req).searchParams.get('id'))
      if (!id) {
        return json(res, 400, { ok: false, error: 'id 不合法（1-64 位字母/数字/_/-）' })
      }
      const [getOut, historyOut, conflictOut] = await Promise.all([
        invoke(['get', id], 15000),
        invoke(['history', id, '--limit', '50'], 15000),
        invoke(['conflicts', 'list', '--status', 'open', '--limit', '200'], 20000),
      ])
      if (!getOut.ok) {
        return json(res, statusFor(getOut.error), {
          ok: false,
          error: getOut.error.message,
          code: getOut.error.code,
          ...(getOut.error.hint ? { hint: getOut.error.hint } : {}),
        })
      }

      const data = getOut.data ?? {}
      const item = {
        ...normalizeMemory(data),
        // 详情页要完整正文（列表里的 text 也是全文，但这里显式再取一次）。
        text: String(data.text ?? data.text_raw ?? ''),
        pinned: data.pinned === true || data.pinned === 1,
        sensitivity: textOrNull(data.sensitivity),
        created_at: textOrNull(data.created_at),
        recorded_at: textOrNull(data.recorded_at),
        last_used_at: textOrNull(data.last_used_at),
        use_count: numberOrNull(data.use_count),
        evidence_count: numberOrNull(data.evidence_count),
        scope_key: textOrNull(data.scope_key),
        claim_sig: textOrNull(data.claim_sig),
        canonical_id: textOrNull(data.canonical_id),
        supersedes_id: textOrNull(data.supersedes_id),
        superseded_by: textOrNull(data.superseded_by),
        retracted: data.retracted === true,
        source: data.source ?? null,
        extra: data.extra ?? null,
      }

      const history = (historyOut.ok && Array.isArray(historyOut.data?.items)
        ? historyOut.data.items
        : []).map(entry => ({
        at: textOrNull(entry.ts ?? entry.at),
        action: textOrNull(entry.op ?? entry.action),
        actor: textOrNull(entry.actor),
        note: textOrNull(entry.payload?.rationale ?? entry.payload?.reason ?? entry.note),
        payload: entry.payload ?? null,
      }))

      // evidence：用正文第一行（最多 60 字）去原话流水里按字查（trace 是精确
      // 子串，不能先把换行/空白压掉，否则匹配不上）。查不到就给空数组。
      let evidence = []
      const probe = String(item.text)
        .split('\n')
        .map(line => line.trim())
        .find(line => line.length >= 3)?.slice(0, 60) ?? ''
      if (probe.length >= 3) {
        const traceOut = await invoke(['trace', probe, '--limit', '10'], 15000)
        if (traceOut.ok && Array.isArray(traceOut.data?.items)) {
          evidence = traceOut.data.items.slice(0, 10).map(entry => ({
            session: textOrNull(entry.session_id ?? entry.session),
            line: numberOrNull(entry.line_no ?? entry.line),
            role: textOrNull(entry.role),
            at: textOrNull(entry.ts),
            text: String(entry.text ?? ''),
            id: textOrNull(entry.id),
            retracted: entry.retracted === true,
          }))
        }
      }

      const conflicts = (conflictOut.ok && Array.isArray(conflictOut.data?.items)
        ? conflictOut.data.items
        : [])
        .filter(entry => entry?.left === id || entry?.right === id)
        .map(entry => ({
          id: textOrNull(entry.id),
          kind: textOrNull(entry.kind),
          slot: textOrNull(entry.slot),
          severity: numberOrNull(entry.severity),
          status: textOrNull(entry.status),
          other: entry.left === id ? textOrNull(entry.right) : textOrNull(entry.left),
          created_at: textOrNull(entry.created_at),
        }))

      const degraded = []
      if (!historyOut.ok) degraded.push('history')
      if (!conflictOut.ok) degraded.push('conflicts')

      return json(res, 200, { ok: true, item, history, evidence, conflicts, degraded })
    } catch (error) {
      return fail(res, error)
    }
  })

  // ==========================================================================
  // POST /memory/feedback
  // ==========================================================================
  route('exact', `${CONSOLE_API}/memory/feedback`, async (req, res) => {
    if (!requirePost(req, res)) return
    const body = await bodyOrRespond(req, res)
    if (!body) return
    try {
      const id = validateId(body.id)
      if (!id) return json(res, 400, { ok: false, error: 'id 不合法' })
      const signal = String(body.signal ?? '').trim()
      if (!FEEDBACK_SIGNALS.includes(signal)) {
        return json(res, 400, {
          ok: false,
          error: `signal 只能是 ${FEEDBACK_SIGNALS.join(' / ')}`,
        })
      }
      const note = body.note === null || body.note === undefined
        ? null
        : String(body.note).slice(0, 500)
      const argv = ['feedback', id, signal]
      if (note) argv.push('--note', note)
      const out = await invoke(argv, 20000)
      if (!out.ok) {
        return json(res, statusFor(out.error), {
          ok: false,
          error: out.error.message,
          code: out.error.code,
        })
      }
      return json(res, 200, { ok: true, id, signal, data: out.data })
    } catch (error) {
      return fail(res, error)
    }
  })

  // ==========================================================================
  // POST /memory/update
  // ==========================================================================
  route('exact', `${CONSOLE_API}/memory/update`, async (req, res) => {
    if (!requirePost(req, res)) return
    const body = await bodyOrRespond(req, res)
    if (!body) return
    try {
      const id = validateId(body.id)
      if (!id) return json(res, 400, { ok: false, error: 'id 不合法' })
      const { id: _id, ...patchInput } = body
      const patch = normalizeUpdatePatch(patchInput, UPDATE_FIELDS)
      const stateKeys = Object.keys(patch).filter(key => key !== 'reason')
      if (!stateKeys.length) {
        return json(res, 400, {
          ok: false,
          error: `没有可更新的字段（${UPDATE_FIELDS.filter(key => key !== 'reason').join(' / ')}）`,
        })
      }

      const { applied, failed } = await applyMemoryPatch(id, patch)
      const appliedFields = Object.keys(applied)
      const fresh = appliedFields.length
        ? await invoke(['get', id], 15000)
        : { ok: false }
      const status = !appliedFields.length && failed.length ? statusFor(failed[0].error) : 200
      return json(res, status, {
        ok: failed.length === 0,
        id,
        applied,
        failed,
        updated: appliedFields.length > 0,
        item: fresh.ok ? normalizeMemory(fresh.data) : null,
        ...(appliedFields.length && !fresh.ok ? { warning: '更新成功，但重新读取失败' } : {}),
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // ==========================================================================
  // POST /memory/delete
  // ==========================================================================
  route('exact', `${CONSOLE_API}/memory/delete`, async (req, res) => {
    if (!requirePost(req, res)) return
    const body = await bodyOrRespond(req, res)
    if (!body) return
    try {
      const id = validateId(body.id)
      if (!id) return json(res, 400, { ok: false, error: 'id 不合法' })
      if (body.hard !== undefined && body.hard !== null && typeof body.hard !== 'boolean') {
        return json(res, 400, { ok: false, error: 'hard 必须是布尔值' })
      }
      const hard = body.hard === true
      const argv = ['delete', id]
      if (hard) argv.push('--hard')
      const out = await invoke(argv, 30000)
      if (!out.ok) {
        return json(res, statusFor(out.error), {
          ok: false,
          error: out.error.message,
          code: out.error.code,
        })
      }
      return json(res, 200, {
        ok: true,
        id,
        hard,
        deleted: numberOrNull(out.data?.deleted) ?? 0,
        cascaded: out.data?.cascaded ?? null,
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // ==========================================================================
  // GET /profile?budget=
  // ==========================================================================
  route('exact', `${CONSOLE_API}/profile`, async (req, res) => {
    if (!requireGet(req, res)) return
    try {
      const budget = intIn(
        urlOf(req).searchParams.get('budget'),
        intIn(config.budget, 300, 80, 1200),
        80,
        1200,
      )
      const out = await invoke(['profile', '--budget', String(budget)], 20000)
      if (!out.ok) {
        return json(res, statusFor(out.error), {
          ok: false,
          error: out.error.message,
          code: out.error.code,
        })
      }
      return json(res, 200, { ok: true, budget, profile: profileOf(out) })
    } catch (error) {
      return fail(res, error)
    }
  })

  // ==========================================================================
  // GET /groups[?path=&tbl=&limit=]
  // ==========================================================================
  route('exact', `${CONSOLE_API}/groups`, async (req, res) => {
    if (!requireGet(req, res)) return
    try {
      const url = urlOf(req)
      const path = String(url.searchParams.get('path') ?? '').trim().slice(0, PATH_MAX)
      const tbl = String(url.searchParams.get('tbl') ?? 'memory').trim() || 'memory'
      if (!GROUP_TABLES.includes(tbl)) {
        return json(res, 400, { ok: false, error: `tbl 只能是 ${GROUP_TABLES.join(' / ')}` })
      }
      if (path && path.startsWith('-')) {
        return json(res, 400, { ok: false, error: 'path 不能以 - 开头' })
      }
      const limit = intIn(url.searchParams.get('limit'), 100, 1, 500)

      if (path) {
        const out = await invoke(['group', 'targets', path, '--tbl', tbl, '--limit', String(limit)], 20000)
        if (!out.ok) {
          return json(res, statusFor(out.error), {
            ok: false,
            error: out.error.message,
            code: out.error.code,
          })
        }
        return json(res, 200, {
          ok: true,
          path,
          tbl,
          limit,
          items: Array.isArray(out.data?.items) ? out.data.items : [],
        })
      }

      const [treeOut, statsOut] = await Promise.all([
        invoke(['group', 'tree'], 20000),
        invoke(['group', 'stats'], 20000),
      ])
      const degraded = []
      if (!treeOut.ok) degraded.push('tree')
      if (!statsOut.ok) degraded.push('stats')
      return json(res, 200, {
        ok: true,
        tree: treeOut.ok && Array.isArray(treeOut.data?.items) ? treeOut.data.items : [],
        stats: statsOut.ok ? statsOut.data : null,
        degraded,
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // ==========================================================================
  // GET /conflicts?status=&limit=
  // ==========================================================================
  route('exact', `${CONSOLE_API}/conflicts`, async (req, res) => {
    if (!requireGet(req, res)) return
    try {
      const url = urlOf(req)
      const status = String(url.searchParams.get('status') ?? 'open').trim() || 'open'
      if (!TOKEN_RE.test(status)) {
        return json(res, 400, { ok: false, error: 'status 不合法' })
      }
      const limit = intIn(url.searchParams.get('limit'), 20, 1, 200)
      const out = await invoke(['conflicts', 'list', '--status', status, '--limit', String(limit)], 20000)
      if (!out.ok) {
        return json(res, statusFor(out.error), {
          ok: false,
          error: out.error.message,
          code: out.error.code,
        })
      }
      return json(res, 200, {
        ok: true,
        status,
        limit,
        items: Array.isArray(out.data?.items) ? out.data.items : [],
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // ==========================================================================
  // GET /stats
  // ==========================================================================
  route('exact', `${CONSOLE_API}/stats`, async (req, res) => {
    if (!requireGet(req, res)) return
    try {
      const [dbOut, groupOut, receiptOut, netOut, conflictOut] = await Promise.all([
        invoke(['stats'], 15000),
        invoke(['group', 'stats'], 20000),
        invoke(['receipt', '--stats'], 15000),
        invoke(['net', 'stats'], 15000),
        invoke(['conflicts', 'list', '--status', 'open', '--limit', '100'], 20000),
      ])
      const degraded = []
      if (!dbOut.ok) degraded.push('stats')
      if (!groupOut.ok) degraded.push('groups')
      if (!receiptOut.ok) degraded.push('receipt')
      if (!netOut.ok) degraded.push('net')
      if (!conflictOut.ok) degraded.push('conflicts')

      if (!dbOut.ok) {
        return json(res, statusFor(dbOut.error), {
          ok: false,
          error: dbOut.error.message,
          code: dbOut.error.code,
        })
      }

      const counts = reader?.counts ? reader.counts(localDayKey()) : null
      const openConflicts = conflictOut.ok && Array.isArray(conflictOut.data?.items)
        ? conflictOut.data.items.length
        : null
      return json(res, 200, {
        ok: true,
        version,
        stats: dbOut.data,
        groups: groupOut.ok ? groupOut.data : null,
        receipt: receiptOut.ok ? receiptOut.data : null,
        net: netOut.ok ? netOut.data : null,
        conflicts: { open: openConflicts },
        counts: counts ?? null,
        degraded,
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // ==========================================================================
  // GET /doctor
  // ==========================================================================
  route('exact', `${CONSOLE_API}/doctor`, async (req, res) => {
    if (!requireGet(req, res)) return
    try {
      const [doctorOut, statsOut] = await Promise.all([
        invoke(['doctor'], 30000),
        invoke(['stats'], 15000),
      ])
      if (!doctorOut.ok) {
        return json(res, statusFor(doctorOut.error), {
          ok: false,
          error: doctorOut.error.message,
          code: doctorOut.error.code,
        })
      }
      const db = statsOut.ok ? statsOut.data : null
      return json(res, 200, {
        ok: true,
        version,
        doctor: doctorOut.data,
        stats: db
          ? {
            memory_total: numberOrNull(db.memory_total),
            db_size_bytes: numberOrNull(db.db_size_bytes),
            db_path: textOrNull(db.db_path),
            schema_version: textOrNull(db.schema_version),
            integrity_ok: db.integrity_ok === true,
          }
          : null,
        ...(statsOut.ok ? {} : { warning: '统计信息没取到' }),
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // ==========================================================================
  // P1：实体 / 关系 / 备份 / 批量 / 向量重建 / 运维白名单
  //
  // 共同约束：所有用户输入都过 safeValue / 白名单 / 数值夹取，再以 argv 数组
  // 交给 execFile（index.js 的 callPmem），不拼 shell；写路由只收 POST。
  // ==========================================================================

  /** 实体列表降级：没有 node:sqlite 时用 CLI（没有 grade/groups/offset）。 */
  async function entityListFallback({ query, type, limit, offset }) {
    const fetchLimit = Math.min(500, offset + limit)
    const argv = ['net', 'entities']
    if (type) argv.push('--type', type)
    if (query) argv.push('--query', query)
    argv.push('--limit', String(fetchLimit))
    const out = await invoke(argv, 20000)
    if (!out.ok) return out
    const raw = Array.isArray(out.data?.items) ? out.data.items : []
    return {
      ok: true,
      data: {
        total: raw.length,
        items: raw.slice(offset, offset + limit).map(item => normalizeEntity(item)),
        fallback: true,
      },
    }
  }

  /** 实体详情降级：CLI 只能给 brief + 关系，别名/提及拿不到（标 degraded）。 */
  async function entityDetailFallback(ref) {
    const out = await invoke(['net', 'query', ref, '--depth', '1', '--limit', '100'], 20000)
    if (!out.ok) return out
    const src = out.data?.src ?? null
    if (!src) {
      return { ok: false, error: { code: 'E_NOT_FOUND', message: `找不到实体 ${ref}` } }
    }
    const entityId = String(src.id)
    const { relations, degraded } = await fetchEntityRelations(entityId)
    return {
      ok: true,
      data: {
        entity: normalizeEntity(src),
        aliases: [],
        mentions: [],
        counts: { aliases: 0, mentions: 0, relations_out: relations.filter(item => item.direction === 'out').length, relations_in: relations.filter(item => item.direction === 'in').length },
        relations,
        degraded: ['aliases', 'mentions', ...degraded],
        fallback: true,
      },
    }
  }

  /** 1 跳关系：出边 + 入边各查一次，合并后标方向。 */
  async function fetchEntityRelations(entityId) {
    const [outward, inward] = await Promise.all([
      invoke(['net', 'query', entityId, '--depth', '1', '--limit', '100'], 20000),
      invoke(['net', 'query', '--dst', entityId, '--depth', '1', '--limit', '100'], 20000),
    ])
    const degraded = []
    if (!outward.ok) degraded.push('relations_out')
    if (!inward.ok) degraded.push('relations_in')
    const items = [
      ...relationsWithDirection(outward.ok ? outward.data?.items : [], entityId),
      ...relationsWithDirection(inward.ok ? inward.data?.items : [], entityId),
    ]
    const seen = new Set()
    const relations = items.filter(item => {
      const key = `${item?.relation_id ?? ''}|${item?.src?.id ?? ''}|${item?.dst?.id ?? ''}|${item?.predicate ?? ''}`
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    return { relations, degraded }
  }

  /** 合并用：解析实体 → 实体 or {conflict} or {error}。 */
  async function resolveEntityForMerge(ref) {
    if (reader?.entityByIdOrName) {
      const detail = reader.entityByIdOrName(ref)
      if (detail?.conflict) return { conflict: detail.conflict }
      if (detail?.entity) return { entity: detail.entity, counts: detail.counts }
    }
    const out = await invoke(['net', 'query', ref, '--depth', '1', '--limit', '1'], 20000)
    if (!out.ok) return { error: out.error }
    if (!out.data?.src) return { notFound: true }
    return { entity: normalizeEntity(out.data.src), counts: null }
  }

  // --------------------------------------------------------------------------
  // GET /entities?q=&type=&limit=&offset=
  // --------------------------------------------------------------------------
  route('exact', `${CONSOLE_API}/entities`, async (req, res) => {
    if (!requireGet(req, res)) return
    try {
      const url = urlOf(req)
      const query = String(url.searchParams.get('q') ?? '').slice(0, 200).trim()
      const typeRaw = String(url.searchParams.get('type') ?? '').trim()
      const limit = intIn(url.searchParams.get('limit'), 20, 1, 100)
      const offset = intIn(url.searchParams.get('offset'), 0, 0, 5000)
      let type = null
      if (typeRaw) {
        type = safeRegexValue(typeRaw, TYPE_RE, 32)
        if (!type) return json(res, 400, { ok: false, error: 'type 不合法' })
      }

      const fallback = { query, type, limit, offset }
      const result = reader?.entities
        ? reader.entities(fallback)
        : await entityListFallback(fallback)
      if (!result || result?.ok === false) {
        return result?.ok === false
          ? json(res, statusFor(result.error), errorBody(result.error))
          : json(res, 500, { ok: false, error: '实体列表读取失败' })
      }
      const data = result.items ? result : result.data
      const items = Array.isArray(data?.items) ? data.items : []
      const total = numberOrNull(data?.total) ?? items.length
      return json(res, 200, {
        ok: true,
        query,
        type,
        offset,
        limit,
        total,
        hasMore: offset + items.length < total,
        items,
        ...(data?.fallback ? { degraded: ['grade', 'groups', 'offset'] } : {}),
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // --------------------------------------------------------------------------
  // GET /entity?id= 或 ?name=
  // --------------------------------------------------------------------------
  route('exact', `${CONSOLE_API}/entity`, async (req, res) => {
    if (!requireGet(req, res)) return
    try {
      const url = urlOf(req)
      const idRaw = String(url.searchParams.get('id') ?? '').trim()
      const nameRaw = String(url.searchParams.get('name') ?? '').trim()
      let ref = ''
      if (idRaw) {
        ref = validateId(idRaw) ?? ''
      } else if (nameRaw) {
        ref = safeValue(nameRaw, 200) ?? ''
      }
      if (!ref) {
        return json(res, 400, { ok: false, error: '需要合法的 id 或 name' })
      }

      let detail = reader?.entityByIdOrName ? reader.entityByIdOrName(ref) : null
      if (detail && detail.conflict) {
        return json(res, 409, {
          ok: false,
          error: `实体名 ${ref} 对应 ${detail.conflict.length} 个身份，请用 id 指定`,
          code: 'E_CONFLICT',
          details: { candidates: detail.conflict },
        })
      }
      let fallback = false
      if (!detail) {
        const viaCli = await entityDetailFallback(ref)
        if (!viaCli.ok) {
          return json(res, statusFor(viaCli.error), errorBody(viaCli.error))
        }
        detail = viaCli.data
        fallback = true
      }

      const entityId = String(detail.entity?.id ?? '')
      const relationResult = fallback
        ? { relations: detail.relations ?? [], degraded: [] }
        : await fetchEntityRelations(entityId)
      const degraded = [...new Set([...(detail.degraded ?? []), ...relationResult.degraded])]
      return json(res, 200, {
        ok: true,
        entity: detail.entity,
        aliases: detail.aliases ?? [],
        mentions: detail.mentions ?? [],
        counts: detail.counts ?? null,
        relations: relationResult.relations,
        ...(fallback ? { fallback: true } : {}),
        ...(degraded.length ? { degraded } : {}),
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // --------------------------------------------------------------------------
  // POST /net/query {src?, dst?, depth?, predicate?, limit?, dormant?, as_of?}
  // --------------------------------------------------------------------------
  route('exact', `${CONSOLE_API}/net/query`, async (req, res) => {
    if (!requirePost(req, res)) return
    const body = await bodyOrRespond(req, res)
    if (!body) return
    try {
      const src = body.src === undefined || body.src === null ? null : safeValue(body.src, 200)
      const dst = body.dst === undefined || body.dst === null ? null : safeValue(body.dst, 200)
      if (body.src !== undefined && body.src !== null && !src) {
        return json(res, 400, { ok: false, error: 'src 不合法（不得为空、不得以 - 开头）' })
      }
      if (body.dst !== undefined && body.dst !== null && !dst) {
        return json(res, 400, { ok: false, error: 'dst 不合法（不得为空、不得以 - 开头）' })
      }
      if (!src && !dst) {
        return json(res, 400, { ok: false, error: 'src 和 dst 至少要给一个' })
      }
      let depth = 2
      if (body.depth !== undefined && body.depth !== null) {
        const value = Number(body.depth)
        if (value !== 1 && value !== 2) {
          return json(res, 400, { ok: false, error: 'depth 只支持 1 或 2' })
        }
        depth = value
      }
      const argv = ['net', 'query']
      if (src) argv.push(src)
      if (dst) argv.push('--dst', dst)
      if (body.predicate !== undefined && body.predicate !== null) {
        const predicate = safeValue(body.predicate, 64)
        if (!predicate) return json(res, 400, { ok: false, error: 'predicate 不合法' })
        argv.push('--predicate', predicate)
      }
      argv.push('--depth', String(depth))
      argv.push('--limit', String(intIn(body.limit, 30, 1, 100)))
      if (body.dormant !== undefined && body.dormant !== null) {
        if (typeof body.dormant !== 'boolean') {
          return json(res, 400, { ok: false, error: 'dormant 必须是布尔值' })
        }
        if (body.dormant) argv.push('--dormant')
      }
      if (body.as_of !== undefined && body.as_of !== null) {
        const asOf = safeRegexValue(body.as_of, /^\d{4}-\d{2}-\d{2}([T ][0-9:.+\-Zz]+)?$/, 40)
        if (!asOf) return json(res, 400, { ok: false, error: 'as_of 不是合法时间（YYYY-MM-DD 或 ISO-8601）' })
        argv.push('--as-of', asOf)
      }

      const out = await invoke(argv, 30000)
      if (!out.ok) return json(res, statusFor(out.error), errorBody(out.error))
      const data = out.data ?? {}
      return json(res, 200, {
        ok: true,
        src: data.src ?? null,
        dst: data.dst ?? null,
        depth: numberOrNull(data.depth) ?? depth,
        scanned: numberOrNull(data.scanned) ?? 0,
        request: data.request ?? { src, dst },
        items: Array.isArray(data.items) ? data.items : [],
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // --------------------------------------------------------------------------
  // POST /net/merge {left, right, dry_run?}
  // --------------------------------------------------------------------------
  route('exact', `${CONSOLE_API}/net/merge`, async (req, res) => {
    if (!requirePost(req, res)) return
    const body = await bodyOrRespond(req, res)
    if (!body) return
    try {
      const left = safeValue(body.left, 200)
      const right = safeValue(body.right, 200)
      if (!left || !right) {
        return json(res, 400, { ok: false, error: 'left 和 right 都必须给出（实体 id 或名字）' })
      }
      if (body.dry_run !== undefined && body.dry_run !== null && typeof body.dry_run !== 'boolean') {
        return json(res, 400, { ok: false, error: 'dry_run 必须是布尔值' })
      }
      const dryRun = body.dry_run === true
      if (left === right) {
        return json(res, 400, { ok: false, error: 'left 和 right 不能是同一个实体' })
      }

      if (dryRun) {
        const [keepResult, dropResult] = await Promise.all([
          resolveEntityForMerge(left),
          resolveEntityForMerge(right),
        ])
        if (keepResult.conflict || dropResult.conflict) {
          const seen = new Set()
          const candidates = [...(keepResult.conflict ?? []), ...(dropResult.conflict ?? [])]
            .filter(item => {
              const key = String(item?.id ?? item?.name ?? '')
              if (!key || seen.has(key)) return false
              seen.add(key)
              return true
            })
          return json(res, 409, {
            ok: false,
            error: '有一个名字对应多个身份，请用 id 指定',
            code: 'E_CONFLICT',
            details: { candidates },
          })
        }
        if (keepResult.error) return json(res, statusFor(keepResult.error), errorBody(keepResult.error))
        if (dropResult.error) return json(res, statusFor(dropResult.error), errorBody(dropResult.error))
        if (keepResult.notFound || dropResult.notFound) {
          return json(res, 404, { ok: false, error: '待合并的实体不存在', code: 'E_NOT_FOUND' })
        }
        let between = []
        let degraded = []
        if (keepResult.entity?.id && dropResult.entity?.id) {
          const out = await invoke(
            ['net', 'query', String(keepResult.entity.id), '--dst', String(dropResult.entity.id), '--depth', '1', '--limit', '50'],
            20000,
          )
          if (out.ok) between = Array.isArray(out.data?.items) ? out.data.items : []
          else degraded = ['relations_between']
        }
        return json(res, 200, {
          ok: true,
          dry_run: true,
          keep: keepResult.entity,
          drop: dropResult.entity,
          keep_counts: keepResult.counts,
          drop_counts: dropResult.counts,
          relations_between: between,
          will_merge: { keep: keepResult.entity, drop: dropResult.entity },
          ...(degraded.length ? { degraded } : {}),
        })
      }

      const out = await invoke(['net', 'merge', left, right], 60000)
      if (!out.ok) return json(res, statusFor(out.error), errorBody(out.error))
      const data = out.data ?? {}
      return json(res, 200, {
        ok: true,
        dry_run: false,
        keep: data.keep ?? null,
        merged: data.merged ?? null,
        aliases: numberOrNull(data.aliases) ?? 0,
        mentions: numberOrNull(data.mentions) ?? 0,
        relations: numberOrNull(data.relations) ?? 0,
        result: data,
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // --------------------------------------------------------------------------
  // GET /backup —— 列备份目录（和 POST /backup 共用一个 exact 路径：DSH 的
  // webServer 同路径不同方法会互相覆盖，所以由 handler 自己按方法分发）
  // --------------------------------------------------------------------------
  async function backupList(req, res) {
    try {
      const dir = resolveBackupDir(config)
      if (!dir) {
        return json(res, 200, {
          ok: true,
          dir: null,
          exists: false,
          items: [],
          counts: { total: 0, total_bytes: 0 },
          warning: '没有数据库路径，无法定位备份目录',
        })
      }
      const files = listBackupFiles(dir)
      const metaOut = await invoke(['backup', 'list'], 30000)
      const metaMap = new Map()
      if (metaOut.ok && Array.isArray(metaOut.data?.items)) {
        for (const item of metaOut.data.items) {
          const name = basename(String(item?.path ?? ''))
          if (name) metaMap.set(name, item)
        }
      }
      const items = (files ?? []).map(file => {
        const meta = metaMap.get(file.name) ?? null
        return {
          ...file,
          kind: textOrNull(meta?.kind),
          snapshot_id: textOrNull(meta?.id),
          memory_count: numberOrNull(meta?.memory_count),
          sha256: textOrNull(meta?.sha256),
          verified_at: textOrNull(meta?.verified_at),
        }
      })
      return json(res, 200, {
        ok: true,
        dir,
        exists: existsSync(dir),
        items,
        counts: {
          total: items.length,
          total_bytes: items.reduce((sum, item) => sum + (item.size_bytes ?? 0), 0),
        },
        ...(metaOut.ok ? {} : { degraded: ['backup-list'] }),
      })
    } catch (error) {
      return fail(res, error)
    }
  }

  // --------------------------------------------------------------------------
  // POST /backup {note?, kind?}
  // --------------------------------------------------------------------------
  async function backupCreate(req, res) {
    const body = await bodyOrRespond(req, res)
    if (!body) return
    try {
      const kind = body.kind === undefined || body.kind === null
        ? 'manual'
        : String(body.kind).trim()
      if (!BACKUP_KINDS.includes(kind)) {
        return json(res, 400, { ok: false, error: `kind 只能是 ${BACKUP_KINDS.join(' / ')}` })
      }
      const note = body.note === undefined || body.note === null
        ? null
        : String(body.note).replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, 200)
      const out = await invoke(['backup', 'run', '--kind', kind], 120000)
      if (!out.ok) return json(res, statusFor(out.error), errorBody(out.error))
      const data = out.data ?? {}
      const file = {
        name: data.path ? basename(String(data.path)) : null,
        size_bytes: numberOrNull(data.size_bytes),
        sha256: textOrNull(data.sha256),
        memory_count: numberOrNull(data.memory_count),
        verified: data.verified === true,
        kind: textOrNull(data.kind) ?? kind,
        snapshot_id: textOrNull(data.snapshot_id),
        duration_ms: numberOrNull(data.duration_ms),
        counts: data.counts ?? null,
      }
      return json(res, 200, {
        ok: true,
        file,
        dir: resolveBackupDir(config),
        note,
      })
    } catch (error) {
      return fail(res, error)
    }
  }

  // 同一路径按方法分发：方法不对给 405，而不是被另一个 handler 顶掉。
  route('exact', `${CONSOLE_API}/backup`, async (req, res) => {
    const method = methodOf(req)
    if (method === 'POST') return backupCreate(req, res)
    if (method === 'GET') return backupList(req, res)
    return json(res, 405, { ok: false, error: '只接受 GET 或 POST' })
  })

  // --------------------------------------------------------------------------
  // GET /backup/file?name= —— 只允许备份目录里的 .db basename
  // --------------------------------------------------------------------------
  route('exact', `${CONSOLE_API}/backup/file`, async (req, res) => {
    if (!requireGet(req, res)) return
    try {
      const name = safeBackupName(urlOf(req).searchParams.get('name'))
      if (!name) {
        return json(res, 400, {
          ok: false,
          error: 'name 不合法（只允许备份目录里的 .db 文件名，不能带路径）',
        })
      }
      const dir = resolveBackupDir(config)
      if (!dir) return json(res, 404, { ok: false, error: '没有备份目录' })
      const target = resolve(join(dir, name))
      const root = resolve(dir)
      if (dirname(target) !== root || !target.startsWith(root + sep)) {
        return json(res, 400, { ok: false, error: 'name 不合法（路径穿越被拒绝）' })
      }
      let info
      try {
        info = statSync(target)
      } catch {
        return json(res, 404, { ok: false, error: '备份文件不存在' })
      }
      if (!info.isFile()) return json(res, 404, { ok: false, error: '备份文件不存在' })

      res.statusCode = 200
      res.setHeader('content-type', 'application/octet-stream')
      res.setHeader('content-length', String(info.size))
      res.setHeader('content-disposition', `attachment; filename="${name}"`)
      res.setHeader('cache-control', 'no-store')
      if (info.size > 8 * 1024 * 1024) {
        const stream = createReadStream(target)
        stream.on('error', () => {
          try { res.destroy() } catch { /* 已经断了 */ }
        })
        stream.pipe(res)
      } else {
        res.end(readFileSync(target))
      }
    } catch (error) {
      return fail(res, error)
    }
  })

  // --------------------------------------------------------------------------
  // POST /vector/rebuild {dry_run?}
  // --------------------------------------------------------------------------
  route('exact', `${CONSOLE_API}/vector/rebuild`, async (req, res) => {
    if (!requirePost(req, res)) return
    const body = await bodyOrRespond(req, res)
    if (!body) return
    try {
      if (body.dry_run !== undefined && body.dry_run !== null && typeof body.dry_run !== 'boolean') {
        return json(res, 400, { ok: false, error: 'dry_run 必须是布尔值' })
      }
      const dryRun = body.dry_run === true
      const out = dryRun
        ? await invoke(['vector', 'status'], 60000)
        : await invoke(['vector', 'rebuild'], 600000)
      if (!out.ok) return json(res, statusFor(out.error), errorBody(out.error))
      return json(res, 200, {
        ok: true,
        dry_run: dryRun,
        ...(dryRun ? { status: out.data } : { result: out.data }),
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // --------------------------------------------------------------------------
  // POST /memory/batch {ids, op, signal?, note?, hard?}
  // --------------------------------------------------------------------------
  route('exact', `${CONSOLE_API}/memory/batch`, async (req, res) => {
    if (!requirePost(req, res)) return
    const body = await bodyOrRespond(req, res)
    if (!body) return
    try {
      if (!Array.isArray(body.ids)) {
        return json(res, 400, { ok: false, error: 'ids 必须是数组' })
      }
      if (body.ids.length === 0) {
        return json(res, 400, { ok: false, error: 'ids 不能为空' })
      }
      if (body.ids.length > BATCH_MAX_IDS) {
        return json(res, 400, { ok: false, error: `一次最多 ${BATCH_MAX_IDS} 条` })
      }
      const ids = []
      for (const raw of body.ids) {
        const id = validateId(raw)
        if (!id) return json(res, 400, { ok: false, error: `id 不合法：${String(raw).slice(0, 40)}` })
        if (!ids.includes(id)) ids.push(id)
      }
      const op = String(body.op ?? '').trim()
      if (!BATCH_OPS.includes(op)) {
        return json(res, 400, { ok: false, error: `op 只能是 ${BATCH_OPS.join(' / ')}` })
      }
      // op=update：patch 先整份校验，非法字段/值直接 400，不做半批应用。
      let patch = null
      if (op === 'update') {
        if (!body.patch || typeof body.patch !== 'object' || Array.isArray(body.patch)) {
          return json(res, 400, { ok: false, error: 'op=update 需要 patch 对象' })
        }
        patch = normalizeUpdatePatch(body.patch, BATCH_UPDATE_FIELDS)
        if (!Object.keys(patch).length) {
          return json(res, 400, { ok: false, error: 'patch 不能为空' })
        }
      }
      let signal = null
      if (op === 'feedback') {
        signal = String(body.signal ?? '').trim()
        if (!FEEDBACK_SIGNALS.includes(signal)) {
          return json(res, 400, { ok: false, error: `signal 只能是 ${FEEDBACK_SIGNALS.join(' / ')}` })
        }
      }
      if (body.hard !== undefined && body.hard !== null && typeof body.hard !== 'boolean') {
        return json(res, 400, { ok: false, error: 'hard 必须是布尔值' })
      }
      const hard = op === 'delete' && body.hard === true
      const note = body.note === undefined || body.note === null
        ? null
        : String(body.note).replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, 500)

      const results = []
      for (const id of ids) {
        if (op === 'update') {
          const { applied, failed } = await applyMemoryPatch(id, patch)
          results.push({
            id,
            ok: failed.length === 0,
            applied,
            ...(failed.length ? { failed } : {}),
          })
          continue
        }
        let args
        let timeout = 30000
        if (op === 'feedback') {
          args = ['feedback', id, signal]
          if (note) args.push('--note', note)
        } else if (op === 'retract') {
          args = ['retract', id]
          if (note) args.push('--reason', note)
        } else {
          args = ['delete', id]
          if (hard) args.push('--hard')
          timeout = 60000
        }
        const out = await invoke(args, timeout)
        if (out.ok) results.push({ id, ok: true })
        else {
          results.push({
            id,
            ok: false,
            error: {
              code: out.error.code,
              message: out.error.message,
              ...(out.error.hint ? { hint: out.error.hint } : {}),
            },
          })
        }
      }
      const okCount = results.filter(item => item.ok).length
      const failCount = results.length - okCount
      return json(res, 200, {
        ok: failCount === 0,
        op,
        requested: body.ids.length,
        processed: results.length,
        okCount,
        failCount,
        results,
        ...(op === 'update' ? { applied_fields: Object.keys(patch) } : {}),
        ...(op === 'delete' ? { hard } : {}),
      })
    } catch (error) {
      return fail(res, error)
    }
  })

  // --------------------------------------------------------------------------
  // POST /action {name: 'doctor'|'vacuum'|'evolve'|'net-extract', dry_run?}
  // --------------------------------------------------------------------------
  route('exact', `${CONSOLE_API}/action`, async (req, res) => {
    if (!requirePost(req, res)) return
    const body = await bodyOrRespond(req, res)
    if (!body) return
    try {
      const name = String(body.name ?? '').trim()
      if (!ACTION_NAMES.includes(name)) {
        return json(res, 400, {
          ok: false,
          error: `name 只能是 ${ACTION_NAMES.join(' / ')}`,
        })
      }
      if (body.dry_run !== undefined && body.dry_run !== null && typeof body.dry_run !== 'boolean') {
        return json(res, 400, { ok: false, error: 'dry_run 必须是布尔值' })
      }
      const dryRun = body.dry_run === true

      if (name === 'doctor') {
        const out = await invoke(['doctor'], 30000)
        if (!out.ok) return json(res, statusFor(out.error), errorBody(out.error))
        return json(res, 200, { ok: true, name, dry_run: false, result: out.data })
      }

      if (name === 'vacuum') {
        if (dryRun) {
          const out = await invoke(['stats'], 15000)
          if (!out.ok) return json(res, statusFor(out.error), errorBody(out.error))
          return json(res, 200, {
            ok: true,
            name,
            dry_run: true,
            planned: ['vacuum', ...(body.analyze === true ? ['analyze'] : [])],
            before: {
              db_size_bytes: numberOrNull(out.data?.db_size_bytes),
              db_path: textOrNull(out.data?.db_path),
            },
          })
        }
        const argv = ['vacuum']
        if (body.analyze === true) argv.push('--analyze')
        const out = await invoke(argv, 300000)
        if (!out.ok) return json(res, statusFor(out.error), errorBody(out.error))
        return json(res, 200, { ok: true, name, dry_run: false, result: out.data })
      }

      if (name === 'evolve') {
        const argv = ['evolve', 'run']
        const layers = parseList(body.layer)
        for (const layer of layers) {
          if (!EVOLVE_LAYERS.includes(layer)) {
            return json(res, 400, {
              ok: false,
              error: `layer 只能是 ${EVOLVE_LAYERS.join(' / ')}：${layer}`,
            })
          }
        }
        for (const layer of layers) argv.push('--layer', layer)
        if (dryRun) argv.push('--dry-run')
        const out = await invoke(argv, 300000)
        if (!out.ok) return json(res, statusFor(out.error), errorBody(out.error))
        return json(res, 200, {
          ok: true,
          name,
          dry_run: dryRun,
          layers,
          result: out.data,
        })
      }

      // net-extract
      if (dryRun) {
        const out = await invoke(['net', 'stats'], 15000)
        if (!out.ok) return json(res, statusFor(out.error), errorBody(out.error))
        return json(res, 200, { ok: true, name, dry_run: true, planned: ['net', 'extract'], stats: out.data })
      }
      const argv = ['net', 'extract']
      if (body.limit !== undefined && body.limit !== null) {
        const limit = Number(body.limit)
        if (!Number.isFinite(limit) || limit < 1 || limit > 10000) {
          return json(res, 400, { ok: false, error: 'limit 必须是 1-10000 的整数' })
        }
        argv.push('--limit', String(Math.trunc(limit)))
      }
      if (body.reset !== undefined && body.reset !== null) {
        if (typeof body.reset !== 'boolean') {
          return json(res, 400, { ok: false, error: 'reset 必须是布尔值' })
        }
        if (body.reset) argv.push('--reset')
      }
      const out = await invoke(argv, 600000)
      if (!out.ok) return json(res, statusFor(out.error), errorBody(out.error))
      return json(res, 200, { ok: true, name, dry_run: false, result: out.data })
    } catch (error) {
      return fail(res, error)
    }
  })

  return { routes, reader }
}
