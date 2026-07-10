# Admin Session Query Projection Test Plan / Admin 会话查询投影测试计划

日期：2026-07-10

状态：Draft

设计文档：[03-design.md](./03-design.md)

需求：[02-requirements.md](./02-requirements.md)

输入分析：[01-analysis.md](./01-analysis.md)

实现计划：[05-implementation-plan.md](./05-implementation-plan.md)

迁移运行手册：[06-migration-runbook.md](./06-migration-runbook.md)

生产化路线图：[production-readiness-roadmap.md](../production-readiness-roadmap.md)

## 1. 目的

验证 Admin session query adapters 在不改变现有业务语义和权限的前提下，确实让无搜索列表与 daily analytics 脱离 session 文件全量扫描，并且在 backfill、并发写、删除、SQLite/PostgreSQL 故障和回滚时保持可解释的一致性。

本计划把“结果正确”和“查询不再扫文件”作为两个独立门禁；只有接口测试通过但仍然读取全部 `.meta.json`，不算优化完成。

## 2. 测试层级

| 层级 | 主要对象 | 目标 |
| --- | --- | --- |
| Module | `AdminSessionQuery` + SQLite/PostgreSQL adapters | 共享 contract、schema、projection、filter/sort/page/daily、状态和竞争语义。 |
| Integration | `Session` / `SessionService` / VikingFS fixture / query adapter | 所有 mutation 都能投影；事实源与派生层失败隔离。 |
| API | Admin routers + auth + read modes | 兼容 response、权限、fallback、503 和 query metadata。 |
| UI/build | Dashboard、Sessions、Pages.css | loading/empty/stale/error、文案、可访问性和稳定布局。 |
| Benchmark | synthetic index + HTTP/VikingFS fixture | p50/p95、I/O budget、规模增长曲线和写入开销。 |
| Operations | rebuild CLI + restart/fault injection | dry-run、resume、verify、corruption recovery 和 rollout/rollback。 |

## 3. 固定语义基线

以下行为必须先由现有测试固化，并在 index read path 下重复执行：

- `activity_at = last_message_at || updated_at || created_at`。
- daily analytics 把一个 session 的全部 summary 计入最后活动日，不是逐 message 日期统计。
- 请求 `tz` 决定日期过滤与 day bucket。
- 正常日期范围不超过 120 天时补齐空日；更大范围不强制补全部空日。
- stable sort：主字段后按 `last_active DESC`、`user_id ASC`、`session_id ASC` 处理 tie。
- page 超过末页时 clamp 到最后一页；无结果时 page 为 1、total_pages 为 0。
- `q` 搜索匹配所有 text part、tool fields，并读取 live/archive raw messages。
- detail 即使 `commit_count` 过期也读取实际 archive。
- account ADMIN 不能访问其他 account；ROOT 可按现有规则访问。

相关现有覆盖集中在 [test_admin_api.py](../../../tests/server/test_admin_api.py#L533-L1730)。

## 4. Module 测试

建议新建共享 contract 和 adapter tests：

```text
tests/service/test_admin_session_query_contract.py
tests/service/test_admin_session_query_sqlite.py
tests/service/test_admin_session_query_postgres.py
```

共享文件是 pytest 可发现的参数化 contract suite。Adapter-specific fixture 只提供 factory 和故障注入，不复制业务断言。SQLite 测试使用临时目录中的真实 SQLite 文件；PostgreSQL 测试使用临时数据库；两者都不 mock SQL。

### 4.1 初始化与 schema

1. 空目录初始化创建 DB、state table 和 session table。
2. 重复初始化幂等，不丢已有 row。
3. schema version 相同时不运行 migration。
4. 可升级旧 schema 时在事务内完成 migration。
5. 不可识别的未来 schema version 进入 `error`，不降级覆盖文件。
6. WAL、foreign keys、busy timeout 和 file mode 符合设计。
7. connection/executor 在 `close()` 后释放，WAL checkpoint 行为可预期。
8. PostgreSQL schema migration、pool、statement/lock timeout 和 transaction snapshot 符合设计。
9. PostgreSQL 微秒时间、计数、token 和 generation 使用 `BIGINT`；用大于 32-bit 的值 round-trip，不能溢出或截断。

### 4.2 Projection

| 用例 | 操作 | 期望 |
| --- | --- | --- |
| Insert | project 新 session meta | 生成完整 row，state/last projection 更新。 |
| Idempotent | 相同 fingerprint 重复 project | 业务 row 不变化，不重复计数。 |
| Update | message/tool/feedback count 改变 | 所有 typed column 与 payload 同步。 |
| Older write | 先写新 meta，再写更旧 `source_meta_updated_at` | 旧值不能覆盖新值。 |
| Equal timestamp | 相同时间、不同 fingerprint | online 优先于 rebuild；其余冲突保留现有 row 并记录 conflict，不按 hash 随机覆盖。 |
| Recreate | tombstone 后在线事实源明确重新创建同 key | 在同一事务清除 tombstone，session 恢复可见。 |
| Invalid meta | summary incomplete/version old | 拒绝进入 ready payload，标记 repair pending。 |
| Numeric normalization | `None`、string number、负数或超大值 | 按设计规范化或拒绝，不能生成 SQL 类型漂移。 |

### 4.3 Account isolation

构造两个 account，使用相同 `user_id` 与 `session_id`：

- composite primary key 可同时保存两行。
- account A list/daily/status 不出现 account B 数据。
- 即使调用者遗漏 user filter，SQL 仍强制 account predicate。
- payload 中的 identity 由 typed columns 覆盖，伪造 payload identity 不得越权。
- Account delete purge 所有 row/state/checkpoint；子账号删除保持历史 session。

### 4.4 列表查询

对每个允许的 `sort_by` 做 asc/desc 参数化测试：

- `last_active`
- `created_at`
- `updated_at`
- `message_count`
- `user_message_count`
- `assistant_message_count`
- `tool_call_count`
- `failed_tool_call_count`
- `token_total`
- `matched_message_count`（无 `q` 时固定为 0，按稳定 tie-breaker 排序）
- `feedback_count`
- `positive_feedback_count`
- `negative_feedback_count`
- `user_id`

每组断言：

1. 主字段顺序正确。
2. 相等值的 last-active/user/session tie-breaker 稳定。
3. 不同时区 offset 表示的同一 instant 顺序相同。
4. user/date filter 在分页前应用。
5. `total`、`items`、`total_pages` 来自一致快照。
6. page 1、page N、末页、越界页、空页行为与当前 API 相同。
7. 未知 `sort_by` 不进入 SQL 拼接，按现有默认值处理。

### 4.5 Daily analytics

构造 session：

- A：`2026-06-13T23:30:00Z`，2 user messages、1 assistant、1 feedback。
- B：`2026-06-14T01:00:00Z`，另一个 user。
- C：无 `last_message_at`，使用 `updated_at`。
- D：无 last/updated，使用 `created_at`。

分别以 `UTC`、`Asia/Singapore`、`America/Los_Angeles` 查询，断言：

- bucket 日历日正确。
- whole-session summary 只进入一个最后活动日 bucket。
- daily active user 与 totals active user 去重正确。
- token/feedback/tool counts 与现有实现相同。
- 14/30/120 天补空日；121 天范围只返回有数据日。
- DST 切换日不重复或遗漏 session。
- naive timestamp 继续按 UTC 处理。

### 4.6 Tombstone 与 generation

1. delete 后 row 不出现在 list/daily，但 tombstone 保留。
2. backfill 读到旧 meta 后发生 delete，迟到 upsert 不得复活 row。
3. row 从未投影时发生 delete，独立 tombstone 仍能阻止迟到 rebuild 创建 row。
4. rebuild generation 中在线 update，mark-and-sweep 不得删除新 row。
5. delete 后同 identity 明确重新创建，在线 projection 在同一事务清除 tombstone 并恢复 active row。
6. source 中已不存在的旧 row 在完整 generation 成功后被 sweep。
7. rebuild 中断时不执行 sweep，账号不能进入 `ready`。
8. resume 从 checkpoint 继续，最终结果与从头 rebuild 一致。

## 5. Session/Service 集成测试

建议新建：

```text
tests/session/test_admin_session_projection.py
tests/service/test_admin_session_index_rebuild.py
```

### 5.1 Mutation 覆盖

每个用例同时读取事实源 `.meta.json` 与 index row：

1. `Session.ensure_exists()`：空 session 可在 Admin list 中出现，计数为零。
2. `Session.add_message(user)`：message/user count、first/last/activity time 更新。
3. `Session.add_message(assistant, token_usage)`：assistant/token count 更新。
4. 添加 tool part：tool count 更新。
5. `update_tool_part(failed -> completed)` 与反向更新：failed count 正确增减。
6. `set_message_feedback(up/down)`：feedback summary 更新。
7. `clear_message_feedback()`：feedback count 回零。
8. feedback memory `pending/completed/failed/skipped`：对应 count 更新。
9. `commit_async()`：live messages 清空后累计 audit summary 仍保持现有语义。
10. 普通 delete 与 Admin delete：事实目录删除，index tombstone。

### 5.2 sync/async seam

- 从同步 `add_message()`/`update_tool_part()` 进入 `_save_meta_sync()` 时 projection 确实执行。
- 从异步 feedback/commit/ensure-exists 进入 `_save_meta()` 时只执行一次 projection。
- 不能只通过 HTTP route 更新；直接 local client/session 调用也必须投影。
- read mode `off` 且 adapter disabled 时不创建 DB，不增加 projection side effect。
- adapter 为 SQLite/PostgreSQL 且账号 `read_mode=off` 时可以先 projection/rebuild，但 list/daily 仍只返回 filesystem 结果。

### 5.3 失败隔离

注入 projection failure：

- add message 仍返回成功且 raw message/meta 已保存。
- feedback 仍保存，不能因派生层失败丢反馈。
- commit 事实归档不回滚。
- index state 转为 stale/error，repair pending 增加。
- reconciliation 成功后 row 与 meta 一致，repair pending 清零。

注入事实源写失败：

- projection 不得先于事实源成功。
- index 不得出现未提交的 message/feedback/update。

## 6. Backfill 与枚举测试

### 6.1 超过默认 node limit

使用轻量 fake AGFS/VikingFS fixture 构造：

- 1 account、1001 users、每 user 1 session。
- 1 account、1 user、1001 sessions。
- 100 users、每 user 1001 sessions。

断言：

- rebuild source 看到全部节点，最终 source/index count 一致。
- 任何底层 truncated/unknown completeness 状态都使 rebuild 失败，不能标 `ready`。
- 兼容 filesystem 请求若触发 cap，response `partial=true` 或返回明确错误，不能静默宣称 total 完整。

### 6.2 Legacy summary

构造没有 `audit_summary`、旧 version 和 `audit_summary_complete=false` 的 session：

- Admin list/daily ready-index 请求不读取 raw messages 做 request-time backfill。
- rebuild 读取 raw messages并直接投影，默认不修改 `.meta.json`。
- 单个损坏 session 被记录为 per-item error，账号是否允许 ready 按明确策略执行；默认要求零未解释错误。
- 重跑不会重复计数或重复 archive 读取已修复 session。
- 独立 `repair-meta --dry-run/--resume` 才写回 current summary，并验证旧版本兼容。

### 6.3 中断与并发

- 在 25%、50%、99% checkpoint 强制中断并 resume。
- rebuild 期间并发 add message、feedback、delete、recreate。
- backfill 旧 row 不能覆盖在线新 row。
- 完成后抽样比对 meta fingerprint 和 index fingerprint。

## 7. API 合同测试

扩展 [test_admin_api.py](../../../tests/server/test_admin_api.py)：

### 7.1 多 adapter 兼容

将现有 session audit/daily tests 参数化为：

- `read_mode=off`
- SQLite `read_mode=prefer` 且账号 `ready`
- PostgreSQL `read_mode=prefer` 且账号 `ready`

对同一 fixture 比较业务 payload：

- items identity、count、token、feedback。
- sort/page metadata。
- daily rows/totals/timezone。
- detail/messages/feedback。

允许只在 index 模式新增 `query_meta`。

### 7.2 `query_meta`

| 场景 | 期望 source | 期望 state |
| --- | --- | --- |
| ready 无搜索 list | `index` | `ready` |
| ready daily | `index` | `ready` |
| shadow | `filesystem` | `building` 或 `ready` |
| prefer 未完成 | `filesystem` | `building` |
| prefer stale | `filesystem` | `stale` |
| q search | `raw_search` | 当前 index state |

断言 `partial`、`fallback_reason`、`last_projection_at` 的 null/非 null 语义。

另外用两个 account 设置不同 `read_mode`，验证切换一个租户不会改变另一个租户的 source、fallback 或错误语义；服务重启后 account-scoped mode 仍从状态存储恢复。

### 7.3 Required mode

- ready：正常 200。
- building/stale/error/schema mismatch：无搜索 list/daily 返回 HTTP 503。
- error code 为 `ADMIN_SESSION_INDEX_UNAVAILABLE`。
- 不返回旧 items/totals 冒充新结果。
- `q` raw search 与 detail 仍按设计可用。

### 7.4 权限与输入

重复现有：

- invalid `user_id`、date、timezone 返回 422。
- USER 不能访问 Admin endpoint。
- account ADMIN 不能跨 account。
- ROOT 访问规则不变。
- `sort_by` 白名单不能产生 SQL injection。
- page/page_size 上限保持。

## 8. I/O budget 测试

这是 release hard gate。对 `VikingFS.ls/read_file` 安装 spy，并确保账号 index 已 `ready`。

| 请求 | `ls` | `.meta.json` read | `messages.jsonl` read | Query storage |
| --- | ---: | ---: | ---: | ---: |
| list，无 q，page 1 | 0 | 0 | 0 | SQLite/PostgreSQL |
| list，无 q，page N | 0 | 0 | 0 | SQLite/PostgreSQL |
| daily，14 天 | 0 | 0 | 0 | SQLite/PostgreSQL |
| daily，120 天 | 0 | 0 | 0 | SQLite/PostgreSQL |
| list，带 q | >= 1 | >= 0 | >= 1 | 可选 join |
| detail | >= 1 archive listing | >= 1 | >= 1 | 0 或仅 status |

同时断言：

- ready list/daily 不调用 `_collect_admin_session_summaries()`。
- ready list/daily 不调用 `_backfill_admin_session_meta_summary()`。
- `page_size` 只影响返回条数，不再改变文件 IO，因为文件 IO 必须为零。

## 9. 故障注入

### 9.1 SQLite busy/locked

- 短暂 lock 在 busy timeout 内恢复：请求/投影成功并记录 wait。
- 超时：projection 不影响事实写；query 按 mode fallback 或 503。
- 连续失败后 state 为 stale/error，不无限阻塞 event loop。

### 9.2 磁盘与文件

- index path 不可写。
- disk full/`SQLITE_FULL`。
- DB header 损坏。
- WAL/SHM 遗留后的重启。
- file permission 错误。
- schema migration 中断。

每种场景断言：日志含稳定 error code，不打印密钥或 message text；`off/shadow/prefer/required` 行为符合设计。

### 9.3 进程与部署

- shutdown 等待/取消 rebuild，connection 正常关闭。
- crash 后 checkpoint 可恢复。
- 检测不支持的多进程/共享本地 index 配置时 fail-fast。
- application encryption 开启且未显式允许明文派生元数据时拒绝启动 index。

### 9.4 PostgreSQL

- Connection pool acquire timeout。
- Statement timeout 与 lock timeout。
- Transaction serialization/deadlock retry。
- Database failover/connection reset。
- Migration role 与 runtime role 权限错误。
- 多 API/worker 实例并发 project/rebuild。
- PITR 恢复后 projection full rebuild。

## 10. Frontend 验证

当前 `admin/package.json` 没有独立 test/lint script，因此 v1 至少执行 build 与手动浏览器 QA；若实现引入纯 helper，可在同一变更中补最小测试脚本，但不为一个状态条引入大型 UI 测试体系。

### 10.1 Dashboard

1. 初次 loading：显示稳定 loader，不短暂显示全零 KPI。
2. 成功零数据：显示真实零会话/暂无反馈，不出现错误条。
3. refresh：保留旧数据并淡化，完成后原位更新。
4. analytics 失败：不把 totals 清零；显示“重新加载统计”。
5. building/fallback：显示文本+图标 warning，KPI 是否来自 fallback 清晰。
6. stale：显示数据截止时间，不只使用黄色。
7. 503 required：显示索引不可用与修复建议，不把 users/health 一并判失败。

### 10.2 Sessions

1. ready index：无额外成功 banner。
2. building/fallback/stale/error：状态条位置稳定，表头和分页不跳动。
3. raw search：helper 表明会读取会话内容且可能较慢。
4. loading 时已有 rows 保持并淡化；空列表文案不变。
5. error 后“重新加载会话”可键盘触发，focus ring 可见。
6. warning/error 使用 icon+text，不只靠颜色。
7. selected row、排序按钮、分页 disabled 状态不回归。

### 10.3 响应式与可访问性

桌面至少验证 1440×900，移动至少验证 390×844：

- 状态文本换行不遮挡日期、搜索、刷新按钮。
- table 仍可横向滚动，不被 banner 扩宽。
- icon-only control 保持 accessible name。
- status/alert 使用合适的 `role=status` 或 `role=alert`，避免重复播报 refresh。
- reduced motion 下没有新增非必要动画。

## 11. Benchmark 计划

建议新增：

```text
scripts/benchmark_admin_sessions.py
```

脚本必须记录 commit、Python/SQLite/PostgreSQL 版本、机器、磁盘、adapter、read mode、fixture seed 和冷热缓存状态。

### 11.1 数据集

| Profile | Users | Sessions/user | Total sessions | 用途 |
| --- | ---: | ---: | ---: | --- |
| S | 10 | 100 | 1,000 | CI smoke |
| M | 100 | 100 | 10,000 | 日常回归 |
| L | 100 | 1,000 | 100,000 | release benchmark |

PostgreSQL 另跑 Fleet profile：建立 8000 个 account state，session 数使用 Phase 0 得到的租户分布并包含至少一个 100,000-session 热点租户；执行冷热租户混合查询、projection 和限速 rebuild。Fleet profile 不进入普通 CI，但属于 production canary 前硬门禁。

summary 分布包含：

- 0/4/20/100 message counts。
- 0/3/10 archive metadata（raw search/detail profile 使用）。
- 不同 token、tool failure、positive/negative feedback。
- 最近 14/30/120 天与范围外 activity time。
- 大量相同 sort value，用于稳定排序。

### 11.2 场景

每个场景 warm-up 10 次，测量至少 100 次并报告 p50/p95/p99：

- list page 1，page_size 50。
- list 中间页、末页、越界页。
- 每个常用 sort：last_active、message_count、negative_feedback_count、token_total。
- user filter + 14 天日期 filter。
- daily 14/30/120 天。
- projection 单写与 20 并发写。
- 8000-account fleet 下混合 list/daily/project/rebuild，验证 pool 配额和 noisy-neighbor 隔离。
- q search 和 detail 作为未优化对照，不纳入 v1 query-projection SLO。

### 11.3 硬门禁

1. L profile ready list/daily 的 session VikingFS IO 为零。
2. list page 1 p95 目标不高于 300 ms；daily 14 天 p95 目标不高于 500 ms。首次 Stage 0 baseline 后可以基于固定参考机收紧，不能无记录地放宽。
3. 从 M 到 L，list page 1 p95 不得按 10 倍 session 数近似线性增长；接受条件为 `p95(L) <= 2 * p95(M) + 100ms`。Daily 记录候选/scanned row，并以 14 天 p95 绝对门禁和 Phase 0 批准的扫描预算验收；若超过 500 ms 或扫描预算，必须在该规模切读前增加 daily materialization，不得放宽为“只要不读文件即可”。
4. Fleet profile 中热点租户的限速 rebuild 不得导致其他租户 query 超出批准 SLO、pool acquire timeout 或饥饿；并发模型和阈值必须随 Phase 0 基线归档。
5. 单次 projection p95 对 session 写路径增加不高于 20 ms；超出时必须解释 adapter lock/fsync/transaction/pool 配置或改为有界 batch 设计。
6. event loop heartbeat 在并发 projection 时最大延迟不高于 100 ms，证明 sqlite 工作未在主 loop 执行。

CI 只跑 S profile 的功能和 I/O gate；M/L 在 release 或 nightly 环境运行，避免共享 CI 噪声造成随机失败。

## 12. Shadow 比对

`read_mode=shadow` 时按采样率对同一请求执行两条读路径，并比较：

- total、total_pages。
- ordered `(account_id, user_id, session_id)`。
- 所有 list typed count/token/feedback 字段。
- daily date rows 与 totals。
- response timezone。

差异日志只记录 identity、field name、fingerprint 和数值摘要，不记录 raw messages。连续一个发布周期无未解释差异，才允许切 `prefer`。

## 13. 运维验收

### 13.1 CLI

验证：

```bash
python scripts/admin_session_index.py status --account <id>
python scripts/admin_session_index.py rebuild --account <id> --dry-run
python scripts/admin_session_index.py rebuild --account <id> --resume
python scripts/admin_session_index.py verify --account <id>
```

验收：

- dry-run 不改 DB/meta，输出预计 user/session、legacy、bytes 和 truncation risk。
- rebuild 有 checkpoint、进度、错误摘要和非零失败退出码。
- verify 比较 count、抽样 fingerprint、tombstone 和 repair pending。
- 命令输出不包含 API key、message content 或 feedback reason。

### 13.2 Rollout/rollback

1. `off -> shadow` 不改变 API 业务结果。
2. `shadow -> prefer` 后 ready account 使用 index，未 ready account 明确 fallback。
3. `prefer -> required` 后 index 故障返回 503，不扫文件。
4. 任意阶段切回 `off` 后现有 filesystem API 恢复。
5. 删除 SQLite DB 或清空 PostgreSQL projection 后 rebuild 可恢复全部 list/daily 结果。

## 14. 编码门禁

实现完成后至少运行：

```bash
npm run build --prefix admin
uv run pytest tests/service/test_admin_session_query_contract.py
uv run pytest tests/service/test_admin_session_query_sqlite.py
uv run pytest tests/service/test_admin_session_query_postgres.py
uv run pytest tests/service/test_admin_session_index_rebuild.py
uv run pytest tests/session/test_admin_session_projection.py
uv run pytest tests/server/test_admin_api.py tests/server/test_api_sessions.py
uv run ruff check openviking tests scripts
git diff --check
```

必须先构建 Admin frontend，再运行会初始化 FastAPI static mount 的 backend tests，避免 `admin/dist/assets` 构建竞争造成假失败。

## 15. 发布判定

只有以下全部成立才允许目标 adapter 从 `shadow` 切到 `prefer`；生产租户必须使用 PostgreSQL adapter：

- 功能/API 双模式结果一致。
- ready list/daily I/O hard gate 通过。
- backfill >1000 节点、中断续跑、并发 mutation/delete 通过。
- projection 失败隔离和 required 503 通过。
- Dashboard 不再把 analytics error 显示成零数据。
- S/M/L benchmark 有保存结果，达到硬门禁或有经过评审的明确例外。
- shadow 差异为零，或所有差异都有已修复并回归的原因。
- 运维人员能执行 status/rebuild/verify/rollback。
- PostgreSQL adapter 已通过共享 contract、故障、并发和 PITR 演练。
