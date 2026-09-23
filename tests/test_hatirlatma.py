"""R3: PWA, web push abonelikleri, 17:00 hatırlatması. webpush ve SMTP sahte; saat sabitlenir; ağa çıkılmaz."""
import json
import smtplib
from datetime import date, datetime, time

import httpx
import pytest
from fastapi.testclient import TestClient
from pywebpush import WebPushException
from sqlalchemy import create_engine, func, select, text

import api
import app as uygulama
import guvenlik
import servisler
import veritabani
from veritabani import HatirlatmaGonderimi, Kullanici, KullaniciAyari, Madde, OturumYapici, PushAbonelik, Rapor, Temel, motor

SIFRE = "dogru-sifre-123"
TOKEN = "test-cron-token"
CARSAMBA = date(2026, 9, 16)
CUMARTESI = date(2026, 9, 19)


class SahteYanit:
    def __init__(self, kod):
        self.status_code = kod


class SahtePush:
    """servisler.webpush yerine: çağrıları kaydeder; endpoint → HTTP kodu verilirse WebPushException fırlatır."""

    def __init__(self):
        self.cagrilar: list[dict] = []
        self.hatalar: dict[str, int] = {}

    def __call__(self, subscription_info, data, **kw):
        self.cagrilar.append({"endpoint": subscription_info["endpoint"], "data": data, **kw})
        kod = self.hatalar.get(subscription_info["endpoint"])
        if kod:
            raise WebPushException(f"Push failed: {kod}", response=SahteYanit(kod))


class SahteSmtp:
    gonderilen: list = []
    hata: Exception | None = None

    def __init__(self, sunucu, port, timeout=None):
        assert (sunucu, port) == ("smtp.gmail.com", 465)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, kullanici, sifre):
        if SahteSmtp.hata:
            raise SahteSmtp.hata

    def send_message(self, mesaj):
        SahteSmtp.gonderilen.append(mesaj)


class SahteResend:
    """servisler.httpx.Client yerine geçer: Resend isteklerini kaydeder, ayarlanan yanıtı döner."""

    istekler: list = []
    kod: int = 200
    govde: dict | None = None
    hata: Exception | None = None

    def __init__(self, *a, **kw):
        pass

    def post(self, url, json=None, headers=None):
        SahteResend.istekler.append({"url": url, "govde": json, "headers": headers})
        if SahteResend.hata:
            raise SahteResend.hata
        return httpx.Response(SahteResend.kod, json=SahteResend.govde if SahteResend.govde is not None else {"id": "re_1"})


@pytest.fixture
def resend(monkeypatch):
    SahteResend.istekler, SahteResend.kod, SahteResend.govde, SahteResend.hata = [], 200, None, None
    monkeypatch.setenv("RESEND_API_KEY", "re_test_anahtar")
    monkeypatch.setenv("EPOSTA_GONDEREN", "rapor@medusarights.com")
    monkeypatch.setattr(servisler.httpx, "Client", SahteResend)
    return SahteResend


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
    monkeypatch.setenv("VAPID_CLAIM_EMAIL", "mailto:test@ornek.com")
    monkeypatch.setenv("APP_URL", "https://rapor.ornek.com")
    monkeypatch.setenv("CRON_TOKEN", TOKEN)
    monkeypatch.setattr(api, "son_hatirlat_ping", None)
    SahteSmtp.gonderilen, SahteSmtp.hata = [], None
    monkeypatch.setattr(servisler.smtplib, "SMTP_SSL", SahteSmtp)
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
    ayarla(CARSAMBA, 17, 0)
    return ayarla


def kullanici_olustur(eposta="a@ornek.com", ad="A", gmail=True, rol="uye", **ayar) -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad=ad, sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol=rol, aktif=True, sifre_degistirmeli=False)
        db.add(k)
        db.commit()
        a = KullaniciAyari(user_id=k.id, kurulum_tamam=True, **ayar)
        if gmail:
            a.gmail_kullanici, a.gmail_sifre_enc = eposta.replace("@ornek.com", "@gmail.com"), guvenlik.sifrele("uyg-sifre")
        db.add(a)
        db.commit()
        return k.id


def abonelik_ekle(uid: int, endpoint="https://push.ornek.com/1") -> int:
    with OturumYapici() as db:
        a = PushAbonelik(user_id=uid, endpoint=endpoint, p256dh="p", auth="a", cihaz_adi="iPhone Safari")
        db.add(a)
        db.commit()
        return a.id


def istemci(eposta="a@ornek.com") -> TestClient:
    c = TestClient(uygulama.app, follow_redirects=False)
    c.__enter__()
    assert c.post("/giris", data={"eposta": eposta, "sifre": SIFRE}).status_code == 303
    return c


def cron(c=None, **kw):
    c = c or TestClient(uygulama.app)
    return c.post(f"/api/hatirlat?token={TOKEN}", **kw)


def satir(yanit, uid) -> dict:
    return next(x for x in yanit.json()["kullanicilar"] if x["user_id"] == uid)


# ---------------------------------------------------------------- token

def test_token_yok_yanlis_401_dogru_200(push, saat, monkeypatch):
    c = TestClient(uygulama.app)
    assert c.get("/api/saglik").json()["son_hatirlat_ping"] is None
    assert c.post("/api/hatirlat").status_code == 401
    assert c.post("/api/hatirlat?token=yanlis").status_code == 401
    assert c.post("/api/hatirlat", headers={"Authorization": "Bearer yanlis"}).status_code == 401
    assert c.get("/api/saglik").json()["son_hatirlat_ping"] is None
    assert c.post(f"/api/hatirlat?token={TOKEN}").status_code == 200
    assert c.post("/api/hatirlat", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert c.get("/api/saglik").json()["son_hatirlat_ping"].startswith("2026-09-16T17:00")
    monkeypatch.setenv("CRON_TOKEN", "")
    assert c.post("/api/hatirlat?token=").status_code == 401
    assert c.post("/api/hatirlat", headers={"Authorization": "Bearer "}).status_code == 401


# ---------------------------------------------------------------- zamanlama

def test_1659_gonderim_yok_1700_var(push, saat):
    uid = kullanici_olustur()
    abonelik_ekle(uid)
    saat(CARSAMBA, 16, 59)
    r = cron()
    assert satir(r, uid)["push"] == "atlandı" and "17:00" in satir(r, uid)["neden"]
    assert push.cagrilar == [] and SahteSmtp.gonderilen == []

    saat(CARSAMBA, 17, 0)
    r = cron()
    assert satir(r, uid) == {"user_id": uid, "push": "1/1", "eposta": "gönderildi", "neden": ""}
    assert len(push.cagrilar) == 1 and len(SahteSmtp.gonderilen) == 1


def test_hafta_sonu_varsayilan_gonderim_yok(push, saat):
    uid = kullanici_olustur()
    abonelik_ekle(uid)
    saat(CUMARTESI, 18, 0)
    r = cron()
    assert satir(r, uid)["neden"] == "bugün hatırlatma günü değil"
    assert push.cagrilar == [] and SahteSmtp.gonderilen == []


def test_ikinci_ping_ayni_gun_tekrar_gondermez(push, saat):
    uid = kullanici_olustur()
    abonelik_ekle(uid)
    cron()
    saat(CARSAMBA, 17, 4)
    r = cron()
    assert satir(r, uid)["push"] == "atlandı" and satir(r, uid)["eposta"] == "atlandı"
    assert "push bugün gönderildi" in satir(r, uid)["neden"]
    assert len(push.cagrilar) == 1 and len(SahteSmtp.gonderilen) == 1
    with OturumYapici() as db:
        assert sorted(db.scalars(select(HatirlatmaGonderimi.kanal))) == ["eposta", "push"]


def test_bugun_rapor_varsa_gondermez(push, saat):
    uid = kullanici_olustur()
    abonelik_ekle(uid)
    with OturumYapici() as db:
        db.add(Rapor(user_id=uid, tarih=CARSAMBA, tur="gunluk", metin="rapor"))
        db.commit()
    r = cron()
    assert satir(r, uid)["neden"] == "bugünün raporu kopyalanmış"
    assert push.cagrilar == [] and SahteSmtp.gonderilen == []


def test_ozel_saat_ve_gunler(push, saat):
    uid = kullanici_olustur(hatirlatma_saat=time(9, 30), hatirlatma_gunler="6")
    saat(CUMARTESI, 9, 30)
    assert satir(cron(), uid)["eposta"] == "gönderildi"
    saat(CARSAMBA, 18, 0)
    assert satir(cron(), uid)["neden"] == "bugün hatırlatma günü değil"


def test_sure_siniri_asilinca_kalanlar_sonraki_pinge(push, saat, monkeypatch):
    kullanici_olustur("a@ornek.com")
    kullanici_olustur("b@ornek.com", "B")
    monkeypatch.setattr(api, "SURE_SINIRI", 0)
    r = cron().json()
    assert r["kullanicilar"] == [] and r["kalan"] == 2
    assert SahteSmtp.gonderilen == []


# ---------------------------------------------------------------- kanallar

def test_push_410_abonelik_silinir_500_son_hata(push, saat):
    uid = kullanici_olustur(gmail=False)
    abonelik_ekle(uid, "https://push.ornek.com/gitti")
    kalan = abonelik_ekle(uid, "https://push.ornek.com/bozuk")
    push.hatalar = {"https://push.ornek.com/gitti": 410, "https://push.ornek.com/bozuk": 500}
    r = cron()
    assert satir(r, uid)["push"] == "0/2"
    with OturumYapici() as db:
        abonelikler = db.scalars(select(PushAbonelik)).all()
        assert [a.id for a in abonelikler] == [kalan]
        assert "500" in abonelikler[0].son_hata


def test_eposta_hatasi_push_yine_gider(push, saat):
    uid = kullanici_olustur()
    abonelik_ekle(uid)
    SahteSmtp.hata = smtplib.SMTPAuthenticationError(535, b"bad")
    r = satir(cron(), uid)
    assert r["push"] == "1/1"
    assert r["eposta"] == "hata" and "uygulama şifresi" in r["neden"]
    assert "uyg-sifre" not in r["neden"]


def test_bildirim_ve_eposta_icerigi(push, saat, monkeypatch):
    monkeypatch.setattr(servisler, "gmail_tara", lambda *a, **k: [servisler.madde("eposta", "MESAM'a e-posta"), servisler.madde("eposta", "MSG'ye e-posta")])
    monkeypatch.setattr(servisler, "github_tara", lambda *a, **k: [servisler.madde("medusa", "Arama hızlandı")])
    uid = kullanici_olustur(github_repo="ornek/medusa", github_token_enc=guvenlik.sifrele("gh-token"))
    abonelik_ekle(uid)
    cron()
    veri = json.loads(push.cagrilar[0]["data"])
    assert veri == {"baslik": "Günlük Rapor", "govde": "Bugünün raporu hazır bekliyor · 2 e-posta, 1 commit bulundu", "url": "https://rapor.ornek.com"}
    mesaj = SahteSmtp.gonderilen[0]
    assert mesaj["Subject"] == "Günlük rapor hatırlatması – 16.09.2026"
    govde = mesaj.get_content()
    assert govde.startswith("Bugünün raporu hazır bekliyor · 2 e-posta, 1 commit bulundu")
    assert "• MESAM'a e-posta" in govde and "• Arama hızlandı" in govde and "https://rapor.ornek.com" in govde
    assert govde.rstrip().endswith("Bu hatırlatma, raporu kopyaladığın gün gelmez.")
    with OturumYapici() as db:  # tarama bulunanları maddelere yazdı
        assert db.scalar(select(func.count()).select_from(Madde).where(Madde.tur == "bulunan")) == 3


def test_bulunan_yoksa_ozet_metni():
    assert api.hatirlatma_ozeti([]) == "Bugün için bulunan yok, yapılanları ekle"
    assert api.hatirlatma_ozeti([Madde(kaynak="medusa")] * 4) == "Bugünün raporu hazır bekliyor · 4 commit bulundu"


def test_eposta_adresi_bossa_giris_epostasina(push, saat):
    bos = kullanici_olustur("a@ornek.com")
    dolu = kullanici_olustur("b@ornek.com", "B", hatirlatma_eposta_adres="baska@ornek.com")
    cron()
    alicilar = {m["From"].addresses[0].addr_spec: m["To"] for m in SahteSmtp.gonderilen}  # O1-ek: görünen ad eklendi
    assert alicilar == {"a@gmail.com": "a@ornek.com", "b@gmail.com": "baska@ornek.com"}
    assert bos != dolu


def test_push_cihazi_yoksa_0_kayit_acilmaz(push, saat):
    """E-posta kapalıyken push kanalı cihaz yoksa kayıt açmaz; gün içinde cihaz eklenirse gönderir."""
    uid = kullanici_olustur(gmail=False, hatirlatma_eposta=False)
    r = satir(cron(), uid)
    assert r["eposta"] == "atlandı" and "e-posta kapalı" in r["neden"] and r["push"] == "0/0"
    with OturumYapici() as db:
        assert db.scalar(select(func.count()).select_from(HatirlatmaGonderimi)) == 0
    abonelik_ekle(uid)
    assert satir(cron(), uid)["push"] == "1/1"


# ---------------------------------------------------------------- abonelik uçları

def test_abonelik_kullanici_ayrimi_ve_upsert(push):
    kullanici_olustur("a@ornek.com")
    kullanici_olustur("b@ornek.com", "B")
    a, b = istemci("a@ornek.com"), istemci("b@ornek.com")
    govde = {"subscription": {"endpoint": "https://push.ornek.com/x", "keys": {"p256dh": "p1", "auth": "a1"}}}
    ua = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"}
    ilk = a.post("/api/push/abone", json=govde, headers=ua).json()
    assert ilk["cihaz_adi"] == "iPhone Safari"
    govde["subscription"]["keys"] = {"p256dh": "p2", "auth": "a2"}
    ikinci = a.post("/api/push/abone", json=govde).json()
    assert ikinci["id"] == ilk["id"]
    with OturumYapici() as db:
        assert db.scalar(select(func.count()).select_from(PushAbonelik)) == 1
        assert db.scalar(select(PushAbonelik.p256dh)) == "p2"

    assert b.get("/api/push/abone").json()["cihazlar"] == []
    assert b.delete(f"/api/push/abone/{ilk['id']}").status_code == 404
    assert len(a.get("/api/push/abone").json()["cihazlar"]) == 1
    assert a.delete(f"/api/push/abone/{ilk['id']}").status_code == 200
    assert a.get("/api/push/abone").json()["cihazlar"] == []
    assert a.post("/api/push/abone", json={"subscription": {"endpoint": "http://x", "keys": {"p256dh": "p", "auth": "a"}}}).status_code == 422


def test_anahtar_ve_test_bildirimi(push):
    uid = kullanici_olustur()
    abonelik_ekle(uid, "https://push.ornek.com/iyi")
    abonelik_ekle(uid, "https://push.ornek.com/gitti")
    push.hatalar = {"https://push.ornek.com/gitti": 404}
    c = istemci()
    assert c.get("/api/push/anahtar").json()["anahtar"] == servisler.vapid_anahtarlari()[0]
    s = c.post("/api/push/dene").json()
    assert (s["basarili"], s["toplam"]) == (1, 2)
    assert [x["durum"] for x in s["cihazlar"]] == ["ok", "silindi"]
    assert "Bildirimler çalışıyor" in push.cagrilar[0]["data"]
    assert push.cagrilar[0]["vapid_claims"]["sub"] == "mailto:test@ornek.com"
    assert TestClient(uygulama.app).get("/api/push/anahtar").status_code == 401


def test_hatirlatma_ayarlari_kaydet_ve_dogrula():
    kullanici_olustur(gmail=False)
    c = istemci()
    a = c.get("/api/ayarlar").json()
    assert (a["hatirlatma_saat"], a["hatirlatma_gunler"], a["hatirlatma_push"], a["hatirlatma_eposta"]) == ("17:00", [1, 2, 3, 4, 5], True, True)
    assert a["giris_eposta"] == "a@ornek.com"
    a = c.put("/api/ayarlar", json={
        "hatirlatma_saat": "08:45", "hatirlatma_gunler": [7, 1, 1], "hatirlatma_push": False,
        "hatirlatma_eposta_adres": " Ben@Ornek.com ",
    }).json()
    assert (a["hatirlatma_saat"], a["hatirlatma_gunler"], a["hatirlatma_push"], a["hatirlatma_eposta_adres"]) == ("08:45", [1, 7], False, "ben@ornek.com")
    assert c.put("/api/ayarlar", json={"hatirlatma_saat": "25:00"}).status_code == 422
    assert c.put("/api/ayarlar", json={"hatirlatma_gunler": [8]}).status_code == 422
    assert c.put("/api/ayarlar", json={"hatirlatma_eposta_adres": "adres-degil"}).status_code == 422
    assert c.put("/api/ayarlar", json={"hatirlatma_gunler": []}).json()["hatirlatma_gunler"] == []


@pytest.mark.parametrize("ua, beklenen", [
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1", "iPhone Safari"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36", "Mac Chrome"),
    ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36", "Android Chrome"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36 Edg/128.0", "Windows Edge"),
])
def test_cihaz_adi_bul(ua, beklenen):
    assert api.cihaz_adi_bul(ua) == beklenen


# ---------------------------------------------------------------- PWA ve şema

def test_pwa_dosyalari_oturumsuz_ve_sayfalarda_manifest():
    c = TestClient(uygulama.app, follow_redirects=False)
    sw = c.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"] and "showNotification" in sw.text
    assert "addEventListener('fetch'" not in sw.text
    m = c.get("/static/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone"
    assert c.get("/static/ikon-512.png").status_code == 200
    assert 'rel="manifest"' in c.get("/giris").text
    kullanici_olustur(gmail=False)
    k = istemci()
    for yol in ("/", "/ayarlar", "/gecmis"):
        assert 'rel="apple-touch-icon"' in k.get(yol).text
    assert "serviceWorker.register('/sw.js')" in k.get("/").text


def test_sema_guncelle_hatirlatma_iki_kez(tmp_path):
    eski = create_engine(f"sqlite:///{tmp_path}/eski.db")
    with eski.begin() as b:
        b.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, eposta VARCHAR(254))"))
        b.execute(text("CREATE TABLE user_settings (user_id INTEGER PRIMARY KEY, gmail_kullanici VARCHAR(254))"))
        b.execute(text("INSERT INTO users (id, eposta) VALUES (1, 'a@ornek.com')"))
        b.execute(text("INSERT INTO user_settings (user_id) VALUES (1)"))

    eklenen = veritabani.sema_guncelle(eski)
    assert eklenen == [
        "push_abonelikleri", "hatirlatma_gonderimleri", "claude_kullanim", "kategoriler",
        "user_settings.hatirlatma_saat", "user_settings.hatirlatma_gunler", "user_settings.hatirlatma_push",
        "user_settings.hatirlatma_eposta", "user_settings.hatirlatma_eposta_adres",
        "user_settings.kaynaklar", "user_settings.kurulum_tamam", "users.davet_eposta_tarihi", "user_settings.eposta_gruplama",
        "user_settings.rapor_bicimi", "user_settings.karistir", "user_settings.kendi_alanlar", "user_settings.ekip_ici_atla",
        "user_settings.google_refresh_enc", "user_settings.google_eposta", "user_settings.google_baglanti",
        "user_settings.google_durum", "user_settings.google_kapsamlar",
        "user_settings.otomatik_gonder", "user_settings.otomatik_saat", "user_settings.patron_eposta",
        "user_settings.patron_adi", "user_settings.otomatik_kopya_bana", "user_settings.ad_eslemeleri",
    ]
    assert veritabani.sema_guncelle(eski) == []
    with eski.connect() as b:
        assert b.execute(text(
            "SELECT hatirlatma_saat, hatirlatma_gunler, hatirlatma_push, hatirlatma_eposta, hatirlatma_eposta_adres FROM user_settings"
        )).one() == ("17:00:00", "1,2,3,4,5", 1, 1, None)
        b.execute(text("INSERT INTO push_abonelikleri (user_id, endpoint, p256dh, auth, cihaz_adi, olusturma) VALUES (1, 'https://x', 'p', 'a', 'Mac', '2026-09-16 17:00:00')"))
    eski.dispose()
    assert veritabani.sema_guncelle() == []
    assert veritabani.sema_guncelle() == []


# ---------------------------------------------------------------- K1-ek: log, yönetim sütunu, e-posta testi

def test_hatirlat_kullanici_basina_tek_info_logu(push, saat, caplog):
    uid = kullanici_olustur()
    abonelik_ekle(uid)
    with caplog.at_level("INFO", logger="gunluk-rapor"):
        cron()
    satirlar = [k.message for k in caplog.records if k.message.startswith("hatirlat user=")]
    assert satirlar == [f"hatirlat user={uid} push=1/1 eposta=gönderildi neden=-"]
    assert "a@gmail.com" not in satirlar[0] and "uyg-sifre" not in satirlar[0]


def test_hatirlat_logu_atlanan_kullanici_icin_de_yazilir(push, saat, caplog):
    uid = kullanici_olustur()
    saat(CUMARTESI, 18, 0)
    with caplog.at_level("INFO", logger="gunluk-rapor"):
        cron()
    satirlar = [k.message for k in caplog.records if k.message.startswith("hatirlat user=")]
    assert satirlar == [f"hatirlat user={uid} push=atlandı eposta=atlandı neden=bugün hatırlatma günü değil"]


def test_eposta_hatasi_warning_loglanir_ve_kayda_yazilir(push, saat, caplog):
    uid = kullanici_olustur()
    SahteSmtp.hata = smtplib.SMTPAuthenticationError(535, b"bad")
    with caplog.at_level("INFO", logger="gunluk-rapor"):
        cron()
    uyarilar = [k.message for k in caplog.records if k.levelname == "WARNING" and "kanal hatasi" in k.message]
    assert len(uyarilar) == 1
    assert uyarilar[0].startswith(f"hatirlat kanal hatasi user={uid} kanal=eposta neden=Gmail'e giriş yapılamadı")
    assert "uyg-sifre" not in uyarilar[0]
    with OturumYapici() as db:
        kayit = db.scalars(select(HatirlatmaGonderimi).where(HatirlatmaGonderimi.kanal == "eposta")).one()
        assert kayit.durum == "hata" and "Gmail'e giriş yapılamadı" in kayit.hata_metni


def test_push_hatasi_warning_loglanir_ve_kayda_yazilir(push, saat, caplog):
    uid = kullanici_olustur(gmail=False, hatirlatma_eposta=False)
    abonelik_ekle(uid)
    push.hatalar["https://push.ornek.com/1"] = 500
    with caplog.at_level("INFO", logger="gunluk-rapor"):
        cron()
    uyarilar = [k.message for k in caplog.records if k.levelname == "WARNING" and "kanal hatasi" in k.message]
    assert len(uyarilar) == 1 and f"user={uid} kanal=push" in uyarilar[0]
    with OturumYapici() as db:
        kayit = db.scalars(select(HatirlatmaGonderimi).where(HatirlatmaGonderimi.kanal == "push")).one()
        assert kayit.durum == "hata" and "500" in kayit.hata_metni


def test_yonetim_son_hatirlatma_sutunu(push, saat):
    yonetici = kullanici_olustur("y@ornek.com", "Yonetici", gmail=False, rol="admin",
                                 hatirlatma_push=False, hatirlatma_eposta=False)
    uid = kullanici_olustur("b@ornek.com", "B")
    abonelik_ekle(uid, "https://push.ornek.com/b")
    SahteSmtp.hata = smtplib.SMTPAuthenticationError(535, b"bad")
    cron()

    y = istemci("y@ornek.com").get("/yonetim")
    assert y.status_code == 200
    assert "Son hatırlatma" in y.text
    assert "e-posta ✗ (Gmail&#39;e giriş yapılamadı" in y.text
    assert "push ✓" in y.text
    assert "16.09.2026" in y.text
    # Hiç gönderimi olmayan yöneticinin satırında tire durur.
    with OturumYapici() as db:
        assert yonetici not in api.son_hatirlatmalar(db)
    assert '<span class="yok">—</span>' in y.text
    assert "uyg-sifre" not in y.text


def test_son_hatirlatmalar_kayit_yoksa_bos():
    kullanici_olustur()
    with OturumYapici() as db:
        assert api.son_hatirlatmalar(db) == {}


def test_son_hatirlatmalar_yalniz_en_son_gunu_dondurur():
    uid = kullanici_olustur()
    with OturumYapici() as db:
        db.add_all([
            HatirlatmaGonderimi(user_id=uid, tarih=date(2026, 9, 15), kanal="push", durum="gonderildi"),
            HatirlatmaGonderimi(user_id=uid, tarih=CARSAMBA, kanal="push", durum="gonderildi"),
            HatirlatmaGonderimi(user_id=uid, tarih=CARSAMBA, kanal="eposta", durum="hata",
                                hata_metni="A" * 90),
        ])
        db.commit()
        ozet = api.son_hatirlatmalar(db)
    assert ozet[uid]["tarih"] == CARSAMBA
    assert ozet[uid]["kanallar"] == [
        {"ad": "e-posta", "ok": False, "neden": "A" * 60},
        {"ad": "push", "ok": True, "neden": ""},
    ]


def test_eposta_dene_gonderir():
    kullanici_olustur()
    y = istemci().post("/api/hatirlat/eposta-dene")
    assert y.status_code == 200 and y.json() == {"ok": True}
    assert len(SahteSmtp.gonderilen) == 1
    mesaj = SahteSmtp.gonderilen[0]
    assert mesaj["Subject"] == "Günlük rapor — e-posta testi"
    assert mesaj["To"] == "a@ornek.com" and mesaj["From"].addresses[0].addr_spec == "a@gmail.com"
    assert mesaj["From"].addresses[0].display_name == "A - Günlük Rapor"  # O1-ek2


def test_eposta_dene_kayitli_hatirlatma_adresine_gider():
    kullanici_olustur(hatirlatma_eposta_adres="baska@ornek.com")
    assert istemci().post("/api/hatirlat/eposta-dene").json() == {"ok": True}
    assert SahteSmtp.gonderilen[0]["To"] == "baska@ornek.com"


def test_eposta_dene_smtp_hatasinda_neden_doner(caplog):
    kullanici_olustur()
    SahteSmtp.hata = smtplib.SMTPAuthenticationError(535, b"bad")
    with caplog.at_level("INFO", logger="gunluk-rapor"):
        y = istemci().post("/api/hatirlat/eposta-dene")
    assert y.status_code == 200
    govde = y.json()
    assert govde["ok"] is False and "uygulama şifresi" in govde["neden"]
    assert "uyg-sifre" not in govde["neden"]
    assert any(k.levelname == "WARNING" and "eposta testi" in k.message for k in caplog.records)


def test_eposta_dene_hicbir_gonderim_yolu_yoksa_neden_doner():
    """Ne RESEND_API_KEY ne de Gmail yedeği varsa: 400 değil, ekranda gösterilecek neden."""
    kullanici_olustur(gmail=False)
    y = istemci().post("/api/hatirlat/eposta-dene")
    assert y.status_code == 200 and y.json()["ok"] is False
    assert "RESEND_API_KEY tanımlı değil" in y.json()["neden"]
    assert SahteSmtp.gonderilen == []


def test_eposta_dene_oturumsuz_401():
    kullanici_olustur()
    assert TestClient(uygulama.app).post("/api/hatirlat/eposta-dene").status_code == 401


# ---------------------------------------------------------------- K1-ek2: Resend HTTPS gönderimi

def test_resend_basarida_none_doner_ve_istegi_kurar(resend):
    assert servisler.eposta_gonder("kime@ornek.com", "Konu", "Gövde", yanit_adresi="ben@ornek.com") is None
    istek = resend.istekler[0]
    assert istek["url"] == "https://api.resend.com/emails"
    assert istek["headers"]["Authorization"] == "Bearer re_test_anahtar"
    assert istek["govde"] == {
        "from": "Günlük Rapor <rapor@medusarights.com>", "to": ["kime@ornek.com"],
        "subject": "Konu", "text": "Gövde", "reply_to": "ben@ornek.com",
    }


def test_resend_yanit_adresi_yoksa_alan_gonderilmez(resend):
    servisler.eposta_gonder("kime@ornek.com", "Konu", "Gövde")
    assert "reply_to" not in resend.istekler[0]["govde"]


def test_resend_gonderen_varsayilani_ve_ortam_degiskeni(resend, monkeypatch):
    monkeypatch.delenv("EPOSTA_GONDEREN", raising=False)
    assert servisler.gonderen_adresi() == "rapor@medusarights.com"
    monkeypatch.setenv("EPOSTA_GONDEREN", "baska@ornek.com")
    servisler.eposta_gonder("kime@ornek.com", "Konu", "Gövde")
    assert resend.istekler[0]["govde"]["from"] == "Günlük Rapor <baska@ornek.com>"


def test_resend_422_kisa_turkce_neden_doner(resend):
    resend.kod, resend.govde = 422, {"message": "D" * 200}
    hata = servisler.eposta_gonder("kime@ornek.com", "Konu", "Gövde")
    assert hata == "Resend 422: " + "D" * 120


def test_resend_baglanti_hatasi_yutulur(resend):
    resend.hata = httpx.ConnectTimeout("zaman aşımı")
    assert servisler.eposta_gonder("kime@ornek.com", "Konu", "Gövde") == "E-posta gönderilemedi (ConnectTimeout)"


def test_anahtar_yoksa_smtp_yedegi_calisir():
    assert servisler.eposta_gonder(
        "kime@ornek.com", "Konu", "Gövde", yanit_adresi="ben@ornek.com",
        gmail_kullanici="a@gmail.com", gmail_sifre="uyg-sifre") is None
    mesaj = SahteSmtp.gonderilen[0]
    assert mesaj["From"].addresses[0].addr_spec == "a@gmail.com" and mesaj["Reply-To"] == "ben@ornek.com"


def test_anahtar_da_gmail_de_yoksa_neden_doner():
    hata = servisler.eposta_gonder("kime@ornek.com", "Konu", "Gövde")
    assert "RESEND_API_KEY tanımlı değil" in hata and SahteSmtp.gonderilen == []


def test_hatirlatma_resend_ile_gider_ve_yanit_adresi_tasir(push, saat, resend):
    uid = kullanici_olustur()
    r = satir(cron(), uid)
    assert r["eposta"] == "gönderildi"
    govde = resend.istekler[0]["govde"]
    assert govde["to"] == ["a@ornek.com"] and govde["reply_to"] == "a@ornek.com"
    assert govde["from"] == 'A - Günlük Rapor <rapor@medusarights.com>'  # O1-ek2: tırnaksız
    assert "Günlük rapor hatırlatması – 16.09.2026" == govde["subject"]
    assert SahteSmtp.gonderilen == []  # SMTP'ye hiç düşülmez


def test_hatirlatma_gmail_ayari_olmayan_kullaniciya_da_gider(push, saat, resend):
    uid = kullanici_olustur(gmail=False)
    r = satir(cron(), uid)
    assert r["eposta"] == "gönderildi" and "Gmail" not in r["neden"]
    assert resend.istekler[0]["govde"]["to"] == ["a@ornek.com"]


def test_hatirlatma_resend_hatasi_kayda_ve_loga_yazilir(push, saat, resend, caplog):
    uid = kullanici_olustur(gmail=False)
    resend.kod, resend.govde = 422, {"message": "Alan adı doğrulanmadı"}
    with caplog.at_level("INFO", logger="gunluk-rapor"):
        assert satir(cron(), uid)["eposta"] == "hata"
    with OturumYapici() as db:
        kayit = db.scalars(select(HatirlatmaGonderimi).where(HatirlatmaGonderimi.kanal == "eposta")).one()
        assert kayit.durum == "hata" and kayit.hata_metni == "Resend 422: Alan adı doğrulanmadı"
    assert any("kanal=eposta" in k.message and "Resend 422" in k.message for k in caplog.records)


def test_eposta_dene_resend_ile_gider(resend):
    kullanici_olustur(gmail=False)
    y = istemci().post("/api/hatirlat/eposta-dene")
    assert y.status_code == 200 and y.json() == {"ok": True}
    govde = resend.istekler[0]["govde"]
    assert govde["subject"] == "Günlük rapor — e-posta testi"
    assert govde["to"] == ["a@ornek.com"] and govde["reply_to"] == "a@ornek.com"
    assert "rapor@medusarights.com" in govde["text"]


def test_eposta_dene_resend_hatasinda_neden_doner(resend):
    kullanici_olustur()
    resend.kod, resend.govde = 403, {"message": "Alan adı doğrulanmadı"}
    govde = istemci().post("/api/hatirlat/eposta-dene").json()
    assert govde == {"ok": False, "neden": "Resend 403: Alan adı doğrulanmadı"}


def test_ayarlar_gonderen_adresini_doner(resend):
    kullanici_olustur()
    assert istemci().get("/api/ayarlar").json()["eposta_gonderen"] == "rapor@medusarights.com"
