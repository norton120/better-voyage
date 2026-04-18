import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent


def load_http_fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / "http" / name).read_text())
