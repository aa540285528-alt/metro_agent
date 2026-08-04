import importlib
import sys


def test_skill_runtime_import_does_not_construct_a_provider_client(monkeypatch) -> None:
    monkeypatch.setattr(
        "metro_agent.config.build_Chat_QwenLLM",
        lambda: (_ for _ in ()).throw(AssertionError("provider client constructed")),
    )
    sys.modules.pop("metro_agent.skills.skill_runtime", None)

    importlib.import_module("metro_agent.skills.skill_runtime")
