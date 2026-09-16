"""Günlük rapor servisi: arayüzü sunar, bugünün e-postalarını ve MEDUSA commit'lerini madde olarak önerir."""
import os
import secrets
import threading
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import servisler

load_dotenv(Path(__file__).with_name(".env"))

KULLANICI = "ufuk"
SIFRE = os.environ.get("RAPOR_SIFRE")
if not SIFRE:
    raise RuntimeError("RAPOR_SIFRE ortam değişkeni tanımlı değil; uygulama başlatılmadı.")

ANAHTARLAR = ("GMAIL_KULLANICI", "GMAIL_UYGULAMA_SIFRESI", "GITHUB_TOKEN", "GITHUB_REPO", "ANTHROPIC_API_KEY")
SAYFA = Path(__file__).with_name("templates") / "index.html"

guvenlik = HTTPBasic()


def dogrula(kimlik: HTTPBasicCredentials = Depends(guvenlik)) -> None:
    kullanici_dogru = secrets.compare_digest(kimlik.username.encode(), KULLANICI.encode())
    sifre_dogru = secrets.compare_digest(kimlik.password.encode(), SIFRE.encode())
    if not (kullanici_dogru and sifre_dogru):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Kimlik doğrulanamadı",
            headers={"WWW-Authenticate": 'Basic realm="gunluk-rapor"'},
        )


app = FastAPI(title="Günlük rapor", dependencies=[Depends(dogrula)], docs_url=None, redoc_url=None, openapi_url=None)

_onbellek: dict = {}
_kilit = threading.Lock()


@app.get("/", response_class=HTMLResponse)
def ana_sayfa() -> str:
    return SAYFA.read_text(encoding="utf-8")


@app.get("/api/bugun")
def bugun(yenile: int = 0) -> dict:
    tarih = servisler.istanbul_bugun().isoformat()
    with _kilit:
        if yenile or _onbellek.get("tarih") != tarih:
            ortam = {k: os.environ.get(k, "") for k in ANAHTARLAR}
            _onbellek.clear()
            _onbellek.update(servisler.raporu_uret(ortam))
        return dict(_onbellek)


@app.get("/api/saglik")
def saglik() -> dict:
    return {"ok": True}
