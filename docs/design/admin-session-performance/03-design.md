# Admin Session Query Projection Design / Admin 会话查询投影设计

日期：2026-07-10

状态：Draft

输入分析：[01-analysis.md](./01-analysis.md)

需求：[02-requirements.md](./02-requirements.md)

测试计划：[04-test-plan.md](./04-test-plan.md)

实现计划：[05-implementation-plan.md](./05-implementation-plan.md)

迁移运行手册：[06-migration-runbook.md](./06-migration-runbook.md)

生产化路线图：[production-readiness-roadmap.md](../production-readiness-roadmap.md)

## 1. 文档审阅结论

原分析对主要性能瓶颈的判断成立：无搜索列表与 daily analytics 都会跨用户枚举 session、逐个读取 `.meta.json`，然后在内存中排序或聚合；分页只缩小响应体，不减少服务端扫描。[session_service.py](../../../openviking/service/session_service.py#L630-L912)

结合当前代码，设计需要补充或收紧以下事实：

| 项目 | 审阅结论 | 对设计的影响 |
| --- | --- | --- |
| Daily analytics 语义 | 当前实现把一个 session 的全部计数归入该 session 的最后活动日，而不是按每条 message 的发生日统计。[session_service.py](../../../openviking/service/session_service.py#L784-L912) | v1 必须保持该兼容语义；真正的事件级日统计另立版本，不能在性能优化中悄然改口径。 |
| 写入入口 | `Session.add_message()`、`update_tool_part()` 是同步方法，并通过 `_save_meta_sync()` 落盘；feedback 与 commit 走异步 `_save_meta()`。[session.py](../../../openviking/session/session.py#L446-L471) [session.py](../../../openviking/session/session.py#L850-L973) | 索引投影必须挂在统一的 meta 持久化 seam，不能只在 HTTP 路由或异步 service 方法中更新。 |
| `ls()` 截断 | `VikingFS.ls()` 默认最多返回 1000 个节点，当前 user/session 枚举没有显式处理截断。[viking_fs.py](../../../openviking/storage/viking_fs.py#L1724-L1835) | 请求路径不再依赖目录枚举；backfill 必须使用可证明完整的枚举接口，并在无法完整枚举时失败而不是发布 partial index。 |
| 查询存储依赖 | 项目直接依赖中没有 SQLAlchemy/aiosqlite；`uv.lock` 中的 SQLAlchemy 是传递依赖，不构成稳定 interface。[pyproject.toml](../../../pyproject.toml#L24-L63) | SQLite adapter 使用标准库 `sqlite3`；PostgreSQL adapter 必须显式声明生产 driver，不能依赖传递依赖。 |
| Dashboard 错误语义 | analytics 请求失败会被前端转换为空 totals，用户会看到近似“零数据”，而不是失败状态。[Dashboard.tsx](../../../admin/src/pages/Dashboard.tsx#L715-L749) | API 必须暴露索引状态；Dashboard 必须保留最近一次成功数据并显示明确错误/过期状态。 |
| 更大范围的存储风险 | 原分析关于 OSS、文档 registry、向量库与多节点的判断有价值，但不是本次 Admin session 优化的实现范围。 | 本设计只建立可重建的 Admin 查询派生层，不顺带迁移文档、资源或消息事实源。 |

## 2. 目标

第一阶段目标：

1. 当账号索引状态为 `ready` 时，无搜索会话列表和 daily analytics 的请求路径不调用 VikingFS `ls()`、不读取 session `.meta.json` 或 `messages.jsonl`。
2. 保持现有 Admin API 的过滤、排序、分页、时区、计数和权限语义。
3. 让 session 文件树继续作为事实源；索引可以删除、重建和回滚。
4. 把历史 summary backfill 移出 Admin 请求路径。
5. 索引故障不影响用户会话消息、工具状态、feedback 和 commit 的事实写入。
6. 提供足够的状态与指标，区分 `index`、`filesystem fallback` 和 `raw search` 三种查询来源。
7. 稳定 `AdminSessionQuery` interface，使 SQLite 本地 adapter 与 PostgreSQL 生产 adapter 不改变调用者。
8. 保持酒店=`account_id`、租户子账号=`user_id` 和现有 `ROOT/ADMIN/USER` 权限语义。

## 3. 非目标

v1 不包含：

- 不把 `messages.jsonl`、archive 或文档资源迁入关系型数据库。
- 不实现消息全文索引；带 `q` 的查询继续走当前 raw-message 搜索路径。
- 不实现 `admin_daily_metrics` 物化表。现有任意 IANA 时区与 session-last-activity 口径先从 session index 动态聚合。
- 不改变 session detail 的消息加载方式。
- 不支持多个 OpenViking 进程或多台机器共同写同一个本地索引。
- 不解决文档 registry、对象存储、VectorDB 或资源事务问题。

## 4. 核心设计决策

术语约定：本文统一把可重建的派生数据称为 **query projection（查询投影）**，把隐藏查询、投影、状态和重建行为的深模块称为 `AdminSessionQuery`。`index` 仅作为具体查询存储、状态、表/CLI 和数据库索引的运维短称，不再作为 module/interface 名称。

### 4.1 事实源与派生层

- AGFS/VikingFS 下的 session 目录、`.meta.json`、`feedback.json`、live/archive messages 是事实源。
- `AdminSessionQuery` adapter 保存 Admin 查询投影，只保存列表和统计需要的元数据，不保存 message/tool output 正文。
- 索引结果不得反向写回 session 事实源。
- 索引 schema 不兼容、文件损坏或校验失败时，可以删除后从事实源重建。

### 4.2 一个深模块

新增 `AdminSessionQuery` 深模块。调用者只需要知道以下 interface：

```python
class AdminSessionQuery:
    async def project(self, change: SessionProjectionChange) -> ProjectionResult: ...
    async def tombstone(self, identity: SessionIdentity) -> ProjectionResult: ...
    async def purge_account(self, account_id: str) -> ProjectionResult: ...
    async def list_sessions(self, query: AdminSessionListQuery) -> AdminSessionPage: ...
    async def daily_analytics(self, query: AdminDailyQuery) -> AdminDailyAnalytics: ...
    async def status(self, account_id: str) -> AdminSessionIndexStatus: ...
    async def rebuild(self, account_id: str, options: RebuildOptions) -> RebuildResult: ...
```

模块内部隐藏：

- SQLite connection 或 PostgreSQL pool、事务和 schema migration。
- SQL 的 filter/order/limit/count 生成。
- 当前 `_sort_admin_sessions()` 的稳定 tie-breaker 语义。
- payload JSON 的序列化与版本校验。
- backfill generation、checkpoint、conditional upsert 和 tombstone 竞争处理。
- 索引状态与错误记录。

不增加泛化到所有业务的 `Repository`/ORM interface。这里已有两个真实 adapter：

- `SQLiteAdminSessionQuery`：本地开发、测试和单节点 shadow 验证。
- `PostgresAdminSessionQuery`：约 8000 酒店租户的生产目标。

两个 adapter 运行同一份 interface contract tests；SQLite 验证迁移机制，不承担最终 HA 生产职责。

### 4.3 seam 放置

meta 成功持久化是投影更新的唯一写入 seam：

```text
Session mutation
  -> write messages / feedback as needed
  -> Session._save_meta()                 fact committed
       -> SessionMetaProjection.project() derived best effort

Admin delete
  -> VikingFS.rm(session_uri)             fact deleted
  -> SessionMetaProjection.tombstone()    derived best effort
```

`SessionService.session()` 创建 `Session` 时注入一个内部 `SessionMetaProjection`。实现调用当前配置的 `AdminSessionQuery.project()`；query adapter disabled 时不注入。测试可以注入记录型 projection，但该内部 seam 不进入 Admin API interface。

当前 `_save_meta_sync()` 本身通过 `run_async(_save_meta())` 完成写入，因此 sync/async mutation 都能收敛到 `_save_meta()`。投影调用必须位于 `.meta.json` 成功写入之后，并在 seam 内捕获、记录所有派生层异常，不能让异常从 `_save_meta()` 反向改变已经成功的事实写语义。本地阶段可以在有界 timeout 内直接调用 adapter；最终多实例生产架构通过 durable outbox/worker 驱动同一个 `project()` interface。

这使以下路径自动覆盖：

- `ensure_exists()` 创建空 session。
- `add_message()` 更新 message/token/tool summary。
- `update_tool_part()` 更新失败工具数。
- `set_message_feedback()`、`clear_message_feedback()` 及 feedback memory 状态更新。
- `commit_async()` 清空 live messages 但保留累计 audit summary。
- legacy summary 离线 backfill 后直接投影；只有独立 `repair-meta` 操作才写回 meta。

### 4.4 写入失败语义

事实源先写，索引后写。索引写入失败时：

1. 不回滚已经成功的 session 写入。
2. `ProjectionResult` 记录失败，索引状态转为 `stale` 或 `error`。
3. 记录结构化日志和 metric，保留需要 repair 的 session identity。
4. `prefer` 模式的 Admin 读取回退到 filesystem；`required` 模式返回明确的 503，不把旧索引伪装成最新数据。
5. reconciliation 重新从 `.meta.json` 投影该 session。

## 5. 运行结构

```text
                         +-----------------------------+
Session writes -------->| AGFS session files (truth)  |
                         +--------------+--------------+
                                        |
                         meta persisted | rebuild source
                                        v
                         +-----------------------------+
                         | AdminSessionQuery           |
                         | SQLite(local) / PostgreSQL  |
                         +---------+----------+--------+
                                   |          |
                     list_sessions |          | daily_analytics
                                   v          v
                         +-----------+   +-----------+
                         | Sessions  |   | Dashboard |
                         +-----------+   +-----------+

q search / detail -----------------------------------> AGFS raw messages
```

## 6. 查询存储设计

本节定义两个 adapter 共用的逻辑 schema。SQLite 负责本地/测试验证；PostgreSQL 是生产 adapter。调用者不得依赖任一数据库的内部 SQL 或连接对象。

以下 DDL 是 SQLite reference schema，不可原样复制为 PostgreSQL migration。PostgreSQL 必须把所有微秒时间、计数、token 和 generation 字段映射为 `BIGINT`，把布尔语义映射为 `BOOLEAN`，并按 driver 决策把 `payload_json` 保存为经 schema 校验的 `JSONB` 或等价类型，避免 32-bit `INTEGER` 溢出。

### 6.1 SQLite 路径

默认路径：

```text
{storage.workspace}/_system/admin_sessions/index.sqlite3
```

该目录属于可重建派生数据。备份策略可以包含它以加速恢复，但恢复正确性不能依赖它。

### 6.2 `admin_session_index`

```sql
CREATE TABLE admin_session_index (
  account_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  uri TEXT NOT NULL,

  created_at_us INTEGER,
  updated_at_us INTEGER,
  first_message_at_us INTEGER,
  last_message_at_us INTEGER,
  activity_at_us INTEGER NOT NULL,

  message_count INTEGER NOT NULL,
  user_message_count INTEGER NOT NULL,
  assistant_message_count INTEGER NOT NULL,
  tool_call_count INTEGER NOT NULL,
  failed_tool_call_count INTEGER NOT NULL,
  context_ref_count INTEGER NOT NULL,

  feedback_count INTEGER NOT NULL,
  positive_feedback_count INTEGER NOT NULL,
  negative_feedback_count INTEGER NOT NULL,
  latest_feedback_at_us INTEGER,
  feedback_memory_pending_count INTEGER NOT NULL,
  feedback_memory_completed_count INTEGER NOT NULL,
  feedback_memory_failed_count INTEGER NOT NULL,
  feedback_memory_skipped_count INTEGER NOT NULL,
  feedback_memory_extracted_count INTEGER NOT NULL,

  prompt_tokens INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  total_tokens INTEGER NOT NULL,

  audit_summary_version INTEGER NOT NULL,
  audit_summary_complete INTEGER NOT NULL,
  source_meta_updated_at_us INTEGER NOT NULL,
  source_fingerprint TEXT NOT NULL,
  payload_schema_version INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  projection_origin TEXT NOT NULL,
  projected_at_us INTEGER NOT NULL,
  deleted_at_us INTEGER,
  seen_generation INTEGER NOT NULL DEFAULT 0,

  PRIMARY KEY (account_id, user_id, session_id)
);
```

`payload_json` 保存 `_admin_session_summary_result()` 的兼容 payload，使已有未参与 SQL 排序的字段仍可返回；可查询字段必须同时有类型明确的列，不能在请求路径用 JSON 表达式排序。

`projection_origin` 只允许 `online`、`rebuild`、`reconcile`，用于冲突诊断和同时间优先级；它不是调用者可筛选的业务字段。

推荐索引：

```sql
CREATE INDEX idx_admin_session_activity
  ON admin_session_index(account_id, activity_at_us DESC, user_id, session_id)
  WHERE deleted_at_us IS NULL;

CREATE INDEX idx_admin_session_user_activity
  ON admin_session_index(account_id, user_id, activity_at_us DESC, session_id)
  WHERE deleted_at_us IS NULL;

CREATE INDEX idx_admin_session_created
  ON admin_session_index(account_id, created_at_us, user_id, session_id)
  WHERE deleted_at_us IS NULL;

CREATE INDEX idx_admin_session_updated
  ON admin_session_index(account_id, updated_at_us, user_id, session_id)
  WHERE deleted_at_us IS NULL;
```

其他数值排序索引在 benchmark 证明有需要后再增加，避免每次 session 更新维护过多二级索引。排序规则必须精确复刻当前实现：先按白名单 primary field 和请求方向排序；primary field 不是 `last_active` 时再追加 `activity_at_us DESC`；最后追加 `user_id ASC, session_id ASC`。空值和非法时间映射到当前 Python 实现使用的零值，不能依赖 SQLite/PostgreSQL 不同的默认 NULL 顺序。无 `q` 查询中的 `matched_message_count` 固定为 `0`，不读取 raw message。

### 6.3 Tombstone 表

删除可能发生在 row 首次投影之前，因此只在 `admin_session_index.deleted_at_us` 上更新已有 row 不足以阻止迟到 rebuild 把会话复活。两个 adapter 都需要保存独立 tombstone：

```sql
CREATE TABLE admin_session_tombstone (
  account_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  deleted_at_us INTEGER NOT NULL,
  delete_generation INTEGER NOT NULL,
  PRIMARY KEY (account_id, user_id, session_id)
);
```

`tombstone()` 在同一事务中 upsert tombstone，并把已有 row 标为 deleted。在线事实源明确重新创建同一 identity 后，`project()` 才能在同一事务清除 tombstone 并 upsert 新 row；历史 rebuild 不得自行清除当前 generation 内的 tombstone。恢复旧事实源备份仍走隔离 projection + full rebuild，不能把 tombstone 时间戳当成跨恢复点的全局顺序。

### 6.4 状态表

```sql
CREATE TABLE admin_session_index_state (
  account_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL,
  state TEXT NOT NULL,
  read_mode TEXT NOT NULL DEFAULT 'off',
  active_generation INTEGER NOT NULL,
  rebuild_checkpoint TEXT,
  rebuild_started_at_us INTEGER,
  rebuild_completed_at_us INTEGER,
  last_projection_at_us INTEGER,
  last_error_at_us INTEGER,
  last_error_code TEXT,
  indexed_session_count INTEGER NOT NULL DEFAULT 0,
  source_session_count INTEGER,
  repair_pending_count INTEGER NOT NULL DEFAULT 0
);
```

合法状态：`disabled`、`building`、`ready`、`stale`、`error`。

`read_mode` 合法值为 `off`、`shadow`、`prefer`、`required`，并按 `account_id` 独立持久化。状态（是否完整/新鲜）和模式（如何读）是两个不同维度，不能通过把 `state` 改成 `ready-required` 一类组合值来耦合。

### 6.5 SQLite 运行参数

- `journal_mode=WAL`
- `synchronous=NORMAL`
- `foreign_keys=ON`
- 有界 `busy_timeout`
- 所有 SQL 在专用单线程 executor 中执行，避免阻塞 asyncio event loop，并序列化 connection 使用。
- page 的 `COUNT` 与 items 查询使用同一只读事务，避免 `total` 与当前页来自不同快照。
- 初始化后将 DB 文件权限收紧为 owner read/write。

### 6.6 PostgreSQL 运行要求

- 使用显式直接依赖的 PostgreSQL driver 和有界 connection pool。
- 配置 statement timeout、lock timeout 和 pool acquire timeout。
- Schema migration 与 runtime 账号分权。
- page 的 count/items 使用一致事务快照。
- 所有主键、唯一键和高频索引包含 `account_id`。
- 提供 pool saturation、query duration、lock wait 和 error metrics。
- 支持多 API/worker 实例，projection/rebuild 保持幂等。
- Query、projection 和 rebuild 使用独立或有配额的并发预算；单个大租户 rebuild 必须可限速/暂停，不能耗尽整个 pool。

## 7. 查询语义

### 7.1 无搜索会话列表

`q == ""` 且账号 index 为 `ready` 时：

1. 使用 `account_id` 强制租户过滤。
2. 可选 `user_id`、`activity_at` 日期范围过滤。
3. 使用白名单映射 `sort_by` 到实际列，不能拼接任意 SQL identifier。
4. 执行 `COUNT(*)` 和 `ORDER BY ... LIMIT ... OFFSET ...`。
5. 保持现有 page clamp、`has_prev`、`has_next`、`total_pages` 语义。
6. 返回 payload 时重新覆盖 identity、排序字段和 token/feedback count，防止旧 payload 与索引列漂移。

现有 page-number API 保留，因此很深的页仍会受到 SQL `OFFSET` 成本影响。若 benchmark 证明 page N 成为瓶颈，再设计 cursor pagination；v1 不在同一变更中改 Admin URL 契约。

### 7.2 Daily analytics

v1 保持当前口径：

```text
activity_at = last_message_at || updated_at || created_at
day = activity_at 转换到请求 tz 后的日历日
该 session 的全部 summary count 计入 day
```

流程：

1. 根据请求 IANA timezone 把本地日期范围转换为 UTC 边界，SQL 只读取账号、可选用户和该时间范围内的候选 row。
2. Adapter 可以在数据库内或 Python 中按现有 IANA timezone 规则生成 day key，但两个 adapter 必须通过同一结果 contract。
3. 保持 120 天以内填充空 bucket、超过 120 天只返回有数据日期的行为。
4. `active_users` 按每天/区间去重。

不在 v1 建 `admin_daily_metrics`，原因是它会引入任意时区物化、session 活动日移动时的减旧加新、feedback 修改和 delete 的增量一致性问题，而 session projection 已经可以移除最昂贵的文件扫描。但动态聚合仍与日期范围内的候选 row 数量相关：benchmark 若无法满足 daily 的绝对延迟或 scanned-row budget，必须在该规模生产切读前提前实施 daily materialization，不能用“已经不读文件”代替性能验收。

### 7.3 搜索与详情

- `q != ""`：继续调用当前 `_collect_admin_sessions()`，读取 raw messages；响应标记 `source=raw_search`。
- session detail：继续从 live/archive messages 组装，确保审计内容来自事实源。
- 后续 FTS 必须单独设计 message-level projection、删除/归档语义和权限过滤，不复用 session summary 表冒充全文索引。

## 8. Backfill、reconciliation 与竞争处理

### 8.1 完整枚举

新增内部 `SessionSummarySource` adapter，负责：

- 完整列出 account 下所有 user 和 session。
- 读取当前 `.meta.json`；legacy summary 必要时读取 raw messages，默认只把结果写入 query projection。
- 产出规范化的 `SessionIdentity + SessionMeta`。

现有 `VikingFS.ls(node_limit=1000)` 不能作为完整 backfill interface。source 必须使用新建的无静默上限枚举方法；若底层 backend 不能保证完整性，则 rebuild 失败并把账号保持为 `error/building`，不能设置 `ready`。

### 8.2 generation rebuild

1. 为账号生成新的 `active_generation`，state 设为 `building`。
2. 按稳定 `(user_id, session_id)` 顺序扫描并批量事务 upsert，保存 checkpoint。
3. 在线 `project()` 同时写入当前 generation。
4. upsert 仅在 incoming `source_meta_updated_at_us` 不旧于现有 row 时覆盖，避免 backfill 覆盖在线新写入。
5. delete 写独立 tombstone；历史 backfill 不能清除当前 generation 的 tombstone，只有事实源成功在线 recreate 后的 `project()` 可以事务性清除。
6. 完整扫描结束后 mark-and-sweep 未在本 generation 见到的旧 row。
7. 比较 source/index count，抽样校验 payload fingerprint；成功后原子更新 state 为 `ready`。

第 4 条的相等时间必须有确定语义：fingerprint 相同视为幂等；fingerprint 不同视为冲突，保留现有 row、记录 repair/error item，不通过任意 hash 大小决定“新旧”。在线 projection 和 rebuild 必须携带 origin/generation，使同时间下在线事实写优先于历史扫描。

任务必须幂等、可中断续跑，并提供 `--dry-run`、`--account`、`--resume` 和 `--verify` 运维入口。

`.meta.json` 升级由独立 `repair-meta` 操作执行，拥有自己的 dry-run、checkpoint、兼容检查和备份要求。Index rebuild 不得隐式修改事实源。

### 8.3 request-time legacy 数据

- index `ready` 后，Admin list/daily 请求不再触发 `_backfill_admin_session_meta_summary()`。
- legacy session 由 rebuild/reconciliation 修复。
- `prefer` 模式在账号尚未 ready 时可以临时走原 filesystem 行为，但 response 必须标记 fallback，且不能把截断结果标记为完整。

### 8.4 账号删除与备份恢复

- 删除单个 session 后写 tombstone。
- 删除整个 account 时调用 `purge_account()`，清理 row、tombstone、state、checkpoint 和 repair item。
- 删除子账号保持现状：撤销身份，不默认删除历史 session。
- 恢复旧事实源备份后隔离现有 projection，使用新 generation full rebuild；不能依赖时间戳把旧备份与未来 index 增量合并。

## 9. 配置与生命周期

在 OpenViking 主配置增加：

```json
{
  "admin_sessions": {
    "query_adapter": "filesystem",
    "default_mode": "off",
    "index_path": null,
    "postgres_dsn_env": null,
    "reconcile_on_start": false,
    "allow_plaintext_derived_metadata": false
  }
}
```

`query_adapter` 合法值为 `filesystem`、`sqlite`、`postgres`；`filesystem` 表示不初始化查询投影 adapter，只允许 `off`。SQLite/PostgreSQL 的连接与 schema 细节仍隐藏在 module 内。

部署配置中的 `default_mode` 只决定没有 account state row 时的默认值；生产切换由状态表中的 account-scoped `read_mode` 控制，CLI/API 每次只能修改明确的 `account_id`。全局配置不得把全部 8000 个租户一次性从 `off` 切到 `prefer/required`。

`read_mode`：

- `off`：保持现有文件扫描行为，默认值。
- `shadow`：响应仍来自 filesystem；后台执行 index query 并比较 count/order/payload，记录差异。
- `prefer`：账号 ready 时使用 index，否则显式 fallback。
- `required`：无搜索 list/daily 只接受 ready index；不可用时返回 503。

生命周期接入 [core.py](../../../openviking/service/core.py#L229-L384)：

1. 根据 adapter 初始化 SQLite 或 PostgreSQL schema/pool；SQLite 初始化发生在 workspace PID lock 后。
2. 将 projection 和 query module 注入 `SessionService`。
3. 可选启动低优先级 reconcile task，不阻塞服务 ready。
4. shutdown 时先停止 reconcile，再关闭 adapter executor/pool，最后关闭其他存储。

SQLite adapter 只允许单 OpenViking 进程写。检测到不兼容的多 worker/共享 workspace 部署时，SQLite `prefer/required` 应 fail-fast；多实例生产使用 PostgreSQL adapter。

## 10. API 契约

现有 endpoint 和请求参数不变：

- `GET /api/v1/admin/accounts/{account_id}/sessions`
- `GET /api/v1/admin/accounts/{account_id}/analytics/daily`
- detail/delete endpoint 不变。

list 与 analytics response 增加兼容字段：

```json
{
  "query_meta": {
    "source": "index",
    "index_state": "ready",
    "index_schema_version": 1,
    "last_projection_at": "2026-07-10T08:00:00Z",
    "repair_pending_count": 0,
    "fallback_reason": null,
    "partial": false
  }
}
```

允许的 `source`：`index`、`filesystem`、`raw_search`。

`required` 模式下 index 不可用：

```text
HTTP 503
error.code = ADMIN_SESSION_INDEX_UNAVAILABLE
error.message = Admin session index is not ready; rebuild or repair the index.
```

路由必须使用真实的 HTTP error response（例如 `JSONResponse(status_code=503, ...)`）；只构造 `Response(status="error")` 不会改变 HTTP status code。

权限仍由现有 `_check_account_access()` 和 ROOT/ADMIN role 保证；index 查询必须再次带 `account_id` predicate，不能只依赖路由层检查。

## 11. Admin UI 设计

Admin 是高密度运维工具，沿用现有 tokens 和组件，不新增装饰性卡片、渐变或大幅动画。

### 11.1 状态展示

- `ready + source=index`：不额外显示成功 banner，保持界面安静。
- `building`：在筛选区下方显示紧凑 info/warning 条，文本“会话索引正在构建，当前查询可能较慢。”
- `stale`：显示“会话索引更新延迟，数据截至 {time}。”，同时保留已有结果。
- `filesystem` fallback：显示“当前使用文件扫描查询，数据量较大时加载会变慢。”
- `error/503`：显示发生了什么和下一步动作，如“会话索引不可用。请重建索引或切换回兼容模式。”；按钮文案使用“重新加载统计”或“重新加载会话”。
- 状态不能只靠颜色，必须包含文本和 `AlertTriangle`/`AlertCircle` 图标。

### 11.2 Dashboard

- 把 analytics error 与 users/health error 分离；不能再把 analytics failure 转成零 totals。
- refresh 时保留最近一次成功的 KPI/图表并使用现有 `loading-fade`，不让布局跳变。
- 没有成功数据时显示明确 empty/error state；真正的零数据仍显示正常的零会话口径。

### 11.3 Sessions

- 保持当前表格、排序、分页和 selected row 结构。
- 索引状态条放在 error box 与 table 之间，宽度随表格，不嵌套 card。
- raw search 期间在现有“搜索原始会话”附近显示 helper：“全文搜索会读取会话内容，结果可能较慢。”
- loading、empty、error、selected、disabled、keyboard focus 继续保持现有状态，新增状态不改变固定表头和分页区尺寸。
- 桌面与移动宽度都要验证状态文本换行不会遮挡筛选控件。

## 12. 可观测性

每个 Admin query 记录：

- endpoint、source、index_state、elapsed_ms。
- returned_count、total_count、page/page_size。
- Query adapter 的 query/lock/pool wait 时间。
- filesystem fallback 时的 listed users/sessions、meta reads、raw message reads、archive reads、estimated bytes。
- projection success/error、repair pending、projection lag。
- rebuild scanned/projected/skipped/error、checkpoint、duration。

Prometheus 建议指标：

```text
openviking_admin_session_query_duration_seconds{endpoint,source}
openviking_admin_session_query_total{endpoint,source,status}
openviking_admin_session_index_projection_total{status}
openviking_admin_session_index_repair_pending
openviking_admin_session_index_rebuild_progress{account_id}
openviking_admin_session_index_last_success_unixtime{account_id}
```

`account_id` 若基数不可控，不进入普通 query counter label；只在受控 rebuild/status gauge 或结构化日志中出现。

## 13. 安全与隐私

- index 不保存 message text、tool input/output、feedback comment、memory content 或密钥。
- 它仍保存 account/user/session identifier、时间和使用量，属于敏感运维元数据。
- 当前 VikingFS application encryption 不会自动加密直接写入的 SQLite 文件。
- 当 `encryption.enabled=true` 时，v1 默认拒绝启用明文 index；只有索引路径位于受控加密卷且显式设置 `allow_plaintext_derived_metadata=true` 时才允许启动。
- 后续若需要 application-level encrypted SQLite，应单独选择 SQLCipher 或列级加密设计，不能只在文档里声称“workspace 已加密”。
- 所有 SQL 使用参数绑定；`sort_by` 使用固定枚举映射。
- PostgreSQL 使用 TLS、静态加密、最小权限 runtime role 和独立 migration role。

## 14. 发布步骤

1. **Observe**：加入请求 I/O/延迟观测与 benchmark，不改变读路径。
2. **Local Build**：实现 module、projection、read-only rebuild、状态 API 和 SQLite adapter，所有账号 `read_mode=off`。
3. **Local/Stage Shadow**：使用 SQLite 验证 contract、迁移和 shadow 差异。
4. **Production Adapter**：实现 PostgreSQL adapter，运行相同 contract/failure/benchmark suite。
5. **Production Shadow**：按租户比较 filesystem/PostgreSQL 的 total、排序与 payload fingerprint。
6. **Prefer**：差异为零且 backfill 完成后按租户切 `prefer`。
7. **Required**：稳定一个发布周期后切 `required`，确保默认查询不再退化为全量扫描。
8. **Search follow-up**：另行评审 FTS/message index，不与本阶段混合上线。

回滚查询只需要把目标账号的 `read_mode` 切回 `off` 或 `shadow`。Read-only rebuild 不修改 session 事实源；如果独立执行过 `repair-meta`，按其兼容和备份策略处理，而不是把它隐含在 query rollback 中。

## 15. 预计代码落点

- `openviking/service/admin_session_query/`：深模块 interface、adapter、schema、query、rebuild state。
- `openviking/service/session_service.py`：从 scan 切换到 index interface；保留 raw search/detail fallback。
- `openviking/session/session.py`：注入内部 meta-persisted projection hook。
- `openviking/service/core.py`：index 生命周期和依赖注入。
- `openviking_cli/utils/config/`：`AdminSessionsConfig`。
- `openviking/server/routers/admin.py`：`query_meta` 与 503 error mapping。
- `openviking/storage/observers/prometheus_observer.py`：Admin index/query metrics。
- `admin/src/pages/Dashboard.tsx`：独立 analytics error/stale state。
- `admin/src/pages/Sessions.tsx`、`admin/src/pages/Pages.css`：query source/index state 展示。
- `scripts/admin_session_index.py`：status/inventory/dry-run/rebuild/resume/verify/repair-meta 运维入口。

## 16. 验收标准

必须同时满足：

1. ready index 下，无搜索 list/daily 请求对 session VikingFS 的 `ls/read_file` 调用数均为 0。
2. 现有 Admin session API 测试在 `off`、SQLite ready 和 PostgreSQL ready 下返回相同业务结果。
3. 1000 以上 user/session 的 backfill 不静默截断；无法完整枚举时 index 不进入 `ready`。
4. create/add/tool status/feedback/commit/delete 后，index 在同一操作完成时或明确的 repair 状态下可观测。
5. projection 故障不使事实写入失败；`required` 读取不返回伪新数据。
6. Dashboard 能区分真实零数据、loading、stale 和 error。
7. index 可从空文件开始幂等重建，重启/中断后可续跑。
8. account filter 在模块层强制执行，跨账号同名 user/session 不串数据。
9. `q` search 和 detail 的现有 raw-message 审计行为不变。
10. 通过配套测试计划中的功能、故障、I/O budget、性能和手动 UI 验证。
11. PostgreSQL adapter 通过共享 contract suite 后，才允许面向生产租户切 `prefer`。

## 17. 明确拒绝的方案

- **只增加 `.meta.json` cache**：已经存在，仍需逐 session 读文件，不能改变复杂度。
- **只把 `page_size` 调小**：切片发生在全量收集和排序之后，不减少扫描。
- **在请求内并发读取所有 meta**：可能降低单次墙钟时间，但增加 IO 峰值且仍是 O(session_count)。
- **请求时自动全量 backfill**：把迁移成本转成用户延迟并造成并发重复工作。
- **v1 直接物化任意时区 daily 表**：在当前 session-level 统计口径下增加一致性复杂度，收益小于先建立 session index。
- **用向量库代替元数据索引**：向量库不是 Admin filter/order/count 的权威关系查询层。
- **依赖传递性 SQLAlchemy**：依赖边界不稳定，而且 ORM 不会消除当前主要复杂度。
