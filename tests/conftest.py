"""Testler yerel sqlite ve geçici anahtarla koşar; .env'deki gerçek bağlantılar ve anahtarlar kullanılmaz."""
import os
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

_gecici_klasor = tempfile.mkdtemp(prefix="gunluk-rapor-test-")
# app.py load_dotenv'i override=False ile çağırır; burada tanımlananlar .env'dekilerin önüne geçer.
os.environ["DATABASE_URL"] = f"sqlite:///{_gecici_klasor}/test.db"
os.environ["GIZLI_ANAHTAR"] = Fernet.generate_key().decode()
for anahtar in ("ANTHROPIC_API_KEY", "ADMIN_EPOSTA", "ADMIN_SIFRE", "RENDER"):
    os.environ[anahtar] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import pytest  # noqa: E402


class SahteClaude:
    """servisler._claude_cagir yerine geçer: gelen istek gövdelerini saklar, sıradaki yanıtı döner.
    yanıt: str (metin), callable(istek) -> str ya da Exception örneği."""

    def __init__(self):
        self.istekler: list[dict] = []
        self.yanitlar: list = []

    def __call__(self, istek, api_anahtari, istemci=None):
        self.istekler.append(istek)
        yanit = self.yanitlar.pop(0) if self.yanitlar else "[]"
        if isinstance(yanit, Exception):
            raise yanit
        return yanit(istek) if callable(yanit) else yanit


@pytest.fixture
def sahte_claude(monkeypatch):
    import servisler

    sahte = SahteClaude()
    monkeypatch.setattr(servisler, "_claude_cagir", sahte)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anahtar")
    return sahte
