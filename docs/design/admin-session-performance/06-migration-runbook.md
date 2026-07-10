# Admin Session Query Projection Migration Runbook / Admin 会话查询投影迁移运行手册

日期：2026-07-10

状态：Draft，命令为目标 interface，需在实现阶段落地

相关文档：

- [需求](./02-requirements.md)
- [设计](./03-design.md)
- [测试计划](./04-test-plan.md)
- [实现计划](./05-implementation-plan.md)
- [生产化路线图](../production-readiness-roadmap.md)

## 1. 目的

指导平台运维把一个或多个酒店租户的历史 session summary 从当前 AGFS/VikingFS 事实源投影到 Admin 查询存储，并通过 dry-run、rebuild、verify、shadow、prefer 和 required 完成可回滚切换。

本 Runbook 只迁移 Admin 查询派生数据。原始 messages、archive、feedback 和知识素材不因本流程搬迁。

## 2. 固定原则

1. `account_id` 是迁移、状态、校验和切换的最小单位。
2. 先启用在线 projection，再扫描历史数据。
3. Index rebuild 默认只读，不修改 `.meta.json`。
4. `.meta.json` repair 是独立命令和变更窗口。
5. 无法证明完整时不能把租户标记为 `ready`。
6. 事实源写入优先于派生层；派生失败不能回滚前台数据。
7. 恢复旧事实源备份后必须 full rebuild，不能与未来 index 增量合并。
8. 所有生产切换先 canary，后分批扩展。

## 3. 目标运维命令

实现阶段提供统一入口：

```bash
python scripts/admin_session_index.py status --account <account_id>
python scripts/admin_session_index.py inventory --account <account_id>
python scripts/admin_session_index.py rebuild --account <account_id> --dry-run
python scripts/admin_session_index.py rebuild --account <account_id> --resume
python scripts/admin_session_index.py verify --account <account_id>
python scripts/admin_session_index.py repair-meta --account <account_id> --dry-run
python scripts/admin_session_index.py repair-meta --account <account_id> --resume
python scripts/admin_session_index.py set-mode --account <account_id> --mode shadow
python scripts/admin_session_index.py rollback --account <account_id> --to-mode off
```

生产部署可以把脚本实现替换为受鉴权的 job/管理入口，但操作语义和输出字段必须一致。

## 4. 迁移对象

### 4.1 事实源

```text
viking://session/{user_id}/{session_id}/
  .meta.json
  messages.jsonl
  feedback.json
  history/archive_NNN/messages.jsonl
```

物理路径继续由 `RequestContext.account_id` 映射到租户目录。

### 4.2 派生目标

- `admin_session_index`
- `admin_session_tombstone`
- `admin_session_index_state`
- rebuild checkpoint/generation
- repair pending/error report

生产目标是 PostgreSQL；SQLite 仅用于本地和受控验证。

### 4.3 不在本流程中的数据

- 知识库资源正文。
- VectorDB 内容。
- API Key/子账号注册表迁移。
- Message full-text index。
- Object Storage 迁移。

### 4.4 历史数据边界矩阵

Dry-run 和 verify 必须逐项覆盖以下边界，不能只比较总 row 数：

| 边界 | 处理原则 |
| --- | --- |
| 不同租户存在相同 `user_id/session_id` | 以 `(account_id, user_id, session_id)` 隔离；交叉覆盖立即阻断迁移。 |
| 子账号已删除但 session 仍存在 | 保留历史 session 供租户 ADMIN 审计；不因身份缺失丢弃。 |
| 空 session、缺失 `.meta.json`、旧 summary version | 规范化为空/legacy；必要时读取 raw data 后只写 projection。 |
| 损坏 JSON、截断 JSONL、缺失 archive | 进入 quarantine/error report；未获明确处置前不能标记 `ready`。 |
| naive、非法、未来或极旧 timestamp | 使用现有兼容解析规则；非法值计数并抽样验证，不能让数据库 NULL 排序悄然改变结果。 |
| 重复或悬空 feedback/message id | 按当前 `Session` 归一化语义计算并记录异常计数，不在迁移中发明新业务规则。 |
| 超过 backend 默认 node limit | 必须证明完整分页/continuation；完整性未知即失败。 |
| rebuild 同时 add/update/delete/recreate | 在线 projection 优先；delete 即使无 row 也写 tombstone；未完整扫描不 sweep。 |
| 任务崩溃、重复执行、schema 部署中断 | 使用 generation/checkpoint 幂等 resume；旧代码和新 schema 必须在兼容窗口共存。 |
| 事实源从更早备份恢复 | 隔离现有 projection/tombstone/checkpoint 后 full rebuild，不做普通增量合并。 |
| 单租户数据异常大或小文件极多 | 按租户限速、暂停和拆批；不得因一个热点租户阻塞其他批次。 |
| 数据保留、擦除或 legal hold 要求 | 不由普通 backfill 推断；进入独立、可审计的 retention/purge workflow。 |

## 5. 状态与允许操作

| 状态 | 允许行为 | 禁止行为 |
| --- | --- | --- |
| `disabled` | inventory、dry-run、启用 projection | index 读流量 |
| `building` | projection、rebuild、resume、verify | 切 required |
| `ready` | shadow/prefer/required、增量 projection | 无验证 schema 变更 |
| `stale` | repair、rebuild、prefer fallback | required 返回旧数据 |
| `error` | status、诊断、rollback、full rebuild | 宣称数据完整 |

## 6. 上线前准备

### 6.1 代码和配置

- 部署包含 projection hook、query adapter、status 和 migration CLI 的兼容版本。
- 所有目标账号初始 `read_mode=off`。
- PostgreSQL migration 已在 staging 完成演练。
- DB 用户权限区分 runtime、migration 和只读诊断。
- Query timeout、lock timeout、pool size 和 statement logging 已配置。

### 6.2 备份

- 备份/快照 session 事实源。
- PostgreSQL 开启 PITR 或等价恢复能力。
- 记录部署版本、schema version、配置和迁移工具版本。
- SQLite 验证环境保留复制件，不在唯一生产 workspace 上直接试验破坏性恢复。

### 6.3 容量

Inventory 至少输出：

- user count。
- session count。
- current/legacy/missing/corrupt meta count。
- raw message/archive file count。
- estimated bytes to read。
- target index estimated rows/bytes。
- 最大单用户 session 数。
- `node_limit`/枚举完整性能力。

PostgreSQL 还需确认：

- 表和索引空间。
- WAL 增长预算。
- connection pool 余量。
- backup/PITR 空间。

### 6.4 业务窗口

- 迁移默认在线执行，但必须设置每租户 batch 和 I/O 限速。
- 最大历史租户优先在低峰演练。
- Shadow 查询使用采样，不能让每个 Admin 请求同时执行两条重查询。
- 明确当班 owner、观察窗口和 rollback 权限。

## 7. Canary 选择

第一批至少覆盖：

1. 内部测试租户。
2. 小数据量、无 legacy 的酒店。
3. 含 legacy summary 的酒店。
4. 有 archive、feedback、tool failure 的酒店。
5. 超过 1000 session 的酒店。
6. 当前历史数据最大的酒店。

不能只选择最干净的小租户后直接全量发布。

## 8. 标准迁移流程

### Step 1：记录初始状态

```bash
python scripts/admin_session_index.py status --account <account_id>
python scripts/admin_session_index.py inventory --account <account_id>
```

保存输出到发布记录，确认目标租户、事实源路径和 adapter。

### Step 2：Dry-run

```bash
python scripts/admin_session_index.py rebuild \
  --account <account_id> \
  --dry-run
```

通过条件：

- 枚举完整性为 `complete`。
- source session count 明确。
- corrupt/unknown item 为 0，或已有逐项处置决定。
- 预计时间、读取字节和 DB 空间在窗口预算内。
- 未修改 `.meta.json` 和 query DB 业务 row。

### Step 3：先启用在线 projection

把租户状态设为 `building`，记录新的 generation，然后启用该租户在线 projection。

验证：

- 新建 session 进入新 generation。
- 新消息/feedback/tool status 能更新 row。
- projection failure 产生 repair pending，但前台事实写入仍成功。

### Step 4：执行 read-only rebuild

```bash
python scripts/admin_session_index.py rebuild \
  --account <account_id> \
  --resume \
  --batch-size <approved_size> \
  --max-read-mbps <approved_limit>
```

Rebuild 必须：

- 按稳定 `(user_id, session_id)` 顺序扫描。
- 每批事务后保存 checkpoint。
- 使用 conditional upsert，不能覆盖更晚在线 projection。
- 对 delete 保留 tombstone。
- 不默认写回 `.meta.json`。
- 被取消或崩溃后保持 `building`，不执行最终 sweep。

### Step 5：Verify

```bash
python scripts/admin_session_index.py verify --account <account_id>
```

验证层级：

1. Source 与 index count。
2. 全量 key set 或可证明完整的分片 key count。
3. 抽样/全量 summary fingerprint。
4. 分日期、用户、反馈、token、tool failure aggregate。
5. 排序和分页稳定性。
6. Tombstone、repair pending 和 error report。

通过后原子更新 state 为 `ready`。任何未知缺口都不能标记 ready。

### Step 6：Shadow

```bash
python scripts/admin_session_index.py set-mode \
  --account <account_id> \
  --mode shadow
```

Shadow response 仍来自 filesystem。按批准采样率比较：

- total/total_pages。
- ordered session keys。
- typed count/token/feedback fields。
- daily rows/totals/timezone。
- error/empty behavior。

观察至少覆盖酒店的正常 Admin 使用周期和高峰时段。

### Step 7：Prefer

```bash
python scripts/admin_session_index.py set-mode \
  --account <account_id> \
  --mode prefer
```

观察：

- query source 应主要为 `index`。
- fallback 原因必须为零或已解释。
- p50/p95/p99、DB wait、pool saturation。
- projection lag、repair pending、shadow mismatch。
- Dashboard/Sessions 错误与 stale UI。

### Step 8：Required

在一个稳定发布周期后：

```bash
python scripts/admin_session_index.py set-mode \
  --account <account_id> \
  --mode required
```

Required 代表默认 list/daily 正式脱离 filesystem scan。索引不可用时返回 503，不能静默全量扫描或返回未知陈旧结果。

## 9. Legacy Meta Repair

Index rebuild 与 meta repair 必须分开。

### 9.1 Dry-run

```bash
python scripts/admin_session_index.py repair-meta \
  --account <account_id> \
  --dry-run
```

输出：待升级 session、raw bytes、archive count、预计写入数、错误和旧版本兼容检查。

### 9.2 执行条件

- 新字段对当前和允许回滚的旧版本 reader 向后兼容。
- 事实源已有备份/快照。
- Repair 使用幂等版本检查。
- 每个 session 写入后验证 JSON 和 fingerprint。
- Repair 失败不影响已经 ready 的 query index；记录独立 repair status。

## 10. 特殊生命周期

### 10.1 删除单个 session

事实源删除成功后写 tombstone。若 projection 失败，租户进入 stale/repair pending，由 reconciliation 修复。

### 10.2 删除子账号

保持现状：撤销子账号身份，不默认删除历史 session。Index row 继续保留供租户 ADMIN 审计。

如果收到数据擦除要求，执行独立 purge job，不复用普通 `remove_user`。

### 10.3 删除整个租户账号

账号事实源删除流程必须同时：

- purge `admin_session_index` rows。
- 删除 index state/checkpoint/repair item。
- 删除 search/daily 等派生数据。
- 记录审计结果和失败补偿任务。

当前账号删除直接清理多个 account scope，不能只依赖单 session tombstone hook。

### 10.4 恢复旧备份

恢复事实源后：

1. 立即把租户读模式切 `off` 或维护状态。
2. 隔离/删除该租户当前 projection row 和 checkpoint。
3. 新建 generation，执行 full rebuild。
4. verify 后重新 shadow/prefer/required。

旧备份时间戳可能小于现有 index，不允许普通 conditional upsert 保留“未来数据”。

## 11. 故障处置

### 11.1 枚举被截断或完整性未知

- 停止 rebuild。
- 保持 `building/error`。
- 不执行 sweep。
- 修复 backend 枚举能力后从 checkpoint 或新 generation 重跑。

### 11.2 损坏 `.meta.json`

- Read-only rebuild 可以读取 raw messages 计算 index summary。
- 记录 session identity、错误码和 fingerprint，不记录正文。
- 是否 repair meta 由独立操作决定。

### 11.3 损坏 raw messages/archive

- 不静默跳过坏行并宣称完整。
- 将 session 置为 quarantined/error item。
- 默认阻止租户进入 required，直到修复或有明确的数据丢失批准。

### 11.4 PostgreSQL 不可用

- 前台 session 事实写入按设计继续。
- Projection 进入 repair pending/outbox retry。
- `prefer` 显式 fallback；`required` 返回 503。
- 不允许无限同步等待 DB 阻塞前台路径。

### 11.5 Projection lag 持续增长

- 暂停扩大租户批次。
- 检查 DB pool、lock、worker backlog 和单租户热点。
- 必要时将受影响租户回到 shadow/off。
- 清空 backlog 前不切 required。

### 11.6 Shadow mismatch

- 保存请求参数、identity key、字段差异和 fingerprint。
- 禁止记录 raw message。
- 维持 shadow，不切 prefer。
- 修复后从相同 fixture 和生产采样重验。

## 12. 回滚

### 12.1 从 Prefer/Required 回滚读路径

```bash
python scripts/admin_session_index.py rollback \
  --account <account_id> \
  --to-mode off
```

- 只切换读模式。
- 不删除事实源。
- 保留 projection 和错误现场供诊断。
- 若 fallback filesystem 可能截断，UI/API 必须显式 partial/error。

### 12.2 回滚 adapter 部署

- 新 schema 必须保持旧代码可忽略。
- 先回滚读路径，再回滚应用实例。
- 不在应用 rollback 中自动 drop DB schema。
- Schema rollback 使用单独 migration 和备份恢复决策。

### 12.3 放弃并重建派生层

当 index 损坏或版本不可恢复时：

1. 切 `off`。
2. 隔离旧 DB/table/generation。
3. 初始化空派生层。
4. 从事实源 full rebuild。
5. verify 后重新切换。

## 13. 批次发布记录

每批保存：

- 应用 commit、schema version、adapter。
- account list 和数据规模。
- dry-run/inventory 结果。
- rebuild 时间、读取字节、error/quarantine。
- verify 输出和 shadow mismatch。
- prefer/required 切换时间。
- query SLI、projection lag 和 fallback。
- owner、批准人和 rollback point。

## 14. 完成检查表

- [ ] 目标租户与子账号语义确认。
- [ ] 事实源备份和 DB PITR 可用。
- [ ] Inventory/dry-run 完整。
- [ ] 在线 projection 先于 backfill 启用。
- [ ] Rebuild 无静默截断。
- [ ] Verify count/key/fingerprint/aggregate 通过。
- [ ] Shadow 无未解释 mismatch。
- [ ] Prefer 观察窗口通过。
- [ ] Required 故障语义演练通过。
- [ ] 账号删除和旧备份恢复演练通过。
- [ ] Rollback 命令和权限验证。
- [ ] 发布记录归档。
