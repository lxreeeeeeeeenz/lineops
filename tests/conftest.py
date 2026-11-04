import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "acars"))
os.environ.setdefault("LINEOPS_IVAO_POLLER", "0")   # nos testes, o laço da IVAO roda à mão (ivao.tick)
os.environ.setdefault("LINEOPS_OFFLINE", "1")       # nos testes, nada de buscar METAR na internet
os.environ.setdefault("LINEOPS_NOTIFY", "0")        # nos testes, sem laço de celular
os.environ.setdefault("AEROSCALE_NO_RPL_UPDATE", "1")  # nos testes, o motor não procura RPL novo na internet
import pytest  # noqa: E402

from lineops import aeroscale_import, db  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures"


@pytest.fixture
def cl(tmp_path):
    from fastapi.testclient import TestClient

    from lineops.app import create_app
    with TestClient(create_app(tmp_path / "data")) as c:
        yield c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    return c
