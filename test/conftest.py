import json
from pathlib import Path

import pytest

from ires_fetch.docs import SwaggerSpec

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture(scope="session")
def spec() -> SwaggerSpec:
    return json.loads((DATA_DIR / "ires.json").read_text())


@pytest.fixture(scope="session")
def spec_text() -> bytes:
    return (DATA_DIR / "ires.json").read_bytes()


@pytest.fixture(scope="session")
def docs_html() -> str:
    return (DATA_DIR / "apidocs.html").read_text()
