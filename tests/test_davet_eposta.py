"""K1-ek3: davet ve şifre sıfırlama e-postası. Resend sahte; ağa çıkılmaz."""
import re
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

import app as uygulama
import guvenlik
import servisler
import veritabani
from veritabani import Kullanici, KullaniciAyari, OturumYapici, Temel, motor

SIFRE = "dogru-sifre-123"


class SahteResend:
    """servisler.httpx.Client yerine geçer: Resend isteklerini kaydeder, ayarlanan yanıtı döner."""

    istekler: list = []
    kod: int = 200
    govde: dict | None = None

    def __init__(self, *a, **kw):
        pass

    def post(self, url, json=None, headers=None):
        SahteResend.istekler.append({"url": url, "govde": json, "headers": headers})
        return httpx.Response(SahteResend.kod, json=SahteResend.govde if SahteResend.govde is not None else {"id": "re_1"})


@pytest.fixture(autouse=True)
def ortam(monkeypatch):
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    monkeypatch.setenv("APP_URL", "https://rapor.ornek.com")
    yield


@pytest.fixture
def resend(monkeypatch):
    SahteResend.istekler, SahteResend.kod, SahteResend.govde = [], 200, None
    monkeypatch.setenv("RESEND_API_KEY", "re_test_anahtar")
    monkeypatch.setenv("EPOSTA_GONDEREN", "rapor@medusarights.com")
    monkeypatch.setattr(servisler.httpx, "Client", SahteResend)
    return SahteResend


def kullanici_olustur(eposta="yonetici@ornek.com", ad="Ufuk", rol="admin") -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad=ad, sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol=rol,
                      aktif=True, sifre_degistirmeli=False)
        db.add(k)
        db.commit()
        db.add(KullaniciAyari(user_id=k.id, kurulum_tamam=True))
        db.commit()
        return k.id


def istemci(eposta="yonetici@ornek.com") -> TestClient:
    c = TestClient(uygulama.app, follow_redirects=False)
    assert c.post("/giris", data={"eposta": eposta, "sifre": SIFRE}).status_code == 303
    return c


def davet_et(c, ad="Ayşe", eposta="ayse@ornek.com"):
    y = c.post("/yonetim/davet", data={"ad": ad, "eposta": eposta})
    assert y.status_code == 200
    return y.text


def sifre_ve_hedef(html: str) -> tuple[str, str]:
    """Ekranda gösterilen geçici şifre ile elle gönderme formunun hedef id'si (mail gitmişse form yok → "")."""
    sifre = re.search(r'<code id="gecici">([^<]+)</code>', html).group(1)
    hedef = re.search(r'action="/yonetim/(\d+)/davet-eposta"', html)
    return sifre, (hedef.group(1) if hedef else "")


# ---------------------------------------------------------------- gövde ve başlıklar

def test_davet_govdesinde_baglanti_eposta_ve_gecici_sifre_gecer(resend):
    kullanici_olustur()
    c = istemci()
    sifre, hedef = sifre_ve_hedef(davet_et(c))
    y = c.post(f"/yonetim/{hedef}/davet-eposta", data={"sifre": sifre, "tur": "davet"})

    assert y.status_code == 200
    govde = resend.istekler[0]["govde"]
    assert govde["subject"] == "Günlük rapor aracına davet"
    assert govde["to"] == ["ayse@ornek.com"]
    metin = govde["text"]
    assert "https://rapor.ornek.com/giris" in metin
    assert "ayse@ornek.com" in metin and sifre in metin
    assert "İlk girişte yeni şifre belirleyeceksin." in metin
    assert "İlk açılışta 4 adımlık kurulum seni karşılar." in metin
    assert "Bu e-postayı yanıtlarsan Ufuk'a ulaşır." in metin
    assert 'class="sonuc">Davet e-postası gönderildi — ayse@ornek.com' in y.text and sifre in y.text


def test_yanit_adresi_daveti_gonderen_yoneticinin_epostasi(resend):
    kullanici_olustur(eposta="patron@ornek.com", ad="Ufuk")
    c = istemci("patron@ornek.com")
    sifre, hedef = sifre_ve_hedef(davet_et(c))
    c.post(f"/yonetim/{hedef}/davet-eposta", data={"sifre": sifre})
    assert resend.istekler[0]["govde"]["reply_to"] == "patron@ornek.com"


def test_sifirlama_epostasi_yeni_gecici_sifreyi_ve_baglantiyi_tasir(resend):
    kullanici_olustur()
    c = istemci()
    _, hedef = sifre_ve_hedef(davet_et(c))
    y = c.post(f"/yonetim/{hedef}/sifirla")
    yeni_sifre, hedef2 = sifre_ve_hedef(y.text)
    assert hedef2 == hedef

    c.post(f"/yonetim/{hedef}/davet-eposta", data={"sifre": yeni_sifre, "tur": "sifirlama"})
    govde = resend.istekler[0]["govde"]
    assert govde["subject"] == "Günlük rapor — şifren sıfırlandı"
    assert yeni_sifre in govde["text"] and "https://rapor.ornek.com/giris" in govde["text"]
    assert "şifren sıfırlandı" in govde["text"]


def test_yonelme_eki_yoneticinin_adina_uyar():
    _, metin = servisler.davet_eposta_metni("A", "a@b.c", "s", "https://x/giris", "Ali")
    assert metin.endswith("Bu e-postayı yanıtlarsan Ali'ye ulaşır.")


# ---------------------------------------------------------------- yetki ve doğrulama

def test_sifre_govdede_yoksa_422(resend):
    kullanici_olustur()
    c = istemci()
    _, hedef = sifre_ve_hedef(davet_et(c))
    assert c.post(f"/yonetim/{hedef}/davet-eposta", data={"tur": "davet"}).status_code == 422
    assert resend.istekler == []


def test_uye_rolu_403(resend):
    kullanici_olustur()
    c = istemci()
    _, hedef = sifre_ve_hedef(davet_et(c))
    with OturumYapici() as db:
        uye = db.get(Kullanici, int(hedef))
        uye.sifre_hash, uye.sifre_degistirmeli = guvenlik.sifre_ozeti(SIFRE), False
        db.commit()
    uye_c = istemci("ayse@ornek.com")
    y = uye_c.post(f"/yonetim/{hedef}/davet-eposta", data={"sifre": "x", "tur": "davet"})
    assert y.status_code == 403 and resend.istekler == []


def test_oturumsuz_giris_sayfasina(resend):
    kullanici_olustur()
    y = TestClient(uygulama.app, follow_redirects=False).post("/yonetim/1/davet-eposta", data={"sifre": "x"})
    assert y.status_code == 303 and y.headers["location"] == "/giris"
    assert resend.istekler == []


def test_bilinmeyen_kullanici_404(resend):
    kullanici_olustur()
    y = istemci().post("/yonetim/9999/davet-eposta", data={"sifre": "x"})
    assert y.status_code == 404 and resend.istekler == []


# ---------------------------------------------------------------- ekrandaki sonuç ve yapılandırma

def test_resend_anahtari_yoksa_dugme_pasif_ve_aciklama_var(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "")
    kullanici_olustur()
    html = davet_et(istemci())
    assert "Davet e-postası gönder" in html
    assert re.search(r'<button class="primary" type="submit" disabled>Davet e-postası gönder', html)
    assert "RESEND_API_KEY yok" in html


def test_resend_anahtari_varken_dugme_etkin(resend):
    kullanici_olustur()
    html = davet_et(istemci())
    assert '<button class="primary" type="submit">Davet e-postası gönder' in html


def test_resend_anahtari_yokken_gonderim_denenmez(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "")
    kullanici_olustur()
    c = istemci()
    sifre, hedef = sifre_ve_hedef(davet_et(c))
    y = c.post(f"/yonetim/{hedef}/davet-eposta", data={"sifre": sifre})
    assert y.status_code == 400 and "RESEND_API_KEY yok" in y.text
    with OturumYapici() as db:
        assert db.get(Kullanici, int(hedef)).davet_eposta_tarihi is None


def test_gonderim_hatasi_turkce_neden_olarak_ekranda(resend):
    kullanici_olustur()
    c = istemci()
    sifre, hedef = sifre_ve_hedef(davet_et(c))
    resend.kod, resend.govde = 403, {"message": "Alan adı doğrulanmadı"}
    y = c.post(f"/yonetim/{hedef}/davet-eposta", data={"sifre": sifre})
    assert y.status_code == 200 and "Resend 403: Alan adı doğrulanmadı" in y.text
    assert 'class="sonuc">' not in y.text and "şifreyi elle ilet" in y.text
    with OturumYapici() as db:
        assert db.get(Kullanici, int(hedef)).davet_eposta_tarihi is None


# ---------------------------------------------------------------- davet_eposta_tarihi

def test_basarili_gonderim_davet_eposta_tarihini_yazar_ve_listede_gorunur(resend):
    kullanici_olustur()
    c = istemci()
    sifre, hedef = sifre_ve_hedef(davet_et(c))
    c.post(f"/yonetim/{hedef}/davet-eposta", data={"sifre": sifre})
    with OturumYapici() as db:
        an = db.get(Kullanici, int(hedef)).davet_eposta_tarihi
    assert isinstance(an, datetime)
    assert "davet e-postası gönderildi" in c.get("/yonetim").text


def test_gonderilmemis_kullanicida_tarih_satiri_yok(resend):
    kullanici_olustur()
    c = istemci()
    davet_et(c)
    assert "davet e-postası gönderildi" not in c.get("/yonetim").text


# ---------------------------------------------------------------- migration

def test_sema_guncelle_davet_eposta_tarihi_kolonunu_idempotent_ekler(tmp_path):
    eski = create_engine(f"sqlite:///{tmp_path}/eski.db")
    with eski.begin() as b:
        b.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, eposta VARCHAR(254))"))
    assert "users.davet_eposta_tarihi" in veritabani.sema_guncelle(eski)
    assert "users.davet_eposta_tarihi" not in veritabani.sema_guncelle(eski)
    with eski.begin() as b:
        b.execute(text("INSERT INTO users (id, eposta) VALUES (1, 'a@b.c')"))
        assert b.execute(text("SELECT davet_eposta_tarihi FROM users")).scalar() is None


# ---------------------------------------------------------------- K1-ek4: tek adımda gönderim

def test_davet_tikliyken_mail_ayni_istekte_gider_ve_tarih_yazilir(resend):
    kullanici_olustur()
    c = istemci()
    y = c.post("/yonetim/davet", data={"ad": "Ayşe", "eposta": "ayse@ornek.com", "mail_gonder": "on"})

    assert y.status_code == 200
    govde = resend.istekler[0]["govde"]
    assert govde["to"] == ["ayse@ornek.com"] and govde["subject"] == "Günlük rapor aracına davet"
    assert govde["reply_to"] == "yonetici@ornek.com"
    sifre, hedef = sifre_ve_hedef(y.text)
    assert sifre in govde["text"]
    assert 'class="sonuc">Davet e-postası gönderildi — ayse@ornek.com' in y.text
    with OturumYapici() as db:
        k = db.scalar(select(Kullanici).where(Kullanici.eposta == "ayse@ornek.com"))
        assert k.davet_eposta_tarihi is not None and k.sifre_degistirmeli is True


def test_davet_tik_kapaliyken_mail_gitmez_elle_gonderme_dugmesi_kalir(resend):
    kullanici_olustur()
    c = istemci()
    y = c.post("/yonetim/davet", data={"ad": "Ayşe", "eposta": "ayse@ornek.com"})

    assert y.status_code == 200 and resend.istekler == []
    assert "Davet e-postası gönder" in y.text and 'class="sonuc">' not in y.text
    assert re.search(r'action="/yonetim/\d+/davet-eposta"', y.text)
    with OturumYapici() as db:
        assert db.scalar(select(Kullanici).where(Kullanici.eposta == "ayse@ornek.com")).davet_eposta_tarihi is None


def test_davette_mail_hatasi_kullanici_olusturmayi_geri_almaz(resend):
    kullanici_olustur()
    resend.kod, resend.govde = 403, {"message": "Alan adı doğrulanmadı"}
    c = istemci()
    y = c.post("/yonetim/davet", data={"ad": "Ayşe", "eposta": "ayse@ornek.com", "mail_gonder": "on"})

    assert y.status_code == 200
    assert "E-posta gönderilemedi: Resend 403: Alan adı doğrulanmadı — şifreyi elle ilet" in y.text
    with OturumYapici() as db:
        k = db.scalar(select(Kullanici).where(Kullanici.eposta == "ayse@ornek.com"))
        assert k is not None and k.davet_eposta_tarihi is None
    # şifre panelde durur, elle gönderme düğmesi de
    sifre, hedef = sifre_ve_hedef(y.text)
    assert sifre and hedef


def test_sifirlama_tikliyken_mail_ayni_istekte_gider(resend):
    kullanici_olustur()
    c = istemci()
    _, hedef = sifre_ve_hedef(davet_et(c))
    y = c.post(f"/yonetim/{hedef}/sifirla", data={"mail_gonder": "on"})

    assert y.status_code == 200
    yeni_sifre, _ = sifre_ve_hedef(y.text)
    govde = resend.istekler[0]["govde"]
    assert govde["subject"] == "Günlük rapor — şifren sıfırlandı" and yeni_sifre in govde["text"]
    assert 'class="sonuc">Yeni şifre e-postayla gönderildi — ayse@ornek.com' in y.text
    with OturumYapici() as db:
        assert db.get(Kullanici, int(hedef)).davet_eposta_tarihi is not None


def test_sifirlama_tik_kapaliyken_mail_gitmez_ama_sifre_degisir(resend):
    kullanici_olustur()
    c = istemci()
    eski_sifre, hedef = sifre_ve_hedef(davet_et(c))
    y = c.post(f"/yonetim/{hedef}/sifirla")

    assert y.status_code == 200 and resend.istekler == []
    yeni_sifre, _ = sifre_ve_hedef(y.text)
    assert yeni_sifre != eski_sifre
    with OturumYapici() as db:
        assert db.get(Kullanici, int(hedef)).davet_eposta_tarihi is None


def test_sifirlamada_mail_hatasi_yeni_sifreyi_geri_almaz(resend):
    kullanici_olustur()
    c = istemci()
    eski_sifre, hedef = sifre_ve_hedef(davet_et(c))
    resend.kod, resend.govde = 403, {"message": "Alan adı doğrulanmadı"}
    y = c.post(f"/yonetim/{hedef}/sifirla", data={"mail_gonder": "on"})

    assert y.status_code == 200 and "şifreyi elle ilet" in y.text
    yeni_sifre, _ = sifre_ve_hedef(y.text)
    assert yeni_sifre != eski_sifre
    with OturumYapici() as db:
        k = db.get(Kullanici, int(hedef))
        assert k.davet_eposta_tarihi is None and guvenlik.sifre_dogru(yeni_sifre, k.sifre_hash)


def test_tik_varsayilan_isaretli_resend_yoksa_pasif(monkeypatch, resend):
    kullanici_olustur()
    html = istemci().get("/yonetim").text
    assert html.count('<input type="checkbox" name="mail_gonder" checked>') >= 1

    monkeypatch.setenv("RESEND_API_KEY", "")
    html = istemci().get("/yonetim").text
    assert '<input type="checkbox" name="mail_gonder" checked disabled>' in html
    assert "RESEND_API_KEY yok" in html


def test_resend_yokken_tik_gonderilse_bile_neden_gosterilir(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "")
    kullanici_olustur()
    y = istemci().post("/yonetim/davet", data={"ad": "Ayşe", "eposta": "ayse@ornek.com", "mail_gonder": "on"})
    assert y.status_code == 200 and "RESEND_API_KEY yok" in y.text and "şifreyi elle ilet" in y.text
    with OturumYapici() as db:
        assert db.scalar(select(Kullanici).where(Kullanici.eposta == "ayse@ornek.com")) is not None
