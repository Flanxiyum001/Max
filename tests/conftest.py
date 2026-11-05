from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from max_core.api import create_app
from max_core.config import Settings

# Public deterministic fixture, NOT a credential for any real environment.
TEST_TOKEN = "test-token-" + "0123456789abcdef" * 3


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(bearer_token=TEST_TOKEN)), base_url="http://localhost")
