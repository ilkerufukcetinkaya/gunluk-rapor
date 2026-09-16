"""Şifre özeti (argon2), alan şifreleme (Fernet), oturum imzası (itsdangerous)."""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

GIZLI_ANAHTAR = os.environ.get("GIZLI_ANAHTAR", "")
if not GIZLI_ANAHTAR:
    raise RuntimeError("GIZLI_ANAHTAR ortam değişkeni tanımlı değil; uygulama başlatılmadı.")

_fernet = Fernet(GIZLI_ANAHTAR.encode())
_hasher = PasswordHasher()
_SAHTE_HASH = _hasher.hash("zamanlama-esitleme")
_imzaci = URLSafeTimedSerializer(hashlib.sha256(b"oturum:" + GIZLI_ANAHTAR.encode()).hexdigest(), salt="oturum")

GECICI_ALFABE = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def sifre_ozeti(sifre: str) -> str:
    return _hasher.hash(sifre)


def sifre_dogru(sifre: str, ozet: str | None) -> bool:
    try:
        return _hasher.verify(ozet or _SAHTE_HASH, sifre) and ozet is not None
    except (VerificationError, InvalidHashError):
        return False


def sifrele(metin: str) -> str:
    return _fernet.encrypt(metin.encode()).decode()


def coz(sifreli: str | None) -> str:
    if not sifreli:
        return ""
    try:
        return _fernet.decrypt(sifreli.encode()).decode()
    except InvalidToken:
        return ""


def gecici_sifre(uzunluk: int = 12) -> str:
    return "".join(secrets.choice(GECICI_ALFABE) for _ in range(uzunluk))


def sifre_izi(ozet: str) -> str:
    """Oturuma gömülür: şifre değişince eski oturumlar geçersizleşir."""
    return hashlib.sha256(ozet.encode()).hexdigest()[:12]


def oturum_imzala(veri: dict) -> str:
    return _imzaci.dumps(veri)


HATIRLA_SURESI = 30 * 24 * 3600
KISA_SURE = 24 * 3600


def oturum_coz(deger: str) -> dict | None:
    """Beni hatırla işaretliyse 30 gün, değilse 1 gün geçerli."""
    try:
        veri, zaman = _imzaci.loads(deger, max_age=HATIRLA_SURESI, return_timestamp=True)
    except BadSignature:
        return None
    if not isinstance(veri, dict):
        return None
    if not veri.get("r") and (datetime.now(timezone.utc) - zaman).total_seconds() > KISA_SURE:
        return None
    return veri
