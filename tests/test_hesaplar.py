import json
import re
from datetime import datetime, time, timezone
from email.utils import format_datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import api
import app as uygulama
import guvenlik
import servisler
from veritabani import Kullanici, KullaniciAyari, Madde, OturumYapici, Temel, motor

SIFRE = "dogru-sifre-123"
# Fikstür tarayıcıları sahtesiyle değiştirmeden önceki gerçek fonksiyonlar.
GERCEK_GMAIL_TARA = servisler.gmail_tara
GERCEK_GITHUB_TARA = servisler.github_tara


@pytest.fixture(autouse=True)
def temiz_veritabani(monkeypatch):
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    api.onbellegi_temizle()
    # Ağa çıkılmasın: tarayıcılar varsayılan olarak boş döner.
    monkeypatch.setattr(servisler, "gmail_tara", lambda *a, **k: [])
    monkeypatch.setattr(servisler, "github_tara", lambda *a, **k: [])
    yield


def kullanici_olustur(eposta="a@ornek.com", ad="A", rol="uye", sifre=SIFRE, degistirmeli=False, aktif=True) -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad=ad, sifre_hash=guvenlik.sifre_ozeti(sifre), rol=rol,
                      aktif=aktif, sifre_degistirmeli=degistirmeli)
        db.add(k)
        db.commit()
        return k.id


def istemci() -> TestClient:
    return TestClient(uygulama.app, follow_redirects=False)


def giris(c: TestClient, eposta="a@ornek.com", sifre=SIFRE):
    return c.post("/giris", data={"eposta": eposta, "sifre": sifre})


# ---------------------------------------------------------------- giriş

def test_giris_ve_cikis():
    kullanici_olustur()
    with istemci() as c:
        r = giris(c, "A@Ornek.com ")
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
        assert c.get("/").status_code == 200
        assert c.get("/api/durum").json()["kullanici"]["ad"] == "A"
        r = c.post("/cikis")
        assert r.status_code == 303 and r.headers["location"] == "/giris"
        assert c.get("/api/durum").status_code == 401


def test_beni_hatirla_kalici_cerez():
    kullanici_olustur()
    with istemci() as c:
        r = c.post("/giris", data={"eposta": "a@ornek.com", "sifre": SIFRE, "hatirla": "1"})
        assert "max-age=2592000" in r.headers["set-cookie"].lower()


def test_yanlis_sifre_ve_pasif_kullanici():
    kullanici_olustur()
    kullanici_olustur("p@ornek.com", aktif=False)
    with istemci() as c:
        r = giris(c, sifre="yanlis")
        assert r.status_code == 401 and "E-posta veya şifre hatalı" in r.text
        assert "set-cookie" not in r.headers
        assert giris(c, "yok@ornek.com").status_code == 401
        assert giris(c, "p@ornek.com").status_code == 403
        assert c.get("/api/durum").status_code == 401


def test_oturumsuz_istekler():
    with istemci() as c:
        for yol in ("/", "/ayarlar", "/yonetim", "/sifre"):
            r = c.get(yol)
            assert r.status_code == 303 and r.headers["location"] == "/giris", yol
        for yontem, yol in (("GET", "/api/durum"), ("GET", "/api/bugun"), ("POST", "/api/maddeler"), ("PUT", "/api/ayarlar")):
            r = c.request(yontem, yol, json={})
            assert r.status_code == 401 and r.json()["detail"] == "Oturum açılmamış", yol
        assert c.get("/api/saglik").json() == {"ok": True, "son_hatirlat_ping": None}
        c.cookies.set("oturum", "sahte.imza")
        assert c.get("/api/durum").status_code == 401


def test_pasife_alinan_kullanicinin_oturumu_duser():
    uid = kullanici_olustur()
    with istemci() as c:
        giris(c)
        assert c.get("/api/durum").status_code == 200
        with OturumYapici() as db:
            db.get(Kullanici, uid).aktif = False
            db.commit()
        assert c.get("/api/durum").status_code == 401


# ---------------------------------------------------------------- yönetim

def test_uye_yonetim_uclarina_erisemez():
    uid = kullanici_olustur()
    with istemci() as c:
        giris(c)
        assert c.get("/yonetim").status_code == 403
        assert c.post("/yonetim/davet", data={"ad": "X", "eposta": "x@ornek.com"}).status_code == 403
        assert c.post(f"/yonetim/{uid}/sifirla").status_code == 403
        assert c.post(f"/yonetim/{uid}/aktiflik").status_code == 403
    with OturumYapici() as db:
        assert db.scalar(select(func.count()).select_from(Kullanici)) == 1


def test_davet_gecici_sifre_ve_sifre_zorlamasi():
    kullanici_olustur("admin@ornek.com", "Yönetici", rol="admin")
    with istemci() as yonetici, istemci() as yeni:
        giris(yonetici, "admin@ornek.com")
        r = yonetici.post("/yonetim/davet", data={"ad": "Deniz", "eposta": "Deniz@Ornek.com"})
        assert r.status_code == 200
        gecici = re.search(r'<code id="gecici">([^<]+)</code>', r.text).group(1)
        assert len(gecici) == 12
        # geçici şifre yalnız bu yanıtta görünür
        assert gecici not in yonetici.get("/yonetim").text
        assert yonetici.post("/yonetim/davet", data={"ad": "D2", "eposta": "deniz@ornek.com"}).status_code == 409

        r = giris(yeni, "deniz@ornek.com", gecici)
        assert r.headers["location"] == "/sifre"
        assert yeni.get("/").headers["location"] == "/sifre"
        assert yeni.get("/api/durum").status_code == 403
        assert yeni.get("/sifre").status_code == 200

        assert yeni.post("/sifre", data={"mevcut": gecici, "yeni": "kisa", "tekrar": "kisa"}).status_code == 400
        r = yeni.post("/sifre", data={"mevcut": gecici, "yeni": "yeni-sifre-uzun", "tekrar": "yeni-sifre-uzun"})
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert yeni.get("/").status_code == 200
        assert yeni.get("/api/durum").json()["kullanici"] == {"ad": "Deniz", "rol": "uye"}

    with istemci() as c:
        assert giris(c, "deniz@ornek.com", gecici).status_code == 401
        assert giris(c, "deniz@ornek.com", "yeni-sifre-uzun").headers["location"] == "/"


def test_sifre_sifirlama_ve_aktiflik():
    admin_id = kullanici_olustur("admin@ornek.com", "Yönetici", rol="admin")
    uid = kullanici_olustur()
    with istemci() as yonetici, istemci() as uye:
        giris(yonetici, "admin@ornek.com")
        giris(uye)
        r = yonetici.post(f"/yonetim/{uid}/sifirla")
        gecici = re.search(r'<code id="gecici">([^<]+)</code>', r.text).group(1)
        assert uye.get("/api/durum").status_code == 401  # eski oturum düştü
        assert giris(uye, sifre=gecici).headers["location"] == "/sifre"

        assert yonetici.post(f"/yonetim/{admin_id}/aktiflik").status_code == 400
        assert yonetici.post(f"/yonetim/{uid}/aktiflik").status_code == 303
        with OturumYapici() as db:
            assert db.get(Kullanici, uid).aktif is False
            assert db.get(Kullanici, admin_id).aktif is True


def test_ilk_yonetici_envden(monkeypatch):
    monkeypatch.setenv("ADMIN_EPOSTA", "Patron@Ornek.com")
    monkeypatch.setenv("ADMIN_SIFRE", SIFRE)
    with istemci() as c:  # lifespan çalışır
        assert giris(c, "patron@ornek.com").headers["location"] == "/"
        assert c.get("/yonetim").status_code == 200


# ---------------------------------------------------------------- kullanıcı ayrımı

def test_iki_kullanici_birbirinin_maddesine_erisemez():
    kullanici_olustur("a@ornek.com", "A")
    kullanici_olustur("b@ornek.com", "B")
    with istemci() as a, istemci() as b:
        giris(a, "a@ornek.com")
        giris(b, "b@ornek.com")
        madde = a.post("/api/maddeler", json={"tur": "surekli", "metin": "A'nın işi"}).json()
        a.post("/api/maddeler", json={"tur": "devam", "metin": "A devam", "asama": "bekleniyor"})
        b.post("/api/maddeler", json={"tur": "surekli", "metin": "B'nin işi"})

        assert b.patch(f"/api/maddeler/{madde['id']}", json={"tikli": False}).status_code == 404
        assert b.delete(f"/api/maddeler/{madde['id']}").status_code == 404
        assert b.post("/api/maddeler/sira", json={"tur": "surekli", "idler": [madde["id"]]}).status_code == 404

        assert [m["metin"] for m in a.get("/api/durum").json()["maddeler"]] == ["A devam", "A'nın işi"]
        assert [m["metin"] for m in b.get("/api/durum").json()["maddeler"]] == ["B'nin işi"]
        assert a.patch(f"/api/maddeler/{madde['id']}", json={"tikli": False}).json()["tikli"] is False


def test_bugun_metni_gun_basina_tek_satir_ve_siralama():
    kullanici_olustur()
    with istemci() as c:
        giris(c)
        ilk = c.post("/api/maddeler", json={"tur": "bugun", "kaynak": "yapilanlar", "metin": "a"}).json()
        ikinci = c.post("/api/maddeler", json={"tur": "bugun", "kaynak": "yapilanlar", "metin": "a\nb"}).json()
        assert ilk["id"] == ikinci["id"] and ikinci["metin"] == "a\nb"
        assert c.post("/api/maddeler", json={"tur": "bugun", "metin": "x"}).status_code == 422

        x = c.post("/api/maddeler", json={"tur": "surekli", "metin": "x"}).json()
        y = c.post("/api/maddeler", json={"tur": "surekli", "metin": "y"}).json()
        assert c.post("/api/maddeler/sira", json={"tur": "surekli", "idler": [y["id"], x["id"]]}).status_code == 200
        surekli = [m["metin"] for m in c.get("/api/durum").json()["maddeler"] if m["tur"] == "surekli"]
        assert surekli == ["y", "x"]


# ---------------------------------------------------------------- şifreli ayarlar

def test_fernet_gidis_donus():
    sifreli = guvenlik.sifrele("uygulama şifresi")
    assert sifreli != "uygulama şifresi"
    assert guvenlik.coz(sifreli) == "uygulama şifresi"
    assert guvenlik.coz("bozuk") == ""


def test_ayarlarda_sifre_ve_token_donmez_bos_alan_degistirmez():
    uid = kullanici_olustur()
    with istemci() as c:
        giris(c)
        r = c.put("/api/ayarlar", json={
            "gmail_kullanici": "Ben@Gmail.com", "gmail_sifre": "gmail-gizli-sifre",
            "github_token": "ghp_gizlitoken", "github_repo": "ben/proje", "proje_adi": "MEDUSA",
            "alan_sozlugu": "msg.org.tr=MSG\n\nornek.com = Örnek",
        })
        assert r.status_code == 200
        govde = r.json()
        assert govde["gmail_sifre_kayitli"] and govde["github_token_kayitli"]
        assert govde["alan_sozlugu"] == {"msg.org.tr": "MSG", "ornek.com": "Örnek"}
        for yanit in (r, c.get("/api/ayarlar"), c.get("/api/durum")):
            assert "gmail-gizli-sifre" not in yanit.text and "ghp_gizlitoken" not in yanit.text
            assert "_enc" not in yanit.text

        with OturumYapici() as db:
            a = db.get(KullaniciAyari, uid)
            enc_once = (a.gmail_sifre_enc, a.github_token_enc)
            assert "gmail-gizli-sifre" not in a.gmail_sifre_enc
            assert guvenlik.coz(a.github_token_enc) == "ghp_gizlitoken"

        r = c.put("/api/ayarlar", json={"gmail_sifre": "", "github_token": "   ", "rapor_basligi": "Rapor"})
        assert r.status_code == 200 and r.json()["rapor_basligi"] == "Rapor"
        with OturumYapici() as db:
            a = db.get(KullaniciAyari, uid)
            assert (a.gmail_sifre_enc, a.github_token_enc) == enc_once

        assert c.put("/api/ayarlar", json={"github_repo": "gecersiz"}).status_code == 422
        assert c.put("/api/ayarlar", json={"alan_sozlugu": "esittirsiz satir"}).status_code == 422


def test_ayar_girilmemisse_kaynak_atlanir():
    kullanici_olustur()
    with istemci() as c:
        giris(c)
        mesajlar = [h["mesaj"] for h in c.get("/api/bugun").json()["hatalar"]]
        assert mesajlar == ["Gmail ayarı girilmemiş (Ayarlar)", "GitHub ayarı girilmemiş (Ayarlar)"]
        assert c.post("/api/ayarlar/test").json()["sonuc"] == "Gmail: ayar girilmemiş · GitHub: ayar girilmemiş"


def test_baglanti_testi_kullanicinin_cozulmus_ayarlarini_kullanir(monkeypatch):
    kullanici_olustur()
    gorulen = {}
    monkeypatch.setattr(servisler, "gmail_test", lambda k, s: gorulen.update(gmail=(k, s)) or "Gmail: bağlandı, Gönderilmiş klasörü bulundu")

    def github_hatasi(token, repo):
        gorulen["github"] = (token, repo)
        raise servisler.KaynakHatasi("GitHub token'ı geçersiz veya süresi dolmuş")

    monkeypatch.setattr(servisler, "github_test", github_hatasi)
    with istemci() as c:
        giris(c)
        c.put("/api/ayarlar", json={"gmail_kullanici": "ben@gmail.com", "gmail_sifre": "s1", "github_token": "t1", "github_repo": "ben/proje"})
        r = c.post("/api/ayarlar/test").json()
    assert gorulen == {"gmail": ("ben@gmail.com", "s1"), "github": ("t1", "ben/proje")}
    assert r["sonuc"] == "Gmail: bağlandı, Gönderilmiş klasörü bulundu · GitHub: GitHub token'ı geçersiz veya süresi dolmuş"
    assert r["gmail_ok"] and not r["github_ok"]
    assert "s1" not in json.dumps(r) and "t1" not in json.dumps(r)


def test_github_test_commit_sayar():
    istemci_ = httpx.Client(transport=httpx.MockTransport(lambda istek: httpx.Response(200, json=[{}] * 12)))
    assert servisler.github_test("t", "ben/proje", istemci_) == "GitHub: 12 commit görüldü"


# ---------------------------------------------------------------- içe aktarma

ORNEK_V2 = {
    "recurring": [
        {"id": "a1", "text": "Yazışmalar takip edildi | Meslek birlikleriyle yazışıldı", "on": True},
        {"id": "a2", "text": "Coverz ekibine teknik destek verildi", "on": False},
        {"id": "a3", "text": "   ", "on": True},
    ],
    "ongoing": [{"id": "b1", "text": "MSG Ağustos itirazı", "stage": "yanıt bekleniyor", "on": True}],
    "daily": {"date": None, "done": "MSG'ye itiraz gönderildi", "plan": "Coverz listesi"},
    "settings": {"phone": "905551112233", "title": "Günlük Rapor – Ufuk"},
    "lastCopy": {"date": "2026-09-15", "time": "18:02"},
    "found": {"2026-09-15:abc": {"hidden": True}},
}


def test_ice_aktar_turlere_dagilir_ikinci_cagri_409():
    kullanici_olustur()
    veri = {**ORNEK_V2, "daily": {**ORNEK_V2["daily"], "date": servisler.istanbul_bugun().isoformat()}}
    with istemci() as c:
        giris(c)
        r = c.post("/api/ice-aktar", json=veri)
        assert r.status_code == 200 and r.json() == {"ok": True, "surekli": 2, "devam": 1, "bugun": 2}
        d = c.get("/api/durum").json()
        tur = lambda t: [(m["metin"], m["tikli"], m["asama"], m["kaynak"]) for m in d["maddeler"] if m["tur"] == t]  # noqa: E731
        assert tur("surekli") == [
            ("Yazışmalar takip edildi | Meslek birlikleriyle yazışıldı", True, "", None),
            ("Coverz ekibine teknik destek verildi", False, "", None),
        ]
        assert tur("devam") == [("MSG Ağustos itirazı", True, "yanıt bekleniyor", None)]
        assert sorted(tur("bugun")) == [("Coverz listesi", True, "", "yarin"), ("MSG'ye itiraz gönderildi", True, "", "elle")]
        assert d["ayarlar"]["patron_telefon"] == "905551112233"
        assert d["ayarlar"]["rapor_basligi"] == "Günlük Rapor – Ufuk"
        assert c.post("/api/ice-aktar", json=veri).status_code == 409


def test_ice_aktar_eski_gunun_metinlerini_almaz():
    kullanici_olustur()
    with istemci() as c:
        giris(c)
        r = c.post("/api/ice-aktar", json={**ORNEK_V2, "daily": {**ORNEK_V2["daily"], "date": "2020-01-01"}})
        assert r.json()["bugun"] == 0


def test_ice_aktar_bulunan_ve_bugun_satirlari_engellemez():
    uid = kullanici_olustur()
    tarih = servisler.istanbul_bugun()
    with OturumYapici() as db:
        for i in range(3):
            db.add(Madde(user_id=uid, tur="bulunan", metin=f"bulunan {i}", tarih=tarih, kaynak="medusa", kaynak_id=f"k{i}", sira=i))
        db.add(Madde(user_id=uid, tur="bugun", metin="elle yazılan", tarih=tarih, kaynak="yapilanlar", kaynak_id="yapilanlar"))
        db.commit()
    veri = {**ORNEK_V2, "daily": {**ORNEK_V2["daily"], "date": tarih.isoformat()}}
    with istemci() as c:
        giris(c)
        r = c.post("/api/ice-aktar", json=veri)
        assert r.status_code == 200 and r.json() == {"ok": True, "surekli": 2, "devam": 1, "bugun": 1}
        d = c.get("/api/durum").json()["maddeler"]
        assert [m["metin"] for m in d if m["tur"] == "surekli"] == [
            "Yazışmalar takip edildi | Meslek birlikleriyle yazışıldı", "Coverz ekibine teknik destek verildi",
        ]
        assert [m["metin"] for m in d if m["tur"] == "bulunan"] == ["bulunan 0", "bulunan 1", "bulunan 2"]
        assert sorted((m["kaynak"], m["metin"]) for m in d if m["tur"] == "bugun") == [
            ("elle", "elle yazılan"), ("yarin", "Coverz listesi"),
        ]
        assert c.post("/api/ice-aktar", json=veri).status_code == 409


# ---------------------------------------------------------------- bulunanlar

def test_bulunan_mukerrer_yazilmaz_duzenleme_korunur(monkeypatch):
    uid = kullanici_olustur()
    bulunanlar = [servisler.madde("medusa", "Add trigram index"), servisler.madde("medusa", "Fix N+1")]
    cagrilar = []
    monkeypatch.setattr(servisler, "github_tara", lambda token, repo, bugun: cagrilar.append((token, repo)) or list(bulunanlar))
    with istemci() as c:
        giris(c)
        c.put("/api/ayarlar", json={"github_token": "t", "github_repo": "ben/proje"})
        ilk = c.get("/api/bugun?yenile=1").json()
        assert [m["metin"] for m in ilk["bulunan"]] == ["Add trigram index", "Fix N+1"]
        assert ilk["sayim"] == {"eposta": 0, "medusa": 2}

        duzenlenen = ilk["bulunan"][0]
        c.patch(f"/api/maddeler/{duzenlenen['id']}", json={"metin": "İndeks eklendi", "gizli": True})

        bulunanlar.append(servisler.madde("medusa", "Yeni commit"))
        ikinci = c.get("/api/bugun?yenile=1").json()
        assert [(m["metin"], m["gizli"]) for m in ikinci["bulunan"]] == [
            ("İndeks eklendi", True), ("Fix N+1", False), ("Yeni commit", False),
        ]
        # önbellekten döner, yeniden taramaz
        assert len(c.get("/api/bugun").json()["bulunan"]) == 3
        assert cagrilar == [("t", "ben/proje")] * 2

    with OturumYapici() as db:
        assert db.scalar(select(func.count()).select_from(Madde).where(Madde.user_id == uid, Madde.tur == "bulunan")) == 3


def test_claude_yalniz_yeni_maddeleri_ceviri(monkeypatch):
    kullanici_olustur()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    commitler = [servisler.madde("medusa", "a")]
    gonderilen = []

    def sahte_ceviri(maddeler, anahtar, istemci=None, proje_adi=""):
        gonderilen.append(([m["metin"] for m in maddeler], proje_adi))
        return [{**m, "metin": m["metin"].upper()} for m in maddeler], None

    monkeypatch.setattr(servisler, "github_tara", lambda *a: list(commitler))
    monkeypatch.setattr(servisler, "claude_cevir", sahte_ceviri)
    with istemci() as c:
        giris(c)
        c.put("/api/ayarlar", json={"github_token": "t", "github_repo": "ben/proje", "proje_adi": "MEDUSA"})
        c.get("/api/bugun?yenile=1")
        commitler.append(servisler.madde("medusa", "b"))
        bulunan = c.get("/api/bugun?yenile=1").json()["bulunan"]
    assert gonderilen == [(["a"], "MEDUSA"), (["b"], "MEDUSA")]
    # çeviri metin_ai'ye yazılır, metin ham kalır
    assert [(m["metin"], m["metin_ai"], m["rapor_metni"]) for m in bulunan] == [("a", "A", "A"), ("b", "B", "B")]


def test_claude_prompt_proje_adi():
    def istemci_(yakalanan):
        def isleyici(istek):
            yakalanan.update(json.loads(istek.content))
            return httpx.Response(200, json={"content": [{"type": "text", "text": "[]"}], "stop_reason": "end_turn"})
        return httpx.Client(transport=httpx.MockTransport(isleyici))

    m = [servisler.madde("medusa", "Fix bug")]
    ile, ilesiz = {}, {}
    servisler.claude_cevir(m, "k", istemci_(ile), proje_adi="ATLAS")
    servisler.claude_cevir(m, "k", istemci_(ilesiz))
    assert "ürün adı her zaman ATLAS" in ile["system"] and "ATLAS" in ile["messages"][0]["content"]
    assert "MEDUSA" not in json.dumps(ilesiz, ensure_ascii=False) and "ürün adı" not in ilesiz["system"]


# ---------------------------------------------------------------- R5: kaynağın saati

class SahteImap:
    """imaplib.IMAP4_SSL yerine: Gönderilmiş klasöründe verilen başlıklarla mailler döner."""
    basliklar: list[bytes] = []

    def __init__(self, host, timeout=None):
        pass

    def login(self, kullanici, sifre):
        return "OK", [b"giris"]

    def list(self):
        return "OK", [b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']

    def select(self, ad, readonly=False):
        return "OK", [str(len(self.basliklar)).encode()]

    def search(self, charset, *kriter):
        return "OK", [" ".join(str(i + 1) for i in range(len(self.basliklar))).encode()]

    def fetch(self, idler, sorgu):
        parcalar = []
        for i, b in enumerate(self.basliklar, start=1):
            parcalar += [(f"{i} (BODY[HEADER.FIELDS (DATE SUBJECT FROM TO CC)] {{{len(b)}}}".encode(), b), b")"]
        return "OK", parcalar

    def logout(self):
        return "BYE", []


def _baslik(zaman: datetime, kime: str, konu: str) -> bytes:
    return (f"Date: {format_datetime(zaman)}\r\nSubject: {konu}\r\nFrom: a@ornek.com\r\nTo: {kime}\r\n\r\n").encode()


def test_bulunanlara_eposta_ve_commit_saati_yazilir(monkeypatch):
    uid = kullanici_olustur()
    bugun = servisler.istanbul_bugun()
    ist = lambda s, d: datetime.combine(bugun, time(s, d), servisler.ISTANBUL)  # noqa: E731
    SahteImap.basliklar = [
        _baslik(ist(9, 12), "ayse@msg.org.tr", "A"),
        _baslik(ist(14, 37).astimezone(timezone.utc), "crd@msg.org.tr", "B"),  # UTC başlık, grupta en son
        _baslik(ist(11, 5), "x@imro.ie", "C"),
    ]
    monkeypatch.setattr(servisler.imaplib, "IMAP4_SSL", SahteImap)
    monkeypatch.setattr(servisler, "gmail_tara", GERCEK_GMAIL_TARA)

    def commitler(istek):
        return httpx.Response(200, json=[
            {"commit": {"message": "Rapor ekranı hızlandı", "author": {"date": f"{bugun.isoformat()}T07:05:00Z"}}},
        ])
    monkeypatch.setattr(servisler, "github_tara", lambda t, r, b: GERCEK_GITHUB_TARA(
        t, r, b, httpx.Client(transport=httpx.MockTransport(commitler))))

    with istemci() as c:
        giris(c)
        c.put("/api/ayarlar", json={"gmail_kullanici": "a@ornek.com", "gmail_sifre": "s", "github_token": "t", "github_repo": "ben/proje"})
        bulunan = c.get("/api/bugun?yenile=1").json()["bulunan"]

    saat = lambda z: datetime.fromisoformat(z).astimezone(servisler.ISTANBUL).strftime("%H:%M")  # noqa: E731
    assert [(m["kaynak"], m["metin"][:4], saat(m["kaynak_zaman"])) for m in bulunan] == [
        ("eposta", "MSG'", "14:37"), ("eposta", "IMRO", "11:05"), ("medusa", "Rapo", "10:05"),
    ]
    with OturumYapici() as db:
        assert all(m.kaynak_zaman is not None for m in db.scalars(select(Madde).where(Madde.user_id == uid)))
