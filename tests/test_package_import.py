def test_package_exposes_api_factory() -> None:
    from metro_agent.api import create_app

    assert callable(create_app)
