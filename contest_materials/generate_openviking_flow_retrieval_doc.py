from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt
from PIL import Image, ImageDraw, ImageFont

from generate_ai_contest_docs import (
    ACCENT,
    ACCENT_DARK,
    LOGO,
    OUT,
    add_bullets,
    add_callout,
    add_code_block,
    add_footer,
    add_para,
    add_table,
    set_run_font,
    style_document,
)


DOCX_PATH = OUT / "OpenViking文档入库分片检索排序流程说明.docx"
MD_PATH = OUT / "OpenViking文档入库分片检索排序流程说明.md"
ASSET_DIR = OUT.parent / "assets" / "flow_retrieval_diagrams"

FONT_PATHS = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Arial Unicode.ttf",
]


def _font(size: int, bold: bool = False):
    path = FONT_PATHS[0 if bold else 1]
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _wrap(text: str, width: int) -> List[str]:
    if not text:
        return []
    lines: List[str] = []
    current = ""
    for ch in text:
        if ch == "\n":
            if current:
                lines.append(current)
            current = ""
            continue
        current += ch
        if len(current) >= width:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    return lines


def _box(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[int, int, int, int],
    title: str,
    body: str = "",
    fill: str = "#F7FAFC",
    outline: str = "#1F4E79",
    title_fill: str = "#163957",
):
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=16, fill=fill, outline=outline, width=3)
    title_font = _font(28, bold=True)
    body_font = _font(22)
    draw.text((x1 + 22, y1 + 18), title, fill=title_fill, font=title_font)
    y = y1 + 60
    for line in _wrap(body, max(10, (x2 - x1 - 44) // 22)):
        draw.text((x1 + 22, y), line, fill="#263238", font=body_font)
        y += 30


def _arrow(
    draw: ImageDraw.ImageDraw,
    start: Tuple[int, int],
    end: Tuple[int, int],
    color: str = "#1F4E79",
):
    draw.line([start, end], fill=color, width=4)
    sx, sy = start
    ex, ey = end
    if abs(ex - sx) >= abs(ey - sy):
        direction = 1 if ex >= sx else -1
        points = [(ex, ey), (ex - 16 * direction, ey - 9), (ex - 16 * direction, ey + 9)]
    else:
        direction = 1 if ey >= sy else -1
        points = [(ex, ey), (ex - 9, ey - 16 * direction), (ex + 9, ey - 16 * direction)]
    draw.polygon(points, fill=color)


def _save_diagram(name: str, size: Tuple[int, int], draw_fn) -> Path:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw_fn(draw)
    path = ASSET_DIR / name
    image.save(path)
    return path


def build_diagrams() -> dict[str, Path]:
    diagrams: dict[str, Path] = {}

    def architecture(draw):
        draw.text((50, 28), "OpenViking 总体架构图", fill="#163957", font=_font(34, bold=True))
        boxes = {
            "client": (70, 110, 360, 250),
            "service": (450, 80, 790, 280),
            "pipeline": (900, 80, 1320, 280),
            "storage": (450, 390, 1320, 610),
        }
        _box(draw, boxes["client"], "AI 应用 / 客户端", "客服机器人、智能投标、SDK、HTTP、Console、MCP", "#EEF7FF")
        _box(draw, boxes["service"], "服务层", "ResourceService\nSearchService\nSessionService\nFS/Content/Relation", "#F4F8FB")
        _box(draw, boxes["pipeline"], "核心处理层", "Parser / TreeBuilder\nSemanticQueue / SemanticDag\nIntentAnalyzer / Retriever / Rerank", "#F8FBF4")
        _box(draw, boxes["storage"], "存储层", "VikingFS/AGFS 保存 L2 原文、目录树、.abstract.md、.overview.md；VectorDB 保存 L0/L1/L2 向量、metadata、score、owner_space。", "#FFF8E8")
        _arrow(draw, (360, 180), (450, 180))
        _arrow(draw, (790, 180), (900, 180))
        _arrow(draw, (1090, 280), (1090, 390))
        _arrow(draw, (620, 390), (620, 280))

    diagrams["architecture"] = _save_diagram("architecture.png", (1400, 700), architecture)

    def ingestion(draw):
        draw.text((50, 28), "文档入库时序流程图", fill="#163957", font=_font(34, bold=True))
        labels = [
            ("1 接收资源", "temp_upload 或 add_resource\n生成 RequestContext"),
            ("2 编排请求", "ResourceService 校验参数\n注册 knowledge document 回调"),
            ("3 解析来源", "UnifiedResourceProcessor\n选择 URL/目录/文件/ZIP/文本策略"),
            ("4 分片成树", "Parser 写临时 VikingFS\n生成 L2 文件/目录"),
            ("5 URI 落位", "TreeBuilder 计算 root_uri\ntemp_uri/candidate_uri"),
            ("6 正式落盘", "首次 mv 到 AGFS\n增量更新加 lifecycle lock"),
            ("7 投递任务", "Summarizer 构造 SemanticMsg\n进入 SemanticQueue"),
            ("8 摘要索引", "SemanticProcessor/DAG\n生成 L0/L1 并向量化"),
        ]
        x, y = 55, 115
        w, h, gap = 300, 130, 40
        positions = []
        for i, (title, body) in enumerate(labels):
            row = i // 4
            col = i % 4
            px = x + col * (w + gap)
            py = y + row * 220
            positions.append((px, py, px + w, py + h))
            _box(draw, positions[-1], title, body, "#F7FAFC")
        for i in range(3):
            _arrow(draw, (positions[i][2], positions[i][1] + h // 2), (positions[i + 1][0], positions[i + 1][1] + h // 2))
        _arrow(draw, (positions[3][2] - 30, positions[3][3]), (positions[7][2] - 30, positions[7][1]))
        for i in range(7, 4, -1):
            _arrow(draw, (positions[i][0], positions[i][1] + h // 2), (positions[i - 1][2], positions[i - 1][1] + h // 2))
        draw.text((70, 595), "输出：root_uri、document_id、.abstract.md、.overview.md、L2 内容、向量索引记录、processing_status=ready/failed", fill="#263238", font=_font(24))

    diagrams["ingestion"] = _save_diagram("ingestion_flow.png", (1450, 680), ingestion)

    def chunking(draw):
        draw.text((50, 28), "分片决策流程图", fill="#163957", font=_font(34, bold=True))
        nodes = [
            ((70, 115, 330, 225), "输入文档", "PDF/Word/Markdown/网页/目录/仓库"),
            ((430, 115, 720, 225), "转换为 Markdown/文本结构", "PDF 注入标题\nOffice 转文本和表格"),
            ((820, 115, 1110, 225), "查找标题", "排除代码块、HTML 注释\n识别 # / h1-h6 / 书签/字体标题"),
            ((1180, 115, 1440, 225), "是否小文档", "<= max_section_size\n且 <= max_section_chars"),
            ((1180, 330, 1440, 440), "单文件", "保留为一个 L2 文件"),
            ((820, 330, 1110, 470), "按章节分片", "标题层级变目录\n子章节变子目录或文件"),
            ((430, 330, 720, 470), "合并/拆分", "小片段 < 512 tokens 合并\n超大段按段落/字符拆分"),
            ((70, 330, 330, 470), "输出知识树", "目录 + L2 文件\n.preview-order.json"),
        ]
        for xy, title, body in nodes:
            _box(draw, xy, title, body, "#F4F8FB")
        _arrow(draw, (330, 170), (430, 170))
        _arrow(draw, (720, 170), (820, 170))
        _arrow(draw, (1110, 170), (1180, 170))
        _arrow(draw, (1310, 225), (1310, 330))
        _arrow(draw, (1180, 385), (1110, 385))
        _arrow(draw, (820, 400), (720, 400))
        _arrow(draw, (430, 400), (330, 400))
        draw.text((75, 548), "原则：OpenViking 优先保持文档自然层级，不做固定长度硬切；分片结果直接影响 L2 内容、L0/L1 摘要和检索路径。", fill="#263238", font=_font(24))

    diagrams["chunking"] = _save_diagram("chunking_flow.png", (1500, 640), chunking)

    def retrieval(draw):
        draw.text((50, 28), "检索与排序流程图", fill="#163957", font=_font(34, bold=True))
        nodes = [
            ((70, 105, 330, 220), "find/search", "find: 单查询\nsearch: 可带 session"),
            ((430, 105, 720, 220), "意图分析", "search 读取会话摘要和最近消息\n生成 TypedQuery"),
            ((820, 105, 1110, 220), "根目录选择", "resource/memory/skill\n或 target_uri"),
            ((1180, 105, 1460, 220), "全局向量召回", "L0/L1/L2 候选\n租户和 owner_space 过滤"),
            ((1180, 330, 1460, 445), "起点合并", "目录候选作为递归起点\nL2 作为初始候选"),
            ((820, 330, 1110, 445), "递归搜索子节点", "PathScope depth=1\n目录进入优先队列"),
            ((430, 330, 720, 445), "Rerank/回退", "thinking 模式调用 rerank\n失败回退向量分"),
            ((70, 330, 330, 445), "最终排序", "分数传播 + 阈值\nhotness boost + 去重"),
        ]
        for xy, title, body in nodes:
            _box(draw, xy, title, body, "#EEF7FF")
        _arrow(draw, (330, 162), (430, 162))
        _arrow(draw, (720, 162), (820, 162))
        _arrow(draw, (1110, 162), (1180, 162))
        _arrow(draw, (1320, 220), (1320, 330))
        _arrow(draw, (1180, 388), (1110, 388))
        _arrow(draw, (820, 388), (720, 388))
        _arrow(draw, (430, 388), (330, 388))
        draw.text((75, 535), "输出：MatchedContext(uri, level, abstract, score, context_type, relations)。L0/L1 URI 会补 .abstract.md 或 .overview.md 后缀。", fill="#263238", font=_font(24))

    diagrams["retrieval"] = _save_diagram("retrieval_ranking_flow.png", (1520, 640), retrieval)

    return diagrams


def heading(doc: Document, level: int, text: str):
    p = doc.add_heading(text, level=level)
    p.paragraph_format.keep_with_next = True
    return p


def add_picture(doc: Document, path: Path, width: float = 6.65):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run()
    run.add_picture(str(path), width=Inches(width))
    doc.add_paragraph()


def add_cover(doc: Document):
    if LOGO.exists():
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run()
        run.add_picture(str(LOGO), width=Inches(1.2))

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(24)
    run = p.add_run("OpenViking 文档入库、分片、检索与排序流程说明")
    set_run_font(run, size=21, bold=True, color=ACCENT_DARK)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("含架构图、流程图、分片规则、检索路径与排序算法说明")
    set_run_font(run, size=12, color=ACCENT)

    doc.add_paragraph()
    add_table(
        doc,
        ["项目", "内容"],
        [
            ["文档目的", "通过图和步骤说明，让读者理解 OpenViking 从文档入库到检索排序的处理全过程。"],
            ["核心范围", "资源接入、文档解析、分片成树、L0/L1 语义加工、向量化、find/search、分层召回、rerank、最终排序。"],
            ["阅读对象", "评审专家、AI 应用研发、知识库实施、运维和交付人员。"],
            ["版本口径", f"基于当前源码生成，日期 {date.today().isoformat()}。"],
        ],
        widths=[3.1, 11.9],
        font_size=9.2,
    )
    add_callout(
        doc,
        "核心结论",
        "OpenViking 的处理流程不是“上传后直接向量化”，而是先把资料拆成保持语义层级的知识树，再自底向上生成 L0/L1 摘要，最后在检索时通过意图分析、全局召回、层级递归、重排和热度融合完成排序。",
    )
    doc.add_page_break()


def add_architecture(doc: Document, diagrams: dict[str, Path]):
    heading(doc, 1, "1. 架构图与总体流程")
    add_picture(doc, diagrams["architecture"], width=6.7)
    add_para(
        doc,
        "架构上，OpenViking 把上层 AI 应用、服务编排、核心处理管线和双层存储分离。文档入库、会话记忆和检索都依赖同一个 VikingFS/VectorDB 底座，因此客服机器人、智能投标、售前方案助手等应用可以复用同一套知识与上下文能力。",
    )
    add_picture(doc, diagrams["ingestion"], width=6.75)
    doc.add_page_break()
    heading(doc, 2, "1.1 处理链路摘要")
    add_table(
        doc,
        ["阶段", "关键模块", "主要结果"],
        [
            ["接入", "resources.py、ResourceService", "合法 source、RequestContext、document 回调"],
            ["解析", "UnifiedResourceProcessor、ParserRegistry、Parser", "临时 VikingFS 知识树、ParseResult"],
            ["落位", "TreeBuilder、ResourceProcessor", "root_uri、temp_uri、正式 AGFS 目录"],
            ["语义加工", "Summarizer、SemanticQueue、SemanticProcessor、SemanticDag", ".abstract.md、.overview.md、向量化任务"],
            ["检索排序", "VikingFS.find/search、IntentAnalyzer、HierarchicalRetriever、Rerank", "MatchedContext 和最终 score"],
        ],
        widths=[2.4, 5.8, 6.8],
        font_size=8.4,
    )
    doc.add_page_break()


def add_chunking(doc: Document, diagrams: dict[str, Path]):
    heading(doc, 1, "2. 分片机制详细说明")
    add_picture(doc, diagrams["chunking"], width=6.7)
    add_para(
        doc,
        "OpenViking 的分片发生在 Parser 阶段。它不是按固定字符长度粗暴切块，而是先尽量把不同来源转换成 Markdown/文本结构，再根据标题层级、章节长度、段落边界和字符上限生成目录树与 L2 内容文件。TreeBuilder 不负责切分，只负责把 Parser 产生的临时树落到最终 URI。",
    )
    add_table(
        doc,
        ["判断条件", "处理方式", "输出形态"],
        [
            ["文档较小", "估算 token <= max_section_size，且字符数 <= max_section_chars", "保存为单个 L2 文件"],
            ["无标题的大文档", "按空行分段，累计 token/字符超过限制时切分", "{文档名}_1.md、{文档名}_2.md..."],
            ["有标题的大文档", "识别最高层标题作为一级分片，保留标题层级", "标题对应文件或目录"],
            ["章节有子标题", "父章节成为目录，直接内容作为虚拟 section，子标题继续递归", "目录 + 子文件/子目录"],
            ["小章节", "小于 DEFAULT_MIN_SECTION_TOKENS，进入 pending，和相邻小节合并", "merged 或 {first}_{count}more 文件"],
            ["超大章节且无子标题", "按段落切分；单段过长时按 max_section_chars 强制切", "{章节名}_1.md、{章节名}_2.md..."],
            ["文件名过长或重复", "sanitize 后截断，并用 hash 后缀保证唯一", "稳定、可落盘的 URI segment"],
        ],
        widths=[4.1, 6.5, 4.4],
        font_size=8.0,
    )
    doc.add_page_break()
    heading(doc, 2, "2.1 分片配置与常量")
    add_table(
        doc,
        ["配置/常量", "当前口径", "说明"],
        [
            ["ParserConfig.max_section_size", "默认 1000 tokens", "超过后触发分片；MarkdownParser 内部兜底常量为 1024。"],
            ["DEFAULT_MIN_SECTION_TOKENS", "512 tokens", "小于该值的章节优先合并，避免碎片过小影响检索。"],
            ["ParserConfig.max_section_chars", "默认 6000 字符", "硬性字符上限，防止 token 估算误差导致超大文件。"],
            ["token 估算", "CJK 字符约 0.7 token，其他非空白字符约 0.3 token", "用于分片决策，不等同于模型 tokenizer 精确计数。"],
            ["PREVIEW_ORDER_FILENAME", ".preview-order.json", "记录生成的 markdown_paths，便于预览和顺序恢复。"],
        ],
        widths=[4.2, 4.0, 6.8],
        font_size=8.2,
    )
    add_callout(
        doc,
        "PDF/Office 如何进入分片",
        "PDF 解析会尽量通过书签或字体分析识别标题，并把标题注入为 Markdown heading；Word、Excel、PowerPoint 等办公文档会先转换为可读文本/表格结构，再进入统一的分片与树构建逻辑。",
        fill="F4F8FB",
    )
    doc.add_page_break()


def add_semantic_vector(doc: Document):
    heading(doc, 1, "3. 语义层与向量化")
    add_para(
        doc,
        "分片只产生 L2 内容树。真正用于检索的 L0/L1 摘要和向量索引由 SemanticProcessor/SemanticDag 异步生成。DAG 先处理叶子文件，再汇总子目录，最终自底向上生成父目录的 overview 和 abstract。",
    )
    add_code_block(
        doc,
        """目录节点处理:
  文件摘要 -> 子目录 abstract -> 生成 .overview.md
  .overview.md -> 提取 .abstract.md
  写入 VikingFS
  vectorize_directory_meta:
      L0 Context(level=ABSTRACT, vectorize=abstract)
      L1 Context(level=OVERVIEW, vectorize=overview)

叶子文件处理:
  _generate_single_file_summary -> summary_dict
  vectorize_file:
      text 文件: summary_first / summary_only / raw content
      非文本文件: 使用 summary
      写入 L2 Context(level 默认 detail)"""
    )
    add_table(
        doc,
        ["层级", "物理位置/记录", "向量化文本", "检索作用"],
        [
            ["L0", "每个目录的 .abstract.md 对应一条目录向量记录", "短摘要 abstract", "快速粗召回，定位相关知识区域"],
            ["L1", "每个目录的 .overview.md 对应一条目录向量记录", "目录 overview", "辅助目录导航和重排，理解子节点结构"],
            ["L2", "分片后的正文文件、代码文件、媒体摘要文件", "summary 或原文截断内容", "最终命中的证据片段和回答上下文"],
        ],
        widths=[2.0, 5.4, 3.7, 3.9],
        font_size=8.4,
    )
    add_bullets(
        doc,
        [
            "目录向量会写入 context_type、account_id、owner_space、level、parent_uri、active_count 等 metadata。",
            "文本文件向量化默认优先使用摘要，避免长文档直接嵌入导致输入过长；必要时读取原文并按 max_input_chars 截断。",
            "EmbeddingTaskTracker 会追踪 SemanticMsg 关联的向量任务，全部完成后更新 knowledge document 状态。",
            "skip_vectorization=true 时只生成 L0/L1 文本，不写向量索引，适合只需要摘要不需要检索的场景。",
        ],
    )


def add_retrieval(doc: Document, diagrams: dict[str, Path]):
    heading(doc, 1, "4. 检索流程详细说明")
    add_picture(doc, diagrams["retrieval"], width=6.7)
    add_para(
        doc,
        "OpenViking 提供 find 和 search 两类语义检索。find 不依赖会话上下文，直接把用户 query 包装成 TypedQuery；search 可读取 session 的 latest_archive_overview 和最近消息，通过 IntentAnalyzer 生成多个 TypedQuery，并可同时检索 resource、memory、skill。",
    )
    add_table(
        doc,
        ["接口", "是否使用会话", "TypedQuery 来源", "典型场景"],
        [
            ["find", "否", "直接构造单个 TypedQuery", "知识库搜索框、快速定位资料、单轮问答"],
            ["search", "可选", "有 session 时由 IntentAnalyzer 生成 0-5 条 TypedQuery；无 session 时按目标类型构造", "多轮对话、智能投标、复杂客服问题"],
            ["grep/glob", "否", "不走向量召回", "精确文本查找、路径匹配、调试定位"],
        ],
        widths=[2.3, 2.6, 6.5, 3.6],
        font_size=8.3,
    )
    doc.add_page_break()
    heading(doc, 2, "4.1 检索步骤展开")
    add_table(
        doc,
        ["检索步骤", "输入", "处理逻辑", "输出"],
        [
            ["目标类型推断", "target_uri 或 context_type", "通过 URI 判断 resource/memory/skill；无 target_uri 时可搜索所有类型", "TypedQuery.context_type"],
            ["权限过滤", "RequestContext", "向量过滤 account_id、owner_space；resources 可账号共享，memory/skill 按用户或 agent 空间隔离", "scope_filter"],
            ["全局向量召回", "query dense/sparse vector", "search_global_roots_in_tenant 在 L0/L1/L2 中找候选", "global_results"],
            ["起点合并", "root_uris + global_results", "L0/L1 作为递归起点，L2 作为初始候选；thinking 模式可先 rerank 起点", "starting_points、initial_candidates"],
            ["递归子节点搜索", "当前目录 URI", "PathScope depth=1 搜索直接子节点；目录进入优先队列，L2 为终止命中", "collected_by_uri"],
            ["收敛判断", "当前 topK URI 集合", "topK 连续不变且达到 limit，最多 3 轮后停止", "候选列表"],
            ["结果转换", "候选记录", "读取 related_uri 的 L0 摘要，补 L0/L1 后缀，构造 MatchedContext", "FindResult"],
        ],
        widths=[2.7, 3.2, 6.2, 2.9],
        font_size=7.8,
    )
    doc.add_page_break()


def add_ranking(doc: Document):
    heading(doc, 1, "5. 排序与打分规则")
    add_para(
        doc,
        "排序不是单一向量分数。OpenViking 在不同阶段会使用向量分数、rerank 分数、目录父级分数传播、阈值过滤和热度分数融合，最终返回 MatchedContext.score。",
    )
    add_code_block(
        doc,
        """局部候选分数:
  local_score = rerank_score if rerank 可用且 mode=thinking else vector_score

递归传播分数:
  final_score = alpha * local_score + (1 - alpha) * parent_score
  alpha = SCORE_PROPAGATION_ALPHA = 0.5

阈值过滤:
  final_score > score_threshold
  或 score_gte=true 时 final_score >= score_threshold

最终展示分数:
  display_score = (1 - HOTNESS_ALPHA) * semantic_score + HOTNESS_ALPHA * hotness_score
  HOTNESS_ALPHA = 0.2"""
    )
    add_table(
        doc,
        ["排序因子", "来源", "作用"],
        [
            ["vector_score", "向量库 dense/sparse/hybrid 搜索返回的 _score", "基础相似度，用于全局召回和子节点召回。"],
            ["rerank_score", "RerankClient.rerank_batch(query, documents)", "在 thinking 模式下重排候选摘要，提高语义贴合度。"],
            ["parent_score", "当前进入目录的分数", "通过分数传播让高相关目录下的子节点获得合理加权。"],
            ["score_threshold", "调用参数或 rerank_config.threshold", "过滤低分候选，控制噪声。"],
            ["hotness_score", "active_count + updated_at", "常用、近期更新的上下文获得轻微提升。"],
            ["dedup by URI", "collected_by_uri", "同一 URI 多次命中时保留最高 final_score。"],
            ["relations", "VikingFS relation graph", "最终结果带最多 5 个相关节点摘要，扩展上下文。"],
        ],
        widths=[3.3, 5.4, 6.3],
        font_size=8.2,
    )
    add_callout(
        doc,
        "Rerank 失败时怎么办",
        "如果 rerank 服务异常、返回长度不一致或未配置，系统会记录 warning，并回退到向量分数。这样检索质量可能下降，但服务不会因为重排失败而不可用。",
        fill="FFF8E8",
    )
    doc.add_page_break()
    heading(doc, 2, "5.1 为什么要分层排序")
    add_table(
        doc,
        ["为什么要分层排序", "解释"],
        [
            ["避免只命中孤立碎片", "L0/L1 先定位相关知识区域，再进入子节点，能保留文档结构和上下文关系。"],
            ["降低大库搜索成本", "全局召回只选起点，递归搜索只展开高分目录，减少无关节点遍历。"],
            ["改善答案可解释性", "MatchedContext 保留 level、uri、abstract、relations，方便追溯来源。"],
            ["支持复杂任务", "search 可把一个问题拆成多个 TypedQuery，分别检索资源、记忆和技能。"],
        ],
        widths=[4.0, 11.0],
        font_size=8.4,
    )
    doc.add_page_break()


def add_example_and_source(doc: Document):
    heading(doc, 1, "6. 端到端示例")
    add_code_block(
        doc,
        """示例: 上传“智能投标产品手册.docx”并问“这份材料里有没有酒店行业案例？”

入库:
  1. temp_upload -> temp_file_id
  2. add_resource(wait=true, instruction=面向投标提取产品能力、案例、资质)
  3. WordParser 转换内容，MarkdownParser 分片成章节树
  4. TreeBuilder 生成 root_uri=viking://resources/投标资料/智能投标产品手册
  5. SemanticDag 生成每个目录的 .abstract.md/.overview.md
  6. VectorDB 写入 L0/L1/L2 向量记录

检索:
  1. search(query, session_id) 读取会话摘要和最近消息
  2. IntentAnalyzer 生成 resource 类型 TypedQuery
  3. 全局召回命中“行业案例”相关 L0/L1
  4. 递归进入该目录，召回酒店案例 L2 分片
  5. rerank 提升与“酒店行业案例”最相关的分片
  6. 返回 MatchedContext，供 RAG 生成答案并引用来源"""
    )
    add_table(
        doc,
        ["跟踪对象", "产生阶段", "用途"],
        [
            ["root_uri", "TreeBuilder / ResourceProcessor", "正式知识树入口，检索时可作为 target_uri。"],
            ["分片文件 URI", "MarkdownParser/Parser", "L2 内容节点，最终答案引用来源。"],
            [".abstract.md", "SemanticDag", "L0 快速召回文本。"],
            [".overview.md", "SemanticDag", "L1 目录导航和重排文本。"],
            ["SemanticMsg.id", "Summarizer/SemanticQueue", "关联语义处理和 embedding 任务。"],
            ["document_id", "KnowledgeDocumentRegistry", "查询 ready/failed 处理状态。"],
            ["MatchedContext.score", "HierarchicalRetriever", "最终排序后的可解释检索分数。"],
        ],
        widths=[3.6, 5.0, 6.4],
        font_size=8.4,
    )

    doc.add_page_break()
    heading(doc, 1, "7. 源码映射")
    add_table(
        doc,
        ["主题", "关键源码", "说明"],
        [
            ["资源接入", "openviking/server/routers/resources.py", "temp_upload、add_resource 路由和请求参数。"],
            ["入库编排", "openviking/service/resource_service.py", "URI 约束、文档登记、wait、watch task。"],
            ["执行处理", "openviking/utils/resource_processor.py", "解析、TreeBuilder、正式落盘、Summarizer 投递。"],
            ["来源路由", "openviking/utils/media_processor.py", "URL、目录、文件、ZIP、原始文本路由。"],
            ["分片核心", "openviking/parse/parsers/markdown.py", "标题识别、小节合并、超大段落拆分、preview-order。"],
            ["解析器选择", "openviking/parse/registry.py", "根据扩展名、URL 类型选择 parser。"],
            ["语义 DAG", "openviking/storage/queuefs/semantic_dag.py", "自底向上生成 L0/L1 并调度向量化。"],
            ["向量化", "openviking/utils/embedding_utils.py", "目录 L0/L1 和文件 L2 的 Context/EmbeddingMsg。"],
            ["检索入口", "openviking/storage/viking_fs.py", "find/search、IntentAnalyzer 调用和结果聚合。"],
            ["意图分析", "openviking/retrieve/intent_analyzer.py", "会话上下文转 TypedQuery。"],
            ["分层召回排序", "openviking/retrieve/hierarchical_retriever.py", "全局召回、递归搜索、rerank、分数传播、hotness。"],
            ["向量过滤", "openviking/storage/viking_vector_index_backend.py", "tenant filter、PathScope、target_directories、owner_space。"],
        ],
        widths=[3.0, 6.2, 5.8],
        font_size=7.8,
    )


def build_markdown() -> str:
    return """# OpenViking 文档入库、分片、检索与排序流程说明

正式交付文件为 `OpenViking文档入库分片检索排序流程说明.docx`。

文档包含：

- 总体架构图
- 文档入库时序流程图
- 分片决策流程图
- 检索与排序流程图
- 分片规则、检索路径、排序公式和源码映射
"""


def build_docx():
    OUT.mkdir(parents=True, exist_ok=True)
    diagrams = build_diagrams()
    doc = Document()
    style_document(doc)
    add_footer(doc, "OpenViking 文档入库、分片、检索与排序流程说明")
    add_cover(doc)
    add_architecture(doc, diagrams)
    add_chunking(doc, diagrams)
    add_semantic_vector(doc)
    add_retrieval(doc, diagrams)
    add_ranking(doc)
    add_example_and_source(doc)
    doc.save(DOCX_PATH)
    MD_PATH.write_text(build_markdown(), encoding="utf-8")


if __name__ == "__main__":
    build_docx()
    print(DOCX_PATH)
    print(MD_PATH)
