from types import SimpleNamespace

from openviking.service.search_service import resolve_search_limit


def test_resolve_search_limit_uses_config_default(monkeypatch):
    monkeypatch.setattr(
        "openviking.service.search_service.get_openviking_config",
        lambda: SimpleNamespace(default_search_limit=7),
    )

    assert resolve_search_limit(None) == 7
    assert resolve_search_limit(3) == 3
