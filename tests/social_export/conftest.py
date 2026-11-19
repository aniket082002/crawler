"""All Module B tests run offline: network access is blocked."""
import pytest

from tests.social_common.offline import block_network


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    block_network(monkeypatch)
    yield
