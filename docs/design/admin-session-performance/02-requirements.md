# Admin Session Performance Requirements / Admin 会话性能优化需求

日期：2026-07-10

状态：Draft

相关文档：

- [现状分析](./01-analysis.md)
- [技术设计](./03-design.md)
- [测试计划](./04-test-plan.md)
- [实现计划](./05-implementation-plan.md)
- [迁移运行手册](./06-migration-runbook.md)
- [生产化路线图](../production-readiness-roadmap.md)

## 1. 背景

当前代码仍处于 MVP 阶段。Admin Dashboard 与会话列表把租户 session 文件树当作查询数据库使用：请求会枚举用户和 session、逐个读取 `.meta.json`，然后在内存中聚合、排序和分页。随着租户与会话增长，请求延迟和文件 IO 近似线性增长。

本需求以慢查询为第一条生产化纵向切片，目标不只是缩短一个 endpoint 的响应时间，而是建立后续可复用的查询投影、历史迁移、租户隔离、观测、灰度切换和回滚机制。

本文中的 **query projection（查询投影）** 指可从 session 事实源删除后重建的派生数据；`AdminSessionQuery` 指隐藏查询、投影、状态和重建行为的深模块。`index` 仅作为具体查询存储、状态、表/CLI 和数据库索引的运维短称，不作为 module/interface 名称。

## 2. 已确认的业务决策

以下决策为本阶段固定输入，不在实现中重新建模：

1. 一个酒店对应一个租户账号 `account_id`。
2. 酒店前台、技术人员等操作人是租户下的子账号 `user_id`。
3. 继续使用现有 `ROOT / ADMIN / USER` 角色与权限语义。
4. `account_id` 是知识库素材、资源、会话、记忆、反馈、统计和派生索引的最高隔离维度。
5. `user_id` 负责租户内身份和私有数据归属，不作为跨租户隔离手段。
6. `agent_id` 表示 Agent 配置和空间，不表示酒店或租户。
7. 目标规模约为 8000 个酒店租户；具体每租户子账号数、日会话量、消息量与留存期需要通过生产观测持续校准。

## 3. 操作角色

### 3.1 酒店前台

- 通常使用 `USER` 子账号。
- 使用本租户共享知识库和素材。
- 创建和查看自己的会话。
- 不能查看其他租户数据。
- 不承担索引、迁移或系统运维操作。

### 3.2 酒店技术人员

- 普通使用场景可为 `USER`，需要管理本酒店时使用 `ADMIN`。
- `ADMIN` 可以管理本租户子账号、知识素材和全部会话审计。
- 不能跨 `account_id` 访问其他酒店。

### 3.3 平台运维

- 使用 `ROOT`。
- 负责租户创建/删除、迁移、索引修复、监控和故障处置。
- 跨租户访问必须进入审计日志，不能成为普通酒店操作路径。

## 4. 范围

### 4.1 本阶段范围

- Admin 无搜索会话列表。
- Admin daily analytics。
- Session summary 的增量投影。
- 历史 session summary 的只读回填和校验。
- `off / shadow / prefer / required` 读路径切换。
- 查询来源、索引状态、延迟和文件 IO 观测。
- Dashboard/Sessions 的 loading、fallback、stale 和 error 状态。
- SQLite 本地 adapter 和 PostgreSQL 生产 adapter 的统一 interface。

### 4.2 非本阶段范围

- 不改变现有角色和权限模型。
- 不新增 Organization、Property 或跨租户角色绑定。
- 不迁移原始 message/archive 到关系型数据库。
- 不实现消息全文索引；`q` 搜索继续读取原始消息。
- 不改变 session detail 的事实源读取方式。
- 不在本阶段迁移知识库 registry、对象存储或 VectorDB。
- 不在本阶段实现任意维度的通用数据仓库。

## 5. 功能需求

### FR-1 租户隔离

1. 每个索引 row 必须包含 `account_id`。
2. session 唯一键必须至少为 `(account_id, user_id, session_id)`。
3. Admin 查询模块必须在内部强制应用 `account_id` predicate，不能只依赖路由或 UI 筛选。
4. 相同 `user_id/session_id` 可以同时存在于不同租户且互不覆盖。
5. cache key、迁移 checkpoint、outbox event 和 audit log 必须包含 `account_id`。

### FR-2 现有权限兼容

1. `ROOT` 保持现有跨租户管理能力。
2. `ADMIN` 只能查看和管理本租户会话。
3. `USER` 不能访问 Admin session/analytics endpoint。
4. 子账号创建、删除、角色修改的 endpoint 和角色语义保持不变。

### FR-3 无搜索会话列表

1. 保持现有 `user_id/from_date/to_date/tz/sort_by/sort_order/page/page_size` 参数。
2. 保持现有排序字段和稳定 tie-breaker。
3. 保持 page clamp、total、total_pages、has_prev、has_next 语义。
4. 索引 ready 后，请求路径不得枚举 session 目录或读取 session `.meta.json/messages.jsonl`。
5. PostgreSQL 生产 adapter 必须使用 `WHERE/ORDER BY/LIMIT/OFFSET` 或兼容查询执行分页，不允许取回租户全部 row 后在应用内分页。

### FR-4 Daily analytics

1. v1 保持当前兼容口径：一个 session 的全部 summary 归入其最后活动日。
2. `activity_at = last_message_at || updated_at || created_at`。
3. 请求 `tz` 决定日期过滤和日历日 bucket。
4. 120 天以内保持空日期补齐，超过 120 天保持当前稀疏返回行为。
5. 索引 ready 后不得读取 session 文件。
6. 真正逐 message 的日统计必须另行设计和版本化，不能在本优化中改变现有指标。

### FR-5 Session projection

1. create、add message、tool status、feedback、commit 和 delete 必须覆盖投影更新。
2. 投影只能在事实源写入成功后发生。
3. 投影失败不能使前台消息、feedback 或 commit 事实写入失败。
4. 投影失败必须产生稳定 error code、repair pending 记录和 metric。
5. 在线投影必须优先于迟到的历史 backfill 数据。

### FR-6 历史数据回填

1. Index rebuild 默认只读 session 事实源，不修改 `.meta.json`。
2. Legacy session 可从 raw live/archive messages 计算 summary 后直接投影到查询存储。
3. `.meta.json` repair/write-back 是独立显式操作，必须单独 dry-run、执行和验证。
4. Backfill 必须幂等、可断点续跑、按租户执行。
5. 无法证明枚举完整时，租户索引不能进入 `ready`。
6. 损坏或无法解析的 session 必须进入错误报告，不能静默跳过。

### FR-7 索引状态

每个 `account_id` 独立维护：

- `disabled`
- `building`
- `ready`
- `stale`
- `error`

状态 response 至少包含：schema version、last projection time、repair pending、source/index count、last error 和 rebuild checkpoint。

### FR-8 读路径模式

- `off`：现有 filesystem 路径。
- `shadow`：filesystem 返回结果，按受控采样执行索引查询并比对。
- `prefer`：租户 ready 时使用索引，否则显式 fallback。
- `required`：无搜索 list/daily 只接受 ready 索引；不可用时返回真实 HTTP 503。

切换必须按租户执行，不允许一次性切换全部 8000 个租户。

### FR-9 删除语义

1. 删除单个 session 后必须写 tombstone 或删除对应派生 row。
2. 即使 session 尚无 projection row，删除也必须能创建独立 tombstone，阻止迟到 rebuild 复活它。
3. 删除整个 account 时必须清理所有该租户的 index、tombstone、checkpoint、repair item 和 search projection。
4. 删除子账号当前保持现有语义：撤销身份凭据，不默认删除历史 session。
5. 如果未来需要用户数据擦除，必须使用独立、可审计的 purge workflow。

### FR-10 搜索与详情

1. `q != ""` 继续使用 raw-message search，并标记 `query_meta.source=raw_search`。
2. session detail 继续从 live/archive 事实源读取。
3. 索引不可用不能影响用户查看允许访问的事实详情，除非 endpoint 本身依赖该索引。

## 6. 非功能需求

### NFR-1 性能

硬门禁：

1. ready list/daily 的 session VikingFS `ls/read_file` 次数为 0。
2. 100,000 session reference fixture 下，list page 1 p95 初始目标不高于 300 ms。
3. 同一 fixture 下，14 天 daily analytics p95 初始目标不高于 500 ms。
4. 从 10,000 增长到 100,000 session 时，列表 page 1 p95 不得近似按 10 倍线性增长；daily 动态聚合以绝对 p95、日期范围和 scanned-row budget 验收，达不到门禁时必须在生产切读前实施日统计物化。
5. Projection 对事实写路径增加的 p95 延迟初始目标不高于 20 ms；最终值由 Phase 0 baseline 校准。
6. PostgreSQL fleet benchmark 必须覆盖 8000 个 account state、冷热租户倾斜和 rebuild/query/projection 混合负载；单个大租户迁移不能耗尽全局 pool 或阻塞其他租户查询。

### NFR-2 可用性

1. Admin 查询派生层故障不影响前台会话事实写入。
2. PostgreSQL production adapter 必须支持多 API 实例。
3. SQLite 仅用于本地、测试和受控迁移验证，不作为 8000 租户最终 HA 存储。
4. `required` 模式不得在故障时静默返回陈旧数据。

### NFR-3 一致性

1. Session 文件仍是本阶段事实源。
2. Query index 是可删除重建的派生层。
3. Rebuild 使用 generation、checkpoint、conditional upsert 和 tombstone 处理并发写/删。
4. 恢复更旧的事实源备份后必须丢弃或隔离现有 index 并 full rebuild，不能按普通增量合并。

### NFR-4 安全

1. Index 不保存 message/tool output 正文、密钥或 feedback 原因正文。
2. 所有 SQL 使用参数绑定，排序字段使用白名单映射。
3. SQLite 明文派生元数据在 application encryption 开启时默认禁用。
4. PostgreSQL 使用传输加密、静态加密、最小权限账号和受控 migration role。
5. 迁移日志不得输出原始消息内容或凭据。

### NFR-5 可观测性

至少观测：

- query duration、source、status。
- returned/total count。
- filesystem `ls/read` 次数和估算字节数。
- projection success/error/lag。
- repair pending。
- rebuild scanned/projected/skipped/error/checkpoint。
- shadow mismatch。
- PostgreSQL pool、lock wait 和 query timeout。

Prometheus 普通 query metric 不使用 `account_id` 高基数 label；租户级信息进入受控状态表和结构化日志。

### NFR-6 运维

1. 提供 status、dry-run、rebuild、resume、verify、repair-meta 和 rollback 操作。
2. 长任务必须可暂停、限速和恢复。
3. 每次切换保留明确 rollback point。
4. Build frontend 必须先于会 mount `admin/dist/assets` 的 backend tests。

## 7. Adapter 需求

`AdminSessionQuery` 是稳定 interface，至少有两个真实 adapter：

- `SQLiteAdminSessionQuery`：本地开发、测试、单节点 shadow 验证。
- `PostgresAdminSessionQuery`：8000 酒店租户生产环境。

两个 adapter 必须运行同一份 contract test suite，保证过滤、排序、分页、daily、状态和 migration 语义一致。

调用者不得直接访问 SQLite connection、PostgreSQL pool、SQL 表或 migration 工具。

## 8. 兼容要求

- 现有 Admin endpoint 与请求参数不变。
- 现有 list/daily 业务字段不删除或改义。
- `query_meta` 为向后兼容新增字段。
- 现有 raw search/detail 行为不变。
- 现有 `ROOT/ADMIN/USER` 权限不变。
- 新 `.meta.json` 字段若通过 repair 写回，必须验证旧版本 reader 能忽略或正确读取。

## 9. 验收标准

阶段完成必须同时满足：

1. 需求对应的自动化测试全部通过。
2. 现有 Admin session/API 测试在 filesystem 与索引 adapter 下业务结果一致。
3. 两个租户使用相同 user/session id 时严格隔离。
4. 1001+ 用户/session 的 backfill 不静默截断。
5. 并发 backfill 与在线写/delete 不产生复活或覆盖新值。
6. ready list/daily 达到 I/O hard gate。
7. Dashboard 能区分零数据、loading、fallback、stale 和 error。
8. 运维人员可以在演练环境执行 dry-run、rebuild、verify 和 rollback。
9. PostgreSQL adapter 通过相同 contract tests 和 reference benchmark 后，才允许生产 `prefer`。

## 10. 待基线确认项

以下参数不应在没有生产数据时永久写死：

- 每租户日 session/message 分布。
- 峰值并发和高峰时段。
- Session、反馈和消息留存期。
- Projection freshness SLO。
- Admin 查询可用性目标。
- PostgreSQL 容量、分区和连接池参数。
- Daily 候选/scanned-row budget 与 8000 租户混合负载并发模型。
- Shadow 采样率和单批 backfill 限速。

Phase 0 必须产出这些基线或明确的初始保守值。
