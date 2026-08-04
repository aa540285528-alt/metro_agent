"""
技能加载器 —— Skill Runtime
============================
负责技能发现、选择和加载，核心理念是"渐进式披露加载"：
  1. 首次加载：只暴露技能的 name + description（轻量 skill cards）
  2. LLM 筛选：根据用户输入从候选技能中选出相关者
  3. 按需加载：只加载被选中技能的完整 SKILL.md 正文

这样避免把所有技能文档一次性塞入上下文，节省 token。
"""

import json
import sys
from pathlib import Path

# 将项目根目录加入 sys.path，以便导入 config 等模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import HumanMessage, SystemMessage

from metro_agent.config import SKILL_SELECTOR_PROMPT, build_Chat_QwenLLM


SKILL_ROOT = Path(__file__).resolve().parent

# 解析skill.md文件
def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)

    if len(parts) < 3:
        return {}, text

    raw_frontmatter = parts[1].strip()
    body = parts[2].strip()

    data = {}

    for line in raw_frontmatter.splitlines():
        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")

    return data, body

# 找到对应agent下的skill.md文件，读取name、description、path并按对应格式返回skill cards
# 比如 agents/GeneralAgent/skills/SKILL.md
# 返回格式：
# [{"name": "fault-analysis-report", "description": "description", "path": "path"}...]
def discover_skills(agent_name: str) -> list[dict]:
    agent_skill_dir = SKILL_ROOT / agent_name

    if not agent_skill_dir.exists():
        return []

    skills = []

    for skill_file in agent_skill_dir.glob("*/SKILL.md"):
        text = skill_file.read_text(encoding="utf-8")
        frontmatter, _ = parse_frontmatter(text)

        name = frontmatter.get("name")
        description = frontmatter.get("description")

        if not name or not description:
            continue

        skills.append({
            "name": name,
            "description": description,
            "path": str(skill_file),
        })

    return skills

# 把用户输入+skill cards传给llm，返回llm需要加载哪些skill
def select_skills_with_llm(llm, user_input: str, skill_cards: list[dict]) -> list[dict]:
    if not skill_cards:
        return []
    selector_prompt = f"""
    {SKILL_SELECTOR_PROMPT}

    用户输入：
    {user_input}

    技能列表：
    {json.dumps(skill_cards, ensure_ascii=False, indent=2)}
    """
    response=llm.invoke([
        SystemMessage(content=selector_prompt),
        HumanMessage(content=user_input),
    ])

    try:
        result = json.loads(response.content)
    except json.JSONDecodeError:
        result = []
    selected_names = result.get("skills", [])

    if not isinstance(selected_names, list):
        return []

    skill_by_name = {
        skill["name"]: skill
        for skill in skill_cards
    }

    selected_skills = []

    for name in selected_names:
        if name in skill_by_name:
            selected_skills.append(skill_by_name[name])

    return selected_skills

if __name__ == "__main__":
    selected = select_skills_with_llm(
        llm=build_Chat_QwenLLM(),
        user_input="根据这次无线基站故障生成一份故障分析报告",
        skill_cards=discover_skills("diagnosis"),
    )
    print(selected)
