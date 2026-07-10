# Admin Session Performance Implementation Plan / Admin 会话性能实现计划

日期：2026-07-10

状态：Draft

相关文档：

- [需求](./02-requirements.md)
- [设计](./03-design.md)
- [测试计划](./04-test-plan.md)
- [迁移运行手册](./06-migration-runbook.md)
- [生产化路线图](../production-readiness-roadmap.md)

## 1. 实现目标

以小型、可独立验证的纵向切片实现 `AdminSessionQuery` 深模块：先建立观测和兼容 contract，再实现 SQLite 本地 adapter、历史迁移与灰度切换，最后实现 PostgreSQL production adapter。

计划不追求一次提交完成最终架构。每个切片都必须：

- 保持现有 API/权限兼容。
- 有自动化测试和观测证据。
- 不依赖未完成的后续切片才能安全合并。
- 有明确开关或回滚方式。

## 2. 固定设计约束

1. 酒店租户=`account_id`，子账号=`user_id`。
2. `ROOT/ADMIN/USER` 角色语义不变。
3. Session 文件是本阶段事实源。
4. Query storage 是派生层。
5. `AdminSessionQuery` 是调用者和测试共同穿过的 interface。
6. SQLite 与 PostgreSQL 运行同一 contract suite。
7. Rebuild 默认只读；meta repair 独立。
8. 所有切读按 account 执行。

## 3. 目标模块结构

```text
openviking/service/admin_session_query/
  __init__.py
  models.py                 query/result/change/status types
  interface.py              AdminSessionQuery interface
  sqlite_adapter.py         local/test/shadow adapter
  postgres_adapter.py       production adapter
  projection.py             SessionMeta -> projection row
  rebuild.py                source scan/generation/checkpoint
  contract.py               shared validation helpers

openviking/service/session_service.py
openviking/session/session.py
openviking/service/core.py
openviking/server/routers/admin.py
openviking/storage/observers/prometheus_observer.py

scripts/admin_session_index.py
```

不创建泛化到所有业务的 BaseRepository。该 module 只隐藏 Admin session 查询和迁移复杂度。

## 4. 依赖顺序

```text
Baseline/contract
      |
      v
Domain models + interface
      |
      +--> SQLite adapter --> local rebuild --> shadow
      |
      +--> PostgreSQL adapter --> production rebuild --> per-account cutover
      |
      v
Projection hook + status/query_meta + UI states
```

Projection hook 可以在 adapter 完成前通过 disabled/recording implementation 合并；读路径切换必须等 adapter、migration 和 contract tests 完成。

## 5. 实现切片

### Slice 0：固化现有契约

改动：

- 为 list/daily/search/detail 增加或整理兼容 contract fixture。
- 固化排序 tie-breaker、timezone、120-day bucket、page clamp。
- 固化 ROOT/ADMIN/USER 和跨 account 行为。

测试：

- 复用/扩展 `tests/server/test_admin_api.py`。
- 不改变生产代码行为。

完成条件：后续 adapter 可以对同一 fixture 比较业务结果。

### Slice 1：I/O 观测与枚举完整性

改动：

- Admin query telemetry context。
- 统计 user/session/meta/raw/archive read 和 estimated bytes。
- 增加不会静默截断的内部完整枚举 interface。
- 兼容 scan 触发 cap 时返回 partial/error 语义。

测试：

- 1001 users。
- 1001 sessions/user。
- 指标计数和 disabled overhead。

回滚：关闭 telemetry；枚举正确性修复保留。

### Slice 2：领域类型与深模块 interface

改动：

- `SessionIdentity`。
- `SessionProjectionChange`。
- `AdminSessionListQuery/Page`。
- `AdminDailyQuery/Analytics`。
- `AdminSessionIndexStatus`。
- `AdminSessionQuery` interface。

要求：

- Query 类型必须包含 account scope。
- 调用者不能传裸 SQL sort field。
- Error mode、freshness 和 performance characteristic 写入 docstring/interface tests。

测试：

- 类型规范化和白名单。
- 跨 account identity 不相等。

### Slice 3：共享 contract suite

新增：

```text
tests/service/test_admin_session_query_contract.py
```

覆盖：

- upsert/idempotence/older write/tombstone。
- 全部 sort/page/filter。
- daily timezone 和 totals。
- account isolation。
- state/generation/checkpoint。
- schema mismatch/error semantics。

该文件使用 adapter 参数化 fixture，确保 pytest 会直接收集同一组 contract cases。SQLite 与 PostgreSQL adapter-specific 文件只提供 factory/故障 fixture，不复制两套业务断言。

### Slice 4：SQLite adapter

改动：

- 标准库 `sqlite3` adapter。
- WAL、busy timeout、schema migration。
- 专用单线程 executor，避免阻塞 event loop。
- Logical schema 和 typed payload。
- DB file permission/encryption guard。

测试：

- Contract suite。
- locked/full/corrupt/schema upgrade。
- event loop heartbeat。

回滚：adapter 未启用时无 DB 文件；启用后可删除重建。

### Slice 5：Projection seam

改动：

- 在 `Session` meta 成功持久化后调用内部 projection hook。
- 覆盖 sync `_save_meta_sync()` 与 async `_save_meta()`。
- `SessionService` delete 后写 tombstone。
- 即使 row 尚不存在，也写独立 tombstone；在线明确 recreate 时事务性清除 tombstone。
- account delete 调用 `purge_account()`。
- 子账号删除不默认 purge session。

失败语义：

- 事实写成功、projection 失败时返回原业务成功。
- 记录 repair pending、error code 和 metric。
- 不在前台请求无限同步重试。

测试：create/message/tool/feedback/commit/delete/account delete 和注入失败。

### Slice 6：Read-only rebuild 与 status CLI

改动：

- `SessionSummarySource` adapter。
- generation/checkpoint/conditional upsert/tombstone/sweep。
- `status/inventory/rebuild --dry-run/--resume/verify`。
- corrupt/quarantine/error report。

关键要求：

- Rebuild 不写 `.meta.json`。
- 未完整扫描不执行 sweep。
- 在线 projection 优先于旧 backfill。

测试：1001+、25/50/99% 中断、并发写/delete/recreate、旧备份场景。

### Slice 7：Meta repair 独立命令

改动：

- `repair-meta --dry-run/--resume`。
- summary version compatibility check。
- 每项写后验证和独立 checkpoint。

该切片不阻塞 query index 上线；只有明确需要升级历史 `.meta.json` 时执行。

### Slice 8：Query mode 与 API metadata

改动：

- `off/shadow/prefer/required` account-scoped 配置/状态。
- `read_mode` 按 `account_id` 持久化；部署 `default_mode` 只用于没有状态 row 的租户。
- list/daily response 增加 `query_meta`。
- required 不可用返回真实 JSON HTTP 503。
- Shadow 采样与 mismatch 记录。

测试：

- 每个 mode/source/state。
- Response business payload 兼容。
- Shadow 不记录 raw message。

### Slice 9：无搜索 list 切 index

改动：

- `SessionService.list_admin_sessions_paginated()` 调用 `AdminSessionQuery.list_sessions()`。
- 保留 q search 当前 raw path。
- 保持 page clamp 和稳定排序。

测试：

- Contract/API tests。
- Ready path VikingFS IO=0。
- 所有 sort/page/filter。

### Slice 10：Daily analytics 切 index

改动：

- `get_admin_daily_analytics()` 调用 module。
- 先从 session projection 动态按请求 tz 聚合。
- 不在本切片引入 daily materialized table。

测试：

- UTC/IANA/DST/naive timestamp。
- 120-day gap behavior。
- Ready path VikingFS IO=0。

### Slice 11：Admin UI 状态

改动：

- Dashboard 将 analytics error 与 users/health error 分离。
- 保留最近成功数据，不把 error 渲染为零。
- Sessions/Dashboard 显示 building/fallback/stale/error。
- Raw search helper 文案。

验证：

- Desktop/mobile。
- Keyboard/focus/alert role。
- loading/empty/error/stale 稳定布局。
- `npm run build --prefix admin`。

### Slice 12：SQLite shadow 演练

交付：

- 本地和 staging migration runbook 演练。
- S/M/L benchmark。
- Shadow result report。
- 故障注入和 rollback 记录。

该切片完成只证明 interface 和迁移方案成立，不代表 8000 租户 production ready。

### Slice 13：PostgreSQL adapter

改动：

- PostgreSQL schema migration。
- Async/线程隔离的 connection pool adapter。
- 参数化 SQL、statement timeout、lock timeout。
- Account-scoped state/rebuild/purge。
- Metrics 和 health。

测试：

- 运行完整共享 contract suite。
- Transaction、concurrency、deadlock/timeout、pool exhaustion。
- 10 万 session benchmark。
- PITR/schema rollback staging 演练。

### Slice 14：PostgreSQL shadow/prefer/required

按迁移 Runbook：

- Internal account。
- 小租户。
- Legacy/archive 租户。
- 最大租户。
- 分批扩展。

每批单独保存 evidence 和 rollback point。

### Slice 15：生产运维护栏

改动：

- Prometheus/structured logs/alerts。
- Projection lag、repair backlog、migration progress dashboard。
- Account delete/purge compensation。
- Backup restore full rebuild workflow。
- On-call runbook 与 owner。

## 6. 建议提交边界

每个 slice 拆成一个或少量可独立 review 的提交。示例：

```text
1. Add admin session IO telemetry
2. Make admin session enumeration explicit about truncation
3. Define admin session query contracts
4. Add shared admin session query contract tests
5. Add SQLite admin session query adapter
6. Project persisted session metadata into admin query storage
7. Add read-only admin session index rebuild commands
8. Add account-scoped query modes and metadata
9. Read admin session pages from query projection
10. Read admin daily analytics from query projection
11. Surface admin query health in the UI
12. Add PostgreSQL admin session query adapter
13. Add production migration and operational metrics
```

不要把 SQLite、PostgreSQL、UI、全文搜索和对象存储混在一个大提交中。

## 7. 测试与执行顺序

文档/纯 module 测试可以并行，但涉及 FastAPI static mount 时：

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

必须先 build frontend，再运行会 mount `admin/dist/assets` 的 backend tests。

PostgreSQL adapter 另加：

```text
temporary PostgreSQL start
-> schema migrate
-> shared contract suite
-> concurrency/failure tests
-> cleanup
```

## 8. Review 检查点

每个 slice review 都回答：

1. 是否保持 account isolation？
2. 是否改变现有 API/指标语义？
3. 事实写与 projection 写的顺序是什么？
4. 失败是否会影响前台主路径？
5. 是否新增 request-time scan/backfill？
6. 是否可以关闭或回滚？
7. 测试是否穿过 module interface，而不是断言内部 SQL？

## 9. Phase Definition of Done

### Local/SQLite 阶段

- Interface 和 contract suite 稳定。
- Read-only rebuild 可恢复。
- Shadow 无业务差异。
- Ready list/daily 文件 IO 为零。
- UI 能显示完整状态。

### PostgreSQL production-query 阶段

- PostgreSQL adapter 通过相同 contract suite。
- 性能和连接池门禁通过。
- 按租户 migration/rollback 演练完成。
- Production canary 达标。
- Required 模式不再全量扫描。

### 全平台可投产阶段

- API 多实例、共享耐久存储、PITR 和恢复演练完成。
- 账号/子账号/资源/session 全链路租户隔离。
- Outbox/worker 可恢复。
- 运维告警、runbook、发布和回滚 owner 明确。

## 10. 暂不实现

- Message FTS/search adapter。
- Daily materialized table。
- Object Storage 迁移。
- AccountRegistry PostgreSQL adapter。
- 多实例任务系统改造。

这些属于生产路线图后续阶段；本计划预留 interface，不在慢查询第一批代码中顺带实现。
