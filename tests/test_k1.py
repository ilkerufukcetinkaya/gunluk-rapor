"""K1: kaynak modülleri, Claude kotası, kurulum sihirbazı, not e-postaları. sqlite; ağa çıkılmaz."""
import json
import threading
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import format_datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text

import api
import app as uygulama
import guvenlik
import servisler
import veritabani
from veritabani import ClaudeKullanim, Kullanici, KullaniciAyari, Madde, OturumYapici, Temel, motor

SIFRE = "dogru-sifre-123"
TOKEN = "test-cron-token"
BEN = "ben@gmail.com"
GERCEK_GMAIL_TARA = servisler.gmail_tara


@pytest.fixture(autouse=True)
def ortam(monkeypatch):
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    api.onbellegi_temizle()
    monkeypatch.setattr(servisler, "gmail_tara", lambda *a, **k: [])
    monkeypatch.setattr(servisler, "github_tara", lambda *a, **k: [])
    yield


def kullanici_olustur(eposta="a@ornek.com", ad="A", kurulum=True, **ayar) -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad=ad, sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol="uye", aktif=True, sifre_degistirmeli=False)
        db.add(k)
        db.commit()
        if kurulum or ayar:
            db.add(KullaniciAyari(user_id=k.id, kurulum_tamam=kurulum, **ayar))
            db.commit()
        return k.id


def istemci(eposta="a@ornek.com") -> TestClient:
    c = TestClient(uygulama.app, follow_redirects=False)
    c.__enter__()
    assert c.post("/giris", data={"eposta": eposta, "sifre": SIFRE}).status_code == 303
    return c


def ekle(**alanlar) -> int:
    with OturumYapici() as db:
        m = Madde(**alanlar)
        db.add(m)
        db.commit()
        return m.id


# ---------------------------------------------------------------- 1) kaynak modülleri

def test_github_kapaliyken_taranmaz_hata_yazilmaz(monkeypatch):
    cagrilar = []
    monkeypatch.setattr(servisler, "github_tara", lambda *a, **k: cagrilar.append(a) or [servisler.madde("medusa", "x")])
    uid = kullanici_olustur(github_repo="ben/proje", github_token_enc=guvenlik.sifrele("t"))
    ekle(user_id=uid, tur="bulunan", tarih=servisler.istanbul_bugun(), kaynak="medusa", kaynak_id="eski", metin="sabahki commit")
    c = istemci()
    # seçim yokken token kayıtlı → açık
    assert c.get("/api/ayarlar").json()["kaynaklar"] == {"gmail": False, "github": True, "medusa": False}
    ayar = c.put("/api/ayarlar", json={"kaynaklar": {"github": False, "medusa": True}}).json()
    assert ayar["kaynaklar"] == {"gmail": False, "github": False, "medusa": False}  # medusa açılamaz
    assert c.put("/api/ayarlar", json={"kaynaklar": {"dropbox": True}}).status_code == 422

    d = c.get("/api/bugun?yenile=1").json()
    assert cagrilar == [] and d["hatalar"] == [] and d["bulunan"] == []
    assert [m for m in c.get("/api/durum").json()["maddeler"] if m["tur"] == "bulunan"] == []

    c.put("/api/ayarlar", json={"kaynaklar": {"github": True}})
    d = c.get("/api/bugun?yenile=1").json()
    assert len(cagrilar) == 1 and [m["metin"] for m in d["bulunan"]] == ["sabahki commit", "x"]


def test_hatirlatma_ozeti_kapali_kaynagi_saymaz(monkeypatch):
    monkeypatch.setenv("CRON_TOKEN", TOKEN)
    monkeypatch.setenv("APP_URL", "https://rapor.ornek.com")
    monkeypatch.setattr(api, "istanbul_simdi", lambda: datetime.combine(date(2026, 9, 16), time(17, 30), servisler.ISTANBUL))
    gonderilen = []
    monkeypatch.setattr(servisler, "eposta_gonder", lambda *a, **kw: gonderilen.append((a, kw)) or None)
    monkeypatch.setattr(servisler, "gmail_tara", lambda *a, **k: [servisler.madde("eposta", "MSG'ye e-posta"), servisler.madde("eposta", "IMRO'ya e-posta")])
    uid = kullanici_olustur(
        gmail_kullanici=BEN, gmail_sifre_enc=guvenlik.sifrele("s"), github_repo="ben/proje",
        github_token_enc=guvenlik.sifrele("t"), kaynaklar={"gmail": True, "github": False, "medusa": False},
    )
    ekle(user_id=uid, tur="bulunan", tarih=date(2026, 9, 16), kaynak="medusa", kaynak_id="c1", metin="Arama hızlandı")
    r = TestClient(uygulama.app).post(f"/api/hatirlat?token={TOKEN}").json()
    assert r["kullanicilar"][0]["eposta"] == "gönderildi"
    konu, govde = gonderilen[0][0][1], gonderilen[0][0][2]
    assert govde.startswith("Bugünün raporu hazır bekliyor · 2 e-posta bulundu")
    assert "commit" not in govde and "Arama hızlandı" not in govde and "16.09.2026" in konu


def test_sema_guncelle_eski_semada_kolon_tablo_ve_geri_doldurma(tmp_path):
    eski = create_engine(f"sqlite:///{tmp_path}/eski.db")
    with eski.begin() as b:
        b.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, eposta VARCHAR(254))"))
        b.execute(text("CREATE TABLE user_settings (user_id INTEGER PRIMARY KEY, gmail_kullanici VARCHAR(254), "
                       "gmail_sifre_enc TEXT, github_token_enc TEXT, hatirlatma_saat TIME, hatirlatma_gunler VARCHAR(20), "
                       "hatirlatma_push BOOLEAN, hatirlatma_eposta BOOLEAN, hatirlatma_eposta_adres VARCHAR(254))"))
        b.execute(text("CREATE TABLE push_abonelikleri (id INTEGER PRIMARY KEY)"))
        b.execute(text("CREATE TABLE hatirlatma_gonderimleri (id INTEGER PRIMARY KEY)"))
        for uid in (1, 2, 3):
            b.execute(text("INSERT INTO users (id, eposta) VALUES (:u, 'x')"), {"u": uid})
        b.execute(text("INSERT INTO user_settings (user_id, gmail_kullanici, gmail_sifre_enc, github_token_enc) VALUES "
                       "(1, 'a@gmail.com', 'enc-g', 'enc-t'), (2, NULL, NULL, 'enc-t'), (3, 'c@gmail.com', '', NULL)"))

    assert veritabani.sema_guncelle(eski) == [
        "claude_kullanim", "kategoriler", "user_settings.kaynaklar", "user_settings.kurulum_tamam",
        "hatirlatma_gonderimleri.hata_metni", "users.davet_eposta_tarihi", "user_settings.eposta_gruplama",
        "user_settings.rapor_bicimi", "user_settings.karistir", "user_settings.kendi_alanlar", "user_settings.ekip_ici_atla",
        "user_settings.google_refresh_enc", "user_settings.google_eposta", "user_settings.google_baglanti",
        "user_settings.google_durum", "user_settings.google_kapsamlar",
        "user_settings.otomatik_gonder", "user_settings.otomatik_saat", "user_settings.patron_eposta",
        "user_settings.patron_adi", "user_settings.otomatik_kopya_bana",
    ]
    assert veritabani.sema_guncelle(eski) == []
    with eski.begin() as b:
        satirlar = b.execute(text("SELECT user_id, kaynaklar, kurulum_tamam FROM user_settings ORDER BY user_id")).all()
        assert [(u, json.loads(k), t) for u, k, t in satirlar] == [
            (1, {"gmail": True, "github": True, "medusa": False}, 1),
            (2, {"gmail": False, "github": True, "medusa": False}, 0),
            (3, {"gmail": False, "github": False, "medusa": False}, 0),
        ]
        b.execute(text("INSERT INTO claude_kullanim (user_id, tarih, cagri, girdi_token, cikti_token) VALUES (1, '2026-09-16', 1, 0, 0)"))
    eski.dispose()
    assert veritabani.sema_guncelle() == []


# ---------------------------------------------------------------- 2) kota

def claude_istemcisi(girdi: int, cikti: int):
    """Gerçek _claude_cagir'ı sahte HTTP yanıtıyla çalıştırır; usage alanı döner."""
    def isleyici(istek: httpx.Request) -> httpx.Response:
        girdiler = json.loads(json.loads(istek.content)["messages"][0]["content"].split("Düzeltilecek maddeler:\n", 1)[1])
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": json.dumps([{"id": g["id"], "metin": g["metin"] + "."} for g in girdiler])}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": girdi, "output_tokens": cikti, "cache_read_input_tokens": 0},
        })
    gercek = servisler._claude_cagir
    return lambda istek, anahtar, istemci=None: gercek(istek, anahtar, httpx.Client(transport=httpx.MockTransport(isleyici)))


def test_kota_sekizinci_gecer_dokuzuncu_atlanir_token_yazilir(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anahtar")
    monkeypatch.setattr(servisler, "_claude_cagir", claude_istemcisi(120, 30))
    uid = kullanici_olustur()
    c = istemci()
    for n in range(1, 10):
        m = c.post("/api/maddeler", json={"tur": "bugun", "kaynak": "elle", "metin": f"madde {n}"}).json()
        r = c.post("/api/duzelt")
        assert r.status_code == 200
        if n <= 8:
            assert r.json()["duzeltilen"] == 1 and "atlandi" not in r.json()
        else:
            assert r.json()["atlandi"] == "günlük sınır" and r.json()["duzeltilen"] == 0
    durum = {x["id"]: x for x in c.get("/api/durum").json()["maddeler"]}
    assert durum[m["id"]]["rapor_metni"] == "madde 9"  # ham metinle devam
    assert c.patch(f"/api/maddeler/{m['id']}/ai", json={"yenile": True}).json()["hatalar"] == [api.KOTA_MESAJI]

    with OturumYapici() as db:
        satirlar = db.scalars(select(ClaudeKullanim).where(ClaudeKullanim.user_id == uid)).all()
        assert [(s.tarih, s.cagri, s.girdi_token, s.cikti_token) for s in satirlar] == [
            (servisler.istanbul_bugun(), 8, 8 * 120, 8 * 30),
        ]

    # haftalık da aynı sayaçtan düşer: 429 + Türkçe mesaj
    pazartesi = date(2026, 9, 14)
    with OturumYapici() as db:
        db.add(veritabani.Rapor(user_id=uid, tarih=pazartesi, metin="• x", tur="gunluk"))
        db.commit()
    r = c.post("/api/haftalik", json={"hafta_baslangic": pazartesi.isoformat()})
    assert r.status_code == 429 and "hakkın doldu" in r.json()["detail"]


def test_kota_gun_degisince_sifirlanir_ve_haftalik_sayilir(monkeypatch, sahte_claude):
    uid = kullanici_olustur()
    bugun = date(2026, 9, 16)
    monkeypatch.setattr(api, "bugun", lambda: bugun)
    with OturumYapici() as db:
        db.add(ClaudeKullanim(user_id=uid, tarih=bugun, cagri=8, girdi_token=5, cikti_token=5))
        db.add(veritabani.Rapor(user_id=uid, tarih=date(2026, 9, 14), metin="• x", tur="gunluk"))
        db.commit()
    c = istemci()
    assert c.post("/api/haftalik", json={"hafta_baslangic": "2026-09-14"}).status_code == 429
    assert sahte_claude.istekler == []

    bugun = date(2026, 9, 17)
    sahte_claude.yanitlar = ["*Haftalık Özet*"]
    assert c.post("/api/haftalik", json={"hafta_baslangic": "2026-09-14"}).status_code == 200
    with OturumYapici() as db:
        assert [(s.tarih, s.cagri) for s in db.scalars(select(ClaudeKullanim).order_by(ClaudeKullanim.tarih))] == [
            (date(2026, 9, 16), 8), (date(2026, 9, 17), 1),
        ]
        assert api.aylik_claude_cagrilari(db, date(2026, 9, 17)) == {uid: 9}
        assert api.aylik_claude_cagrilari(db, date(2026, 10, 1)) == {}


def test_kota_eszamanli_cagrilarda_unique_ihlali_yok():
    uid = kullanici_olustur()
    tarih = date(2026, 9, 16)
    sonuclar, hatalar = [], []

    def cagir():
        try:
            with OturumYapici() as db:
                sonuclar.append(api.claude_hakki_al(db, uid, tarih) is not None)
        except Exception as e:  # pragma: no cover - başarısızlıkta görünsün
            hatalar.append(e)

    ipler = [threading.Thread(target=cagir) for _ in range(12)]
    for i in ipler:
        i.start()
    for i in ipler:
        i.join()
    assert hatalar == [] and sorted(sonuclar) == [False] * 4 + [True] * 8
    with OturumYapici() as db:
        assert db.scalar(select(func.count()).select_from(ClaudeKullanim)) == 1


# ---------------------------------------------------------------- 3) kurulum sihirbazı

def test_yeni_kullanici_kuruluma_yonlenir_bitirince_ana_sayfa():
    uid = kullanici_olustur(kurulum=False)
    c = istemci()
    for yol in ("/", "/gecmis", "/ayarlar"):
        r = c.get(yol)
        assert (r.status_code, r.headers["location"]) == (303, "/kurulum")
    assert c.get("/api/durum").status_code == 200
    assert c.get("/sifre").status_code == 200
    k = c.get("/kurulum")
    assert k.status_code == 200 and "Sonra yaparım" in k.text and "Uygulama şifreleri" in k.text

    bilgi = c.get("/api/kurulum").json()
    assert bilgi["ad"] == "A" and [s["ad"] for s in bilgi["sablonlar"]] == ["Telif ve meslek birlikleri", "Lisanslama", "Genel / idari"]
    assert bilgi["ayarlar"]["kurulum_tamam"] is False
    p = c.post("/api/kurulum/profil", json={"ad": " Ayşe ", "rapor_basligi": "Günlük Rapor", "patron_telefon": "905551112233"}).json()
    assert (p["ad"], p["rapor_basligi"], p["patron_telefon"]) == ("Ayşe", "Günlük Rapor", "905551112233")
    assert c.post("/api/kurulum/profil", json={"ad": "  "}).status_code == 422

    assert c.post("/api/kurulum/bitir").json() == {"ok": True}
    assert c.get("/").status_code == 200
    assert c.get("/kurulum").status_code == 200  # "Kurulumu yeniden aç"
    c.post("/api/kurulum/bitir")
    with OturumYapici() as db:
        assert db.get(KullaniciAyari, uid).kurulum_tamam is True
    assert 'href="/kurulum"' in c.get("/ayarlar").text
    assert c.post("/cikis").headers["location"] == "/giris"


def test_sablon_secimi_surekli_yazar_mukerrer_eklemez_mevcudu_korur():
    uid = kullanici_olustur(kurulum=False)
    ekle(user_id=uid, tur="surekli", metin="Bekleyen konular takip edildi", sira=1)
    ekle(user_id=uid, tur="surekli", metin="Kendi eski işim", sira=2, tikli=False)
    c = istemci()
    genel = servisler.SUREKLI_SABLONLAR[2]["maddeler"]
    r = c.post("/api/kurulum/surekli", json={"metinler": [genel[0], "  bekleyen  konular TAKİP edildi ", "Özel satırım", "", genel[0]]}).json()
    assert r["eklenen"] == 2
    assert r["surekli"] == ["Bekleyen konular takip edildi", "Kendi eski işim", genel[0], "Özel satırım"]
    assert c.post("/api/kurulum/surekli", json={"metinler": [genel[0], "Özel satırım"]}).json()["eklenen"] == 0
    with OturumYapici() as db:
        maddeler = db.scalars(select(Madde).where(Madde.user_id == uid).order_by(Madde.sira)).all()
        assert [(m.tur, m.sira, m.tikli) for m in maddeler] == [("surekli", 1, True), ("surekli", 2, False), ("surekli", 3, True), ("surekli", 4, True)]


def test_yonetim_listesinde_kurulum_kaynak_ve_cagri_sutunlari():
    admin = kullanici_olustur("admin@ornek.com", "Yönetici")
    with OturumYapici() as db:
        db.get(Kullanici, admin).rol = "admin"
        db.add(ClaudeKullanim(user_id=admin, tarih=servisler.istanbul_bugun(), cagri=5, girdi_token=0, cikti_token=0))
        db.commit()
    kullanici_olustur("b@ornek.com", "Bekleyen", kurulum=False, gmail_sifre_enc=guvenlik.sifrele("s"), gmail_kullanici="b@gmail.com")
    r = istemci("admin@ornek.com").get("/yonetim")
    assert r.status_code == 200
    assert "Bu ay çağrı" in r.text and "bekliyor" in r.text and 'title="Gmail"' in r.text
    assert '<b class="cagri">5</b>' in r.text


# ---------------------------------------------------------------- 4) not e-postaları

def baslik(konu: str, kime=BEN, mid="<a@mail>", saat=(10, 0), gun=None) -> dict:
    gun = gun or servisler.istanbul_bugun()
    return {"date": format_datetime(datetime.combine(gun, time(*saat), servisler.ISTANBUL)), "subject": konu,
            "from": BEN, "to": kime, "cc": None, "message-id": mid}


def test_not_onekleri_ve_govde_satirlari():
    bugun = servisler.istanbul_bugun()
    mailler = [
        baslik("rapor: Köprü Film ile görüşüldü", mid="<1@m>"),
        baslik("  Not:  MSG itirazı hazırlandı", mid="<2@m>"),
        {**baslik("RAPOR:", mid="<3@m>"), "govde": "\nMESAM'a dönüldü\n  \nIMRO listesi kontrol edildi\nCRD indirildi\n"},
        baslik("rapor hatırlatma", mid="<4@m>"),  # iki nokta yok
        baslik("rapor: başkasına", kime="ali@msg.org.tr", mid="<5@m>"),
        baslik("rapor: dünkü", mid="<6@m>", gun=bugun - timedelta(days=1)),
    ]
    notlar = servisler.notlari_maddele(mailler, BEN, bugun)
    assert [(m["kaynak"], m["metin"]) for m in notlar] == [
        ("not", "Köprü Film ile görüşüldü"), ("not", "MSG itirazı hazırlandı"),
        ("not", "MESAM'a dönüldü"), ("not", "IMRO listesi kontrol edildi"), ("not", "CRD indirildi"),
    ]
    assert len({m["id"] for m in notlar}) == 5 and all(len(m["id"]) <= 40 for m in notlar)
    assert notlar == servisler.notlari_maddele(mailler, BEN, bugun)  # aynı Message-ID → aynı id
    uzun = {**baslik("rapor:", mid="<7@m>"), "govde": "\n".join(f"satır {i}" for i in range(15))}
    assert len(servisler.notlari_maddele([uzun], BEN, bugun)) == 10


class SahteImap:
    """Gönderilmiş klasörü: başlık sorgusu ve tek mailin tam gövdesi."""
    mailler: list[EmailMessage] = []
    govde_istenen: list[str] = []

    def __init__(self, host, timeout=None):
        pass

    def login(self, kullanici, sifre):
        return "OK", [b""]

    def list(self):
        return "OK", [b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']

    def select(self, ad, readonly=False):
        return "OK", [b"1"]

    def search(self, charset, *kriter):
        return "OK", [" ".join(str(i + 1) for i in range(len(self.mailler))).encode()]

    def fetch(self, idler, sorgu):
        if sorgu == "(BODY.PEEK[])":
            SahteImap.govde_istenen.append(idler)
            ham = self.mailler[int(idler) - 1].as_bytes()
            return "OK", [(f"{idler} (BODY[] {{{len(ham)}}}".encode(), ham), b")"]
        assert "MESSAGE-ID" in sorgu
        parcalar = []
        for i, m in enumerate(self.mailler, start=1):
            b = m.as_bytes().split(b"\n\n", 1)[0] + b"\n\n"
            parcalar += [(f"{i} (BODY[HEADER.FIELDS (DATE SUBJECT FROM TO CC MESSAGE-ID)] {{{len(b)}}}".encode(), b), b")"]
        return "OK", parcalar

    def logout(self):
        return "BYE", []


def eposta(konu: str, kime=BEN, govde="", mid="<x@m>") -> EmailMessage:
    m = EmailMessage()
    m["Date"] = format_datetime(datetime.combine(servisler.istanbul_bugun(), time(9, 30), servisler.ISTANBUL))
    m["Subject"], m["From"], m["To"], m["Message-ID"] = konu, BEN, kime, mid
    m.set_content(govde)
    return m


def test_not_epostalari_bugunun_yapilanlarina_duser(monkeypatch):
    SahteImap.mailler = [
        eposta("rapor: Köprü Film ile görüşüldü", mid="<n1@m>"),
        eposta("Not:", govde="MESAM'a dönüldü\n\nIMRO listesi kontrol edildi\nCRD indirildi\n", mid="<n2@m>"),
        eposta("kendime hatırlatma", mid="<k@m>"),  # önek'siz kendine mail: atlanır
        eposta("Ağustos CRD", kime="ayse@msg.org.tr", mid="<g@m>"),
    ]
    SahteImap.govde_istenen = []
    monkeypatch.setattr(servisler.imaplib, "IMAP4_SSL", SahteImap)
    monkeypatch.setattr(servisler, "gmail_tara", GERCEK_GMAIL_TARA)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    uid = kullanici_olustur(gmail_kullanici=BEN, gmail_sifre_enc=guvenlik.sifrele("s"))
    c = istemci()

    d = c.get("/api/bugun?yenile=1").json()
    assert [m["metin"] for m in d["bulunan"]] == ["MSG'ye 'Ağustos CRD' konulu e-posta gönderildi"]
    assert d["sayim"] == {"eposta": 1, "medusa": 0}
    assert SahteImap.govde_istenen == ["2"]  # gövde yalnız önek-yalnız mail için okunur

    bugun = [m for m in c.get("/api/durum").json()["maddeler"] if m["tur"] == "bugun"]
    assert [(m["kaynak"], m["metin"], m["tikli"]) for m in bugun] == [
        ("not", "Köprü Film ile görüşüldü", True), ("not", "MESAM'a dönüldü", True),
        ("not", "IMRO listesi kontrol edildi", True), ("not", "CRD indirildi", True),
    ]
    assert all(m["kaynak_id"].startswith("not-") and m["kaynak_zaman"] for m in bugun)

    # ikinci tarama tekrar yazmaz; × ile kaldırılan geri gelmez
    assert c.delete(f"/api/maddeler/{bugun[3]['id']}").json() == {"ok": True}
    c.get("/api/bugun?yenile=1")
    with OturumYapici() as db:
        notlar = db.scalars(select(Madde).where(Madde.user_id == uid, Madde.kaynak == "not").order_by(Madde.sira)).all()
        assert [(n.metin, n.gizli) for n in notlar] == [
            ("Köprü Film ile görüşüldü", False), ("MESAM'a dönüldü", False),
            ("IMRO listesi kontrol edildi", False), ("CRD indirildi", True),
        ]
        assert db.scalar(select(func.count()).select_from(Madde).where(Madde.tur == "bulunan")) == 1

    # düzeltme paketine elle maddeler gibi girer; gizlenen girmez
    with OturumYapici() as db:
        paket = api.duzeltme_paketi(db, db.get(Kullanici, uid), servisler.istanbul_bugun())
        assert sorted(m.metin for m in paket if m.tur == "bugun") == ["IMRO listesi kontrol edildi", "Köprü Film ile görüşüldü", "MESAM'a dönüldü"]


def test_not_maddeleri_gonderildi_gruplamasina_girmez():
    bugun = servisler.istanbul_bugun()
    mailler = [baslik("rapor: Köprü Film ile görüşüldü"), baslik("not: x"), baslik("Liste", kime="ops@coverz.io")]
    assert [m["metin"] for m in servisler.epostalari_maddele(mailler, BEN, bugun)] == ["Coverz'e 'Liste' konulu e-posta gönderildi"]
