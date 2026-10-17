import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ap_searcher import config as config_mod  # noqa: E402
from ap_searcher.cache import Store  # noqa: E402

FIXTURES = ROOT / "fixtures"

# config.yaml is gitignored (it carries a personal contact address), so a fresh
# clone only has the example. Both are the same shape; the tests read whichever
# is present so the suite passes before anyone writes their own config.
ROOT_CONFIG = ROOT / "config.yaml"
if not ROOT_CONFIG.exists():
    ROOT_CONFIG = ROOT / "config.example.yaml"


def _fixture_html(name: str) -> str:
    """Wrap a saved __NEXT_DATA__ payload back into the page shape we parse."""
    payload = (FIXTURES / name).read_text(encoding="utf-8")
    return (
        "<html><body><script id=\"__NEXT_DATA__\" type=\"application/json\">"
        + payload
        + "</script></body></html>"
    )


@pytest.fixture
def search_html() -> str:
    return _fixture_html("search_next_data.json")


@pytest.fixture
def detail_html() -> str:
    return _fixture_html("detail_next_data.json")


@pytest.fixture
def cfg(tmp_path):
    cfg = config_mod.load(ROOT_CONFIG)
    cfg.cache.path = str(tmp_path / "test.sqlite3")
    cfg.path = tmp_path / "config.yaml"
    (tmp_path / "config.yaml").write_text(
        ROOT_CONFIG.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return cfg


@pytest.fixture
def store(cfg):
    with Store(cfg.cache.path) as s:
        yield s


@pytest.fixture
def sample_listing():
    data = json.loads((FIXTURES / "search_next_data.json").read_text(encoding="utf-8"))
    query = data["props"]["pageProps"]["dehydratedState"]["queries"][0]
    return query["state"]["data"]["results"][0]
