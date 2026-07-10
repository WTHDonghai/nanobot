# Admin Session Performance 文档集

日期：2026-07-10

状态：Draft

## 目标

本目录维护从 Admin session 慢查询出发、渐进形成可投产架构的完整设计依据。它覆盖当前代码事实、需求、设计、测试、实现切片和历史迁移，但不把单个查询优化等同于整个平台已经 production ready。

平台级最终目标和跨模块阶段见 [OpenViking 生产化路线图](../production-readiness-roadmap.md)。

## 固定业务决策

- 一个酒店对应一个租户账号 `account_id`。
- 酒店前台、技术人员等操作人使用租户下的子账号 `user_id`。
- 保持现有 `ROOT / ADMIN / USER` 角色和权限语义。
- `account_id` 继续隔离知识素材、资源、会话、记忆、反馈、统计、缓存、事件和查询投影。
- 不新增 Organization、Property 或 RoleBinding 模型。
- Session 文件仍是当前事实源；Admin 查询存储是可删除重建的 projection。
- SQLite 用于本地、测试和受控验证；PostgreSQL 是约 8000 酒店租户的生产目标。

## 阅读顺序

| 顺序 | 文档 | 回答的问题 |
| ---: | --- | --- |
| 1 | [现状分析](./01-analysis.md) | 当前慢在哪里，代码和存储事实是什么？ |
| 2 | [需求](./02-requirements.md) | 必须保持哪些业务语义和生产门禁？ |
| 3 | [设计](./03-design.md) | `AdminSessionQuery` module、adapter、projection 和状态如何设计？ |
| 4 | [测试计划](./04-test-plan.md) | 如何证明兼容、隔离、性能、迁移和故障语义？ |
| 5 | [实现计划](./05-implementation-plan.md) | 如何拆成可独立验证和回滚的纵向切片？ |
| 6 | [迁移运行手册](./06-migration-runbook.md) | 历史数据如何 dry-run、rebuild、verify、灰度和回滚？ |

## 实施主线

```text
观测与枚举完整性
  -> AdminSessionQuery interface + contract tests
  -> SQLite adapter 验证 seam/rebuild
  -> PostgreSQL production adapter
  -> 按 account: off -> shadow -> prefer -> required
  -> 多实例、共享内容存储、durable outbox/worker
```

Phase 0-3 可以独立发布，但只完成 Admin 查询纵向切片。面向全部酒店投产仍需满足生产化路线图中的多实例、数据耐久、恢复演练、安全、观测和运维门禁。

## 维护规则

1. 业务语义或范围变化先更新需求，再同步设计和测试计划。
2. Interface、schema、状态机或迁移语义变化必须同时更新设计、测试计划、实现计划和运行手册。
3. 文档中的代码事实必须链接到当前实现；代码迁移后同步修正引用。
4. 性能阈值只能依据保存的 baseline/benchmark 调整，不能无记录放宽。
5. 历史 rebuild 默认只读；`.meta.json` repair 始终作为独立操作维护。
6. 新的重大且难以逆转的决策放入本目录 `adr/`；没有实际 ADR 前不预建空目录。
