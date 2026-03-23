# OpenViking 图片回复链路改动 Review

## 1. 背景

这批改动的目标很明确：

1. 让 `DOCX` 里的内嵌图片在解析阶段被抽取出来，而不是只保留文本。
2. 让 `openviking_read(level="read")` 在读取文档时，尽量返回可直接发送到聊天渠道的图片引用。
3. 让 Agent 在最终回答阶段优先保留“步骤 + 对应图片”的结构，而不是把图片信息打散。
4. 让钉钉渠道在真正发送前，把 `send://...` 图片引用上传成 `media_id`。

从整体设计上看，这是一条完整的链路改造，覆盖了解析、资源读取、回答生成、渠道发送四个阶段，方向是合理的。

## 2. 这批改动做了什么

### 2.1 Word 解析器

文件：`openviking/parse/parsers/word.py`

核心变化：

1. `WordParser.parse()` 不再只是把 DOCX 转成纯 Markdown。
2. 解析时会为图片建立 `partname -> filename` 映射。
3. 段落转换改成按 run 顺序拼装，图片会以 `![alt](ov-asset://filename)` 的占位形式插入到文本中。
4. DOCX zip 包中的 `word/media/*` 会被抽出来，写入解析结果目录下的 `_images/`。
5. 解析结果 `meta` 里会记录 `embedded_image_count` 和 `embedded_images_dir`。

带来的效果：

1. 文档中的“文字在前、图片在中间、文字在后”的顺序可以保留下来。
2. 后续读取链路可以依据 `ov-asset://` 占位符把图片还原成可发送引用。

### 2.2 OpenViking 客户端能力

文件：`bot/vikingbot/openviking_mount/ov_server.py`

新增能力：

1. `stat()`：补齐资源状态查询。
2. `download_content()`：下载二进制内容，用于图片导出。
3. `export_uri_for_send()`：把图片文件或 `_images/` 目录导出成 `send://...` Markdown 图片行。
4. `resolve_read_uri()`：当用户对目录执行 `level="read"` 时，尝试落到某个具体文本叶子。
5. `materialize_inline_image_refs()`：把 `ov-asset://...` 占位符替换成 `send://...`。
6. `export_related_images_for_send()`：在当前文本节点附近寻找 `_images/` 目录并导出图片。

这部分是整条图片发送链路的中枢。

### 2.3 Bot 文件工具

文件：`bot/vikingbot/agent/tools/ov_file.py`

核心变化：

1. `openviking_read` 新增了 `include_images` 和 `max_images`。
2. 对图片资源本身，`level="read"` 会直接返回可发送的 Markdown 图片行。
3. 对目录资源，工具不再直接读目录，而是先尝试解析到某个正文叶子。
4. 对普通文本资源，会优先把正文中的 `ov-asset://` 替换成 `send://`。
5. 如果正文里没有内联图片，再兜底补充“相关图片”列表。

### 2.4 Agent 最终回复重写

文件：`bot/vikingbot/agent/loop.py`

核心变化：

1. 从用户问题里抽取检索词。
2. 把 Markdown 内容按标题切成 section。
3. 优先挑出一个包含 `send://` 的相关 section。
4. 把 section 解析成“引言 + 步骤 + 图片 segment”。
5. 在最终回答阶段，用更自然的方式输出整理后的内容，同时避免暴露内部检索措辞。

这部分的目标不是“重新生成内容”，而是“保留证据结构，改好呈现方式”。

### 2.5 DingTalk 渠道

文件：`bot/vikingbot/channels/dingtalk.py`

核心变化：

1. 新增 `_upload_image_to_dingtalk()`，把图片字节上传成钉钉 `media_id`。
2. 新增 `_replace_inline_images()`，把消息里的 `send://...` 或 Markdown 图片引用改写成钉钉可发送格式。
3. `send()` 在真正发消息前先做图片替换。

这意味着 Agent 最终回复里只需要保留 `send://...`，渠道层负责适配平台。

### 2.6 Prompt 与文档

文件：

1. `bot/workspace/SOUL.md`
2. `bot/workspace/TOOLS.md`
3. `bot/workspace/USER.md`
4. `docs/en/api/02-resources.md`
5. `docs/zh/api/02-resources.md`

这些改动主要是在提示词里强化两件事：

1. 先检索，再回答，不要把搜索结果直接当最终证据。
2. 如果拿到了可发送图片，就尽量把图片和步骤绑定起来一起回复。

## 3. 端到端链路

本次改动后的典型路径如下：

1. DOCX 被 `WordParser` 解析成 Markdown，并把内嵌图片落到 `_images/`。
2. 文本中的图片位置保留为 `ov-asset://...` 占位符。
3. Agent 调用 `openviking_search` 找到目标资源。
4. Agent 调用 `openviking_read(level="read", include_images=true)` 读取正文。
5. `VikingReadTool` 优先把内联图片物化成 `send://...`。
6. `AgentLoop` 从工具结果里抽取最相关 section，并按步骤重排。
7. 渠道层把 `send://...` 上传成平台可识别的图片对象。
8. 用户最终看到的是“自然语言说明 + 原位图片”。

整体上，这条链路已经闭环。

## 4. Review 结论

### 4.1 [P1] 多意图问题仍然只支持单个 section，和当前测试意图不一致

涉及文件：

1. `bot/vikingbot/agent/loop.py:279`
2. `bot/vikingbot/agent/loop.py:306`
3. `bot/vikingbot/agent/loop.py:489`
4. `tests/bot/test_agent_loop_openviking_reply.py:74`

问题说明：

1. `_select_relevant_markdown_section()` 只会返回一个最佳 section。
2. `_select_relevant_openviking_section()` 也只接受“唯一且明确的一个 section”。
3. `_build_rewritten_openviking_reply()` 只消费一个 `best_section`。
4. 但新增测试已经开始覆盖“安装 + 安装后设置”这种多 section 问题，并直接调用了并不存在的 `_select_relevant_openviking_sections()`。

实际影响：

1. 用户一次提两个相关问题时，当前实现最多只能重写其中一个 section。
2. 更糟的是，测试已经表达了“需要支持多个 section”的预期，但实现还没有跟上。
3. 一旦把这些测试纳入执行，这部分会直接失败。

建议：

1. 把 section 选择改成“按 draft reply / query terms 选择多个 section”。
2. `rewrite` 阶段按 section 列表依次渲染，而不是只渲染一个 `best_section`。
3. 补上真正存在的多 section helper，并让测试与实现对齐。

### 4.2 [P2] 单张内联图片解析失败会让整篇文档读取失败

涉及文件：

1. `bot/vikingbot/openviking_mount/ov_server.py:240`
2. `bot/vikingbot/agent/tools/ov_file.py:114`
3. `bot/vikingbot/agent/tools/ov_file.py:130`

问题说明：

1. `materialize_inline_image_refs()` 对任意一个无法解析的 `ov-asset://...` 都会直接抛异常。
2. `VikingReadTool.execute()` 没有对这一步做降级处理，而是把异常变成整条 `"Error reading from Viking: ..."`。
3. 结果是，只要某一张图片缺失、导出失败，整篇正文都返回不了，连文本也拿不到。

实际影响：

1. 文档整体可读，但图片目录局部损坏时，用户会得到“整篇读取失败”。
2. Agent 失去正文证据，无法继续回答。
3. 这类失败在 DOCX 导入链路上并不罕见，因为图片提取、目录落盘、后续重命名任何一步出问题都会触发。

建议：

1. 对单张图片失败做 best-effort 降级，保留正文文本。
2. 可以把失败图片保留成原始占位符，或记录 warning 后继续处理其他图片。
3. 只有正文本身不可读时才整体失败。

### 4.3 [P2] 新增测试当前仍是未跟踪文件，实际不会进入提交产物

涉及文件：

1. `tests/bot/test_ov_server.py`
2. `tests/bot/test_agent_loop_openviking_reply.py`
3. `tests/bot/test_ov_file_tool.py`
4. `tests/bot/test_dingtalk_channel.py`
5. `tests/parse/test_word_parser.py`

问题说明：

`git status --short` 显示上述测试文件目前都是 `??`，也就是未跟踪状态。

实际影响：

1. 如果这批改动直接提交而没有 `git add` 这些测试，CI 根本不会执行到它们。
2. 当前这次改动最关键的新行为，实际上没有被版本库正式覆盖。

建议：

1. 在确认测试内容后，把它们纳入版本控制。
2. 如果其中有尚未完成的用例，至少先拆成最小可运行集，避免把“计划中的测试”误当成“已接入的测试”。

## 5. 其他值得关注的点

### 5.1 容错后的错误字符串可能会被 Agent 当成成功工具结果

文件：`bot/vikingbot/agent/loop.py:664`

当前 `execute_success` 的判断仍然只检查结果里是否包含 `"Error executing"`。这意味着：

1. `openviking_read` 返回 `"Error reading from Viking: ..."` 时，`execute_success` 仍会被记成成功。
2. 新增图片链路之后，`openviking_read` 的失败面被放大了，这个旧判断会更容易误导后续分析。

这不是本次 diff 新引入的逻辑，但它会放大本次新增失败路径的影响。

## 6. 验证情况

已完成：

1. `python3 -m py_compile` 校验以下文件语法通过：
   `bot/vikingbot/agent/loop.py`
   `bot/vikingbot/agent/tools/ov_file.py`
   `bot/vikingbot/channels/dingtalk.py`
   `bot/vikingbot/openviking_mount/ov_server.py`
   `openviking/parse/parsers/word.py`
   `tests/bot/test_ov_server.py`
   `tests/bot/test_agent_loop_openviking_reply.py`
   `tests/bot/test_ov_file_tool.py`
   `tests/bot/test_dingtalk_channel.py`
   `tests/parse/test_word_parser.py`

未完成：

1. `uv run pytest tests/bot/test_ov_server.py tests/bot/test_agent_loop_openviking_reply.py tests/bot/test_ov_file_tool.py tests/bot/test_dingtalk_channel.py tests/parse/test_word_parser.py`
2. 失败原因：当前环境缺少 `pytest_asyncio`，pytest 在加载 `tests/conftest.py` 时中断。

## 7. 总结

这次改动的主方向是对的，而且链路设计已经接近完整可用：

1. 解析阶段保住图片位置。
2. 读取阶段把图片变成可发送引用。
3. 回复阶段尽量保住步骤和图片的对应关系。
4. 渠道阶段做平台适配。

当前最值得优先修的有两件事：

1. 把单 section 的回答重写扩展成多 section。
2. 把“图片失败导致整篇文档失败”的策略改成 best-effort 降级。

如果这两点补上，这条链路会稳很多。
