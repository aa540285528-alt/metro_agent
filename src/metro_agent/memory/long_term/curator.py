"""
记忆策展层（Curator）
====================
负责从对话中自动提取值得长期保存的记忆候选项。

职责边界：
  - 只负责"提取"：从 user_text + assistant_text 中识别可持久化的记忆
  - 不负责冲突检测、去重、存储（这些在上层 policy 中处理）
  - 返回 MemoryCandidate 列表，由上层决定是否写入

依赖：
  - langmem.create_memory_manager：LangMem 的记忆管理器，驱动 LLM 提取结构化记忆
  - config.MEMORY_INSTRUCTIONS_PROMPT：提取规则的系统提示词
  - models.MemoryCandidate：记忆候选项的数据模型
"""

import sys
from pathlib import Path

# 确保能导入项目根目录的 config 和同级的 memory_system
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langmem import create_memory_manager
from langchain_core.messages import AIMessage, HumanMessage

from metro_agent.config import (
    MEMORY_INSTRUCTIONS_PROMPT,
    build_Ollama_qwenLLM,
)
from metro_agent.memory.long_term.models import MemoryCandidate


# ============================================================
# 记忆策展器
# ============================================================
class MemoryCurator:
    """
    从对话中提取长期记忆候选项。

    内部封装了 LangMem 的 create_memory_manager，配置为：
      - enable_inserts=True   → 允许新增记忆
      - enable_updates=False  → 禁止自动更新（更新由上层 policy 控制）
      - enable_deletes=False  → 禁止自动删除（删除由上层 policy 控制）

    使用方式：
      curator = MemoryCurator()
      candidates = curator.extract(user_text="...", assistant_text="...")
    """

    def __init__(self):
        # 创建 LangMem 记忆管理器，绑定 LLM 和输出 schema
        self.manager = create_memory_manager(
            build_Ollama_qwenLLM(),                  # 使用 Ollama Qwen 模型
            schemas=[MemoryCandidate],               # 强制输出符合 MemoryCandidate 结构
            instructions=MEMORY_INSTRUCTIONS_PROMPT, # 系统提示词：定义提取规则
            enable_inserts=True,                      # 允许提取新记忆
            enable_updates=False,                     # 不在此层做更新
            enable_deletes=False,                     # 不在此层做删除
        )

    def extract(
        self,
        *,
        user_text: str,       # 用户原始输入
        assistant_text: str,  # 助手回复内容
    ) -> list[MemoryCandidate]:
        """
        从一轮对话中提取长期记忆候选项。

        流程：
          1. 将 user_text 和 assistant_text 组装为消息列表
          2. 调用 LangMem manager 进行结构化提取
          3. 将返回结果统一转换为 MemoryCandidate 列表

        返回：MemoryCandidate 列表（可能为空，表示无值得记忆的内容）
        """
        # 调用 LangMem manager，传入对话消息
        extracted = self.manager.invoke({
            "messages": [
                HumanMessage(content=user_text)
            ]
        })

        # 统一转换为 MemoryCandidate 列表
        # manager 可能直接返回 MemoryCandidate 对象，也可能返回 dict，做兼容处理
        candidates = []
        for item in extracted:
            content = item.content
            if isinstance(content, MemoryCandidate):
                # 已是 MemoryCandidate 对象，直接加入
                candidates.append(content)
            else:
                # 是 dict 或其他格式，通过 Pydantic 校验转换
                candidates.append(MemoryCandidate.model_validate(content))

        return candidates


# ============================================================
# 单例构建函数
# ============================================================
_memory_curator = None


def build_memory_curator() -> MemoryCurator:
    """
    构建/获取 MemoryCurator 全局单例。

    首次调用时创建 MemoryCurator 实例（初始化 LangMem manager），
    后续调用直接返回已创建的实例，避免重复初始化。
    """
    global _memory_curator
    if _memory_curator is None:
        _memory_curator = MemoryCurator()
    return _memory_curator
