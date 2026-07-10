# Admin Session Performance Analysis / Admin 会话查询性能分析

日期：2026-07-09

状态：Draft

## 背景

生产启动方式与本地开发启动方式类似：

```bash
OPENVIKING_CONFIG_FILE=~/.openviking/ov-dev.conf \
OPENVIKING_CLI_CONFIG_FILE=~/.openviking/ovcli.conf \
openviking-server \
  --host 127.0.0.1 \
  --port 1933 \
  --with-bot
```

这份文档用于解释 Admin Dashboard 与 Admin 会话列表在会话数据增长后变慢的原因，并为下一步修复、优化和长期维护提供依据。文档只记录非敏感配置事实，不记录令牌、密钥等凭据。

后续文档：

- [需求](./02-requirements.md)
- [设计](./03-design.md)
- [测试计划](./04-test-plan.md)
- [实现计划](./05-implementation-plan.md)
- [迁移运行手册](./06-migration-runbook.md)
- [生产化路线图](../production-readiness-roadmap.md)

## 结论摘要

当前 Admin 慢查询的主因不是前端渲染，也不是向量检索，而是 Admin 查询路径把会话文件树当作查询数据库使用：

- 会话事实源是 `VikingFS/AGFS` 文件树，消息和元信息分散在每个 session 目录中。
- Dashboard daily analytics 和无搜索的会话列表会跨用户遍历所有 session，并逐个读取 `.meta.json`。
- 会话列表分页是在后端完成全量收集、排序之后才切片，`page_size` 不能减少后端扫描成本。
- 搜索和详情会进一步读取 `messages.jsonl` 与历史归档 `history/archive_NNN/messages.jsonl`，成本随消息总量和归档数量增长。
- `VikingFS.read_file()` 默认整文件读入；`VikingFS.append_file()` 也是读旧文件、拼接、整文件写回，会带来读写放大。
- 当前没有独立文件服务器/OSS 时，`storage.workspace` 同时承担会话事实源、知识文档 registry、资源文件、临时上传和本地向量库路径职责，后续备份、恢复、迁移、扩容和一致性修复都会变重。
- 当前没有关系型数据库时，风险不只在性能，也在元数据事务、唯一性约束、分页查询、审计、迁移和跨进程并发控制。

因此，随着会话数量增长，Dashboard 和列表页延迟大体按 session 数线性增长；带搜索时还会按总消息字节数继续放大。

## 启动与配置事实

`openviking-server` 启动时，`--config` 会写入 `OPENVIKING_CONFIG_FILE`，随后服务端通过 `load_server_config()` 与 `OpenVikingConfigSingleton.initialize()` 加载配置。若命令没有传 `--config`，则会使用环境变量中的 `OPENVIKING_CONFIG_FILE`。[openviking/server/bootstrap.py](../../../openviking/server/bootstrap.py#L112-L123)

服务端配置加载顺序为：显式 `config_path`、`OPENVIKING_CONFIG_FILE`、默认 `~/.openviking/ov.conf`。[openviking/server/config.py](../../../openviking/server/config.py#L75-L95) OpenViking 全局配置还会在后续 fallback 到 `/etc/openviking/ov.conf`。[openviking_cli/utils/config/open_viking_config.py](../../../openviking_cli/utils/config/open_viking_config.py#L262-L320)

本机 `~/.openviking/ov-dev.conf` 当前只显式配置了 `storage.workspace`，没有显式配置 `storage.agfs`。在这种配置下，以代码默认值为准：

- `storage.workspace` 是本地数据根目录，且会覆写 AGFS 与 VectorDB 的 path。[openviking_cli/utils/config/storage_config.py](../../../openviking_cli/utils/config/storage_config.py#L17-L66)
- AGFS 默认 `mode` 是 `binding-client`，默认 `backend` 是 `local`。[openviking_cli/utils/config/agfs_config.py](../../../openviking_cli/utils/config/agfs_config.py#L85-L112)
- 如果 `mode == "http-client"`，服务才会启动 `AGFSManager` 子进程；否则会直接创建 AGFS client。[openviking/service/core.py](../../../openviking/service/core.py#L108-L155)

`OPENVIKING_CLI_CONFIG_FILE` 影响 CLI 客户端连接哪个 server、超时时间和 API key；它不决定服务端会话数据落盘位置。

## 当前数据存储方式

OpenViking 文档定义的是双层存储：AGFS 保存内容，向量库保存 URI、向量和元数据，不保存文件内容。[docs/zh/concepts/05-storage.md](../../zh/concepts/05-storage.md#L1-L33)

会话使用 `viking://session/{user_space}/{session_id}` 作为 URI。当前 `user_space_name()` 返回 `user_id`。[openviking/session/session.py](../../../openviking/session/session.py#L352-L355) [openviking_cli/session/user_id.py](../../../openviking_cli/session/user_id.py#L48-L50)

VikingFS 将 `viking://...` 映射成账号隔离的 AGFS 路径 `/local/{account_id}/...`。[openviking/storage/viking_fs.py](../../../openviking/storage/viking_fs.py#L1204-L1217) 对 local AGFS backend 而言，`/local` 对应 `storage.workspace/viking`。[openviking/agfs_manager.py](../../../openviking/agfs_manager.py#L157-L164)

因此本地或类似生产配置下，session 目录形态可以理解为：

```text
{storage.workspace}/viking/{account_id}/session/{user_id}/{session_id}/
  messages.jsonl
  .meta.json
  feedback.json
  history/
    archive_001/
      messages.jsonl
      .abstract.md
      .overview.md
      .meta.json
      .done
```

关键文件：

- `.meta.json`：保存 `SessionMeta`，包括 `audit_summary`、`feedback_summary`、token 统计等。[openviking/session/session.py](../../../openviking/session/session.py#L81-L164)
- `messages.jsonl`：保存当前 live session 消息。`Session.load()` 会整文件读取并逐行反序列化。[openviking/session/session.py](../../../openviking/session/session.py#L367-L428)
- `history/archive_NNN/messages.jsonl`：commit/archive 后的历史消息文件。[openviking/session/session.py](../../../openviking/session/session.py#L1038-L1055)
- `feedback.json`：assistant message 反馈索引，加载 session 时读取并归一化。[openviking/session/session.py](../../../openviking/session/session.py#L418-L426)

新增消息时，`Session.add_message()` 会更新内存中的 `audit_summary`，append 到 `messages.jsonl`，并同步保存 `.meta.json`。[openviking/session/session.py](../../../openviking/session/session.py#L850-L920)

## Admin Dashboard 查询链路

Admin Dashboard 会同时请求账户列表、健康检查、就绪检查和 daily analytics；其中与慢加载直接相关的是：

```text
GET /api/v1/admin/accounts/{account_id}/analytics/daily
```

前端调用见 [admin/src/pages/Dashboard.tsx](../../../admin/src/pages/Dashboard.tsx#L715-L742)。路由入口见 [openviking/server/routers/admin.py](../../../openviking/server/routers/admin.py#L572-L596)。

后端 `get_admin_daily_analytics()` 会调用 `_collect_admin_session_summaries()`，后者会：

1. 列出账号下用户。
2. 对每个用户列出 `viking://session/{user_id}` 下所有 session。
3. 对每个 session 读取 `.meta.json`。
4. 基于 `last_message_at/updated_at/created_at` 做日期过滤。
5. 在内存中按天聚合 active users、session count、message count、tool count、feedback count、token usage。

实现位置：[openviking/service/session_service.py](../../../openviking/service/session_service.py#L630-L670) 与 [openviking/service/session_service.py](../../../openviking/service/session_service.py#L784-L912)。

复杂度：

```text
O(users + sessions directory listing + session_count * read(.meta.json) + session_count sort/aggregation)
```

即使只看最近 14 天，目前仍然需要先扫描 session meta，再根据 meta 中的活动时间过滤。除非数据天然按日期分目录或存在独立索引，否则日期范围不能有效减少 IO。

## Admin 会话列表查询链路

Admin 会话列表请求：

```text
GET /api/v1/admin/accounts/{account_id}/sessions
```

前端会传 `page`、`page_size`、`sort_by`、`sort_order`、日期范围、可选 `q`。[admin/src/pages/Sessions.tsx](../../../admin/src/pages/Sessions.tsx#L1150-L1193) 路由入口见 [openviking/server/routers/admin.py](../../../openviking/server/routers/admin.py#L599-L632)。

无搜索时，`list_admin_sessions_paginated()` 会走 `_collect_admin_session_summaries()`，读取所有候选 session 的 `.meta.json`。随后 `_sort_admin_sessions()` 在内存排序，最后才按 `page/page_size` 切片返回。[openviking/service/session_service.py](../../../openviking/service/session_service.py#L722-L782)

带 `q` 搜索时，`list_admin_sessions_paginated()` 会走 `_collect_admin_sessions()`。该路径对每个 session 调用 `get_admin_session_detail()`，而 `get_admin_session_detail()` 会先读取所有 archive 和 live messages，再做 `_message_matches_query()`。[openviking/service/session_service.py](../../../openviking/service/session_service.py#L672-L720) [openviking/service/session_service.py](../../../openviking/service/session_service.py#L452-L484)

测试也锁定了当前设计意图：plain paginated list 和 daily analytics 在 summary 可用时不应读取 raw `messages.jsonl`，而应使用 `.meta.json` summary。[tests/server/test_admin_api.py](../../../tests/server/test_admin_api.py#L1101-L1147) [tests/server/test_admin_api.py](../../../tests/server/test_admin_api.py#L1150-L1154)

复杂度：

```text
无搜索列表:
O(users + session_count * read(.meta.json) + session_count log session_count)

带 q 搜索:
O(users + session_count * (load live messages + list archives + read archive messages))
≈ O(total_message_bytes + total_archive_files)
```

当前分页只减少响应体大小，不减少服务端扫描、读取和排序成本。

## 文件 IO 放大点

`VikingFS.read_file()` 会调用 `agfs.read(path)` 读取完整文件，然后再按 line offset/limit 做内存切片；开启加密时也必须先解密完整文件。[openviking/storage/viking_fs.py](../../../openviking/storage/viking_fs.py#L1616-L1662)

`VikingFS.append_file()` 当前不是底层 append。它会读取已有文件、解密、decode，拼接新内容，再整文件写回。[openviking/storage/viking_fs.py](../../../openviking/storage/viking_fs.py#L1693-L1724)

影响：

- 长 live `messages.jsonl` 会让每次新增消息写入成本变高。
- session detail、搜索、backfill 读取历史消息时会有整文件读放大。
- 如果未来开启加密，部分读取也无法避免完整文件解密。

## 隐藏正确性与维护风险

### 1. `ls()` 默认 node limit

`VikingFS.ls()` 默认 `node_limit=1000`，`_ls_original()` 达到 limit 后直接停止收集。[openviking/storage/viking_fs.py](../../../openviking/storage/viking_fs.py#L1724-L1755) [openviking/storage/viking_fs.py](../../../openviking/storage/viking_fs.py#L1805-L1833)

当前 `SessionService.sessions()` 和 `_list_admin_user_ids()` 调用 `ls()` 时没有显式传更大的 `node_limit`。[openviking/service/session_service.py](../../../openviking/service/session_service.py#L339-L365) [openviking/service/session_service.py](../../../openviking/service/session_service.py#L486-L502)

风险：

- 单账号用户数超过 1000 或单用户 session 数超过 1000 时，Admin 列表和统计可能不完整。
- 即使暂时没有超过，也会在增长后变成难以察觉的数据缺失。

### 2. 请求时 backfill

如果 `.meta.json` 缺少当前版本 `audit_summary`，`_read_admin_session_meta_summary()` 会在请求路径上回退到 `_backfill_admin_session_meta_summary()`，读取所有消息、生成 summary 并写回 `.meta.json`。[openviking/service/session_service.py](../../../openviking/service/session_service.py#L504-L539)

风险：

- 第一次访问历史数据时，列表或 Dashboard 请求会被 backfill 放大。
- backfill 写入发生在读接口路径上，容易造成延迟尖刺。
- 多个 Admin 请求并发触发时，会重复竞争同一批 session 文件。

### 3. `.meta.json` 是局部缓存，不是查询索引

`.meta.json` 缓存了单 session summary，能避免无搜索列表读取所有 raw messages，但它仍然分散在每个 session 目录中。Admin 查询没有一个可按账号、日期、排序字段直接查的全局索引。

风险：

- 运维列表、统计、排序、日期范围都必须扫描大量 session 目录。
- `.meta.json` 版本升级需要 request-time backfill 或离线迁移。
- 排序字段越多，越倾向于全量内存排序。

### 4. 搜索缺少消息级索引

`q` 搜索直接扫描 raw message 和 tool fields，没有倒排索引或 FTS。它适合小规模审计，不适合生产大规模交互式搜索。

风险：

- 搜索会成为最慢的交互。
- 搜索与详情共享大量读取逻辑，容易互相影响。
- 用户频繁搜索时会把 AGFS 小文件读压力放大。

### 5. 配置文档与代码默认值存在漂移风险

代码中 `AGFSConfig.mode` 默认是 `binding-client`。[openviking_cli/utils/config/agfs_config.py](../../../openviking_cli/utils/config/agfs_config.py#L101-L108) 配置指南中 agfs 表格仍写着 `http-client` 默认值。[docs/zh/guides/01-configuration.md](../../zh/guides/01-configuration.md#L558-L606)

风险：

- 排查生产性能时，维护者可能按文档误判实际运行模式。
- 后续优化方案需要先以运行时配置和代码默认值为准。

### 6. 单节点本地存储约束

服务初始化会对 `storage.workspace` 获取 advisory PID lock；代码明确提示多个 OpenViking 进程共享同一个 data directory 会造成存储竞争和数据损坏风险。[openviking/utils/process_lock.py](../../../openviking/utils/process_lock.py#L59-L82)

AGFS 内置 queuefs 当前配置为 SQLite backend，并在代码注释中说明该 backend 是 single-node only；多节点部署需要切换到共享队列后端，但当前 TiDB 路径也还缺少可靠的 ack/recover 语义。[openviking/agfs_manager.py](../../../openviking/agfs_manager.py#L136-L153)

风险：

- 生产如果通过多个 `openviking-server` 进程或多台机器直接共享同一个本地 `storage.workspace`，不是简单横向扩容，会触发一致性和队列可见性问题。
- Admin index / daily metrics 若落在同一 workspace，也应按单写者或明确锁策略设计，避免索引派生层先于事实源出现并发损坏。

## 文档资源与数据库缺失的维护风险

除了 Admin 会话查询性能，当前“无独立文件服务器/OSS、无关系型数据库”的形态也会影响已有文档和资源数据的长期维护。

当前事实：

- `storage.workspace` 是本地数据根目录，并会覆写 AGFS 与 VectorDB 的 path；上传临时目录固定在 `{workspace}/temp/upload`。[openviking_cli/utils/config/storage_config.py](../../../openviking_cli/utils/config/storage_config.py#L20-L75)
- VectorDB 默认 backend 是 `local`，即本地文件型向量库；如果没有显式配置远程 VectorDB，它也在同一个 workspace 维护本地数据。[openviking_cli/utils/config/vectordb_config.py](../../../openviking_cli/utils/config/vectordb_config.py#L53-L63)
- 知识文档 registry 是文件型 registry，位于 `{workspace}/knowledge_documents/{account_id}/records/*.json` 与 `folders/*.json`，列表查询通过 glob 读取 JSON 后排序。[openviking/service/knowledge_document_registry.py](../../../openviking/service/knowledge_document_registry.py#L288-L329)
- 文档 upsert 会在本地源文件存在时复制原始文件，并写入 JSON record。[openviking/service/knowledge_document_registry.py](../../../openviking/service/knowledge_document_registry.py#L594-L676)
- HTTP 上传会先写入本地 temp 目录，再由 add resource 解析 temp id 后进入资源处理链路。[openviking/server/routers/resources.py](../../../openviking/server/routers/resources.py#L157-L215) [openviking/server/routers/resources.py](../../../openviking/server/routers/resources.py#L327-L358)
- `ResourceService` 只在当前进程内用 `asyncio.Lock` 保护文档 registry 操作；它不是跨进程、跨机器的分布式锁。[openviking/service/resource_service.py](../../../openviking/service/resource_service.py#L72-L91)
- 文档删除、移动、文件夹重命名需要同步 AGFS resource tree 与文件型 registry；失败时部分路径有 rollback，但仍属于应用层补偿逻辑，不是数据库事务。[openviking/service/resource_service.py](../../../openviking/service/resource_service.py#L438-L466) [openviking/service/resource_service.py](../../../openviking/service/resource_service.py#L497-L584) [openviking/service/resource_service.py](../../../openviking/service/resource_service.py#L669-L704)
- 代码已经提供 `cleanup_orphan_resource_vectors()`，通过扫描向量索引并检查 AGFS 文件是否存在来清理孤儿向量记录，这说明资源内容、registry 与向量索引之间存在需要运维修复的一致性边界。[openviking/service/resource_service.py](../../../openviking/service/resource_service.py#L586-L667)

### 没有独立文件服务器/OSS的风险

1. **本地 workspace 成为单点耐久边界。** 原始文档副本、AGFS 资源树、session 数据、registry JSON、临时上传、本地向量库可能都落在同一块磁盘或同一目录树下。磁盘损坏、误删、空间耗尽、inode 耗尽都会同时影响多个数据域。
2. **备份恢复容易不一致。** 如果只备份 `viking/`，会漏掉 `knowledge_documents/` registry；只备份 registry，又会漏掉资源正文或本地向量库。恢复时还需要保证 session 文件、文档 registry、资源树、向量索引来自同一时间点。
3. **对象生命周期能力不足。** 本地目录没有 OSS/S3 常见的版本控制、生命周期清理、对象锁、跨区复制、服务端校验和、低频归档、签名下载 URL 等能力。对已有文档的留存、回滚和合规删除都需要自己补。
4. **大文件会与在线请求抢 IO。** 上传、文档解析、资源移动、向量化、Admin 列表扫描、session 写入都共享同一个本地 IO 面。文档量变大后，慢的不只是资源管理页，也可能拖慢会话写入和 Dashboard。
5. **横向扩容受限。** 如果多台机器各自有本地 workspace，数据会分裂；如果多进程/多机器共享同一目录，又会遇到前面提到的锁、队列和文件一致性问题。没有共享对象存储时，很难把 app server 做成无状态。
6. **迁移和目录重组成本高。** 移动文档或重命名文件夹时，资源 URI、registry record、向量索引都要同步。任何中途失败都可能产生 registry 指向不存在资源、资源存在但 registry 丢失、向量记录仍指向旧 URI 等状态。
7. **临时上传也需要运维策略。** 当前 temp upload 依赖本地目录和按时间清理。生产上需要监控 temp 目录大小、清理失败、异常中断留下的临时文件，否则会逐步侵占业务数据盘。

### 没有关系型数据库的风险

这里的结论不是“必须把原始文档和所有消息正文都放进数据库”。更合理的边界是：对象存储保存 blob/文档/媒体，AGFS 或兼容层保存内容事实源，关系型数据库保存元数据、索引、任务状态和审计。

当前缺少关系型数据库时，主要风险包括：

1. **查询能力弱。** 文档列表、session 列表、统计、排序、过滤都倾向于扫描 JSON 文件或目录树；无法自然使用 `WHERE/ORDER BY/LIMIT/OFFSET`、复合索引、唯一约束。
2. **事务边界弱。** 一次资源变更通常跨 AGFS 文件、registry JSON、向量索引，甚至后台处理任务。没有 DB transaction/outbox 时，失败恢复依赖应用层补偿和后续扫描。
3. **并发控制弱。** 进程内 `asyncio.Lock` 能保护单进程内的 registry 操作，但不能保护多 worker、多进程或多机器。生产如果未来改成多进程服务，这里会成为正确性风险。
4. **约束和引用关系弱。** 文件型 JSON registry 难以表达和强制 document id 唯一、folder path 唯一、document-folder 外键、删除级联、资源 URI 唯一等约束。
5. **迁移与版本升级重。** JSON schema 或 `.meta.json` summary 升级需要手写 backfill、版本判断、失败重跑和兼容读取；数据量越大，越需要可观测的迁移任务表。
6. **审计和追溯弱。** 谁上传、谁移动、谁删除、何时清理了孤儿向量、一次后台任务处理到哪一步，如果只靠分散文件和日志，后期问题定位会困难。
7. **修复工具依赖扫描。** 孤儿资源、孤儿向量、registry 缺失、checksum 不一致，都需要扫描整个 workspace 才能发现。数据量增长后，修复本身也会变成重 IO 任务。

### 建议的演进边界

短期不要急着大迁移，先给现有 workspace 加运维护栏：

- 明确备份范围：至少覆盖 `viking/`、`knowledge_documents/`、本地 VectorDB 数据和必要的 `_system/` 配置；`temp/upload` 可按策略排除，但要能清理。
- 备份应尽量使用停机快照或文件系统快照，避免 registry 与资源树来自不同时间点。
- 增加文档资源 inventory：document count、folder count、resource root count、原始文件总字节、temp upload 总字节、orphan vector count。
- 为资源文件记录 checksum、size、created_at、updated_at，并定期 dry-run 校验 registry 指向的资源是否存在。
- 定期 dry-run `cleanup_orphan_resource_vectors()`，把 orphan 数量作为告警指标。
- 增加磁盘容量、inode、IO wait、temp 目录大小、单文件大小告警。

中期建议引入独立对象存储或文件服务：

- 原始上传文档、解析后的资源文件、大媒体文件优先迁移到 OSS/S3/MinIO 等对象存储。
- registry 或 DB 中只保存 object key、checksum、size、content type、版本、生命周期状态。
- 下载/预览大文件时优先使用签名 URL 或独立文件服务，app server 负责鉴权和元数据，不直接承担大文件传输。
- 如果继续保留 AGFS URI 语义，可以做 object-store-backed AGFS，避免一次性破坏上层调用。

中长期建议引入关系型数据库作为 metadata/control plane：

- 单节点开发/验证阶段可以先用 SQLite 建 admin/session index；约 8000 酒店租户的多实例生产目标使用 PostgreSQL query/control plane。
- 优先落表：`knowledge_documents`、`knowledge_folders`、`resource_objects`、`admin_session_index`、`admin_daily_metrics`、`background_jobs`、`audit_events`、`outbox_events`。
- 向量库只作为检索索引，不作为 metadata 的权威来源。
- 对 AGFS/object store 和向量库的写入，使用 job/outbox 记录状态，支持失败重试、幂等、人工修复。
- 历史迁移按 dual-read、backfill、校验、dual-write、切读路径的顺序推进，避免一次性切换不可回滚。

## 优化目标

下一步优化不应直接把所有会话源数据迁移到数据库。更稳妥的目标是：

1. 保留现有 session 文件树作为事实源，避免破坏已有数据兼容性。
2. 为 Admin 查询建立专用的物化索引/统计层，让列表和 Dashboard 不再全量扫描 session 文件。
3. 把历史数据 backfill 从请求路径移到离线任务或启动后后台任务。
4. 为搜索建立独立消息索引，避免交互式搜索读取所有 raw messages。
5. 给性能行为加观测指标，保证优化前后可比较、可回滚。

## 分阶段方案

### 阶段 0：观测与止血

目标：先证明每个请求到底读了多少文件、花了多少时间。

建议新增轻量指标：

- endpoint elapsed time。
- listed user count。
- listed session count。
- `.meta.json` read count。
- backfill session count。
- raw `messages.jsonl` read count。
- archive count。
- estimated bytes read。
- returned item count。

建议先覆盖：

- `GET /api/v1/admin/accounts/{account_id}/analytics/daily`
- `GET /api/v1/admin/accounts/{account_id}/sessions`
- `GET /api/v1/admin/accounts/{account_id}/sessions?q=...`
- `GET /api/v1/admin/accounts/{account_id}/sessions/{session_id}`

同时修复或显式处理 `ls(node_limit=1000)` 风险：Admin session/user listing 应该明确传入足够大的 limit、支持 continuation，或在达到 limit 时返回告警字段。

### 阶段 1：Admin session index

目标：让普通会话列表不再逐 session 读 `.meta.json`。

引入一个 Admin 查询投影：先用本地 SQLite adapter 验证 interface、schema、迁移和回滚，再用同一 interface 的 PostgreSQL adapter 承担约 8000 酒店租户的生产查询；两者都不替代 AGFS 事实源。

建议表结构：

```text
admin_session_index(
  account_id,
  user_id,
  session_id,
  uri,
  created_at,
  updated_at,
  first_message_at,
  last_message_at,
  message_count,
  user_message_count,
  assistant_message_count,
  tool_call_count,
  failed_tool_call_count,
  feedback_count,
  positive_feedback_count,
  negative_feedback_count,
  prompt_tokens,
  completion_tokens,
  total_tokens,
  audit_summary_version,
  audit_summary_complete,
  index_updated_at,
  deleted_at
)
```

推荐索引：

- `(account_id, last_message_at desc)`
- `(account_id, user_id, last_message_at desc)`
- `(account_id, created_at)`
- `(account_id, updated_at)`
- `(account_id, negative_feedback_count desc)`
- `(account_id, total_tokens desc)`

写入策略：

- `Session.add_message()` 更新 `.meta.json` 后，同步或异步 upsert index。
- feedback 更新后 upsert feedback summary。
- delete session 后设置 `deleted_at` 或删除 index row。
- 历史数据通过离线 backfill 建索引，支持幂等重跑。

读路径：

- 无搜索列表直接走 `admin_session_index` 的 `WHERE/ORDER BY/LIMIT/OFFSET`。
- Dashboard daily analytics 可以先从 index 聚合，避免读每个 `.meta.json`。
- 如果 index 缺失或版本不匹配，返回明确 stale 状态或后台补齐，不在用户请求里全量 backfill。

### 阶段 2：Daily analytics 物化

目标：Dashboard 不随 session 数增长而线性变慢。

两种可选方式：

1. 直接从 `admin_session_index` 按日期聚合，适合中等规模。
2. 维护 `admin_daily_metrics` 物化表，适合数据量继续增长。

建议表结构：

```text
admin_daily_metrics(
  account_id,
  user_id_or_all,
  date,
  timezone_policy,
  active_user_count,
  session_count,
  message_count,
  user_message_count,
  assistant_message_count,
  tool_call_count,
  failed_tool_call_count,
  feedback_count,
  positive_feedback_count,
  negative_feedback_count,
  prompt_tokens,
  completion_tokens,
  total_tokens,
  updated_at
)
```

注意：当前 daily analytics 支持浏览器时区参数 `tz`。如果做物化 daily 表，需要明确时区策略：

- 方案 A：统一用 UTC 存储，前端时区查询时动态转换。
- 方案 B：为主要业务时区单独物化，例如 `Asia/Shanghai`。
- 方案 C：只物化 UTC，总览 KPI 走 UTC，钻取按用户时区动态聚合。

当前 API 支持任意 IANA 时区，且尚未确认每个租户的固定业务时区。v1 先从 session projection 动态聚合；如果基线证明必须物化，应先明确 UTC 或租户配置时区的产品语义，再建立可重算的日统计，不能直接把浏览器时区固化成全局业务口径。

### 阶段 3：消息搜索索引

目标：`q` 搜索不再扫描所有 session 的 raw messages。

建议引入消息级索引：

```text
admin_message_search(
  account_id,
  user_id,
  session_id,
  message_id,
  role,
  created_at,
  searchable_text,
  tool_names,
  tool_statuses
)
```

SQLite 可用 FTS5；如果部署环境已有外部搜索服务，也可以用 OpenSearch/Meilisearch 等。但第一步建议嵌入式索引，减少运维面。

搜索结果先返回匹配的 `(user_id, session_id, matched_message_count)`，再 join `admin_session_index` 输出列表字段。这样 `q` 搜索的成本从扫描全部 raw messages 变为索引查询。

### 阶段 4：写路径与大文件治理

目标：降低单 session 长对话和频繁消息写入的读写放大。

可选优化：

- 为 local/binding backend 增加真正 append 能力，避免 `append_file()` 每次读旧文件再整文件写回。
- 控制 live `messages.jsonl` 最大大小，更积极地 archive。
- detail 页默认按页读取消息，避免打开详情就返回全部历史消息。
- 对 archive 建 metadata index，详情页按 archive/message page 懒加载。
- 如果启用加密，需要评估“整文件解密”对分页读取的限制，必要时改成分块加密格式。

## 验证计划

优化前需要先固化基线。建议写一个可重复的本地 benchmark/fixture：

```text
accounts: 1
users: 10, 100
sessions_per_user: 100, 1000
messages_per_session: 4, 20, 100
archives_per_session: 0, 3, 10
```

每组测：

- Dashboard daily analytics p50/p95。
- sessions list page 1 p50/p95。
- sessions list page N p50/p95。
- sessions list with q p50/p95。
- session detail with and without messages p50/p95。
- AGFS `ls/read/write/stat` 次数。
- 估算读取字节数。
- backfill 数量。

验收目标建议：

- 无搜索会话列表延迟与总 session 数解耦，主要受 `page_size` 和索引查询影响。
- Dashboard 默认日期范围查询不读取 session `.meta.json`。
- `q` 搜索不读取非命中 session 的 raw messages。
- 达到 `node_limit` 时有明确错误或分页机制，不静默丢数据。
- 所有索引都可以从 session 文件事实源重建。

## 维护策略

### 事实源与派生层边界

短中期建议保持：

- AGFS session 文件树是事实源。
- Admin index / daily metrics / FTS 是派生查询层。
- 派生层可删除重建，不作为唯一数据源。

这样可以降低迁移风险，并允许优化失败时回退到现有文件扫描路径。

### Backfill

Backfill 要求：

- 幂等。
- 可断点续跑。
- 可 dry-run 输出预计 session 数、文件数、字节数。
- 有进度与错误报告。
- 不在 Admin 请求路径上做大规模同步 backfill。

### 版本控制

建议给 index schema 与 `.meta.json` summary 分别加版本：

- `admin_session_index.schema_version`
- `SessionMeta.audit_summary_version`
- `SessionMeta.audit_summary_complete`

当版本不一致时，后台任务负责补齐；用户请求只返回 stale 提示或临时回退小范围读取。

### 运维检查

生产巡检建议包括：

- `storage.workspace` 所在磁盘容量、inode、IO wait。
- session 总数、用户数、最近 7/30 天新增 session 数。
- `.meta.json` 缺失或 summary 版本落后数量。
- 单个 `messages.jsonl` 最大文件大小。
- archive 总数和最大 archive 数。
- 知识文档 record 数、folder 数、原始文件总字节、资源树总字节。
- `temp/upload` 总字节与最老临时文件时间。
- registry 指向不存在资源、资源存在但 registry 缺失、向量索引指向不存在资源的数量。
- Admin index 最后更新时间与 lag。
- daily metrics 最后更新时间与 lag。

## 下一步建议

优先顺序：

1. 增加 Admin 查询路径性能观测和 benchmark fixture。
2. 修复 `ls(node_limit=1000)` 对 Admin user/session listing 的静默截断风险。
3. 设计并实现 `admin_session_index`，先覆盖无搜索列表和 Dashboard daily analytics。
4. 把历史 `.meta.json` backfill 与 index backfill 做成离线任务。
5. 为知识文档和资源补 inventory、checksum、orphan dry-run 与磁盘/inode/temp 监控。
6. 为 `q` 搜索引入消息 FTS/倒排索引。
7. 优化 `append_file()` 与详情页消息分页，处理长会话读写放大。
8. 设计对象存储与 metadata DB 的边界：对象存储承载文档和资源 blob，关系型数据库承载文档/session/index/job/audit 元数据。

最小可落地修复应先让默认 Dashboard 和无搜索会话列表脱离全量文件扫描；搜索可以作为第二阶段单独处理。
