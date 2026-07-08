# 会话反馈指标测试计划

## 目的

本文档记录将含糊的满意度指标替换为四个显式反馈指标时的验收检查。

目标是在编码开始前，让统计语义可以被测试和验收。

## 公式验收用例

### 混合反馈

给定：

- `assistant_message_count = 100`
- `feedback_count = 10`
- `positive_feedback_count = 5`
- `negative_feedback_count = 5`

期望：

- 显式好评率：`50%`
- 反馈覆盖率：`10%`
- 倒赞率：`5%`
- 赞赏率：`5%`

### 只有正向反馈

给定：

- `assistant_message_count = 20`
- `feedback_count = 4`
- `positive_feedback_count = 4`
- `negative_feedback_count = 0`

期望：

- 显式好评率：`100%`
- 反馈覆盖率：`20%`
- 倒赞率：`0%`
- 赞赏率：`20%`

### 只有负向反馈

给定：

- `assistant_message_count = 20`
- `feedback_count = 4`
- `positive_feedback_count = 0`
- `negative_feedback_count = 4`

期望：

- 显式好评率：`0%`
- 反馈覆盖率：`20%`
- 倒赞率：`20%`
- 赞赏率：`0%`

### 有回复但全沉默

给定：

- `assistant_message_count = 100`
- `feedback_count = 0`
- `positive_feedback_count = 0`
- `negative_feedback_count = 0`

期望：

- 显式好评率：`-`
- 反馈覆盖率：`0%`
- 倒赞率：`0%`
- 赞赏率：`0%`

### 没有 assistant 回复

给定：

- `assistant_message_count = 0`
- `feedback_count = 0`
- `positive_feedback_count = 0`
- `negative_feedback_count = 0`

期望：

- 显式好评率：`-`
- 反馈覆盖率：`-`
- 倒赞率：`-`
- 赞赏率：`-`

## 后端/API 检查

后端测试应继续断言原始计数：

- 会话详情包含 `assistant_message_count`。
- 会话详情包含 `feedback_count`、`positive_feedback_count` 和 `negative_feedback_count`。
- Daily analytics totals 包含同样字段。
- 一个包含一条 assistant 回复和一个倒赞的会话应聚合为：
  - `assistant_message_count = 1`
  - `feedback_count = 1`
  - `positive_feedback_count = 0`
  - `negative_feedback_count = 1`
- 清除反馈后，所有反馈计数回到零。

任何后端测试都不应断言沉默等于正向反馈。

## 前端检查

Dashboard 页面：

- 顶部不再使用“满意率”表示 `positive_feedback_count / feedback_count`。
- 显式好评率标签为“显式好评率”。
- 反馈覆盖率标签为“反馈覆盖率”。
- 负向反馈率标签为“倒赞率”。
- 赞赏率标签为“赞赏率”。
- 说明文字包含分子和分母计数。
- `feedback_count == 0` 时，显式好评率渲染为 `-`。
- `assistant_message_count > 0` 且 `feedback_count == 0` 时，反馈覆盖率渲染为 `0%`。
- 反馈覆盖率、倒赞率、赞赏率位于首屏可见的次级质量区，而不是隐藏在设置或 tooltip 中。

Sessions 列表：

- 反馈列不暗示沉默回复已经满意。
- 主百分比需要标明“显式好评”，不能只显示裸百分比。
- 没有反馈的行渲染为 `-` 或 `暂无反馈`。
- 有倒赞的行除了颜色外，还展示 warning 文本或提示标签。

Session 详情：

- 详情网格使用“显式好评率”，不再使用“满意率”。
- 详情视图暴露足够的分母上下文，可以通过指标说明文字，或通过相邻的 `Agent 回复` 和反馈计数说明。

权限与可见性：

- 普通用户端不展示这些运维指标。
- 非 Admin 客户端不要求新增派生指标 API。
- 后续隐式转化率应单独验收，不得复用显式好评率测试结论。

## 手动 QA

使用小型 fixture 或种子账号构造这些会话：

| 会话 | Assistant 回复数 | 赞 | 倒赞 | 验收重点 |
| --- | ---: | ---: | ---: | --- |
| A | 100 | 5 | 5 | 混合反馈样本限制。 |
| B | 20 | 4 | 0 | 只有正向反馈。 |
| C | 20 | 0 | 4 | 负向风险清晰可见。 |
| D | 100 | 0 | 0 | 沉默不被展示为满意。 |
| E | 0 | 0 | 0 | 分母为空时显示 `-`。 |

在桌面和移动宽度下验证：

- 指标标签不重叠。
- 百分比和说明文字保持可读。
- warning/success 状态不只依赖颜色表达。
- 钻取链接仍然跳转到对应排序的会话列表。

## 编码门禁

合并实现前运行：

```bash
uv run pytest tests/server/test_api_sessions.py tests/server/test_admin_api.py
npm run build --prefix admin
```

如果后续为指标 helper 增加前端单元测试，也应将对应命令加入本门禁。
