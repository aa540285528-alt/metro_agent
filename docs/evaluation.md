# Evaluation

Metro Agent uses three complementary quality loops.

1. **Offline deterministic RAG evaluation** binds each case to the knowledge-index
   build, retrieved chunks, rerank order, model configuration, and local artifact.
2. **Trace-based Agent evaluation** binds `case_id`, trace evidence, tool calls,
   selected path, latency, token usage, and final-answer hashes for reproducible review.
3. **SME sampling** reviews safety, correctness, and operational suitability before a
   change reaches production.

The optional `evaluation` dependency group enables RAGAS and DeepEval runners. These
runners can call a configured judge model and may send case data to that provider; use
only sanitized evaluation cases and an approved provider configuration. CI runs only
offline tests and never receives provider credentials.

Evaluation artifacts, raw traces, vector indexes, and production knowledge are local
runtime data. They are excluded from version control and can be inspected locally when
performing a regression review.
