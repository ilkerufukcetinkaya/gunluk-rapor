"""Günlük rapor servisi: hesaplar, sayfalar ve API. Her kullanıcı kendi Gmail/GitHub ayarlarıyla çalışır."""
import hmac
import logging
import mimetypes
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

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
import kimlik  # noqa: E402
import servisler  # noqa: E402
from kimlik import (  # noqa: E402
    GirisGerekli, SifreDegistirilmeli, aktif_kullanici, cerez_sil, cerez_yaz, giris_yapmis, oturum_verisi,
    oturumdaki_kullanici, yonetici,
)
from veritabani import (  # noqa: E402
    Kullanici, KullaniciAyari, OturumYapici, kullaniciyi_sil, oturum, simdi, tablolari_olustur,
)

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
    return sayfa(request, "ayarlar.html", kullanici=kullanici, google_ayarli=servisler.google_ayarli())


@app.get("/kurulum", response_class=HTMLResponse)
def kurulum_sayfasi(request: Request, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)):
    """Her zaman açılır (Ayarlar'daki "Kurulumu yeniden aç" da buraya gelir); Bitir kurulum_tamam'ı true bırakır."""
    a = db.get(KullaniciAyari, kullanici.id)
    return sayfa(request, "kurulum.html", kullanici=kullanici, kurulum_tamam=bool(a and a.kurulum_tamam),
                 google_ayarli=servisler.google_ayarli())


# ---------------------------------------------------------------- Google ile bağlan (OAuth 2.0 + PKCE)

GOOGLE_DONUSLER = ("/ayarlar", "/kurulum")  # ?donus= beyaz listesi; ilki varsayılan


def google_yonlendirme_adresi(request: Request) -> str:
    """APP_URL + /oauth/google/geri. Yerelde (localhost) ya da APP_URL yoksa isteğin kökü: http://localhost:8765."""
    yerel = request.url.hostname in ("localhost", "127.0.0.1")
    kok = ("" if yerel else (os.environ.get("APP_URL") or "").strip().rstrip("/")) or str(request.base_url).rstrip("/")
    return kok + "/oauth/google/geri"


def google_donusu(hedef: str, **parametre) -> RedirectResponse:
    yanit = RedirectResponse(f"{hedef}?{urlencode(parametre)}", status_code=303)
    kimlik.pkce_cerezi_sil(yanit)
    return yanit


@app.get("/oauth/google/basla")
def google_basla(request: Request, donus: str = "", kullanici: Kullanici = Depends(aktif_kullanici)):
    hedef = donus if donus in GOOGLE_DONUSLER else GOOGLE_DONUSLER[0]
    if not servisler.google_ayarli():
        return google_donusu(hedef, google="hata", neden="Google bağlantısı bu sunucuda ayarlı değil")
    dogrulayici, challenge = servisler.pkce_cifti()
    state, nonce = kimlik.google_state_uret(kullanici.id, hedef)
    yanit = RedirectResponse(servisler.google_yetki_adresi(
        google_yonlendirme_adresi(request), state, challenge, login_hint=kullanici.eposta), status_code=303)
    kimlik.pkce_cerezi_yaz(yanit, nonce, dogrulayici)
    return yanit


@app.get("/oauth/google/geri")
def google_geri(
    request: Request, code: str = "", state: str = "", error: str = "",
    kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
):
    """state (imza, 10 dk, oturumdaki kullanıcı) ve PKCE çerezi doğrulanır; hata/iptal → donus?google=hata&neden=…"""
    veri = kimlik.google_state_coz(state)
    hedef = veri["d"] if veri and veri.get("d") in GOOGLE_DONUSLER else GOOGLE_DONUSLER[0]
    if veri is None:
        return google_donusu(hedef, google="hata", neden="İstek geçersiz ya da süresi doldu; yeniden deneyin")
    if veri["u"] != kullanici.id:
        return google_donusu(hedef, google="hata", neden="Oturum eşleşmedi; yeniden deneyin")
    if error:
        neden = "İzin verilmedi" if error == "access_denied" else "Google isteği reddetti"
        return google_donusu(hedef, google="hata", neden=neden)
    pkce = kimlik.pkce_cerezi_oku(request)
    if not pkce or not hmac.compare_digest(pkce["n"], veri["n"]):
        return google_donusu(hedef, google="hata", neden="Doğrulama başarısız; yeniden deneyin")
    if not code or not servisler.google_ayarli():
        return google_donusu(hedef, google="hata", neden="Google yetki kodu gelmedi")
    try:
        token = servisler.google_kod_takas(code, pkce["v"], google_yonlendirme_adresi(request))
    except servisler.GoogleHatasi as e:
        log.warning("google baglanti hatasi user=%s neden=%s", kullanici.id, str(e)[:200])
        return google_donusu(hedef, google="hata", neden=str(e))
    api.google_baglantisini_kaydet(db, kullanici, token)
    return google_donusu(hedef, google="bagli")


@app.post("/oauth/google/kaldir")
def google_kaldir(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    return api.ayar_ozeti(api.google_baglantisini_kaldir(db, kullanici), kullanici)


# ---------------------------------------------------------------- yönetim

def eposta_gonderilebilir() -> bool:
    """Davet/sıfırlama e-postaları yalnız Resend ile gider; Gmail yedeği kişisel ayar olduğu için sayılmaz."""
    return bool((os.environ.get("RESEND_API_KEY") or "").strip())


def yonetim_sayfasi(request: Request, ben: Kullanici, db: Session, durum: int = 200, hata=None, bilgi=None):
    kullanicilar = db.scalars(select(Kullanici).order_by(Kullanici.olusturma, Kullanici.id)).all()
    ayarlar = {a.user_id: a for a in db.scalars(select(KullaniciAyari))}
    cagrilar = api.aylik_claude_cagrilari(db, api.bugun())
    hatirlatmalar = api.son_hatirlatmalar(db)
    ozet = {}
    for k in kullanicilar:
        a = ayarlar.get(k.id) or KullaniciAyari(user_id=k.id)
        ozet[k.id] = {
            "kurulum": bool(a.kurulum_tamam),
            "kaynaklar": [ad for ad, acik in api.kaynak_durumu(a).items() if acik],
            "ay_cagri": cagrilar.get(k.id, 0),
            "son_hatirlatma": hatirlatmalar.get(k.id),
        }
    return sayfa(request, "yonetim.html", durum, kullanici=ben, kullanicilar=kullanicilar, ozet=ozet,
                 hata=hata, bilgi=bilgi, eposta_acik=eposta_gonderilebilir())


def hedef_kullanici(db: Session, kullanici_id: int) -> Kullanici | None:
    return db.get(Kullanici, kullanici_id)


def tek_yonetici_kaldi(db: Session) -> bool:
    return db.scalar(select(func.count()).select_from(Kullanici).where(Kullanici.rol == "admin")) <= 1


def davet_epostasi_yolla(db: Session, hedef: Kullanici, sifre: str, ben: Kullanici, sifirlama: bool) -> dict:
    """{"ok", "neden"}. Gönderim hatası yükseltilmez: kullanıcı ve yeni şifre yerinde kalır, neden ekrana çıkar."""
    if not eposta_gonderilebilir():
        return {"ok": False, "neden": "E-posta gönderimi yapılandırılmamış (RESEND_API_KEY yok)"}
    konu, metin = servisler.davet_eposta_metni(
        hedef.ad, hedef.eposta, sifre, f"{api.app_url()}/giris", ben.ad, sifirlama)
    try:
        hata = servisler.eposta_gonder(hedef.eposta, konu, metin, yanit_adresi=ben.eposta, gonderen_adi=ben.ad)
    except Exception as e:
        hata = f"E-posta gönderilemedi ({e.__class__.__name__})"
    if hata:
        log.warning("davet epostası user=%s neden=%s", hedef.id, hata[:200])
    else:
        hedef.davet_eposta_tarihi = simdi()
        db.commit()
        log.info("davet epostası user=%s neden=gönderildi", hedef.id)
    return {"ok": not hata, "neden": hata}


@app.get("/yonetim", response_class=HTMLResponse)
def yonetim(request: Request, ben: Kullanici = Depends(kurulmus_yonetici), db: Session = Depends(oturum)):
    return yonetim_sayfasi(request, ben, db)


@app.post("/yonetim/davet", response_class=HTMLResponse)
def davet_et(
    request: Request, ad: str = Form(""), eposta: str = Form(""), mail_gonder: str | None = Form(None),
    ben: Kullanici = Depends(yonetici), db: Session = Depends(oturum),
):
    ad, eposta = ad.strip(), eposta.strip().lower()
    if not ad or not EPOSTA_BICIMI.match(eposta):
        return yonetim_sayfasi(request, ben, db, 400, hata="Ad ve geçerli bir e-posta girin")
    if db.scalar(select(Kullanici).where(Kullanici.eposta == eposta)):
        return yonetim_sayfasi(request, ben, db, 409, hata=f"{eposta} zaten kayıtlı")
    gecici = guvenlik.gecici_sifre()
    yeni = Kullanici(eposta=eposta, ad=ad, sifre_hash=guvenlik.sifre_ozeti(gecici),
                     rol="uye", aktif=True, sifre_degistirmeli=True)
    db.add(yeni)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return yonetim_sayfasi(request, ben, db, 409, hata=f"{eposta} zaten kayıtlı")
    bilgi = {"baslik": f"{ad} davet edildi", "eposta": eposta, "sifre": gecici, "hedef": yeni.id, "tur": "davet"}
    if mail_gonder:
        bilgi["gonderim"] = davet_epostasi_yolla(db, yeni, gecici, ben, sifirlama=False)
    return yonetim_sayfasi(request, ben, db, bilgi=bilgi)


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


@app.delete("/yonetim/{kullanici_id}", response_class=HTMLResponse)
@app.post("/yonetim/{kullanici_id}/sil", response_class=HTMLResponse)
def kullaniciyi_kaldir(
    request: Request, kullanici_id: int, ben: Kullanici = Depends(yonetici), db: Session = Depends(oturum),
):
    """Kullanıcıyı ve ona bağlı her şeyi siler; kayıt gittiği için açık oturumları da düşer."""
    hedef = hedef_kullanici(db, kullanici_id)
    if hedef is None:
        return yonetim_sayfasi(request, ben, db, 404, hata="Kullanıcı bulunamadı")
    if hedef.rol == "admin" and tek_yonetici_kaldi(db):
        return yonetim_sayfasi(request, ben, db, 400, hata="Son yöneticiyi silemezsin")
    if hedef.id == ben.id:
        return yonetim_sayfasi(request, ben, db, 400, hata="Kendi hesabınızı silemezsiniz")
    ad, eposta = hedef.ad, hedef.eposta
    kullaniciyi_sil(db, hedef)
    log.warning("kullanıcı silindi id=%s ad=%s silen=%s", kullanici_id, ad, ben.id)
    return yonetim_sayfasi(request, ben, db, bilgi={"baslik": f"{ad} silindi", "eposta": eposta, "silindi": True})


@app.post("/yonetim/{kullanici_id}/sifirla", response_class=HTMLResponse)
def sifreyi_sifirla(
    request: Request, kullanici_id: int, mail_gonder: str | None = Form(None),
    ben: Kullanici = Depends(yonetici), db: Session = Depends(oturum),
):
    hedef = hedef_kullanici(db, kullanici_id)
    if hedef is None:
        return yonetim_sayfasi(request, ben, db, 404, hata="Kullanıcı bulunamadı")
    gecici = guvenlik.gecici_sifre()
    hedef.sifre_hash = guvenlik.sifre_ozeti(gecici)
    hedef.sifre_degistirmeli = True
    db.commit()
    bilgi = {"baslik": f"{hedef.ad} için yeni geçici şifre", "eposta": hedef.eposta, "sifre": gecici,
             "hedef": hedef.id, "tur": "sifirlama"}
    if mail_gonder:
        bilgi["gonderim"] = davet_epostasi_yolla(db, hedef, gecici, ben, sifirlama=True)
    return yonetim_sayfasi(request, ben, db, bilgi=bilgi)


@app.post("/yonetim/{kullanici_id}/davet-eposta", response_class=HTMLResponse)
def davet_epostasi_gonder(
    request: Request, kullanici_id: int, sifre: str = Form(...), tur: str = Form("davet"),
    ben: Kullanici = Depends(yonetici), db: Session = Depends(oturum),
):
    """Geçici şifre sunucuda saklanmaz; yalnız ekranda görüldüğü an form gövdesiyle geri gelir."""
    hedef = hedef_kullanici(db, kullanici_id)
    if hedef is None:
        return yonetim_sayfasi(request, ben, db, 404, hata="Kullanıcı bulunamadı")
    sifirlama = tur == "sifirlama"
    bilgi = {
        "baslik": f"{hedef.ad} için yeni geçici şifre" if sifirlama else f"{hedef.ad} davet edildi",
        "eposta": hedef.eposta, "sifre": sifre, "hedef": hedef.id, "tur": tur,
    }
    if not eposta_gonderilebilir():
        bilgi["gonderim"] = {"ok": False, "neden": "E-posta gönderimi yapılandırılmamış (RESEND_API_KEY yok)"}
        return yonetim_sayfasi(request, ben, db, 400, bilgi=bilgi)
    bilgi["gonderim"] = davet_epostasi_yolla(db, hedef, sifre, ben, sifirlama)
    return yonetim_sayfasi(request, ben, db, bilgi=bilgi)
