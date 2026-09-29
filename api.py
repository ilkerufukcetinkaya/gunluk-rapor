"""JSON uçları. Kullanıcı her zaman oturumdan gelir; tüm sorgular user_id ile süzülür."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import re
import threading
import time as saat_
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import guvenlik
import servisler
from kimlik import aktif_kullanici
from veritabani import (
    OZET_BICIMLERI, RAPOR_TURLERI, ClaudeKullanim, GunlukIfade, HatirlatmaGonderimi, Kategori, Kullanici, KullaniciAyari,
    Madde, PushAbonelik, Rapor, RaporDuzeni, oturum, simdi,
)

router = APIRouter(prefix="/api")
log = logging.getLogger("gunluk-rapor")

REPO_BICIMI = re.compile(r"^[\w.-]+/[\w.-]+$")
EPOSTA_BICIMI = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SAAT_BICIMI = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
ALAN_BICIMI = re.compile(r"^[\w-]+(\.[\w-]+)+$")
VARSAYILAN_SAAT = time(17, 0)
VARSAYILAN_OTOMATIK_SAAT = time(18, 30)
VARSAYILAN_GUNLER = "1,2,3,4,5"
TEMEL_KAYNAKLAR = ("gmail", "github", "medusa")
GOOGLE_KAYNAKLARI = ("takvim", "drive")  # yalnız Google bağlantısıyla (ve kapsamı verilmişse) açılabilir
MICROSOFT_KAYNAKLARI = ("outlook", "outlook_takvim", "onedrive")  # yalnız Microsoft bağlantısıyla (ve kapsamıyla)
OAUTH_KAYNAKLARI = GOOGLE_KAYNAKLARI + MICROSOFT_KAYNAKLARI
KAYNAKLAR = TEMEL_KAYNAKLAR + OAUTH_KAYNAKLARI
# Kategori pilleri: "Takvim" iki sağlayıcıyı da kapsar, ayrı Outlook takvimi pili yok.
KATEGORI_KAYNAKLARI = tuple(k for k in KAYNAKLAR if k != "outlook_takvim")
YAKINDA = {"medusa"}  # arayüzde "yakında"; açılamaz
# Bulunan maddenin kaynak alanı → hangi kaynak modülünden geldiği ('medusa' tarihsel olarak GitHub commit'leridir).
BULUNAN_KAYNAGI = {"eposta": "gmail", "outlook": "outlook", "medusa": "github", "takvim": "takvim", "drive": "drive",
                   "onedrive": "onedrive"}
# Bulunan maddeyi görünür kılan kaynak modülleri: toplantılar iki sağlayıcıdan da 'takvim' olarak gelir.
BULUNAN_MODULLERI = {**{k: (m,) for k, m in BULUNAN_KAYNAGI.items()}, "takvim": ("takvim", "outlook_takvim")}
# Kendi kategorisi seçilmemiş Microsoft kaynağı Google karşılığının kategorisine düşer.
KATEGORI_YEDEGI = {"outlook": "gmail", "onedrive": "drive"}
GOOGLE_YAKINDA_MESAJI = "Google bağlantın yarın yenilenmeli"
GOOGLE_UYARI_SURESI = timedelta(hours=24)
GOOGLE_BASLA = "/oauth/google/basla"
MICROSOFT_BASLA = "/oauth/microsoft/basla"
GUNLUK_CLAUDE_SINIRI = 8
KOTA_MESAJI = "Bugünkü düzeltme hakkı doldu, yarın devam"
GECMIS_GUN = 30  # geçmiş gün düzenleme: bugün … 30 gün önce
RAPOR_BICIMLERI = ("kategorili", "duz")
# Rapora elle yazılmış gibi giren bugün satırları: elle, e-postayla gelen not, sesle eklenen.
ELLE_KAYNAKLARI = ("elle", "not", "ses")
SESLI_NOT_SINIRI = 5000  # karakter
AD_EN_KISA, AD_EN_UZUN = 2, 60  # görünen ad
# Raporlarda ve e-postalarda görünmemesi gereken yer tutucu adlar (Ayarlar'da sarı ipucu)
YER_TUTUCU_ADLAR = frozenset({"yönetici", "yonetici", "admin", "administrator", "kullanıcı", "kullanici"})
ESLEME_KAYNAK_EN_KISA, ESLEME_KAYNAK_EN_UZUN, ESLEME_HEDEF_EN_UZUN, ESLEME_SINIRI = 2, 60, 80, 50


def bugun() -> date:
    return servisler.istanbul_bugun()


def gun_sec(tarih: str | date | None) -> date:
    """Gün kapsamlı uçların ?tarih= değeri; boşsa bugün (Istanbul). Aralık dışı ya da okunamayan tarih 400."""
    bugun_ = bugun()
    if tarih is None or (isinstance(tarih, str) and not tarih.strip()):
        return bugun_
    if isinstance(tarih, str):
        try:
            tarih = date.fromisoformat(tarih.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="Tarih YYYY-AA-GG biçiminde olmalı") from None
    if not bugun_ - timedelta(days=GECMIS_GUN) <= tarih <= bugun_:
        raise HTTPException(status_code=400, detail=f"Yalnız bugün ve son {GECMIS_GUN} gün düzenlenebilir")
    return tarih


def ai_anahtari() -> str:
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


# ---------------------------------------------------------------- yardımcılar

def ayar_satiri(db: Session, kullanici: Kullanici) -> KullaniciAyari:
    return db.get(KullaniciAyari, kullanici.id) or KullaniciAyari(user_id=kullanici.id)


def gunleri_ayristir(deger: str | list) -> list[int]:
    parcalar = deger.split(",") if isinstance(deger, str) else deger
    gunler = set()
    for g in parcalar:
        if isinstance(g, str):
            g = g.strip()
            if not g:
                continue
            if not g.isdigit():
                raise HTTPException(status_code=422, detail="Hatırlatma günleri 1–7 arası olmalı")
            g = int(g)
        if isinstance(g, bool) or not isinstance(g, int) or not 1 <= g <= 7:
            raise HTTPException(status_code=422, detail="Hatırlatma günleri 1–7 arası olmalı")
        gunler.add(g)
    return sorted(gunler)


def hatirlatma_ayari(a: KullaniciAyari) -> dict:
    """Satır yoksa ya da kolon boşsa varsayılanlar."""
    return {
        "saat": a.hatirlatma_saat or VARSAYILAN_SAAT,
        "gunler": gunleri_ayristir(VARSAYILAN_GUNLER if a.hatirlatma_gunler is None else a.hatirlatma_gunler),
        "push": a.hatirlatma_push is not False,
        "eposta": a.hatirlatma_eposta is not False,
        "adres": a.hatirlatma_eposta_adres or "",
    }


def otomatik_ayari(a: KullaniciAyari) -> dict:
    """O1 ayarları; günler hatırlatmanınkiyle aynıdır."""
    return {
        "acik": a.otomatik_gonder is True,
        "saat": a.otomatik_saat or VARSAYILAN_OTOMATIK_SAAT,
        "patron_eposta": a.patron_eposta or "",
        "patron_adi": a.patron_adi or "",
        "kopya_bana": a.otomatik_kopya_bana is not False,
    }


def kaynak_durumu(a: KullaniciAyari) -> dict[str, bool]:
    """Kaydedilmiş seçim; kaynak için seçim yoksa şifresi/token'ı (Gmail'de Google izni) kayıtlıysa açık sayılır.
    Takvim ve Drive yalnız GOOGLE_*, Outlook / Outlook takvimi / OneDrive yalnız MICROSOFT_* tanımlıyken listelenir ve
    yalnız izni verilmiş bağlantıyla açık olur."""
    secim = a.kaynaklar if isinstance(a.kaynaklar, dict) else {}
    kapsam = google_kapsamlari(a) | microsoft_kapsamlari(a)
    varsayilan = {"gmail": bool(a.gmail_sifre_enc) or "gmail" in kapsam, "github": bool(a.github_token_enc)}
    anahtarlar = TEMEL_KAYNAKLAR + (GOOGLE_KAYNAKLARI if servisler.google_ayarli() else ()) \
        + (MICROSOFT_KAYNAKLARI if servisler.microsoft_ayarli() else ())
    return {
        k: k not in YAKINDA and (k not in OAUTH_KAYNAKLARI or k in kapsam) and bool(secim.get(k, varsayilan.get(k, False)))
        for k in anahtarlar
    }


# ---------------------------------------------------------------- OAuth bağlantıları (Google, Microsoft)

class Saglayici:
    """Google ve Microsoft bağlantısının ortak işleyişi (token önbelleği, yenileme, tarama ayarı) için sağlayıcıya
    özgü parçalar. servisler'deki işlevler çağrı anında bulunur (testler onları değiştirebilir)."""

    def __init__(self, ad: str, onek: str, basla: str):
        self.ad, self.onek, self.basla = ad, onek, basla  # onek: user_settings kolon öneki
        self.erisim: dict[int, tuple[str, float]] = {}  # user_id → (access token, geçerlilik sonu epoch); yalnız bellekte
        self.sinif = ad.capitalize()

    def ayarli(self) -> bool:
        return getattr(servisler, f"{self.ad}_ayarli")()

    def kisa_kapsamlar(self, kapsamlar) -> list[str]:
        return getattr(servisler, f"{self.ad}_kisa_kapsamlar")(kapsamlar)

    def yenile(self, refresh: str) -> tuple:
        return getattr(servisler, f"{self.ad}_yenile")(refresh)

    @property
    def yenilenmeli(self) -> type:
        return getattr(servisler, f"{self.sinif}Yenilenmeli")

    @property
    def hata(self) -> type:
        return getattr(servisler, f"{self.sinif}Hatasi")

    @property
    def yenile_mesaji(self) -> str:
        return getattr(servisler, f"{self.ad.upper()}_YENILE_MESAJI")

    def alan(self, a: KullaniciAyari, ad: str):
        return getattr(a, f"{self.onek}_{ad}")


GOOGLE = Saglayici("google", "google", GOOGLE_BASLA)
MICROSOFT = Saglayici("microsoft", "ms", MICROSOFT_BASLA)
_google_erisim = GOOGLE.erisim
_ms_erisim = MICROSOFT.erisim


def oauth_bagli(a: KullaniciAyari, s: Saglayici) -> bool:
    """Refresh token kayıtlı ('yenile' durumunda da); sağlayıcının değişkenleri tanımlı değilse bağlantı yok sayılır."""
    return s.ayarli() and bool(s.alan(a, "refresh_enc"))


def oauth_kapsamlari(a: KullaniciAyari, s: Saglayici) -> set[str]:
    return set(s.kisa_kapsamlar(s.alan(a, "kapsamlar"))) if oauth_bagli(a, s) else set()


def oauth_erisim_tokeni(db: Session, a: KullaniciAyari, s: Saglayici) -> str:
    """Önbellekteki access token; süresi dolmuşsa (ya da 60 sn kaldıysa) refresh. Sağlayıcı yeni refresh token dönerse
    (Microsoft rotasyonu) şifreli olarak kaydedilir. invalid_grant → durum 'yenile' yazılır ve Yenilenmeli yükselir."""
    kayit = s.erisim.get(a.user_id)
    if kayit and kayit[1] > saat_.time() + 60:
        return kayit[0]
    try:
        with _kullanici_kilidi(a.user_id, s.ad):
            kayit = s.erisim.get(a.user_id)  # kilidi bekleyen istek, öncekinin yenilediği token'ı kullanır
            if kayit and kayit[1] > saat_.time() + 60:
                return kayit[0]
            refresh = guvenlik.coz(s.alan(a, "refresh_enc"))
            if not refresh:
                raise s.yenilenmeli(s.yenile_mesaji)
            token, sure, *yeni = s.yenile(refresh)
    except s.yenilenmeli:
        s.erisim.pop(a.user_id, None)
        setattr(a, f"{s.onek}_durum", "yenile")
        db.commit()
        log.warning("%s baglantisi yenilenmeli user=%s", s.ad, a.user_id)
        raise
    if yeni and yeni[0] and yeni[0] != refresh:
        setattr(a, f"{s.onek}_refresh_enc", guvenlik.sifrele(yeni[0]))
        db.commit()
    s.erisim[a.user_id] = (token, saat_.time() + sure)
    return token


def oauth_tarama_ayari(db: Session, a: KullaniciAyari, s: Saglayici) -> dict:
    """raporu_uret'in 'google' / 'microsoft' ayarı. Bu bağlantıyla taranacak açık kaynak yoksa token alınmaz."""
    if not oauth_bagli(a, s):
        return {}
    kapsam = oauth_kapsamlari(a, s)
    g = {"kapsamlar": sorted(kapsam), "eposta": s.alan(a, "eposta") or ""}
    if not any(acik and k in kapsam for k, acik in kaynak_durumu(a).items()):
        return g
    if s.alan(a, "durum") == "yenile":
        return {**g, "yenile": True}
    try:
        return {**g, "token": oauth_erisim_tokeni(db, a, s)}
    except s.yenilenmeli:
        return {**g, "yenile": True}
    except s.hata as e:
        return {**g, "hata": str(e)}


def _baglanti_alanlarini_yaz(a: KullaniciAyari, s: Saglayici, token: dict | None) -> None:
    """token verilirse onay dönüşünün alanları yazılır (refresh token şifreli), None ise hepsi silinir."""
    if token is None:
        for alan in ("refresh_enc", "eposta", "baglanti", "durum", "kapsamlar"):
            setattr(a, f"{s.onek}_{alan}", None)
        return
    setattr(a, f"{s.onek}_refresh_enc", guvenlik.sifrele(token["refresh_token"]))
    setattr(a, f"{s.onek}_eposta", token["eposta"])
    setattr(a, f"{s.onek}_baglanti", simdi())
    setattr(a, f"{s.onek}_durum", "bagli")
    setattr(a, f"{s.onek}_kapsamlar", list(token["kapsamlar"]))


def yenile_uyarisi(a: KullaniciAyari, s: Saglayici) -> dict | None:
    """'yenile' durumu: Bugün'de turuncu şerit, hatırlatmada satır."""
    if not (oauth_bagli(a, s) and s.alan(a, "durum") == "yenile"):
        return None
    return {"tur": "doldu", "metin": s.yenile_mesaji, "eylem": "Yeniden bağlan", "adres": s.basla}


def baglanti_uyarilari(a: KullaniciAyari, an: datetime | None = None) -> list[dict]:
    return [u for u in (google_uyari(a, an), microsoft_uyari(a)) if u]


# ---------------------------------------------------------------- Google bağlantısı

def google_bagli(a: KullaniciAyari) -> bool:
    return oauth_bagli(a, GOOGLE)


def google_kapsamlari(a: KullaniciAyari) -> set[str]:
    """Verilen izinlerin kısa adları: gmail / takvim / drive."""
    return oauth_kapsamlari(a, GOOGLE)


def google_bitis(a: KullaniciAyari) -> datetime | None:
    """Test modunda bağlantı onaydan 7 gün sonra düşer; GOOGLE_TEST_MODU=0 iken bitiş yok."""
    if not (servisler.google_test_modu() and google_bagli(a) and a.google_baglanti):
        return None
    return utc(a.google_baglanti) + servisler.GOOGLE_TEST_SURESI


def google_uyari(a: KullaniciAyari, an: datetime | None = None) -> dict | None:
    """Bugün şeridi ve hatırlatma için: kalan ≤ 24 saat 'yakinda', süre dolmuş ya da 'yenile' durumu 'doldu'."""
    if not google_bagli(a):
        return None
    an = an or simdi()
    bitis = google_bitis(a)
    if a.google_durum == "yenile" or (bitis is not None and bitis <= an):
        return {"tur": "doldu", "metin": servisler.GOOGLE_YENILE_MESAJI, "eylem": "Yeniden bağlan", "adres": GOOGLE_BASLA}
    if bitis is not None and bitis - an <= GOOGLE_UYARI_SURESI:
        return {"tur": "yakinda", "metin": GOOGLE_YAKINDA_MESAJI, "eylem": "Şimdi yenile", "adres": GOOGLE_BASLA}
    return None


def google_ozeti(a: KullaniciAyari) -> dict:
    """Arayüz için; token hiçbir zaman dönmez."""
    bagli, kapsam, bitis = google_bagli(a), google_kapsamlari(a), google_bitis(a)
    return {
        "ayarli": servisler.google_ayarli(),
        "bagli": bagli,
        "eposta": (a.google_eposta or "") if bagli else "",
        "durum": ("yenile" if a.google_durum == "yenile" else "bagli") if bagli else None,
        "kapsamlar": {k: k in kapsam for k in servisler.GOOGLE_KAPSAMLARI},
        "test_modu": servisler.google_test_modu(),
        "bitis": zaman_iso(bitis),
        "gecerlilik": servisler.gecerlilik_metni(bitis) if bitis else "",
        "uyari": google_uyari(a),
    }


def google_erisim_tokeni(db: Session, a: KullaniciAyari) -> str:
    return oauth_erisim_tokeni(db, a, GOOGLE)


def google_tarama_ayari(db: Session, a: KullaniciAyari) -> dict:
    return oauth_tarama_ayari(db, a, GOOGLE)


def google_baglantisini_kaydet(db: Session, kullanici: Kullanici, token: dict) -> KullaniciAyari:
    """Onay dönüşü: refresh token şifreli saklanır; verilen kapsamların kaynakları açılır, verilmeyenler kapanır
    (Gmail izni verilmediyse uygulama şifreli Gmail seçimi korunur)."""
    a = ayar_satiri(db, kullanici)
    db.add(a)
    _baglanti_alanlarini_yaz(a, GOOGLE, token)
    kisa = set(servisler.google_kisa_kapsamlar(a.google_kapsamlar))
    onceki = kaynak_durumu(a)
    a.kaynaklar = {**onceki, "gmail": "gmail" in kisa or (onceki["gmail"] and bool(a.gmail_sifre_enc)),
                   **{k: k in kisa for k in GOOGLE_KAYNAKLARI}}
    db.commit()
    _google_erisim[kullanici.id] = (token["access_token"], saat_.time() + token["expires_in"])
    onbellegi_temizle(kullanici.id)
    log.info("google baglandi user=%s kapsamlar=%s", kullanici.id, ",".join(sorted(kisa)) or "-")
    return a


def google_baglantisini_kaldir(db: Session, kullanici: Kullanici) -> KullaniciAyari:
    """Google'daki izin geri alınır (hata olsa da), alanlar silinir; Takvim/Drive kapanır, Gmail uygulama şifresi
    varsa IMAP ile sürer."""
    a = ayar_satiri(db, kullanici)
    db.add(a)
    refresh = guvenlik.coz(a.google_refresh_enc)
    if refresh:
        servisler.google_iptal(refresh)
    onceki = kaynak_durumu(a)
    _baglanti_alanlarini_yaz(a, GOOGLE, None)
    a.kaynaklar = {**onceki, "gmail": onceki["gmail"] and bool(a.gmail_sifre_enc), **{k: False for k in GOOGLE_KAYNAKLARI}}
    db.commit()
    _google_erisim.pop(kullanici.id, None)
    onbellegi_temizle(kullanici.id)
    log.info("google baglantisi kaldirildi user=%s", kullanici.id)
    return a


# ---------------------------------------------------------------- Microsoft bağlantısı

def microsoft_bagli(a: KullaniciAyari) -> bool:
    return oauth_bagli(a, MICROSOFT)


def microsoft_kapsamlari(a: KullaniciAyari) -> set[str]:
    """Verilen izinlerin kısa adları: outlook / outlook_takvim / onedrive."""
    return oauth_kapsamlari(a, MICROSOFT)


def microsoft_uyari(a: KullaniciAyari, an: datetime | None = None) -> dict | None:
    """Microsoft'ta test modu süresi yok: yalnız 'yenile' durumu uyarır."""
    return yenile_uyarisi(a, MICROSOFT)


def microsoft_ozeti(a: KullaniciAyari) -> dict:
    """Arayüz için; token hiçbir zaman dönmez."""
    bagli, kapsam = microsoft_bagli(a), microsoft_kapsamlari(a)
    return {
        "ayarli": servisler.microsoft_ayarli(),
        "bagli": bagli,
        "eposta": (a.ms_eposta or "") if bagli else "",
        "durum": ("yenile" if a.ms_durum == "yenile" else "bagli") if bagli else None,
        "kapsamlar": {k: k in kapsam for k in servisler.MICROSOFT_KAPSAMLARI},
        "uyari": microsoft_uyari(a),
    }


def microsoft_erisim_tokeni(db: Session, a: KullaniciAyari) -> str:
    return oauth_erisim_tokeni(db, a, MICROSOFT)


def microsoft_tarama_ayari(db: Session, a: KullaniciAyari) -> dict:
    return oauth_tarama_ayari(db, a, MICROSOFT)


def microsoft_baglantisini_kaydet(db: Session, kullanici: Kullanici, token: dict) -> KullaniciAyari:
    """Onay dönüşü: refresh token şifreli saklanır; verilen izinlerin kaynakları (Outlook, Takvim, OneDrive) açılır,
    verilmeyenler kapanır. Google ve uygulama şifreli Gmail seçimlerine dokunulmaz."""
    a = ayar_satiri(db, kullanici)
    db.add(a)
    _baglanti_alanlarini_yaz(a, MICROSOFT, token)
    kisa = set(servisler.microsoft_kisa_kapsamlar(a.ms_kapsamlar))
    a.kaynaklar = {**kaynak_durumu(a), **{k: k in kisa for k in MICROSOFT_KAYNAKLARI}}
    db.commit()
    _ms_erisim[kullanici.id] = (token["access_token"], saat_.time() + token["expires_in"])
    onbellegi_temizle(kullanici.id)
    log.info("microsoft baglandi user=%s kapsamlar=%s", kullanici.id, ",".join(sorted(kisa)) or "-")
    return a


def microsoft_baglantisini_kaldir(db: Session, kullanici: Kullanici) -> KullaniciAyari:
    """Graph'ta izin geri alma ucu yok: alanlar silinir, Microsoft kaynakları kapanır; hesaptan kaldırma notu arayüzde."""
    a = ayar_satiri(db, kullanici)
    db.add(a)
    onceki = kaynak_durumu(a)
    _baglanti_alanlarini_yaz(a, MICROSOFT, None)
    a.kaynaklar = {**onceki, **{k: False for k in MICROSOFT_KAYNAKLARI}}
    db.commit()
    _ms_erisim.pop(kullanici.id, None)
    onbellegi_temizle(kullanici.id)
    log.info("microsoft baglantisi kaldirildi user=%s", kullanici.id)
    return a


def acik_bulunan_kaynaklari(a: KullaniciAyari) -> set[str]:
    acik = kaynak_durumu(a)
    return {kaynak for kaynak, moduller in BULUNAN_MODULLERI.items() if any(acik.get(m) for m in moduller)}


def ayar_ozeti(a: KullaniciAyari, kullanici: Kullanici | None = None) -> dict:
    """Şifre ve token hiçbir zaman dönmez; yalnız kayıtlı olup olmadıkları."""
    h, o = hatirlatma_ayari(a), otomatik_ayari(a)
    return {
        "ad": kullanici.ad if kullanici else "",
        "ad_yer_tutucu": ad_yer_tutucu(kullanici.ad) if kullanici else False,
        "ad_eslemeleri": ad_eslemeleri(a),
        "otomatik_gonder": o["acik"],
        "otomatik_saat": o["saat"].strftime("%H:%M"),
        "patron_eposta": o["patron_eposta"],
        "patron_adi": o["patron_adi"],
        "otomatik_kopya_bana": o["kopya_bana"],
        "hatirlatma_saat": h["saat"].strftime("%H:%M"),
        "hatirlatma_gunler": h["gunler"],
        "hatirlatma_push": h["push"],
        "hatirlatma_eposta": h["eposta"],
        "hatirlatma_eposta_adres": h["adres"],
        "giris_eposta": kullanici.eposta if kullanici else "",
        "eposta_gonderen": servisler.gonderen_adresi(),
        "gmail_kullanici": a.gmail_kullanici or "",
        "gmail_sifre_kayitli": bool(a.gmail_sifre_enc),
        "github_token_kayitli": bool(a.github_token_enc),
        "github_repo": a.github_repo or "",
        "proje_adi": a.proje_adi or "",
        "patron_telefon": a.patron_telefon or "",
        "rapor_basligi": a.rapor_basligi or "",
        "alan_sozlugu": alan_sozlugu(a),
        "kaynaklar": kaynak_durumu(a),
        "kurulum_tamam": bool(a.kurulum_tamam),
        "eposta_gruplama": eposta_gruplama(a),
        "rapor_bicimi": rapor_bicimi(a),
        "karistir": a.karistir is not False,
        "kendi_alanlar_otomatik": otomatik_kendi_alanlar(a, kullanici),
        "kendi_alanlar": list(a.kendi_alanlar or []),
        "ekip_ici_atla": a.ekip_ici_atla is not False,
        "eposta_kurallari": eposta_saglayicisi_var(a),
        "google": google_ozeti(a),
        "microsoft": microsoft_ozeti(a),
    }


def eposta_saglayicisi_var(a: KullaniciAyari) -> bool:
    """Ayarlar'daki "E-posta kuralları" bölümü: Gmail uygulama şifresi, Gmail izinli Google ya da Outlook izinli
    Microsoft bağlantısı varsa görünür."""
    return bool(a.gmail_sifre_enc) or "gmail" in google_kapsamlari(a) or "outlook" in microsoft_kapsamlari(a)


def alan_sozlugu(a: KullaniciAyari) -> dict[str, str]:
    return servisler.KURUMLAR if a.alan_sozlugu is None else a.alan_sozlugu


def ad_eslemeleri(a: KullaniciAyari | None) -> list[dict]:
    """[{kaynak, hedef}]; satır ya da kolon yoksa boş."""
    return [dict(e) for e in (a.ad_eslemeleri if a is not None and isinstance(a.ad_eslemeleri, list) else [])
            if isinstance(e, dict)]


def ad_yer_tutucu(ad: str | None) -> bool:
    return servisler._kucult((ad or "").strip()) in YER_TUTUCU_ADLAR


def otomatik_kendi_alanlar(a: KullaniciAyari, kullanici: Kullanici | None) -> list[str]:
    """Giriş, Gmail ve Microsoft adresinin alan adları + sözlükte 'şirket içi' eşlenenler (Ayarlar'da gri rozet)."""
    adresler = [kullanici.eposta if kullanici else "", a.gmail_kullanici or "", a.ms_eposta or ""]
    return servisler.kendi_alanlari(adresler, sozluk=alan_sozlugu(a))


def kendi_alanlar(a: KullaniciAyari, kullanici: Kullanici | None) -> list[str]:
    return list(dict.fromkeys(otomatik_kendi_alanlar(a, kullanici) + list(a.kendi_alanlar or [])))


def eposta_gruplama(a: KullaniciAyari) -> str:
    return a.eposta_gruplama if a.eposta_gruplama in servisler.GRUPLAMALAR else "konu"


def cozulmus_ayarlar(a: KullaniciAyari, kullanici: Kullanici | None = None) -> dict:
    kendi = kendi_alanlar(a, kullanici)
    return {
        "kendi_alanlar": kendi,
        "kendi_sirket": servisler.kendi_sirket_adlari(kendi, alan_sozlugu(a)),
        "ekip_ici_atla": a.ekip_ici_atla is not False,
        "eposta_gruplama": eposta_gruplama(a),
        "kaynaklar": kaynak_durumu(a),
        "gmail_kullanici": a.gmail_kullanici or "",
        "gmail_sifre": guvenlik.coz(a.gmail_sifre_enc),
        "github_token": guvenlik.coz(a.github_token_enc),
        "github_repo": a.github_repo or "",
        "proje_adi": a.proje_adi or "",
        "alan_sozlugu": alan_sozlugu(a),
        "ad_eslemeleri": ad_eslemeleri(a),
    }


def kullanici_maddesi(db: Session, kullanici: Kullanici, madde_id: int) -> Madde:
    madde = db.get(Madde, madde_id)
    if madde is None or madde.user_id != kullanici.id:
        raise HTTPException(status_code=404, detail="Madde bulunamadı")
    return madde


def sonraki_sira(db: Session, kullanici: Kullanici, tur: str, tarih: date | None = None) -> int:
    sorgu = select(func.max(Madde.sira)).where(Madde.user_id == kullanici.id, Madde.tur == tur)
    if tarih is not None:
        sorgu = sorgu.where(Madde.tarih == tarih)
    return (db.scalar(sorgu) or 0) + 1


_kilitler: dict[tuple[int, str], threading.Lock] = {}
_kilitler_kilidi = threading.Lock()


def _kullanici_kilidi(user_id: int, is_: str = "tarama") -> threading.Lock:
    with _kilitler_kilidi:
        return _kilitler.setdefault((user_id, is_), threading.Lock())


def satirlara_bol(metin: str) -> list[str]:
    satirlar = (re.sub(r"^[-•*]\s*", "", x.strip()) for x in (metin or "").splitlines())
    return [x for x in satirlar if x]


def bugunku_ifadeler(db: Session, user_id: int, tarih: date, item_idler=None) -> dict[int, str]:
    sorgu = select(GunlukIfade).where(GunlukIfade.user_id == user_id, GunlukIfade.tarih == tarih)
    if item_idler is not None:
        sorgu = sorgu.where(GunlukIfade.item_id.in_(list(item_idler)))
    return {i.item_id: i.metin_ai for i in db.scalars(sorgu)}


def madde_rapor_metni(m: Madde, ifade: str | None, tarih: date) -> str:
    """Rapora giren metin: kullanıcının düzenlediği > Claude'un düzelttiği > ham. Sürekli işte günün ifadesi > '|' varyantı."""
    ai_acik = m.ai_kullan is not False
    if m.tur == "surekli":
        return ifade if (ifade and ai_acik) else servisler.surekli_varyant(m.metin, tarih)
    ham = m.metin + (f" — {m.asama}" if m.tur == "devam" and m.asama else "")
    if m.kullanici_duzenledi:
        return ham
    if ai_acik and m.metin_ai:
        return m.metin_ai
    return ham


def madde_json(m: Madde, ifadeler: dict[int, str], tarih: date, eslemeler: list[dict] | None = None) -> dict:
    """rapor_metni ad eşlemeleri uygulanmış hâlidir; metin, metin_ai ve gunun_ifadesi ham kalır ("orijinali gör")."""
    ifade = ifadeler.get(m.id) if m.tur == "surekli" else None
    veri = {
        **m.sozluk(), "gunun_ifadesi": ifade, "rapor_metni": servisler.ad_esle(madde_rapor_metni(m, ifade, tarih), eslemeler),
        "olusturma": zaman_iso(m.olusturma), "kaynak_zaman": zaman_iso(m.kaynak_zaman),
    }
    if m.tur == "devam":  # yalnız ekranda gösterilir; rapor metnine girmez
        veri["bekleme_gun"] = bekleme_gunu(m.olusturma, tarih)
    return veri


def utc(z: datetime) -> datetime:
    """sqlite saat dilimini saklamaz; naive değerler UTC kabul edilir."""
    return z.astimezone(timezone.utc) if z.tzinfo else z.replace(tzinfo=timezone.utc)


def zaman_iso(z: datetime | None) -> str | None:
    return None if z is None else utc(z).isoformat()


def bekleme_gunu(olusturma: datetime | None, tarih: date) -> int:
    """Devam eden işin kaç gündür beklediği: bugün − eklendiği gün (Istanbul takvimiyle)."""
    if olusturma is None:
        return 0
    return max(0, (tarih - utc(olusturma).astimezone(servisler.ISTANBUL).date()).days)


def yapilanlari_bol(db: Session, kullanici: Kullanici, tarih: date) -> None:
    """R1'in tek parça 'yapılanlar' metni bir kez satır satır 'elle' maddelerine çevrilir."""
    eskiler = db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, Madde.tur == "bugun", Madde.tarih == tarih, Madde.kaynak == "yapilanlar",
    ).order_by(Madde.id)).all()
    if not eskiler:
        return
    sira = sonraki_sira(db, kullanici, "bugun", tarih)
    for eski in eskiler:
        for satir in satirlara_bol(eski.metin):
            db.add(Madde(user_id=kullanici.id, tur="bugun", tarih=tarih, kaynak="elle", metin=satir, tikli=eski.tikli, sira=sira))
            sira += 1
        db.delete(eski)
    db.commit()


# ---------------------------------------------------------------- rapor kategorileri ve günün düzeni

def baslangic_kategorileri(a: KullaniciAyari) -> list[dict]:
    return [
        {"ad": "Yazışmalar", "kaynaklar": ["gmail"]},
        {"ad": f"{a.proje_adi or 'Uygulama'} Çalışmaları"[:80], "kaynaklar": ["github", "medusa"]},
        {"ad": "Genel İşler", "kaynaklar": []},
        {"ad": "Devam Eden İşler", "kaynaklar": [], "sistem": "devam"},
        {"ad": "Önemli Konular", "kaynaklar": [], "sistem": "onemli"},
    ]


def kategorileri_hazirla(db: Session, kullanici: Kullanici) -> list[Kategori]:
    """Kullanıcının kategorileri (sıralı). Hiç yoksa başlangıç kategorileri bir kez açılır."""
    sorgu = select(Kategori).where(Kategori.user_id == kullanici.id).order_by(Kategori.sira, Kategori.id)
    kategoriler = db.scalars(sorgu).all()
    if kategoriler:
        return list(kategoriler)
    with _kullanici_kilidi(kullanici.id, "kategori"):
        if not db.scalar(select(func.count()).select_from(Kategori).where(Kategori.user_id == kullanici.id)):
            for sira, k in enumerate(baslangic_kategorileri(ayar_satiri(db, kullanici)), start=1):
                db.add(Kategori(user_id=kullanici.id, sira=sira, **k))
            db.commit()
    return list(db.scalars(sorgu).all())


class KategoriBaglami:
    """Etkin kategori kuralı için kullanıcının kategorileri üzerinde hazır aramalar."""

    def __init__(self, kategoriler: list[Kategori]):
        self.kategoriler = kategoriler
        self.idler = {k.id for k in kategoriler}
        self.sistem = {k.sistem: k.id for k in kategoriler if k.sistem}
        self.secilebilir = {k.id for k in kategoriler if not k.sistem}
        genel = next((k for k in kategoriler if not k.sistem and k.ad.strip().lower() == "genel işler"), None)
        genel = genel or next((k for k in kategoriler if not k.sistem and not k.kaynaklar), None)
        genel = genel or next((k for k in kategoriler if not k.sistem), None) or (kategoriler[0] if kategoriler else None)
        self.genel = genel.id if genel else None

    def kaynagin_kategorisi(self, modul: str) -> int | None:
        """Outlook / OneDrive için kategori seçilmemişse Gmail / Drive'ın kategorisi."""
        for m in (modul, KATEGORI_YEDEGI.get(modul)):
            k = next((k.id for k in self.kategoriler if m and m in (k.kaynaklar or [])), None)
            if k is not None:
                return k
        return None

    def dogal(self, m: Madde) -> int | None:
        """Günün düzeni hariç kural: önemli > devam > maddenin kategorisi > bulunanın kaynağı > Genel İşler."""
        if m.onemli and "onemli" in self.sistem:
            return self.sistem["onemli"]
        if m.tur == "devam" and "devam" in self.sistem:
            return self.sistem["devam"]
        if m.kategori_id in self.idler:  # elle seçilen ya da Claude'un önerdiği
            return m.kategori_id
        if m.tur == "bulunan":
            k = self.kaynagin_kategorisi(BULUNAN_KAYNAGI.get(m.kaynak, m.kaynak))
            if k is not None:
                return k
        return self.genel

    def etkin(self, m: Madde, duzen: RaporDuzeni | None) -> int | None:
        if duzen is not None and duzen.kategori_id in self.idler:
            return duzen.kategori_id
        return self.dogal(m)


def rapor_adaylari(db: Session, kullanici: Kullanici, tarih: date, a: KullaniciAyari) -> list[Madde]:
    """O günün raporuna girebilecek maddeler (tik durumundan bağımsız): sürekli, devam, elle/not/ses, açık kaynağın bulunanları."""
    acik = acik_bulunan_kaynaklari(a)
    maddeler = db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, or_(Madde.tur.in_(("surekli", "devam")), Madde.tarih == tarih),
    ).order_by(Madde.id)).all()
    return [m for m in maddeler if m.tur in ("surekli", "devam")
            or (m.tur == "bugun" and m.kaynak in ELLE_KAYNAKLARI and not m.gizli)
            or (m.tur == "bulunan" and m.kaynak in acik and not m.gizli)]


def _tohum(tarih: date, item_id: int, tuz: str = "") -> float:
    ozet = hashlib.sha256(f"{tarih.isoformat()}:{item_id}:{tuz}".encode()).digest()
    return int.from_bytes(ozet[:8], "big") / 2 ** 64


def serpistir(temel: list[Madde], surekli: list[Madde], tarih: date) -> list[Madde]:
    """Sürekli işleri tarih + madde id tohumlu yerlere dağıtır: gün içinde sabit, günden güne farklı."""
    yerler = sorted(surekli, key=lambda m: (int(_tohum(tarih, m.id, "yer") * (len(temel) + 1)), _tohum(tarih, m.id)))
    sonuc, i = [], 0
    for yer in range(len(temel) + 1):
        while i < len(yerler) and int(_tohum(tarih, yerler[i].id, "yer") * (len(temel) + 1)) == yer:
            sonuc.append(yerler[i])
            i += 1
        if yer < len(temel):
            sonuc.append(temel[yer])
    return sonuc


def kategori_ici_sira(maddeler: list[Madde], satirlar: dict[int, RaporDuzeni], tarih: date, karistir: bool) -> list[Madde]:
    """Günün düzeninde sırası olanlar önce; kalanlarda bugün/bulunan eklenme sırasıyla, sürekli işler arada ya da sonda."""
    elle = sorted((m for m in maddeler if m.id in satirlar), key=lambda m: (satirlar[m.id].sira, m.id))
    kalan = [m for m in maddeler if m.id not in satirlar]
    surekli = sorted((m for m in kalan if m.tur == "surekli"), key=lambda m: (m.sira, m.id))
    temel = sorted((m for m in kalan if m.tur != "surekli"),
                   key=lambda m: (m.tur == "devam", m.sira if m.tur == "devam" else 0, m.id))
    return elle + (serpistir(temel, surekli, tarih) if karistir else temel + surekli)


class GunDuzeni:
    """Bir günün rapor düzeni: kategori sırasıyla bölümler, maddelerin etkin kategorisi ve düz biçim sırası."""

    def __init__(self, db: Session, kullanici: Kullanici, tarih: date, a: KullaniciAyari | None = None):
        a = a or ayar_satiri(db, kullanici)
        self.tarih = tarih
        self.bicim = rapor_bicimi(a)
        self.kategoriler = kategorileri_hazirla(db, kullanici)
        self.baglam = KategoriBaglami(self.kategoriler)
        self.adaylar = rapor_adaylari(db, kullanici, tarih, a)
        self.satirlar = {r.item_id: r for r in db.scalars(select(RaporDuzeni).where(
            RaporDuzeni.user_id == kullanici.id, RaporDuzeni.tarih == tarih,
        ))}
        self.etkin = {m.id: self.baglam.etkin(m, self.satirlar.get(m.id)) for m in self.adaylar}
        karistir = a.karistir is not False
        self.bolumler: list[tuple[Kategori, list[Madde]]] = [
            (k, kategori_ici_sira([m for m in self.adaylar if self.etkin[m.id] == k.id], self.satirlar, tarih, karistir))
            for k in self.kategoriler
        ]

    @property
    def ozel(self) -> bool:
        return any(m.id in self.satirlar for m in self.adaylar)

    def duz(self) -> tuple[list[Madde], list[Madde]]:
        """(Yapılanlar, Devam eden). Eski sıra: elle, bulunan, sürekli; günün düzeninde sırası olanlar o sırayla öne geçer."""
        def tur_sirasi(*turler: str) -> list[Madde]:
            eski = [m for tur in turler for m in sorted((m for m in self.adaylar if m.tur == tur), key=lambda m: (m.sira, m.id))]
            yer = {m.id: i for i, m in enumerate(eski)}
            return sorted(eski, key=lambda m: (0, self.satirlar[m.id].sira) if m.id in self.satirlar else (1, yer[m.id]))
        return tur_sirasi("bugun", "bulunan", "surekli"), tur_sirasi("devam")

    def json(self) -> dict:
        yapilanlar, devam = self.duz()
        return {
            "kategoriler": [k.sozluk() for k in self.kategoriler],
            "bolumler": [{"kategori_id": k.id, "maddeler": [m.id for m in liste]} for k, liste in self.bolumler],
            "duz": {"yapilanlar": [m.id for m in yapilanlar], "devam": [m.id for m in devam]},
            "ozel": self.ozel,
            "genel": self.baglam.genel,
        }


def rapor_bicimi(a: KullaniciAyari) -> str:
    return a.rapor_bicimi if a.rapor_bicimi in RAPOR_BICIMLERI else "kategorili"


def rapor_metni_olustur(
    baslik: str, tarih: date, bicim: str, bolumler: list[tuple[str, list[str]]], duz: tuple[list[str], list[str]],
    yarin: list[str],
) -> str:
    """WhatsApp metni. kategorili: her dolu bölüm '*Ad:*' + maddeler; duz: Yapılanlar / Devam eden. Yarın en sonda."""
    satirlar = [f"*{baslik or 'Günlük Rapor'} – {tarih.strftime('%d.%m.%Y')}*", ""]
    if bicim == "duz":
        parcalar = [("Yapılanlar", duz[0]), ("Devam eden", duz[1]), ("Yarın", yarin)]
    else:
        parcalar = [(f"{ad}:", liste) for ad, liste in bolumler] + [("Yarın:", yarin)]
    for ad, liste in parcalar:
        if liste:
            satirlar += [f"*{ad}*"] + [f"• {x}" for x in liste] + [""]
    return "\n".join(satirlar).strip()


def gunun_rapor_metni(db: Session, kullanici: Kullanici, tarih: date, duzen: GunDuzeni | None = None) -> str:
    """Tikli maddelerden günün rapor metni; arayüzdeki önizlemeyle aynı kural. Son adımda ad eşlemeleri bütün metne
    (başlık, kategori adları, elle yazılanlar ve Yarın dahil) uygulanır."""
    a = ayar_satiri(db, kullanici)
    duzen = duzen or GunDuzeni(db, kullanici, tarih, a)
    ifadeler = bugunku_ifadeler(db, kullanici.id, tarih)

    def metin(m: Madde) -> str:
        return madde_rapor_metni(m, ifadeler.get(m.id), tarih)

    def kat_metin(m: Madde) -> str:  # kategorili biçimde devam eden işler "iş — aşama" olarak yazılır
        return m.metin + (f" — {m.asama}" if m.asama else "") if m.tur == "devam" else metin(m)

    yapilanlar, devam = duzen.duz()
    yarin = db.scalar(select(Madde.metin).where(
        Madde.user_id == kullanici.id, Madde.tur == "bugun", Madde.tarih == tarih, Madde.kaynak == "yarin",
    ))
    return servisler.ad_esle(rapor_metni_olustur(
        a.rapor_basligi or "", tarih, duzen.bicim,
        [(k.ad, [kat_metin(m) for m in liste if m.tikli]) for k, liste in duzen.bolumler],
        ([metin(m) for m in yapilanlar if m.tikli], [metin(m) for m in devam if m.tikli]),
        satirlara_bol(yarin or ""),
    ), ad_eslemeleri(a))


def kacirilan_gun(db: Session, kullanici: Kullanici, a: KullaniciAyari, bugun_: date) -> date | None:
    """Hatırlatma günlerine göre bir önceki iş günü (en fazla 7 gün geri); günlük raporu yoksa o gün."""
    gunler = hatirlatma_ayari(a)["gunler"]
    onceki = next((g for g in (bugun_ - timedelta(days=i) for i in range(1, 8)) if g.isoweekday() in gunler), None)
    if onceki is None:
        return None
    if kullanici.olusturma and utc(kullanici.olusturma).astimezone(servisler.ISTANBUL).date() > onceki:
        return None  # hesap o günden sonra açılmış
    if db.scalar(select(Rapor.id).where(Rapor.user_id == kullanici.id, Rapor.tur == "gunluk", Rapor.tarih == onceki)):
        return None
    return onceki


# ---------------------------------------------------------------- durum

@router.get("/durum")
def durum(tarih: str | None = None, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    bugun_, tarih = bugun(), gun_sec(tarih)
    yapilanlari_bol(db, kullanici, tarih)
    a = ayar_satiri(db, kullanici)
    duzen = GunDuzeni(db, kullanici, tarih, a)
    maddeler = db.scalars(
        select(Madde)
        .where(Madde.user_id == kullanici.id, or_(Madde.tur.in_(("surekli", "devam")), Madde.tarih == tarih))
        .order_by(Madde.tur, Madde.sira, Madde.id)
    ).all()
    ifadeler = bugunku_ifadeler(db, kullanici.id, tarih)
    rapor = db.scalar(select(Rapor).where(Rapor.user_id == kullanici.id, Rapor.tur == "gunluk", Rapor.tarih == tarih))
    onbellek = _onbellek.get((kullanici.id, tarih.isoformat())) or {}
    acik = acik_bulunan_kaynaklari(a)
    kacirilan = kacirilan_gun(db, kullanici, a, bugun_) if tarih == bugun_ else None
    return {
        "kullanici": {"ad": kullanici.ad, "rol": kullanici.rol},
        "tarih": tarih.isoformat(),
        "bugun": bugun_.isoformat(),
        "kacirilan_gun": kacirilan.isoformat() if kacirilan else None,
        "maddeler": [
            {**madde_json(m, ifadeler, tarih, ad_eslemeleri(a)), "etkin_kategori_id": duzen.etkin.get(m.id)}
            for m in maddeler if m.tur != "bulunan" or m.kaynak in acik
        ],
        "duzen": duzen.json(),
        "rapor_metni": gunun_rapor_metni(db, kullanici, tarih, duzen),
        "ayarlar": ayar_ozeti(a, kullanici),
        "ai_anahtari": bool(ai_anahtari()),
        "son_kopya": zaman_iso(rapor.olusturma) if rapor else None,
        "gonderim": (rapor.gonderim or "elle") if rapor else None,  # 'otomatik': patrona e-postayla gitti
        "otomatik": otomatik_ozeti(db, kullanici, a, tarih) if tarih == bugun_ else None,
        "tarama_zamani": onbellek.get("tarama_zamani"),
    }


# ---------------------------------------------------------------- maddeler

class MaddeYeni(BaseModel):
    tur: Literal["surekli", "devam", "bugun"]
    metin: str = ""
    asama: str | None = None
    tikli: bool = True
    kaynak: Literal["elle", "yapilanlar", "yarin"] | None = None
    tarih: date | None = None  # bugün/yarın satırlarının günü; boşsa bugün


class MaddeGuncelle(BaseModel):
    metin: str | None = None
    asama: str | None = None
    tikli: bool | None = None
    gizli: bool | None = None
    kategori_id: int | None = None  # null: otomatik kurala dön
    onemli: bool | None = None


class AiSecimi(BaseModel):
    kullan: bool | None = None
    yenile: bool = False


class Siralama(BaseModel):
    tur: Literal["surekli", "devam", "bulunan"]
    idler: list[int]


@router.post("/maddeler", status_code=201)
def madde_ekle(govde: MaddeYeni, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    tarih = gun_sec(govde.tarih)
    if govde.tur == "bugun" and govde.kaynak == "elle":
        metin = govde.metin.strip()
        if not metin:
            raise HTTPException(status_code=422, detail="Metin boş olamaz")
        madde = Madde(
            user_id=kullanici.id, tur="bugun", tarih=tarih, kaynak="elle", metin=metin, tikli=govde.tikli,
            sira=sonraki_sira(db, kullanici, "bugun", tarih),
        )
        db.add(madde)
    elif govde.tur == "bugun":
        # "Yarın" (ve eski "yapılanlar") metni gün başına tek satırdır; ikinci ekleme mevcut satırı günceller.
        if govde.kaynak is None:
            raise HTTPException(status_code=422, detail="bugün maddesi için kaynak gerekli (elle | yarin)")
        madde = db.scalar(select(Madde).where(
            Madde.user_id == kullanici.id, Madde.tur == "bugun", Madde.tarih == tarih, Madde.kaynak_id == govde.kaynak,
        ))
        if madde is None:
            madde = Madde(user_id=kullanici.id, tur="bugun", tarih=tarih, kaynak=govde.kaynak, kaynak_id=govde.kaynak)
            db.add(madde)
        madde.metin = govde.metin
    else:
        metin = govde.metin.strip()
        if not metin:
            raise HTTPException(status_code=422, detail="Metin boş olamaz")
        madde = Madde(
            user_id=kullanici.id, tur=govde.tur, metin=metin, asama=(govde.asama or "").strip() or None,
            tikli=govde.tikli, sira=sonraki_sira(db, kullanici, govde.tur),
        )
        db.add(madde)
    db.commit()
    return madde_json(madde, {}, tarih, ad_eslemeleri(ayar_satiri(db, kullanici)))


def kullanici_kategorisi(db: Session, kullanici: Kullanici, kategori_id: int) -> Kategori:
    kategori = db.get(Kategori, kategori_id)
    if kategori is None or kategori.user_id != kullanici.id:
        raise HTTPException(status_code=404, detail="Kategori bulunamadı")
    return kategori


def gunun_duzen_satiri(db: Session, kullanici: Kullanici, tarih: date, madde_id: int) -> RaporDuzeni | None:
    return db.scalar(select(RaporDuzeni).where(
        RaporDuzeni.user_id == kullanici.id, RaporDuzeni.tarih == tarih, RaporDuzeni.item_id == madde_id,
    ))


@router.patch("/maddeler/{madde_id}")
def madde_guncelle(
    madde_id: int, govde: MaddeGuncelle, tarih: str | None = None,
    kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    madde = kullanici_maddesi(db, kullanici, madde_id)
    tarih = gun_sec(tarih)
    serbest_metin = madde.tur == "bugun" and madde.kaynak in ("yarin", "yapilanlar")
    veri = govde.model_dump(exclude_unset=True)
    if "kategori_id" in veri:
        kategori_id = veri.pop("kategori_id")
        if kategori_id is not None and kullanici_kategorisi(db, kullanici, kategori_id).sistem:
            raise HTTPException(status_code=422, detail="Sistem kategorisi maddeye atanamaz")
        madde.kategori_id = kategori_id
        satir = gunun_duzen_satiri(db, kullanici, tarih, madde.id)
        if satir is not None and satir.kategori_id is not None:  # o günün düzeninde kategori varsa o da izler
            satir.kategori_id = kategori_id
    if veri.get("onemli") is not None and bool(veri["onemli"]) != bool(madde.onemli):
        satir = gunun_duzen_satiri(db, kullanici, tarih, madde.id)
        if satir is not None:  # yıldız, günün düzenindeki elle kategoriden önce gelsin
            satir.kategori_id = None
    for alan, deger in veri.items():
        if deger is None:
            continue
        if alan == "metin" and not serbest_metin:
            deger = deger.strip()
            if not deger:
                raise HTTPException(status_code=422, detail="Metin boş olamaz")
            if deger != madde.metin:
                if madde.tur == "surekli":  # şablon değişti; günün ifadesi yeniden üretilsin
                    db.execute(delete(GunlukIfade).where(GunlukIfade.item_id == madde.id, GunlukIfade.tarih == tarih))
                else:  # kullanıcı ne yazdıysa rapora o girer
                    madde.kullanici_duzenledi = True
                    madde.metin_ai = None
                    madde.ai_tarih = None
        if alan == "asama" and madde.tur == "devam" and (deger or "") != (madde.asama or ""):
            madde.metin_ai = None
            madde.ai_tarih = None
        setattr(madde, alan, deger)
    db.commit()
    return madde_json(madde, bugunku_ifadeler(db, kullanici.id, tarih, [madde.id]), tarih, ad_eslemeleri(ayar_satiri(db, kullanici)))


@router.patch("/maddeler/{madde_id}/ai")
def madde_ai(
    madde_id: int, govde: AiSecimi, tarih: str | None = None,
    kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    madde = kullanici_maddesi(db, kullanici, madde_id)
    tarih = gun_sec(tarih)
    hatalar: list[str] = []
    if govde.yenile:
        anahtar = ai_anahtari()
        if not anahtar:
            raise HTTPException(status_code=400, detail="Claude anahtarı tanımlı değil (ANTHROPIC_API_KEY)")
        with _kullanici_kilidi(kullanici.id, "duzelt"):
            if madde.tur == "surekli":
                db.execute(delete(GunlukIfade).where(GunlukIfade.item_id == madde.id, GunlukIfade.tarih == tarih))
            else:
                madde.metin_ai = None
                madde.ai_tarih = None
                madde.kullanici_duzenledi = False
            madde.ai_kullan = True
            db.commit()
            sonuc = duzeltmeyi_uygula(db, kullanici, [madde], tarih, anahtar)
            hatalar = [KOTA_MESAJI] if sonuc.get("atlandi") else sonuc["hatalar"]
    elif govde.kullan is not None:
        madde.ai_kullan = govde.kullan
        db.commit()
    return {**madde_json(madde, bugunku_ifadeler(db, kullanici.id, tarih, [madde.id]), tarih,
                         ad_eslemeleri(ayar_satiri(db, kullanici))), "hatalar": hatalar}


@router.delete("/maddeler/{madde_id}")
def madde_sil(madde_id: int, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    madde = kullanici_maddesi(db, kullanici, madde_id)
    if madde.kaynak == "not":  # silinirse sonraki taramada geri gelirdi; gizlenir
        madde.gizli = True
    else:
        db.execute(delete(RaporDuzeni).where(RaporDuzeni.item_id == madde.id))
        db.delete(madde)
    db.commit()
    return {"ok": True}


@router.post("/maddeler/sira")
def madde_sirala(govde: Siralama, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    maddeler = {m.id: m for m in db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, Madde.tur == govde.tur, Madde.id.in_(govde.idler),
    ))}
    if len(maddeler) != len(set(govde.idler)):
        raise HTTPException(status_code=404, detail="Madde bulunamadı")
    for sira, madde_id in enumerate(govde.idler, start=1):
        maddeler[madde_id].sira = sira
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- bugün bulunanlar

_onbellek: dict[tuple[int, str], dict] = {}


def onbellegi_temizle(user_id: int | None = None) -> None:
    """Hepsini ya da yalnız bir kullanıcının taramalarını atar (kaynak bağlantısı değişince yeniden taransın)."""
    for anahtar in [k for k in _onbellek if user_id is None or k[0] == user_id]:
        del _onbellek[anahtar]


def eposta_eskilerini_gizle(mevcut: dict, sonuc: dict, kaynak: str = "eposta", hata_kaynagi: str = "gmail") -> None:
    """Bu taramada artık üretilmeyen bugünkü e-posta maddeleri (ör. E1 öncesi alıcı başına gruplu madde ya da
    gruplama ayarı değişince eski biçim) gizlenir, silinmez; kullanıcının düzenlediğine dokunulmaz.
    Gmail (Outlook için Outlook) hata verdiyse ya da hiç e-posta bulunmadıysa hiçbir şey gizlenmez."""
    if not sonuc[kaynak] or any(h["kaynak"] == hata_kaynagi for h in sonuc["hatalar"]):
        return
    guncel = {m["id"] for m in sonuc[kaynak]}
    for kaynak_id, m in mevcut.items():
        if m.kaynak == kaynak and kaynak_id not in guncel and not m.kullanici_duzenledi:
            m.gizli = True


def google_eskilerini_gizle(mevcut: dict, sonuc: dict, bugun_mu: bool) -> None:
    """Takvim/Drive/OneDrive: bugünün taramasında hatasız taranan kaynağın artık üretilmeyen (iptal edilen toplantı, son
    değişikliği başkası yapan dosya) ve kullanıcının düzenlemediği maddeleri gizlenir, silinmez."""
    if not bugun_mu:
        return
    for kaynak in [k for k in sonuc.get("taranan", []) if k in ("takvim", "drive", "onedrive")]:
        guncel = {m["id"] for m in sonuc[kaynak]}
        for kaynak_id, m in mevcut.items():
            if m.kaynak == kaynak and kaynak_id not in guncel and not m.kullanici_duzenledi:
                m.gizli = True


def bugun_taramasi(db: Session, kullanici: Kullanici, tarih: date, yenile: bool = False) -> dict:
    """Kullanıcı başına günde bir tarama (kilit + önbellek); bulunanları maddelere yazar, önbellek özetini döner."""
    anahtar = (kullanici.id, tarih.isoformat())
    with _kullanici_kilidi(kullanici.id):
        if yenile or anahtar not in _onbellek:
            mevcut = {m.kaynak_id: m for m in db.scalars(select(Madde).where(
                Madde.user_id == kullanici.id, Madde.tur == "bulunan", Madde.tarih == tarih,
            ))}
            # kullanıcının düzenlediği madde Claude'a hiç gitmez; metni değişen düzenlenmemiş madde yeniden çevrilir
            haric = {k: None if m.kullanici_duzenledi else m.metin for k, m in mevcut.items()}
            a = ayar_satiri(db, kullanici)
            ayarlar = cozulmus_ayarlar(a, kullanici)
            ayarlar["google"] = google_tarama_ayari(db, a)
            ayarlar["microsoft"] = microsoft_tarama_ayari(db, a)
            sonuc = servisler.raporu_uret(ayarlar, os.environ.get("ANTHROPIC_API_KEY", ""), haric, tarih)
            for s in (GOOGLE, MICROSOFT):  # reddedilen access token bir sonraki taramada yenilensin
                if sonuc.get(f"{s.ad}_yetkisiz"):
                    s.erisim.pop(kullanici.id, None)
            notlar = set(db.scalars(select(Madde.kaynak_id).where(
                Madde.user_id == kullanici.id, Madde.tur == "bugun", Madde.tarih == tarih, Madde.kaynak == "not",
            )))
            not_sirasi = sonraki_sira(db, kullanici, "bugun", tarih)
            for m in sonuc.get("not", []):  # kendine atılan notlar bugünün yapılanlarına düşer
                if m["id"] in notlar:
                    continue
                zaman = m.get("kaynak_zaman")
                db.add(Madde(
                    user_id=kullanici.id, tur="bugun", metin=m["metin"], tarih=tarih, kaynak="not", kaynak_id=m["id"],
                    tikli=True, sira=not_sirasi, kaynak_zaman=utc(zaman) if zaman else None,
                ))
                notlar.add(m["id"])
                not_sirasi += 1
            sira = sonraki_sira(db, kullanici, "bulunan", tarih)
            for m in sonuc["eposta"] + sonuc["outlook"] + sonuc["medusa"] + sonuc["takvim"] + sonuc["drive"] + sonuc["onedrive"]:
                zaman = m.get("kaynak_zaman")
                eski = mevcut.get(m["id"])
                if eski is not None:
                    # aynı konuya gün içinde yeni mail: madde güncellenir, çoğalmaz; kullanıcı düzenlediyse dokunulmaz
                    if not eski.kullanici_duzenledi and eski.metin != m["metin"]:
                        eski.metin = m["metin"]
                        eski.metin_ai = m.get("metin_ai")
                        eski.ai_tarih = tarih if m.get("metin_ai") else None
                    if zaman and not eski.kullanici_duzenledi:
                        eski.kaynak_zaman = utc(zaman)
                    continue
                mevcut[m["id"]] = Madde(
                    user_id=kullanici.id, tur="bulunan", metin=m["metin"], tarih=tarih,
                    kaynak=m["kaynak"], kaynak_id=m["id"], tikli=True, sira=sira,
                    metin_ai=m.get("metin_ai"), ai_tarih=tarih if m.get("metin_ai") else None,
                    kaynak_zaman=utc(zaman) if zaman else None,
                )
                db.add(mevcut[m["id"]])
                sira += 1
            eposta_eskilerini_gizle(mevcut, sonuc)
            eposta_eskilerini_gizle(mevcut, sonuc, "outlook", "outlook")
            google_eskilerini_gizle(mevcut, sonuc, tarih == bugun())
            db.commit()
            # geçmiş günler de önbellekte kalır; düzenlenebilir aralığın dışına düşenler atılır
            sinir = (bugun() - timedelta(days=GECMIS_GUN)).isoformat()
            for eski in [k for k in _onbellek if k[0] == kullanici.id and k[1] < sinir]:
                del _onbellek[eski]
            _onbellek[anahtar] = {
                "hatalar": sonuc["hatalar"],
                "sayim": {"eposta": len(sonuc["eposta"]), "medusa": len(sonuc["medusa"]),
                          **{k: len(sonuc[k]) for k in sonuc["taranan"]}},
                "tarama_zamani": zaman_iso(simdi()),
            }
        return _onbellek[anahtar]


@router.get("/bugun")
def bugun_bulunanlar(
    yenile: int = 0, tarih: str | None = None, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)
) -> dict:
    tarih = gun_sec(tarih)
    onbellek = bugun_taramasi(db, kullanici, tarih, bool(yenile))
    a = ayar_satiri(db, kullanici)
    bulunanlar = db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, Madde.tur == "bulunan", Madde.tarih == tarih,
        Madde.kaynak.in_(acik_bulunan_kaynaklari(a)),
    ).order_by(Madde.sira, Madde.id)).all()
    return {"tarih": tarih.isoformat(), "bulunan": [madde_json(m, {}, tarih, ad_eslemeleri(a)) for m in bulunanlar], **onbellek}


# ---------------------------------------------------------------- Claude ile düzeltme

def duzeltme_paketi(db: Session, kullanici: Kullanici, tarih: date) -> list[Madde]:
    """Bugünkü rapora girecek ve henüz düzeltilmemiş maddeler."""
    maddeler = db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, Madde.tikli.is_(True),
        or_(Madde.tur.in_(("surekli", "devam")), Madde.tarih == tarih),
    ).order_by(Madde.tur, Madde.sira, Madde.id)).all()
    ifadeler = bugunku_ifadeler(db, kullanici.id, tarih)
    acik = acik_bulunan_kaynaklari(ayar_satiri(db, kullanici))
    paket = []
    for m in maddeler:
        if m.tur == "surekli":
            secilir = m.id not in ifadeler
        elif m.tur == "bugun":  # e-postayla gelen notlar ve sesle eklenenler elle maddeler gibi düzeltilir
            secilir = m.kaynak in ELLE_KAYNAKLARI and not m.gizli and not m.kullanici_duzenledi and not m.metin_ai
        elif m.tur == "bulunan":
            secilir = m.kaynak in acik and not m.gizli and not m.kullanici_duzenledi and not m.metin_ai
        else:  # devam
            secilir = not m.kullanici_duzenledi and not m.metin_ai
        if secilir:
            paket.append(m)
    return paket


def claude_hakki_al(db: Session, user_id: int, tarih: date) -> ClaudeKullanim | None:
    """Çağrıdan önce günün sayacını bir artırır; sınır dolduysa None. Satır yoksa açılır (aynı anda açılırsa yeniden okunur)."""
    with _kullanici_kilidi(user_id, "kota"):
        for deneme in range(2):
            satir = db.scalar(select(ClaudeKullanim).where(ClaudeKullanim.user_id == user_id, ClaudeKullanim.tarih == tarih))
            if satir is None:
                satir = ClaudeKullanim(user_id=user_id, tarih=tarih, cagri=0, girdi_token=0, cikti_token=0)
                db.add(satir)
            if (satir.cagri or 0) >= GUNLUK_CLAUDE_SINIRI:
                return None
            satir.cagri = (satir.cagri or 0) + 1
            try:
                db.commit()
                return satir
            except IntegrityError:
                db.rollback()
                if deneme:
                    raise
    return None


def claude_kullanimini_yaz(db: Session, satir: ClaudeKullanim) -> None:
    """Son yanıtın usage alanını günün satırına ekler."""
    kullanim = servisler.son_kullanim()
    db.execute(ClaudeKullanim.__table__.update().where(ClaudeKullanim.id == satir.id).values(
        girdi_token=ClaudeKullanim.girdi_token + kullanim["girdi"],
        cikti_token=ClaudeKullanim.cikti_token + kullanim["cikti"],
    ))
    db.commit()


def aylik_claude_cagrilari(db: Session, tarih: date) -> dict[int, int]:
    """user_id → bu ay yapılan Claude çağrısı."""
    satirlar = db.execute(select(ClaudeKullanim.user_id, func.sum(ClaudeKullanim.cagri)).where(
        ClaudeKullanim.tarih >= tarih.replace(day=1), ClaudeKullanim.tarih <= tarih,
    ).group_by(ClaudeKullanim.user_id))
    return {uid: int(toplam or 0) for uid, toplam in satirlar}


def duzeltmeyi_uygula(db: Session, kullanici: Kullanici, paket: list[Madde], tarih: date, anahtar: str) -> dict:
    """Tek Claude çağrısı; hata olursa hiçbir maddeye yazılmaz. Günlük sınır dolduysa çağrılmaz, ham metin kalır.
    tarih düzeltilen gündür; kota her zaman çağrının yapıldığı günün (bugünün) sayacından düşer.
    Kategorisi olmayan elle/not/ses maddeleri için Claude'dan kategori önerisi de istenir (aynı çağrıda)."""
    if not paket:
        return {"duzeltilen": 0, "gonderilen": 0, "hatalar": []}
    hak = claude_hakki_al(db, kullanici.id, bugun())
    if hak is None:
        return {"atlandi": "günlük sınır", "duzeltilen": 0, "gonderilen": len(paket), "hatalar": []}
    secilebilir = [k for k in kategorileri_hazirla(db, kullanici) if not k.sistem]
    girdiler = []
    for m in paket:
        girdi = {"id": m.id, "tur": m.tur, "metin": m.metin}
        if m.tur == "devam" and m.asama:
            girdi["asama"] = m.asama
        if secilebilir and m.tur == "bugun" and m.kaynak in ELLE_KAYNAKLARI and m.kategori_id is None:
            girdi["kategori_sec"] = True
        girdiler.append(girdi)
    kategori_listesi = [{"id": k.id, "ad": k.ad} for k in secilebilir] if any(g.get("kategori_sec") for g in girdiler) else None
    son_raporlar = list(db.scalars(select(Rapor.metin).where(
        Rapor.user_id == kullanici.id, Rapor.tur == "gunluk", Rapor.tarih < tarih,
    ).order_by(Rapor.tarih.desc()).limit(3)))
    ayar = ayar_satiri(db, kullanici)
    proje_adi = ayar.proje_adi or ""
    kendi = kendi_alanlar(ayar, kullanici)
    servisler.kullanimi_sifirla()
    try:
        sonuc, oneriler = servisler.claude_duzelt_kategorili(
            girdiler, son_raporlar, anahtar, proje_adi, kategoriler=kategori_listesi,
            kendi_sirket=servisler.kendi_sirket_adlari(kendi, alan_sozlugu(ayar)), eslemeler=ad_eslemeleri(ayar))
    except servisler.ClaudeHatasi as e:
        return {"duzeltilen": 0, "gonderilen": len(paket), "hatalar": [f"Claude: {e}; ham metin kullanılıyor"]}
    except Exception as e:
        return {"duzeltilen": 0, "gonderilen": len(paket), "hatalar": [f"Claude: beklenmeyen hata ({e.__class__.__name__}); ham metin kullanılıyor"]}
    finally:
        claude_kullanimini_yaz(db, hak)
    gecerli_kategori = {k.id for k in secilebilir}
    istenen = {g["id"] for g in girdiler if g.get("kategori_sec")}
    for m in paket:  # sistem ya da başkasının kategorisi yok sayılır
        if m.id in istenen and oneriler.get(m.id) in gecerli_kategori:
            m.kategori_id = oneriler[m.id]
    duzeltilen = 0
    for m in paket:
        metin = sonuc.get(m.id)
        if not metin:
            continue
        duzeltilen += 1
        if m.tur == "surekli":
            db.add(GunlukIfade(user_id=kullanici.id, item_id=m.id, tarih=tarih, metin_ai=metin))
        else:
            m.metin_ai = metin
            m.ai_tarih = tarih
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"duzeltilen": 0, "gonderilen": len(paket), "hatalar": ["Düzeltme aynı anda iki kez çalıştı; sayfayı yenileyin"]}
    hatalar = [] if duzeltilen == len(paket) else [f"{len(paket) - duzeltilen} madde için Claude yanıt vermedi; ham metin kullanılıyor"]
    return {"duzeltilen": duzeltilen, "gonderilen": len(paket), "hatalar": hatalar}


@router.post("/duzelt")
def duzelt(tarih: str | None = None, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    tarih = gun_sec(tarih)
    anahtar = ai_anahtari()
    if not anahtar:
        return {"atlandi": "anahtar yok", "duzeltilen": 0, "gonderilen": 0, "hatalar": []}
    with _kullanici_kilidi(kullanici.id, "duzelt"):
        paket = duzeltme_paketi(db, kullanici, tarih)
        sonuc = duzeltmeyi_uygula(db, kullanici, paket, tarih, anahtar)
    ifadeler = bugunku_ifadeler(db, kullanici.id, tarih, [m.id for m in paket])
    eslemeler = ad_eslemeleri(ayar_satiri(db, kullanici))
    return {**sonuc, "maddeler": [madde_json(m, ifadeler, tarih, eslemeler) for m in paket]}


# ---------------------------------------------------------------- sesle madde ekleme

class SesliNot(BaseModel):
    metin: str
    tarih: date | None = None


class SesliMadde(BaseModel):
    metin: str
    tur: Literal["bugun", "devam"] = "bugun"
    asama: str | None = None
    kategori_id: int | None = None


class SesliEkle(BaseModel):
    maddeler: list[SesliMadde]
    tarih: date | None = None


@router.post("/sesli-not")
def sesli_not(govde: SesliNot, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Dikte metnini maddelere böler, hiçbir şey yazmaz. Claude varsa (düzeltme kotasından 1 çağrı) böler, kategori ve
    'devam' önerir; anahtar yoksa, kota doluysa ya da yanıt bozuksa metin basitçe bölünür, kategori boş kalır."""
    tarih = gun_sec(govde.tarih)
    metin = govde.metin.strip()
    if not metin:
        raise HTTPException(status_code=422, detail="Metin boş olamaz")
    if len(metin) > SESLI_NOT_SINIRI:
        raise HTTPException(status_code=422, detail=f"Metin en fazla {SESLI_NOT_SINIRI} karakter olabilir")
    secilebilir = [k for k in kategorileri_hazirla(db, kullanici) if not k.sistem]
    sonuc: dict = {"tarih": tarih.isoformat(), "hatalar": []}
    maddeler = None
    anahtar = ai_anahtari()
    hak = claude_hakki_al(db, kullanici.id, bugun()) if anahtar else None
    if not anahtar:
        sonuc["atlandi"] = "anahtar yok"
    elif hak is None:
        sonuc["atlandi"] = "günlük sınır"
    else:
        ayar = ayar_satiri(db, kullanici)
        servisler.kullanimi_sifirla()
        try:
            maddeler = servisler.claude_sesli_bol(
                metin, anahtar, [{"id": k.id, "ad": k.ad} for k in secilebilir], ayar.proje_adi or "",
                servisler.kendi_sirket_adlari(kendi_alanlar(ayar, kullanici), alan_sozlugu(ayar)),
                eslemeler=ad_eslemeleri(ayar))
        except servisler.ClaudeHatasi as e:
            sonuc["hatalar"].append(f"Claude: {e}; metin basitçe bölündü")
        except Exception as e:
            sonuc["hatalar"].append(f"Claude: beklenmeyen hata ({e.__class__.__name__}); metin basitçe bölündü")
        finally:
            claude_kullanimini_yaz(db, hak)
    if maddeler is None:
        maddeler = servisler.sesi_basitce_bol(metin)
    gecerli = {k.id for k in secilebilir}
    for m in maddeler:  # sistem ya da başkasının kategorisi yok sayılır
        if m["kategori_id"] not in gecerli:
            m["kategori_id"] = None
    return {**sonuc, "maddeler": maddeler}


@router.post("/sesli-not/ekle", status_code=201)
def sesli_not_ekle(govde: SesliEkle, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Onaylanan maddeler: 'bugun' → seçili günün kaynak='ses' satırı (düzeltme paketine girer), 'devam' → devam eden iş."""
    tarih = gun_sec(govde.tarih)
    if not govde.maddeler:
        raise HTTPException(status_code=422, detail="Eklenecek madde yok")
    for m in govde.maddeler:
        if not m.metin.strip():
            raise HTTPException(status_code=422, detail="Metin boş olamaz")
        if m.kategori_id is not None and kullanici_kategorisi(db, kullanici, m.kategori_id).sistem:
            raise HTTPException(status_code=422, detail="Sistem kategorisi maddeye atanamaz")
    sira = {"bugun": sonraki_sira(db, kullanici, "bugun", tarih), "devam": sonraki_sira(db, kullanici, "devam")}
    yeniler = []
    for m in govde.maddeler:
        madde = Madde(user_id=kullanici.id, tur=m.tur, kaynak="ses", metin=m.metin.strip(), kategori_id=m.kategori_id,
                      tikli=True, sira=sira[m.tur])
        if m.tur == "bugun":
            madde.tarih = tarih
        else:
            madde.asama = (m.asama or "").strip() or None
        sira[m.tur] += 1
        db.add(madde)
        yeniler.append(madde)
    db.commit()
    eslemeler = ad_eslemeleri(ayar_satiri(db, kullanici))
    return {"maddeler": [madde_json(m, {}, tarih, eslemeler) for m in yeniler]}


# ---------------------------------------------------------------- rapor geçmişi

class RaporYeni(BaseModel):
    metin: str
    tur: Literal["gunluk", "haftalik"] = "gunluk"
    hafta_baslangic: date | None = None
    tarih: date | None = None  # günlük raporun günü; ?tarih= ile de verilebilir, boşsa bugün


class HaftalikIstek(BaseModel):
    hafta_baslangic: date


def pazartesi_mi(tarih: date | None) -> date:
    if tarih is None or tarih.weekday() != 0:
        raise HTTPException(status_code=422, detail="hafta_baslangic bir Pazartesi olmalı")
    return tarih


def rapor_json(r: Rapor, eslemeler: list[dict] | None = None) -> dict:
    """metin ad eşlemeleri uygulanmış hâliyle döner (Geçmiş'te görülen ve kopyalanan)."""
    metin = servisler.ad_esle(r.metin, eslemeler)
    satirlar = metin.splitlines()
    return {
        "id": r.id, "tarih": r.tarih.isoformat(), "tur": r.tur,
        "hafta_baslangic": r.hafta_baslangic.isoformat() if r.hafta_baslangic else None,
        "metin": metin,
        "ilk_satir": next((x.strip() for x in satirlar if x.strip()), ""),
        "madde_sayisi": sum(1 for x in satirlar if x.lstrip().startswith("•")),
        "olusturma": zaman_iso(r.olusturma),
        "gonderim": r.gonderim or "elle",
        "bicim": r.bicim,
        "istatistik": r.istatistik,
        "yapi": servisler.yapi_esle(r.yapi, eslemeler) if r.yapi else None,
    }


def rapor_yaz(db: Session, user_id: int, tarih: date, tur: str, metin: str, hafta: date | None = None,
              gonderim: str | None = None, bicim: str | None = None, istatistik: dict | None = None,
              yapi: dict | None = None) -> Rapor:
    """(kullanıcı, gün, tür, biçim) başına tek satır; son yazan kazanır. gonderim, istatistik ve yapi verilmezse eski
    değer korunur. bicim, istatistik ve yapi yalnız aylık/yıllık özette dolu."""
    kosul = (Rapor.user_id == user_id, Rapor.tarih == tarih, Rapor.tur == tur,
             Rapor.bicim == bicim if bicim else Rapor.bicim.is_(None))
    with _kullanici_kilidi(user_id, "rapor"):
        for deneme in range(2):
            rapor = db.scalar(select(Rapor).where(*kosul))
            if rapor is None:
                rapor = Rapor(user_id=user_id, tarih=tarih, tur=tur, hafta_baslangic=hafta, metin=metin, bicim=bicim)
                db.add(rapor)
            rapor.metin = metin
            rapor.olusturma = simdi()  # son kopyalanan kazanır
            if gonderim:
                rapor.gonderim = gonderim
            if istatistik is not None:
                rapor.istatistik = istatistik
            if yapi is not None:
                rapor.yapi = yapi
            try:
                db.commit()
                return rapor
            except IntegrityError:
                db.rollback()
                if deneme:
                    raise
    return rapor


@router.post("/raporlar")
def rapor_kaydet(
    govde: RaporYeni, tarih: str | None = None,
    kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    metin = govde.metin.strip()
    if not metin:
        raise HTTPException(status_code=422, detail="Rapor metni boş olamaz")
    if govde.tur == "haftalik":
        hafta = pazartesi_mi(govde.hafta_baslangic)
        tarih = hafta
    else:
        hafta, tarih = None, gun_sec(govde.tarih or tarih)
    return rapor_json(rapor_yaz(db, kullanici.id, tarih, govde.tur, metin, hafta), ad_eslemeleri(ayar_satiri(db, kullanici)))


@router.get("/raporlar")
def raporlari_listele(
    q: str = "", tur: str = "", limit: int = 50, offset: int = 0,
    kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    kosullar = [Rapor.user_id == kullanici.id]
    if tur:
        if tur not in RAPOR_TURLERI:
            raise HTTPException(status_code=422, detail="tur gunluk, haftalik, aylik ya da yillik olmalı")
        kosullar.append(Rapor.tur == tur)
    q = q.strip()
    if q:
        kacisli = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        kosullar.append(Rapor.metin.ilike(f"%{kacisli}%", escape="\\"))
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    toplam = db.scalar(select(func.count()).select_from(Rapor).where(*kosullar))
    raporlar = db.scalars(
        select(Rapor).where(*kosullar).order_by(Rapor.tarih.desc(), Rapor.id.desc()).limit(limit).offset(offset)
    ).all()
    eslemeler = ad_eslemeleri(ayar_satiri(db, kullanici))
    return {"toplam": toplam, "raporlar": [rapor_json(r, eslemeler) for r in raporlar]}


@router.post("/haftalik")
def haftalik_ozet(govde: HaftalikIstek, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    pazartesi = pazartesi_mi(govde.hafta_baslangic)
    raporlar = db.scalars(select(Rapor).where(
        Rapor.user_id == kullanici.id, Rapor.tur == "gunluk",
        Rapor.tarih >= pazartesi, Rapor.tarih <= pazartesi + timedelta(days=6),
    ).order_by(Rapor.tarih)).all()
    if not raporlar:
        raise HTTPException(status_code=400, detail="Bu hafta için kayıtlı günlük rapor yok")
    anahtar = ai_anahtari()
    if not anahtar:
        raise HTTPException(status_code=400, detail="Claude anahtarı tanımlı değil (ANTHROPIC_API_KEY); haftalık özet üretilemez")
    bitis = max(raporlar[-1].tarih, min(pazartesi + timedelta(days=4), bugun()))
    hak = claude_hakki_al(db, kullanici.id, bugun())
    if hak is None:
        raise HTTPException(status_code=429, detail=f"Bugünkü Claude hakkın doldu (günde {GUNLUK_CLAUDE_SINIRI}); yarın yeniden dene")
    servisler.kullanimi_sifirla()
    try:
        metin = servisler.claude_haftalik(
            [(r.tarih, r.metin) for r in raporlar], servisler.hafta_basligi(pazartesi, bitis), anahtar,
            eslemeler=ad_eslemeleri(ayar_satiri(db, kullanici)),
        )
    except servisler.ClaudeHatasi as e:
        raise HTTPException(status_code=502, detail=f"Claude: {e}") from e
    finally:
        claude_kullanimini_yaz(db, hak)
    return {"metin": metin, "hafta_baslangic": pazartesi.isoformat(), "rapor_sayisi": len(raporlar)}


# ---------------------------------------------------------------- aylık / yıllık özet (A1, A1v2, A1v3)

OZET_TURLERI = ("aylik", "yillik")
OZET_METIN_SINIRI = 40_000  # karakter; aylık girdi bunu aşarsa günlük madde sayısı kısaltılır, haftalık özetler eklenir
OZET_KISA_MADDE = 12  # kısaltılmış girdide gün başına ilk N madde
OZET_ORNEK_MADDE = 30  # yıllıkta dökümü olmayan ayın örnek madde sayısı
# İstatistikteki kaynak grupları (ekrandaki sırayla): (anahtar, etiket, maddenin kaynak alanları). E-posta ve dosyada iki
# sağlayıcı birleşir. Uygulama grubunun etiketi, kullanıcının GitHub/Medusa kaynaklı kategorisi varsa onun adıdır.
KAYNAK_GRUPLARI = (
    ("eposta", "E-posta", ("eposta", "outlook")), ("commit", "Uygulama çalışmaları", ("medusa",)),
    ("elle", "Elle eklenen", ("elle",)), ("ses", "Sesle eklenen", ("ses",)), ("not", "Notlar", ("not",)),
    ("dosya", "Dosyalar", ("drive", "onedrive")), ("toplanti", "Toplantılar", ("takvim",)),
)
OZET_ILK = 5  # istatistikte ilk 5 kategori ve kurum/kişi
TR_AYLAR = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık")


class OzetIstek(BaseModel):
    tur: Literal["aylik", "yillik"]
    donem: str  # '2026-09' | '2026'
    bicim: Literal["patron", "basari"] = "patron"


class OzetKaydet(OzetIstek):
    metin: str | None = None  # eski (A1) düz metin düzenlemesi
    yapi: dict | None = None  # A1v2: alan bazlı düzenleme


def ozet_donemi(tur: str, donem: str) -> tuple[date, date]:
    """'2026-09' → (1 Eylül, 30 Eylül); '2026' → (1 Ocak, 31 Aralık). Biçim bozuksa ya da dönem gelecekteyse 422."""
    donem = (donem or "").strip()
    desen = r"^\d{4}-(0[1-9]|1[0-2])$" if tur == "aylik" else r"^\d{4}$"
    if tur not in OZET_TURLERI or not re.match(desen, donem):
        raise HTTPException(status_code=422, detail="Dönem aylıkta YYYY-AA, yıllıkta YYYY biçiminde olmalı")
    if tur == "aylik":
        bas = date(int(donem[:4]), int(donem[5:]), 1)
        bit = (bas.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    else:
        bas, bit = date(int(donem), 1, 1), date(int(donem), 12, 31)
    if bas > bugun():
        raise HTTPException(status_code=422, detail="Gelecekteki bir dönemin özeti çıkarılamaz")
    return bas, bit


def _gunler(bas: date, bit: date):
    for n in range((bit - bas).days + 1):
        yield bas + timedelta(days=n)


def _ilk_n(sayilar: dict[str, int], eslemeler: list[dict]) -> list[dict]:
    birlesik: dict[str, int] = {}
    for ad, sayi in sayilar.items():  # eşlemeden sonra aynı ada düşenler toplanır
        ad = servisler.ad_esle(ad, eslemeler).strip()
        if ad:
            birlesik[ad] = birlesik.get(ad, 0) + sayi
    return [{"ad": ad, "sayi": n} for ad, n in sorted(birlesik.items(), key=lambda x: (-x[1], x[0]))[:OZET_ILK]]


def tarih_araligi(bas: date, bit: date) -> str:
    """'17–29 Eylül', '28 Eylül – 2 Ekim', '17 Eylül'; yıl yazılmaz (dönem adında var)."""
    if bas == bit:
        return f"{bas.day} {TR_AYLAR[bas.month - 1]}"
    if (bas.year, bas.month) == (bit.year, bit.month):
        return f"{bas.day}–{bit.day} {TR_AYLAR[bit.month - 1]}"
    return f"{bas.day} {TR_AYLAR[bas.month - 1]} – {bit.day} {TR_AYLAR[bit.month - 1]}"


def kullanim_araligi(bas: date, son: date, rapor_gunleri: set[date]) -> dict | None:
    """Kapsamanın gerçek aralığı: ilk rapor günü dönem başından sonraysa ilk–son rapor günü ve "(aracın kullanıldığı
    dönem)"; değilse dönem başı – bugün/dönem sonu. Rapor yoksa None."""
    if not rapor_gunleri:
        return None
    ilk, sonuncu = min(rapor_gunleri), max(rapor_gunleri)
    arac = ilk > bas
    a, b = (ilk, sonuncu) if arac else (bas, max(son, sonuncu))
    return {"baslangic": a.isoformat(), "bitis": b.isoformat(), "arac": arac,
            "metin": tarih_araligi(a, b) + (" (aracın kullanıldığı dönem)" if arac else "")}


def donem_girenleri(db: Session, kullanici: Kullanici, bas: date, bit: date) -> tuple[list[Rapor], list[Madde], list[Madde]]:
    """(dönemin günlük raporları, dönemin bugün/bulunan maddeleri, rapora girenler). Rapora giren: günlük raporu
    kaydedilmiş günün tikli, gizlenmemiş elle/not/ses ya da bulunan maddesi."""
    raporlar = db.scalars(select(Rapor).where(
        Rapor.user_id == kullanici.id, Rapor.tur == "gunluk", Rapor.tarih >= bas, Rapor.tarih <= bit,
    ).order_by(Rapor.tarih)).all()
    rapor_gunleri = {r.tarih for r in raporlar}
    donem_maddeleri = db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, Madde.tarih >= bas, Madde.tarih <= bit, Madde.tur.in_(("bugun", "bulunan")),
    ).order_by(Madde.tarih, Madde.id)).all()
    girenler = [m for m in donem_maddeleri if m.tarih in rapor_gunleri and m.tikli and not m.gizli
                and (m.tur == "bulunan" or m.kaynak in ELLE_KAYNAKLARI)]
    return list(raporlar), list(donem_maddeleri), girenler


def kategori_baglami(db: Session, kullanici: Kullanici) -> tuple[KategoriBaglami, list[Kategori]]:
    """Kullanıcının kategorileri (yoksa açılmaz)."""
    kategoriler = list(db.scalars(select(Kategori).where(Kategori.user_id == kullanici.id).order_by(Kategori.sira, Kategori.id)))
    return KategoriBaglami(kategoriler), kategoriler


def devam_rapor_metni(m: Madde) -> str:
    """Devam eden işin rapordaki adı: Claude düzeltmesi (iş + aşama tek cümle) varsa o, yoksa kullanıcının yazdığı."""
    if not m.kullanici_duzenledi and m.ai_kullan is not False and m.metin_ai:
        return m.metin_ai
    return m.metin


def donem_istatistigi(db: Session, kullanici: Kullanici, bas: date, bit: date) -> dict:
    """Claude'suz dönem istatistiği. Madde sayımları günlük raporu kaydedilmiş günlerin tikli, gizlenmemiş maddelerinden;
    toplam madde ve kategoriler kayıtlı rapor metinlerinden. İş günü hatırlatma günlerine göre, dönemin başı (ya da hesabın
    açıldığı / ilk raporun günü) ile bugün arasında sayılır. Kaynaklarda sıfır olan satır yoktur; tamamlanan ve açık
    işlerin metni rapor metnidir (düzeltilmiş, ad eşlemeli). A1v3: aylıkta gunluk_dagilim (ayın her günü: madde,
    rapor_var, is_gunu — hatırlatma günlerine göre takvim işareti —, kullanim_oncesi, gelecek), yıllıkta aylik_dagilim
    (12 ay) ve en_yogun_ay; ortalama, en_yogun_gun, otomatik_gun_sayisi, son_tamamlanan, yazisma."""
    a = ayar_satiri(db, kullanici)
    eslemeler = ad_eslemeleri(a)
    son = min(bit, bugun())
    raporlar, donem_maddeleri, girenler = donem_girenleri(db, kullanici, bas, bit)
    rapor_gunleri = {r.tarih for r in raporlar}

    ilk = utc(kullanici.olusturma).astimezone(servisler.ISTANBUL).date() if kullanici.olusturma else bas
    ilk_rapor = db.scalar(select(func.min(Rapor.tarih)).where(Rapor.user_id == kullanici.id, Rapor.tur == "gunluk"))
    ilk_kullanim = min(ilk, ilk_rapor) if ilk_rapor else ilk
    ilk = max(bas, ilk_kullanim)
    calisma_gunleri = hatirlatma_ayari(a)["gunler"]
    is_gunleri = {g for g in _gunler(ilk, son) if g.isoweekday() in calisma_gunleri} if ilk <= son else set()

    baglam, _ = kategori_baglami(db, kullanici)
    uygulama = next((k for k in baglam.kategoriler if not k.sistem and {"github", "medusa"} & set(k.kaynaklar or [])), None)
    kaynaklar = []
    for k, ad, grup in KAYNAK_GRUPLARI:
        sayi = sum(1 for m in girenler if m.kaynak in grup)
        if sayi:
            kaynaklar.append({"anahtar": k, "ad": servisler.ad_esle(uygulama.ad, eslemeler) if k == "commit" and uygulama else ad,
                              "sayi": sayi})

    kurumlar: dict[str, int] = {}
    for m in girenler:
        if m.kaynak in ("eposta", "outlook"):
            for ad, adet in servisler.eposta_hedefleri(m.metin):
                kurumlar[ad] = kurumlar.get(ad, 0) + adet

    # A1v3 "Ayın özeti": gün gün (aylık) ya da ay ay (yıllık) rapora giren madde; sayım rapor metninden (toplam_madde ile aynı)
    gun_maddesi = {r.tarih: servisler.rapor_madde_sayisi(r.metin) for r in raporlar}
    bugun_ = bugun()
    gunluk = [{"tarih": g.isoformat(), "madde": gun_maddesi.get(g, 0), "rapor_var": g in rapor_gunleri,
               "is_gunu": g.isoweekday() in calisma_gunleri, "kullanim_oncesi": g < ilk_kullanim, "gelecek": g > bugun_}
              for g in _gunler(bas, bit)]
    en_yogun = max(gun_maddesi.items(), key=lambda x: (x[1], -x[0].toordinal())) if gun_maddesi else None

    tamamlanan = [{"metin": servisler.ad_esle(madde_rapor_metni(m, None, m.tarih), eslemeler), "tarih": m.tarih.isoformat()}
                  for m in donem_maddeleri if m.tur == "bugun" and m.kaynak == "elle" and not m.gizli
                  and (servisler.TAMAMLANDI.search(m.metin or "") or servisler.TAMAMLANDI.search(madde_rapor_metni(m, None, m.tarih)))]
    bitis_ani = datetime.combine(bit + timedelta(days=1), time.min, servisler.ISTANBUL)
    devamlar = [m for m in db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, Madde.tur == "devam").order_by(Madde.olusturma, Madde.id))
        if m.olusturma is None or utc(m.olusturma) < bitis_ani]
    acik = [{"metin": servisler.ad_esle(devam_rapor_metni(m), eslemeler),
             "asama": "" if devam_rapor_metni(m) != m.metin else servisler.ad_esle(m.asama or "", eslemeler),
             "gun": bekleme_gunu(m.olusturma, son)} for m in devamlar]
    onemli = sum(1 for m in girenler if m.onemli) + sum(
        1 for m in devamlar if m.onemli and m.olusturma and utc(m.olusturma) >= datetime.combine(bas, time.min, servisler.ISTANBUL))

    toplam_madde = sum(gun_maddesi.values())
    dagilim: dict = {}
    if (bit - bas).days > 31:  # yıllık: 12 aylık sütun
        aylar = []
        for ay in range(1, 13):
            ay_bas = date(bas.year, ay, 1)
            ay_gunleri = [x for x in gunluk if x["tarih"][5:7] == f"{ay:02d}"]
            aylar.append({"ay": ay_bas.isoformat()[:7], "madde": sum(x["madde"] for x in ay_gunleri),
                          "rapor_gunu": sum(1 for x in ay_gunleri if x["rapor_var"]),
                          "is_gunu": sum(1 for g in is_gunleri if g.month == ay),
                          "kullanim_oncesi": ay_sonu(ay_bas) < ilk_kullanim, "gelecek": ay_bas > bugun_})
        dagilim["aylik_dagilim"] = aylar
        yogun_ay = max((x for x in aylar if x["madde"]), key=lambda x: x["madde"], default=None)
        dagilim["en_yogun_ay"] = {"ay": yogun_ay["ay"], "madde": yogun_ay["madde"]} if yogun_ay else None
    else:
        dagilim["gunluk_dagilim"] = gunluk
    son_tamamlanan = max(tamamlanan, key=lambda x: x["tarih"]) if tamamlanan else None

    return {
        "baslangic": bas.isoformat(), "bitis": son.isoformat(),
        "kullanim": kullanim_araligi(bas, son, rapor_gunleri),
        "rapor_gunu": len(raporlar),
        "elle": sum(1 for r in raporlar if (r.gonderim or "elle") != "otomatik"),
        "otomatik": sum(1 for r in raporlar if r.gonderim == "otomatik"),
        "is_gunu": len(is_gunleri),
        "raporlu_is_gunu": len(rapor_gunleri & is_gunleri),
        "kapsama": round(100 * len(rapor_gunleri & is_gunleri) / len(is_gunleri)) if is_gunleri else None,
        "toplam_madde": toplam_madde,
        "kaynaklar": kaynaklar,
        "kategoriler": _ilk_n(servisler.kategori_sayilari([r.metin for r in raporlar]), eslemeler),
        "kurumlar": _ilk_n(kurumlar, eslemeler),
        "yazisma": sum(kurumlar.values()),  # kurum/kişilere giden bütün e-postalar (ilk 5 ile sınırlı değil)
        "tamamlanan": tamamlanan,
        "acik": acik,
        "onemli": onemli,
        "ortalama": round(toplam_madde / len(raporlar), 1) if raporlar else None,  # rapor günü başına madde
        "en_yogun_gun": {"tarih": en_yogun[0].isoformat(), "madde": en_yogun[1]} if en_yogun and en_yogun[1] else None,
        "otomatik_gun_sayisi": sum(1 for r in raporlar if r.gonderim == "otomatik"),
        "son_tamamlanan": son_tamamlanan,
        **dagilim,
    }


def _surekli_girdisi(db: Session, kullanici: Kullanici, eslemeler: list[dict]) -> list[str]:
    return [servisler.ad_esle(x.split("|")[0].strip(), eslemeler) for x in surekli_metinleri(db, kullanici) if x.strip()]


def aylik_girdi(db: Session, kullanici: Kullanici, bas: date, bit: date, ist: dict,
                eslemeler: list[dict]) -> tuple[dict, str, dict]:
    """(Claude girdisi, kaynak, bilinen maddeler). Girdi: raporlu günlerin tikli, gizlenmemiş maddeleri id + etkin kategori
    + kaynak + rapor metniyle; sürekli, devam eden ve tamamlanan işler. Maddesi olmayan raporlu günlerin rapor metni bağlam
    olarak eklenir. OZET_METIN_SINIRI aşılırsa gün başına ilk OZET_KISA_MADDE madde gider ve o aya düşen haftalık özetler
    bağlam olur. kaynak: gunluk | kisaltilmis | haftalik. bilinen: {madde id: ([id], metin)} — yalnız gönderilenler."""
    raporlar, _, girenler = donem_girenleri(db, kullanici, bas, bit)
    if not raporlar:
        raise HTTPException(status_code=400, detail="Bu ay için kayıtlı günlük rapor yok")
    baglam, kategoriler = kategori_baglami(db, kullanici)
    adlar = {k.id: servisler.ad_esle(k.ad, eslemeler) for k in kategoriler}
    duzen = {(r.tarih, r.item_id): r for r in db.scalars(select(RaporDuzeni).where(
        RaporDuzeni.user_id == kullanici.id, RaporDuzeni.tarih >= bas, RaporDuzeni.tarih <= bit))}
    maddeler = [{
        "id": m.id, "tarih": m.tarih.isoformat(), "kategori": adlar.get(baglam.etkin(m, duzen.get((m.tarih, m.id))), ""),
        "kaynak": servisler.OZET_KAYNAK_ETIKETI.get(m.kaynak, m.kaynak or ""),
        "metin": servisler.ad_esle(madde_rapor_metni(m, None, m.tarih), eslemeler),
    } for m in girenler]
    maddeli = {m.tarih for m in girenler}
    bos_gunler = [r for r in raporlar if r.tarih not in maddeli]
    girdi = {
        "maddeler": maddeler,
        "surekli": _surekli_girdisi(db, kullanici, eslemeler),
        "devam_eden": [{"is": x["metin"], "asama": x["asama"], "gun": x["gun"]} for x in ist["acik"]],
        "tamamlanan": [x["metin"] for x in ist["tamamlanan"]],
    }
    if bos_gunler:
        girdi["gunluk_raporlar"] = [{"tarih": r.tarih.isoformat(), "metin": servisler.ad_esle(r.metin, eslemeler)} for r in bos_gunler]
    kaynak = "gunluk"
    if len(json.dumps(girdi, ensure_ascii=False)) > OZET_METIN_SINIRI:
        gun_sayaci: dict[str, int] = {}
        kisa = []
        for m in maddeler:
            gun_sayaci[m["tarih"]] = gun_sayaci.get(m["tarih"], 0) + 1
            if gun_sayaci[m["tarih"]] <= OZET_KISA_MADDE:
                kisa.append(m)
        girdi["maddeler"] = kisa
        if bos_gunler:
            girdi["gunluk_raporlar"] = [{"tarih": r.tarih.isoformat(), "metin": servisler.ad_esle(
                servisler.rapor_kisalt(r.metin, OZET_KISA_MADDE), eslemeler)} for r in bos_gunler]
        haftaliklar = db.scalars(select(Rapor).where(
            Rapor.user_id == kullanici.id, Rapor.tur == "haftalik", Rapor.tarih >= bas - timedelta(days=6), Rapor.tarih <= bit,
        ).order_by(Rapor.tarih)).all()
        kaynak = "kisaltilmis"
        if haftaliklar:
            girdi["haftalik_ozetler"] = [{"hafta": h.tarih.isoformat(), "metin": servisler.ad_esle(h.metin, eslemeler)}
                                         for h in haftaliklar]
            kaynak = "haftalik"
    return girdi, kaynak, {m["id"]: ([m["id"]], m["metin"]) for m in girdi["maddeler"]}


def yillik_aylari(db: Session, kullanici: Kullanici, bas: date) -> tuple[dict[int, Rapor], list[int]]:
    """(ay → seçilen aylık özet (başarı dökümü önce), özeti olmayan ama günlük raporu olan aylar)."""
    ozetler: dict[int, Rapor] = {}
    for r in db.scalars(select(Rapor).where(
        Rapor.user_id == kullanici.id, Rapor.tur == "aylik", Rapor.tarih >= bas, Rapor.tarih <= date(bas.year, 12, 31),
    )):
        if r.tarih.month not in ozetler or r.bicim == "basari":
            ozetler[r.tarih.month] = r
    gunluk_aylari = {t.month for t in db.scalars(select(Rapor.tarih).where(
        Rapor.user_id == kullanici.id, Rapor.tur == "gunluk", Rapor.tarih >= bas, Rapor.tarih <= date(bas.year, 12, 31),
    ))}
    return ozetler, sorted(gunluk_aylari - set(ozetler))


def ay_sonu(bas: date) -> date:
    return (bas.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def yillik_girdi(db: Session, kullanici: Kullanici, bas: date, eslemeler: list[dict]) -> tuple[dict, list[int], dict]:
    """Yıllık girdi aylık yapılardan: başarı dökümü yapısı olan ayın temaları "ay-sıra" anahtarıyla (o temanın madde
    id'lerine açılır); yapısı olmayan ayda (patron özeti, eski düz metin ya da hiç özet yok) ayın istatistiği + rapora
    giren maddelerden tekrarsız ilk OZET_ORNEK_MADDE örnek (id'siyle) ve varsa özet metni. Eksik aylar için aylık özet
    üretilmez. Dönen: (girdi, özetsiz aylar, bilinen)."""
    ozetler, ozetsiz = yillik_aylari(db, kullanici, bas)
    if not ozetler and not ozetsiz:
        raise HTTPException(status_code=400, detail="Bu yıl için kayıtlı rapor yok")
    aylar, bilinen = [], {}
    for ay in sorted(set(ozetler) | set(ozetsiz)):
        ay_bas = date(bas.year, ay, 1)
        ad = servisler.donem_adi("aylik", ay_bas)
        r = ozetler.get(ay)
        yapi = r.yapi if r is not None and r.bicim == "basari" and isinstance(r.yapi, dict) else None
        if yapi:
            yapi = servisler.yapi_esle(yapi, eslemeler)
            alanlar, n = [], 0
            for a in yapi.get("alanlar") or []:
                temalar = []
                for t in a.get("temalar") or []:
                    n += 1
                    anahtar = f"{ay}-{n}"
                    idler = [i for i in t.get("madde_idleri") or [] if isinstance(i, int) and not isinstance(i, bool)]
                    bilinen[anahtar] = (idler, t.get("ad") or "")
                    temalar.append({"id": anahtar, "ad": t.get("ad") or "", "ozet": t.get("ozet") or "",
                                    "etiketler": t.get("etiketler") or [], "madde_sayisi": len(idler)})
                alanlar.append({"ad": a.get("ad") or "", "temalar": temalar})
            aylar.append({"ay": ad, "one_cikanlar": yapi.get("one_cikanlar") or [], "alanlar": alanlar,
                          "tamamlanan": yapi.get("tamamlanan") or [], "devam_eden": yapi.get("devam_eden") or [],
                          "surekli": yapi.get("surekli") or ""})
            continue
        ist = donem_istatistigi(db, kullanici, ay_bas, ay_sonu(ay_bas))
        ornek, gorulen = [], set()
        for m in donem_girenleri(db, kullanici, ay_bas, ay_sonu(ay_bas))[2]:
            metin = servisler.ad_esle(madde_rapor_metni(m, None, m.tarih), eslemeler)
            if metin.strip() and servisler._kucult(metin.strip()) not in gorulen and len(ornek) < OZET_ORNEK_MADDE:
                gorulen.add(servisler._kucult(metin.strip()))
                ornek.append({"id": m.id, "metin": metin})
                bilinen[m.id] = ([m.id], metin)
        blok = {"ay": ad, "istatistik": servisler.istatistik_metni(ist), "maddeler": ornek}
        if r is not None:
            blok["ozet_metni"] = servisler.ad_esle(r.metin, eslemeler)
        aylar.append(blok)
    return {"aylar": aylar}, ozetsiz, bilinen


def ozet_sayilari(ist: dict, yapi: dict) -> dict:
    """Belgedeki sayılar sunucudan: istatistik + yapıdaki listelerin uzunlukları (Claude'un sayılarına güvenilmez)."""
    k = {x["anahtar"]: x["sayi"] for x in ist.get("kaynaklar") or []}
    tamamlanan = yapi.get("tamamlanan") if yapi.get("bicim") == "basari" else None
    return {
        "rapor_gunu": ist.get("rapor_gunu", 0), "raporlu_is_gunu": ist.get("raporlu_is_gunu", ist.get("rapor_gunu", 0)),
        "is_gunu": ist.get("is_gunu", 0), "kapsama": ist.get("kapsama"), "toplam_madde": ist.get("toplam_madde", 0),
        "eposta": k.get("eposta", 0), "uygulama": k.get("commit", 0), "toplanti": k.get("toplanti", 0),
        "dosya": k.get("dosya", 0), "elle": k.get("elle", 0) + k.get("ses", 0) + k.get("not", 0),
        "tamamlanan": len(tamamlanan) if tamamlanan else len(ist.get("tamamlanan") or []),
        "acik": len(ist.get("acik") or []),
        "kurum": (ist.get("kurumlar") or [None])[0],
        "yazisma": ist.get("yazisma", sum(x["sayi"] for x in ist.get("kurumlar") or [])),
        "kurum_adlari": [x["ad"] for x in (ist.get("kurumlar") or [])[:2]],
        "kapsam": (ist.get("kullanim") or {}).get("metin"),
        "madde_sayisi": sum(a.get("madde_sayisi", 0) for a in yapi.get("alanlar") or []),
    }


def ozet_yapisi(tur: str, bicim: str, bas: date, yapi: dict, ist: dict) -> tuple[dict, str]:
    """Doğrulanmış yapıya başlık ve hesaplanan sayılar eklenir; (yapı, düz metin)."""
    baslik = servisler.ozet_basligi(tur, bicim, bas)
    yapi = {"surum": 2, **yapi, "baslik": baslik}
    yapi["sayilar"] = ozet_sayilari(ist, yapi)
    return yapi, servisler.ozet_metni(baslik, yapi)


def ozet_kaydi(db: Session, kullanici: Kullanici, tur: str, bas: date, bicim: str) -> Rapor | None:
    return db.scalar(select(Rapor).where(Rapor.user_id == kullanici.id, Rapor.tur == tur, Rapor.tarih == bas,
                                         Rapor.bicim == bicim))


def ozet_kayitlari(db: Session, kullanici: Kullanici, tur: str, bas: date, eslemeler: list[dict]) -> dict:
    kayitlar = {r.bicim: r for r in db.scalars(select(Rapor).where(
        Rapor.user_id == kullanici.id, Rapor.tur == tur, Rapor.tarih == bas, Rapor.bicim.in_(OZET_BICIMLERI)))}
    return {b: rapor_json(kayitlar[b], eslemeler) if b in kayitlar else None for b in OZET_BICIMLERI}


def ay_adlari(bas: date, aylar: list[int]) -> list[str]:
    return [servisler.donem_adi("aylik", date(bas.year, ay, 1)) for ay in aylar]


@router.get("/ozet")
def ozet_getir(tur: str = "aylik", donem: str = "", kullanici: Kullanici = Depends(aktif_kullanici),
               db: Session = Depends(oturum)) -> dict:
    """Dönemin güncel istatistiği ve kayıtlı özetleri (biçim başına biri ya da null)."""
    bas, bit = ozet_donemi(tur, donem)
    eslemeler = ad_eslemeleri(ayar_satiri(db, kullanici))
    sonuc = {
        "tur": tur, "donem": donem.strip(), "donem_adi": servisler.donem_adi(tur, bas),
        "basliklar": {b: servisler.ozet_basligi(tur, b, bas) for b in OZET_BICIMLERI},
        "istatistik": donem_istatistigi(db, kullanici, bas, bit),
        "ozetler": ozet_kayitlari(db, kullanici, tur, bas, eslemeler),
    }
    if tur == "yillik":
        sonuc["ozetsiz_aylar"] = ay_adlari(bas, yillik_aylari(db, kullanici, bas)[1])
    return sonuc


@router.post("/ozet")
def ozet_uret(govde: OzetIstek, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Aylık/yıllık Yönetici Özeti ('patron') ya da Performans Özeti ('basari'); 1 Claude çağrısı (günlük sayaçtan; JSON
    bozuksa ya da Yönetici Özeti sınırları aşılırsa aynı hakla bir yeniden istem). Claude'un JSON'u doğrulanır, sayılar sunucuda hesaplanır; (tür, dönem, biçim) satırına yapi + düz
    metin yazılır. Claude hatasında ya da iki kez bozuk JSON'da 502 ve kayıtlı özet değişmez."""
    bas, bit = ozet_donemi(govde.tur, govde.donem)
    a = ayar_satiri(db, kullanici)
    eslemeler = ad_eslemeleri(a)
    ist = donem_istatistigi(db, kullanici, bas, bit)
    ozetsiz: list[int] = []
    if govde.tur == "aylik":
        girdi, kaynak, bilinen = aylik_girdi(db, kullanici, bas, bit, ist, eslemeler)
    else:
        (girdi, ozetsiz, bilinen), kaynak = yillik_girdi(db, kullanici, bas, eslemeler), "aylik"
    anahtar = ai_anahtari()
    if not anahtar:
        raise HTTPException(status_code=400, detail="Claude anahtarı tanımlı değil (ANTHROPIC_API_KEY); özet üretilemez")
    hak = claude_hakki_al(db, kullanici.id, bugun())
    if hak is None:
        raise HTTPException(status_code=429, detail=f"Bugünkü Claude hakkın doldu (günde {GUNLUK_CLAUDE_SINIRI}); yarın yeniden dene")
    kendi = servisler.kendi_sirket_adlari(kendi_alanlar(a, kullanici), alan_sozlugu(a))
    servisler.kullanimi_sifirla()
    try:
        ham = servisler.claude_ozet(
            govde.tur, govde.bicim, servisler.ozet_basligi(govde.tur, govde.bicim, bas), girdi,
            servisler.istatistik_metni(ist), anahtar, kendi_sirket=kendi, eslemeler=eslemeler)
    except servisler.ClaudeHatasi as e:
        raise HTTPException(status_code=502, detail=f"Claude: {e}; kayıtlı özet değişmedi") from e
    finally:
        claude_kullanimini_yaz(db, hak)
    yapi = servisler.ozet_yapisini_dogrula(govde.bicim, ham, bilinen, eslemeler, kendi)
    yapi, metin = ozet_yapisi(govde.tur, govde.bicim, bas, yapi, ist)
    rapor = rapor_yaz(db, kullanici.id, bas, govde.tur, metin, bicim=govde.bicim, istatistik=ist, yapi=yapi)
    return {"metin": rapor_json(rapor, eslemeler)["metin"], "ozet": rapor_json(rapor, eslemeler), "istatistik": ist,
            "girdi": kaynak, "ozetsiz_aylar": ay_adlari(bas, ozetsiz)}


def yapi_idleri(yapi: dict | None) -> set[int]:
    """Kayıtlı yapıdaki madde id'leri: Performans Özeti'nde temalardan, Yönetici Özeti'nde bölümlerden."""
    kaplar = [t for a in (yapi or {}).get("alanlar") or [] for t in a.get("temalar") or []] + list((yapi or {}).get("bolumler") or [])
    return {i for k in kaplar if isinstance(k, dict) for i in k.get("madde_idleri") or []
            if isinstance(i, int) and not isinstance(i, bool)}


@router.put("/ozet")
def ozet_kaydet(govde: OzetKaydet, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Arayüzde düzenlenen özet; Claude çağrılmaz. yapi verilirse alan bazlı düzenlemedir: yapı yeniden doğrulanır
    (madde id'leri yalnız kayıtlı yapıdakilerden, sayılar sunucudan) ve düz metin yapıdan üretilir. Yalnız metin
    verilirse eski (düz metin) düzenlemedir; kayıttaki yapı silinir. Satır yoksa güncel istatistikle açılır."""
    bas, bit = ozet_donemi(govde.tur, govde.donem)
    a = ayar_satiri(db, kullanici)
    eslemeler = ad_eslemeleri(a)
    eski = ozet_kaydi(db, kullanici, govde.tur, bas, govde.bicim)
    ist = eski.istatistik if eski is not None and eski.istatistik else donem_istatistigi(db, kullanici, bas, bit)
    if govde.yapi is not None:
        bilinen = {i: ([i], "") for i in yapi_idleri(eski.yapi if eski is not None else None)}
        kendi = servisler.kendi_sirket_adlari(kendi_alanlar(a, kullanici), alan_sozlugu(a))
        yapi = servisler.ozet_yapisini_dogrula(govde.bicim, govde.yapi, bilinen, eslemeler, kendi, duzenleme=True)
        yapi, metin = ozet_yapisi(govde.tur, govde.bicim, bas, yapi, ist)
        rapor = rapor_yaz(db, kullanici.id, bas, govde.tur, metin, bicim=govde.bicim, istatistik=ist, yapi=yapi)
        return rapor_json(rapor, eslemeler)
    metin = (govde.metin or "").strip()
    if not metin:
        raise HTTPException(status_code=422, detail="Özet metni boş olamaz")
    rapor = rapor_yaz(db, kullanici.id, bas, govde.tur, metin, bicim=govde.bicim,
                      istatistik=None if eski is not None else ist)
    if rapor.yapi is not None:
        rapor.yapi = None
        db.commit()
    return rapor_json(rapor, eslemeler)


TR_ASCII = str.maketrans("çğıöşüÇĞİÖŞÜâîûÂÎÛ", "cgiosuCGIOSUaiuAIU")


def dosya_parcasi(metin: str) -> str:
    """'Ufuk Çetinkaya' → 'Ufuk-Cetinkaya' (ASCII, tireli)."""
    metin = unicodedata.normalize("NFKD", (metin or "").translate(TR_ASCII)).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", metin).strip("-")


def ozet_dosya_adi(tur: str, bicim: str, bas: date, ad: str) -> str:
    """'Performans-Ozeti-Eylul-2026-Ufuk-Cetinkaya.pdf', 'Yonetici-Ozeti-Eylul-2026-…', 'Yonetici-Ozeti-2026-…'."""
    tip = servisler.BICIM_ADLARI[bicim]
    parcalar = [tip, servisler.donem_adi(tur, bas), ad]
    return "-".join(p for p in (dosya_parcasi(x) for x in parcalar) if p) + ".pdf"


def renk_gruplarini_isle(db: Session, kullanici: Kullanici, yapi: dict, eslemeler: list[dict]) -> None:
    """PDF için (kayda yazılmaz): Performans Özeti alanlarına baskın kaynağın renk grubu ("grup"), Yönetici Özeti
    bölümlerine ikon türü ("tur"). Kaynaklar madde id'lerinden okunur; ekrandaki donut'la aynı eşleme."""
    idler = yapi_idleri(yapi)
    kaynak = dict(db.execute(select(Madde.id, Madde.kaynak).where(Madde.user_id == kullanici.id, Madde.id.in_(idler))).all()) if idler else {}
    for a in yapi.get("alanlar") or []:
        ids = [i for t in a.get("temalar") or [] for i in t.get("madde_idleri") or []]
        a["grup"] = servisler.baskin_grup([kaynak.get(i) for i in ids if i in kaynak])
    uygulama = next((k for k in kategori_baglami(db, kullanici)[1]
                     if not k.sistem and {"github", "medusa"} & set(k.kaynaklar or [])), None)
    uygulama_adi = servisler.ad_esle(uygulama.ad, eslemeler) if uygulama else ""
    for b in yapi.get("bolumler") or []:
        b["tur"] = servisler.yonetici_bolum_turu(
            b.get("ad") or "", [kaynak.get(i) for i in b.get("madde_idleri") or [] if i in kaynak], uygulama_adi)


@router.get("/ozet/pdf")
def ozet_pdf(tur: str = "aylik", donem: str = "", bicim: str = "basari", kullanici: Kullanici = Depends(aktif_kullanici),
             db: Session = Depends(oturum)) -> Response:
    """Kayıtlı özetin sunucuda üretilen PDF'i (ReportLab, IBM Plex Sans). Performans Özeti: kapak + ayrıntı sayfaları;
    Yönetici Özeti: tek sayfa (taşarsa ikinci). Özet yoksa 404."""
    import pdf_uret  # ReportLab yalnız PDF istenince yüklenir

    if bicim not in OZET_BICIMLERI:
        raise HTTPException(status_code=422, detail="bicim patron ya da basari olmalı")
    bas, bit = ozet_donemi(tur, donem)
    kayit = ozet_kaydi(db, kullanici, tur, bas, bicim)
    if kayit is None:
        raise HTTPException(status_code=404, detail="Bu dönem için kayıtlı özet yok; önce özeti üret")
    eslemeler = ad_eslemeleri(ayar_satiri(db, kullanici))
    ist = kayit.istatistik or donem_istatistigi(db, kullanici, bas, bit)
    yapi = servisler.yapi_esle(kayit.yapi, eslemeler) if isinstance(kayit.yapi, dict) else None
    if yapi is not None and "sayilar" not in yapi:
        yapi["sayilar"] = ozet_sayilari(ist, yapi)
    if yapi is not None:
        renk_gruplarini_isle(db, kullanici, yapi, eslemeler)
    icerik = pdf_uret.ozet_pdf(
        tur=tur, bicim=bicim, donem_adi=servisler.donem_adi(tur, bas), yapi=yapi,
        metin=servisler.ad_esle(kayit.metin, eslemeler), istatistik=ist,
        ad=servisler.ad_esle(kullanici.ad, eslemeler), unvan=servisler.ad_esle(kullanici.unvan or "", eslemeler),
        hazirlanma=utc(kayit.olusturma).astimezone(servisler.ISTANBUL).date() if kayit.olusturma else bugun(),
    )
    ad = ozet_dosya_adi(tur, bicim, bas, kullanici.ad)
    utf8 = quote(ad)
    return Response(icerik, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=\"{ad}\"; filename*=UTF-8''{utf8}", "Cache-Control": "no-store"})


# ---------------------------------------------------------------- günün rapor düzeni (sıra + kategori)

class DuzenSatiri(BaseModel):
    item_id: int
    kategori_id: int | None = None


class DuzenYaz(BaseModel):
    tarih: date | None = None
    sirali: list[DuzenSatiri]


class DuzenKaristir(BaseModel):
    tarih: date | None = None


def duzen_yanit(db: Session, kullanici: Kullanici, tarih: date) -> dict:
    duzen = GunDuzeni(db, kullanici, tarih)
    return {"tarih": tarih.isoformat(), **duzen.json(), "etkin": {str(k): v for k, v in duzen.etkin.items()},
            "rapor_metni": gunun_rapor_metni(db, kullanici, tarih, duzen)}


def duzeni_yaz(db: Session, kullanici: Kullanici, tarih: date, satirlar: list[tuple[int, int | None]]) -> None:
    """Günün düzenini baştan yazar: (madde, elle kategori) sırayla; kategori None ise kural belirler."""
    db.execute(delete(RaporDuzeni).where(RaporDuzeni.user_id == kullanici.id, RaporDuzeni.tarih == tarih))
    for sira, (item_id, kategori_id) in enumerate(satirlar, start=1):
        db.add(RaporDuzeni(user_id=kullanici.id, tarih=tarih, item_id=item_id, kategori_id=kategori_id, sira=sira))
    db.commit()


@router.get("/rapor-duzeni")
def rapor_duzeni_getir(tarih: str | None = None, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    return duzen_yanit(db, kullanici, gun_sec(tarih))


@router.post("/rapor-duzeni")
def rapor_duzeni_yaz(govde: DuzenYaz, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Düzenle modunda sürükle-bırak sonrası günün tam sırası. Kategori, maddenin kuralla bulunan kategorisinden
    farklıysa saklanır; aynıysa saklanmaz ki sonradan yıldız ya da seçici kuralı işlesin."""
    tarih = gun_sec(govde.tarih)
    idler = [s.item_id for s in govde.sirali]
    if len(set(idler)) != len(idler):
        raise HTTPException(status_code=422, detail="Bir madde sırada iki kez geçemez")
    maddeler = {m.id: m for m in db.scalars(select(Madde).where(Madde.user_id == kullanici.id, Madde.id.in_(idler)))}
    if len(maddeler) != len(idler):
        raise HTTPException(status_code=404, detail="Madde bulunamadı")
    baglam = KategoriBaglami(kategorileri_hazirla(db, kullanici))
    with _kullanici_kilidi(kullanici.id, "duzen"):
        satirlar = []
        for s in govde.sirali:
            if s.kategori_id is not None and s.kategori_id not in baglam.idler:
                raise HTTPException(status_code=404, detail="Kategori bulunamadı")
            elle = s.kategori_id if s.kategori_id is not None and s.kategori_id != baglam.dogal(maddeler[s.item_id]) else None
            satirlar.append((s.item_id, elle))
        duzeni_yaz(db, kullanici, tarih, satirlar)
    return duzen_yanit(db, kullanici, tarih)


@router.post("/rapor-duzeni/karistir")
def rapor_duzeni_karistir(
    govde: DuzenKaristir | None = None, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    """Her kategorinin içini yeniden rastgele sıralar; maddeler kategorilerinde kalır."""
    tarih = gun_sec(govde.tarih if govde else None)
    with _kullanici_kilidi(kullanici.id, "duzen"):
        duzen = GunDuzeni(db, kullanici, tarih)
        satirlar = []
        for _, liste in duzen.bolumler:
            liste = list(liste)
            random.shuffle(liste)
            satirlar += [(m.id, duzen.satirlar[m.id].kategori_id if m.id in duzen.satirlar else None) for m in liste]
        duzeni_yaz(db, kullanici, tarih, satirlar)
    return duzen_yanit(db, kullanici, tarih)


@router.delete("/rapor-duzeni")
def rapor_duzeni_sifirla(tarih: str | None = None, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    tarih = gun_sec(tarih)
    db.execute(delete(RaporDuzeni).where(RaporDuzeni.user_id == kullanici.id, RaporDuzeni.tarih == tarih))
    db.commit()
    return duzen_yanit(db, kullanici, tarih)


# ---------------------------------------------------------------- rapor kategorileri

class KategoriYeni(BaseModel):
    ad: str
    kaynaklar: list[str] = []


class KategoriGuncelle(BaseModel):
    ad: str | None = None
    kaynaklar: list[str] | None = None
    sira: int | None = None


class KategoriSirasi(BaseModel):
    idler: list[int]


def kategori_adi(ad: str) -> str:
    ad = re.sub(r"\s+", " ", ad or "").strip()
    if not ad:
        raise HTTPException(status_code=422, detail="Kategori adı boş olamaz")
    if len(ad) > 80:
        raise HTTPException(status_code=422, detail="Kategori adı en fazla 80 karakter olabilir")
    return ad


def kategori_kaynaklari(kaynaklar: list[str]) -> list[str]:
    bilinmeyen = set(kaynaklar) - set(KATEGORI_KAYNAKLARI)
    if bilinmeyen:
        raise HTTPException(status_code=422, detail=f"Bilinmeyen kaynak: {', '.join(sorted(bilinmeyen))}")
    return list(dict.fromkeys(kaynaklar))


def kategori_listesi(db: Session, kullanici: Kullanici) -> dict:
    return {"kategoriler": [k.sozluk() for k in kategorileri_hazirla(db, kullanici)]}


@router.get("/kategoriler")
def kategorileri_getir(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    return kategori_listesi(db, kullanici)


@router.post("/kategoriler", status_code=201)
def kategori_ekle(govde: KategoriYeni, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    kategorileri_hazirla(db, kullanici)
    sira = (db.scalar(select(func.max(Kategori.sira)).where(Kategori.user_id == kullanici.id)) or 0) + 1
    kategori = Kategori(user_id=kullanici.id, ad=kategori_adi(govde.ad), kaynaklar=kategori_kaynaklari(govde.kaynaklar), sira=sira)
    db.add(kategori)
    db.commit()
    return kategori.sozluk()


@router.patch("/kategoriler/{kategori_id}")
def kategori_guncelle(
    kategori_id: int, govde: KategoriGuncelle, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    """Sistem kategorisinin yalnız adı ve sırası değişir."""
    kategori = kullanici_kategorisi(db, kullanici, kategori_id)
    if govde.ad is not None:
        kategori.ad = kategori_adi(govde.ad)
    if govde.kaynaklar is not None:
        kaynaklar = kategori_kaynaklari(govde.kaynaklar)
        if kategori.sistem and kaynaklar:
            raise HTTPException(status_code=422, detail="Sistem kategorisine kaynak bağlanamaz")
        kategori.kaynaklar = kaynaklar
    if govde.sira is not None:
        kategori.sira = govde.sira
    db.commit()
    return kategori.sozluk()


@router.delete("/kategoriler/{kategori_id}")
def kategori_sil(kategori_id: int, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Maddeler kategorisiz kalır (kurala göre Genel İşler'e düşer); sistem kategorisi silinemez."""
    kategori = kullanici_kategorisi(db, kullanici, kategori_id)
    if kategori.sistem:
        raise HTTPException(status_code=400, detail="Sistem kategorisi silinemez")
    # sqlite ON DELETE SET NULL'u zorlamadığı için elle
    db.execute(Madde.__table__.update().where(Madde.user_id == kullanici.id, Madde.kategori_id == kategori.id).values(kategori_id=None))
    db.execute(RaporDuzeni.__table__.update().where(
        RaporDuzeni.user_id == kullanici.id, RaporDuzeni.kategori_id == kategori.id).values(kategori_id=None))
    db.delete(kategori)
    db.commit()
    return kategori_listesi(db, kullanici)


@router.post("/kategoriler/sira")
def kategori_sirala(govde: KategoriSirasi, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Verilen sıra başa, listede olmayanlar mevcut sıralarıyla sona."""
    kategoriler = kategorileri_hazirla(db, kullanici)
    bizim = {k.id: k for k in kategoriler}
    if len(set(govde.idler)) != len(govde.idler) or any(i not in bizim for i in govde.idler):
        raise HTTPException(status_code=404, detail="Kategori bulunamadı")
    sirali = [bizim[i] for i in govde.idler] + [k for k in kategoriler if k.id not in set(govde.idler)]
    for sira, k in enumerate(sirali, start=1):
        k.sira = sira
    db.commit()
    return kategori_listesi(db, kullanici)


# ---------------------------------------------------------------- ayarlar

class AyarGuncelle(BaseModel):
    gmail_kullanici: str | None = None
    gmail_sifre: str | None = None
    github_token: str | None = None
    github_repo: str | None = None
    proje_adi: str | None = None
    patron_telefon: str | None = None
    rapor_basligi: str | None = None
    alan_sozlugu: str | dict[str, str] | None = None
    hatirlatma_saat: str | None = None
    hatirlatma_gunler: list[int] | str | None = None
    hatirlatma_push: bool | None = None
    hatirlatma_eposta: bool | None = None
    hatirlatma_eposta_adres: str | None = None
    kaynaklar: dict[str, bool] | None = None
    eposta_gruplama: Literal["konu", "alici"] | None = None
    rapor_bicimi: Literal["kategorili", "duz"] | None = None
    karistir: bool | None = None
    kendi_alanlar: str | list[str] | None = None
    ekip_ici_atla: bool | None = None
    otomatik_gonder: bool | None = None
    otomatik_saat: str | None = None
    patron_eposta: str | None = None
    patron_adi: str | None = None
    otomatik_kopya_bana: bool | None = None
    ad_eslemeleri: str | list[dict] | None = None


def ad_eslemelerini_ayristir(deger: str | list) -> list[dict]:
    """Satır satır "Medusa Right = Edisyon uygulaması" ya da [{kaynak, hedef}]. Kaynak 2–60 karakter ve tek; hedef boş
    olabilir (ad raporda silinir). Hedef hiçbir kaynağı içeremez: eşleme ikinci kez uygulanınca metin değişmesin."""
    if isinstance(deger, str):
        ciftler = []
        for satir in deger.splitlines():
            if not satir.strip():
                continue
            kaynak, esit, hedef = satir.partition("=")
            if not esit:
                raise HTTPException(status_code=422, detail=f"Ad eşlemesinde satır anlaşılamadı: {satir.strip()!r} (biçim: Eski ad = Yeni ad)")
            ciftler.append((kaynak, hedef))
    else:
        ciftler = [(x.get("kaynak"), x.get("hedef")) if isinstance(x, dict) else (None, None) for x in deger]
    sonuc, gorulen = [], set()
    for kaynak, hedef in ciftler:
        if not isinstance(kaynak, str) or not isinstance(hedef, (str, type(None))):
            raise HTTPException(status_code=422, detail="Ad eşlemesi {kaynak, hedef} biçiminde olmalı")
        kaynak, hedef = re.sub(r"\s+", " ", kaynak).strip(), re.sub(r"\s+", " ", hedef or "").strip()
        if not ESLEME_KAYNAK_EN_KISA <= len(kaynak) <= ESLEME_KAYNAK_EN_UZUN:
            raise HTTPException(status_code=422, detail=f"Ad eşlemesinde soldaki ad {ESLEME_KAYNAK_EN_KISA}–{ESLEME_KAYNAK_EN_UZUN} karakter olmalı: {kaynak!r}")
        if len(hedef) > ESLEME_HEDEF_EN_UZUN:
            raise HTTPException(status_code=422, detail=f"Ad eşlemesinde sağdaki ad en fazla {ESLEME_HEDEF_EN_UZUN} karakter olabilir")
        anahtar = servisler._kucult(kaynak)
        if anahtar in gorulen:
            raise HTTPException(status_code=422, detail=f"Ad eşlemesinde {kaynak!r} iki kez yazılmış")
        gorulen.add(anahtar)
        sonuc.append({"kaynak": kaynak, "hedef": hedef})
    if len(sonuc) > ESLEME_SINIRI:
        raise HTTPException(status_code=422, detail=f"En fazla {ESLEME_SINIRI} ad eşlemesi olabilir")
    for e in sonuc:
        if servisler.esleme_iceriyor(e["hedef"], sonuc):
            raise HTTPException(status_code=422, detail=f"Ad eşlemesinde sağdaki ad soldaki bir adı içeremez: {e['hedef']!r}")
    return sonuc


def kendi_alanlari_ayristir(deger: str | list[str]) -> list[str]:
    """Virgül, boşluk ya da satırla ayrılmış alan adları; '@' öneki ve büyük harf yok sayılır."""
    parcalar = re.split(r"[\s,;]+", deger) if isinstance(deger, str) else deger
    sonuc = []
    for p in parcalar:
        alan = (p or "").strip().lstrip("@").lower().rstrip(".")
        if not alan:
            continue
        if not ALAN_BICIMI.match(alan):
            raise HTTPException(status_code=422, detail=f"Alan adı anlaşılamadı: {alan!r} (ör. ilsvision.com)")
        if alan not in sonuc:
            sonuc.append(alan)
    return sonuc


def sozlugu_ayristir(deger: str | dict[str, str]) -> dict[str, str]:
    if isinstance(deger, dict):
        satirlar = [f"{k}={v}" for k, v in deger.items()]
    else:
        satirlar = deger.splitlines()
    sonuc = {}
    for satir in satirlar:
        if not satir.strip():
            continue
        alan, esit, kurum = satir.partition("=")
        if not esit or not alan.strip() or not kurum.strip():
            raise HTTPException(status_code=422, detail=f"Alan adı sözlüğünde satır anlaşılamadı: {satir.strip()!r} (biçim: alan=Kurum)")
        sonuc[alan.strip().lower()] = kurum.strip()
    return sonuc


@router.get("/ayarlar")
def ayarlari_getir(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    return ayar_ozeti(ayar_satiri(db, kullanici), kullanici)


@router.put("/ayarlar")
def ayarlari_kaydet(govde: AyarGuncelle, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    a = ayar_satiri(db, kullanici)
    db.add(a)
    veri = govde.model_dump(exclude_unset=True)
    secim = veri.pop("kaynaklar", None)
    if secim is not None:
        bilinmeyen = set(secim) - set(KAYNAKLAR)
        if bilinmeyen:
            raise HTTPException(status_code=422, detail=f"Bilinmeyen kaynak: {', '.join(sorted(bilinmeyen))}")
        # yeni sözlük atanır (JSON kolonunda yerinde değişiklik izlenmez); gelmeyen kaynak olduğu gibi kalır
        a.kaynaklar = {**kaynak_durumu(a), **{k: bool(v) and k not in YAKINDA for k, v in secim.items()}}
    for alan in ("gmail_sifre", "github_token"):
        deger = (veri.pop(alan, None) or "").strip()
        if deger:  # boş bırakılan yazma-yalnız alan kaydı değiştirmez
            setattr(a, alan + "_enc", guvenlik.sifrele(deger))
    if "alan_sozlugu" in veri:
        deger = veri.pop("alan_sozlugu")
        a.alan_sozlugu = None if deger is None else sozlugu_ayristir(deger)
    for alan, ad in (("hatirlatma_saat", "Hatırlatma saati"), ("otomatik_saat", "Otomatik gönderim saati")):
        saat = veri.pop(alan, None)
        if saat is not None:
            eslesme = SAAT_BICIMI.match(saat.strip())
            if not eslesme:
                raise HTTPException(status_code=422, detail=f"{ad} SS:DD biçiminde olmalı")
            setattr(a, alan, time(int(eslesme.group(1)), int(eslesme.group(2))))
    gunler = veri.pop("hatirlatma_gunler", None)
    if gunler is not None:
        a.hatirlatma_gunler = ",".join(map(str, gunleri_ayristir(gunler)))
    gruplama = veri.pop("eposta_gruplama", None)
    if gruplama is not None:
        a.eposta_gruplama = gruplama
    if "kendi_alanlar" in veri:
        deger = veri.pop("kendi_alanlar")
        a.kendi_alanlar = kendi_alanlari_ayristir(deger) if deger is not None else None
    for alan in ("hatirlatma_push", "hatirlatma_eposta", "rapor_bicimi", "karistir", "ekip_ici_atla",
                 "otomatik_gonder", "otomatik_kopya_bana"):
        deger = veri.pop(alan, None)
        if deger is not None:
            setattr(a, alan, deger)
    if "hatirlatma_eposta_adres" in veri:
        adres = (veri.pop("hatirlatma_eposta_adres") or "").strip().lower() or None
        if adres and not EPOSTA_BICIMI.match(adres):
            raise HTTPException(status_code=422, detail="Hatırlatma e-posta adresi geçerli değil")
        a.hatirlatma_eposta_adres = adres
    if "patron_eposta" in veri:
        adres = (veri.pop("patron_eposta") or "").strip().lower() or None
        if adres and not EPOSTA_BICIMI.match(adres):
            raise HTTPException(status_code=422, detail="Patron e-posta adresi geçerli değil")
        a.patron_eposta = adres
    if "patron_adi" in veri:
        a.patron_adi = re.sub(r"\s+", " ", veri.pop("patron_adi") or "").strip()[:120] or None
    if "ad_eslemeleri" in veri:
        deger = veri.pop("ad_eslemeleri")
        a.ad_eslemeleri = ad_eslemelerini_ayristir(deger) if deger is not None else None
    for alan, deger in veri.items():
        deger = (deger or "").strip() or None
        if alan == "github_repo" and deger and not REPO_BICIMI.match(deger):
            raise HTTPException(status_code=422, detail="GitHub repo 'kullanici/repo' biçiminde olmalı")
        if alan == "gmail_kullanici" and deger:
            deger = deger.lower()
        setattr(a, alan, deger)
    if a.otomatik_gonder and not a.patron_eposta:  # commit yok; istek bitince oturum geri alınır
        raise HTTPException(status_code=422, detail="Patron e-postası gerekli")
    db.commit()
    return ayar_ozeti(a, kullanici)


@router.post("/ayarlar/test")
def baglantiyi_test_et(
    kaynak: Literal["", "gmail", "github"] = "", kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    """kaynak verilirse yalnız o kaynak denenir (kurulum sihirbazı Gmail adımı). Gmail Google bağlantısıyla
    okunuyorsa Google token'ı yenilenerek denenir."""
    a = ayar_satiri(db, kullanici)
    ayarlar = cozulmus_ayarlar(a)
    parcalar, gmail_ok, github_ok = [], False, False

    if kaynak != "github":
        if "gmail" in google_kapsamlari(a) and a.google_durum != "yenile":
            try:
                google_erisim_tokeni(db, a)
                parcalar.append(f"Gmail: Google hesabıyla bağlı ({a.google_eposta})")
                gmail_ok = True
            except servisler.GoogleHatasi as e:
                parcalar.append(f"Gmail: {e}")
        elif ayarlar["gmail_kullanici"] and ayarlar["gmail_sifre"]:
            try:
                parcalar.append(servisler.gmail_test(ayarlar["gmail_kullanici"], ayarlar["gmail_sifre"]))
                gmail_ok = True
            except servisler.KaynakHatasi as e:
                parcalar.append(f"Gmail: {e}")
            except Exception as e:
                parcalar.append(f"Gmail: beklenmeyen hata ({e.__class__.__name__})")
        else:
            parcalar.append("Gmail: ayar girilmemiş")

    if kaynak != "gmail":
        if ayarlar["github_token"] and ayarlar["github_repo"]:
            try:
                parcalar.append(servisler.github_test(ayarlar["github_token"], ayarlar["github_repo"]))
                github_ok = True
            except servisler.KaynakHatasi as e:
                parcalar.append(f"GitHub: {e}")
            except Exception as e:
                parcalar.append(f"GitHub: beklenmeyen hata ({e.__class__.__name__})")
        else:
            parcalar.append("GitHub: ayar girilmemiş")

    return {"sonuc": " · ".join(parcalar), "gmail_ok": gmail_ok, "github_ok": github_ok}


# ---------------------------------------------------------------- ilk kurulum sihirbazı

class KurulumProfil(BaseModel):
    ad: str | None = None
    rapor_basligi: str | None = None
    patron_telefon: str | None = None


class ProfilGuncelle(BaseModel):
    ad: str | None = None
    unvan: str | None = None  # A1v2: PDF kapağında "Ad · Unvan"; boş → kaldırılır


UNVAN_EN_UZUN = 80


def unvan_temizle(unvan: str | None) -> str | None:
    unvan = re.sub(r"\s+", " ", "".join(h if h.isprintable() else " " for h in (unvan or ""))).strip()
    if len(unvan) > UNVAN_EN_UZUN:
        raise HTTPException(status_code=422, detail=f"Unvan en çok {UNVAN_EN_UZUN} karakter olabilir")
    return unvan or None


def gorunen_ad(ad: str | None) -> str:
    """Raporlarda ve e-postalarda görünen ad (kurulum sihirbazı ve Ayarlar aynı alanı yazar): boşluklar sadeleşir,
    denetim karakterleri atılır; 2–60 karakter."""
    ad = re.sub(r"\s+", " ", "".join(h if h.isprintable() else " " for h in (ad or ""))).strip()
    if not ad:
        raise HTTPException(status_code=422, detail="Ad boş olamaz")
    if not AD_EN_KISA <= len(ad) <= AD_EN_UZUN:
        raise HTTPException(status_code=422, detail=f"Ad {AD_EN_KISA}–{AD_EN_UZUN} karakter olmalı")
    return ad


@router.patch("/profil")
def profil_guncelle(govde: ProfilGuncelle, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Görünen ad ve/veya unvan; gövdede verilmeyen alan değişmez."""
    alanlar = govde.model_fields_set
    if not alanlar & {"ad", "unvan"}:
        raise HTTPException(status_code=422, detail="Ad ya da unvan verilmeli")
    if "ad" in alanlar:
        kullanici.ad = gorunen_ad(govde.ad)
    if "unvan" in alanlar:
        kullanici.unvan = unvan_temizle(govde.unvan)
    db.commit()
    return {"ad": kullanici.ad, "ad_yer_tutucu": ad_yer_tutucu(kullanici.ad), "unvan": kullanici.unvan or ""}


class KurulumSurekli(BaseModel):
    metinler: list[str]


def _metin_anahtari(metin: str) -> str:
    return servisler._kucult(re.sub(r"\s+", " ", metin).strip())


def surekli_metinleri(db: Session, kullanici: Kullanici) -> list[str]:
    return list(db.scalars(select(Madde.metin).where(
        Madde.user_id == kullanici.id, Madde.tur == "surekli",
    ).order_by(Madde.sira, Madde.id)))


@router.get("/kurulum")
def kurulum_bilgisi(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    return {
        "ad": kullanici.ad, "eposta": kullanici.eposta,
        "ayarlar": ayar_ozeti(ayar_satiri(db, kullanici), kullanici),
        "sablonlar": servisler.SUREKLI_SABLONLAR,
        "surekli": surekli_metinleri(db, kullanici),
    }


@router.post("/kurulum/profil")
def kurulum_profili(govde: KurulumProfil, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    veri = govde.model_dump(exclude_unset=True)
    if "ad" in veri:
        kullanici.ad = gorunen_ad(veri["ad"])
    a = ayar_satiri(db, kullanici)
    db.add(a)
    for alan in ("rapor_basligi", "patron_telefon"):
        if alan in veri:
            setattr(a, alan, (veri[alan] or "").strip() or None)
    db.commit()
    return {"ad": kullanici.ad, **ayar_ozeti(a, kullanici)}


@router.post("/kurulum/surekli")
def kurulum_surekli(govde: KurulumSurekli, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Seçilen satırlar listenin sonuna eklenir; mevcut sürekli işler korunur, aynı metin ikinci kez eklenmez."""
    with _kullanici_kilidi(kullanici.id, "kurulum"):
        gorulen = {_metin_anahtari(m) for m in surekli_metinleri(db, kullanici)}
        sira = sonraki_sira(db, kullanici, "surekli")
        eklenen = 0
        for metin in govde.metinler:
            metin = re.sub(r"\s+", " ", metin or "").strip()
            if not metin or _metin_anahtari(metin) in gorulen:
                continue
            db.add(Madde(user_id=kullanici.id, tur="surekli", metin=metin, tikli=True, sira=sira))
            gorulen.add(_metin_anahtari(metin))
            sira += 1
            eklenen += 1
        db.commit()
    return {"eklenen": eklenen, "surekli": surekli_metinleri(db, kullanici)}


@router.post("/kurulum/bitir")
def kurulum_bitir(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    a = ayar_satiri(db, kullanici)
    db.add(a)
    a.kurulum_tamam = True
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- eski localStorage verisini içe aktarma

def _metin(deger) -> str:
    return deger.strip() if isinstance(deger, str) else ""


@router.post("/ice-aktar")
def ice_aktar(
    govde: dict = Body(...), kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)
) -> dict:
    """gunluk-rapor-v2 biçimi: recurring[{text,on}], ongoing[{text,stage,on}], daily{date,done,plan}, settings{phone,title}."""
    # Yalnız kalıcı maddeler engeller; sayfa açılışında oluşan bulunan/bugün satırları taşımayı durdurmaz.
    if db.scalar(select(func.count()).select_from(Madde).where(
        Madde.user_id == kullanici.id, Madde.tur.in_(("surekli", "devam")),
    )):
        raise HTTPException(status_code=409, detail="Bu hesapta zaten sürekli/devam eden iş var; içe aktarma yapılmadı")

    sayim = {"surekli": 0, "devam": 0, "bugun": 0}
    for tur, liste in (("surekli", govde.get("recurring")), ("devam", govde.get("ongoing"))):
        for x in liste if isinstance(liste, list) else []:
            if not isinstance(x, dict) or not _metin(x.get("text")):
                continue
            sayim[tur] += 1
            db.add(Madde(
                user_id=kullanici.id, tur=tur, metin=_metin(x["text"]),
                asama=(_metin(x.get("stage")) or None) if tur == "devam" else None,
                tikli=x.get("on") is not False, sira=sayim[tur],
            ))

    tarih = bugun()
    gunluk = govde.get("daily")
    if isinstance(gunluk, dict) and gunluk.get("date") == tarih.isoformat():
        mevcut = set(db.scalars(select(Madde.kaynak).where(
            Madde.user_id == kullanici.id, Madde.tur == "bugun", Madde.tarih == tarih,
        )))
        # mevcut satırlara dokunulmaz
        if isinstance(gunluk.get("done"), str) and not mevcut & {"elle", "yapilanlar"}:
            for sira, satir in enumerate(satirlara_bol(gunluk["done"]), start=1):
                sayim["bugun"] += 1
                db.add(Madde(user_id=kullanici.id, tur="bugun", tarih=tarih, kaynak="elle", metin=satir, sira=sira))
        plan = gunluk.get("plan")
        if isinstance(plan, str) and plan.strip() and "yarin" not in mevcut:
            sayim["bugun"] += 1
            db.add(Madde(user_id=kullanici.id, tur="bugun", tarih=tarih, kaynak="yarin", kaynak_id="yarin", metin=plan))

    ayar = govde.get("settings")
    if isinstance(ayar, dict):
        a = ayar_satiri(db, kullanici)
        db.add(a)
        if _metin(ayar.get("phone")):
            a.patron_telefon = _metin(ayar["phone"])
        if _metin(ayar.get("title")):
            a.rapor_basligi = _metin(ayar["title"])

    db.commit()
    return {"ok": True, **sayim}


# ---------------------------------------------------------------- web push abonelikleri

def app_url() -> str:
    return (os.environ.get("APP_URL") or "").strip() or "https://gunluk-rapor.onrender.com"


def cihaz_adi_bul(ua: str) -> str:
    """User-Agent'tan kısa ad: 'iPhone Safari', 'Mac Chrome'."""
    ua = ua or ""
    cihaz = next((ad for anahtar, ad in (
        ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
        ("Windows", "Windows"), ("Linux", "Linux"),
    ) if anahtar in ua), "Cihaz")
    if "Edg" in ua:
        tarayici = "Edge"
    elif "Firefox/" in ua or "FxiOS/" in ua:
        tarayici = "Firefox"
    elif "Chrome/" in ua or "CriOS/" in ua:
        tarayici = "Chrome"
    elif "Safari/" in ua or cihaz in ("iPhone", "iPad", "Mac"):  # ana ekran uygulamasının UA'sında 'Safari' yok
        tarayici = "Safari"
    else:
        tarayici = "Tarayıcı"
    return f"{cihaz} {tarayici}"


def abonelik_json(a: PushAbonelik) -> dict:
    return {
        "id": a.id, "cihaz_adi": a.cihaz_adi, "olusturma": zaman_iso(a.olusturma),
        "son_basari": zaman_iso(a.son_basari), "son_hata": a.son_hata,
    }


class PushAnahtarlari(BaseModel):
    p256dh: str
    auth: str


class PushAbonelikBilgisi(BaseModel):
    endpoint: str
    keys: PushAnahtarlari


class PushAboneIstek(BaseModel):
    subscription: PushAbonelikBilgisi
    cihaz_adi: str | None = None


def push_cihazlarina_gonder(db: Session, user_id: int, veri: dict) -> tuple[int, int, list[dict]]:
    """404/410 → abonelik silinir; diğer hata son_hata'ya yazılır. (başarılı, toplam, cihaz başına ayrıntı)."""
    abonelikler = db.scalars(select(PushAbonelik).where(PushAbonelik.user_id == user_id).order_by(PushAbonelik.id)).all()
    basarili, ayrinti = 0, []
    for a in abonelikler:
        kod, mesaj = servisler.push_gonder({"endpoint": a.endpoint, "keys": {"p256dh": a.p256dh, "auth": a.auth}}, veri)
        satir = {"id": a.id, "cihaz_adi": a.cihaz_adi}
        if kod is None:
            basarili += 1
            a.son_basari, a.son_hata = simdi(), None
            satir.update(durum="ok", mesaj="")
        elif kod in (404, 410):
            db.delete(a)
            satir.update(durum="silindi", mesaj="Abonelik artık geçerli değil; kaldırıldı")
        else:
            a.son_hata = mesaj
            satir.update(durum="hata", mesaj=mesaj)
        ayrinti.append(satir)
        db.commit()
    return basarili, len(abonelikler), ayrinti


@router.get("/push/anahtar")
def push_anahtari(kullanici: Kullanici = Depends(aktif_kullanici)) -> dict:
    return {"anahtar": servisler.vapid_anahtarlari()[0]}


@router.get("/push/abone")
def push_abonelikleri(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    abonelikler = db.scalars(select(PushAbonelik).where(PushAbonelik.user_id == kullanici.id).order_by(PushAbonelik.id))
    return {"cihazlar": [abonelik_json(a) for a in abonelikler]}


@router.post("/push/abone")
def push_abone_ol(
    govde: PushAboneIstek, request: Request,
    kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    abonelik = govde.subscription
    if not abonelik.endpoint.startswith("https://"):
        raise HTTPException(status_code=422, detail="Geçersiz abonelik adresi")
    cihaz = ((govde.cihaz_adi or "").strip() or cihaz_adi_bul(request.headers.get("user-agent", "")))[:60]
    for deneme in range(2):
        a = db.scalar(select(PushAbonelik).where(PushAbonelik.endpoint == abonelik.endpoint))
        if a is None:
            a = PushAbonelik(endpoint=abonelik.endpoint)
            db.add(a)
        a.user_id, a.cihaz_adi, a.son_hata = kullanici.id, cihaz, None
        a.p256dh, a.auth = abonelik.keys.p256dh, abonelik.keys.auth
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            if deneme:
                raise
    return abonelik_json(a)


@router.delete("/push/abone/{abonelik_id}")
def push_abonelik_sil(abonelik_id: int, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    a = db.get(PushAbonelik, abonelik_id)
    if a is None or a.user_id != kullanici.id:
        raise HTTPException(status_code=404, detail="Abonelik bulunamadı")
    db.delete(a)
    db.commit()
    return {"ok": True}


@router.post("/push/dene")
def push_dene(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    basarili, toplam, ayrinti = push_cihazlarina_gonder(
        db, kullanici.id, {"baslik": "Günlük Rapor", "govde": "Bildirimler çalışıyor", "url": app_url()},
    )
    return {"basarili": basarili, "toplam": toplam, "cihazlar": ayrinti}


# ---------------------------------------------------------------- 17:00 hatırlatması (cron ucu)

SURE_SINIRI = 20  # sn; aşılırsa kalan kullanıcılar sonraki ping'e kalır
son_hatirlat_ping: str | None = None
_hatirlat_kilidi = threading.Lock()


def istanbul_simdi() -> datetime:
    return datetime.now(servisler.ISTANBUL)


def cron_tokeni_dogru(request: Request, token: str) -> bool:
    beklenen = (os.environ.get("CRON_TOKEN") or "").strip()
    if not beklenen:
        return False
    yetki = request.headers.get("authorization", "")
    gelen = token or (yetki[7:].strip() if yetki[:7].lower() == "bearer " else "")
    return hmac.compare_digest(gelen.encode(), beklenen.encode())


def hatirlatma_ozeti(bulunanlar: list[Madde]) -> str:
    """E-posta ve dosya sayıları sağlayıcıdan bağımsız toplamdır (Gmail + Outlook, Drive + OneDrive)."""
    turler = {"eposta": ("eposta", "outlook"), "takvim": ("takvim",), "drive": ("drive", "onedrive")}
    sayi = {k: sum(1 for m in bulunanlar if m.kaynak in kaynaklar) for k, kaynaklar in turler.items()}
    commit = len(bulunanlar) - sum(sayi.values())
    parcalar = [f"{n} {ad}" for n, ad in ((sayi["eposta"], "e-posta"), (commit, "commit"),
                                           (sayi["takvim"], "toplantı"), (sayi["drive"], "dosya")) if n]
    if not parcalar:
        return "Bugün için bulunan yok, yapılanları ekle"
    return "Bugünün raporu hazır bekliyor · " + ", ".join(parcalar) + " bulundu"


def hatirlatma_epostasi(ozet: str, bulunanlar: list[Madde], tarih: date, uyarilar: dict | list[dict] | None = None,
                        eslemeler: list[dict] | None = None) -> str:
    """uyarilar: baglanti_uyarilari sonucu (tek sözlük de olur); Bugün şeritlerindeki cümle ve bağlantı eklenir.
    Ad eşlemeleri gövdeye uygulanır."""
    satirlar = [ozet, ""]
    if bulunanlar:
        satirlar += [f"• {madde_rapor_metni(m, None, tarih)}" for m in bulunanlar] + [""]
    for u in [uyarilar] if isinstance(uyarilar, dict) else uyarilar or []:
        satirlar += [f"{u['metin']} · {u['eylem']}: {app_url()}{u['adres']}", ""]
    return servisler.ad_esle("\n".join(satirlar + [app_url(), "", "Bu hatırlatma, raporu kopyaladığın gün gelmez."]), eslemeler)


KANAL_ADI = {"push": "push", "eposta": "e-posta", "otomatik": "patrona e-posta", "otomatik_uyari": "ön uyarı"}


def son_hatirlatmalar(db: Session) -> dict[int, dict]:
    """Kullanıcı → en son gönderim günü ve o günün kanal sonuçları (yönetim ekranı için)."""
    son = dict(db.execute(select(
        HatirlatmaGonderimi.user_id, func.max(HatirlatmaGonderimi.tarih),
    ).group_by(HatirlatmaGonderimi.user_id)).all())
    if not son:
        return {}
    kosullar = [(HatirlatmaGonderimi.user_id == uid) & (HatirlatmaGonderimi.tarih == t) for uid, t in son.items()]
    ozet: dict[int, dict] = {uid: {"tarih": t, "kanallar": []} for uid, t in son.items()}
    for g in db.scalars(select(HatirlatmaGonderimi).where(or_(*kosullar)).order_by(
        HatirlatmaGonderimi.kanal, HatirlatmaGonderimi.id,
    )):
        ozet[g.user_id]["kanallar"].append({
            "ad": KANAL_ADI.get(g.kanal, g.kanal),
            "ok": g.durum == "gonderildi",
            "neden": "" if g.durum == "gonderildi" else (g.hata_metni or g.durum or "")[:60],
        })
    return ozet


def kanal_talep_et(db: Session, user_id: int, tarih: date, kanal: str, durum: str = "gonderiliyor") -> HatirlatmaGonderimi | None:
    """Gönderimden önce satırı yazar; aynı gün başka bir ping bu kanalı aldıysa None."""
    kayit = HatirlatmaGonderimi(user_id=user_id, tarih=tarih, kanal=kanal, durum=durum)
    db.add(kayit)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return kayit


def kanal_hatasi_yaz(kayit: HatirlatmaGonderimi, user_id: int, kanal: str, neden: str) -> None:
    """Gönderim satırını 'hata' olarak kapatır ve nedeni WARNING olarak loglar (adres/şifre yazılmaz)."""
    kayit.durum = "hata"
    kayit.hata_metni = neden[:200]
    log.warning("hatirlat kanal hatasi user=%s kanal=%s neden=%s", user_id, kanal, neden[:200])


def kullaniciya_hatirlat(db: Session, k: Kullanici, an: datetime) -> dict:
    """Sonucu döner ve kullanıcı başına tek satır INFO logu yazar."""
    sonuc = _kullaniciya_hatirlat(db, k, an)
    log.info("hatirlat user=%s push=%s eposta=%s neden=%s",
             sonuc["user_id"], sonuc["push"], sonuc["eposta"], sonuc["neden"] or "-")
    try:
        otomatik = otomatik_denetle(db, k, an)
    except Exception as e:  # hatırlatmanın sonucu korunur
        db.rollback()
        otomatik = f"beklenmeyen hata ({e.__class__.__name__})"
        log.warning("otomatik user=%s neden=%s", k.id, otomatik)
    if otomatik is not None:  # yalnız otomatik gönderimi açık kullanıcılarda
        sonuc["otomatik"] = otomatik
        log.info("otomatik user=%s sonuc=%s", k.id, otomatik)
    return sonuc


def _kullaniciya_hatirlat(db: Session, k: Kullanici, an: datetime) -> dict:
    tarih = an.date()
    sonuc = {"user_id": k.id, "push": "atlandı", "eposta": "atlandı", "neden": ""}
    a = ayar_satiri(db, k)
    h = hatirlatma_ayari(a)
    if tarih.isoweekday() not in h["gunler"]:
        sonuc["neden"] = "bugün hatırlatma günü değil"
        return sonuc
    if an.time() < h["saat"]:
        sonuc["neden"] = f"saat {h['saat'].strftime('%H:%M')} olmadı"
        return sonuc
    if db.scalar(select(Rapor.id).where(Rapor.user_id == k.id, Rapor.tur == "gunluk", Rapor.tarih == tarih)):
        sonuc["neden"] = "bugünün raporu kopyalanmış"
        return sonuc

    islenmis = set(db.scalars(select(HatirlatmaGonderimi.kanal).where(
        HatirlatmaGonderimi.user_id == k.id, HatirlatmaGonderimi.tarih == tarih,
    )))
    cihaz_sayisi = db.scalar(select(func.count()).select_from(PushAbonelik).where(PushAbonelik.user_id == k.id))
    ayarlar = cozulmus_ayarlar(a)
    nedenler = []
    push_gerekli = eposta_gerekli = False
    if not h["push"]:
        nedenler.append("push kapalı")
    elif "push" in islenmis:
        nedenler.append("push bugün gönderildi")
    elif not cihaz_sayisi:
        sonuc["push"] = "0/0"  # kayıt açılmaz: gün içinde cihaz eklenirse sonraki ping gönderir
    else:
        push_gerekli = True
    if not h["eposta"]:
        nedenler.append("e-posta kapalı")
    elif "eposta" in islenmis:
        nedenler.append("e-posta bugün gönderildi")
    else:  # Resend ile gönderildiği için kullanıcının Gmail ayarı olmasa da gider
        eposta_gerekli = True
    if not (push_gerekli or eposta_gerekli):
        sonuc["neden"] = "; ".join(nedenler)
        return sonuc

    try:
        bugun_taramasi(db, k, tarih)
    except Exception as e:  # tarama düşerse hatırlatma yine gider
        db.rollback()
        nedenler.append(f"tarama yapılamadı ({e.__class__.__name__})")
    bulunanlar = db.scalars(select(Madde).where(  # kapalı kaynağın satırları özete ve e-postaya girmez
        Madde.user_id == k.id, Madde.tur == "bulunan", Madde.tarih == tarih, Madde.gizli.is_(False),
        Madde.kaynak.in_(acik_bulunan_kaynaklari(a)),
    ).order_by(Madde.sira, Madde.id)).all()
    ozet = hatirlatma_ozeti(bulunanlar)
    uyarilar = baglanti_uyarilari(a, an)  # tarama 'yenile' yazmış olabilir
    push_govdesi = servisler.ad_esle(" · ".join([ozet] + [u["metin"] for u in uyarilar]), ad_eslemeleri(a))

    # Kanallar bağımsız: biri düşerse diğeri yine gider. Hata da kaydedilir; aynı gün yeniden denenmez.
    if push_gerekli:
        kayit = kanal_talep_et(db, k.id, tarih, "push")
        if kayit is None:
            nedenler.append("push bugün gönderildi")
        else:
            try:
                basarili, toplam, ayrinti = push_cihazlarina_gonder(db, k.id, {"baslik": "Günlük Rapor", "govde": push_govdesi, "url": app_url()})
                sonuc["push"] = f"{basarili}/{toplam}"
                if basarili:
                    kayit.durum = "gonderildi"
                else:
                    kanal_hatasi_yaz(kayit, k.id, "push", "; ".join(
                        c["mesaj"] for c in ayrinti if c["durum"] != "ok") or "gönderilebilen cihaz yok")
            except Exception as e:
                db.rollback()
                sonuc["push"] = "hata"
                neden = f"beklenmeyen hata ({e.__class__.__name__}: {e})"
                nedenler.append(f"push: {neden}")
                kanal_hatasi_yaz(kayit, k.id, "push", neden)
            db.commit()

    if eposta_gerekli:
        kayit = kanal_talep_et(db, k.id, tarih, "eposta")
        if kayit is None:
            nedenler.append("e-posta bugün gönderildi")
        else:
            try:
                hata = servisler.eposta_gonder(
                    h["adres"] or k.eposta,
                    f"Günlük rapor hatırlatması – {tarih.strftime('%d.%m.%Y')}",
                    hatirlatma_epostasi(ozet, bulunanlar, tarih, uyarilar, ad_eslemeleri(a)),
                    yanit_adresi=k.eposta,
                    gmail_kullanici=ayarlar["gmail_kullanici"], gmail_sifre=ayarlar["gmail_sifre"], gonderen_adi=k.ad,
                )
            except Exception as e:
                hata = f"E-posta gönderilemedi ({e.__class__.__name__})"
            sonuc["eposta"] = "hata" if hata else "gönderildi"
            if hata:
                nedenler.append(hata)
                kanal_hatasi_yaz(kayit, k.id, "eposta", hata)
            else:
                kayit.durum = "gonderildi"
            db.commit()
    sonuc["neden"] = "; ".join(nedenler)
    return sonuc


@router.post("/hatirlat/eposta-dene")
def hatirlatma_eposta_dene(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Hatırlatma adresine gerçek bir test maili gönderir."""
    a = ayar_satiri(db, kullanici)
    ayarlar = cozulmus_ayarlar(a)
    hedef = hatirlatma_ayari(a)["adres"] or kullanici.eposta
    metin = "\n".join([
        f"Bu bir test e-postasıdır; hatırlatmalar da {servisler.gonderen_adresi()} adresinden böyle gelir.",
        "", app_url(),
    ])
    try:
        hata = servisler.eposta_gonder(
            hedef, "Günlük rapor — e-posta testi", metin, yanit_adresi=kullanici.eposta,
            gmail_kullanici=ayarlar["gmail_kullanici"], gmail_sifre=ayarlar["gmail_sifre"], gonderen_adi=kullanici.ad)
    except Exception as e:
        hata = f"E-posta gönderilemedi ({e.__class__.__name__}: {e})"
    if hata:
        log.warning("hatirlat eposta testi user=%s neden=%s", kullanici.id, hata[:200])
        return {"ok": False, "neden": hata}
    log.info("hatirlat eposta testi user=%s neden=gönderildi", kullanici.id)
    return {"ok": True}


@router.post("/hatirlat")
def hatirlat(request: Request, token: str = "", db: Session = Depends(oturum)) -> dict:
    """Oturumsuz; cron-job.org çağırır. Kullanıcılar id sırasıyla, süre sınırı aşılırsa kalanlar sonraki ping'e."""
    global son_hatirlat_ping
    if not cron_tokeni_dogru(request, token):
        raise HTTPException(status_code=401, detail="Geçersiz token")
    an = istanbul_simdi()
    son_hatirlat_ping = an.isoformat(timespec="seconds")
    if not _hatirlat_kilidi.acquire(blocking=False):
        return {"zaman": son_hatirlat_ping, "mesgul": True, "kullanicilar": [], "kalan": 0}
    try:
        baslangic = saat_.monotonic()
        idler = list(db.scalars(select(Kullanici.id).where(Kullanici.aktif.is_(True)).order_by(Kullanici.id)))
        sonuclar = []
        for sira, uid in enumerate(idler):
            if saat_.monotonic() - baslangic >= SURE_SINIRI:
                return {"zaman": son_hatirlat_ping, "kullanicilar": sonuclar, "kalan": len(idler) - sira}
            try:
                sonuclar.append(kullaniciya_hatirlat(db, db.get(Kullanici, uid), istanbul_simdi()))
            except Exception as e:
                db.rollback()
                sonuclar.append({"user_id": uid, "push": "hata", "eposta": "hata", "neden": f"beklenmeyen hata ({e.__class__.__name__})"})
        return {"zaman": son_hatirlat_ping, "kullanicilar": sonuclar, "kalan": 0}
    finally:
        _hatirlat_kilidi.release()


# ---------------------------------------------------------------- O1: unutursan otomatik e-posta teslimi

OTOMATIK_UYARI_ONCESI = timedelta(minutes=15)
OTOMATIK_ALT_SATIR = "Bu rapor Günlük Rapor ile gönderildi."
OTOMATIK_KAYIT_NEDENI = {
    "iptal": "bugün iptal edildi", "gonderildi": "bugün gönderildi", "gonderiliyor": "gönderim sürüyor",
    "hata": "bugün denendi, hata verdi",
}


def patron_hedefi(o: dict, varsayilan: str = "patronuna") -> str:
    """'Ahmet Bey' → "Ahmet Bey'e"; ad yoksa varsayılan."""
    return servisler.yonelme_eki(o["patron_adi"]) if o["patron_adi"] else varsayilan


def otomatik_kaydi(db: Session, user_id: int, tarih: date) -> HatirlatmaGonderimi | None:
    return db.scalar(select(HatirlatmaGonderimi).where(
        HatirlatmaGonderimi.user_id == user_id, HatirlatmaGonderimi.tarih == tarih, HatirlatmaGonderimi.kanal == "otomatik",
    ))


def otomatik_kaydini_devral(db: Session, kayit_id: int, eski: tuple[str, ...], yeni: str) -> bool:
    """Satırın durumu hâlâ `eski`lerden biriyse tek UPDATE ile `yeni`ye geçirir (aynı anda iki istek tek kazanır)."""
    tablo = HatirlatmaGonderimi.__table__
    n = db.execute(tablo.update().where(tablo.c.id == kayit_id, tablo.c.durum.in_(eski)).values(
        durum=yeni, hata_metni=None)).rowcount
    db.commit()
    return bool(n)


def otomatik_ozeti(db: Session, k: Kullanici, a: KullaniciAyari, tarih: date) -> dict:
    """Bugün sayfası için: bilgi satırı, mavi şerit ve başarısız gönderim uyarısı."""
    o = otomatik_ayari(a)
    kayit = otomatik_kaydi(db, k.id, tarih)
    return {
        "acik": o["acik"] and bool(o["patron_eposta"]),
        "saat": o["saat"].strftime("%H:%M"),
        "patron_adi": o["patron_adi"],
        "hedef": patron_hedefi(o),
        "gun": tarih.isoweekday() in hatirlatma_ayari(a)["gunler"],
        "durum": kayit.durum if kayit else None,  # None | gonderiliyor | gonderildi | hata | iptal
        "hata": (kayit.hata_metni or "") if kayit else "",
    }


def otomatik_hazirlik(db: Session, k: Kullanici, tarih: date) -> int:
    """Bulunanlar taranır (günde bir, önbellekli; tarama düşerse mevcut maddelerle devam edilir).
    Rapora girecek tikli madde sayısını döner."""
    try:
        bugun_taramasi(db, k, tarih)
    except Exception as e:
        db.rollback()
        log.warning("otomatik tarama yapilamadi user=%s neden=%s", k.id, e.__class__.__name__)
    return sum(1 for m in rapor_adaylari(db, k, tarih, ayar_satiri(db, k)) if m.tikli)


def otomatik_rapor_metni(db: Session, k: Kullanici, tarih: date, duzelt: bool = True) -> str:
    """Kopyala'nın üreteceği metin. duzelt: önce /api/duzelt gibi günün paketi düzeltilir (kota dahil);
    anahtar yoksa, kota doluysa ya da Claude hata verirse ham metinle devam edilir."""
    anahtar = ai_anahtari() if duzelt else ""
    if anahtar:
        try:
            with _kullanici_kilidi(k.id, "duzelt"):
                sonuc = duzeltmeyi_uygula(db, k, duzeltme_paketi(db, k, tarih), tarih, anahtar)
            neden = sonuc.get("atlandi") or "; ".join(sonuc["hatalar"])
            if neden:
                log.info("otomatik duzeltme user=%s neden=%s", k.id, neden[:200])
        except Exception as e:
            db.rollback()
            log.warning("otomatik duzeltme user=%s beklenmeyen hata (%s)", k.id, e.__class__.__name__)
    return gunun_rapor_metni(db, k, tarih)


def otomatik_eposta(k: Kullanici, tarih: date, metin: str, test: bool = False,
                    eslemeler: list[dict] | None = None) -> tuple[str, str]:
    """(konu, gövde): gövde, Kopyala metninin kalın işaretsiz hâli ve tek satırlık alt not. Ad eşlemeleri ikisine de
    uygulanır (metin zaten eşlenmiştir; ikinci uygulama bir şey değiştirmez)."""
    konu = servisler.ad_esle(f"Günlük Rapor – {k.ad} – {tarih.strftime('%d.%m.%Y')}", eslemeler)
    govde = servisler.ad_esle(f"{servisler.kalin_isaretsiz(metin)}\n\n{OTOMATIK_ALT_SATIR}", eslemeler)
    return (f"[TEST] {konu}" if test else konu), govde


def otomatik_gonder(db: Session, k: Kullanici, tarih: date, kayit: HatirlatmaGonderimi, bildir: bool = True) -> str | None:
    """kayit: 'gonderiliyor' ön kaydı (çift gönderim kilidi). Başarıda None; hatada neden yazılır, bildir ise push'la
    kullanıcıya haber verilir. Kayıt kaldığı için cron aynı gün yeniden denemez."""
    a = ayar_satiri(db, k)
    o = otomatik_ayari(a)
    try:
        metin = otomatik_rapor_metni(db, k, tarih)
        konu, govde = otomatik_eposta(k, tarih, metin, eslemeler=ad_eslemeleri(a))
        ayarlar = cozulmus_ayarlar(a)
        hata = servisler.eposta_gonder(
            o["patron_eposta"], konu, govde, yanit_adresi=k.eposta, kopya=k.eposta if o["kopya_bana"] else None,
            gmail_kullanici=ayarlar["gmail_kullanici"], gmail_sifre=ayarlar["gmail_sifre"], gonderen_adi=k.ad,
        )
    except Exception as e:
        db.rollback()
        hata = f"beklenmeyen hata ({e.__class__.__name__})"
    if hata:
        kanal_hatasi_yaz(kayit, k.id, "otomatik", hata)
        db.commit()
        if bildir:
            try:
                push_cihazlarina_gonder(db, k.id, {
                    "baslik": "Günlük Rapor", "govde": f"Otomatik gönderim başarısız: {hata} — elle gönder", "url": app_url(),
                })
            except Exception:
                db.rollback()
        return hata
    rapor_yaz(db, k.id, tarih, "gunluk", metin, gonderim="otomatik")
    kayit.durum = "gonderildi"
    db.commit()
    return None


def otomatik_uyarisi(db: Session, k: Kullanici, a: KullaniciAyari, o: dict, tarih: date) -> str:
    """15 dk önce bir kez: push (cihaz varsa) + e-posta hatırlatması açıksa e-posta. Bağlantı mavi şeridi açar."""
    kayit = kanal_talep_et(db, k.id, tarih, "otomatik_uyari")
    if kayit is None:
        return "ön uyarı gönderildi"
    cumle = servisler.ad_esle(
        f"Raporun {servisler.saatte_eki(o['saat'].strftime('%H:%M'))} {patron_hedefi(o, 'patrona')} e-postayla gidecek.",
        ad_eslemeleri(a))
    adres = f"{app_url()}/?otomatik=uyari"
    parcalar, hatalar, ulasti = [], [], False
    try:
        basarili, toplam, ayrinti = push_cihazlarina_gonder(db, k.id, {"baslik": "Günlük Rapor", "govde": cumle, "url": adres})
        if toplam:
            parcalar.append(f"push {basarili}/{toplam}")
            ulasti = ulasti or bool(basarili)
            hatalar += [c["mesaj"] for c in ayrinti if c["durum"] != "ok"]
    except Exception as e:
        db.rollback()
        hatalar.append(f"push: beklenmeyen hata ({e.__class__.__name__})")
    h = hatirlatma_ayari(a)
    if h["eposta"]:
        ayarlar = cozulmus_ayarlar(a)
        govde = "\n".join([cumle, "", f"Şimdi gönder ya da bugün iptal et: {adres}", "",
                           "Rapor bu saate kadar kopyalanırsa hiçbir şey gönderilmez."])
        try:
            hata = servisler.eposta_gonder(
                h["adres"] or k.eposta, f"{cumle[:-1]} – {tarih.strftime('%d.%m.%Y')}", govde, yanit_adresi=k.eposta,
                gmail_kullanici=ayarlar["gmail_kullanici"], gmail_sifre=ayarlar["gmail_sifre"], gonderen_adi=k.ad,
            )
        except Exception as e:
            hata = f"E-posta gönderilemedi ({e.__class__.__name__})"
        parcalar.append("e-posta " + ("hata" if hata else "gönderildi"))
        ulasti = ulasti or not hata
        if hata:
            hatalar.append(hata)
    if ulasti:
        kayit.durum = "gonderildi"
    else:
        kanal_hatasi_yaz(kayit, k.id, "otomatik_uyari", "; ".join(hatalar) or "bildirim açılmış cihaz yok, e-posta hatırlatması kapalı")
    db.commit()
    return "ön uyarı: " + (", ".join(parcalar) or "ulaşılacak kanal yok")


def otomatik_denetle(db: Session, k: Kullanici, an: datetime) -> str | None:
    """Cron ping'i başına: otomatik gönderim kapalıysa None; değilse ne yapıldığını ya da neden yapılmadığını döner."""
    a = ayar_satiri(db, k)
    o = otomatik_ayari(a)
    if not (o["acik"] and o["patron_eposta"]):
        return None
    tarih = an.date()
    if tarih.isoweekday() not in hatirlatma_ayari(a)["gunler"]:
        return "bugün gönderim günü değil"
    gonderim_ani = datetime.combine(tarih, o["saat"], tzinfo=an.tzinfo)
    uyari_ani = gonderim_ani - OTOMATIK_UYARI_ONCESI
    if an < uyari_ani:
        return f"saat {uyari_ani.strftime('%H:%M')} olmadı"
    if db.scalar(select(Rapor.id).where(Rapor.user_id == k.id, Rapor.tur == "gunluk", Rapor.tarih == tarih)):
        return "bugünün raporu kopyalanmış"
    kayitlar = {g.kanal: g for g in db.scalars(select(HatirlatmaGonderimi).where(
        HatirlatmaGonderimi.user_id == k.id, HatirlatmaGonderimi.tarih == tarih,
        HatirlatmaGonderimi.kanal.in_(("otomatik", "otomatik_uyari")),
    ))}
    if "otomatik" in kayitlar:
        return OTOMATIK_KAYIT_NEDENI.get(kayitlar["otomatik"].durum, kayitlar["otomatik"].durum)
    uyari_vakti = an < gonderim_ani  # saat geçtiyse (ping kaçtıysa) uyarı atlanır, doğrudan gönderilir
    if uyari_vakti and "otomatik_uyari" in kayitlar:
        return "ön uyarı gönderildi"
    if not otomatik_hazirlik(db, k, tarih):
        return "gönderilecek tikli madde yok"
    if uyari_vakti:
        return otomatik_uyarisi(db, k, a, o, tarih)
    kayit = kanal_talep_et(db, k.id, tarih, "otomatik")
    if kayit is None:
        return "gönderim sürüyor"
    hata = otomatik_gonder(db, k, tarih, kayit)
    return f"hata: {hata}" if hata else "gönderildi"


class OtomatikGun(BaseModel):
    tarih: date | None = None


def otomatik_gunu(tarih: date | None) -> date:
    bugun_ = bugun()
    if tarih is not None and tarih != bugun_:
        raise HTTPException(status_code=422, detail="Yalnız bugünün otomatik gönderimi değiştirilebilir")
    return bugun_


@router.post("/otomatik/iptal")
def otomatik_iptal(
    govde: OtomatikGun | None = None, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    """Bugünün otomatik gönderimini iptal eder: kanal='otomatik' satırı 'iptal' olarak yazılır."""
    tarih = otomatik_gunu(govde.tarih if govde else None)
    if kanal_talep_et(db, kullanici.id, tarih, "otomatik", durum="iptal") is None:
        kayit = otomatik_kaydi(db, kullanici.id, tarih)
        if kayit is None or not (kayit.durum == "iptal" or otomatik_kaydini_devral(db, kayit.id, ("hata",), "iptal")):
            raise HTTPException(status_code=409, detail="Rapor zaten e-postayla gönderildi")
    log.info("otomatik iptal user=%s", kullanici.id)
    return {"ok": True, "otomatik": otomatik_ozeti(db, kullanici, ayar_satiri(db, kullanici), tarih)}


@router.post("/otomatik/simdi")
def otomatik_simdi(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Kullanıcı eylemi: aynı gönderim hemen. İptal edilmiş ya da hata vermiş gün yeniden gönderilebilir."""
    tarih = bugun()
    a = ayar_satiri(db, kullanici)
    if not otomatik_ayari(a)["patron_eposta"]:
        raise HTTPException(status_code=422, detail="Patron e-postası gerekli")
    if db.scalar(select(Rapor.id).where(Rapor.user_id == kullanici.id, Rapor.tur == "gunluk", Rapor.tarih == tarih)):
        raise HTTPException(status_code=409, detail="Bugünün raporu zaten gönderildi")
    if not otomatik_hazirlik(db, kullanici, tarih):
        raise HTTPException(status_code=422, detail="Gönderilecek tikli madde yok")
    kayit = kanal_talep_et(db, kullanici.id, tarih, "otomatik")
    if kayit is None:
        kayit = otomatik_kaydi(db, kullanici.id, tarih)
        if kayit is None or not otomatik_kaydini_devral(db, kayit.id, ("iptal", "hata"), "gonderiliyor"):
            raise HTTPException(status_code=409, detail="Rapor zaten e-postayla gönderildi")
        db.refresh(kayit)
    hata = otomatik_gonder(db, kullanici, tarih, kayit, bildir=False)
    log.info("otomatik simdi user=%s sonuc=%s", kullanici.id, hata[:200] if hata else "gönderildi")
    if hata:
        raise HTTPException(status_code=502, detail=f"Gönderilemedi: {hata}")
    rapor = db.scalar(select(Rapor).where(Rapor.user_id == kullanici.id, Rapor.tur == "gunluk", Rapor.tarih == tarih))
    return {"ok": True, "rapor": rapor_json(rapor, ad_eslemeleri(a)), "otomatik": otomatik_ozeti(db, kullanici, a, tarih)}


@router.post("/otomatik/test")
def otomatik_test(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Yalnız kullanıcının kendisine, patrona gidecek biçimde (konu başında [TEST]). Claude kotası harcanmaz;
    rapor kaydedilmez."""
    tarih = bugun()
    if not otomatik_hazirlik(db, kullanici, tarih):
        raise HTTPException(status_code=422, detail="Gönderilecek tikli madde yok")
    a = ayar_satiri(db, kullanici)
    konu, govde = otomatik_eposta(kullanici, tarih, otomatik_rapor_metni(db, kullanici, tarih, duzelt=False), test=True,
                                  eslemeler=ad_eslemeleri(a))
    ayarlar = cozulmus_ayarlar(a)
    try:
        hata = servisler.eposta_gonder(
            kullanici.eposta, konu, govde, yanit_adresi=kullanici.eposta,
            gmail_kullanici=ayarlar["gmail_kullanici"], gmail_sifre=ayarlar["gmail_sifre"], gonderen_adi=kullanici.ad)
    except Exception as e:
        hata = f"E-posta gönderilemedi ({e.__class__.__name__})"
    log.info("otomatik test user=%s sonuc=%s", kullanici.id, hata[:200] if hata else "gönderildi")
    if hata:
        gonderen = servisler.kullanilan_gonderen(ayarlar["gmail_kullanici"], kullanici.ad)
        return {"ok": False, "neden": f"{hata} · Kullanılan gönderen: {gonderen}"}
    return {"ok": True, "adres": kullanici.eposta}
