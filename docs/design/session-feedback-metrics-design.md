# 会话反馈指标设计文档

## 当前状态

会话反馈按 assistant message 存储为 `up` 或 `down`。会话元数据会在 `feedback_summary` 中缓存原始反馈计数，Admin analytics 再将这些计数和消息计数一起聚合。

当前相关字段：

- `assistant_message_count`
- `feedback_count`
- `positive_feedback_count`
- `negative_feedback_count`

Admin UI 当前只计算一个满意度指标：

```text
positive_feedback_count / feedback_count
```

## 设计决策

后端继续作为原始计数事实源，四个面向用户的指标在 Admin UI 边界派生。

原因：

- 现有历史会话已经具备所需原始字段。
- 不需要做历史数据迁移。
- 这些公式属于展示语义，不是新的事实。
- 保留原始计数可以更清楚地解释样本量限制。
- API 对已有客户端保持向后兼容。

## 指标模型

新增一个前端 helper，输入对象包含以下字段：

```ts
type FeedbackMetricSource = {
  assistant_message_count?: number;
  feedback_count?: number;
  positive_feedback_count?: number;
  negative_feedback_count?: number;
};
```

helper 返回稳定结构：

```ts
type FeedbackMetrics = {
  explicitPositiveRate: number | null;
  feedbackCoverageRate: number | null;
  negativeFeedbackRate: number | null;
  appreciationRate: number | null;
};
```

`null` 表示分母不可用，UI 应渲染 `-`。`0` 表示分母存在，但观察到的比例为零。

## 公式规则

```text
explicitPositiveRate = feedback_count > 0
  ? positive_feedback_count / feedback_count
  : null

feedbackCoverageRate = assistant_message_count > 0
  ? feedback_count / assistant_message_count
  : null

negativeFeedbackRate = assistant_message_count > 0
  ? negative_feedback_count / assistant_message_count
  : null

appreciationRate = assistant_message_count > 0
  ? positive_feedback_count / assistant_message_count
  : null
```

## Admin Dashboard 用户体验

Dashboard 应继续保持运营工具的风格：密集、安静、便于扫描。

推荐布局：

1. 顶部 overview KPI 行继续聚焦总量：
   - 服务用户
   - 承接问题
   - 自动响应
   - 显式好评率
2. 在 overview 行附近新增或复用一个紧凑质量区，展示其余质量指标：
   - 反馈覆盖率
   - 倒赞率
   - 赞赏率
3. 使用说明文字暴露样本量：
   - 显式好评率：`8 / 10 条反馈为赞`
   - 反馈覆盖率：`10 / 100 条回复有反馈`
   - 倒赞率：`2 / 100 条回复收到倒赞`
   - 赞赏率：`8 / 100 条回复收到赞`
4. 保留钻取链接：
   - 显式好评率和反馈覆盖率跳转到按 `feedback_count` 排序的会话列表。
   - 倒赞率跳转到按 `negative_feedback_count` 排序的会话列表。
   - 赞赏率跳转到按 `positive_feedback_count` 排序的会话列表。

语义色建议：

- `negative_feedback_count > 0` 时，倒赞率使用 warning 语义。
- 有反馈且没有倒赞时，显式好评率可以使用 success 语义。
- 反馈覆盖率保持 neutral。覆盖率低不一定是坏事，但提示显式反馈样本较小。
- 赞赏率仅在分子大于零时使用正向语义。

## Admin Sessions 用户体验

会话列表反馈列：

- 将主百分比从“满意率”改为“显式好评率”。
- 主百分比必须带“显式好评”标签，避免在按倒赞排序时裸露 `0%` 造成误读。
- 展示样本量，例如 `覆盖 3 / 4 回复`。
- `negative_feedback_count > 0` 时继续展示 warning 语义的提示标签。
- 没有反馈时显示 `暂无反馈` 或 `-`。

会话详情指标：

- 将“满意率”替换为“显式好评率”。
- 如果详情网格空间足够，增加“反馈覆盖率”。
- 保留“Agent 回复”，因为它解释了覆盖率、倒赞率和赞赏率的分母。

## API 与存储影响

后端存储不需要变更。

API 响应结构不需要变更，因为所有公式都可从现有字段派生。本阶段指标只在 Admin 前端派生和展示，不为非 Admin 客户端增加派生指标 API，也不向普通用户暴露这些运维指标。

如果未来有 Admin 以外的运维客户端需要复用同一口径，可以再评估 Admin API 层派生字段，例如：

- 在 `feedback_metrics` 这类嵌套对象下增加派生指标字段。
- 保留原始计数字段。
- 不用派生指标替换现有计数字段。

## 向后兼容

- 现有 `feedback_summary` 继续有效。
- 断言原始计数的现有测试应继续通过。
- 现有排序字段继续有效。
- 没有反馈的旧会话继续在显式好评率上显示 `-`；如果存在 assistant 回复，反馈覆盖率显示 `0%`。

## 实现草案

1. 在现有 dashboard/session 格式化 helper 附近增加一个小型前端 helper；如果两个页面都需要，则提取为共享 admin metrics helper。
2. 将 `satisfactionRate` 使用点替换为 `feedbackMetrics`。
3. 更新 Dashboard 卡片和说明文字。
4. 更新 Sessions 列表和详情里的标签与紧凑样本量文案。
5. 如果项目已有前端测试基础设施，增加聚焦的前端测试；否则用轻量 helper 测试，或用后端/API 测试锁定 UI 依赖的原始计数。

## 后续工作

后续会单独设计隐式转化率 / 隐式完成率，但必须和显式反馈指标分开命名。候选信号包括：

- 没有倒赞。
- 没有转人工请求。
- 没有重复未解决追问。
- 会话自然结束。
- 短时间内没有重新打开相似问题。
- 用户完成了目标动作。

该指标应命名为“隐式转化率”“隐式完成率”或“无负反馈完成率”，不应替代显式好评率，也不得简单等同于“沉默 = 成功”。
