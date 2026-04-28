# OpenViking 通用知识库技术说明文档

本文档已生成正式 Word 版本：OpenViking通用知识库_技术说明文档.docx。

## 核心流程

- 文档处理：add_resource -> Parser -> TreeBuilder -> SemanticQueue -> SemanticProcessor -> VectorDB。
- 记忆处理：Session messages -> commit archive -> archive summary -> MemoryExtractor -> Dedup/Merge -> memory store。
- 检索流程：find/search -> IntentAnalyzer -> root selection -> hierarchical retrieval -> rerank -> MatchedContext。

## 关键设计

- 双层存储：VikingFS/AGFS 保存 L2 原文和 L0/L1 摘要，VectorDB 保存语义索引。
- 上下文分层：L0 abstract、L1 overview、L2 detail/content。
- 上下文类型：resource、memory、skill。
- 多租户隔离：RequestContext 携带 account/user/agent/role，向量记录写入 account_id 和 owner_space。

详细内容请以 DOCX 正式文档为准。
