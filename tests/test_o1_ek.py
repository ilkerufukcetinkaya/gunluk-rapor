"""O1-ek: görünen ad, kişisel gönderen adı, ad eşlemeleri, tırnak içi koruma. Resend, push ve Claude sahte; saat sabit."""
import json
from datetime import date, datetime, time, timedelta
from email.headerregistry import HeaderRegistry
from email.message import EmailMessage

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import api
import app as uygulama
import guvenlik
import servisler
from veritabani import Kategori, Kullanici, KullaniciAyari, Madde, OturumYapici, Rapor, Temel, motor

SIFRE = "dogru-sifre-123"
TOKEN = "test-cron-token"
CARSAMBA = date(2026, 9, 16)
PATRON = "patron@firma.com"
ESLEME = [{"kaynak": "Medusa Right", "hedef": "Edisyon uygulaması"}, {"kaynak": "Medusa", "hedef": "Edisyon uygulaması"}]


class SahteResend:
    istekler: list = []

    def __init__(self, *a, **kw):
        pass

    def post(self, url, json=None, headers=None):
        SahteResend.istekler.append(json)
        return httpx.Response(200, json={"id": "re_1"})


class SahtePush:
    def __init__(self):
        self.cagrilar: list[dict] = []

    def __call__(self, subscription_info, data, **kw):
        self.cagrilar.append(json.loads(data))


@pytest.fixture(autouse=True)
def ortam(monkeypatch):
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    api.onbellegi_temizle()
    monkeypatch.setattr(servisler, "gmail_tara", lambda *a, **k: [])
    monkeypatch.setattr(servisler, "github_tara", lambda *a, **k: [])
    public, private = servisler.vapid_cifti_uret()
    monkeypatch.setenv("VAPID_PUBLIC_KEY", public)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", private)
    monkeypatch.setenv("APP_URL", "https://rapor.ornek.com")
    monkeypatch.setenv("CRON_TOKEN", TOKEN)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_anahtar")
    monkeypatch.setenv("EPOSTA_GONDEREN", "rapor@ornek.com")
    SahteResend.istekler = []
    monkeypatch.setattr(servisler.httpx, "Client", SahteResend)
    yield


@pytest.fixture
def push(monkeypatch):
    sahte = SahtePush()
    monkeypatch.setattr(servisler, "webpush", sahte)
    return sahte


@pytest.fixture
def saat(monkeypatch):
    def ayarla(gun: date, ss: int, dd: int):
        monkeypatch.setattr(api, "istanbul_simdi", lambda: datetime.combine(gun, time(ss, dd), servisler.ISTANBUL))
        monkeypatch.setattr(api, "bugun", lambda: gun)
    ayarla(CARSAMBA, 12, 0)
    return ayarla


def kullanici_olustur(eposta="a@ornek.com", ad="Ayşe Yılmaz", rol="uye", **ayar) -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad=ad, sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol=rol, aktif=True,
                      sifre_degistirmeli=False)
        db.add(k)
        db.commit()
        db.add(KullaniciAyari(user_id=k.id, kurulum_tamam=True, **ayar))
        db.commit()
        return k.id


def istemci(eposta="a@ornek.com") -> TestClient:
    c = TestClient(uygulama.app, follow_redirects=False)
    c.__enter__()
    assert c.post("/giris", data={"eposta": eposta, "sifre": SIFRE}).status_code == 303
    return c


def ekle(uid, metin, tur="bugun", gun=CARSAMBA, **alan) -> int:
    with OturumYapici() as db:
        m = Madde(user_id=uid, tur=tur, metin=metin, tikli=True,
                  **{**({"tarih": gun, "kaynak": "elle"} if tur == "bugun" else {}), **alan})
        db.add(m)
        db.commit()
        return m.id


def madde(mid) -> Madde:
    with OturumYapici() as db:
        return db.get(Madde, mid)


def cron() -> dict:
    r = TestClient(uygulama.app).post(f"/api/hatirlat?token={TOKEN}")
    assert r.status_code == 200
    return {x["user_id"]: x for x in r.json()["kullanicilar"]}


def esle(metin, eslemeler=ESLEME):
    return servisler.ad_esle(metin, eslemeler)


# ---------------------------------------------------------------- 1) görünen ad

def test_profil_adi_patch_dogrulama_ve_kullanici_ayrimi():
    kullanici_olustur()
    kullanici_olustur("b@ornek.com", "Burak")
    c = istemci()
    r = c.patch("/api/profil", json={"ad": "  Ayşe   Demir \n"})
    assert r.status_code == 200 and r.json() == {"ad": "Ayşe Demir", "ad_yer_tutucu": False}
    for kotu in ("", "   ", "A", "x" * 61, None):
        assert c.patch("/api/profil", json={"ad": kotu}).status_code == 422, kotu
    assert c.patch("/api/profil", json={}).status_code == 422
    assert c.patch("/api/profil", json={"ad": "Al"}).status_code == 200
    assert c.patch("/api/profil", json={"ad": "x" * 60}).json()["ad"] == "x" * 60
    with OturumYapici() as db:
        adlar = {k.eposta: k.ad for k in db.scalars(select(Kullanici))}
    assert adlar == {"a@ornek.com": "x" * 60, "b@ornek.com": "Burak"}
    assert TestClient(uygulama.app).patch("/api/profil", json={"ad": "Yeni"}).status_code == 401


def test_yer_tutucu_ad_ipucu_ve_kurulumla_ayni_alan():
    kullanici_olustur(ad="Yönetici", rol="admin")
    c = istemci()
    a = c.get("/api/ayarlar").json()
    assert a["ad"] == "Yönetici" and a["ad_yer_tutucu"] is True
    assert c.patch("/api/profil", json={"ad": "ADMİN"}).json()["ad_yer_tutucu"] is True
    assert c.post("/api/kurulum/profil", json={"ad": "Ayşe Yılmaz"}).json()["ad"] == "Ayşe Yılmaz"
    a = c.get("/api/ayarlar").json()
    assert a["ad"] == "Ayşe Yılmaz" and a["ad_yer_tutucu"] is False
    assert c.post("/api/kurulum/profil", json={"ad": "x" * 61}).status_code == 422  # kurulum da aynı kural
    sayfa = c.get("/ayarlar").text
    assert 'id="adDugme"' in sayfa and "Raporlarda ve e-postalarda görünecek adını yaz" in sayfa


# ---------------------------------------------------------------- 2) gönderen adı

def _from_ayristir(deger: str):
    return HeaderRegistry()("From", deger).addresses


@pytest.mark.parametrize("ad", ['Ayşe Yılmaz', 'Ali "Kral" Veli', "Ters\\Bölü, <x>", "Satır\r\nBcc: kotu@ornek.com"])
def test_gonderen_basligi_rfc5322_tirnaklama(ad):
    deger = servisler.gonderen_basligi("rapor@ornek.com", ad)
    assert deger.startswith('"') and deger.endswith('" <rapor@ornek.com>') and "\n" not in deger and "\r" not in deger
    [adres] = _from_ayristir(deger)  # tek adres: virgül, <> ve satır sonu yeni alıcı açmaz
    assert adres.addr_spec == "rapor@ornek.com"
    assert adres.display_name == " ".join(ad.split()) + " · Günlük Rapor"
    m = EmailMessage()
    m["From"], m["To"] = deger, "x@ornek.com"
    m.set_content("x")
    assert b"Bcc:" not in m.as_bytes().split(b"\n\n")[0].replace(b"From:", b"").split(b"\nTo:")[1]


def test_gonderen_adi_yoksa_yalniz_adres():
    assert servisler.gonderen_basligi("rapor@ornek.com", "") == "rapor@ornek.com"
    assert servisler.gonderen_basligi("rapor@ornek.com", None) == "rapor@ornek.com"


def test_otomatik_test_ve_hatirlatma_testi_kullanici_adiyla(saat):
    uid = kullanici_olustur(ad='Ayşe "Ay" Yılmaz', otomatik_gonder=True, patron_eposta=PATRON)
    ekle(uid, "Teklif hazırlandı")
    c = istemci()
    assert c.post("/api/otomatik/test").json()["ok"] is True
    assert c.post("/api/hatirlat/eposta-dene").json()["ok"] is True
    for g in SahteResend.istekler:
        [adres] = _from_ayristir(g["from"])
        assert adres.addr_spec == "rapor@ornek.com" and adres.display_name == 'Ayşe "Ay" Yılmaz · Günlük Rapor'
    assert SahteResend.istekler[0]["from"] == '"Ayşe \\"Ay\\" Yılmaz · Günlük Rapor" <rapor@ornek.com>'


def test_davet_ve_sifirlama_yonetici_adiyla():
    kullanici_olustur("yonetici@ornek.com", "Mehmet Kaya", rol="admin")
    c = istemci("yonetici@ornek.com")
    assert c.post("/yonetim/davet", data={"ad": "Zeynep", "eposta": "z@ornek.com", "mail_gonder": "1"}).status_code == 200
    with OturumYapici() as db:
        zid = db.scalar(select(Kullanici.id).where(Kullanici.eposta == "z@ornek.com"))
    assert c.post(f"/yonetim/{zid}/sifirla", data={"mail_gonder": "1"}).status_code == 200
    assert [g["from"] for g in SahteResend.istekler] == ['"Mehmet Kaya · Günlük Rapor" <rapor@ornek.com>'] * 2


# ---------------------------------------------------------------- 3) ad eşlemeleri: kural

@pytest.mark.parametrize("girdi, beklenen", [
    ("Medusa Right Geliştirme Önerileri", "Edisyon uygulaması Geliştirme Önerileri"),
    ("MEDUSA'da testler yapıldı", "Edisyon uygulaması'nda testler yapıldı"),
    ("Medusa'nın raporu hazırlandı", "Edisyon uygulaması'nın raporu hazırlandı"),
    ("medusa ile görüşüldü", "Edisyon uygulaması ile görüşüldü"),
    ("Medusa'ya iletildi, Medusa’dan yanıt geldi", "Edisyon uygulaması'na iletildi, Edisyon uygulaması’ndan yanıt geldi"),
    ("'Medusa Right' toplantısı yapıldı", "'Edisyon uygulaması' toplantısı yapıldı"),
    ("medusarights.com ve Medusalı ekip", "medusarights.com ve Medusalı ekip"),  # kelime sınırı
    ("Medusa Rightçı değil", "Edisyon uygulaması Rightçı değil"),
])
def test_ad_eslemesi_ek_uyumu_ve_sinirlar(girdi, beklenen):
    assert esle(girdi) == beklenen
    assert esle(esle(girdi)) == esle(girdi)  # ikinci uygulama bir şey değiştirmez


def test_uzun_eslesme_once_liste_sirasindan_bagimsiz():
    ters = list(reversed([{"kaynak": "Medusa", "hedef": "M"}, {"kaynak": "Medusa Right", "hedef": "Edisyon"}]))
    assert servisler.ad_esle("Medusa Right ve Medusa", ters) == "Edisyon ve M"
    assert servisler.ad_esle("Medusa Right ve Medusa", list(reversed(ters))) == "Edisyon ve M"


@pytest.mark.parametrize("ek, hedef, beklenen", [
    ("da", "Coverz", "Coverz'de"), ("ya", "Coverz", "Coverz'e"), ("nın", "Edisyon", "Edisyon'un"),
    ("da", "Kitap", "Kitap'ta"), ("nın", "Kütüphane", "Kütüphane'nin"), ("ya", "Kütüphane", "Kütüphane'ye"),
    ("yı", "Arşiv", "Arşiv'i"), ("dan", "Edisyon uygulaması", "Edisyon uygulaması'ndan"), ("daki", "Kitap", "Kitap'taki"),
])
def test_ek_uyumu_hedefin_son_sesine_gore(ek, hedef, beklenen):
    assert servisler.ad_esle(f"Medusa'{ek}", [{"kaynak": "Medusa", "hedef": hedef}]) == beklenen


def test_turkce_i_duyarsiz():
    e = [{"kaynak": "İzmir Ofisi", "hedef": "Şube"}]
    for yazim in ("İzmir Ofisi", "izmir ofisi", "IZMIR OFISI", "ızmır ofısı", "İZMİR  OFİSİ"):
        assert servisler.ad_esle(f"{yazim} ziyaret edildi", e) == "Şube ziyaret edildi", yazim


def test_hedef_bossa_ad_temizce_silinir():
    e = [{"kaynak": "Medusa Right", "hedef": ""}, {"kaynak": "Medusa", "hedef": ""}]
    assert servisler.ad_esle("Medusa Right Geliştirme Önerileri", e) == "Geliştirme Önerileri"
    assert servisler.ad_esle("'Medusa Right' toplantısı yapıldı (Medusa)", e) == "toplantısı yapıldı"
    assert servisler.ad_esle("Rapor Medusa'ya , gönderildi", e) == "Rapor, gönderildi"
    assert servisler.ad_esle("• Medusa\n• İkinci  satır", e) == "•\n• İkinci satır"


# ---------------------------------------------------------------- ad eşlemeleri: ayarlar

def test_ayarlar_ad_eslemeleri_kaydet_dogrula_ve_kullanici_ayrimi():
    kullanici_olustur()
    kullanici_olustur("b@ornek.com", "Burak")
    c = istemci()
    r = c.put("/api/ayarlar", json={"ad_eslemeleri": "Medusa Right = Edisyon uygulaması\n\n  medusa   =\n"})
    assert r.status_code == 200
    assert r.json()["ad_eslemeleri"] == [{"kaynak": "Medusa Right", "hedef": "Edisyon uygulaması"},
                                         {"kaynak": "medusa", "hedef": ""}]
    for kotu in ("Medusa Edisyon", "M = x", "x" * 61 + " = y", "Medusa = a\nMEDUSA = b", "Medusa = Medusa Pro",
                 "Atlas = Yeni\nYeni = Başka", "Medusa = " + "y" * 81):
        assert c.put("/api/ayarlar", json={"ad_eslemeleri": kotu}).status_code == 422, kotu
    assert c.get("/api/ayarlar").json()["ad_eslemeleri"][0]["kaynak"] == "Medusa Right"  # 422 kaydı bozmadı
    assert c.put("/api/ayarlar", json={"ad_eslemeleri": ESLEME}).json()["ad_eslemeleri"] == ESLEME
    assert istemci("b@ornek.com").get("/api/ayarlar").json()["ad_eslemeleri"] == []
    assert c.put("/api/ayarlar", json={"ad_eslemeleri": ""}).json()["ad_eslemeleri"] == []


# ---------------------------------------------------------------- ad eşlemeleri: rapor metni ve çıktılar

def _medusali_gun(uid):
    """Elle madde, devam, sürekli, bulunan, kategori adı, başlık ve Yarın'da kaynak ad."""
    with OturumYapici() as db:
        a = db.get(KullaniciAyari, uid)
        a.rapor_basligi = "Medusa Günlüğü"
        a.ad_eslemeleri = ESLEME
        db.add(Kategori(user_id=uid, ad="Medusa Right Çalışmaları", sira=0, kaynaklar=["github", "medusa"]))
        db.commit()
    ekle(uid, "Medusa Right Geliştirme Önerileri hazırlandı")
    ekle(uid, "MEDUSA'da lisans ekranı denendi")
    ekle(uid, "Medusa lisans modülü", tur="devam", asama="Medusa'nın onayı bekleniyor")
    ekle(uid, "medusa kayıtları kontrol edildi | Medusa takibi yapıldı", tur="surekli")
    ekle(uid, "Medusa ile toplantı", kaynak="yarin", kaynak_id="yarin")


def test_elle_madde_kategori_basligi_ve_yarin_rapor_metninde_eslenir(saat):
    uid = kullanici_olustur()
    _medusali_gun(uid)
    c = istemci()
    d = c.get("/api/durum").json()
    metin = d["rapor_metni"]
    assert "medusa" not in metin.lower()
    assert "*Edisyon uygulaması Günlüğü – 16.09.2026*" in metin
    assert "• Edisyon uygulaması Geliştirme Önerileri hazırlandı" in metin
    assert "• Edisyon uygulaması'nda lisans ekranı denendi" in metin
    assert "Edisyon uygulaması lisans modülü — Edisyon uygulaması'nın onayı bekleniyor" in metin
    assert "• Edisyon uygulaması ile toplantı" in metin
    elle = next(m for m in d["maddeler"] if m["metin"].startswith("Medusa Right"))
    assert elle["metin"] == "Medusa Right Geliştirme Önerileri hazırlandı"  # ham metin korunur ("orijinali gör")
    assert elle["rapor_metni"] == "Edisyon uygulaması Geliştirme Önerileri hazırlandı"
    # kategorideki madde kategori başlığının altında
    kid = next(k["id"] for k in d["duzen"]["kategoriler"] if k["ad"] == "Medusa Right Çalışmaları")
    c.patch(f"/api/maddeler/{elle['id']}", json={"kategori_id": kid})
    metin = c.get("/api/durum").json()["rapor_metni"]
    assert "*Edisyon uygulaması Çalışmaları:*\n• Edisyon uygulaması Geliştirme Önerileri hazırlandı" in metin
    # elle yazılan (kullanıcının düzenlediği) metin de eşlenir
    c.patch(f"/api/maddeler/{elle['id']}", json={"metin": "Medusa Right sunumu yapıldı"})
    assert "Edisyon uygulaması sunumu yapıldı" in c.get("/api/durum").json()["rapor_metni"]


def test_esleme_yalniz_kendi_kullanicisina(saat):
    a = kullanici_olustur()
    b = kullanici_olustur("b@ornek.com", "Burak")
    _medusali_gun(a)
    ekle(b, "Medusa Right Geliştirme Önerileri hazırlandı")
    assert "Medusa Right Geliştirme" in istemci("b@ornek.com").get("/api/durum").json()["rapor_metni"]
    assert "medusa" not in istemci().get("/api/durum").json()["rapor_metni"].lower()


def test_otomatik_eposta_hatirlatma_push_ve_gecmiste_kaynak_ad_kalmaz(push, saat, monkeypatch):
    uid = kullanici_olustur(otomatik_gonder=True, patron_eposta=PATRON, patron_adi="Medusa Bey",
                            github_token_enc=guvenlik.sifrele("tok"), github_repo="org/repo")
    _medusali_gun(uid)
    zaman = datetime.combine(CARSAMBA, time(10, 0), servisler.ISTANBUL)
    monkeypatch.setattr(servisler, "github_tara", lambda *a, **k: [servisler.madde("medusa", "Medusa Right entegrasyonu düzeltildi", zaman)])
    with OturumYapici() as db:
        from veritabani import PushAbonelik
        db.add(PushAbonelik(user_id=uid, endpoint="https://push.ornek.com/1", p256dh="p", auth="a", cihaz_adi="iPhone"))
        db.commit()
    saat(CARSAMBA, 18, 15)  # 17:00 hatırlatması + 15 dk önce uyarı
    assert cron()[uid]["otomatik"].startswith("ön uyarı")
    saat(CARSAMBA, 18, 30)
    assert cron()[uid]["otomatik"] == "gönderildi"
    konular = [g["subject"] for g in SahteResend.istekler]
    assert any(k.startswith("Günlük rapor hatırlatması") for k in konular) and any(k.startswith("Günlük Rapor –") for k in konular)
    patrona = next(g for g in SahteResend.istekler if g["to"] == [PATRON])
    assert "Edisyon uygulaması entegrasyonu düzeltildi" in patrona["text"]
    assert "Edisyon uygulaması Çalışmaları:" in patrona["text"]
    ciktilar = json.dumps([SahteResend.istekler, push.cagrilar], ensure_ascii=False).lower()
    assert "medusa" not in ciktilar
    c = istemci()
    gecmis = c.get("/api/raporlar").json()["raporlar"]
    assert gecmis and "medusa" not in json.dumps(gecmis, ensure_ascii=False).lower()


def test_gecmis_kopyasi_eski_kayitta_da_eslenir():
    uid = kullanici_olustur(ad_eslemeleri=ESLEME)
    with OturumYapici() as db:
        db.add(Rapor(user_id=uid, tarih=CARSAMBA, tur="gunluk", metin="*Rapor*\n\n• Medusa'da test yapıldı"))
        db.commit()
    [r] = istemci().get("/api/raporlar").json()["raporlar"]
    assert r["metin"] == "*Rapor*\n\n• Edisyon uygulaması'nda test yapıldı"


def test_haftalik_ozet_girdi_ve_ciktisi_eslenir(sahte_claude):
    uid = kullanici_olustur(ad_eslemeleri=ESLEME)
    pazartesi = api.bugun() - timedelta(days=api.bugun().weekday())
    with OturumYapici() as db:
        db.add(Rapor(user_id=uid, tarih=pazartesi, tur="gunluk", metin="*Rapor*\n\n• Medusa Right sunumu yapıldı"))
        db.commit()
    sahte_claude.yanitlar = ["*Haftalık Özet*\n\n• Medusa'da sunum yapıldı"]
    r = istemci().post("/api/haftalik", json={"hafta_baslangic": pazartesi.isoformat()})
    assert r.status_code == 200
    assert r.json()["metin"] == "*Haftalık Özet*\n\n• Edisyon uygulaması'nda sunum yapıldı"
    istek = json.dumps(sahte_claude.istekler[0], ensure_ascii=False)
    assert "medusa" not in istek.lower() and "Edisyon uygulaması sunumu" in istek
    assert servisler.TIRNAK_KURALI in sahte_claude.istekler[0]["system"]


# ---------------------------------------------------------------- 4) tırnak içi koruma

def test_tirnak_parcalari():
    assert servisler.tirnak_parcalari("MSG'ye 'Ağustos raporu'nu ve 'Eylül' dosyası 'Köprü Film'den teklif' konulu") == [
        "Ağustos raporu", "Eylül", "Köprü Film'den teklif"]
    assert servisler.tirnak_parcalari("MESAM'a e-posta gönderildi") == []


def _duzelt_girdileri(istek):
    return json.loads(istek["messages"][0]["content"].split("Düzeltilecek maddeler:\n", 1)[1])


def test_claude_tirnak_icini_degistirirse_reddedilir_eslenmis_ham_kalir(sahte_claude, saat):
    uid = kullanici_olustur(ad_eslemeleri=ESLEME, proje_adi="Medusa")
    bozan = ekle(uid, "'Medusa Right v2 Kataloğu' dosyası gönderildi")
    koruyan = ekle(uid, "'Ağustos Raporu' medusa ekibine iletildi")

    def yanit(istek):
        g = {x["id"]: x["metin"] for x in _duzelt_girdileri(istek)}
        assert g[bozan] == "'Edisyon uygulaması v2 Kataloğu' dosyası gönderildi"  # Claude'a eşlenmiş gider
        return json.dumps([
            {"id": bozan, "metin": "'Edisyon Uygulaması V2 kataloğu' dosyası gönderildi."},  # tırnak içi değişti
            {"id": koruyan, "metin": "'Ağustos Raporu' Medusa ekibine iletildi."},  # tırnak aynen; çıktı eşlenir
        ], ensure_ascii=False)

    sahte_claude.yanitlar = [yanit]
    c = istemci()
    d = c.post("/api/duzelt").json()
    assert d["duzeltilen"] == 1
    assert madde(bozan).metin_ai is None
    assert madde(koruyan).metin_ai == "'Ağustos Raporu' Edisyon uygulaması ekibine iletildi."
    istek = sahte_claude.istekler[0]
    assert "medusa" not in json.dumps(istek, ensure_ascii=False).lower()  # proje adı dahil
    assert servisler.TIRNAK_KURALI in istek["system"]
    metin = c.get("/api/durum").json()["rapor_metni"]
    assert "• 'Edisyon uygulaması v2 Kataloğu' dosyası gönderildi" in metin  # ham (eşlenmiş) metin
    assert "• 'Ağustos Raporu' Edisyon uygulaması ekibine iletildi." in metin


def test_bulunan_cevirisinde_tirnak_bozulursa_eslenmis_ham(sahte_claude):
    maddeler = [servisler.madde("eposta", "MESAM'a 'Medusa Right Katalog' konulu e-posta gönderildi"),
                servisler.madde("eposta", "MSG'ye 'Eylül' konulu e-posta gönderildi")]
    a, b = maddeler[0]["id"], maddeler[1]["id"]
    sahte_claude.yanitlar = [json.dumps([
        {"id": a, "metin": "MESAM'a Edisyon uygulaması kataloğu hakkında e-posta gönderildi."},
        {"id": b, "metin": "MSG'ye 'Eylül' konulu e-posta gönderildi."},
    ], ensure_ascii=False)]
    sonuc, hata = servisler.claude_cevir(maddeler, "k", eslemeler=ESLEME)
    assert hata is None
    assert [m["metin"] for m in sonuc] == ["MESAM'a 'Edisyon uygulaması Katalog' konulu e-posta gönderildi",
                                            "MSG'ye 'Eylül' konulu e-posta gönderildi."]
    assert "Medusa" not in sahte_claude.istekler[0]["messages"][0]["content"]


def test_sesli_bolmede_tirnak_bozulursa_basit_bolme(sahte_claude):
    kullanici_olustur(ad_eslemeleri=ESLEME)
    sahte_claude.yanitlar = [json.dumps([{"metin": "Katalog dosyası Medusa'ya yüklendi.", "kategori_id": None, "tur": "bugun"}])]
    d = istemci().post("/api/sesli-not", json={"metin": "'Katalog 2026' dosyası medusaya yüklendi"}).json()
    assert d["hatalar"] and "tırnak" in d["hatalar"][0]
    assert [m["metin"] for m in d["maddeler"]] == ["'Katalog 2026' dosyası medusaya yüklendi"]
    sahte_claude.yanitlar = [json.dumps([{"metin": "'Katalog 2026' dosyası Medusa'ya yüklendi.", "kategori_id": None, "tur": "bugun"}])]
    d = istemci().post("/api/sesli-not", json={"metin": "'Katalog 2026' dosyası medusaya yüklendi"}).json()
    assert d["hatalar"] == [] and d["maddeler"][0]["metin"] == "'Katalog 2026' dosyası Edisyon uygulaması'na yüklendi."


def test_tirnak_kurali_tum_promptlarda():
    for sistem in (servisler.claude_sistem(), servisler.duzelt_sistemi(), servisler.sesli_not_sistemi(),
                   servisler.haftalik_sistemi()):
        assert servisler.TIRNAK_KURALI in sistem


def test_sema_guncelle_ad_eslemeleri_kolonu(tmp_path):
    from sqlalchemy import create_engine, inspect

    import veritabani
    eski = create_engine(f"sqlite:///{tmp_path}/eski.db")
    Temel.metadata.create_all(eski)
    with eski.begin() as b:
        b.exec_driver_sql("ALTER TABLE user_settings DROP COLUMN ad_eslemeleri")
    assert veritabani.sema_guncelle(eski) == ["user_settings.ad_eslemeleri"]
    assert veritabani.sema_guncelle(eski) == []
    assert "ad_eslemeleri" in {k["name"] for k in inspect(eski).get_columns("user_settings")}
