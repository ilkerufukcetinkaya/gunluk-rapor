"""Günlük rapor servisi: hesaplar, sayfalar ve API. Her kullanıcı kendi Gmail/GitHub ayarlarıyla çalışır."""
import logging
import mimetypes
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

from fastapi import Depends, FastAPI, Form, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import api  # noqa: E402
import guvenlik  # noqa: E402
from kimlik import (  # noqa: E402
    GirisGerekli, SifreDegistirilmeli, aktif_kullanici, cerez_sil, cerez_yaz, giris_yapmis, oturum_verisi,
    oturumdaki_kullanici, yonetici,
)
from veritabani import Kullanici, KullaniciAyari, OturumYapici, oturum, simdi, tablolari_olustur  # noqa: E402

log = logging.getLogger("gunluk-rapor")
sablonlar = Jinja2Templates(directory=Path(__file__).with_name("templates"))
STATIK = Path(__file__).with_name("static")
mimetypes.add_type("application/manifest+json", ".webmanifest")
EPOSTA_BICIMI = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EN_KISA_SIFRE = 10


def ilk_yoneticiyi_olustur() -> None:
    with OturumYapici() as db:
        if db.scalar(select(func.count()).select_from(Kullanici)):
            return
        eposta = (os.environ.get("ADMIN_EPOSTA") or "").strip().lower()
        sifre = os.environ.get("ADMIN_SIFRE") or ""
        if not (eposta and sifre):
            log.warning("Hiç kullanıcı yok ve ADMIN_EPOSTA / ADMIN_SIFRE tanımlı değil; kimse giriş yapamaz. "
                        "Bu iki değişkeni ekleyip uygulamayı yeniden başlatın.")
            return
        db.add(Kullanici(eposta=eposta, ad="Yönetici", sifre_hash=guvenlik.sifre_ozeti(sifre),
                         rol="admin", aktif=True, sifre_degistirmeli=False))
        db.commit()
        log.warning("İlk yönetici hesabı oluşturuldu.")


@asynccontextmanager
async def yasam(_: FastAPI):
    tablolari_olustur()
    ilk_yoneticiyi_olustur()
    yield


app = FastAPI(title="Günlük rapor", lifespan=yasam, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(api.router)
app.mount("/static", StaticFiles(directory=STATIK), name="static")


@app.exception_handler(GirisGerekli)
def giris_gerekli(request: Request, _: GirisGerekli):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Oturum açılmamış"}, status_code=401)
    return RedirectResponse("/giris", status_code=303)


@app.exception_handler(SifreDegistirilmeli)
def sifre_degistirilmeli(request: Request, _: SifreDegistirilmeli):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Önce şifrenizi değiştirin"}, status_code=403)
    return RedirectResponse("/sifre", status_code=303)


class KurulumGerekli(Exception):
    pass


@app.exception_handler(KurulumGerekli)
def kurulum_gerekli(request: Request, _: KurulumGerekli):
    return RedirectResponse("/kurulum", status_code=303)


def kurulmus_kullanici(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> Kullanici:
    """HTML sayfaları için: ilk kurulum bitmediyse sihirbaza yönlendirir. API uçları, /cikis ve /sifre bunu kullanmaz."""
    a = db.get(KullaniciAyari, kullanici.id)
    if not (a and a.kurulum_tamam):
        raise KurulumGerekli()
    return kullanici


def kurulmus_yonetici(kullanici: Kullanici = Depends(yonetici), _: Kullanici = Depends(kurulmus_kullanici)) -> Kullanici:
    return kullanici


def sayfa(request: Request, ad: str, durum: int = 200, **baglam) -> HTMLResponse:
    return sablonlar.TemplateResponse(request, ad, baglam, status_code=durum)


@app.get("/api/saglik")
def saglik() -> dict:
    return {"ok": True, "son_hatirlat_ping": api.son_hatirlat_ping}


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """Kökten sunulur ki kapsam '/' olsun; yalnız bildirim için, önbellek yok."""
    return FileResponse(STATIK / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- giriş / çıkış / şifre

@app.get("/giris", response_class=HTMLResponse)
def giris_sayfasi(request: Request, db: Session = Depends(oturum)):
    if oturumdaki_kullanici(request, db):
        return RedirectResponse("/", status_code=303)
    return sayfa(request, "giris.html", eposta="", hata=None)


@app.post("/giris", response_class=HTMLResponse)
def giris_yap(
    request: Request,
    eposta: str = Form(""), sifre: str = Form(""), hatirla: str | None = Form(None),
    db: Session = Depends(oturum),
):
    eposta = eposta.strip().lower()
    kullanici = db.scalar(select(Kullanici).where(Kullanici.eposta == eposta)) if eposta else None
    dogru = guvenlik.sifre_dogru(sifre, kullanici.sifre_hash if kullanici else None)
    if not (kullanici and dogru):
        return sayfa(request, "giris.html", 401, eposta=eposta, hata="E-posta veya şifre hatalı")
    if not kullanici.aktif:
        return sayfa(request, "giris.html", 403, eposta=eposta, hata="Bu hesap pasif; yöneticiyle görüşün")
    kullanici.son_giris = simdi()
    db.commit()
    yanit = RedirectResponse("/sifre" if kullanici.sifre_degistirmeli else "/", status_code=303)
    cerez_yaz(yanit, kullanici, bool(hatirla))
    return yanit


@app.post("/cikis")
def cikis():
    yanit = RedirectResponse("/giris", status_code=303)
    cerez_sil(yanit)
    return yanit


@app.get("/sifre", response_class=HTMLResponse)
def sifre_sayfasi(request: Request, kullanici: Kullanici = Depends(giris_yapmis)):
    return sayfa(request, "sifre.html", kullanici=kullanici, hata=None)


@app.post("/sifre", response_class=HTMLResponse)
def sifre_degistir(
    request: Request,
    mevcut: str = Form(""), yeni: str = Form(""), tekrar: str = Form(""),
    kullanici: Kullanici = Depends(giris_yapmis), db: Session = Depends(oturum),
):
    hata = None
    if not guvenlik.sifre_dogru(mevcut, kullanici.sifre_hash):
        hata = "Mevcut şifre hatalı"
    elif len(yeni) < EN_KISA_SIFRE:
        hata = f"Yeni şifre en az {EN_KISA_SIFRE} karakter olmalı"
    elif yeni != tekrar:
        hata = "Yeni şifre ile tekrarı aynı değil"
    elif yeni == mevcut:
        hata = "Yeni şifre mevcut şifreyle aynı olamaz"
    if hata:
        return sayfa(request, "sifre.html", 400, kullanici=kullanici, hata=hata)
    kullanici.sifre_hash = guvenlik.sifre_ozeti(yeni)
    kullanici.sifre_degistirmeli = False
    db.commit()
    yanit = RedirectResponse("/", status_code=303)
    cerez_yaz(yanit, kullanici, bool((oturum_verisi(request) or {}).get("r")))
    return yanit


# ---------------------------------------------------------------- sayfalar

@app.get("/", response_class=HTMLResponse)
def ana_sayfa(request: Request, kullanici: Kullanici = Depends(kurulmus_kullanici)):
    return sayfa(request, "index.html", kullanici=kullanici)


@app.get("/gecmis", response_class=HTMLResponse)
def gecmis_sayfasi(request: Request, kullanici: Kullanici = Depends(kurulmus_kullanici)):
    return sayfa(request, "gecmis.html", kullanici=kullanici, bugun=api.bugun().isoformat())


@app.get("/ayarlar", response_class=HTMLResponse)
def ayarlar_sayfasi(request: Request, kullanici: Kullanici = Depends(kurulmus_kullanici)):
    return sayfa(request, "ayarlar.html", kullanici=kullanici)


@app.get("/kurulum", response_class=HTMLResponse)
def kurulum_sayfasi(request: Request, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)):
    """Her zaman açılır (Ayarlar'daki "Kurulumu yeniden aç" da buraya gelir); Bitir kurulum_tamam'ı true bırakır."""
    a = db.get(KullaniciAyari, kullanici.id)
    return sayfa(request, "kurulum.html", kullanici=kullanici, kurulum_tamam=bool(a and a.kurulum_tamam))


# ---------------------------------------------------------------- yönetim

def yonetim_sayfasi(request: Request, ben: Kullanici, db: Session, durum: int = 200, hata=None, bilgi=None):
    kullanicilar = db.scalars(select(Kullanici).order_by(Kullanici.olusturma, Kullanici.id)).all()
    ayarlar = {a.user_id: a for a in db.scalars(select(KullaniciAyari))}
    cagrilar = api.aylik_claude_cagrilari(db, api.bugun())
    ozet = {}
    for k in kullanicilar:
        a = ayarlar.get(k.id) or KullaniciAyari(user_id=k.id)
        ozet[k.id] = {
            "kurulum": bool(a.kurulum_tamam),
            "kaynaklar": [ad for ad, acik in api.kaynak_durumu(a).items() if acik],
            "ay_cagri": cagrilar.get(k.id, 0),
        }
    return sayfa(request, "yonetim.html", durum, kullanici=ben, kullanicilar=kullanicilar, ozet=ozet, hata=hata, bilgi=bilgi)


def hedef_kullanici(db: Session, kullanici_id: int) -> Kullanici | None:
    return db.get(Kullanici, kullanici_id)


@app.get("/yonetim", response_class=HTMLResponse)
def yonetim(request: Request, ben: Kullanici = Depends(kurulmus_yonetici), db: Session = Depends(oturum)):
    return yonetim_sayfasi(request, ben, db)


@app.post("/yonetim/davet", response_class=HTMLResponse)
def davet_et(
    request: Request, ad: str = Form(""), eposta: str = Form(""),
    ben: Kullanici = Depends(yonetici), db: Session = Depends(oturum),
):
    ad, eposta = ad.strip(), eposta.strip().lower()
    if not ad or not EPOSTA_BICIMI.match(eposta):
        return yonetim_sayfasi(request, ben, db, 400, hata="Ad ve geçerli bir e-posta girin")
    if db.scalar(select(Kullanici).where(Kullanici.eposta == eposta)):
        return yonetim_sayfasi(request, ben, db, 409, hata=f"{eposta} zaten kayıtlı")
    gecici = guvenlik.gecici_sifre()
    db.add(Kullanici(eposta=eposta, ad=ad, sifre_hash=guvenlik.sifre_ozeti(gecici),
                     rol="uye", aktif=True, sifre_degistirmeli=True))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return yonetim_sayfasi(request, ben, db, 409, hata=f"{eposta} zaten kayıtlı")
    return yonetim_sayfasi(request, ben, db, bilgi={"baslik": f"{ad} davet edildi", "eposta": eposta, "sifre": gecici})


@app.post("/yonetim/{kullanici_id}/aktiflik", response_class=HTMLResponse)
def aktifligi_degistir(
    request: Request, kullanici_id: int, ben: Kullanici = Depends(yonetici), db: Session = Depends(oturum),
):
    hedef = hedef_kullanici(db, kullanici_id)
    if hedef is None:
        return yonetim_sayfasi(request, ben, db, 404, hata="Kullanıcı bulunamadı")
    if hedef.id == ben.id:
        return yonetim_sayfasi(request, ben, db, 400, hata="Kendi hesabınızı pasife alamazsınız")
    hedef.aktif = not hedef.aktif
    db.commit()
    return RedirectResponse("/yonetim", status_code=303)


@app.post("/yonetim/{kullanici_id}/sifirla", response_class=HTMLResponse)
def sifreyi_sifirla(
    request: Request, kullanici_id: int, ben: Kullanici = Depends(yonetici), db: Session = Depends(oturum),
):
    hedef = hedef_kullanici(db, kullanici_id)
    if hedef is None:
        return yonetim_sayfasi(request, ben, db, 404, hata="Kullanıcı bulunamadı")
    gecici = guvenlik.gecici_sifre()
    hedef.sifre_hash = guvenlik.sifre_ozeti(gecici)
    hedef.sifre_degistirmeli = True
    db.commit()
    return yonetim_sayfasi(request, ben, db, bilgi={
        "baslik": f"{hedef.ad} için yeni geçici şifre", "eposta": hedef.eposta, "sifre": gecici,
    })
