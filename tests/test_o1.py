"""O1: unutursan otomatik e-posta teslimi — ön uyarı, gönderim, iptal, uçlar. Resend, push ve Claude sahte; saat sabit."""
import json
from datetime import date, datetime, time

import httpx
import pytest
from fastapi.testclient import TestClient
from pywebpush import WebPushException
from sqlalchemy import select

import api
import app as uygulama
import guvenlik
import servisler
from veritabani import (
    ClaudeKullanim, HatirlatmaGonderimi, Kullanici, KullaniciAyari, Madde, OturumYapici, PushAbonelik, Rapor, Temel, motor,
)

SIFRE = "dogru-sifre-123"
TOKEN = "test-cron-token"
CARSAMBA = date(2026, 9, 16)
CUMARTESI = date(2026, 9, 19)
PATRON = "patron@firma.com"
ALT_SATIR = "Bu rapor Günlük Rapor ile gönderildi."


class SahteYanit:
    def __init__(self, kod):
        self.status_code = kod


class SahtePush:
    """servisler.webpush yerine: çağrıları kaydeder; endpoint → HTTP kodu verilirse WebPushException fırlatır."""

    def __init__(self):
        self.cagrilar: list[dict] = []
        self.hatalar: dict[str, int] = {}

    def __call__(self, subscription_info, data, **kw):
        self.cagrilar.append({"endpoint": subscription_info["endpoint"], "veri": json.loads(data)})
        kod = self.hatalar.get(subscription_info["endpoint"])
        if kod:
            raise WebPushException(f"Push failed: {kod}", response=SahteYanit(kod))


class SahteResend:
    """servisler.httpx.Client yerine: Resend isteklerini kaydeder, ayarlanan yanıtı döner."""

    istekler: list = []
    kod: int = 200

    def __init__(self, *a, **kw):
        pass

    def post(self, url, json=None, headers=None):
        SahteResend.istekler.append(json)
        govde = {"id": "re_1"} if SahteResend.kod < 300 else {"message": "Domain not verified"}
        return httpx.Response(SahteResend.kod, json=govde)


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
    SahteResend.istekler, SahteResend.kod = [], 200
    monkeypatch.setenv("RESEND_API_KEY", "re_test_anahtar")
    monkeypatch.setenv("EPOSTA_GONDEREN", "rapor@medusarights.com")
    monkeypatch.setattr(servisler.httpx, "Client", SahteResend)
    yield


@pytest.fixture
def push(monkeypatch):
    sahte = SahtePush()
    monkeypatch.setattr(servisler, "webpush", sahte)
    return sahte


@pytest.fixture
def saat(monkeypatch):
    """İstanbul saati ve 'bugün' birlikte sabitlenir (cron istanbul_simdi'yi, uçlar bugun()'ü okur)."""
    def ayarla(gun: date, ss: int, dd: int):
        monkeypatch.setattr(api, "istanbul_simdi", lambda: datetime.combine(gun, time(ss, dd), servisler.ISTANBUL))
        monkeypatch.setattr(api, "bugun", lambda: gun)
    ayarla(CARSAMBA, 12, 0)
    return ayarla


def kullanici_olustur(eposta="a@ornek.com", ad="Ayşe Yılmaz", otomatik=True, patron=PATRON, patron_adi="Ahmet Bey",
                      **ayar) -> int:
    """Olağan 17:00 hatırlatması 23:00'e alınır ki yalnız otomatik gönderim görünsün."""
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad=ad, sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol="uye", aktif=True,
                      sifre_degistirmeli=False)
        db.add(k)
        db.commit()
        ayar = {"hatirlatma_saat": time(23, 0), "otomatik_gonder": otomatik, "patron_eposta": patron,
                "patron_adi": patron_adi, **ayar}
        db.add(KullaniciAyari(user_id=k.id, kurulum_tamam=True, **ayar))
        db.commit()
        return k.id


def madde_ekle(uid, metin, tur="bugun", tikli=True, gun=CARSAMBA) -> int:
    with OturumYapici() as db:
        m = Madde(user_id=uid, tur=tur, metin=metin, tikli=tikli,
                  **({"tarih": gun, "kaynak": "elle"} if tur == "bugun" else {}))
        db.add(m)
        db.commit()
        return m.id


def abonelik_ekle(uid, endpoint="https://push.ornek.com/1"):
    with OturumYapici() as db:
        db.add(PushAbonelik(user_id=uid, endpoint=endpoint, p256dh="p", auth="a", cihaz_adi="iPhone Safari"))
        db.commit()


def hazir_kullanici(**kw) -> int:
    uid = kullanici_olustur(**kw)
    madde_ekle(uid, "Teklif dosyası hazırlandı")
    madde_ekle(uid, "Günlük yedekler kontrol edildi", tur="surekli")
    abonelik_ekle(uid, f"https://push.ornek.com/{uid}")
    return uid


def istemci(eposta="a@ornek.com") -> TestClient:
    c = TestClient(uygulama.app, follow_redirects=False)
    c.__enter__()
    assert c.post("/giris", data={"eposta": eposta, "sifre": SIFRE}).status_code == 303
    return c


def cron() -> dict:
    r = TestClient(uygulama.app).post(f"/api/hatirlat?token={TOKEN}")
    assert r.status_code == 200
    return {x["user_id"]: x for x in r.json()["kullanicilar"]}


def patrona_gidenler() -> list[dict]:
    return [g for g in SahteResend.istekler if g["to"] == [PATRON]]


def kayitlar(uid) -> dict[str, tuple[str, str | None]]:
    with OturumYapici() as db:
        return {g.kanal: (g.durum, g.hata_metni) for g in db.scalars(select(HatirlatmaGonderimi).where(
            HatirlatmaGonderimi.user_id == uid))}


def rapor(uid, gun=CARSAMBA) -> Rapor | None:
    with OturumYapici() as db:
        return db.scalar(select(Rapor).where(Rapor.user_id == uid, Rapor.tarih == gun, Rapor.tur == "gunluk"))


# ---------------------------------------------------------------- zamanlama

def test_1814_hicbir_sey_1815_on_uyari_bir_kez(push, saat):
    uid = hazir_kullanici()
    saat(CARSAMBA, 18, 14)
    assert cron()[uid]["otomatik"] == "saat 18:15 olmadı"
    assert push.cagrilar == [] and SahteResend.istekler == [] and kayitlar(uid) == {}

    saat(CARSAMBA, 18, 15)
    assert cron()[uid]["otomatik"] == "ön uyarı: push 1/1, e-posta gönderildi"
    assert [c["veri"] for c in push.cagrilar] == [{
        "baslik": "Günlük Rapor", "govde": "Raporun 18:30'da Ahmet Bey'e e-postayla gidecek.",
        "url": "https://rapor.ornek.com/?otomatik=uyari",
    }]
    [eposta] = SahteResend.istekler
    assert eposta["to"] == ["a@ornek.com"] and eposta["subject"] == "Raporun 18:30'da Ahmet Bey'e e-postayla gidecek – 16.09.2026"
    assert "https://rapor.ornek.com/?otomatik=uyari" in eposta["text"] and "cc" not in eposta
    assert kayitlar(uid) == {"otomatik_uyari": ("gonderildi", None)}

    saat(CARSAMBA, 18, 20)
    assert cron()[uid]["otomatik"] == "ön uyarı gönderildi"
    assert len(push.cagrilar) == 1 and len(SahteResend.istekler) == 1


def test_on_uyari_patron_adi_yoksa_patrona_eposta_hatirlatmasi_kapaliysa_yalniz_push(push, saat):
    uid = hazir_kullanici(patron_adi=None, hatirlatma_eposta=False, otomatik_saat=time(19, 0))
    saat(CARSAMBA, 18, 45)
    assert cron()[uid]["otomatik"] == "ön uyarı: push 1/1"
    assert push.cagrilar[0]["veri"]["govde"] == "Raporun 19:00'da patrona e-postayla gidecek."
    assert SahteResend.istekler == []


def test_1830_gonderim_bir_kez_ikinci_ping_tekrar_gondermez(push, saat):
    uid = hazir_kullanici()
    saat(CARSAMBA, 18, 15)
    cron()
    push.cagrilar.clear()
    saat(CARSAMBA, 18, 30)
    assert cron()[uid]["otomatik"] == "gönderildi"
    [g] = patrona_gidenler()
    assert g["from"] == 'Ayşe Yılmaz - Günlük Rapor <rapor@medusarights.com>'  # O1-ek2: tırnaksız
    assert g["subject"] == "Günlük Rapor – Ayşe Yılmaz – 16.09.2026"
    assert g["cc"] == ["a@ornek.com"] and g["reply_to"] == "a@ornek.com"
    assert push.cagrilar == []  # başarıda push yok
    assert kayitlar(uid)["otomatik"] == ("gonderildi", None)

    saat(CARSAMBA, 18, 35)
    assert cron()[uid]["otomatik"] == "bugünün raporu kopyalanmış"  # gönderim raporu kaydetti
    saat(CARSAMBA, 22, 0)
    cron()
    assert len(patrona_gidenler()) == 1


def test_ping_kacarsa_uyari_atlanir_dogrudan_gonderilir(push, saat):
    uid = hazir_kullanici()
    saat(CARSAMBA, 18, 42)
    assert cron()[uid]["otomatik"] == "gönderildi"
    assert push.cagrilar == [] and "otomatik_uyari" not in kayitlar(uid)
    assert len(patrona_gidenler()) == 1


def test_kopya_bana_kapaliysa_cc_yok(push, saat):
    hazir_kullanici(otomatik_kopya_bana=False)
    saat(CARSAMBA, 18, 30)
    cron()
    [g] = patrona_gidenler()
    assert "cc" not in g and g["reply_to"] == "a@ornek.com"


def test_rapor_kopyalanmissa_hicbiri(push, saat):
    uid = hazir_kullanici()
    c = istemci()
    assert c.post("/api/raporlar", json={"metin": "*Rapor*\n• elle", "tur": "gunluk"}).status_code == 200
    for ss, dd in ((18, 15), (18, 30), (19, 0)):
        saat(CARSAMBA, ss, dd)
        assert cron()[uid]["otomatik"] == "bugünün raporu kopyalanmış"
    assert push.cagrilar == [] and SahteResend.istekler == [] and kayitlar(uid) == {}
    assert rapor(uid).gonderim == "elle"


def test_iptal_sonrasi_uyari_ve_gonderim_yok(push, saat):
    uid = hazir_kullanici()
    c = istemci()
    r = c.post("/api/otomatik/iptal", json={"tarih": CARSAMBA.isoformat()})
    assert r.status_code == 200 and r.json()["otomatik"]["durum"] == "iptal"
    assert c.post("/api/otomatik/iptal").status_code == 200  # ikinci kez: aynı sonuç
    for ss, dd in ((18, 15), (18, 30)):
        saat(CARSAMBA, ss, dd)
        assert cron()[uid]["otomatik"] == "bugün iptal edildi"
    assert push.cagrilar == [] and SahteResend.istekler == []
    assert kayitlar(uid) == {"otomatik": ("iptal", None)}
    assert rapor(uid) is None
    # yalnız bugün iptal edilebilir
    assert c.post("/api/otomatik/iptal", json={"tarih": "2026-09-15"}).status_code == 422


def test_sifir_tikli_madde_gonderim_ve_uyari_yok(push, saat):
    uid = kullanici_olustur()
    abonelik_ekle(uid)
    madde_ekle(uid, "tiksiz iş", tikli=False)
    madde_ekle(uid, "tiksiz sürekli", tur="surekli", tikli=False)
    for ss, dd in ((18, 15), (18, 30)):
        saat(CARSAMBA, ss, dd)
        assert cron()[uid]["otomatik"] == "gönderilecek tikli madde yok"
    assert push.cagrilar == [] and SahteResend.istekler == [] and kayitlar(uid) == {}


def test_hafta_sonu_yok(push, saat):
    uid = hazir_kullanici()
    madde_ekle(uid, "cumartesi işi", gun=CUMARTESI)
    for ss, dd in ((18, 15), (18, 30)):
        saat(CUMARTESI, ss, dd)
        assert cron()[uid]["otomatik"] == "bugün gönderim günü değil"
    assert push.cagrilar == [] and SahteResend.istekler == []


def test_otomatik_kapaliysa_sonuc_satirinda_anahtar_yok(push, saat):
    uid = hazir_kullanici(otomatik=False)
    saat(CARSAMBA, 18, 30)
    assert "otomatik" not in cron()[uid]
    assert SahteResend.istekler == []


# ---------------------------------------------------------------- gövde, Claude, kota, hata

@pytest.mark.parametrize("bicim", ["kategorili", "duz"])
def test_govde_kopyala_metniyle_ayni_kalin_isaretsiz(push, saat, bicim):
    uid = hazir_kullanici(rapor_bicimi=bicim, rapor_basligi="İLS Günlük")
    madde_ekle(uid, "Sözleşme taslağı", tur="devam")
    c = istemci()
    saat(CARSAMBA, 18, 29)
    kopya = c.get("/api/durum").json()["rapor_metni"]
    assert kopya.startswith("*İLS Günlük – 16.09.2026*") and "*" in kopya
    saat(CARSAMBA, 18, 30)
    cron()
    [g] = patrona_gidenler()
    assert g["text"] == servisler.kalin_isaretsiz(kopya) + "\n\n" + ALT_SATIR
    assert "*" not in g["text"] and g["text"].startswith("İLS Günlük – 16.09.2026\n")
    assert rapor(uid).metin == kopya  # geçmişe Kopyala'nın metni (kalın işaretli) yazılır


def test_claude_ile_duzeltilip_gider(push, saat, sahte_claude):
    uid = hazir_kullanici()
    sahte_claude.yanitlar = [lambda istek: json.dumps([
        {"id": g["id"], "metin": "D:" + g["metin"]}
        for g in json.loads(istek["messages"][0]["content"].split("Düzeltilecek maddeler:\n", 1)[1])])]
    saat(CARSAMBA, 18, 30)
    cron()
    assert len(sahte_claude.istekler) == 1
    [g] = patrona_gidenler()
    assert "• D:Teklif dosyası hazırlandı" in g["text"] and "• D:Günlük yedekler kontrol edildi" in g["text"]
    with OturumYapici() as db:
        assert db.scalar(select(ClaudeKullanim.cagri).where(ClaudeKullanim.user_id == uid)) == 1


def test_kota_doluyken_ham_metinle_gider(push, saat, sahte_claude):
    uid = hazir_kullanici()
    with OturumYapici() as db:
        db.add(ClaudeKullanim(user_id=uid, tarih=CARSAMBA, cagri=api.GUNLUK_CLAUDE_SINIRI, girdi_token=0, cikti_token=0))
        db.commit()
    saat(CARSAMBA, 18, 30)
    assert cron()[uid]["otomatik"] == "gönderildi"
    assert sahte_claude.istekler == []
    [g] = patrona_gidenler()
    assert "• Teklif dosyası hazırlandı" in g["text"] and "• Günlük yedekler kontrol edildi" in g["text"]


def test_claude_hatasinda_ham_metinle_gider(push, saat, sahte_claude):
    hazir_kullanici()
    sahte_claude.yanitlar = [servisler.ClaudeHatasi("API hatası (500)")]
    saat(CARSAMBA, 18, 30)
    cron()
    [g] = patrona_gidenler()
    assert "• Teklif dosyası hazırlandı" in g["text"]


def test_resend_hatasi_push_uyarisi_hata_metni_tekrar_denemez(push, saat):
    uid = hazir_kullanici()
    SahteResend.kod = 403
    saat(CARSAMBA, 18, 30)
    assert cron()[uid]["otomatik"] == "hata: Resend 403: Domain not verified"
    assert [c["veri"] for c in push.cagrilar] == [{
        "baslik": "Günlük Rapor", "govde": "Otomatik gönderim başarısız: Resend 403: Domain not verified — elle gönder",
        "url": "https://rapor.ornek.com",
    }]
    assert kayitlar(uid)["otomatik"] == ("hata", "Resend 403: Domain not verified")
    assert rapor(uid) is None

    SahteResend.kod = 200
    for dd in (35, 40):
        saat(CARSAMBA, 18, dd)
        assert cron()[uid]["otomatik"] == "bugün denendi, hata verdi"
    assert len(patrona_gidenler()) == 1 and len(push.cagrilar) == 1
    d = istemci().get("/api/durum").json()
    assert d["otomatik"]["durum"] == "hata" and d["otomatik"]["hata"] == "Resend 403: Domain not verified"


def test_gonderim_raporu_otomatik_isaretler_durum_ve_gecmis(push, saat):
    uid = hazir_kullanici()
    c = istemci()
    d = c.get("/api/durum").json()
    assert d["gonderim"] is None and d["son_kopya"] is None
    assert d["otomatik"] == {"acik": True, "saat": "18:30", "patron_adi": "Ahmet Bey", "hedef": "Ahmet Bey'e",
                             "gun": True, "durum": None, "hata": ""}
    saat(CARSAMBA, 18, 30)
    cron()
    assert rapor(uid).gonderim == "otomatik"
    d = c.get("/api/durum").json()
    assert d["gonderim"] == "otomatik" and d["son_kopya"] and d["otomatik"]["durum"] == "gonderildi"
    [r] = c.get("/api/raporlar").json()["raporlar"]
    assert r["gonderim"] == "otomatik" and r["tarih"] == CARSAMBA.isoformat()
    # geçmiş günde otomatik özeti yok
    assert c.get("/api/durum?tarih=2026-09-15").json()["otomatik"] is None
    # sonradan elle kopyalanırsa işaret korunur
    c.post("/api/raporlar", json={"metin": "elle", "tur": "gunluk"})
    assert rapor(uid).gonderim == "otomatik"


def test_elle_kopyalanan_rapor_elle_isaretli(saat):
    kullanici_olustur()
    c = istemci()
    assert c.post("/api/raporlar", json={"metin": "x", "tur": "gunluk"}).json()["gonderim"] == "elle"
    assert c.get("/api/durum").json()["gonderim"] == "elle"


# ---------------------------------------------------------------- ayarlar

def test_ayar_dogrulamasi(saat):
    kullanici_olustur(otomatik=False, patron=None, patron_adi=None)
    c = istemci()
    a = c.get("/api/ayarlar").json()
    assert (a["otomatik_gonder"], a["otomatik_saat"], a["patron_eposta"], a["patron_adi"], a["otomatik_kopya_bana"]) == (
        False, "18:30", "", "", True)

    r = c.put("/api/ayarlar", json={"otomatik_gonder": True})
    assert r.status_code == 422 and r.json()["detail"] == "Patron e-postası gerekli"
    assert c.get("/api/ayarlar").json()["otomatik_gonder"] is False  # kaydedilmedi
    r = c.put("/api/ayarlar", json={"otomatik_gonder": True, "patron_eposta": "  "})
    assert r.status_code == 422 and r.json()["detail"] == "Patron e-postası gerekli"
    r = c.put("/api/ayarlar", json={"patron_eposta": "patron-firma"})
    assert r.status_code == 422 and r.json()["detail"] == "Patron e-posta adresi geçerli değil"
    r = c.put("/api/ayarlar", json={"otomatik_saat": "25:00"})
    assert r.status_code == 422 and "SS:DD" in r.json()["detail"]

    r = c.put("/api/ayarlar", json={"otomatik_gonder": True, "patron_eposta": " Patron@Firma.com ", "patron_adi": " Ahmet  Bey ",
                                    "otomatik_saat": "19:05", "otomatik_kopya_bana": False})
    assert r.status_code == 200
    a = r.json()
    assert (a["otomatik_gonder"], a["otomatik_saat"], a["patron_eposta"], a["patron_adi"], a["otomatik_kopya_bana"]) == (
        True, "19:05", "patron@firma.com", "Ahmet Bey", False)
    # açıkken patron e-postası silinemez; kapatınca silinebilir
    assert c.put("/api/ayarlar", json={"patron_eposta": ""}).status_code == 422
    assert c.put("/api/ayarlar", json={"otomatik_gonder": False, "patron_eposta": ""}).json()["patron_eposta"] == ""
    # başka alanlar kaydedilirken otomatik ayarlara dokunulmaz
    c.put("/api/ayarlar", json={"otomatik_gonder": True, "patron_eposta": PATRON})
    assert c.put("/api/ayarlar", json={"rapor_basligi": "X"}).json()["otomatik_gonder"] is True


# ---------------------------------------------------------------- uçlar

def test_simdi_hemen_gonderir_kilit_ayni(push, saat):
    uid = hazir_kullanici()
    c = istemci()
    saat(CARSAMBA, 10, 0)
    r = c.post("/api/otomatik/simdi")
    assert r.status_code == 200, r.text
    assert r.json()["rapor"]["gonderim"] == "otomatik" and r.json()["otomatik"]["durum"] == "gonderildi"
    [g] = patrona_gidenler()
    assert g["cc"] == ["a@ornek.com"] and g["subject"] == "Günlük Rapor – Ayşe Yılmaz – 16.09.2026"
    assert c.post("/api/otomatik/simdi").status_code == 409
    for ss, dd in ((18, 15), (18, 30)):
        saat(CARSAMBA, ss, dd)
        cron()
    assert len(patrona_gidenler()) == 1 and push.cagrilar == []
    assert c.post("/api/otomatik/iptal").status_code == 409  # gönderilmiş gün iptal edilemez


def test_simdi_iptal_ya_da_hata_sonrasi_yeniden_gonderir(push, saat):
    hazir_kullanici()
    c = istemci()
    c.post("/api/otomatik/iptal")
    SahteResend.kod = 500
    r = c.post("/api/otomatik/simdi")
    assert r.status_code == 502 and r.json()["detail"].startswith("Gönderilemedi: Resend 500")
    assert push.cagrilar == []  # kullanıcı eyleminde hata ekranda gösterilir, push atılmaz
    SahteResend.kod = 200
    assert c.post("/api/otomatik/simdi").status_code == 200
    assert len(patrona_gidenler()) == 2


def test_simdi_kosullari(saat):
    uid = kullanici_olustur(otomatik=False, patron=None)
    c = istemci()
    madde_ekle(uid, "iş")
    assert c.post("/api/otomatik/simdi").json()["detail"] == "Patron e-postası gerekli"
    c.put("/api/ayarlar", json={"patron_eposta": PATRON})
    with OturumYapici() as db:
        for m in db.scalars(select(Madde)):
            m.tikli = False
        db.commit()
    r = c.post("/api/otomatik/simdi")
    assert r.status_code == 422 and r.json()["detail"] == "Gönderilecek tikli madde yok"
    c.post("/api/raporlar", json={"metin": "x", "tur": "gunluk"})
    assert c.post("/api/otomatik/simdi").status_code == 409
    assert SahteResend.istekler == []


def test_test_ucu_yalniz_kullaniciya_ayni_bicimde(push, saat, sahte_claude):
    uid = hazir_kullanici()
    c = istemci()
    kopya = c.get("/api/durum").json()["rapor_metni"]
    r = c.post("/api/otomatik/test")
    assert r.json() == {"ok": True, "adres": "a@ornek.com"}
    [g] = SahteResend.istekler
    assert g["to"] == ["a@ornek.com"] and "cc" not in g and g["reply_to"] == "a@ornek.com"
    assert g["subject"] == "[TEST] Günlük Rapor – Ayşe Yılmaz – 16.09.2026"
    assert g["text"] == servisler.kalin_isaretsiz(kopya) + "\n\n" + ALT_SATIR
    assert sahte_claude.istekler == []  # test kota harcamaz
    assert rapor(uid) is None and kayitlar(uid) == {}

    SahteResend.kod = 500
    r = c.post("/api/otomatik/test").json()
    assert r["ok"] is False and r["neden"].startswith("Resend 500")


def test_uclar_oturum_ister():
    c = TestClient(uygulama.app, follow_redirects=False)
    for yol in ("/api/otomatik/iptal", "/api/otomatik/simdi", "/api/otomatik/test"):
        assert c.post(yol).status_code == 401


def test_kullanici_ayrimi(push, saat):
    a = hazir_kullanici(eposta="a@ornek.com")
    b = hazir_kullanici(eposta="b@ornek.com", ad="Burak", patron="b-patron@firma.com")
    k = hazir_kullanici(eposta="k@ornek.com", otomatik=False)
    istemci("b@ornek.com").post("/api/otomatik/iptal")
    assert istemci("a@ornek.com").get("/api/durum").json()["otomatik"]["durum"] is None

    saat(CARSAMBA, 18, 15)
    sonuc = cron()
    assert sonuc[a]["otomatik"].startswith("ön uyarı:") and sonuc[b]["otomatik"] == "bugün iptal edildi"
    assert "otomatik" not in sonuc[k]
    assert [c["endpoint"] for c in push.cagrilar] == [f"https://push.ornek.com/{a}"]
    saat(CARSAMBA, 18, 30)
    cron()
    gidenler = [g for g in SahteResend.istekler if "cc" in g or g["to"] != ["a@ornek.com"]]
    assert [(g["to"], g["cc"]) for g in gidenler] == [([PATRON], ["a@ornek.com"])]
    assert rapor(a).gonderim == "otomatik" and rapor(b) is None and rapor(k) is None
    assert istemci("b@ornek.com").get("/api/durum").json()["otomatik"]["durum"] == "iptal"


# ---------------------------------------------------------------- yardımcılar

def test_kalin_isaretsiz_ve_saatte_eki():
    assert servisler.kalin_isaretsiz("*Başlık – 16.09.2026*\n\n*Genel İşler:*\n• a*b ve c*d\n• *önemli* iş\n• 2 * 3") == (
        "Başlık – 16.09.2026\n\nGenel İşler:\n• a*b ve c*d\n• önemli iş\n• 2 * 3")
    assert [servisler.saatte_eki(s) for s in ("18:30", "17:00", "18:15", "18:40", "09:12", "20:00")] == [
        "18:30'da", "17:00'de", "18:15'te", "18:40'ta", "09:12'de", "20:00'de"]


def test_smtp_yedeginde_cc_basligi(monkeypatch):
    gonderilen = []

    class SahteSmtp:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def send_message(self, mesaj): gonderilen.append(mesaj)

    monkeypatch.setenv("RESEND_API_KEY", "")
    monkeypatch.setattr(servisler.smtplib, "SMTP_SSL", SahteSmtp)
    assert servisler.eposta_gonder(PATRON, "Konu", "gövde", yanit_adresi="a@ornek.com", gmail_kullanici="a@gmail.com",
                                   gmail_sifre="x", kopya="a@ornek.com") is None
    assert gonderilen[0]["Cc"] == "a@ornek.com" and gonderilen[0]["To"] == PATRON
