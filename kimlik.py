"""Oturum çerezi, istek başına kullanıcı bağımlılıkları ve OAuth'un (Google, Microsoft) state/PKCE çerezi."""
from __future__ import annotations

import hashlib
import os
import secrets

from fastapi import Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

import guvenlik
from veritabani import Kullanici, oturum

CEREZ = "oturum"


class GirisGerekli(Exception):
    pass


class SifreDegistirilmeli(Exception):
    pass


def cerez_yaz(yanit: Response, kullanici: Kullanici, hatirla: bool) -> None:
    deger = guvenlik.oturum_imzala({"u": kullanici.id, "h": guvenlik.sifre_izi(kullanici.sifre_hash), "r": hatirla})
    yanit.set_cookie(
        CEREZ, deger,
        max_age=guvenlik.HATIRLA_SURESI if hatirla else None,
        httponly=True, secure=bool(os.environ.get("RENDER")), samesite="lax", path="/",
    )


def cerez_sil(yanit: Response) -> None:
    yanit.delete_cookie(CEREZ, path="/")


def oturum_verisi(request: Request) -> dict | None:
    deger = request.cookies.get(CEREZ)
    return guvenlik.oturum_coz(deger) if deger else None


def oturumdaki_kullanici(request: Request, db: Session) -> Kullanici | None:
    veri = oturum_verisi(request)
    if not veri or not isinstance(veri.get("u"), int):
        return None
    kullanici = db.get(Kullanici, veri["u"])
    if kullanici is None or not kullanici.aktif or guvenlik.sifre_izi(kullanici.sifre_hash) != veri.get("h"):
        return None
    return kullanici


def giris_yapmis(request: Request, db: Session = Depends(oturum)) -> Kullanici:
    """Şifre değiştirme zorunluluğuna bakmaz; yalnız /sifre için."""
    kullanici = oturumdaki_kullanici(request, db)
    if kullanici is None:
        raise GirisGerekli()
    return kullanici


def aktif_kullanici(kullanici: Kullanici = Depends(giris_yapmis)) -> Kullanici:
    if kullanici.sifre_degistirmeli:
        raise SifreDegistirilmeli()
    return kullanici


def yonetici(kullanici: Kullanici = Depends(aktif_kullanici)) -> Kullanici:
    if kullanici.rol != "admin":
        raise HTTPException(status_code=403, detail="Bu sayfa yalnız yöneticiler içindir")
    return kullanici


# ---------------------------------------------------------------- OAuth (Google, Microsoft): imzalı state + PKCE çerezi

OAUTH_SAGLAYICILARI = ("google", "microsoft")
GOOGLE_STATE_SURESI = 10 * 60  # sn
MICROSOFT_STATE_SURESI = 10 * 60
PKCE_CEREZ = "google_pkce"
MICROSOFT_PKCE_CEREZ = "microsoft_pkce"


def _imzaci(saglayici: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        hashlib.sha256(f"{saglayici}-oauth:".encode() + guvenlik.GIZLI_ANAHTAR.encode()).hexdigest(), salt=f"{saglayici}-oauth")


# Sağlayıcı başına ayrı imza: Google'ın state'i Microsoft dönüşünde (ya da tersi) geçmez.
_imzacilar = {s: _imzaci(s) for s in OAUTH_SAGLAYICILARI}
_google_imzaci = _imzacilar["google"]


def _sure(saglayici: str) -> int:
    return GOOGLE_STATE_SURESI if saglayici == "google" else MICROSOFT_STATE_SURESI


def _pkce_cerezi(saglayici: str) -> str:
    return PKCE_CEREZ if saglayici == "google" else MICROSOFT_PKCE_CEREZ


def oauth_state_uret(saglayici: str, user_id: int, donus: str) -> tuple[str, str]:
    """(state, nonce). state kullanıcıyı, dönüş sayfasını ve nonce'u taşır; 10 dk geçerli."""
    nonce = secrets.token_urlsafe(16)
    return _imzacilar[saglayici].dumps({"u": user_id, "n": nonce, "d": donus}, salt="state"), nonce


def oauth_state_coz(saglayici: str, state: str) -> dict | None:
    """Sahte, bozuk ya da süresi geçmiş state → None."""
    try:
        veri = _imzacilar[saglayici].loads(state or "", salt="state", max_age=_sure(saglayici))
    except BadSignature:
        return None
    if not isinstance(veri, dict) or not isinstance(veri.get("u"), int) or not isinstance(veri.get("n"), str):
        return None
    return veri


def oauth_pkce_yaz(saglayici: str, yanit: Response, nonce: str, dogrulayici: str) -> None:
    """code_verifier tarayıcıda, imzalı ve yalnız /oauth/<sağlayıcı> yolunda; sağlayıcıya giden adreste yer almaz."""
    yanit.set_cookie(
        _pkce_cerezi(saglayici), _imzacilar[saglayici].dumps({"n": nonce, "v": dogrulayici}, salt="pkce"),
        max_age=_sure(saglayici), httponly=True, secure=bool(os.environ.get("RENDER")), samesite="lax",
        path=f"/oauth/{saglayici}",
    )


def oauth_pkce_oku(saglayici: str, request: Request) -> dict | None:
    try:
        veri = _imzacilar[saglayici].loads(request.cookies.get(_pkce_cerezi(saglayici)) or "", salt="pkce",
                                           max_age=_sure(saglayici))
    except BadSignature:
        return None
    return veri if isinstance(veri, dict) and isinstance(veri.get("n"), str) and isinstance(veri.get("v"), str) else None


def oauth_pkce_sil(saglayici: str, yanit: Response) -> None:
    yanit.delete_cookie(_pkce_cerezi(saglayici), path=f"/oauth/{saglayici}")


# G1 adları
def google_state_uret(user_id: int, donus: str) -> tuple[str, str]:
    return oauth_state_uret("google", user_id, donus)


def google_state_coz(state: str) -> dict | None:
    return oauth_state_coz("google", state)


def pkce_cerezi_yaz(yanit: Response, nonce: str, dogrulayici: str) -> None:
    oauth_pkce_yaz("google", yanit, nonce, dogrulayici)


def pkce_cerezi_oku(request: Request) -> dict | None:
    return oauth_pkce_oku("google", request)


def pkce_cerezi_sil(yanit: Response) -> None:
    oauth_pkce_sil("google", yanit)
