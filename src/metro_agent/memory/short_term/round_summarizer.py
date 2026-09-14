"""
RoundConversationSummarizer —— 单轮对话摘要器
=============================================
使用本地 Ollama 模型（Qwen2.5 7B）将一轮对话压缩为 ≤100 字的摘要，
供压缩管线中的 Worker 异步调用。

在压缩管线中的位置：
  ConversationSegmenter (按轮切分)
    → CompressionDetectionNode (检测待压缩轮次)
    → CompressionQueueNode (入队 Redis)
    → CompressionWorker (异步消费)
        → RoundConversationSummarizer (本模块，生成单轮摘要)
    → CompressionResultNode (应用结果)

为什么用本地 Ollama 而非云端大模型：
  1. 摘要任务简单（提炼要点，≤100 字），不需要强推理能力
  2. 本地调用零成本（无 API 费用）、低延迟（无网络往返）
  3. Qwen2.5 7B 在摘要场景下表现稳定，≤100 字的限制防止跑偏

与 ConversationSummarizer 的区别：
  - RoundConversationSummarizer（本类）：压缩单轮对话，给 Worker 调用
  - ConversationSummarizer（conversation_summarizer.py）：滚动压缩多轮，
    迭代式 f(旧摘要 + 新消息) → 新摘要，给 context_node 调用
"""

from collections.abc import Sequence

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from metro_agent.config import (
    build_Ollama_qwenLLM,
    ROUND_SUMMARY_PROMPT,
)
from metro_agent.memory.short_term.context_builder import (
    ConversationContextBuilder,
)

SUMMARY_TARGET_CHARS = 100
SUMMARY_HARD_LIMIT_CHARS = 120


class RoundConversationSummarizer:
    """使用本地 Ollama 模型将单轮对话压缩为短摘要。

    使用方式：
        summarizer = RoundConversationSummarizer()
        summary = summarizer.summarize(round_messages)
        # summary: "调度员询问故障影响范围，值班员回复3号线列车延误5分钟"

    设计约束：
      - 摘要目标长度 ≤ 100 字，允许少量超过，硬上限为 120 字
      - 空摘要 → 抛出 ValueError（Ollama 异常时拒绝存入）
      - 超过硬上限 → 二次压缩，仍超长时确定性截断

    与 ConversationContextBuilder 的复用：
      本类使用 ConversationContextBuilder.format_messages() 将消息列表
      格式化为 "角色:内容" 文本。这样保证了压缩时看到的文本格式与
      agent_context_assembler 中兜底原文的格式一致，LLM 不会因
      格式变化而产生困惑。
    """

    def __init__(self):
        """初始化摘要器。

        创建两个组件：
          - llm: 本地 Ollama 连接（Qwen2.5 7B），通过 config.build_Ollama_qwenLLM()
            获取，配置了 model_name / base_url / temperature 等参数
          - formatter: 消息格式化器，将 [HumanMessage, AIMessage, ...]
            转为 "用户:xxx\n助手:xxx" 格式的纯文本
        """
        self.llm = build_Ollama_qwenLLM()
        self.formatter = ConversationContextBuilder()

    def summarize(
        self,
        messages: Sequence[BaseMessage],
    ) -> str:
        """生成单轮对话的压缩摘要。

        执行流程（3 步）：
          1. 格式化：将消息列表转为 "角色:内容" 纯文本
             例：HumanMessage("3号线什么情况")
                → "用户:3号线什么情况"
          2. 调 LLM：发送 SystemMessage(摘要指令) + HumanMessage(格式化文本)
             到本地 Ollama，要求生成 ≤100 字的摘要
          3. 校验结果：
             - 空摘要 → ValueError（Ollama 返回了空内容或不合法输出）
             - 超过 120 字 → 二次压缩并执行最终长度兜底

        校验的设计思路：
          100 字是目标值，120 字是硬限制。仅在明显超长时重试一次，
          避免因模型偶尔多输出几个字而让整个异步任务失败。

        Args:
            messages: 单轮对话的消息列表，通常包含 HumanMessage（用户输入）
                      和若干 AIMessage / ToolMessage（Agent 的思考和工具调用）。

        Returns:
            str: 该轮对话的压缩摘要（最多 120 字），例如：
                 "用户查询3号线延误情况，系统返回该线路当前延误5分钟"

        Raises:
            ValueError: 摘要为空时抛出（Ollama 返回异常）。
        """





        text = self.formatter.format_messages(messages)







        response = self.llm.invoke([
            SystemMessage(content=ROUND_SUMMARY_PROMPT),
            HumanMessage(content=text),
        ])


        summary = str(response.content).strip()







        if not summary:
            raise ValueError("Ollama返回空摘要")

        if len(summary) > SUMMARY_HARD_LIMIT_CHARS:
            retry_response = self.llm.invoke([
                SystemMessage(
                    content=(
                        f"将下面的摘要压缩到{SUMMARY_TARGET_CHARS}个字符左右，"
                        f"最多不能超过{SUMMARY_HARD_LIMIT_CHARS}个字符。"
                        "保留用户问题、关键实体、结论和待办。"
                        "只输出摘要正文。"
                    )
                ),
                HumanMessage(content=summary),
            ])

            summary = str(
                retry_response.content
            ).strip()

        if not summary:
            raise ValueError("Ollama二次压缩返回空摘要")

        if len(summary) > SUMMARY_HARD_LIMIT_CHARS:
            summary = (
                summary[
                    :SUMMARY_HARD_LIMIT_CHARS - 1
                ].rstrip()
                + "…"
            )

        return summary
