"""
Obsidian 知识库加载器。

本模块负责从 Obsidian 风格的 Markdown 知识库中读取文档，
解析 YAML frontmatter、Wikilinks 等 Obsidian 特有语法，
并将其转换为 llama_index 的 Document 对象，供下游 RAG 流程使用。
"""

import re
from pathlib import Path
from typing import Any

from llama_index.core import Document

# yaml 为可选依赖：若已安装则用 PyYAML 解析 frontmatter，否则回退到手动解析
try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency fallback
    yaml = None


# ---------------------------------------------------------------------------
# 加载时默认排除的系统/元文件
# ---------------------------------------------------------------------------
EXCLUDED_FILE_NAMES = {
    "LLM Wiki.md",  # LLM 生成的汇总页面
    "index.md",     # 索引页面
    "log.md",       # 日志页面
}

# ---------------------------------------------------------------------------
# 用于识别文档类型的 frontmatter 标签集合
# ---------------------------------------------------------------------------
DOC_TYPE_TAGS = {
    "concept",         # 概念型文档
    "entity",          # 实体型文档
    "source-summary",  # 源材料摘要
    "system",          # 系统级页面（通常被过滤）
    "meta",            # 元信息页面
}


def _parse_inline_list(value: str) -> list[str]:
    """解析 YAML 内联列表字符串。

    支持两种格式：
        - 非列表值（如 "concept"）→ 包装为单元素列表 ["concept"]
        - 方括号列表（如 "[a, b, c]"）→ 解析为 ["a", "b", "c"]
        - 空方括号 "[]" → 返回空列表
    """
    value = value.strip()
    # 不以方括号包裹的值视为单个标量
    if not value.startswith("[") or not value.endswith("]"):
        return [value.strip("\"'")]

    inner = value[1:-1].strip()
    if not inner:
        return []

    return [
        item.strip().strip("\"'")
        for item in inner.split(",")
        if item.strip()
    ]


def _parse_frontmatter_fallback(frontmatter: str) -> dict[str, Any]:
    """手动解析 frontmatter（PyYAML 不可用时的回退方案）。

    支持的语法：
        - key: value          → 标量字段
        - key:                → 空列表（后续以 "- item" 行填充）
        - - item              → 追加到当前 key 的列表
        - key: [a, b, c]      → 内联列表
        - 空行和 # 开头的注释行会被跳过
    """
    metadata: dict[str, Any] = {}
    current_key = ""  # 跟踪当前正在填充列表的键名

    for raw_line in frontmatter.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        # 跳过空行和注释行
        if not stripped or stripped.startswith("#"):
            continue

        # 以 "- " 开头的行：追加到当前键对应的列表中
        if stripped.startswith("- ") and current_key:
            metadata.setdefault(current_key, [])
            if isinstance(metadata[current_key], list):
                metadata[current_key].append(stripped[2:].strip().strip("\"'"))
            continue

        # 不含 ":" 的行跳过
        if ":" not in stripped:
            continue

        # 解析 "key: value" 对
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key

        if not value:
            # value 为空 → 初始化为空列表，等待后续 "- item" 行填充
            metadata[key] = []
        elif value.startswith("[") and value.endswith("]"):
            # 内联列表格式
            metadata[key] = _parse_inline_list(value)
        else:
            # 普通标量值，去除首尾引号
            metadata[key] = value.strip("\"'")

    return metadata


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """从 Markdown 文本中分离 frontmatter 和正文。

    Obsidian frontmatter 以 `---` 开头和结尾的 YAML 块形式存在。
    优先使用 PyYAML 解析；若 yaml 不可用或解析失败，则回退到手动解析器。

    Returns:
        (metadata_dict, body_text) — 元数据字典和去除 frontmatter 后的正文。
    """
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    frontmatter = parts[1]
    body = parts[2].strip()

    # 优先使用 PyYAML 解析，失败或不可用时回退
    if yaml is not None:
        parsed = yaml.safe_load(frontmatter) or {}
        if isinstance(parsed, dict):
            return parsed, body

    return _parse_frontmatter_fallback(frontmatter), body


def normalize_wikilinks(text: str) -> tuple[str, list[str]]:
    """标准化 Obsidian Wikilinks 语法。

    将 [[页面名]] 和 ![[嵌入页面]] 替换为纯文本标签，同时收集所有被引用的页面名。
        - [[页面|别名]]  → 显示"别名"，保留"页面"到链接列表
        - ![[图片.png]]  → 显示"图片.png"，并收集该引用
        - [[页面]]       → 显示"页面"，并收集该引用

    Returns:
        (normalized_text, links_list) — 替换后的文本和去重后的链接页面名列表。
    """
    links: list[str] = []

    def replace(match: re.Match) -> str:
        raw = match.group(1).strip()
        page, _, label = raw.partition("|")
        page = page.strip()
        label = label.strip()
        if page:
            links.append(page)
        return label or page

    # 先处理嵌入链接 ![[...]]，再处理普通链接 [[...]]，避免误匹配
    normalized = re.sub(r"!\[\[([^\]]+)\]\]", replace, text)
    normalized = re.sub(r"\[\[([^\]]+)\]\]", replace, normalized)
    # dict.fromkeys 用于去重并保持插入顺序
    return normalized, list(dict.fromkeys(links))


def _as_list(value: Any) -> list[str]:
    """将任意值安全地转换为字符串列表。

    - None         → []
    - list         → 过滤空字符串后的元素列表
    - 其他标量值     → 单元素列表（空字符串则返回 []）
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def _metadata_value(value: Any) -> str | int | float | bool:
    """将元数据值转换为 llama_index Document 可接受的存储格式。

    转换规则：
        - 基本标量（str/int/float/bool）→ 直接返回
        - None                     → 空字符串
        - list                     → 用 " | " 拼接的字符串
        - dict / 其他               → 字符串表示
    """
    if isinstance(value, (str, int, float, bool)):
        return value
    if value is None:
        return ""
    if isinstance(value, list):
        return " | ".join(str(item) for item in value)
    if isinstance(value, dict):
        return str(value)
    return str(value)


def build_obsidian_metadata(
    path: Path,
    root: Path,
    frontmatter: dict[str, Any],
    links: list[str],
) -> dict[str, Any]:
    """\u6839\u636e\u6587\u4ef6\u8def\u5f84\u3001frontmatter \u548c Wikilinks \u6784\u5efa\u7edf\u4e00\u7684\u5143\u6570\u636e\u5b57\u5178\u3002

    \u4ece frontmatter \u4e2d\u63d0\u53d6 tags / aliases / sources / related \u7b49\u5b57\u6bb5\uff0c
    \u5e76\u7ed3\u5408\u6587\u4ef6\u4fe1\u606f\u548c\u94fe\u63a5\u5217\u8868\uff0c\u751f\u6210\u4e0b\u6e38\u68c0\u7d22\u7cfb\u7edf\u4f7f\u7528\u7684\u6807\u51c6\u5316\u5143\u6570\u636e\u3002

    Args:
        path:        \u5f53\u524d Markdown \u6587\u4ef6\u7684\u7edd\u5bf9\u8def\u5f84
        root:        \u77e5\u8bc6\u5e93\u6839\u76ee\u5f55
        frontmatter: split_frontmatter \u89e3\u6790\u51fa\u7684\u5143\u6570\u636e\u5b57\u5178
        links:       normalize_wikilinks \u6536\u96c6\u7684\u94fe\u63a5\u9875\u9762\u540d\u5217\u8868

    Returns:
        \u6241\u5e73\u5316\u7684\u5143\u6570\u636e\u5b57\u5178\uff0c\u6240\u6709\u503c\u5747\u53ef\u76f4\u63a5\u5b58\u5165 llama_index Document.metadata\u3002
    """
    # \u63d0\u53d6\u6838\u5fc3\u5b57\u6bb5
    tags = _as_list(frontmatter.get("tags"))
    aliases = _as_list(frontmatter.get("aliases"))
    sources = _as_list(frontmatter.get("sources"))
    related = _as_list(frontmatter.get("related"))

    # \u6587\u6863\u7c7b\u578b\u4f18\u5148\u4ece tags \u4e2d\u5339\u914d\u5df2\u77e5\u7c7b\u578b\uff0c\u5426\u5219\u53d6\u7b2c\u4e00\u4e2a tag
    doc_type = next(
        (tag for tag in tags if tag in DOC_TYPE_TAGS),
        tags[0] if tags else "unknown",
    )

    metadata: dict[str, Any] = {
        "title": path.stem,
        "file_name": path.name,
        "file_path": str(path),
        "relative_path": str(path.relative_to(root)),
        "doc_type": doc_type,
        "tags": tags,
        "aliases": aliases,
        "sources": sources,
        "related": related,
        "related_pages": links,
        "created": frontmatter.get("created", ""),
        "updated": frontmatter.get("updated", ""),
        "status": frontmatter.get("status", "unknown"),
        "knowledge_source": "obsidian",
    }

    # \u4e3a\u6bcf\u4e2a tag \u751f\u6210\u72ec\u7acb\u7684\u5e03\u5c14\u6807\u8bb0\u5b57\u6bb5\uff08\u5982 tag_concept: True\uff09\uff0c\u4fbf\u4e8e\u6309\u6807\u7b7e\u8fc7\u6ee4
    for tag in tags:
        safe_tag = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]", "_", tag)
        metadata[f"tag_{safe_tag}"] = True

    # \u5c06\u6240\u6709\u503c\u8f6c\u6362\u4e3a\u53ef\u5b58\u50a8\u683c\u5f0f
    return {
        key: _metadata_value(value)
        for key, value in metadata.items()
    }


def load_obsidian_documents(
    input_dir: str | Path,
    include_system_pages: bool = False,
) -> list[Document]:
    """加载 Obsidian 知识库中所有 Markdown 文件并转换为 llama_index Document 列表。

    处理流程：
        1. 扫描 input_dir 下所有 .md 文件
        2. 过滤排除文件（LLM Wiki.md / index.md / log.md）
        3. 分离 frontmatter → 解析 Wikilinks → 构建元数据
        4. 过滤 system 类型的文档（include_system_pages=False 时）
        5. 拼接页面信息头 + 正文，生成 Document 对象

    Args:
        input_dir:            Obsidian 知识库根目录路径
        include_system_pages: 是否包含 doc_type 为 "system" 的系统页面

    Returns:
        llama_index Document 对象列表

    Raises:
        ValueError: 路径不存在或知识库为空时抛出
    """
    root = Path(input_dir)
    if not root.exists():
        raise ValueError(f"知识库路径不存在: {root}")

    documents: list[Document] = []

    # 递归扫描所有 .md 文件，按字典序处理以保证确定性
    for path in sorted(root.rglob("*.md")):
        # 跳过默认排除的系统/元文件
        if not include_system_pages and path.name in EXCLUDED_FILE_NAMES:
            continue

        raw_text = path.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(raw_text)
        normalized_body, links = normalize_wikilinks(body)
        metadata = build_obsidian_metadata(path, root, frontmatter, links)

        # 二次过滤：跳过 doc_type 为 system 的页面
        if not include_system_pages and metadata.get("doc_type") == "system":
            continue

        # 拼接页面信息头，帮助 LLM 理解文档上下文
        page_header = (
            f"标题: {metadata['title']}\n"
            f"类型: {metadata['doc_type']}\n"
            f"别名: {metadata.get('aliases', '')}\n"
            f"关联页面: {metadata.get('related_pages', '')}\n\n"
        )

        documents.append(
            Document(
                text=page_header + normalized_body,
                metadata=metadata,
                id_=str(path),
            )
        )

    if not documents:
        raise ValueError(f"知识库为空或没有可入库的 Markdown 文件: {root}")

    return documents
