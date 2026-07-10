# OpenViking Production Readiness Roadmap / OpenViking 生产化路线图

日期：2026-07-10

状态：Draft

Admin session 文档集：[README](./admin-session-performance/README.md)

入口需求：[Admin session 性能需求](./admin-session-performance/02-requirements.md)

## 1. 目标

从当前 MVP 出发，以 Admin session 慢查询为第一条纵向切片，逐步形成可服务约 8000 个酒店租户的生产版本。

路线图遵循三个约束：

1. 酒店租户继续使用 `account_id`，租户子账号继续使用 `user_id`。
2. 继续使用现有 `ROOT / ADMIN / USER` 权限语义。
3. 每个阶段都必须可验证、可发布、可回滚，不能依赖一次性大迁移。

## 2. 当前状态与最终状态

| 能力 | MVP 当前状态 | 生产目标 |
| --- | --- | --- |
| API | 单实例倾向、持有本地状态 | Stateless 多实例、负载均衡、滚动发布 |
| 租户隔离 | `account_id` 已进入 RequestContext/VikingFS | 所有 DB、对象、缓存、事件和搜索都强制 account predicate |
| Session 事实源 | 本地/AGFS 文件树 | Object-store-backed AGFS 或共享耐久内容层 |
| Admin 查询 | 扫描目录和 `.meta.json` | PostgreSQL query projection |
| 历史迁移 | 请求时 backfill | 离线、可恢复、按租户 generation rebuild |
| 账号注册表 | 分散文件与进程内索引 | 保持 interface，生产 adapter 使用事务存储 |
| 搜索 | 扫描 raw messages | 独立、可重建的全文索引 |
| 统计 | 请求时扫描并聚合 | Session projection + 必要的日统计物化 |
| 队列/任务 | 单节点本地能力较多 | Durable outbox/worker、幂等、可恢复 |
| 可观测性 | 局部 metrics/logs | SLO、租户状态、projection lag、迁移与恢复仪表盘 |
| 容灾 | 本地目录备份依赖强 | DB PITR、对象版本、恢复演练、明确 RPO/RTO |

## 3. 目标生产架构

```text
酒店前台 / 酒店技术人员
           |
     API Gateway / LB
           |
  Stateless OpenViking API replicas
           |
    +------+------+----------------+
    |             |                |
    v             v                v
PostgreSQL   Object Storage    Durable Workers
metadata     AGFS contents     outbox/projection
RBAC         resources         migration/repair
sessions     archives               |
summary          |                  +--> Search projection
outbox           |                  +--> Daily metrics
jobs             |                  +--> Reconciliation
    |             |
    +------ AdminSessionQuery ------+
```

### 3.1 权威数据与派生数据

| 数据 | 权威来源 | 是否可重建 |
| --- | --- | --- |
| 租户、子账号、角色、凭据 | PostgreSQL control plane | 否，需要备份/PITR |
| Session raw messages/archive | AGFS/Object Storage | 否，需要版本和备份 |
| 知识库素材与资源正文 | AGFS/Object Storage | 否，需要版本和备份 |
| Admin session summary | PostgreSQL projection | 是 |
| Daily metrics | PostgreSQL projection/materialization | 是 |
| Full-text search | Search engine | 是 |
| Cache | Redis/进程缓存 | 是 |
| Migration checkpoint/job | PostgreSQL control plane | 任务运行期必须耐久 |

## 4. 深模块与 adapter 演进

生产化不应让 Dashboard、SessionService 或路由理解存储实现。稳定 interface：

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

Adapter 演进：

```text
Filesystem scan        兼容读路径，不实现该 interface 的生产写能力
SQLite adapter         本地开发、contract test、shadow 验证
PostgreSQL adapter     8000 租户生产目标
```

SQLite 的价值是验证 interface、schema 和迁移机制，不是建设另一套 HA 数据库。

## 5. 阶段路线图

Phase 0-3 是可以独立发布和回滚的生产化增量，但只完成 Admin 查询切片，不等于整个平台已经可投产。面向全部酒店宣布 production ready，仍必须达到 Phase 4 和第 6/9 节的完整门禁；Phase 5 中尚未索引的能力在此之前必须有明确的 timeout、范围、大小和 rate-limit 护栏。

### Phase 0：事实基线与止血

交付：

- Admin endpoint elapsed time 与 VikingFS I/O 观测。
- user/session 枚举完整性与 `node_limit` 截断处理。
- S/M/L benchmark fixture。
- 现有 list/daily/search/detail 兼容 contract 固化。

退出门禁：

- 能回答请求读取多少 user/session/meta/raw/archive 和字节数。
- 1001+ 节点不会静默返回完整结果。
- 基线结果保存并可重复。

回滚：只移除/关闭观测，不涉及数据迁移。

### Phase 1：查询 module 与本地 adapter

交付：

- `AdminSessionQuery` interface 和领域 query/result 类型。
- SQLite adapter、schema migration 和 contract tests。
- 统一 projection hook。
- Read-only rebuild、status、verify。
- 按 `account_id` 持久化的 `off/shadow` 模式。

退出门禁：

- SQLite 与 filesystem 业务结果一致。
- backfill 幂等、可恢复、不在请求路径执行，且默认不回写 `.meta.json`。
- projection 失败不影响事实写入。
- ready 查询文件 IO 为零。

回滚：目标账号 `read_mode=off`；确认无账号读取 SQLite 后可删除该派生文件。

### Phase 2：PostgreSQL production adapter

交付：

- PostgreSQL schema、migration、connection pool 和 adapter。
- 与 SQLite 共用的 contract suite。
- account-scoped rebuild/checkpoint/state。
- PostgreSQL shadow comparison。
- DB pool、query、lock、timeout metrics。

退出门禁：

- PostgreSQL adapter 通过所有 contract、隔离、故障和 benchmark 测试。
- 10 万 session reference fixture 达到性能门禁。
- DB 不可用时事实写入与 Admin 失败语义符合设计。
- 备份、PITR 和 schema rollback 在演练环境验证。

回滚：读路径回 `off`；保留 PostgreSQL projection 供排查，不反向修改事实源。

### Phase 3：按租户切读

批次：

1. 内部测试租户。
2. 小数据量酒店 canary。
3. 中等数据量酒店。
4. 最大历史数据酒店。
5. 分批扩至全部租户。

每个租户执行：

```text
enable projection
-> dry-run
-> rebuild
-> verify
-> shadow
-> prefer
-> required
```

退出门禁：

- Shadow 无未解释 mismatch。
- Projection lag、query p95、fallback 与 error rate 达标。
- 账号删除、备份恢复、重建和 rollback 演练通过。

### Phase 4：服务无状态化与共享存储

交付：

- API 多实例和负载均衡。
- 托管 PostgreSQL HA。
- AGFS 内容层迁移到 Object Storage/shared backend。
- Durable outbox 和 projection workers。
- 账号/子账号注册表生产 adapter。
- Secret/KMS、备份、PITR、对象版本和生命周期。

退出门禁：

- 任一 API 实例故障不影响整体服务。
- 多实例下租户隔离、projection 幂等和任务恢复通过。
- 已完成实际恢复演练，而不只是存在备份配置。
- 本地 workspace 不再是生产唯一耐久边界。

### Phase 5：搜索、统计与长期治理

按真实需求交付：

- Message full-text projection。
- 酒店级 daily metrics 物化。
- 数据保留、归档、合规删除。
- 容量预测和成本告警。
- 更细的审计与运营报表。

Search engine 和分析数据库只在数据与查询证明需要时引入；不因为“未来可能很大”提前增加所有基础设施。

## 6. 生产就绪矩阵

| 维度 | 必须达到的状态 |
| --- | --- |
| 正确性 | 文件与 projection 结果一致；并发写/删/backfill 无复活和覆盖新值 |
| 租户隔离 | 所有持久化与查询使用 account_id；跨租户自动化测试为硬门禁 |
| 性能 | ready list/daily 文件 IO 为零；达到 reference p95 |
| 可用性 | 前台写入不依赖 Admin projection；API 支持多实例 |
| 数据耐久 | PostgreSQL PITR；Object Storage 版本；定期恢复演练 |
| 迁移 | dry-run、checkpoint、resume、verify、shadow、rollback 可操作 |
| 安全 | 加密、最小权限、凭据轮换、审计、租户删除闭环 |
| 观测 | endpoint/source/lag/error/backfill 指标与告警 |
| 运维 | Runbook、on-call 入口、故障分级和明确 owner |
| 发布 | canary、按租户切换、回滚开关和兼容窗口 |

任一项缺失都不能仅凭“功能测试通过”宣称可投产。

## 7. 容量与分区原则

已知规模是约 8000 个租户，未知的是每租户会话分布。数据库设计先遵循：

- 所有主键、唯一键和高频索引以 `account_id` 开头或包含它。
- Admin session projection 表按实际总量评估按月/时间分区，不在无基线时预设复杂 sharding。
- 大租户必须能独立 backfill、暂停和限速。
- PostgreSQL connection pool 按 API/worker 实例总量统一预算。
- Prometheus 不把 8000 个 account 直接作为所有指标 label。
- 对象存储 prefix、生命周期和清理任务按 account 分组。

## 8. 发布与回滚原则

1. Schema 先兼容旧代码，再部署新写入，再切新读取。
2. Projection 必须先于历史扫描启用，覆盖 backfill 窗口的新写入。
3. 读路径按租户切换，不全局一刀切。
4. Shadow 采用受控采样，不能让新旧查询使生产 IO 翻倍。
5. `prefer` 是验证阶段，不是永久隐藏错误的 fallback。
6. `required` 才代表该租户默认查询正式脱离文件扫描。
7. 回滚读路径不删除事实数据，也不要求反向迁移 raw session。

## 9. Production Definition of Done

面向全部目标租户宣布可投产前，至少满足：

- PostgreSQL production adapter 已用于无搜索 list/daily。
- API 至少两个可替换实例，实例本地磁盘不是事实源。
- 租户知识、素材、会话、记忆、统计和凭据隔离测试通过。
- 历史数据已按租户迁移并完成 count/fingerprint/aggregate 校验。
- 账号删除和备份恢复能够同步处理所有派生层。
- 前台主路径在 Admin index、搜索或 analytics 故障时仍可用。
- 尚未完成 FTS/message pagination 的 `q` 搜索和详情读取已有超时、日期/结果/大小上限与限流，不能拖垮事实写入或其他租户。
- 性能、projection lag、错误率、容量和磁盘/DB 告警已接入。
- 备份恢复、数据库故障、worker 中断和回滚均完成演练。
- 运维人员持有可执行 runbook，发布和回滚 owner 明确。

## 10. 近期顺序

1. 完成并评审本组文档。
2. 实施 Phase 0 观测与枚举完整性。
3. 建立 `AdminSessionQuery` contract 和 adapter contract tests。
4. 实施 SQLite adapter 以验证 seam 和迁移流程。
5. 在生产切读前完成 PostgreSQL adapter，不把 SQLite 当作 8000 租户终态。
