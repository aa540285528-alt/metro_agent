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

    # ------------------------------------------------------------------
    # 可分配扩展区域列表
    # 这些是除历史（history）之外的额外上下文区域，
    # 按 Agent 的 priority 顺序依次分配
    # ------------------------------------------------------------------
    SECTIONS = (
        "rag",           # RAG 检索到的知识库文档上下文
        "tool",          # 工具调用结果的上下文（来自 tool_context_builder）
        "agent_output",  # 其他 Agent 的输出结果（Supervisor 聚合场景）
    )

    # ------------------------------------------------------------------
    # 上下文模式 → history_max 系数
    #
    # 不同模式对应不同的历史重视程度：
    #   compact  (0.25)：输出优先，几乎不看历史（适用于简单直给的 Agent）
    #   standard (0.60)：默认平衡，历史与输出兼顾
    #   extended (1.00)：历史优先，尽可能多地看历史（有辅助上下文时启用）
    #
    # 乘以 history_max 得到该模式下的历史上限，但不低于 history_min
    # （确保即使 compact 模式也能看到最低限度的历史）。
    # ------------------------------------------------------------------
    MODE_FACTORS = {
        "compact": 0.25,    # 紧凑模式：历史预算仅为最大值的 25%
        "standard": 0.60,   # 标准模式：历史预算为最大值的 60%
        "extended": 1.00,   # 扩展模式：历史预算为最大值的 100%
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
        # ==============================================================
        # 步骤 1：校验输入 + 加载 Agent 配置
        # ==============================================================

        # 检查 agent_name 是否在配置表中注册
        if agent_name not in AGENT_CONTEXT_PROFILES:
            raise ValueError(
                f"未知Agent：{agent_name}"
            )

        # 检查 mode 是否有效（compact / standard / extended）
        if mode not in self.MODE_FACTORS:
            raise ValueError(
                f"未知上下文模式：{mode}"
            )

        # 加载该 Agent 的配置 profile
        # profile 结构：
        # {
        #     "input_limit": 24576,      # 模型总输入限制
        #     "output_reserve": 12288,   # 为输出预留的 token
        #     "history_min": 2048,       # 历史最小保障
        #     "history_max": 20480,      # 历史最大上限
        #     "rag_max": 15000,          # RAG 区域上限（可选）
        #     "tool_max": 10000,         # 工具上下文上限（可选）
        #     "agent_output_max": 64000, # Agent 输出上限（可选）
        #     "priority": ["tool", "rag"],  # 扩展区域分配优先级
        # }
        profile = AGENT_CONTEXT_PROFILES[agent_name]

        # requested 为 None 时视为空字典（所有扩展区域请求 0 token）
        requested = requested or {}

        # ==============================================================
        # 步骤 2：计算可用空间
        #
        # input_limit = 模型能接受的最大输入 token 数
        # fixed_tokens = system_prompt（不可压缩）的 token 数
        # available = 可以被 history + tool + rag + agent_output 分配的剩余空间
        #
        # 边界保护：
        #   - max(0, fixed_tokens) 防止负数（调用方传了错误值）
        #   - max(0, input_limit - fixed_tokens) 确保 available 不为负
        #     （fixed_tokens 超过 input_limit 时，available=0，
        #      由 mandatory_overflow 字段上报）
        # ==============================================================
        input_limit = profile["input_limit"]
        available = max(
            0,
            input_limit - max(0, fixed_tokens),
        )

        # ==============================================================
        # 步骤 3：最低历史保障
        #
        # history_min：该 Agent 必须看到的最少历史 token 数
        #   即使 available 不够，也保障 min(history_min, available)
        #   极端情况：available=0 → history_tokens=0
        #
        # mode_history_max：按模式缩放后的历史预算上限
        #   计算方式：history_max × MODE_FACTORS[mode]
        #   但不低于 history_min（compact 模式也要看到最低限度）
        # ==============================================================
        history_min = profile["history_min"]
        history_max = profile["history_max"]

        # mode_history_max = max(history_min, history_max × mode_factor)
        # 确保即使 compact 模式（×0.25）也不会把上限压到低于最低保障
        mode_history_max = max(
            history_min,
            int(
                history_max
                * self.MODE_FACTORS[mode]
            ),
        )

        # 最近历史获得最低保障（从 available 中优先扣除）
        history_tokens = min(
            history_min,
            available,     # available 不够时有多少给多少
        )
        available -= history_tokens

        # ==============================================================
        # 步骤 4：按优先级分配扩展区域（rag / tool / agent_output）
        #
        # 遍历 profile["priority"] 列表，列表中的顺序即优先级顺序。
        # 每个区域受三重约束：
        #   wanted  = requested.get(section, 0)   → 请求量
        #   maximum = profile.get(f"{section}_max", 0) → 该区域的硬上限
        #   available                                → 剩余可用空间
        #
        # granted = min(wanted, maximum, available)
        # 取三者最小值，保证不超出任何一个约束。
        #
        # 先到先得：排在 priority 前面的区域先分配，
        # 后面的区域只能使用剩余空间。
        # ==============================================================

        # 初始化所有扩展区域配额为 0
        allocations = {
            section: 0
            for section in self.SECTIONS
        }

        # 按 Agent 职责分配 RAG、工具或 Agent 输出
        for section in profile.get("priority", []):
            # 请求量：调用方希望为该区域分配的 token 数
            wanted = max(
                0,
                requested.get(section, 0),
            )

            # 硬上限：该区域在 agent profile 中定义的最大值
            # 如 rag_max=15000 表示最多给 RAG 15000 token
            maximum = profile.get(
                f"{section}_max",
                0,
            )

            # 三重约束取最小值：
            #   - 不能超过请求量（不浪费）
            #   - 不能超过硬上限（不越界）
            #   - 不能超过剩余空间（不超额）
            granted = min(
                wanted,
                maximum,
                available,
            )

            # 写入分配结果，扣减可用空间
            allocations[section] = granted
            available -= granted

        # ==============================================================
        # 步骤 5：剩余空间回填给历史
        #
        # 扩展区域分配完毕后，available 中可能还有剩余空间。
        # 这些空间不会浪费——回填给历史，让对话历史尽可能多。
        #
        # 回填上限 = mode_history_max - history_tokens
        #   （历史总额不能超过 mode 缩放后的上限）
        #
        # extra_history = min(回填上限, 剩余空间)
        # ==============================================================
        extra_history = min(
            mode_history_max - history_tokens,  # 距离上限还有多少空间
            available,                           # 实际还剩多少空间
        )
        history_tokens += extra_history
        available -= extra_history

        # ==============================================================
        # 构建诊断/监控字段
        # ==============================================================

        # dropped_tokens：每个扩展区域被丢弃的 token 数
        # = max(0, 请求量 - 实际分配量)
        # 正值意味着该区域需要的上下文超出了预算，需要截断
        dropped_tokens = {
            section: max(
                0,
                requested.get(section, 0)
                - allocations[section],
            )
            for section in self.SECTIONS
        }

        # ==============================================================
        # 返回完整分配账单
        #
        # 账单中的关键告警字段：
        #   mandatory_overflow > 0：
        #     system_prompt 本身就超出了模型输入限制，这是配置错误，
        #     需要缩减 system_prompt 或升级到更大的模型。
        #
        #   history_shortfall > 0：
        #     历史分配未达到 history_min，Agent 可能因上下文不足
        #     而缺乏足够的对话背景，需要检查 fixed_tokens 是否过大了。
        #
        #   dropped_tokens 中有正值：
        #     某些区域的请求量超出了预算，需要截断对应上下文。
        #     如果频繁出现，可以考虑增大模型上下文窗口或缩减固定消耗。
        # ==============================================================
        return {
            # ---- 基本信息（回显输入参数） ----
            "agent_name": agent_name,
            "mode": mode,

            # ---- 模型限制 ----
            "input_limit": input_limit,              # 模型总输入 token 限制
            "output_reserve": profile[
                "output_reserve"
            ],                                       # 为输出预留的 token 数

            # ---- 分配结果 ----
            "fixed_tokens": fixed_tokens,            # 固定消耗（system_prompt）
            "history_tokens": history_tokens,         # 对话历史配额
            "rag_tokens": allocations["rag"],         # RAG 知识库配额
            "tool_tokens": allocations["tool"],       # 工具上下文配额
            "agent_output_tokens": allocations[
                "agent_output"
            ],                                       # Agent 输出配额

            # ---- 剩余与告警 ----
            "unused_tokens": available,              # 未使用的 token（含输出预留后的剩余）
            "mandatory_overflow": max(
                0,
                fixed_tokens - input_limit,           # system_prompt 超出输入限制的量
            ),
            "history_shortfall": max(
                0,
                history_min - history_tokens,         # 历史未达到最低保障的缺口
            ),
            "dropped_tokens": dropped_tokens,        # 各扩展区域被丢弃的 token 数
        }
