"""G1: Google ile bağlan — OAuth (state + PKCE), Gmail REST, Takvim, Drive, test modu yenileme uyarısı.
sqlite; Google'a giden her istek httpx.MockTransport'taki sahte sunucuya gider, ağa çıkılmaz."""
import base64
import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import api
import app as uygulama
import guvenlik
import kimlik
import servisler
from veritabani import Kullanici, KullaniciAyari, Madde, OturumYapici, PushAbonelik, Temel, motor

BEN = "ufuk@ilsvision.com"
SIFRE = "dogru-sifre-123"
ISTEMCI_ID = "test-istemci.apps.googleusercontent.com"
ISTEMCI_SIRRI = "GOCSPX-test-sirri"
REFRESH = "1//yenileme-gizli-deger"
ERISIM = "ya29.erisim-gizli-deger"
TUM_KAPSAMLAR = ["openid", "https://www.googleapis.com/auth/userinfo.email", *servisler.GOOGLE_KAPSAMLARI.values()]
GUN = date(2026, 9, 16)  # Çarşamba; API testlerinde "bugün"
IST = servisler.ISTANBUL


def id_token(eposta=BEN, aud=ISTEMCI_ID) -> str:
    parca = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{parca({'alg': 'RS256'})}.{parca({'aud': aud, 'email': eposta, 'email_verified': True})}.imza"


def istanbul(gun: date, ss: int, dd: int = 0) -> datetime:
    return datetime.combine(gun, time(ss, dd), IST)


def iso(z: datetime) -> str:
    return z.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class SahteGoogle:
    """Token, revoke, Gmail, Takvim ve Drive uçlarını taklit eder; gelen istekleri saklar."""

    def __init__(self):
        self.istekler: list[httpx.Request] = []
        self.kapsamlar = list(TUM_KAPSAMLAR)
        self.yenile_hatasi: str | None = None
        self.iptal_kodu = 200
        self.yenileme_sayisi = 0
        self.mesajlar: dict[str, dict] = {}  # id → {"basliklar": {...}, "raw": str}
        self.sayfalar: list[list[str]] = [[]]
        self.etkinlikler: list[dict] = []
        self.dosyalar: list[dict] = []
        self.takvim_kodu = 200

    def istemci(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def yollar(self, parca: str) -> list[httpx.Request]:
        return [i for i in self.istekler if parca in str(i.url)]

    def __call__(self, istek: httpx.Request) -> httpx.Response:
        self.istekler.append(istek)
        adres = f"{istek.url.scheme}://{istek.url.host}{istek.url.path}"
        q = istek.url.params
        if adres == servisler.GOOGLE_TOKEN_URL:
            form = dict(parse_qsl(istek.content.decode()))
            if form["grant_type"] == "authorization_code":
                return httpx.Response(200, json={
                    "access_token": ERISIM, "expires_in": 3599, "refresh_token": REFRESH, "token_type": "Bearer",
                    "scope": " ".join(self.kapsamlar), "id_token": id_token(),
                })
            self.yenileme_sayisi += 1
            if self.yenile_hatasi:
                return httpx.Response(400, json={"error": self.yenile_hatasi, "error_description": "Token has been expired or revoked."})
            return httpx.Response(200, json={"access_token": f"{ERISIM}-{self.yenileme_sayisi}", "expires_in": 3599})
        if adres == servisler.GOOGLE_IPTAL_URL:
            return httpx.Response(self.iptal_kodu)
        if adres == f"{servisler.GMAIL_API}/messages":
            sira = int(q.get("pageToken") or 0)
            govde = {"messages": [{"id": x, "threadId": x} for x in self.sayfalar[sira]]}
            if sira + 1 < len(self.sayfalar):
                govde["nextPageToken"] = str(sira + 1)
            return httpx.Response(200, json=govde)
        if adres.startswith(f"{servisler.GMAIL_API}/messages/"):
            m = self.mesajlar[adres.rsplit("/", 1)[1]]
            if q.get("format") == "raw":
                return httpx.Response(200, json={"raw": m["raw"]})
            istenen = q.get_list("metadataHeaders")
            return httpx.Response(200, json={"payload": {"headers": [
                {"name": ad, "value": d} for ad, d in m["basliklar"].items() if ad in istenen]}})
        if adres == servisler.TAKVIM_API:
            if self.takvim_kodu != 200:
                return httpx.Response(self.takvim_kodu, json={"error": {"message": "Backend Error"}})
            return httpx.Response(200, json={"items": self.etkinlikler})
        if adres == servisler.DRIVE_API:
            return httpx.Response(200, json={"files": self.dosyalar})
        return httpx.Response(404, json={"error": {"message": "yok"}})


IMAP_CAGRILARI: list = []


@pytest.fixture(autouse=True)
def ortam(monkeypatch):
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    api.onbellegi_temizle()
    api._google_erisim.clear()
    IMAP_CAGRILARI.clear()
    monkeypatch.setenv("GOOGLE_CLIENT_ID", ISTEMCI_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", ISTEMCI_SIRRI)
    monkeypatch.delenv("GOOGLE_TEST_MODU", raising=False)
    monkeypatch.setenv("APP_URL", "https://rapor.ornek.com")
    monkeypatch.setattr(servisler, "gmail_tara", lambda *a, **k: IMAP_CAGRILARI.append(a) or [])
    monkeypatch.setattr(servisler, "github_tara", lambda *a, **k: [])
    yield


@pytest.fixture
def google(monkeypatch):
    sahte = SahteGoogle()
    monkeypatch.setattr(servisler, "google_istemci", sahte.istemci)
    return sahte


@pytest.fixture
def gun(monkeypatch):
    """API'de "bugün" GUN olur; o günün her toplantısı bitmiş sayılır."""
    monkeypatch.setattr(api, "bugun", lambda: GUN)
    return GUN


def kullanici_olustur(eposta=BEN, sifre_gmail=False, **ayar) -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad="Ufuk", sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol="uye", aktif=True,
                      sifre_degistirmeli=False)
        db.add(k)
        db.commit()
        a = KullaniciAyari(user_id=k.id, kurulum_tamam=True, **ayar)
        if sifre_gmail:
            a.gmail_kullanici, a.gmail_sifre_enc = "ufuk@gmail.com", guvenlik.sifrele("uyg-sifre")
        db.add(a)
        db.commit()
        return k.id


def google_bagla(uid: int, kapsamlar=None, baglanti: datetime | None = None, durum="bagli", kaynaklar=None) -> None:
    with OturumYapici() as db:
        a = db.get(KullaniciAyari, uid)
        a.google_refresh_enc = guvenlik.sifrele(REFRESH)
        a.google_eposta = BEN
        a.google_baglanti = baglanti or datetime.now(timezone.utc)
        a.google_durum = durum
        a.google_kapsamlar = list(TUM_KAPSAMLAR if kapsamlar is None else kapsamlar)
        a.kaynaklar = kaynaklar or {"gmail": True, "github": False, "medusa": False, "takvim": True, "drive": True}
        db.commit()


def ayar(uid: int) -> KullaniciAyari:
    with OturumYapici() as db:
        return db.get(KullaniciAyari, uid)


def istemci(eposta=BEN) -> TestClient:
    c = TestClient(uygulama.app, follow_redirects=False)
    c.__enter__()
    assert c.post("/giris", data={"eposta": eposta, "sifre": SIFRE}).status_code == 303
    return c


def basla(c: TestClient, donus="/ayarlar") -> dict:
    y = c.get(f"/oauth/google/basla?donus={donus}")
    assert y.status_code == 303
    return {k: v[0] for k, v in parse_qs(urlsplit(y.headers["location"]).query).items()}


def yonlendirme(y) -> tuple[str, dict]:
    parca = urlsplit(y.headers["location"])
    return parca.path, {k: v[0] for k, v in parse_qs(parca.query).items()}


def bulunanlar(uid: int, kaynak: str | None = None, tarih=GUN) -> list[Madde]:
    with OturumYapici() as db:
        sorgu = select(Madde).where(Madde.user_id == uid, Madde.tur == "bulunan", Madde.tarih == tarih)
        if kaynak:
            sorgu = sorgu.where(Madde.kaynak == kaynak)
        return db.scalars(sorgu.order_by(Madde.id)).all()


# ---------------------------------------------------------------- 1) OAuth: başla, state, PKCE, dönüş

def test_basla_yetki_adresi_state_ve_pkce(google):
    uid = kullanici_olustur()
    c = istemci()
    p = basla(c, "/kurulum")
    assert p["client_id"] == ISTEMCI_ID and p["response_type"] == "code"
    assert p["redirect_uri"] == "https://rapor.ornek.com/oauth/google/geri"
    assert p["scope"] == ("openid email https://www.googleapis.com/auth/gmail.readonly "
                          "https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/drive.metadata.readonly")
    assert (p["access_type"], p["prompt"], p["include_granted_scopes"], p["login_hint"]) == ("offline", "consent", "true", BEN)
    assert p["code_challenge_method"] == "S256"
    veri = kimlik.google_state_coz(p["state"])
    assert veri["u"] == uid and veri["d"] == "/kurulum"
    pkce = kimlik._google_imzaci.loads(c.cookies.get(kimlik.PKCE_CEREZ), salt="pkce")
    assert pkce["n"] == veri["n"]
    assert p["code_challenge"] == base64.urlsafe_b64encode(hashlib.sha256(pkce["v"].encode()).digest()).rstrip(b"=").decode()
    assert pkce["v"] not in json.dumps(p)  # verifier Google'a giden adreste yok


@pytest.mark.parametrize("donus, beklenen", [
    ("/kurulum", "/kurulum"), ("/ayarlar", "/ayarlar"), ("https://kotu.ornek.com", "/ayarlar"), ("//kotu.ornek.com", "/ayarlar"),
    ("/yonetim", "/ayarlar"), ("", "/ayarlar"),
])
def test_donus_beyaz_listesi(google, donus, beklenen):
    kullanici_olustur()
    assert kimlik.google_state_coz(basla(istemci(), donus)["state"])["d"] == beklenen


def test_localhost_yonlendirmesi(google):
    kullanici_olustur()
    c = TestClient(uygulama.app, base_url="http://localhost:8765", follow_redirects=False)
    c.__enter__()
    c.post("/giris", data={"eposta": BEN, "sifre": SIFRE})
    assert basla(c)["redirect_uri"] == "http://localhost:8765/oauth/google/geri"


def test_geri_basarili_token_fernetle_saklanir(google):
    uid = kullanici_olustur()
    c = istemci()
    p = basla(c, "/kurulum")
    y = c.get("/oauth/google/geri", params={"code": "4/kod", "state": p["state"]})
    assert yonlendirme(y) == ("/kurulum", {"google": "bagli"})
    form = dict(parse_qsl(google.yollar("oauth2.googleapis.com/token")[0].content.decode()))
    assert form["grant_type"] == "authorization_code" and form["code"] == "4/kod"
    assert form["redirect_uri"] == p["redirect_uri"] and form["client_secret"] == ISTEMCI_SIRRI
    assert base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode() == p["code_challenge"]
    assert c.cookies.get(kimlik.PKCE_CEREZ) is None  # çerez dönüşte silinir
    a = ayar(uid)
    assert a.google_refresh_enc and REFRESH not in a.google_refresh_enc and guvenlik.coz(a.google_refresh_enc) == REFRESH
    assert (a.google_eposta, a.google_durum) == (BEN, "bagli") and a.google_baglanti is not None
    assert a.google_kapsamlar == TUM_KAPSAMLAR
    assert a.kaynaklar == {"gmail": True, "github": False, "medusa": False, "takvim": True, "drive": True}
    assert api._google_erisim[uid][0] == ERISIM  # access token yalnız bellekte


def test_verilmeyen_kapsam_kapali_ve_izin_verilmedi(google):
    uid = kullanici_olustur(sifre_gmail=True)
    google.kapsamlar = ["openid", "https://www.googleapis.com/auth/userinfo.email", servisler.GOOGLE_KAPSAMLARI["takvim"]]
    c = istemci()
    c.get("/oauth/google/geri", params={"code": "k", "state": basla(c)["state"]})
    g = c.get("/api/ayarlar").json()
    assert g["kaynaklar"] == {"gmail": True, "github": False, "medusa": False, "takvim": True, "drive": False}
    assert g["google"]["kapsamlar"] == {"gmail": False, "takvim": True, "drive": False}
    # drive izni yokken açılmaya çalışılsa da kapalı kalır
    assert c.put("/api/ayarlar", json={"kaynaklar": {"drive": True}}).json()["kaynaklar"]["drive"] is False
    assert ayar(uid).kaynaklar["gmail"] is True  # uygulama şifreli Gmail seçimi korunur


def test_sahte_state_reddedilir(google):
    kullanici_olustur()
    c = istemci()
    basla(c)
    yol, q = yonlendirme(c.get("/oauth/google/geri", params={"code": "k", "state": "sahte.state.degeri"}))
    assert yol == "/ayarlar" and q["google"] == "hata" and q["neden"]
    assert not google.yollar("oauth2.googleapis.com/token")


def test_suresi_gecmis_state_reddedilir(google, monkeypatch):
    kullanici_olustur()
    c = istemci()
    p = basla(c, "/kurulum")
    monkeypatch.setattr(kimlik, "GOOGLE_STATE_SURESI", -1)
    yol, q = yonlendirme(c.get("/oauth/google/geri", params={"code": "k", "state": p["state"]}))
    assert (yol, q["google"]) == ("/ayarlar", "hata") and "süresi" in q["neden"]
    assert not google.yollar("oauth2.googleapis.com/token")


def test_pkce_cerezi_yoksa_ya_da_baska_akisinsa_reddedilir(google):
    kullanici_olustur()
    c = istemci()
    p1 = basla(c)
    basla(c)  # ikinci akış çerezi değiştirir: ilk state'in nonce'u artık eşleşmez
    assert yonlendirme(c.get("/oauth/google/geri", params={"code": "k", "state": p1["state"]}))[1]["google"] == "hata"
    p3 = basla(c)
    c.cookies.delete(kimlik.PKCE_CEREZ, path="/oauth/google")
    assert yonlendirme(c.get("/oauth/google/geri", params={"code": "k", "state": p3["state"]}))[1]["google"] == "hata"
    assert not google.yollar("oauth2.googleapis.com/token")


def test_baska_kullanicinin_state_i_reddedilir(google):
    kullanici_olustur()
    kullanici_olustur("baska@ornek.com")
    state = basla(istemci())["state"]
    b = istemci("baska@ornek.com")
    basla(b)
    yol, q = yonlendirme(b.get("/oauth/google/geri", params={"code": "k", "state": state}))
    assert q["google"] == "hata" and "Oturum" in q["neden"]


def test_iptal_ve_google_hatasi(google):
    uid = kullanici_olustur()
    c = istemci()
    yol, q = yonlendirme(c.get("/oauth/google/geri", params={"error": "access_denied", "state": basla(c, "/kurulum")["state"]}))
    assert (yol, q) == ("/kurulum", {"google": "hata", "neden": "İzin verilmedi"})
    assert ayar(uid).google_refresh_enc is None


def test_oturumsuz_basla_girise_yonlenir(google):
    y = TestClient(uygulama.app, follow_redirects=False).get("/oauth/google/basla")
    assert y.status_code == 303 and y.headers["location"] == "/giris"


# ---------------------------------------------------------------- 2) gizlilik

def test_token_hicbir_yanitta_sizmiyor(google, gun):
    uid = kullanici_olustur()
    c = istemci()
    c.get("/oauth/google/geri", params={"code": "k", "state": basla(c)["state"]})
    enc = ayar(uid).google_refresh_enc
    yanitlar = [c.get(y).text for y in ("/api/ayarlar", "/api/durum", "/api/bugun?yenile=1", "/api/kurulum", "/ayarlar", "/kurulum", "/")]
    yanitlar.append(c.post("/api/ayarlar/test?kaynak=gmail").text)
    yanitlar.append(c.post("/oauth/google/kaldir").text)
    for metin in yanitlar:
        for gizli in (REFRESH, ERISIM, enc, ISTEMCI_SIRRI):
            assert gizli not in metin


# ---------------------------------------------------------------- 3) access token önbelleği ve invalid_grant

def test_access_token_onbellekte_suresi_dolunca_yenilenir(google, gun):
    uid = kullanici_olustur()
    google_bagla(uid)
    c = istemci()
    c.get("/api/bugun?yenile=1")
    c.get("/api/bugun?yenile=1")
    assert google.yenileme_sayisi == 1
    api._google_erisim[uid] = (api._google_erisim[uid][0], 0)  # süresi doldu
    c.get("/api/bugun?yenile=1")
    assert google.yenileme_sayisi == 2
    yetki = {i.headers["authorization"] for i in google.yollar("googleapis.com/calendar")}
    assert yetki == {f"Bearer {ERISIM}-1", f"Bearer {ERISIM}-2"}


def test_invalid_grant_yenile_durumu_google_kaynaklari_atlanir(google, gun):
    uid = kullanici_olustur()
    google_bagla(uid)
    google.yenile_hatasi = "invalid_grant"
    c = istemci()
    d = c.get("/api/bugun?yenile=1").json()
    assert {"kaynak": "google", "mesaj": "Google bağlantısı yenilenmeli"} in d["hatalar"]
    assert ayar(uid).google_durum == "yenile"
    assert not google.yollar("gmail.googleapis.com") and not google.yollar("calendar") and not google.yollar("drive")
    assert IMAP_CAGRILARI == []  # uygulama şifresi yok: Gmail de atlanır
    assert not any(h["mesaj"].startswith("Gmail ayarı girilmemiş") for h in d["hatalar"])
    # ikinci taramada refresh bir daha denenmez
    c.get("/api/bugun?yenile=1")
    assert google.yenileme_sayisi == 1
    assert c.get("/api/ayarlar").json()["google"]["uyari"]["tur"] == "doldu"


def test_yenile_durumunda_uygulama_sifresiyle_imape_duser(google, gun):
    uid = kullanici_olustur(sifre_gmail=True)
    google_bagla(uid, durum="yenile")
    d = istemci().get("/api/bugun?yenile=1").json()
    assert len(IMAP_CAGRILARI) == 1 and IMAP_CAGRILARI[0][0] == "ufuk@gmail.com"
    assert {"kaynak": "google", "mesaj": "Google bağlantısı yenilenmeli"} in d["hatalar"]
    assert not google.yollar("gmail.googleapis.com")


def test_google_varken_imap_cagrilmaz(google, gun):
    uid = kullanici_olustur(sifre_gmail=True)
    google_bagla(uid)
    istemci().get("/api/bugun?yenile=1")
    assert IMAP_CAGRILARI == [] and google.yollar("gmail.googleapis.com/gmail/v1/users/me/messages")


def test_ayarlar_testi_google_gmail(google):
    uid = kullanici_olustur()
    google_bagla(uid)
    t = istemci().post("/api/ayarlar/test?kaynak=gmail").json()
    assert t["gmail_ok"] is True and t["sonuc"] == f"Gmail: Google hesabıyla bağlı ({BEN})"


# ---------------------------------------------------------------- 4) bağlantıyı kaldır

@pytest.mark.parametrize("uygulama_sifresi, iptal_kodu", [(True, 200), (False, 400)])
def test_kaldir_revoke_cagirir_ve_alanlari_siler(google, uygulama_sifresi, iptal_kodu):
    uid = kullanici_olustur(sifre_gmail=uygulama_sifresi)
    google_bagla(uid)
    api._google_erisim[uid] = (ERISIM, 9e12)
    google.iptal_kodu = iptal_kodu  # Google hata verse de bağlantı silinir
    y = istemci().post("/oauth/google/kaldir")
    assert y.status_code == 200
    iptal = google.yollar("oauth2.googleapis.com/revoke")
    assert len(iptal) == 1 and dict(parse_qsl(iptal[0].content.decode())) == {"token": REFRESH}
    a = ayar(uid)
    assert (a.google_refresh_enc, a.google_eposta, a.google_baglanti, a.google_durum, a.google_kapsamlar) == (None,) * 5
    assert a.kaynaklar["takvim"] is False and a.kaynaklar["drive"] is False
    assert a.kaynaklar["gmail"] is uygulama_sifresi  # uygulama şifresi varsa Gmail IMAP ile sürer
    assert uid not in api._google_erisim
    assert y.json()["google"]["bagli"] is False


# ---------------------------------------------------------------- 5) test modu: 7 gün, ≤ 24 saat uyarısı, dolmuş

def test_yedi_gun_ve_uyarilar(monkeypatch):
    uid = kullanici_olustur()
    simdi = datetime.now(timezone.utc)
    for once, beklenen in [(timedelta(days=5), None), (timedelta(days=6, hours=1), "yakinda"),
                           (timedelta(days=7, minutes=1), "doldu")]:
        google_bagla(uid, baglanti=simdi - once)
        a = ayar(uid)
        assert api.google_bitis(a) == api.utc(a.google_baglanti) + timedelta(days=7)
        uyari = api.google_uyari(a, simdi)
        assert (uyari or {}).get("tur") == beklenen
    assert api.google_uyari(ayar(uid), simdi) == {"tur": "doldu", "metin": "Google bağlantısı yenilenmeli",
                                                  "eylem": "Yeniden bağlan", "adres": "/oauth/google/basla"}
    google_bagla(uid, baglanti=simdi - timedelta(days=6, hours=1))
    assert api.google_uyari(ayar(uid), simdi)["metin"] == "Google bağlantın yarın yenilenmeli"
    assert api.google_uyari(ayar(uid), simdi)["eylem"] == "Şimdi yenile"

    monkeypatch.setenv("GOOGLE_TEST_MODU", "0")  # yayın modu: bitiş yok, uyarı yok
    for once in (timedelta(days=6, hours=1), timedelta(days=30)):
        google_bagla(uid, baglanti=simdi - once)
        assert api.google_bitis(ayar(uid)) is None and api.google_uyari(ayar(uid), simdi) is None
    google_bagla(uid, durum="yenile")  # gerçek invalid_grant yayın modunda da uyarır
    assert api.google_uyari(ayar(uid), simdi)["tur"] == "doldu"


def test_gecerlilik_metni_ve_ayarlar_ozeti(monkeypatch):
    uid = kullanici_olustur()
    google_bagla(uid, baglanti=datetime(2026, 9, 19, 11, 5, tzinfo=timezone.utc))  # 14:05 İstanbul
    g = api.google_ozeti(ayar(uid))
    assert g["gecerlilik"] == "Bağlantı 26 Eyl 14:05'e kadar geçerli"
    assert (g["bagli"], g["eposta"], g["durum"], g["test_modu"]) == (True, BEN, "bagli", True)
    assert [servisler.saat_yonelme(s) for s in ("09:30", "12:00", "18:06", "10:40", "00:00")] == [
        "09:30'a", "12:00'ye", "18:06'ya", "10:40'a", "00:00'a"]
    monkeypatch.setenv("GOOGLE_TEST_MODU", "0")
    assert api.google_ozeti(ayar(uid))["gecerlilik"] == ""


def test_bugun_seridi_durumda(google, gun):
    uid = kullanici_olustur()
    google_bagla(uid, baglanti=datetime.now(timezone.utc) - timedelta(days=6, hours=2))
    c = istemci()
    assert c.get("/api/durum").json()["ayarlar"]["google"]["uyari"]["tur"] == "yakinda"
    assert 'id="googleSerit"' in c.get("/").text
    google_bagla(uid, durum="yenile")
    uyari = c.get("/api/durum").json()["ayarlar"]["google"]["uyari"]
    assert (uyari["tur"], uyari["metin"], uyari["eylem"]) == ("doldu", "Google bağlantısı yenilenmeli", "Yeniden bağlan")


class SahtePush:
    def __init__(self):
        self.gonderilen: list[dict] = []

    def __call__(self, abonelik, veri):
        self.gonderilen.append(veri)
        return None, ""


@pytest.mark.parametrize("durum, once, beklenen", [
    ("bagli", timedelta(days=6, hours=10), "Google bağlantın yarın yenilenmeli · Şimdi yenile"),
    ("yenile", timedelta(days=1), "Google bağlantısı yenilenmeli · Yeniden bağlan"),
    ("bagli", timedelta(days=2), None),
])
def test_hatirlatma_epostasi_ve_push_uyarisi(google, monkeypatch, durum, once, beklenen):
    an = datetime.now(IST).replace(hour=17, minute=0) if datetime.now(IST).hour < 17 else datetime.now(IST)
    monkeypatch.setattr(api, "bugun", lambda: an.date())
    uid = kullanici_olustur(hatirlatma_gunler="1,2,3,4,5,6,7")
    google_bagla(uid, baglanti=an.astimezone(timezone.utc) - once, durum=durum)
    with OturumYapici() as db:
        db.add(PushAbonelik(user_id=uid, endpoint="https://push.ornek.com/1", p256dh="p", auth="a", cihaz_adi="Mac"))
        db.commit()
    push, epostalar = SahtePush(), []
    monkeypatch.setattr(servisler, "push_gonder", push)
    monkeypatch.setattr(servisler, "eposta_gonder", lambda kime, konu, metin, **k: epostalar.append(metin))
    with OturumYapici() as db:
        api.kullaniciya_hatirlat(db, db.get(Kullanici, uid), an)
    govde, eposta = push.gonderilen[0]["govde"], epostalar[0]
    if beklenen is None:
        assert "Google" not in govde and "Google" not in eposta
    else:
        cumle = beklenen.split(" · ")[0]
        assert govde.endswith(" · " + cumle)
        assert f"{beklenen}: https://rapor.ornek.com/oauth/google/basla" in eposta


def test_test_modu_kapaliyken_hatirlatmada_uyari_yok(google, monkeypatch):
    monkeypatch.setenv("GOOGLE_TEST_MODU", "0")
    uid = kullanici_olustur()
    google_bagla(uid, baglanti=datetime.now(timezone.utc) - timedelta(days=6, hours=10))
    assert api.google_uyari(ayar(uid)) is None
    assert api.hatirlatma_epostasi("özet", [], GUN, None).count("Google") == 0


def test_hatirlatma_ozeti_toplanti_ve_dosya():
    assert api.hatirlatma_ozeti([Madde(kaynak="eposta"), Madde(kaynak="takvim"), Madde(kaynak="takvim"), Madde(kaynak="drive")]) \
        == "Bugünün raporu hazır bekliyor · 1 e-posta, 2 toplantı, 1 dosya bulundu"
    assert api.hatirlatma_ozeti([Madde(kaynak="medusa")] * 4) == "Bugünün raporu hazır bekliyor · 4 commit bulundu"


# ---------------------------------------------------------------- 6) Gmail REST

def mail(to, subject, saat="10:00", cc="", gun_=GUN, kimden=BEN) -> dict:
    return {"Date": f"{gun_.strftime('%a, %d %b %Y')} {saat}:00 +0300", "Subject": subject, "From": kimden, "To": to, "Cc": cc,
            "Message-ID": f"<{hashlib.sha1((subject + saat + to).encode()).hexdigest()[:8]}@mail.gmail.com>"}


def gmail_kur(sahte: SahteGoogle, mailler: list[dict], sayfa_boyu=3, govdeler: dict | None = None) -> None:
    kimlikler = [f"m{i}" for i in range(len(mailler))]
    sahte.mesajlar = {k: {"basliklar": m} for k, m in zip(kimlikler, mailler)}
    for k, govde in (govdeler or {}).items():
        sahte.mesajlar[k]["raw"] = base64.urlsafe_b64encode(govde.encode()).decode().rstrip("=")
    sahte.sayfalar = [kimlikler[i:i + sayfa_boyu] for i in range(0, len(kimlikler), sayfa_boyu)] or [[]]


def kucuk_harfli(m: dict) -> dict:
    return {k.lower(): v for k, v in m.items()}


def test_gmail_rest_sorgu_basliklar_ve_imapla_ayni_maddeler(google):
    kendi = servisler.kendi_alanlari([BEN])
    mailler = [
        mail("Ayşe <ayse@msg.org.tr>", "Ağustos CRD itirazı", "09:12"),
        mail("ayse@msg.org.tr", "Re: ağustos crd İTİRAZI ", "14:37"),
        mail("crd@msg.org.tr", "YNT: Ynt:  Ağustos  CRD itirazı", "11:05"),
        mail("MESAM <a@mesam.org.tr>, b@msg.org.tr", "Tanıtım", cc="c@imro.ie, Ali <ali@ilsvision.com>"),
        mail("ali@ilsvision.com", "Haftalık plan"),  # ekip içi: atlanır
        mail("a@mesam.org.tr", "Dünkü", gun_=GUN - timedelta(days=1)),  # Istanbul günü dışında
        mail(BEN, "rapor: Katalog Coverz'e gönderildi"),
        mail(BEN, "not:", saat="16:00"),
    ]
    gmail_kur(google, mailler, govdeler={"m7": "Subject: not:\nContent-Type: text/plain; charset=utf-8\n\nMSG ile görüşüldü\n\nIMRO takip\n"})
    sonuc = servisler.google_gmail_tara("erisim", BEN, GUN, kendi_alanlar=kendi, istemci=google.istemci())

    liste = google.yollar("/messages?")
    assert len(liste) == 3  # sayfalı: 8 mail, sayfa başına 3
    bas = int(datetime.combine(GUN - timedelta(days=1), time.min, IST).timestamp())
    son = int(datetime.combine(GUN + timedelta(days=1), time.min, IST).timestamp())
    assert liste[0].url.params["q"] == f"in:sent after:{bas} before:{son}" == "in:sent after:1789419600 before:1789592400"
    tek = [i for i in google.yollar("/messages/m0")][0]
    assert tek.url.params["format"] == "metadata"
    assert tek.url.params.get_list("metadataHeaders") == ["From", "To", "Cc", "Subject", "Date", "Message-ID"]
    assert all(i.headers["authorization"] == "Bearer erisim" for i in google.istekler)

    kucuk = [kucuk_harfli(m) for m in mailler]
    kucuk[7]["govde"] = "MSG ile görüşüldü\n\nIMRO takip\n"
    notlar = [m for m in kucuk if servisler.not_konusu(m, BEN) is not None]
    gonderilen = [m for m in kucuk if servisler.not_konusu(m, BEN) is None]
    beklenen = servisler.epostalari_maddele(gonderilen, BEN, GUN, kendi_alanlar=kendi) + servisler.notlari_maddele(notlar, BEN, GUN)
    assert sonuc == beklenen
    assert [m["metin"] for m in sonuc] == [
        "MSG'ye 'Ağustos CRD itirazı' konulu 3 e-posta gönderildi",
        "MESAM'a 'Tanıtım' konulu e-posta gönderildi (ayrıca MSG, IMRO)",
        "Katalog Coverz'e gönderildi", "MSG ile görüşüldü", "IMRO takip",
    ]
    assert [m["kaynak"] for m in sonuc] == ["eposta", "eposta", "not", "not", "not"]


@pytest.mark.parametrize("gruplama", ["konu", "alici"])
def test_gmail_rest_gruplama_ve_ekip_ici_ayari(google, gruplama):
    mailler = [mail("a@mesam.org.tr", "Tanıtım"), mail("a@mesam.org.tr", "Fatura", "11:00"), mail("ali@ilsvision.com", "Plan")]
    gmail_kur(google, mailler)
    kendi = servisler.kendi_alanlari([BEN])
    for atla in (True, False):
        sonuc = servisler.google_gmail_tara("t", BEN, GUN, gruplama=gruplama, kendi_alanlar=kendi, ekip_ici_atla=atla,
                                            istemci=google.istemci())
        assert sonuc == servisler.epostalari_maddele([kucuk_harfli(m) for m in mailler], BEN, GUN, None, gruplama, kendi, atla)


def test_gmail_rest_api_taramasi_kendi_sirket_ve_gruplama_ayarlariyla(google, gun):
    uid = kullanici_olustur(eposta_gruplama="alici")
    google_bagla(uid)
    gmail_kur(google, [mail("a@mesam.org.tr", "Tanıtım"), mail("a@mesam.org.tr", "Fatura", "11:00")])
    istemci().get("/api/bugun?yenile=1")
    assert [m.metin for m in bulunanlar(uid, "eposta")] == ["MESAM'a 2 e-posta gönderildi (konular: Tanıtım; Fatura)"]


def test_gmail_rest_403_hatasi_kaynak_hatasi(google):
    def isleyici(istek):
        return httpx.Response(403, json={"error": {"message": "Gmail API has not been used in project 1 before"}})
    with pytest.raises(servisler.KaynakHatasi, match=r"Gmail \(Google\) erişimi reddedildi \(403: Gmail API"):
        servisler.google_gmail_tara("t", BEN, GUN, istemci=httpx.Client(transport=httpx.MockTransport(isleyici)))


# ---------------------------------------------------------------- 7) Takvim

def etkinlik(eid, baslik, bas, bit, durum="accepted", katilimcilar=(), duzenleyen=False, aciklama="", iptal=False, tum_gun=False):
    e = {"id": eid, "summary": baslik, "status": "cancelled" if iptal else "confirmed",
         "organizer": {"email": BEN if duzenleyen else "d@mesam.org.tr", **({"self": True} if duzenleyen else {})}}
    if tum_gun:
        e["start"], e["end"] = {"date": bas.isoformat()}, {"date": bit.isoformat()}
    else:
        e["start"], e["end"] = {"dateTime": bas.isoformat()}, {"dateTime": bit.isoformat()}
    if aciklama:
        e["description"] = aciklama
    if katilimcilar or not duzenleyen:
        e["attendees"] = [{"email": BEN, "self": True, "responseStatus": durum, **({"organizer": True} if duzenleyen else {})}] \
            + [{"email": a, **({"displayName": ad} if ad else {}), "responseStatus": "accepted"} for a, ad in katilimcilar]
    return e


def takvim_ornekleri() -> list[dict]:
    s = lambda ss, dd=0: istanbul(GUN, ss, dd)  # noqa: E731
    dis = [("a@mesam.org.tr", "MESAM"), ("b@msg.org.tr", None), ("ali@ilsvision.com", "Ali"), ("oda@resource.calendar.google.com", "Oda 1")]
    ornekler = [
        etkinlik("e1", "Tanıtım", s(10), s(11), katilimcilar=dis),
        etkinlik("e2", "Reddedilen", s(11), s(12), durum="declined", katilimcilar=dis),
        etkinlik("e3", "İptal", s(12), s(13), katilimcilar=dis, iptal=True),
        etkinlik("e4", "Yanıtlanmamış", s(12), s(13), durum="needsAction", katilimcilar=dis),
        etkinlik("e5", "Belki", s(13), s(13, 30), durum="tentative", katilimcilar=[("x@coverz.com", "Coverz Ekibi")]),
        etkinlik("e6", "Odak zamanı", s(14), s(15), duzenleyen=True),  # kendine blok: katılımcısız, açıklamasız
        etkinlik("e7", "Müşteri notları", s(15), s(16), duzenleyen=True, aciklama="Sözleşme maddeleri"),
        etkinlik("e8", "Ekip toplantısı", s(9), s(9, 30), duzenleyen=True, katilimcilar=[("ali@ilsvision.com", "Ali")]),
        etkinlik("e9", "Akşam görüşmesi", s(18), s(19), katilimcilar=dis),  # an 17:30: henüz bitmedi
        etkinlik("e10", "Fuar", GUN, GUN + timedelta(days=1), duzenleyen=True, aciklama="Stand", tum_gun=True),
        etkinlik("e11", "Dünkü", istanbul(GUN - timedelta(days=1), 10), istanbul(GUN - timedelta(days=1), 11), katilimcilar=dis),
    ]
    ornekler[0]["attendees"][-1]["resource"] = True  # oda: resource işaretiyle; diğerlerinde alan adından tanınır
    return ornekler


def test_takvim_dahil_haric_kurallari():
    kendi = servisler.kendi_alanlari([BEN])
    an = istanbul(GUN, 17, 30)
    sonuc = servisler.takvim_maddeleri(takvim_ornekleri(), GUN, kendi_alanlar=kendi, an=an)
    assert [m["metin"] for m in sonuc] == [
        "'Tanıtım' toplantısı yapıldı (MESAM, MSG ile)",  # kendi şirketi (Ali) ve toplantı odası yazılmaz
        "'Belki' toplantısı yapıldı (Coverz ile)",
        "'Müşteri notları' toplantısı yapıldı",
        "'Ekip toplantısı' toplantısı yapıldı",  # yalnız kendi şirketi: kurum yazılmaz
        "'Fuar' (tüm gün)",
    ]
    assert all(m["kaynak"] == "takvim" for m in sonuc)
    assert sonuc[0]["id"] == hashlib.sha1(("e1" + GUN.isoformat()).encode()).hexdigest()[:10]
    assert sonuc[0]["kaynak_zaman"] == istanbul(GUN, 10) and sonuc[-1]["kaynak_zaman"] == istanbul(GUN, 0)
    # gün bitince akşam toplantısı da girer
    assert "'Akşam görüşmesi' toplantısı yapıldı (MESAM, MSG ile)" in [
        m["metin"] for m in servisler.takvim_maddeleri(takvim_ornekleri(), GUN, kendi_alanlar=kendi, an=istanbul(GUN, 23))]
    # kendi alanlar verilmezse sözlükteki "şirket içi" yine yazılmaz, bilinmeyen alan adı kurum olur
    assert servisler.takvim_maddeleri(takvim_ornekleri()[:1], GUN, an=an)[0]["metin"] == "'Tanıtım' toplantısı yapıldı (MESAM, MSG ile)"


def test_takvim_kaynak_id_gune_bagli_ve_kararli():
    e = takvim_ornekleri()[:1]
    kendi = servisler.kendi_alanlari([BEN])
    ilk = servisler.takvim_maddeleri(e, GUN, kendi_alanlar=kendi, an=istanbul(GUN, 23))[0]["id"]
    assert ilk == servisler.takvim_maddeleri(e, GUN, kendi_alanlar=kendi, an=istanbul(GUN, 23, 30))[0]["id"]
    assert ilk != servisler.kaynak_kimligi("e1", GUN + timedelta(days=1))


def test_takvim_istegi_gun_araligi(google):
    servisler.takvim_tara("t", date(2026, 9, 10), istemci=google.istemci())
    p = google.yollar("calendar/v3/calendars/primary/events")[0].url.params
    assert (p["timeMin"], p["timeMax"], p["singleEvents"], p["orderBy"]) == (
        "2026-09-09T21:00:00Z", "2026-09-10T21:00:00Z", "true", "startTime")


# ---------------------------------------------------------------- 8) Drive

def dosya(fid, ad, mime, olusturma, degisme, ben=True):
    return {"id": fid, "name": ad, "mimeType": mime, "createdTime": iso(olusturma), "modifiedTime": iso(degisme),
            "lastModifyingUser": {"me": ben}}


def test_drive_olusturuldu_guncellendi_me_suzgeci_uzanti():
    dun = istanbul(GUN - timedelta(days=3), 9)
    dosyalar = [
        dosya("f1", "Katalog 2026.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", istanbul(GUN, 9), istanbul(GUN, 10)),
        dosya("f2", "Sözleşme taslağı", "application/vnd.google-apps.document", dun, istanbul(GUN, 11)),
        dosya("f3", "Fatura.pdf", "application/pdf", dun, istanbul(GUN, 12)),
        dosya("f4", "Sunum", "application/vnd.google-apps.presentation", istanbul(GUN, 0, 30), istanbul(GUN, 13)),
        dosya("f5", "Logo.png", "image/png", dun, istanbul(GUN, 14)),
        dosya("f6", "Başkasının tablosu", "application/vnd.google-apps.spreadsheet", dun, istanbul(GUN, 15), ben=False),
        dosya("f7", "Rapor v2.1", "application/vnd.google-apps.document", dun, istanbul(GUN, 8)),
    ]
    sonuc = servisler.drive_maddeleri(dosyalar, GUN)
    assert [m["metin"] for m in sonuc] == [
        "'Rapor v2.1' belgesi güncellendi",
        "'Katalog 2026' tablosu oluşturuldu",
        "'Sözleşme taslağı' belgesi güncellendi",
        "'Fatura' PDF'i güncellendi",
        "'Sunum' sunumu oluşturuldu",
        "'Logo' dosyası güncellendi",
    ]
    assert all(m["kaynak"] == "drive" for m in sonuc)
    assert sonuc[1]["kaynak_zaman"] == istanbul(GUN, 10)
    assert sonuc[1]["id"] == hashlib.sha1(("f1" + GUN.isoformat()).encode()).hexdigest()[:10]
    assert [servisler.drive_turu(m) for m in ("text/csv", "application/msword", "application/vnd.ms-powerpoint", "video/mp4")] == [
        "tablo", "belge", "sunum", "dosya"]


def test_drive_on_bes_siniri():
    dosyalar = [dosya(f"f{i}", f"Belge {i}", "application/vnd.google-apps.document", istanbul(GUN - timedelta(days=1), 9),
                      istanbul(GUN, 8, i)) for i in range(18)]
    sonuc = servisler.drive_maddeleri(dosyalar, GUN)
    assert len(sonuc) == 16 and sonuc[-1]["metin"] == "ve 3 dosya daha güncellendi"
    assert [m["metin"] for m in sonuc[:2]] == ["'Belge 0' belgesi güncellendi", "'Belge 1' belgesi güncellendi"]
    assert sonuc[-1]["id"] == servisler.drive_maddeleri(dosyalar[:16], GUN)[-1]["id"]  # sayı değişse de aynı madde


def test_drive_istegi(google):
    google.dosyalar = [dosya("f1", "A.docx", "application/msword", istanbul(GUN, 9), istanbul(GUN, 9))]
    assert [m["metin"] for m in servisler.drive_tara("t", GUN, istemci=google.istemci())] == ["'A' belgesi oluşturuldu"]
    p = google.yollar("drive/v3/files")[0].url.params
    assert p["q"] == ("modifiedTime >= '2026-09-15T21:00:00Z' and modifiedTime < '2026-09-16T21:00:00Z' and trashed=false "
                      "and mimeType != 'application/vnd.google-apps.folder'")
    assert p["fields"] == "nextPageToken,files(id,name,mimeType,createdTime,modifiedTime,lastModifyingUser(me))"
    assert p["corpora"] == "user"


# ---------------------------------------------------------------- 9) tarama: kategori, kararlılık, gizleme, geçmiş gün

def test_taramada_takvim_drive_bulunanlara_ve_kategorilere_duser(google, gun):
    uid = kullanici_olustur()
    google_bagla(uid)
    google.etkinlikler = takvim_ornekleri()
    google.dosyalar = [dosya("f1", "Katalog.xlsx", "application/vnd.google-apps.spreadsheet", istanbul(GUN, 9), istanbul(GUN, 10))]
    c = istemci()
    toplanti = c.post("/api/kategoriler", json={"ad": "Toplantılar", "kaynaklar": ["takvim"]})
    assert toplanti.status_code == 201
    dosyalar_k = c.post("/api/kategoriler", json={"ad": "Dosyalar", "kaynaklar": ["drive"]}).json()
    d = c.get("/api/bugun?yenile=1").json()
    assert d["sayim"] == {"eposta": 0, "medusa": 0, "takvim": 6, "drive": 1}
    assert [m["metin"] for m in d["bulunan"] if m["kaynak"] == "drive"] == ["'Katalog' tablosu oluşturuldu"]
    assert "'Akşam görüşmesi' toplantısı yapıldı (MESAM, MSG ile)" in [m["metin"] for m in d["bulunan"]]  # geçmiş gün: bitmiş
    durum = c.get(f"/api/durum?tarih={GUN}").json()
    etkin = {m["kaynak"]: m["etkin_kategori_id"] for m in durum["maddeler"] if m["tur"] == "bulunan"}
    assert etkin == {"takvim": toplanti.json()["id"], "drive": dosyalar_k["id"]}
    toplantilar = durum["rapor_metni"].split("*Toplantılar:*\n")[1].split("\n\n")[0]
    assert "• 'Ekip toplantısı' toplantısı yapıldı" in toplantilar.splitlines()
    assert "*Dosyalar:*\n• 'Katalog' tablosu oluşturuldu" in durum["rapor_metni"]

    # ikinci tarama çoğaltmaz (kaynak_id kararlı)
    ilk = [(m.kaynak_id, m.id) for m in bulunanlar(uid)]
    c.get("/api/bugun?yenile=1")
    assert [(m.kaynak_id, m.id) for m in bulunanlar(uid)] == ilk


def test_kategori_yoksa_genel_islere(google, gun):
    uid = kullanici_olustur()
    google_bagla(uid)
    google.etkinlikler = takvim_ornekleri()[:1]
    c = istemci()
    c.get("/api/bugun?yenile=1")
    durum = c.get("/api/durum").json()
    genel = durum["duzen"]["genel"]
    assert [m["etkin_kategori_id"] for m in durum["maddeler"] if m["kaynak"] == "takvim"] == [genel]


def test_artik_uretilmeyen_takvim_drive_maddesi_gizlenir(google, gun):
    uid = kullanici_olustur()
    google_bagla(uid)
    google.etkinlikler = takvim_ornekleri()[:1] + [takvim_ornekleri()[4]]
    google.dosyalar = [dosya("f1", "A", "application/pdf", istanbul(GUN, 9), istanbul(GUN, 9))]
    c = istemci()
    c.get("/api/bugun?yenile=1")
    duzenlenen = next(m for m in bulunanlar(uid, "takvim") if "Belki" in m.metin)
    c.patch(f"/api/maddeler/{duzenlenen.id}", json={"metin": "Coverz ile görüşüldü"})

    google.takvim_kodu = 500  # tarama hatası: hiçbir şey gizlenmez
    google.etkinlikler, google.dosyalar = [], []
    d = c.get("/api/bugun?yenile=1").json()
    assert any(h["kaynak"] == "takvim" for h in d["hatalar"])
    assert [m.gizli for m in bulunanlar(uid, "takvim")] == [False, False]
    assert [m.gizli for m in bulunanlar(uid, "drive")] == [True]  # Drive hatasız tarandı, dosya artık üretilmiyor

    google.takvim_kodu = 200
    c.get("/api/bugun?yenile=1")
    assert {m.metin: m.gizli for m in bulunanlar(uid, "takvim")} == {
        "'Tanıtım' toplantısı yapıldı (MESAM, MSG ile)": True, "Coverz ile görüşüldü": False}


def test_gecmis_gun_taramasi_dogru_aralik_ve_gizlemez(google, gun):
    uid = kullanici_olustur()
    google_bagla(uid)
    gecmis = GUN - timedelta(days=3)
    google.etkinlikler = [etkinlik("g1", "Eski", istanbul(gecmis, 10), istanbul(gecmis, 11), katilimcilar=[("a@mesam.org.tr", "")])]
    c = istemci()
    d = c.get(f"/api/bugun?yenile=1&tarih={gecmis}").json()
    assert [m["metin"] for m in d["bulunan"]] == ["'Eski' toplantısı yapıldı (MESAM ile)"]
    bas, son = servisler.gun_araligi(gecmis)
    p = google.yollar("calendar")[0].url.params
    assert (p["timeMin"], p["timeMax"]) == (servisler.rfc3339(bas), servisler.rfc3339(son)) == ("2026-09-12T21:00:00Z", "2026-09-13T21:00:00Z")
    assert "modifiedTime >= '2026-09-12T21:00:00Z' and modifiedTime < '2026-09-13T21:00:00Z'" in google.yollar("drive")[0].url.params["q"]
    q = google.yollar("/messages?")[0].url.params["q"]
    assert q == f"in:sent after:{int(istanbul(gecmis - timedelta(days=1), 0).timestamp())} before:{int(istanbul(gecmis + timedelta(days=1), 0).timestamp())}"
    google.etkinlikler = []
    c.get(f"/api/bugun?yenile=1&tarih={gecmis}")
    assert [m.gizli for m in bulunanlar(uid, "takvim", gecmis)] == [False]  # geçmiş günde gizleme yok


def test_kapali_google_kaynagi_taranmaz(google, gun):
    uid = kullanici_olustur()
    google_bagla(uid, kaynaklar={"gmail": False, "github": False, "medusa": False, "takvim": True, "drive": False})
    istemci().get("/api/bugun?yenile=1")
    assert google.yollar("calendar") and not google.yollar("drive") and not google.yollar("gmail.googleapis.com")


def test_claude_istemine_takvim_drive_kurali(sahte_claude):
    sistem = servisler.claude_sistem("", ["ilsvision.com"])
    assert servisler.GOOGLE_KURALI in sistem and "toplantı katılımcısı" in sistem
    assert servisler.GOOGLE_KURALI in servisler.duzelt_sistemi()
    servisler.claude_cevir([servisler.madde("takvim", "'Tanıtım' toplantısı yapıldı (MESAM ile)")], "t")
    icerik = sahte_claude.istekler[0]["messages"][0]["content"]
    assert "'takvim' olanlar bugün yapılan toplantılar" in icerik.split("\n", 1)[0]
    assert "'drive' olanlar" in icerik.split("\n", 1)[0]


# ---------------------------------------------------------------- 10) arayüz: GOOGLE_* yoksa gizli

def test_google_yoksa_dugmeler_gizli(google, monkeypatch):
    kullanici_olustur()
    c = istemci()
    for sayfa in ("/ayarlar", "/kurulum"):
        assert "Google ile bağlan" in c.get(sayfa).text
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET")
    for sayfa in ("/ayarlar", "/kurulum"):
        html = c.get(sayfa).text
        assert 'id="googleSatir"' not in html and 'id="googleBaglan"' not in html and 'id="kaynak_takvim"' not in html
        assert "Uygulama şifresiyle bağlan" not in html and "Diğer yöntem: uygulama şifresi" not in html
    a = c.get("/api/ayarlar").json()
    assert a["google"]["ayarli"] is False and set(a["kaynaklar"]) == {"gmail", "github", "medusa"}
    yol, q = yonlendirme(c.get("/oauth/google/basla?donus=/kurulum"))
    assert yol == "/kurulum" and q["google"] == "hata"


def test_ayarlar_ve_kurulum_sayfalarinda_google_ogeleri(google):
    kullanici_olustur()
    c = istemci()
    ayarlar = c.get("/ayarlar").text
    for parca in ('id="googleSatir"', 'id="kaynak_takvim"', 'id="kaynak_drive"', "Diğer yöntem: uygulama şifresi",
                  "Gelişmiş → Günlük Rapor'a git", "Bağlantıyı kaldır", '["gmail", "github", "medusa", "takvim", "drive"]'):
        assert parca in ayarlar
    kurulum = c.get("/kurulum").text
    assert 'id="googleBaglan"' in kurulum and "Uygulama şifresiyle bağlan" in kurulum
    bugun = c.get("/").text
    assert 'data-say="toplanti"' in bugun and 'data-say="dosya"' in bugun and 'id="googleSerit"' in bugun


def test_sema_guncelle_google_kolonlari_idempotent(tmp_path):
    from sqlalchemy import create_engine, text

    import veritabani
    eski = create_engine(f"sqlite:///{tmp_path}/eski.db")
    Temel.metadata.create_all(eski)
    with eski.begin() as b:
        for kolon in ("google_refresh_enc", "google_eposta", "google_baglanti", "google_durum", "google_kapsamlar"):
            b.execute(text(f"ALTER TABLE user_settings DROP COLUMN {kolon}"))
    assert veritabani.sema_guncelle(eski) == [
        "user_settings.google_refresh_enc", "user_settings.google_eposta", "user_settings.google_baglanti",
        "user_settings.google_durum", "user_settings.google_kapsamlar"]
    assert veritabani.sema_guncelle(eski) == []
    eski.dispose()
