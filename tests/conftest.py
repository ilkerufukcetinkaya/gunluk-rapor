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
