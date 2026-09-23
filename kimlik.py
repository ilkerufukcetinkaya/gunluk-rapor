"""Oturum çerezi, istek başına kullanıcı bağımlılıkları ve Google OAuth'un state/PKCE çerezi."""
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


# ---------------------------------------------------------------- Google OAuth: imzalı state + PKCE çerezi

GOOGLE_STATE_SURESI = 10 * 60  # sn
PKCE_CEREZ = "google_pkce"
_google_imzaci = URLSafeTimedSerializer(
    hashlib.sha256(b"google-oauth:" + guvenlik.GIZLI_ANAHTAR.encode()).hexdigest(), salt="google-oauth")


def google_state_uret(user_id: int, donus: str) -> tuple[str, str]:
    """(state, nonce). state kullanıcıyı, dönüş sayfasını ve nonce'u taşır; 10 dk geçerli."""
    nonce = secrets.token_urlsafe(16)
    return _google_imzaci.dumps({"u": user_id, "n": nonce, "d": donus}, salt="state"), nonce


def google_state_coz(state: str) -> dict | None:
    """Sahte, bozuk ya da süresi geçmiş state → None."""
    try:
        veri = _google_imzaci.loads(state or "", salt="state", max_age=GOOGLE_STATE_SURESI)
    except BadSignature:
        return None
    if not isinstance(veri, dict) or not isinstance(veri.get("u"), int) or not isinstance(veri.get("n"), str):
        return None
    return veri


def pkce_cerezi_yaz(yanit: Response, nonce: str, dogrulayici: str) -> None:
    """code_verifier tarayıcıda, imzalı ve yalnız /oauth/google yolunda; Google'a giden adreste yer almaz."""
    yanit.set_cookie(
        PKCE_CEREZ, _google_imzaci.dumps({"n": nonce, "v": dogrulayici}, salt="pkce"),
        max_age=GOOGLE_STATE_SURESI, httponly=True, secure=bool(os.environ.get("RENDER")), samesite="lax",
        path="/oauth/google",
    )


def pkce_cerezi_oku(request: Request) -> dict | None:
    try:
        veri = _google_imzaci.loads(request.cookies.get(PKCE_CEREZ) or "", salt="pkce", max_age=GOOGLE_STATE_SURESI)
    except BadSignature:
        return None
    return veri if isinstance(veri, dict) and isinstance(veri.get("n"), str) and isinstance(veri.get("v"), str) else None


def pkce_cerezi_sil(yanit: Response) -> None:
    yanit.delete_cookie(PKCE_CEREZ, path="/oauth/google")
