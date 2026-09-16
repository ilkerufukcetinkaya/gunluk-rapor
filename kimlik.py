"""Oturum çerezi ve istek başına kullanıcı bağımlılıkları."""
from __future__ import annotations

import os

from fastapi import Depends, HTTPException, Request, Response
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
