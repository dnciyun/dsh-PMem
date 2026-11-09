# 文档索引

公开仓库里只带两篇文档 + 这份索引：

| 文件 | 内容 |
|---|---|
| [`07-hippocampus.md`](07-hippocampus.md) | 和第三方记忆插件的取舍记录：为什么最后只留数据、不留插件 |
| [`archive-semantics.md`](archive-semantics.md) | 「淡忘 / 归档 / 删除 / 撤回」到底各是什么意思 |

## 关于源码注释里的 `PLAN.md` / `docs/02…06` 引用

那是**内部开发文档的编号**（`PLAN.md`、`02-data-model.md`、`03-algorithms.md`、
`04-interface.md`、`05-evaluation.md`、`06-traceability.md`），不随公开仓库发布：
里面混着本地跑分、对比材料与个人对话，发出来对读者没好处、对别人也不礼貌。

源码注释引用它们是「给改造这套系统的人一个索引」，不是坏链接承诺。想看设计意图，
可以从这几处入手（都在公开仓库里）：

- 数据结构：`src/pmem/base/schema.py`（建表语句自带「为什么这么设计」注释）；
- 检索打分：`src/pmem/memory/retrieval/search.py` 里的 `run_search()`（每一路通道都有实测注释）；
- 接口契约：`README.md` 的「测试数据」与 `deploy/`；
- 行为边界：`tests/` 里每个用例的文件头（很多测试文件开头就是一篇设计说明）。

如果确实需要完整的设计文档，可以联系作者。
