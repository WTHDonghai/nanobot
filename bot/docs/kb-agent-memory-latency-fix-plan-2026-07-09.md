# KB Agent Memory 读取延迟修复方案

**日期:** 2026-07-09
**状态:** Implemented
**范围:** `vikingbot` 知识库问答路径中的 Agent Memory 读取去重与超时优化

## 1. 背景

当前 `/guest` 知识库问答的端到端耗时偶发在 14s 左右。近期日志显示，`read_user_profile` 本身耗时约 0.02s 到 0.03s，且 guest 用户 profile 不存在时不会向 prompt 追加内容。

更明显的新增开销来自 `READ_AGENT_MEMORY`：

1. 默认超时为 1200ms。
2. 最近多次读取结果为 `status=timeout`、`result_count=0`、`memory=None`。
3. 部分请求在同一轮内触发两次读取，形成约 2.4s 的纯等待。

这些 memory hints 如果读到内容，只用于辅助检索措辞、可能文档范围和回答结构，不作为用户答案的事实依据。若读取超时或为空，则不会追加上下文，也不会改变检索 query。

## 2. 根因

同一轮消息中存在两个不同的 `ContextBuilder` 实例：

1. `AgentLoop.__init__()` 初始化了共享的 `self.context`。
2. `_process_message()` 每条消息又创建本轮专用的 `message_context`。
3. `message_context.build_messages(...)` 在知识库模式下会先调用 `_build_knowledge_base_agent_memory(...)`。
4. `_run_kb_fast_batch_loop(...)` 后续读取的是 `self.context.agent_memory_read_stats`，不是 `message_context.agent_memory_read_stats`。
5. 当第一次读取超时或为空时，`messages` 中没有 `## Agent memory hints from helpful feedback`，而 `self.context` 仍显示 `status=not_started`，因此 fast batch 会再读一次。

核心问题不是 memory 功能本身，而是本轮 context 状态没有传递到 fast batch 阶段。

## 3. 目标

1. 同一轮知识库问答最多触发一次 Agent Memory 读取。
2. 已读到的 memory hints 能继续用于构造 memory-guided retrieval query。
3. 已经发生的 `empty`、`timeout`、`error` 状态要被 fast batch 日志正确记录。
4. 保持 Agent Memory 失败不阻断主回答路径。
5. 不引入跨会话、跨用户、跨 workspace 的 context 污染。

## 4. 非目标

1. 不改变 feedback-to-memory 的提取逻辑。
2. 不把 Agent Memory 当作事实证据使用。
3. 不调整 KB search、evidence select、answer generation 的核心策略。
4. 不在第一阶段加入长期缓存或复杂重试策略。

## 5. 推荐方案

推荐采用“本轮 ContextBuilder 显式传递”的方式，而不是替换共享的 `self.context`。

### 5.1 接口调整

在 `AgentLoop._run_agent_loop(...)` 增加可选参数：

```python
message_context: ContextBuilder | None = None
```

在进入 fast batch 时继续传递给 `_run_kb_fast_batch_loop(...)`：

```python
context = message_context or self.context
```

然后 fast batch 内部统一使用这个 turn-scoped context：

```python
agent_memory_stats = context.agent_memory_read_stats

if not agent_memory_hints:
    if agent_memory_stats.get("status") == "not_started":
        agent_memory_hints = await context._build_knowledge_base_agent_memory(
            session_key,
            user_request,
        )
```

### 5.2 调用点调整

在 `_process_message()` 中，构造完 `messages` 后调用 `_run_agent_loop(...)` 时传入本轮 context：

```python
final_content, tools_used, token_usage, iteration = await self._run_agent_loop(
    messages=messages,
    session_key=session_key,
    publish_events=True,
    sender_id=msg.sender_id,
    allow_grounded_history_reuse=allow_grounded_history_reuse,
    stream_response_events=should_stream_response,
    message_context=message_context,
)
```

这样第一次读取后的 stats 会被 fast batch 看到：

1. `status=ok` 且 hints 已在 `messages` 中：复用 hints，不重读。
2. `status=empty`：不重读，日志记录 empty。
3. `status=timeout`：不重读，日志记录 timeout。
4. `status=error`：不重读，日志记录 error，主回答继续。
5. `status=not_started`：兼容旧调用或测试入口，fast batch 可以按原逻辑读取一次。

### 5.3 不推荐方案

不建议在 `_process_message()` 里执行：

```python
self.context = message_context
```

原因：

1. `AgentLoop` 是长生命周期对象，`self.context` 是共享状态。
2. 并发请求时可能出现 sender、workspace、memory stats 串话。
3. 会扩大修复面，增加回归风险。

## 6. 超时优化

去重修复后，仍建议把 `agent_memory_read_timeout_ms` 从 1200ms 调整到 500ms 左右。

建议分两阶段：

1. 第一阶段只修去重，观察 `READ_AGENT_MEMORY` 是否从每轮最多两次降到最多一次。
2. 第二阶段把默认值或本地配置降到 500ms，并观察 memory 命中率与端到端耗时。

如果线上 memory backend 稳定后需要提高命中率，可以再调回 800ms 到 1200ms。当前设计下，timeout 只跳过 hints，不影响主回答正确性。

## 7. 风险与副作用

### 7.1 少一次即时重试机会

去重后，如果第一次 memory 读取超时，不会在同一轮立即再读一次。理论上第二次可能成功，因此会少一次获得 hints 的机会。

风险评估：低。Agent Memory 只是检索提示，不是事实来源；当前日志中的第二次读取也持续超时，收益很低。

### 7.2 降低 timeout 会降低 hints 命中率

如果把超时从 1200ms 降到 500ms，慢查询会更容易被跳过。

风险评估：中低。换来的是主回答路径更稳定；需要通过日志监控 `status=ok` 和 `status=timeout` 比例。

### 7.3 错误 telemetry 被误改

之前 memory 查询错误需要被记录为 `status=error`，而不是被吞掉后显示为 `empty`。本次修复必须保留这个语义。

风险评估：中。测试需要覆盖 `empty`、`timeout`、`error` 三种无 hints 场景都不会二次读取，同时日志仍能反映真实状态。

### 7.4 并发隔离

如果误用共享 `self.context` 承载单轮状态，会产生并发污染。

风险评估：中高。通过显式传递 `message_context` 可以规避。

## 8. 测试方案

### 8.1 单元测试

新增或调整 `tests/bot/test_agent_loop_openviking_reply.py`：

1. `message_context.build_messages(...)` 先读到 empty，随后 `_run_agent_loop(..., message_context=message_context)` 不再重读。
2. `message_context.build_messages(...)` 先 timeout，随后 fast batch 不再重读，`KB_TRACE` stats 保持 timeout。
3. `message_context.build_messages(...)` 先 error，随后 fast batch 不再重读，stats 保持 error。
4. 当 `messages` 已包含 `Agent memory hints from helpful feedback` 时，fast batch 复用 hints 构造检索 query。
5. 当没有传入 `message_context` 且 `self.context.status=not_started` 时，保留现有兼容行为，fast batch 仍可读取一次。

可复用现有测试方向：

1. `test_fast_batch_reuses_existing_agent_memory_hints_without_new_read`
2. `test_fast_batch_does_not_retry_after_empty_agent_memory_read`

需要补一个覆盖真实生产调用形态的测试：先用 `message_context` build messages，再把同一个 `message_context` 传给 `_run_agent_loop`。

### 8.2 目标测试命令

```bash
uv run pytest tests/bot/test_agent_loop_openviking_reply.py tests/bot/test_kb_prompt_optimization.py
```

如果涉及类型或 lint：

```bash
uv run ruff check bot/vikingbot/agent/loop.py tests/bot/test_agent_loop_openviking_reply.py
```

### 8.3 现场验证

在本地 `/guest` 连续提问，例如：

1. `夜审`
2. `报表`
3. `向我介绍宾客状态`

查看 `vikingbot.log`：

```text
[READ_AGENT_MEMORY]
[KB_TRACE] ... memory_read_status=...
```

验收标准：

1. 单轮请求最多出现一次 `[READ_AGENT_MEMORY]`。
2. 如果 memory timeout，`KB_TRACE` 显示 `memory_read_status=timeout`，而不是 `not_started`。
3. `memory_hints=no` 时回答仍正常产出。
4. 端到端耗时在原有基础上减少约 1.2s 到 2.4s，具体取决于该轮之前是否发生双读。

## 9. 实施步骤

1. 修改 `_run_agent_loop(...)` 和 `_run_kb_fast_batch_loop(...)` 签名，增加可选 `message_context`。
2. fast batch 内部用 `message_context or self.context` 读取和更新 agent memory stats。
3. `_process_message()` 调用 `_run_agent_loop(...)` 时传入本轮 `message_context`。
4. 补充测试覆盖生产路径的双读回归。
5. 运行目标测试。
6. 本地 `/guest` 做 2 到 3 轮日志验证。
7. 观察确认后，再考虑降低 `agent_memory_read_timeout_ms`。

## 10. 回滚方案

如果发现 hints 复用影响检索质量或出现并发异常：

1. 回滚 `message_context` 传递相关改动。
2. 保留配置层面的 timeout 调整为可选，不作为代码默认变更。
3. 根据日志对比 `memory_hints=yes/no`、search query 和最终回答质量，再决定是否改为更窄的 stats-only 传递方案。

## 11. 后续可选优化

1. 为 timeout 增加很短 TTL 的 skip cache，例如 30s 到 60s，仅缓存 timeout，不缓存 error。
2. 将 Agent Memory 读取改为后台预热，不阻塞首 token。
3. 对 memory backend 增加独立健康指标：p50/p95、timeout ratio、error ratio、result count。
4. 在 admin memories 页面展示 memory lookup latency，辅助判断是查询慢还是没有可用记忆。
