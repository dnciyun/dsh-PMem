"""库结构（docs/02-data-model.md 的可执行版本）。

设计约束：

1. 原文不可变 —— ``memory`` 的关键列被 BEFORE UPDATE 触发器钉死；
2. 派生可重算 —— 派生表都带 ``run_id`` / ``derivation``；
3. 双时间轴 —— ``created_at`` 与 ``valid_from`` 分开；
4. 零扩展可跑 —— 向量表用普通表 ``memory_vec_plain``（numpy 暴力检索），
   等 sqlite-vec 可用时再加 ``memory_vec`` 虚表（02 §4.6 的回退方案）。
"""

from __future__ import annotations

SCHEMA_VERSION = "1.10.0"

# 实体表单独提出来：M3 的迁移要重建它（SQLite 不支持改 CHECK 约束）
ENTITY_DDL = """
    CREATE TABLE IF NOT EXISTS entity (
      seq          INTEGER PRIMARY KEY AUTOINCREMENT,
      id           TEXT NOT NULL UNIQUE,
      subject_id   TEXT NOT NULL REFERENCES subject(id),
      type         TEXT NOT NULL CHECK (type IN
                     ('person','place','org','time','event','knowledge','tool','goal','concept','trait','cognition')),
      name         TEXT NOT NULL,
      name_norm    TEXT NOT NULL,
      status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','merged','deprecated')),
      canonical_id TEXT REFERENCES entity(id),
      confidence   REAL NOT NULL DEFAULT 0.6,
      provenance   TEXT NOT NULL,
      first_seen   TEXT NOT NULL,
      last_seen    TEXT NOT NULL,
      mention_count INTEGER NOT NULL DEFAULT 0,
      extra_json   TEXT NOT NULL DEFAULT '{}'
    )
    """

ENTITY_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_name ON entity(subject_id, type, name_norm) WHERE status='active'",
    "CREATE INDEX IF NOT EXISTS idx_entity_name ON entity(name_norm)",
    # 实体桥（M22）每次检索都会取 subject 下 mention_count 最高的实体。
    # 没有这条索引时 SQLite 只能按 uq_entity_name(subject) 取全部实体再 TEMP B-TREE 排序，
    # 实体多了之后这一步会随库线性变慢。
    "CREATE INDEX IF NOT EXISTS idx_entity_bridge ON entity(subject_id, status, mention_count DESC)",
]

DDL: list[str] = [
    # ---- 元信息 / 主体 ---------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
      key         TEXT PRIMARY KEY,
      value       TEXT NOT NULL,
      updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subject (
      id           TEXT PRIMARY KEY,
      kind         TEXT NOT NULL CHECK (kind IN ('self','person','group','org')),
      display      TEXT NOT NULL,
      aliases_json TEXT NOT NULL DEFAULT '[]',
      created_at   TEXT NOT NULL,
      status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','merged','deleted')),
      merged_into  TEXT REFERENCES subject(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS episode (
      id           TEXT PRIMARY KEY,
      subject_id   TEXT NOT NULL REFERENCES subject(id),
      channel      TEXT NOT NULL DEFAULT 'dsh',
      started_at   TEXT NOT NULL,
      ended_at     TEXT,
      day_key      TEXT NOT NULL,
      title        TEXT,
      summary      TEXT,
      memory_count INTEGER NOT NULL DEFAULT 0,
      derivation   TEXT,
      extra_json   TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_episode_day ON episode(day_key)",
    # ---- 记忆主表 --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS memory (
      seq            INTEGER PRIMARY KEY AUTOINCREMENT,
      id             TEXT NOT NULL UNIQUE,
      subject_id     TEXT NOT NULL REFERENCES subject(id),
      scope_key      TEXT NOT NULL,

      kind           TEXT NOT NULL CHECK (kind IN
                       ('episodic','semantic','person','relation','experience','goal','meta')),
      text_raw       TEXT NOT NULL,
      text_norm      TEXT NOT NULL,
      text_seg       TEXT NOT NULL DEFAULT '',
      text_sha256    TEXT NOT NULL,
      lang           TEXT NOT NULL DEFAULT 'zh',

      status         TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN
                       ('candidate','active','dormant','archived','contested','superseded','discarded','deleted')),

      importance     REAL NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
      confidence     REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
      provenance     TEXT NOT NULL CHECK (provenance IN
                       ('user_explicit','user_confirmed','external','observed','inferred','unconfirmed')),
      evidence_count INTEGER NOT NULL DEFAULT 1,
      evidence_div   REAL NOT NULL DEFAULT 0.0,

      source_kind    TEXT NOT NULL,
      source_ref     TEXT,
      episode_id     TEXT REFERENCES episode(id),
      session_id     TEXT,
      claim_sig      TEXT,
      sensitivity    TEXT NOT NULL DEFAULT 'personal' CHECK (sensitivity IN ('public','personal','secret')),
      pinned         INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),

      created_at     TEXT NOT NULL,
      recorded_at    TEXT NOT NULL,
      valid_from     TEXT,
      valid_to       TEXT,
      day_key        TEXT NOT NULL,

      canonical_id   TEXT REFERENCES memory(id) ON DELETE SET NULL,
      supersedes_id  TEXT REFERENCES memory(id) ON DELETE SET NULL,
      superseded_by  TEXT,
      cluster_id     TEXT,

      use_count      INTEGER NOT NULL DEFAULT 0,
      -- 「真的被用到」：显式召回 / 浮现回执 / 正面反馈。浮现不算。
      last_used_at   TEXT,
      -- 「只是被浮现出来过」：每轮注入的弱信号。它推进轮换，但不推进淡忘。
      last_surfaced_at TEXT,
      decay_score    REAL NOT NULL DEFAULT 1.0,
      extra_json     TEXT NOT NULL DEFAULT '{}',
      deleted_at     TEXT,

      CHECK (length(text_raw) BETWEEN 1 AND 8000),
      CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from),
      CHECK (status <> 'deleted' OR deleted_at IS NOT NULL)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mem_subj_status ON memory(subject_id, status, kind)",
    "CREATE INDEX IF NOT EXISTS idx_mem_scope ON memory(scope_key, status, day_key)",
    "CREATE INDEX IF NOT EXISTS idx_mem_day ON memory(day_key, status)",
    "CREATE INDEX IF NOT EXISTS idx_mem_session ON memory(session_id, claim_sig)",
    "CREATE INDEX IF NOT EXISTS idx_mem_sig ON memory(claim_sig)",
    "CREATE INDEX IF NOT EXISTS idx_mem_sha ON memory(text_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_mem_canonical ON memory(canonical_id)",
    "CREATE INDEX IF NOT EXISTS idx_mem_last_used ON memory(last_used_at)",
    "CREATE INDEX IF NOT EXISTS idx_mem_last_surfaced ON memory(last_surfaced_at)",
    # 日记派生事实的归属：journal.digest 写 memory.episode_id，journal repair / I20 诊断
    # 要按 episode 查事实。SQLite 不会自动给外键建索引，缺它时只能全表扫。
    "CREATE INDEX IF NOT EXISTS idx_mem_episode ON memory(episode_id)",
    # ---- 原文不可变（P1 的机器执法） --------------------------------------
    """
    CREATE TRIGGER IF NOT EXISTS mem_immutable BEFORE UPDATE ON memory
    WHEN old.id <> new.id
      OR old.text_raw <> new.text_raw
      OR old.text_norm <> new.text_norm
      OR old.text_sha256 <> new.text_sha256
      OR old.created_at <> new.created_at
      OR old.provenance <> new.provenance
    BEGIN
      SELECT RAISE(ABORT, 'memory_is_immutable');
    END
    """,
    # ---- 全文索引（双通道） ----------------------------------------------
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_tok USING fts5(
      text_seg,
      content='memory',
      content_rowid='seq',
      tokenize="unicode61 remove_diacritics 2"
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_tri USING fts5(
      text_norm,
      content='memory',
      content_rowid='seq',
      tokenize='trigram'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memory BEGIN
      INSERT INTO memory_fts_tok(rowid, text_seg) VALUES (new.seq, new.text_seg);
      INSERT INTO memory_fts_tri(rowid, text_norm) VALUES (new.seq, new.text_norm);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memory BEGIN
      INSERT INTO memory_fts_tok(memory_fts_tok, rowid, text_seg) VALUES('delete', old.seq, old.text_seg);
      INSERT INTO memory_fts_tri(memory_fts_tri, rowid, text_norm) VALUES('delete', old.seq, old.text_norm);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE OF text_seg, text_norm ON memory BEGIN
      INSERT INTO memory_fts_tok(memory_fts_tok, rowid, text_seg) VALUES('delete', old.seq, old.text_seg);
      INSERT INTO memory_fts_tok(rowid, text_seg) VALUES (new.seq, new.text_seg);
      INSERT INTO memory_fts_tri(memory_fts_tri, rowid, text_norm) VALUES('delete', old.seq, old.text_norm);
      INSERT INTO memory_fts_tri(rowid, text_norm) VALUES (new.seq, new.text_norm);
    END
    """,
    # ---- 向量（可移植回退表；M1 默认空） ----------------------------------
    """
    CREATE TABLE IF NOT EXISTS memory_vec_plain (
      memory_seq INTEGER PRIMARY KEY REFERENCES memory(seq) ON DELETE CASCADE,
      model_id   TEXT NOT NULL,
      dim        INTEGER NOT NULL,
      vec        BLOB NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    # ---- 幂等 ------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS idem (
      key        TEXT PRIMARY KEY,
      op         TEXT NOT NULL,
      target_id  TEXT,
      payload    TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL
    )
    """,
    # ---- 事件日志（哈希链） ----------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS event_log (
      seq          INTEGER PRIMARY KEY AUTOINCREMENT,
      id           TEXT NOT NULL UNIQUE,
      ts           TEXT NOT NULL,
      actor        TEXT NOT NULL,
      op           TEXT NOT NULL,
      target_tbl   TEXT,
      target_id    TEXT,
      subject_id   TEXT,
      payload_json TEXT NOT NULL DEFAULT '{}',
      idem_key     TEXT UNIQUE,
      run_id       TEXT,
      prev_hash    TEXT,
      hash         TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_event_ts ON event_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_event_target ON event_log(target_tbl, target_id)",
    # run_id 是「一次运行改了什么」的因果链键：睡眠/进化/备份/评测的事件都带它。
    # 0.24.3 引入 event_log 时列就一起在了；早期/手改库若缺列，由
    # Database._migrate_event_log_run 先补列（幂等），这里只负责幂等建索引。
    "CREATE INDEX IF NOT EXISTS idx_event_run ON event_log(run_id)",
    # ---- 关系网络 --------------------------------------------------------
    ENTITY_DDL,
    *ENTITY_INDEXES,
    """
    CREATE TABLE IF NOT EXISTS entity_alias (
      id         TEXT PRIMARY KEY,
      entity_id  TEXT NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
      alias      TEXT NOT NULL,
      alias_norm TEXT NOT NULL,
      lang       TEXT,
      source     TEXT NOT NULL,
      confidence REAL NOT NULL DEFAULT 0.8,
      created_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_alias ON entity_alias(entity_id, alias_norm)",
    "CREATE INDEX IF NOT EXISTS idx_alias_norm ON entity_alias(alias_norm)",
    """
    CREATE TABLE IF NOT EXISTS entity_mention (
      seq        INTEGER PRIMARY KEY AUTOINCREMENT,
      entity_id  TEXT NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
      memory_seq INTEGER NOT NULL REFERENCES memory(seq) ON DELETE CASCADE,
      span_start INTEGER NOT NULL,
      span_end   INTEGER NOT NULL,
      surface    TEXT NOT NULL,
      confidence REAL NOT NULL DEFAULT 0.8,
      extractor  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mention_mem ON entity_mention(memory_seq)",
    "CREATE INDEX IF NOT EXISTS idx_mention_entity ON entity_mention(entity_id)",
    """
    CREATE TABLE IF NOT EXISTS relation_type (
      predicate   TEXT PRIMARY KEY,
      inverse     TEXT,
      domain      TEXT,
      range_      TEXT,
      transitive  INTEGER NOT NULL DEFAULT 0,
      max_weight  REAL NOT NULL DEFAULT 0.8,
      description TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS relation (
      id            TEXT PRIMARY KEY,
      src_id        TEXT NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
      dst_id        TEXT NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
      predicate     TEXT NOT NULL REFERENCES relation_type(predicate),
      weight        REAL NOT NULL DEFAULT 0.5 CHECK (weight BETWEEN 0 AND 1),
      confidence    REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
      provenance    TEXT NOT NULL,
      evidence_json TEXT NOT NULL,
      valid_from    TEXT,
      valid_to      TEXT,
      created_at    TEXT NOT NULL,
      updated_at    TEXT NOT NULL,
      last_support_at TEXT,
      status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','dormant','contested','retracted')),
      derivation    TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_relation ON relation(src_id, dst_id, predicate) WHERE status IN ('active','contested')",
    "CREATE INDEX IF NOT EXISTS idx_rel_src ON relation(src_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_rel_dst ON relation(dst_id, status)",
    # ---- 认知特质 --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS trait (
      id             TEXT PRIMARY KEY,
      subject_id     TEXT NOT NULL REFERENCES subject(id),
      scope          TEXT NOT NULL CHECK (scope IN ('self','person','topic')),
      type           TEXT NOT NULL CHECK (type IN
                       ('interest','habit','cognition','experience','goal','change')),
      key            TEXT NOT NULL,
      value_json     TEXT NOT NULL,
      strength       REAL NOT NULL CHECK (strength BETWEEN 0 AND 1),
      confidence     REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
      status         TEXT NOT NULL DEFAULT 'hypothesis' CHECK (status IN ('hypothesis','established','deprecated')),
      evidence_json  TEXT NOT NULL,
      evidence_days  INTEGER NOT NULL DEFAULT 1,
      first_seen     TEXT NOT NULL,
      last_support_at TEXT NOT NULL,
      version        INTEGER NOT NULL DEFAULT 1,
      prev_id        TEXT REFERENCES trait(id),
      derivation     TEXT NOT NULL,
      created_at     TEXT NOT NULL,
      updated_at     TEXT NOT NULL,
      retired_reason TEXT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_trait ON trait(subject_id, scope, type, key) WHERE status IN ('hypothesis','established')",
    "CREATE INDEX IF NOT EXISTS idx_trait_scope ON trait(subject_id, scope, status)",
    # ---- 冲突 / 规律 / 经验 ----------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS conflict (
      id          TEXT PRIMARY KEY,
      subject_id  TEXT NOT NULL,
      kind        TEXT NOT NULL CHECK (kind IN ('slot','negation','temporal','identity','evidence')),
      left_tbl    TEXT NOT NULL, left_id TEXT NOT NULL,
      right_tbl   TEXT NOT NULL, right_id TEXT NOT NULL,
      slot        TEXT,
      severity    REAL NOT NULL DEFAULT 0.5,
      detail_json TEXT NOT NULL,
      status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN
                    ('open','keep_both','prefer_left','prefer_right','dismissed')),
      resolved_by TEXT,
      resolved_at TEXT,
      created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_conflict_open ON conflict(subject_id, status, kind)",
    """
    CREATE TABLE IF NOT EXISTS pattern (
      id          TEXT PRIMARY KEY,
      subject_id  TEXT NOT NULL,
      kind        TEXT NOT NULL CHECK (kind IN ('frequency','sequence','periodicity','preference')),
      key         TEXT NOT NULL,
      support     INTEGER NOT NULL,
      confidence  REAL NOT NULL,
      window_json TEXT NOT NULL,
      evidence_json TEXT NOT NULL,
      status      TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate','confirmed','rejected','stale')),
      first_seen  TEXT NOT NULL,
      last_seen   TEXT NOT NULL,
      derivation  TEXT NOT NULL,
      created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience (
      id          TEXT PRIMARY KEY,
      subject_id  TEXT NOT NULL,
      situation   TEXT NOT NULL,
      action      TEXT NOT NULL,
      outcome     TEXT NOT NULL CHECK (outcome IN ('success','failure','mixed','unknown')),
      lesson      TEXT,
      context_json TEXT NOT NULL DEFAULT '{}',
      evidence_json TEXT NOT NULL,
      reuse_count INTEGER NOT NULL DEFAULT 0,
      last_reused_at TEXT,
      confidence  REAL NOT NULL DEFAULT 0.5,
      status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','stale','rejected')),
      created_at  TEXT NOT NULL
    )
    """,
    # ---- 反思（只读产出） ------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS reflection (
      id            TEXT PRIMARY KEY,
      subject_id    TEXT NOT NULL,
      period        TEXT NOT NULL,
      questions_json TEXT NOT NULL,
      findings_json TEXT NOT NULL,
      hypotheses_json TEXT NOT NULL,
      metrics_json  TEXT NOT NULL,
      suggestions_json TEXT NOT NULL,
      created_at    TEXT NOT NULL,
      run_id        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reflect_period ON reflection(subject_id, period)",
    # ---- 运行 / 备份 / 调度 ----------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS sleep_run (
      id          TEXT PRIMARY KEY,
      subject_id  TEXT NOT NULL,
      trigger     TEXT NOT NULL CHECK (trigger IN ('schedule','manual','catchup','cli')),
      day_key     TEXT NOT NULL,
      started_at  TEXT NOT NULL,
      finished_at TEXT,
      status      TEXT NOT NULL CHECK (status IN ('running','ok','failed','aborted','partial')),
      stage       TEXT,
      checkpoint_json TEXT NOT NULL DEFAULT '{}',
      stats_json  TEXT NOT NULL DEFAULT '{}',
      snapshot_id TEXT,
      error       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sleep_day ON sleep_run(day_key, status)",
    """
    CREATE TABLE IF NOT EXISTS snapshot (
      id             TEXT PRIMARY KEY,
      created_at     TEXT NOT NULL,
      path           TEXT NOT NULL,
      size_bytes     INTEGER NOT NULL,
      sha256         TEXT NOT NULL,
      schema_version TEXT NOT NULL,
      memory_count   INTEGER NOT NULL,
      verified_at    TEXT,
      kind           TEXT NOT NULL DEFAULT 'daily' CHECK (kind IN ('daily','monthly','manual','pre-migrate'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job (
      id          TEXT PRIMARY KEY,
      type        TEXT NOT NULL,
      payload_json TEXT NOT NULL DEFAULT '{}',
      next_run_at TEXT NOT NULL,
      state       TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','done','failed','dead')),
      attempts    INTEGER NOT NULL DEFAULT 0,
      last_error  TEXT,
      created_at  TEXT NOT NULL,
      updated_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_job_due ON job(state, next_run_at)",
    """
    CREATE TABLE IF NOT EXISTS metrics (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      ts          TEXT NOT NULL,
      day_key     TEXT NOT NULL,
      name        TEXT NOT NULL,
      value       REAL NOT NULL,
      labels_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_metrics_name_day ON metrics(name, day_key)",
    # ---- 使用与反馈 ------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS recall_log (
      id          TEXT PRIMARY KEY,
      ts          TEXT NOT NULL,
      subject_id  TEXT NOT NULL,
      query       TEXT NOT NULL,
      mode        TEXT NOT NULL,
      channels_json TEXT NOT NULL,
      returned_json TEXT NOT NULL,
      injected_tokens INTEGER NOT NULL DEFAULT 0,
      latency_ms  INTEGER NOT NULL,
      client      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_recall_ts ON recall_log(ts)",
    """
    CREATE TABLE IF NOT EXISTS feedback (
      id          TEXT PRIMARY KEY,
      ts          TEXT NOT NULL,
      subject_id  TEXT NOT NULL,
      target_tbl  TEXT NOT NULL, target_id TEXT NOT NULL,
      signal      TEXT NOT NULL CHECK (signal IN
                    ('useful','irrelevant','wrong','confirm','deny','pin','unpin')),
      note        TEXT,
      actor       TEXT NOT NULL,
      recall_id   TEXT REFERENCES recall_log(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_feedback_target ON feedback(target_tbl, target_id)",
    # ---- 进化标签与配置版本（M5） ---------------------------------------
    """
    CREATE TABLE IF NOT EXISTS evolve_label (
      id          TEXT PRIMARY KEY,
      subject_id  TEXT NOT NULL,
      target_tbl  TEXT NOT NULL CHECK (target_tbl IN
                    ('pattern','experience','conflict','trait')),
      target_id   TEXT NOT NULL,
      verdict     TEXT NOT NULL CHECK (verdict IN ('correct','wrong','unsure')),
      note        TEXT,
      actor       TEXT NOT NULL,
      created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_evolve_label_target ON evolve_label(target_tbl, target_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_evolve_label_subject ON evolve_label(subject_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS config_version (
      id            TEXT PRIMARY KEY,
      created_at    TEXT NOT NULL,
      params_json   TEXT NOT NULL,
      baseline_json TEXT NOT NULL DEFAULT '{}',
      metrics_json  TEXT NOT NULL DEFAULT '{}',
      status        TEXT NOT NULL CHECK (status IN ('active','superseded','rejected')),
      reason        TEXT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_config_active ON config_version(status) WHERE status='active'",
    # ---- 分组与分级（M3.5，AI 自建） -------------------------------------
    """
    CREATE TABLE IF NOT EXISTS grp (
      id          TEXT PRIMARY KEY,
      subject_id  TEXT NOT NULL REFERENCES subject(id),
      parent_id   TEXT REFERENCES grp(id) ON DELETE CASCADE,
      name        TEXT NOT NULL,
      name_norm   TEXT NOT NULL,
      path        TEXT NOT NULL,
      depth       INTEGER NOT NULL CHECK (depth BETWEEN 1 AND 4),
      kind        TEXT NOT NULL DEFAULT 'custom' CHECK (kind IN
                    ('self','person','topic','project','event','custom')),
      description TEXT,
      status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','merged','archived')),
      merged_into TEXT REFERENCES grp(id),
      ai_created  INTEGER NOT NULL DEFAULT 1 CHECK (ai_created IN (0,1)),
      created_at  TEXT NOT NULL,
      updated_at  TEXT NOT NULL,
      extra_json  TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_grp_path ON grp(subject_id, path) WHERE status='active'",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_grp_sibling ON grp(subject_id, COALESCE(parent_id,''), name_norm) WHERE status='active'",
    "CREATE INDEX IF NOT EXISTS idx_grp_parent ON grp(parent_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_grp_path ON grp(path)",
    """
    CREATE TABLE IF NOT EXISTS grp_member (
      id          TEXT PRIMARY KEY,
      grp_id      TEXT NOT NULL REFERENCES grp(id) ON DELETE CASCADE,
      target_tbl  TEXT NOT NULL CHECK (target_tbl IN ('memory','entity','trait','experience')),
      target_id   TEXT NOT NULL,
      assigned_by TEXT NOT NULL DEFAULT 'ai',
      created_at  TEXT NOT NULL,
      UNIQUE (grp_id, target_tbl, target_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_grp_member_target ON grp_member(target_tbl, target_id)",
    """
    CREATE TABLE IF NOT EXISTS target_grade (
      target_tbl    TEXT NOT NULL CHECK (target_tbl IN ('memory','entity','trait','experience')),
      target_id     TEXT NOT NULL,
      grade         TEXT NOT NULL CHECK (grade IN ('S','A','B','C','D')),
      rationale     TEXT,
      evidence_json TEXT NOT NULL DEFAULT '[]',
      assigned_by   TEXT NOT NULL DEFAULT 'ai',
      created_at    TEXT NOT NULL,
      updated_at    TEXT NOT NULL,
      PRIMARY KEY (target_tbl, target_id)
    )
    """,
    # ---- 浮现日志（M3.6）：同一会话里同一条只浮现一次 ----
    """
    CREATE TABLE IF NOT EXISTS surface_log (
      id          TEXT PRIMARY KEY,
      session_id  TEXT NOT NULL,
      target_tbl  TEXT NOT NULL CHECK (target_tbl IN ('memory','entity')),
      target_id   TEXT NOT NULL,
      mode        TEXT NOT NULL CHECK (mode IN ('session_start','turn')),
      grade       TEXT,
      tokens      INTEGER NOT NULL DEFAULT 0,
      cue         TEXT,
      ts          TEXT NOT NULL,
      UNIQUE (session_id, target_tbl, target_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_surface_session ON surface_log(session_id, ts)",
    # ---- 日记流水（M10）：当天的原话，消化成 episode 后按天清理 ----
    # 为什么要这一层：`episode`（日记）是**派生**出来的，派生需要原料。
    # 记忆库本身不存对话原文（隐私 + 体积），所以原文只在流水里活很短一段：
    # 消化成功就打上 episode_id，超过保留天数整行删掉。
    # 原文仍然只追加：这里只 INSERT，从不 UPDATE 正文。
    """
    CREATE TABLE IF NOT EXISTS journal_line (
      seq         INTEGER PRIMARY KEY AUTOINCREMENT,
      id          TEXT NOT NULL UNIQUE,
      subject_id  TEXT NOT NULL REFERENCES subject(id),
      session_id  TEXT,
      day_key     TEXT NOT NULL,
      ts          TEXT NOT NULL,
      role        TEXT NOT NULL CHECK (role IN ('user','assistant')),
      text        TEXT NOT NULL,
      episode_id  TEXT REFERENCES episode(id),
      created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_journal_day ON journal_line(day_key, seq)",
    "CREATE INDEX IF NOT EXISTS idx_journal_open ON journal_line(subject_id, episode_id)",
    # ---- 原话撤回墓碑（1.10.0 / M27）：撤回不是删除 ----
    # 为什么不给 journal_line 加一列：原文行是「说过什么」的事实，撤回是「这句
    # 不算数了」的另一条事实。两件事都只追加，谁都不改写谁——这才对得上
    # 「原文只追加、结论必有证据」。而且原文 30 天后会被 prune 掉，墓碑留着，
    # 还能回答「我确实撤回过」。excerpt 存撤回时的摘要：原文过期后仍认得出撤了什么。
    """
    CREATE TABLE IF NOT EXISTS journal_retraction (
      seq         INTEGER PRIMARY KEY AUTOINCREMENT,
      id          TEXT NOT NULL UNIQUE,
      subject_id  TEXT NOT NULL REFERENCES subject(id),
      line_id     TEXT NOT NULL,
      session_id  TEXT,
      role        TEXT,
      reason      TEXT,
      excerpt     TEXT,
      actor       TEXT NOT NULL,
      created_at  TEXT NOT NULL,
      UNIQUE(subject_id, line_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jretr_line ON journal_retraction(subject_id, line_id)",
    # 原文兜底索引（1.7.0）：派生事实会截断/改人称，原话里丢掉的细节靠它找回来。
    # 只做兜底：派生结果薄弱时才查，避免每轮都多一份注入成本。
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS journal_fts USING fts5(
      text_seg,
      tokenize="unicode61 remove_diacritics 2"
    )
    """,
    # ---- 日记正文索引（1.9.0 / M18）：日记自己的检索通道 ----
    # 为什么日记要单独一条通道：日记（episode）是「那一天发生了什么」这一粒度的叙述，
    # 和记忆单元（一条事实）不是一回事。以前它只出现在醒来卡的「最近」一行，
    # 于是「上周咱们都干嘛了」这种问题在事实库里搜不到——派生的事实被截断、被改写，
    # 一天的经过只剩零散几句。rowid 直接对应 episode 的隐式 rowid，派生可重放。
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS episode_fts USING fts5(
      text_seg,
      tokenize="unicode61 remove_diacritics 2"
    )
    """,
    # ---- 关联扩散图（1.8.0 / M12）：记忆之间的稀有词共现边 ----
    # 完全派生：边只由 memory.text_seg 决定，可整表重算（memory/graph.py: rebuild）。
    # 为什么不复用实体图：实体抽取「宁可漏不可错」，日常叙事的细节大多不落在实体上；
    # 稀有词共现是同一层信息，但不需要额外抽取，覆盖率天然 100%。
    """
    CREATE TABLE IF NOT EXISTS memory_edge (
      a_seq    INTEGER NOT NULL REFERENCES memory(seq) ON DELETE CASCADE,
      b_seq    INTEGER NOT NULL REFERENCES memory(seq) ON DELETE CASCADE,
      rel      TEXT NOT NULL DEFAULT 'related' CHECK (rel IN ('related')),
      w        REAL NOT NULL,
      built_at TEXT NOT NULL,
      PRIMARY KEY (a_seq, b_seq, rel)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_me_b ON memory_edge(b_seq)",
]

# 初始谓词表（03 §8.2；M3 扩到 12 个，后续睡眠/进化阶段再拆细）
# 字段：predicate, inverse, domain, range, transitive, max_weight
RELATION_TYPES: list[tuple[str, str | None, str | None, str | None, int, float]] = [
    ("likes", "liked_by", "person", "concept", 0, 0.7),
    ("dislikes", None, "person", "concept", 0, 0.7),
    ("works_at", None, "person", "org", 0, 0.75),
    ("lives_in", None, "person", "place", 0, 0.8),
    ("uses", "used_by", "person", "tool", 0, 0.6),
    ("part_of", "has_part", "concept", "concept", 1, 0.7),
    ("related_to", "related_to", None, None, 0, 0.4),
    ("works_on", "worked_on_by", "person", "goal", 0, 0.8),
    ("knows", "knows", "person", "person", 1, 0.6),
    ("located_in", None, "place", "place", 1, 0.8),
    ("studies_at", None, "person", "org", 0, 0.75),
    ("born_in", None, "person", "place", 0, 0.8),
]
