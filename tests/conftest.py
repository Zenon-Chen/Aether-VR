import asyncio
import sys

import pytest

sys.path.insert(0, ".")


@pytest.fixture
def anyio_backend():
    return "asyncio"
