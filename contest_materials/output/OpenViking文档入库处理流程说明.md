# OpenViking 文档入库处理流程说明

正式交付文件为 `OpenViking文档入库处理流程说明.docx`。

核心链路：

1. HTTP/SDK 提交资源。
2. ResourceService 校验参数、派生 URI、注册知识文档回调。
3. ResourceProcessor 调用 UnifiedResourceProcessor。
4. ParserRegistry 选择解析器，Parser 写入临时 VikingFS。
5. TreeBuilder 解析 root_uri/temp_uri。
6. ResourceProcessor 将临时树移动到正式 AGFS，获取 lifecycle lock。
7. Summarizer 投递 SemanticMsg。
8. SemanticProcessor/SemanticDag 生成 L0/L1，写向量索引。
9. KnowledgeDocumentRegistry 更新 ready/failed 状态。
