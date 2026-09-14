"""
AgentContextBudgetManager —— Agent 上下文 Token 预算管理器
==========================================================
负责为每个 Agent 精确分配 token 预算，将模型的输入限制（input_limit）在
对话历史（history）、工具上下文（tool）、RAG 知识（rag）、Agent 输出
（agent_output）四个区域之间按优先级分配。

这是短期记忆系统"消费端"的预算核心——所有 Agent 在调用 LLM 之前，
都通过本模块获取各区域的 token 配额，确保不超出模型上下文窗口。

设计动机：
  在多 Agent 协作系统中，不同 Agent 有不同的上下文需求：
    - Supervisor 需要大量历史上下文来做调度决策（32K history）
    - Diagnosis 需要工具结果和知识库来诊断问题（tool + rag 优先）
    - Realtime 需要轻量上下文快速响应（8K 总量）
  一刀切的上下文策略要么浪费 token（小 Agent 用大窗口），
  要么信息不足（大 Agent 上下文被截断）。

本模块的职责：
  - 读取 AGENT_CONTEXT_PROFILES 获取各 Agent 的配置
  - 根据 mode（compact/standard/extended）缩放历史配额
  - 按 priority 顺序为扩展区域（tool/rag/agent_output）分配 token
  - 剩余空间回填给历史，最大化上下文利用率
  - 输出完整的分配账单（含溢出/短缺/丢弃统计）

分配优先级模型（从高到低）：
  1. fixed_tokens（system_prompt）：不可压缩，必须从 input_limit 中扣除
  2. history_min（最低历史保障）：每个 Agent 至少能看到这么多历史
  3. priority 区域（按 order 顺序）：先到先得，受各自 max 约束
  4. 剩余空间 → 回填给历史（受 mode_history_max 上限约束）

与上下游的关系：
  上游：agent_context_assembler.py 在 build() 中调用 allocate()
  下游：返回的分配结果直接指导 Assembler 的消息选择和截断逻辑
  配置来源：config.py 中的 AGENT_CONTEXT_PROFILES
"""

from metro_agent.config import AGENT_CONTEXT_PROFILES


class AgentContextBudgetManager:
    """按 Agent 类型和模式精确分配 token 预算。

    使用方式：
        manager = AgentContextBudgetManager()
        allocation = manager.allocate(
            agent_name="diagnosis",
            fixed_tokens=1200,          # system_prompt 的 token 数
            requested={"tool": 3000},   # 当前需要 3000 token 的工具上下文
            mode="standard",            # 标准模式（60% history_max）
        )
        # allocation["history_tokens"] → 分配给对话历史的 token 数
        # allocation["tool_tokens"]    → 分配给工具上下文的 token 数

    该管理器是无状态的——每次调用 allocate() 根据输入参数独立计算，
    不保留任何历史分配记录。
    """






    SECTIONS = (
        "rag",
        "tool",
        "agent_output",
    )












    MODE_FACTORS = {
        "compact": 0.25,
        "standard": 0.60,
        "extended": 1.00,
    }

    def allocate(
        self,
        agent_name: str,
        fixed_tokens: int,
        requested: dict[str, int] | None = None,
        mode: str = "standard",
    ) -> dict:
        """为指定 Agent 分配各部分 token 预算。

        完整分配流程（5 步）：

          步骤 1 —— 校验 + 加载配置
            检查 agent_name 和 mode 的有效性，
            从 AGENT_CONTEXT_PROFILES 加载该 Agent 的配置

          步骤 2 —— 计算可用空间
            available = input_limit - fixed_tokens
            fixed_tokens 是 system_prompt + 额外固定消耗（不可压缩）

          步骤 3 —— 最低历史保障
            history_tokens = min(history_min, available)
            即使 compact 模式也要保证最低历史

          步骤 4 —— 按优先级分配扩展区域
            遍历 profile["priority"] 列表，每个区域：
              granted = min(wanted, maximum, available)
            先到先得，受三重约束

          步骤 5 —— 剩余空间回填历史
            未使用的 available 借给历史，
            但不超过 mode_history_max 上限

        Args:
            agent_name: Agent 名称，必须在 AGENT_CONTEXT_PROFILES 中注册。
                        当前支持：diagnosis / general / realtime / knowledge。
            fixed_tokens: 固定消耗的 token 数，包括：
                          - system_prompt 的 token 数
                          - 额外的输出格式约束文本的 token 数
                          这部分不可压缩，必须优先从 input_limit 中扣除。
            requested: 各扩展区域的请求 token 数，如 {"tool": 1200, "rag": 800}。
                       这些值由调用方（AgentContextAssembler）通过 count_text()
                       预计算后传入。未提供的区域默认为 0。
            mode: 上下文模式，影响历史预算的上限：
                  "compact"  → history_max × 0.25（输出优先）
                  "standard" → history_max × 0.60（默认平衡）
                  "extended" → history_max × 1.00（历史优先）

        Returns:
            dict: 完整的预算分配账单，包含以下键：
              - agent_name (str):          Agent 名称（回显）
              - mode (str):                使用的上下文模式
              - input_limit (int):         Agent 模型的总输入限制（token）
              - output_reserve (int):      为模型输出预留的 token 数
              - fixed_tokens (int):        固定消耗（system_prompt 等）
              - history_tokens (int):      分配给对话历史的 token 数
              - rag_tokens (int):          分配给 RAG 上下文的 token 数
              - tool_tokens (int):         分配给工具上下文的 token 数
              - agent_output_tokens (int): 分配给其他 Agent 输出的 token 数
              - unused_tokens (int):       未能分配出去的剩余 token
              - mandatory_overflow (int):  fixed_tokens 超出 input_limit 的量（>0 即严重错误）
              - history_shortfall (int):   历史分配未达到 history_min 的缺口（>0 即告警）
              - dropped_tokens (dict):     各扩展区域被丢弃（无法满足）的 token 数

        Raises:
            ValueError: agent_name 不在 AGENT_CONTEXT_PROFILES 中
            ValueError: mode 不在 MODE_FACTORS 中
        """





        if agent_name not in AGENT_CONTEXT_PROFILES:
            raise ValueError(
                f"未知Agent：{agent_name}"
            )


        if mode not in self.MODE_FACTORS:
            raise ValueError(
                f"未知上下文模式：{mode}"
            )













        profile = AGENT_CONTEXT_PROFILES[agent_name]


        requested = requested or {}














        input_limit = profile["input_limit"]
        available = max(
            0,
            input_limit - max(0, fixed_tokens),
        )












        history_min = profile["history_min"]
        history_max = profile["history_max"]



        mode_history_max = max(
            history_min,
            int(
                history_max
                * self.MODE_FACTORS[mode]
            ),
        )


        history_tokens = min(
            history_min,
            available,
        )
        available -= history_tokens


















        allocations = {
            section: 0
            for section in self.SECTIONS
        }


        for section in profile.get("priority", []):

            wanted = max(
                0,
                requested.get(section, 0),
            )



            maximum = profile.get(
                f"{section}_max",
                0,
            )





            granted = min(
                wanted,
                maximum,
                available,
            )


            allocations[section] = granted
            available -= granted












        extra_history = min(
            mode_history_max - history_tokens,
            available,
        )
        history_tokens += extra_history
        available -= extra_history








        dropped_tokens = {
            section: max(
                0,
                requested.get(section, 0)
                - allocations[section],
            )
            for section in self.SECTIONS
        }

















        return {

            "agent_name": agent_name,
            "mode": mode,


            "input_limit": input_limit,
            "output_reserve": profile[
                "output_reserve"
            ],


            "fixed_tokens": fixed_tokens,
            "history_tokens": history_tokens,
            "rag_tokens": allocations["rag"],
            "tool_tokens": allocations["tool"],
            "agent_output_tokens": allocations[
                "agent_output"
            ],


            "unused_tokens": available,
            "mandatory_overflow": max(
                0,
                fixed_tokens - input_limit,
            ),
            "history_shortfall": max(
                0,
                history_min - history_tokens,
            ),
            "dropped_tokens": dropped_tokens,
        }
