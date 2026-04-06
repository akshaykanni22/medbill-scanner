# backend/tests/conftest.py
# Configure pytest-asyncio for async test functions.
import pytest


# Tells pytest-asyncio to automatically apply asyncio mode to all
# tests in this package. Avoids needing @pytest.mark.asyncio on each test.
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "asyncio: mark test as async (used by pytest-asyncio)",
    )
