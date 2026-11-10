import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never touch the network; modules that fetch data see it as offline."""
    from bongo.modules.weather import data

    def offline(url):
        raise OSError("network disabled in tests")

    monkeypatch.setattr(data, "fetch_json", offline)
