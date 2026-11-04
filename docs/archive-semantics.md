# 删除语义：软删是「停放」，不是「消失」

> 日期：2026-11-03 ｜ 状态：**已落地**（无 schema 变更 / PMem 0.7.2）
> 影响面：`grp_member`、`target_grade` 与软删记忆的关系，I17 的判定口径，分组列表的显示侧。

---

## 0. 一句话

**软删的记忆还在库里，只是不出场。** 所以它的分组、等级都该留着 —— 留着才捞得回来。
`pmem doctor` 的 I17 因此只问「宿主是不是真的没了」，不问「宿主现在是什么状态」。

---

## 1. 问题是怎么来的

`pmem doctor` 长期有一条红灯：

```
不变量：13/14 通过
I17  1 条成员指向已删除目标
```

追下去是一条普通的记录：一条「上传停稿」的记忆，落在 `我/停机坪`，建好十分钟后被我软删。
`memory.status` 变成 `deleted`，但 `grp_member` 那一行还在 —— 因为软删的代码路径只改状态：

```python
# store.delete(hard=False)
conn.execute("UPDATE memory SET status='deleted', deleted_at=? WHERE id=?", ...)
```

I17 当时的写法把「指向一条 `status='deleted'` 的记忆」当成损坏：

```sql
SELECT COUNT(*) FROM grp_member gm WHERE
  gm.target_tbl='memory' AND NOT EXISTS
   (SELECT 1 FROM memory m WHERE m.id=gm.target_id AND m.status<>'deleted')
```

这条 SQL 并没有判断"宿主不存在"，它判断的是"宿主不存在**或**宿主被停放"。
两件事被混进了一个计数，于是**正常状态被报成了损坏**。

红灯本身不可怕，可怕的是它会教人习惯红灯 —— 红灯多了，真损坏就没人看了。

---

## 2. 两种删除

| | 软删 `delete(hard=False)` | 真删 `delete(hard=True)` |
|---|---|---|
| `memory` 行 | 留着，`status='deleted'`，记 `deleted_at` | 物理删除 |
| FTS / 向量 | 留着（行还在） | 级联清掉 |
| `grp_member` | **留着** | 清掉 |
| `target_grade` | **留着** | 清掉 |
| `surface_log` | 留着（历史如实记录） | 清掉 |
| `relation.evidence_json` | 留着（指针指向的行还在，I4 不报） | 从 JSON 里摘掉指针，关系空了就整条删 |
| 事件日志 | `memory.delete` | `memory.hard_delete`（墓碑） |
| 如何恢复 | `memory_update(id, set={status:'active'})` | 只能从备份 |

软删是**可逆**的，真删不是。既然可逆，就得把恢复所需的状态都留着 —— 其中就包括「它在哪个分组」。

---

## 3. 停放的三个不变量

一条停放的记忆：

1. **行在** —— 正文、来源、时间戳一个不动（原文不可变本来也不允许动）。
2. **分组在** —— `grp_member` 保留，捞回来的瞬间它就回到 `我/停机坪`，不需要重新分组。
3. **等级在** —— `target_grade` 保留，S/A 级不会因为一次误删掉成无级。

**任何列表都不显示它**（延伸 I10）：

```python
# memory/groups.py
LIVE_MEMBER = (
    "NOT (gm.target_tbl='memory' AND EXISTS "
    "(SELECT 1 FROM memory m WHERE m.id=gm.target_id AND m.status='deleted')) "
    "AND NOT (gm.target_tbl='entity' AND EXISTS "
    "(SELECT 1 FROM entity e WHERE e.id=gm.target_id AND e.status<>'active'))"
)
```

`groups.targets()`（分组里有什么）和 `groups.tree()`（每个组几个人）都挂这个条件。
计数和列表必须用同一把尺子 —— 否则会出现「组里显示 3 个，点进去 2 条」这种没人信的数。

`groups_for()` **不挂**。它是「这条记忆属于哪」的直答：你主动要一条停放记忆
（`include_deleted=True`）时，答案应该包括它原来的分组，否则你没法判断该不该捞。

---

## 4. 恢复要清掉删除时间

以前 `memory_update(set={status:'active'})` 只改状态，`deleted_at` 会留在一条活着的记忆上。
`CHECK (status <> 'deleted' OR deleted_at IS NOT NULL)` 只约束了一个方向，所以这不会报错，
但数据会撒谎：将来想按 `deleted_at` 清理墓碑的人会误伤活记忆。

```python
if set_fields.get("status") == "deleted":
    assignments += ", deleted_at=?"
elif row["status"] == "deleted" and "status" in set_fields:
    assignments += ", deleted_at=NULL"   # 捞回来时一起清
```

---

## 5. 为什么不顺手删掉那条 `grp_member`

最短的修法是让软删顺手删成员、恢复时再插回去。不这么做，是因为恢复时**插不回原来的位置**：

- `grp_member.created_at` 是"何时进组"，重插会变成"捞回那天才进组"；
- 组可能在停放期间被改名/合并/删掉，重插要处理一堆边界；
- 万一恢复失败，成员关系已经丢了 —— 一次误删变成两次数据损失。

**删除操作应该是保守的：宁可留一行暂时没用的关系，也不要在删除路径上做额外的破坏性写入。**
I17 是检测器，不是清理器；检测器看到正常状态应该闭嘴，而不是要求数据变形来迁就它。

---

## 6. 实体为什么不一样

`grp_member` 是多态的（`target_tbl` ∈ memory/entity/trait/experience），所以每类宿主的
"活着"定义由业务决定：

- **记忆**：软删 = 停放 → 存在即合法。
- **实体**：合并（`entities.merge`）会把成员**迁到**保留的那一侧，所以留在非活跃实体上的
  成员就是"没迁干净"的残留 → 仍然算 I17 违规。

这不矛盾：差别不在 I17，在于这两种操作对"删除"的定义本来就不同。

---

## 7. 测试

`tests/test_archive.py`（9 条）：

| 断言 | 守的是什么 |
|---|---|
| 软删后 `groups_for` / `grade_of` 仍在 | 恢复所需的状态没丢 |
| `targets()` 不列出停放记忆 | 显示侧不漏 |
| `tree()` 的人数不含停放 | 计数与列表同尺 |
| I17 在停放时通过，且 detail 里报出条数 | 不藏，但也不误报 |
| 手工插一条指向不存在记忆的成员时 I17 失败 | 真损坏仍然抓得到 |
| 真删后成员与等级清空 | 真删路径没退化 |
| 恢复后 `deleted_at` 为 NULL 且回到原组 | 可逆是真是假 |
| 改 `status='deleted'` 会盖时间戳 | 另一条路径没退化 |
| 普通字段更新不碰 `deleted_at` | 没有过度清理 |

---

## 8. 落地情况

无 schema 变更（`grp_member` 本来就是多态表，不需要动结构）。真实库：

```
$ pmem doctor
完整性：ok
事件链：ok（442 events verified）
不变量：14/14 通过
```

I17 的 detail 现在是：

```
0 个分组路径/深度不一致，0 条成员指向非活跃分组，0 条成员指向已消失的目标
（另有 1 条挂在停放记忆上，属软删保留，不报错）
```

那条 `我/停机坪` 的成员关系原样留着。它不影响任何显示，捞回来就能用。

---

## 9. 没做的事

- **自动过期清理**（软删 7 天后真空删）。目前"7 天内可恢复"靠的是行还在，没有任何定时器
  去清理过期的停放行。要不要做、做在 `sleep` 还是 `pmem gc`，是另一个决定。
- **`trait` / `experience` 的成员**。I17 目前只查 memory 与 entity 两类宿主；
  多态表的另外两类还没有对应的"活着"定义。
