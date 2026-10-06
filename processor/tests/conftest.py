"""pytest configuration for processor tests."""
import sys
import os
from unittest.mock import AsyncMock, patch

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "../..")
# Allow importing `processor.src.*` from the bridge-v2 root
sys.path.insert(0, _ROOT)
# bridge_shared (shared/bridge_shared) — in the image it sits next to src/ under /app
sys.path.insert(0, os.path.join(_ROOT, "shared"))


@pytest.fixture(autouse=True)
def _no_global_glossary(request):
    """translate_node and /translate read the service-wide glossary from Redis → Postgres.

    Tests run without either, so it is empty unless a test opts in with
    @pytest.mark.real_global_glossary (and mocks the stores itself).
    """
    if request.node.get_closest_marker("real_global_glossary"):
        yield
        return
    with patch("processor.src.pipeline.glossary.global_glossary", new=AsyncMock(return_value={})):
        yield


def pytest_configure(config):
    config.addinivalue_line("markers", "real_global_glossary: use the real global glossary loader")
