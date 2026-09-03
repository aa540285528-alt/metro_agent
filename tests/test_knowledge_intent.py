import pytest

from metro_agent.knowledge_intent import requires_published_knowledge


@pytest.mark.parametrize(
    "message",
    ["查询通信设备规程", "请解释 CBTC 系统架构", "知识库中有哪些标准作业要求？"],
)
def test_explicit_knowledge_requests_require_preflight(
    message: str,
) -> None:
    assert requires_published_knowledge(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "你好，介绍一下你自己",
        "你记得我有哪些偏好？",
        "设备登录密码是多少？",
        "传输链路故障怎么排查？",
    ],
)
def test_non_knowledge_routes_never_trigger_knowledge_preflight(message: str) -> None:
    assert requires_published_knowledge(message) is False
