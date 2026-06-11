# VikingBot AgentLoop 拆分设计说明

## 背景

`bot/vikingbot/agent/loop.py` 原始规模超过 3,600 行，已经明显超出单一编排类适合承载的复杂度。问题不只是行数本身，而是多个变化频率不同的职责混在同一个文件中：

- 消息订阅、并发控制、session lock 和主循环编排
- LLM 调用、工具调用、工具结果事件发布
- KB 检索路径、证据选择、coverage 判断
- KB 最终回复拼装、图片证据内联、参考文档链接
- trace 摘要、工具状态摘要、调试日志辅助
- session 持久化与 OpenViking 同步

这会导致后续维护时很难判断一个改动是否只影响 KB 回复、trace 还是主循环状态机，也让测试定位和 review 成本变高。

## 目标

本次采用方案 A：行为保持的渐进式拆分。

目标不是一次性重写 `AgentLoop`，而是先把低耦合、高内聚的辅助逻辑从主文件中移出，让 `loop.py` 回到更清晰的编排角色，同时保持现有私有方法调用兼容。

本阶段目标：

- `loop.py` 降到 3,000 行以下。
- 不改变外部行为和现有测试入口。
- 保留 `AgentLoop._xxx()` / `loop._xxx()` 私有 helper 的解析方式。
- 为后续继续拆主循环、工具执行和 session 持久化留下明确边界。

## 当前模块边界

### `loop.py`

保留核心运行时编排职责：

- `AgentLoop` 初始化、依赖装配、默认工具注册
- inbound message 消费和并发控制
- classic / fast batch agent loop 主流程
- LLM tool call 执行和事件发布
- intent router、系统命令、session 持久化和 OpenViking 同步
- 少量仍被主流程直接使用的通用 helper

拆分后，`loop.py` 从 3,631 行降到约 2,442 行。

### `loop_trace.py`

承接纯 trace 摘要逻辑：

- `_summarize_tool_result_for_trace`
- `_extract_search_entries_for_trace`
- `_extract_viking_uris_for_trace`
- `_extract_text_fragments_for_trace`
- `_truncate_trace_text`
- `_count_tool_messages`
- `_summarize_kb_tool_state`

这部分只负责把工具调用结果转成短日志摘要，不参与主流程决策。

### `kb_evidence.py`

承接 KB 证据选择逻辑：

- Markdown / 导入文档标题识别和分段
- section selection prompt 构造
- section selection / coverage JSON 解析
- 单文档和 fast batch 跨文档证据选择
- openviking_read 工具记录的 concrete evidence 判断
- 已选 evidence prompt 的复用和来源 URI 提取
- evidence block 清洗

这部分仍通过 `AgentLoop` 实例访问 `provider`、`fast_model`、`context` 等运行时依赖，但业务边界已经从主循环中分离。

### `kb_response.py`

承接 KB 最终回复收尾逻辑：

- 图片 evidence block 提取
- 图片 evidence segment 构造和选择
- final answer 中图片证据内联
- 参考文档链接生成
- openviking_read URI 回溯
- 工具调用参数查找

这部分负责“已形成草稿之后如何补图片和参考链接”，不再混在主循环内部。

## 兼容策略

当前拆分使用 mixin，而不是立即把逻辑改成独立 service 对象，原因是现有测试和调试代码会直接调用不少 `AgentLoop` 私有方法。

当前继承关系：

```python
class AgentLoop(LoopTraceMixin, KbEvidenceMixin, KbResponseMixin):
    ...
```

这样可以做到：

- 原有 `AgentLoop._prepare_text_block_for_rewrite(...)` 继续可用。
- 原有 `loop._finalize_kb_response(...)` 继续可用。
- helper 之间通过 `self._xxx()` 或 `cls._xxx()` 保持动态分派。
- 不引入 `kb_evidence.py -> loop.py` 的反向 import，避免循环依赖。

## 迁移原则

后续继续拆分时建议遵循以下顺序：

1. 先拆纯函数或低副作用 helper。
2. 再拆依赖 `provider/context/config` 的垂直业务能力。
3. 最后再考虑拆主循环状态机、工具执行和 session 持久化。

每一阶段都应满足：

- 私有方法兼容性不被破坏，除非同步修改测试和调用点。
- 每次拆分后运行 compile、ruff 和相关单测。
- 不把主循环里的状态变量复制成第二套状态。
- 新模块不反向 import `AgentLoop`。

## 后续建议

### Phase 2：工具执行与事件发布

可以考虑提取：

- `_tool_call_dict`
- `_publish_tool_call_event`
- `_publish_tool_result_event`
- `_execute_fast_batch_tool`
- `_tool_record`
- `_publish_thinking_event`

目标模块可以命名为 `loop_tools.py` 或 `tool_execution.py`。

### Phase 3：session / OpenViking 同步

可以考虑提取：

- session memory scope 解析
- OpenViking memory policy
- pending message sync
- session turn persist
- memory consolidate

目标模块可以命名为 `session_sync.py`。这部分副作用较多，建议在 Phase 2 后再动。

### Phase 4：主循环路径收敛

当前 classic loop 和 fast batch loop 仍在 `loop.py` 中。后续可以先统一它们共享的终止条件、finalize 调用和 trace 记录，再判断是否拆成独立 runner。

这一步风险最高，应放在 helper 拆分稳定之后进行。

## 验证门禁

每个阶段至少运行：

```bash
python3 -m compileall -q bot/vikingbot/agent/loop.py bot/vikingbot/agent/loop_trace.py bot/vikingbot/agent/kb_evidence.py bot/vikingbot/agent/kb_response.py
uv run --no-sync ruff check bot/vikingbot/agent/loop.py bot/vikingbot/agent/loop_trace.py bot/vikingbot/agent/kb_evidence.py bot/vikingbot/agent/kb_response.py
uv run --no-sync pytest tests/bot/test_agent_loop_openviking_reply.py tests/bot/test_kb_prompt_optimization.py tests/bot/test_openapi_stream_events.py
```

在修改主循环、session 持久化或工具执行时，应追加更完整的 bot 相关测试集。
