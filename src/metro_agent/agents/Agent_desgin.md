"""
Agent 模块
=========
包含所有 LangGraph 节点函数：supervisor（意图分类）、4 个专业 agent、审查和组装。

子模块：
  - SupervisorAgent: classify_intent、normalize_agent_order
  - KnowledgeAgent:  knowledge_agent（RAG 知识库检索）
  - DiagnosisAgent:   diagnosis_agent（故障诊断）
  - RealtimeAgent:    realtime_agent（实时告警查询）




