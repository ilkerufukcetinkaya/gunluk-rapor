"""M1: Microsoft ile bağlan — OAuth (state + PKCE, tenant common), Outlook, Takvim/Teams, OneDrive.
sqlite; Microsoft'a (ve Google'a) giden her istek httpx.MockTransport'taki sahte sunucuya gider, ağa çıkılmaz."""
import base64
import hashlib
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from email.utils import format_datetime, getaddresses
from urllib.parse import parse_qs, parse_qsl, unquote, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import api
import app as uygulama
import guvenlik
import kimlik
import servisler
from test_g1 import SahteGoogle
from veritabani import Kullanici, KullaniciAyari, Madde, OturumYapici, PushAbonelik, Temel, motor

BEN = "ufuk@ilsvision.com"  # giriş e-postası
MS = "ufuk.cetinkaya@medusarights.com"  # Microsoft hesabı (başka alan adı: kendi şirketi otomatik eklenir)
SIFRE = "dogru-sifre-123"
ISTEMCI_ID = "11111111-2222-3333-4444-555555555555"
ISTEMCI_SIRRI = "ms~istemci-sirri-degeri"
REFRESH = "M.C5-yenileme-gizli-deger"
ERISIM = "EwB-erisim-gizli-deger"
TUM_KAPSAMLAR = ["openid", "email", "profile", "offline_access", "https://graph.microsoft.com/User.Read",
                 "https://graph.microsoft.com/Mail.Read", "https://graph.microsoft.com/Calendars.Read",
                 "https://graph.microsoft.com/Files.Read"]
GUN = date(2026, 9, 16)  # Çarşamba; API testlerinde "bugün"
IST = servisler.ISTANBUL
YONETICI = ("Şirketinin Microsoft yöneticisinin bu uygulamaya bir kez onay vermesi gerekiyor. "
            "BT birimine 'Günlük Rapor uygulamasına kullanıcı onayı' için başvur.")
MS_KAYNAKLARI = {"outlook": True, "outlook_takvim": True, "onedrive": True}


def istanbul(gun: date, ss: int, dd: int = 0) -> datetime:
    return datetime.combine(gun, time(ss, dd), IST)


def iso(z: datetime) -> str:
    return z.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SahteMicrosoft:
    """Token ucu ve Graph (/me, Gönderilmiş Öğeler, mesaj gövdesi, calendarView, drive/recent); istekleri saklar."""

    def __init__(self):
        self.istekler: list[httpx.Request] = []
        self.kapsamlar = list(TUM_KAPSAMLAR)
        self.me = {"mail": MS, "userPrincipalName": "ufuk@medusarights.onmicrosoft.com"}
        self.takas_hatasi: dict | None = None
        self.yenile_hatasi: str | None = None
        self.rotasyon = True
        self.yenileme_sayisi = 0
        self.mesaj_sayfalari: list[list[dict]] = [[]]
        self.govdeler: dict[str, str] = {}
        self.etkinlikler: list[dict] = []
        self.dosyalar: list[dict] = []
        self.kodlar: dict[str, int] = {}  # Graph yolu → hata kodu

    def istemci(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def yollar(self, parca: str) -> list[httpx.Request]:
        return [i for i in self.istekler if parca in unquote(str(i.url))]

    def __call__(self, istek: httpx.Request) -> httpx.Response:
        self.istekler.append(istek)
        adres = f"{istek.url.scheme}://{istek.url.host}{istek.url.path}"
        q = istek.url.params
        if adres == servisler.MICROSOFT_TOKEN_URL:
            form = dict(parse_qsl(istek.content.decode()))
            if form["grant_type"] == "authorization_code":
                if self.takas_hatasi:
                    return httpx.Response(400, json=self.takas_hatasi)
                return httpx.Response(200, json={
                    "token_type": "Bearer", "access_token": ERISIM, "expires_in": 3599, "refresh_token": REFRESH,
                    "scope": " ".join(self.kapsamlar), "id_token": "a.b.c",
                })
            self.yenileme_sayisi += 1
            if self.yenile_hatasi:
                return httpx.Response(400, json={"error": self.yenile_hatasi,
                                                 "error_description": "AADSTS70000: The provided grant has expired."})
            govde = {"access_token": f"{ERISIM}-{self.yenileme_sayisi}", "expires_in": 3599, "token_type": "Bearer"}
            if self.rotasyon:
                govde["refresh_token"] = f"{REFRESH}-{self.yenileme_sayisi}"
            return httpx.Response(200, json=govde)
        if not adres.startswith(servisler.GRAPH_API):
            return httpx.Response(404, json={"error": {"message": "yok"}})
        yol = adres[len(servisler.GRAPH_API):]
        if yol in self.kodlar:
            return httpx.Response(self.kodlar[yol], json={"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}})
        if yol == "/me":
            return httpx.Response(200, json=self.me)
        if yol == "/me/mailFolders/sentitems/messages":
            sira = int(q.get("$skip") or 0) // 50
            govde = {"value": self.mesaj_sayfalari[sira]}
            if sira + 1 < len(self.mesaj_sayfalari):
                govde["@odata.nextLink"] = (f"{servisler.GRAPH_API}/me/mailFolders/sentitems/messages?"
                                            f"%24filter=x&%24top=50&%24skip={(sira + 1) * 50}")
            return httpx.Response(200, json=govde)
        if yol.startswith("/me/messages/"):
            kimlik_ = yol.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": kimlik_, "body": {"contentType": "text", "content": self.govdeler[kimlik_]}})
        if yol == "/me/calendarView":
            return httpx.Response(200, json={"value": self.etkinlikler})
        if yol == "/me/drive/recent":
            return httpx.Response(200, json={"value": self.dosyalar})
        return httpx.Response(404, json={"error": {"message": "yok"}})


@pytest.fixture(autouse=True)
def ortam(monkeypatch):
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    api.onbellegi_temizle()
    api._google_erisim.clear()
    api._ms_erisim.clear()
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", ISTEMCI_ID)
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", ISTEMCI_SIRRI)
    monkeypatch.setenv("APP_URL", "https://rapor.ornek.com")
    monkeypatch.setattr(servisler, "gmail_tara", lambda *a, **k: [])
    monkeypatch.setattr(servisler, "github_tara", lambda *a, **k: [])
    yield


@pytest.fixture
def ms(monkeypatch):
    sahte = SahteMicrosoft()
    monkeypatch.setattr(servisler, "microsoft_istemci", sahte.istemci)
    return sahte


@pytest.fixture
def gun(monkeypatch):
    """API'de "bugün" GUN olur; o günün her toplantısı bitmiş sayılır."""
    monkeypatch.setattr(api, "bugun", lambda: GUN)
    return GUN


def kullanici_olustur(eposta=BEN, **ayar) -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad="Ufuk", sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol="uye", aktif=True,
                      sifre_degistirmeli=False)
        db.add(k)
        db.commit()
        db.add(KullaniciAyari(user_id=k.id, kurulum_tamam=True, **ayar))
        db.commit()
        return k.id


def ms_bagla(uid: int, kapsamlar=None, durum="bagli", kaynaklar=None) -> None:
    with OturumYapici() as db:
        a = db.get(KullaniciAyari, uid)
        a.ms_refresh_enc = guvenlik.sifrele(REFRESH)
        a.ms_eposta = MS
        a.ms_baglanti = datetime.now(timezone.utc)
        a.ms_durum = durum
        a.ms_kapsamlar = list(TUM_KAPSAMLAR if kapsamlar is None else kapsamlar)
        a.kaynaklar = kaynaklar or {"gmail": False, "github": False, "medusa": False, **MS_KAYNAKLARI}
        db.commit()


def ayar(uid: int) -> KullaniciAyari:
    with OturumYapici() as db:
        return db.get(KullaniciAyari, uid)


def istemci(eposta=BEN, base_url="http://testserver") -> TestClient:
    c = TestClient(uygulama.app, base_url=base_url, follow_redirects=False)
    c.__enter__()
    assert c.post("/giris", data={"eposta": eposta, "sifre": SIFRE}).status_code == 303
    return c


def basla(c: TestClient, donus="/ayarlar") -> dict:
    y = c.get(f"/oauth/microsoft/basla?donus={donus}")
    assert y.status_code == 303
    return {k: v[0] for k, v in parse_qs(urlsplit(y.headers["location"]).query).items()}


def yonlendirme(y) -> tuple[str, dict]:
    parca = urlsplit(y.headers["location"])
    return parca.path, {k: v[0] for k, v in parse_qs(parca.query).items()}


def bagla(c: TestClient, donus="/ayarlar"):
    return c.get("/oauth/microsoft/geri", params={"code": "M.kod", "state": basla(c, donus)["state"]})


def bulunanlar(uid: int, kaynak: str | None = None, tarih=GUN) -> list[Madde]:
    with OturumYapici() as db:
        sorgu = select(Madde).where(Madde.user_id == uid, Madde.tur == "bulunan", Madde.tarih == tarih)
        if kaynak:
            sorgu = sorgu.where(Madde.kaynak == kaynak)
        return db.scalars(sorgu.order_by(Madde.id)).all()


# ---------------------------------------------------------------- 1) OAuth: başla, state, PKCE, dönüş

def test_basla_yetki_adresi_state_ve_pkce(ms):
    uid = kullanici_olustur()
    c = istemci()
    y = c.get("/oauth/microsoft/basla?donus=/kurulum")
    assert y.headers["location"].startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    p = {k: v[0] for k, v in parse_qs(urlsplit(y.headers["location"]).query).items()}
    assert (p["client_id"], p["response_type"], p["response_mode"]) == (ISTEMCI_ID, "code", "query")
    assert p["redirect_uri"] == "https://rapor.ornek.com/oauth/microsoft/geri"
    assert p["scope"] == "openid email offline_access User.Read Mail.Read Calendars.Read Files.Read"
    assert (p["prompt"], p["login_hint"], p["code_challenge_method"]) == ("select_account", BEN, "S256")
    veri = kimlik.oauth_state_coz("microsoft", p["state"])
    assert veri["u"] == uid and veri["d"] == "/kurulum"
    pkce = kimlik._imzacilar["microsoft"].loads(c.cookies.get(kimlik.MICROSOFT_PKCE_CEREZ), salt="pkce")
    assert pkce["n"] == veri["n"]
    assert p["code_challenge"] == base64.urlsafe_b64encode(hashlib.sha256(pkce["v"].encode()).digest()).rstrip(b"=").decode()
    assert pkce["v"] not in json.dumps(p)  # verifier Microsoft'a giden adreste yok
    assert kimlik.google_state_coz(p["state"]) is None  # Google'ın imzasıyla çözülmez


@pytest.mark.parametrize("donus, beklenen", [
    ("/kurulum", "/kurulum"), ("/ayarlar", "/ayarlar"), ("https://kotu.ornek.com", "/ayarlar"), ("//kotu.ornek.com", "/ayarlar"),
    ("/yonetim", "/ayarlar"), ("", "/ayarlar"),
])
def test_donus_beyaz_listesi(ms, donus, beklenen):
    kullanici_olustur()
    assert kimlik.oauth_state_coz("microsoft", basla(istemci(), donus)["state"])["d"] == beklenen


def test_localhost_yonlendirmesi(ms):
    kullanici_olustur()
    c = istemci(base_url="http://localhost:8765")
    assert basla(c)["redirect_uri"] == "http://localhost:8765/oauth/microsoft/geri"


def test_geri_basarili_token_fernetle_saklanir_kaynaklar_acilir(ms):
    uid = kullanici_olustur()
    c = istemci()
    p = basla(c, "/kurulum")
    y = c.get("/oauth/microsoft/geri", params={"code": "M.kod", "state": p["state"]})
    assert yonlendirme(y) == ("/kurulum", {"microsoft": "bagli"})
    form = dict(parse_qsl(ms.yollar("oauth2/v2.0/token")[0].content.decode()))
    assert (form["grant_type"], form["code"], form["client_secret"]) == ("authorization_code", "M.kod", ISTEMCI_SIRRI)
    assert form["redirect_uri"] == p["redirect_uri"] and form["scope"] == servisler.MICROSOFT_SCOPE
    assert base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode() == p["code_challenge"]
    me = ms.yollar("/v1.0/me?")[0]
    assert me.headers["authorization"] == f"Bearer {ERISIM}" and me.url.params["$select"] == "mail,userPrincipalName"
    assert c.cookies.get(kimlik.MICROSOFT_PKCE_CEREZ) is None  # çerez dönüşte silinir
    a = ayar(uid)
    assert a.ms_refresh_enc and REFRESH not in a.ms_refresh_enc and guvenlik.coz(a.ms_refresh_enc) == REFRESH
    assert (a.ms_eposta, a.ms_durum, a.ms_kapsamlar) == (MS, "bagli", TUM_KAPSAMLAR) and a.ms_baglanti is not None
    assert a.kaynaklar == {"gmail": False, "github": False, "medusa": False, **MS_KAYNAKLARI}
    assert api._ms_erisim[uid][0] == ERISIM  # access token yalnız bellekte
    m = c.get("/api/ayarlar").json()["microsoft"]
    assert m == {"ayarli": True, "bagli": True, "eposta": MS, "durum": "bagli",
                 "kapsamlar": {"outlook": True, "outlook_takvim": True, "onedrive": True}, "uyari": None}


def test_eposta_mail_yoksa_upn_ve_verilmeyen_kapsam_kapali(ms):
    uid = kullanici_olustur()
    ms.me = {"mail": None, "userPrincipalName": "Ufuk@Kisisel.onmicrosoft.com"}
    ms.kapsamlar = ["openid", "User.Read", "Calendars.Read"]  # kısa adlarla da gelebilir
    c = istemci()
    bagla(c)
    a = c.get("/api/ayarlar").json()
    assert ayar(uid).ms_eposta == "ufuk@kisisel.onmicrosoft.com"
    assert a["microsoft"]["kapsamlar"] == {"outlook": False, "outlook_takvim": True, "onedrive": False}
    assert {k: a["kaynaklar"][k] for k in MS_KAYNAKLARI} == {"outlook": False, "outlook_takvim": True, "onedrive": False}
    # izni olmayan kaynak açılmaya çalışılsa da kapalı kalır
    assert c.put("/api/ayarlar", json={"kaynaklar": {"onedrive": True}}).json()["kaynaklar"]["onedrive"] is False


def test_sahte_suresi_gecmis_ve_google_state_i_reddedilir(ms, monkeypatch):
    kullanici_olustur()
    c = istemci()
    basla(c)
    yol, q = yonlendirme(c.get("/oauth/microsoft/geri", params={"code": "k", "state": "sahte.state"}))
    assert (yol, q["microsoft"]) == ("/ayarlar", "hata")
    google_state, _ = kimlik.google_state_uret(1, "/kurulum")  # başka sağlayıcının imzası
    assert yonlendirme(c.get("/oauth/microsoft/geri", params={"code": "k", "state": google_state}))[1]["microsoft"] == "hata"
    p = basla(c, "/kurulum")
    monkeypatch.setattr(kimlik, "MICROSOFT_STATE_SURESI", -1)
    yol, q = yonlendirme(c.get("/oauth/microsoft/geri", params={"code": "k", "state": p["state"]}))
    assert (yol, q["microsoft"]) == ("/ayarlar", "hata") and "süresi" in q["neden"]
    assert not ms.yollar("oauth2/v2.0/token")


def test_pkce_cerezi_yoksa_ya_da_baska_akisinsa_reddedilir(ms):
    kullanici_olustur()
    c = istemci()
    p1 = basla(c)
    basla(c)  # ikinci akış çerezi değiştirir
    assert yonlendirme(c.get("/oauth/microsoft/geri", params={"code": "k", "state": p1["state"]}))[1]["microsoft"] == "hata"
    p3 = basla(c)
    c.cookies.delete(kimlik.MICROSOFT_PKCE_CEREZ, path="/oauth/microsoft")
    assert yonlendirme(c.get("/oauth/microsoft/geri", params={"code": "k", "state": p3["state"]}))[1]["microsoft"] == "hata"
    assert not ms.yollar("oauth2/v2.0/token")


def test_baska_kullanicinin_state_i_reddedilir(ms):
    kullanici_olustur()
    kullanici_olustur("baska@ornek.com")
    state = basla(istemci())["state"]
    b = istemci("baska@ornek.com")
    basla(b)
    q = yonlendirme(b.get("/oauth/microsoft/geri", params={"code": "k", "state": state}))[1]
    assert q["microsoft"] == "hata" and "Oturum" in q["neden"]


# ---------------------------------------------------------------- 2) hata eşlemesi: yönetici onayı, izin verilmedi

@pytest.mark.parametrize("hata, aciklama, beklenen", [
    ("consent_required", "AADSTS65001: The user or administrator has not consented to use the application.", YONETICI),
    ("access_denied", "AADSTS90094: The grant requires admin permission.", YONETICI),
    ("access_denied", "Need admin approval", YONETICI),
    ("invalid_request", "AADSTS65001: The user or administrator has not consented.\r\nTrace ID: 1", YONETICI),
    ("access_denied", "AADSTS65004: User declined to consent to access the app.", "İzin verilmedi"),
    ("invalid_request", "AADSTS50020: User account from identity provider does not exist in tenant.\r\nTrace ID: abc",
     "Microsoft isteği reddetti (AADSTS50020: User account from identity provider does not exist in tenant.)"),
    ("server_error", "", "Microsoft isteği reddetti (server_error)"),
])
def test_onay_donusu_hata_eslemesi(ms, hata, aciklama, beklenen):
    uid = kullanici_olustur()
    c = istemci()
    y = c.get("/oauth/microsoft/geri", params={"error": hata, "error_description": aciklama, "state": basla(c, "/kurulum")["state"]})
    assert yonlendirme(y) == ("/kurulum", {"microsoft": "hata", "neden": beklenen})
    assert ayar(uid).ms_refresh_enc is None and not ms.yollar("oauth2/v2.0/token")


def test_token_takasinda_aadsts65001_turkce_mesaj(ms):
    uid = kullanici_olustur()
    ms.takas_hatasi = {"error": "invalid_grant", "error_codes": [65001], "error_description":
                       "AADSTS65001: The user or administrator has not consented to use the application with ID "
                       f"'{ISTEMCI_ID}' named 'Günlük Rapor'. Send an interactive authorization request.\r\nTrace ID: x"}
    q = yonlendirme(bagla(istemci()))[1]
    assert q == {"microsoft": "hata", "neden": YONETICI}
    assert ayar(uid).ms_refresh_enc is None
    ms.takas_hatasi = {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided. Ensure the secret."}
    assert yonlendirme(bagla(istemci()))[1]["neden"] == "Microsoft token vermedi (AADSTS7000215: Invalid client secret provided.)"


def test_ayarlarda_yonetici_onayi_mesaji_gosterilir(ms):
    """Arayüz dönüş parametresini Microsoft satırının altındaki sarı şeritte gösterir."""
    kullanici_olustur()
    html = istemci().get("/ayarlar").text
    assert 'id="msHata"' in html and "q.get('microsoft')" in html


# ---------------------------------------------------------------- 3) gizlilik

def test_token_hicbir_yanitta_ve_logda_sizmiyor(ms, gun, caplog):
    caplog.set_level(logging.DEBUG)
    uid = kullanici_olustur()
    c = istemci()
    bagla(c)
    enc = ayar(uid).ms_refresh_enc
    api._ms_erisim[uid] = (ERISIM, 0)  # yenileme de çalışsın (rotasyon)
    yanitlar = [c.get(y).text for y in ("/api/ayarlar", "/api/durum", "/api/bugun?yenile=1", "/api/kurulum", "/ayarlar", "/kurulum", "/")]
    yanitlar.append(c.post("/oauth/microsoft/kaldir").text)
    for metin in yanitlar + [caplog.text]:
        for gizli in (REFRESH, f"{REFRESH}-1", ERISIM, enc, ISTEMCI_SIRRI):
            assert gizli not in metin


# ---------------------------------------------------------------- 4) access token önbelleği, rotasyon, invalid_grant

def test_refresh_rotasyonu_kaydedilir_ve_sonraki_yenilemede_kullanilir(ms, gun):
    uid = kullanici_olustur()
    ms_bagla(uid)
    c = istemci()
    c.get("/api/bugun?yenile=1")
    c.get("/api/bugun?yenile=1")
    assert ms.yenileme_sayisi == 1  # ikinci tarama önbellekteki access token'ı kullanır
    assert guvenlik.coz(ayar(uid).ms_refresh_enc) == f"{REFRESH}-1"
    api._ms_erisim[uid] = (api._ms_erisim[uid][0], 0)  # süresi doldu
    c.get("/api/bugun?yenile=1")
    formlar = [dict(parse_qsl(i.content.decode())) for i in ms.yollar("oauth2/v2.0/token")]
    assert [f["refresh_token"] for f in formlar] == [REFRESH, f"{REFRESH}-1"]
    assert formlar[0]["grant_type"] == "refresh_token" and formlar[0]["scope"] == servisler.MICROSOFT_SCOPE
    assert guvenlik.coz(ayar(uid).ms_refresh_enc) == f"{REFRESH}-2"
    yetki = {i.headers["authorization"] for i in ms.yollar("graph.microsoft.com")}
    assert yetki == {f"Bearer {ERISIM}-1", f"Bearer {ERISIM}-2"}


def test_rotasyonsuz_yanitta_refresh_degismez(ms, gun):
    uid = kullanici_olustur()
    ms_bagla(uid)
    ms.rotasyon = False
    enc = ayar(uid).ms_refresh_enc
    istemci().get("/api/bugun?yenile=1")
    assert ayar(uid).ms_refresh_enc == enc


@pytest.mark.parametrize("hata", ["invalid_grant", "interaction_required"])
def test_invalid_grant_yenile_durumu_microsoft_kaynaklari_atlanir(ms, gun, hata):
    uid = kullanici_olustur()
    ms_bagla(uid)
    ms.yenile_hatasi = hata
    c = istemci()
    d = c.get("/api/bugun?yenile=1").json()
    assert {"kaynak": "microsoft", "mesaj": "Microsoft bağlantısı yenilenmeli"} in d["hatalar"]
    assert ayar(uid).ms_durum == "yenile"
    assert not ms.yollar("graph.microsoft.com")
    c.get("/api/bugun?yenile=1")
    assert ms.yenileme_sayisi == 1  # ikinci taramada refresh bir daha denenmez
    uyari = c.get("/api/durum").json()["ayarlar"]["microsoft"]["uyari"]
    assert uyari == {"tur": "doldu", "metin": "Microsoft bağlantısı yenilenmeli", "eylem": "Yeniden bağlan",
                     "adres": "/oauth/microsoft/basla"}
    html = c.get("/").text
    assert 'id="microsoftSerit"' in html and "h.kaynak === 'microsoft'" in html


def test_graph_401_access_token_atilir(ms, gun):
    uid = kullanici_olustur()
    ms_bagla(uid)
    api._ms_erisim[uid] = (ERISIM, 9e12)
    ms.kodlar["/me/calendarView"] = 401
    d = istemci().get("/api/bugun?yenile=1").json()
    assert "Takvim (Microsoft) oturumu geçersiz; birazdan yeniden deneyin" in [h["mesaj"] for h in d["hatalar"]]
    assert uid not in api._ms_erisim


class SahtePush:
    def __init__(self):
        self.gonderilen: list[dict] = []

    def __call__(self, abonelik, veri):
        self.gonderilen.append(veri)
        return None, ""


def test_hatirlatmada_microsoft_yenile_satiri(ms, monkeypatch):
    an = datetime.now(IST).replace(hour=17, minute=0) if datetime.now(IST).hour < 17 else datetime.now(IST)
    monkeypatch.setattr(api, "bugun", lambda: an.date())
    uid = kullanici_olustur(hatirlatma_gunler="1,2,3,4,5,6,7")
    ms_bagla(uid, durum="yenile")
    with OturumYapici() as db:
        db.add(PushAbonelik(user_id=uid, endpoint="https://push.ornek.com/1", p256dh="p", auth="a", cihaz_adi="Mac"))
        db.commit()
    push, epostalar = SahtePush(), []
    monkeypatch.setattr(servisler, "push_gonder", push)
    monkeypatch.setattr(servisler, "eposta_gonder", lambda kime, konu, metin, **k: epostalar.append(metin))
    with OturumYapici() as db:
        api.kullaniciya_hatirlat(db, db.get(Kullanici, uid), an)
    assert push.gonderilen[0]["govde"].endswith(" · Microsoft bağlantısı yenilenmeli")
    assert "Microsoft bağlantısı yenilenmeli · Yeniden bağlan: https://rapor.ornek.com/oauth/microsoft/basla" in epostalar[0]
    assert "Google" not in epostalar[0]


def test_hatirlatma_ozeti_saglayicidan_bagimsiz_toplam():
    maddeler = [Madde(kaynak=k) for k in ("eposta", "outlook", "outlook", "takvim", "drive", "onedrive", "medusa")]
    assert api.hatirlatma_ozeti(maddeler) == "Bugünün raporu hazır bekliyor · 3 e-posta, 1 commit, 1 toplantı, 2 dosya bulundu"


# ---------------------------------------------------------------- 5) bağlantıyı kaldır

def test_kaldir_alanlari_temizler_bilgi_notu_doner(ms):
    uid = kullanici_olustur()
    ms_bagla(uid, kaynaklar={"gmail": True, "github": False, "medusa": False, **MS_KAYNAKLARI})
    api._ms_erisim[uid] = (ERISIM, 9e12)
    y = istemci().post("/oauth/microsoft/kaldir")
    assert y.status_code == 200
    assert y.json()["bilgi"] == "Hesabından tamamen kaldırmak için account.microsoft.com → Gizlilik → Uygulamalar"
    assert y.json()["microsoft"]["bagli"] is False
    a = ayar(uid)
    assert (a.ms_refresh_enc, a.ms_eposta, a.ms_baglanti, a.ms_durum, a.ms_kapsamlar) == (None,) * 5
    assert {k: a.kaynaklar[k] for k in MS_KAYNAKLARI} == {k: False for k in MS_KAYNAKLARI}
    assert a.kaynaklar["gmail"] is True  # diğer kaynaklara dokunulmaz
    assert uid not in api._ms_erisim
    assert not ms.yollar("oauth2")  # Graph'ta revoke yok


# ---------------------------------------------------------------- 6) Outlook e-posta

def alicilar(liste: str) -> list[dict]:
    """Graph görünen adı boşsa adresin kendisini verir."""
    return [{"emailAddress": {"name": ad or adres, "address": adres}} for ad, adres in getaddresses([liste]) if adres]


def gmesaj(kime: str, konu: str, saat="10:00", cc="", gun_=GUN, kimden=MS, kimlik_=None) -> dict:
    zaman = istanbul(gun_, *map(int, saat.split(":")))
    return {
        "id": kimlik_ or "AAMk" + hashlib.sha1((konu + saat + kime).encode()).hexdigest()[:8],
        "subject": konu, "sentDateTime": iso(zaman),
        "internetMessageId": f"<{hashlib.sha1((konu + saat).encode()).hexdigest()[:8]}@EUR.PROD.OUTLOOK.COM>",
        "from": {"emailAddress": {"name": "Ufuk Çetinkaya", "address": kimden}},
        "toRecipients": alicilar(kime), "ccRecipients": alicilar(cc),
    }


def baslik_sozlugu(m: dict) -> dict:
    """Aynı mailin IMAP/Gmail yolundaki başlık hâli: beklenen maddeler buradan üretilir."""
    adresler = lambda liste: ", ".join(f'"{a["emailAddress"]["name"]}" <{a["emailAddress"]["address"]}>' for a in liste)  # noqa: E731
    return {"date": format_datetime(servisler.iso_zaman(m["sentDateTime"])), "subject": m["subject"],
            "from": adresler([m["from"]]), "to": adresler(m["toRecipients"]), "cc": adresler(m["ccRecipients"]),
            "message-id": m["internetMessageId"]}


def outlook_ornekleri() -> list[dict]:
    return [
        gmesaj("Ayşe <ayse@msg.org.tr>", "Ağustos CRD itirazı", "09:12"),
        gmesaj("ayse@msg.org.tr", "Re: ağustos crd İTİRAZI ", "14:37"),
        gmesaj("crd@msg.org.tr", "YNT: Ynt:  Ağustos  CRD itirazı", "11:05"),
        gmesaj("MESAM <a@mesam.org.tr>, b@msg.org.tr", "Tanıtım", cc="c@imro.ie, Ali <ali@medusarights.com>"),
        gmesaj("ali@medusarights.com", "Haftalık plan"),  # ekip içi (Microsoft adresinin alan adı): atlanır
        gmesaj(MS, "rapor: Katalog Coverz'e gönderildi"),
        gmesaj(MS, "not:", saat="16:00", kimlik_="AAMk-not"),
    ]


def test_outlook_sorgu_sayfalama_ve_imapla_ayni_maddeler(ms):
    mailler = outlook_ornekleri()
    ms.mesaj_sayfalari = [mailler[:3], mailler[3:6], mailler[6:]]
    ms.govdeler = {"AAMk-not": "MSG ile görüşüldü\r\n\r\nIMRO takip\r\n"}
    kendi = servisler.kendi_alanlari([BEN, MS])
    sonuc = servisler.outlook_tara("erisim", MS, GUN, kendi_alanlar=kendi, istemci=ms.istemci())

    liste = ms.yollar("sentitems/messages")
    assert len(liste) == 3  # @odata.nextLink ile sayfalı
    p = liste[0].url.params
    assert p["$filter"] == "sentDateTime ge 2026-09-15T21:00:00Z and sentDateTime lt 2026-09-16T21:00:00Z"
    assert (p["$select"], p["$top"]) == ("subject,toRecipients,ccRecipients,sentDateTime,internetMessageId,from", "50")
    assert "$filter=sentDateTime%20ge%20" in str(liste[0].url)  # OData adı '$' ile, boşluk %20
    govde = ms.yollar("/me/messages/AAMk-not")
    assert len(govde) == 1 and govde[0].url.params["$select"] == "body"
    assert govde[0].headers["prefer"] == 'outlook.body-content-type="text"'
    assert all(i.headers["authorization"] == "Bearer erisim" for i in ms.istekler)

    kucuk = [baslik_sozlugu(m) for m in mailler]
    kucuk[6]["govde"] = ms.govdeler["AAMk-not"]
    notlar = [m for m in kucuk if servisler.not_konusu(m, MS) is not None]
    gonderilen = [m for m in kucuk if servisler.not_konusu(m, MS) is None]
    beklenen = [{**m, "id": "ms-" + m["id"], "kaynak": "outlook"}
                for m in servisler.epostalari_maddele(gonderilen, MS, GUN, kendi_alanlar=kendi)] \
        + servisler.notlari_maddele(notlar, MS, GUN)
    assert sonuc == beklenen
    # G1'deki Gmail REST testiyle aynı metinler
    assert [m["metin"] for m in sonuc] == [
        "MSG'ye 'Ağustos CRD itirazı' konulu 3 e-posta gönderildi",
        "MESAM'a 'Tanıtım' konulu e-posta gönderildi (ayrıca MSG, IMRO)",
        "Katalog Coverz'e gönderildi", "MSG ile görüşüldü", "IMRO takip",
    ]
    assert [m["kaynak"] for m in sonuc] == ["outlook", "outlook", "not", "not", "not"]
    assert all(m["id"].startswith("ms-") for m in sonuc[:2]) and all(m["id"].startswith("not-") for m in sonuc[2:])


def test_outlook_yabanci_nextlink_izlenmez(ms):
    def isleyici(istek):
        return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://kotu.ornek.com/sayfa2"})
    istekler = []
    istemci_ = httpx.Client(transport=httpx.MockTransport(lambda i: istekler.append(i) or isleyici(i)))
    assert servisler.outlook_tara("t", MS, GUN, istemci=istemci_) == []
    assert len(istekler) == 1  # token başka adrese gitmez


def test_outlook_ozel_karakterli_ad_ve_gruplama(ms):
    mailler = [gmesaj("\"Öztürk, Ayşe\" <a@mesam.org.tr>", "Fatura"), gmesaj("a@mesam.org.tr", "Sözleşme", "11:00"),
               gmesaj("\"Şule\" <b@coverz.com>", "Katalog", "12:00")]
    mailler[2]["toRecipients"][0]["emailAddress"]["address"] = "şule@coverz.com"  # ASCII dışı adres taramayı düşürmez
    ms.mesaj_sayfalari = [mailler]
    sonuc = servisler.outlook_tara("t", MS, GUN, gruplama="alici", istemci=ms.istemci())
    assert [m["metin"] for m in sonuc] == ["MESAM'a 2 e-posta gönderildi (konular: Fatura; Sözleşme)",
                                           "Coverz'e 'Katalog' konulu e-posta gönderildi"]


def test_outlook_403_hatasi_kaynak_hatasi(ms):
    ms.kodlar["/me/mailFolders/sentitems/messages"] = 403
    with pytest.raises(servisler.KaynakHatasi, match=r"Outlook \(Microsoft\) erişimi reddedildi \(403: Access is denied"):
        servisler.outlook_tara("t", MS, GUN, istemci=ms.istemci())


def test_api_taramasi_e2_ve_ad_eslemesi_outlook_maddelerinde(ms, gun):
    esleme = [{"kaynak": "MESAM", "hedef": "Meslek Birliği"}]
    uid = kullanici_olustur(ad_eslemeleri=esleme, ekip_ici_atla=True)
    ms_bagla(uid)
    ms.mesaj_sayfalari = [[gmesaj("a@mesam.org.tr", "Tanıtım", cc="ali@medusarights.com"), gmesaj("ali@medusarights.com", "Plan"),
                           gmesaj(MS, "rapor: Katalog 'MESAM Liste' gönderildi")]]
    c = istemci()
    d = c.get("/api/bugun?yenile=1").json()
    outlook = [m for m in d["bulunan"] if m["kaynak"] == "outlook"]
    assert [m["metin"] for m in outlook] == ["MESAM'a 'Tanıtım' konulu e-posta gönderildi"]  # ekip içi ve kendi şirketi yok
    assert outlook[0]["rapor_metni"] == "Meslek Birliği'ne 'Tanıtım' konulu e-posta gönderildi"  # ad eşlemesi
    assert d["sayim"]["outlook"] == 1
    a = c.get("/api/ayarlar").json()
    assert "medusarights.com" in a["kendi_alanlar_otomatik"]  # Microsoft adresinin alan adı kendi şirketi sayılır
    durum = c.get("/api/durum").json()
    assert "• Katalog 'Meslek Birliği Liste' gönderildi" in durum["rapor_metni"]  # not maddesi yapılanlara düşer, eşlenir


def test_claude_girdisinde_outlook_onedrive_ortak_adla_tirnak_korunur(sahte_claude):
    maddeler = [{"id": "ms-1", "metin": "MESAM'a 'Katalog 2026' konulu e-posta gönderildi", "kaynak": "outlook", "kaynak_zaman": None},
                {"id": "od1", "metin": "'Katalog 2026' tablosu güncellendi", "kaynak": "onedrive", "kaynak_zaman": None}]
    sahte_claude.yanitlar = [json.dumps([{"id": "ms-1", "metin": "MESAM'a katalog e-postası gönderildi."},
                                         {"id": "od1", "metin": "'Katalog 2026' tablosu güncellendi."}], ensure_ascii=False)]
    sonuc, hata = servisler.claude_cevir(maddeler, "k")
    assert hata is None
    girdi = json.loads(sahte_claude.istekler[0]["messages"][0]["content"].split("\n", 1)[1])
    assert [g["kaynak"] for g in girdi] == ["eposta", "drive"]
    assert [m["metin"] for m in sonuc] == ["MESAM'a 'Katalog 2026' konulu e-posta gönderildi",  # tırnak bozuldu: ham
                                           "'Katalog 2026' tablosu güncellendi."]
    assert [m["kaynak"] for m in sonuc] == ["outlook", "onedrive"]


# ---------------------------------------------------------------- 7) Google + Outlook birlikte

def test_gmail_ve_outlook_birlikte_iki_kaynak_cakisma_yok(ms, gun, monkeypatch):
    from test_g1 import TUM_KAPSAMLAR as G_KAPSAMLAR, gmail_kur, mail
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "g.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-g")
    google = SahteGoogle()
    monkeypatch.setattr(servisler, "google_istemci", google.istemci)
    uid = kullanici_olustur()
    ms_bagla(uid, kaynaklar={"gmail": True, "github": False, "medusa": False, "takvim": True, "drive": False, **MS_KAYNAKLARI})
    with OturumYapici() as db:
        a = db.get(KullaniciAyari, uid)
        a.google_refresh_enc, a.google_eposta, a.google_durum = guvenlik.sifrele("1//g"), BEN, "bagli"
        a.google_kapsamlar, a.google_baglanti = list(G_KAPSAMLAR), datetime.now(timezone.utc)
        db.commit()
    gmail_kur(google, [mail("a@mesam.org.tr", "Tanıtım")])  # aynı gün, aynı kurum, aynı konu
    ms.mesaj_sayfalari = [[gmesaj("a@mesam.org.tr", "Tanıtım", "12:00")]]
    google.etkinlikler = [{"id": "g1", "summary": "Google toplantısı", "status": "confirmed", "organizer": {"self": True},
                           "start": {"dateTime": istanbul(GUN, 9).isoformat()}, "end": {"dateTime": istanbul(GUN, 10).isoformat()},
                           "attendees": [{"email": "x@coverz.com", "responseStatus": "accepted"}]}]
    ms.etkinlikler = [ms_etkinlik("o1", "Outlook toplantısı", istanbul(GUN, 11), istanbul(GUN, 12),
                                  katilimcilar=[("IMRO", "b@imro.ie")], duzenleyen_adres="b@imro.ie")]
    c = istemci()
    d = c.get("/api/bugun?yenile=1").json()
    epostalar = bulunanlar(uid, "eposta") + bulunanlar(uid, "outlook")
    assert [(m.kaynak, m.metin) for m in epostalar] == [("eposta", "MESAM'a 'Tanıtım' konulu e-posta gönderildi"),
                                                        ("outlook", "MESAM'a 'Tanıtım' konulu e-posta gönderildi")]
    assert epostalar[1].kaynak_id == "ms-" + epostalar[0].kaynak_id
    assert sorted(m.metin for m in bulunanlar(uid, "takvim")) == ["'Google toplantısı' toplantısı yapıldı (Coverz ile)",
                                                                   "'Outlook toplantısı' toplantısı yapıldı (IMRO ile)"]
    assert (d["sayim"]["eposta"], d["sayim"]["outlook"], d["sayim"]["takvim"]) == (1, 1, 2)
    c.get("/api/bugun?yenile=1")  # ikinci tarama çoğaltmaz
    assert len(bulunanlar(uid)) == 4
    # Google takvimi hata verirse Microsoft maddeleri eklenir ama hiçbir takvim maddesi gizlenmez
    google.takvim_kodu, ms.etkinlikler = 500, []
    d = c.get("/api/bugun?yenile=1").json()
    assert "takvim" not in d["sayim"] and all(not m.gizli for m in bulunanlar(uid, "takvim"))


# ---------------------------------------------------------------- 8) Takvim / Teams

def ms_zaman(z: datetime) -> dict:
    return {"dateTime": z.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "Europe/Istanbul"}


def ms_etkinlik(eid, konu, bas, bit, yanit="accepted", katilimcilar=(), duzenleyen=False, onizleme="", iptal=False,
                tum_gun=False, teams=False, duzenleyen_adres="d@mesam.org.tr", odalar=()):
    return {
        "id": eid, "subject": konu, "isCancelled": iptal, "isOrganizer": duzenleyen, "isAllDay": tum_gun,
        "responseStatus": {"response": "organizer" if duzenleyen else yanit},
        "start": ms_zaman(bas), "end": ms_zaman(bit), "bodyPreview": onizleme,
        "isOnlineMeeting": teams, "onlineMeetingProvider": "teamsForBusiness" if teams else "unknown",
        "organizer": {"emailAddress": {"name": "Ufuk" if duzenleyen else "", "address": MS if duzenleyen else duzenleyen_adres}},
        "attendees": ([] if duzenleyen else [{"type": "required", "emailAddress": {"name": "Ufuk", "address": MS}}])
        + [{"type": "required", "emailAddress": {"name": ad, "address": a}} for ad, a in katilimcilar]
        + [{"type": "resource", "emailAddress": {"name": ad, "address": a}} for ad, a in odalar],
    }


def ms_takvim_ornekleri() -> list[dict]:
    s = lambda ss, dd=0: istanbul(GUN, ss, dd)  # noqa: E731
    dis = [("MESAM", "a@mesam.org.tr"), ("", "b@msg.org.tr"), ("Ali", "ali@medusarights.com")]
    oda = [("Toplantı Odası 1", "oda1@medusarights.com")]
    return [
        ms_etkinlik("m1", "Tanıtım", s(10), s(11), katilimcilar=dis, odalar=oda),
        ms_etkinlik("m2", "Reddedilen", s(11), s(12), yanit="declined", katilimcilar=dis),
        ms_etkinlik("m3", "İptal", s(12), s(13), katilimcilar=dis, iptal=True),
        ms_etkinlik("m4", "Yanıtlanmamış", s(12), s(13), yanit="notResponded", katilimcilar=dis),
        ms_etkinlik("m5", "Belki", s(13), s(13, 30), yanit="tentativelyAccepted", katilimcilar=[("Coverz Ekibi", "x@coverz.com")],
                    duzenleyen_adres="x@coverz.com"),
        ms_etkinlik("m6", "Odak zamanı", s(14), s(15), duzenleyen=True),  # kendine blok
        ms_etkinlik("m7", "Müşteri notları", s(15), s(16), duzenleyen=True, onizleme="Sözleşme maddeleri"),
        ms_etkinlik("m8", "Ekip toplantısı", s(9), s(9, 30), duzenleyen=True, teams=True, katilimcilar=[("Ali", "ali@medusarights.com")]),
        ms_etkinlik("m9", "Haftalık senkron", s(8), s(8, 30), teams=True, katilimcilar=[("MESAM", "a@mesam.org.tr")]),
        ms_etkinlik("m10", "Akşam görüşmesi", s(18), s(19), katilimcilar=dis),  # an 17:30: henüz bitmedi
        ms_etkinlik("m11", "Fuar", istanbul(GUN, 0), istanbul(GUN + timedelta(days=1), 0), duzenleyen=True, onizleme="Stand",
                    tum_gun=True),
        ms_etkinlik("m12", "Görüşme", s(16), s(16, 30), duzenleyen_adres="k@imro.ie"),  # yalnız düzenleyen dışarıdan
        ms_etkinlik("m13", "Oda ayırma", s(17), s(17, 15), duzenleyen=True, odalar=oda),  # oda katılımcı sayılmaz
    ]


def test_outlook_takvim_dahil_haric_teams_ve_oda():
    kendi = servisler.kendi_alanlari([BEN, MS])
    an = istanbul(GUN, 17, 30)
    sonuc = servisler.outlook_takvim_maddeleri(ms_takvim_ornekleri(), GUN, MS, kendi_alanlar=kendi, an=an)
    assert [m["metin"] for m in sonuc] == [
        "'Tanıtım' toplantısı yapıldı (MESAM, MSG ile)",  # kendi şirketi (Ali) ve toplantı odası yazılmaz
        "'Belki' toplantısı yapıldı (Coverz ile)",
        "'Müşteri notları' toplantısı yapıldı",
        "'Ekip toplantısı' Teams toplantısı yapıldı",
        "'Haftalık senkron' Teams toplantısı yapıldı (MESAM ile)",
        "'Fuar' (tüm gün)",
        "'Görüşme' toplantısı yapıldı (IMRO ile)",
    ]
    assert all(m["kaynak"] == "takvim" for m in sonuc)
    assert sonuc[0]["id"] == hashlib.sha1(("m1" + GUN.isoformat()).encode()).hexdigest()[:10]
    assert sonuc[0]["kaynak_zaman"] == istanbul(GUN, 10) and sonuc[5]["kaynak_zaman"] == istanbul(GUN, 0)
    gece = servisler.outlook_takvim_maddeleri(ms_takvim_ornekleri(), GUN, MS, kendi_alanlar=kendi, an=istanbul(GUN, 23))
    assert "'Akşam görüşmesi' toplantısı yapıldı (MESAM, MSG ile)" in [m["metin"] for m in gece]


def test_graph_zamani_utc_ve_yedi_haneli_kesir():
    assert servisler.graph_zamani({"dateTime": "2026-09-16T07:00:00.0000000", "timeZone": "UTC"}) == istanbul(GUN, 10)
    assert servisler.graph_zamani({"dateTime": "2026-09-16T10:00:00.1234567", "timeZone": "Turkey Standard Time"}) \
        == istanbul(GUN, 10).replace(microsecond=123456)
    assert servisler.graph_zamani({"dateTime": "yok"}) is None and servisler.graph_zamani(None) is None


def test_outlook_takvim_istegi(ms):
    servisler.outlook_takvim_tara("t", MS, date(2026, 9, 10), istemci=ms.istemci())
    istek = ms.yollar("/me/calendarView")[0]
    p = istek.url.params
    assert (p["startDateTime"], p["endDateTime"]) == ("2026-09-09T21:00:00Z", "2026-09-10T21:00:00Z")
    assert p["$select"] == ("subject,start,end,isAllDay,isCancelled,isOrganizer,responseStatus,attendees,organizer,"
                            "bodyPreview,isOnlineMeeting,onlineMeetingProvider")
    assert istek.headers["prefer"] == 'outlook.timezone="Europe/Istanbul"'


# ---------------------------------------------------------------- 9) OneDrive

def od(iid, ad, olusturma, degisme, kim=MS, surucu="b!surucu1", klasor=False, uzak=False) -> dict:
    oge = {"id": iid, "name": ad, "createdDateTime": iso(olusturma), "lastModifiedDateTime": iso(degisme),
           "parentReference": {"driveId": surucu}, "lastModifiedBy": {"user": {"displayName": "X", "email": kim}}}
    oge["folder" if klasor else "file"] = {"childCount": 1} if klasor else {"mimeType": "application/octet-stream"}
    if uzak:  # başkasının sürücüsünde: asıl alanlar remoteItem'da
        return {"id": "yerel-" + iid, "name": ad, "lastModifiedDateTime": iso(degisme), "remoteItem": oge}
    return oge


def test_onedrive_me_suzgeci_olusturuldu_guncellendi_uzanti_turu():
    dun = istanbul(GUN - timedelta(days=3), 9)
    ogeler = [
        od("i1", "Katalog 2026.xlsx", istanbul(GUN, 9), istanbul(GUN, 10)),
        od("i2", "Sözleşme taslağı.docx", dun, istanbul(GUN, 11)),
        od("i3", "Fatura.pdf", dun, istanbul(GUN, 12)),
        od("i4", "Sunum.pptx", istanbul(GUN, 0, 30), istanbul(GUN, 13)),
        od("i5", "Logo.png", dun, istanbul(GUN, 14)),
        od("i6", "Başkasının tablosu.xlsx", dun, istanbul(GUN, 15), kim="baska@medusarights.com"),
        od("i7", "Rapor v2.1", dun, istanbul(GUN, 8)),
        od("i8", "Klasör", dun, istanbul(GUN, 9), klasor=True),
        od("i9", "Dünkü.docx", dun, istanbul(GUN - timedelta(days=1), 9)),
        od("i10", "Paylaşılan.xlsx", dun, istanbul(GUN, 16), kim=MS.upper(), surucu="b!surucu2", uzak=True),
    ]
    sonuc = servisler.onedrive_maddeleri(ogeler, GUN, MS)
    assert [m["metin"] for m in sonuc] == [
        "'Rapor v2.1' dosyası güncellendi",  # ".1" uzantı sayılmaz (Drive'daki gibi), tür "dosya"
        "'Katalog 2026' tablosu oluşturuldu",
        "'Sözleşme taslağı' belgesi güncellendi",
        "'Fatura' PDF'i güncellendi",
        "'Sunum' sunumu oluşturuldu",
        "'Logo' dosyası güncellendi",
        "'Paylaşılan' tablosu güncellendi",
    ]
    assert all(m["kaynak"] == "onedrive" for m in sonuc)
    assert sonuc[1]["kaynak_zaman"] == istanbul(GUN, 10)
    assert sonuc[1]["id"] == hashlib.sha1(("b!surucu1" + "i1" + GUN.isoformat()).encode()).hexdigest()[:10]
    assert sonuc[-1]["id"] == hashlib.sha1(("b!surucu2" + "i10" + GUN.isoformat()).encode()).hexdigest()[:10]
    assert [servisler.onedrive_turu(a) for a in ("a.XLSX", "b.doc", "c.ppt", "d.csv", "e.mp4", "uzantisiz")] == [
        "tablo", "belge", "sunum", "tablo", "dosya", "dosya"]


def test_onedrive_on_bes_siniri():
    ogeler = [od(f"i{i}", f"Belge {i}.docx", istanbul(GUN - timedelta(days=1), 9), istanbul(GUN, 8, i)) for i in range(18)]
    sonuc = servisler.onedrive_maddeleri(ogeler, GUN, MS)
    assert len(sonuc) == 16 and sonuc[-1]["metin"] == "ve 3 dosya daha güncellendi"
    assert [m["metin"] for m in sonuc[:2]] == ["'Belge 0' belgesi güncellendi", "'Belge 1' belgesi güncellendi"]
    assert sonuc[-1]["id"] == servisler.onedrive_maddeleri(ogeler[:16], GUN, MS)[-1]["id"]  # sayı değişse de aynı madde
    assert sonuc[-1]["id"] != servisler.kaynak_kimligi("drive-fazla", GUN)  # Drive'ın "ve N dosya daha"sıyla çakışmaz


def test_onedrive_istegi(ms):
    ms.dosyalar = [od("i1", "A.docx", istanbul(GUN, 9), istanbul(GUN, 9))]
    assert [m["metin"] for m in servisler.onedrive_tara("t", MS, GUN, istemci=ms.istemci())] == ["'A' belgesi oluşturuldu"]
    assert str(ms.yollar("/me/drive/recent")[0].url) == "https://graph.microsoft.com/v1.0/me/drive/recent"


# ---------------------------------------------------------------- 10) tarama: kategori, gizleme, geçmiş gün, kapalı kaynak

def test_taramada_kategori_eslemesi(ms, gun):
    uid = kullanici_olustur()
    ms_bagla(uid)
    ms.mesaj_sayfalari = [[gmesaj("a@mesam.org.tr", "Tanıtım")]]
    ms.etkinlikler = ms_takvim_ornekleri()[:1]
    ms.dosyalar = [od("i1", "Katalog.xlsx", istanbul(GUN, 9), istanbul(GUN, 10))]
    c = istemci()
    kategoriler = {k["ad"]: k["id"] for k in c.get("/api/kategoriler").json()["kategoriler"]}
    toplanti = c.post("/api/kategoriler", json={"ad": "Toplantılar", "kaynaklar": ["takvim"]}).json()["id"]
    dosyalar = c.post("/api/kategoriler", json={"ad": "Dosyalar", "kaynaklar": ["onedrive"]}).json()["id"]
    assert c.post("/api/kategoriler", json={"ad": "X", "kaynaklar": ["outlook_takvim"]}).status_code == 422  # Takvim tek pil
    d = c.get("/api/bugun?yenile=1").json()
    assert d["sayim"] == {"eposta": 0, "medusa": 0, "outlook": 1, "takvim": 1, "onedrive": 1}
    durum = c.get(f"/api/durum?tarih={GUN}").json()
    etkin = {m["kaynak"]: m["etkin_kategori_id"] for m in durum["maddeler"] if m["tur"] == "bulunan"}
    # Outlook için kategori seçilmemiş: Gmail'in kategorisine (Yazışmalar) düşer
    assert etkin == {"outlook": kategoriler["Yazışmalar"], "takvim": toplanti, "onedrive": dosyalar}
    assert "*Dosyalar:*\n• 'Katalog' tablosu oluşturuldu" in durum["rapor_metni"]
    ozel = c.post("/api/kategoriler", json={"ad": "Outlook", "kaynaklar": ["outlook"]}).json()["id"]
    etkin = {m["kaynak"]: m["etkin_kategori_id"] for m in c.get(f"/api/durum?tarih={GUN}").json()["maddeler"] if m["tur"] == "bulunan"}
    assert etkin["outlook"] == ozel
    ilk = [(m.kaynak_id, m.id) for m in bulunanlar(uid)]
    c.get("/api/bugun?yenile=1")
    assert [(m.kaynak_id, m.id) for m in bulunanlar(uid)] == ilk  # kaynak_id kararlı, çoğalmaz


def test_artik_uretilmeyen_maddeler_gizlenir_hata_olursa_gizlenmez(ms, gun):
    uid = kullanici_olustur()
    ms_bagla(uid)
    ms.mesaj_sayfalari = [[gmesaj("a@mesam.org.tr", "Tanıtım"), gmesaj("b@msg.org.tr", "Fatura")]]
    ms.etkinlikler = ms_takvim_ornekleri()[:1]
    ms.dosyalar = [od("i1", "A.pdf", istanbul(GUN, 9), istanbul(GUN, 9))]
    c = istemci()
    c.get("/api/bugun?yenile=1")
    ms.kodlar = {"/me/calendarView": 500, "/me/mailFolders/sentitems/messages": 500}
    ms.mesaj_sayfalari, ms.etkinlikler, ms.dosyalar = [[]], [], []
    d = c.get("/api/bugun?yenile=1").json()
    assert {h["kaynak"] for h in d["hatalar"]} == {"takvim", "outlook"}
    assert [m.gizli for m in bulunanlar(uid, "outlook") + bulunanlar(uid, "takvim")] == [False, False, False]
    assert [m.gizli for m in bulunanlar(uid, "onedrive")] == [True]  # OneDrive hatasız tarandı
    ms.kodlar = {}
    ms.mesaj_sayfalari = [[gmesaj("a@mesam.org.tr", "Tanıtım")]]
    c.get("/api/bugun?yenile=1")
    assert {m.metin: m.gizli for m in bulunanlar(uid, "outlook")} == {
        "MESAM'a 'Tanıtım' konulu e-posta gönderildi": False, "MSG'ye 'Fatura' konulu e-posta gönderildi": True}
    assert [m.gizli for m in bulunanlar(uid, "takvim")] == [True]


def test_gecmis_gun_taramasi_dogru_aralik(ms, gun):
    uid = kullanici_olustur()
    ms_bagla(uid)
    gecmis = GUN - timedelta(days=3)
    ms.mesaj_sayfalari = [[gmesaj("a@mesam.org.tr", "Eski", gun_=gecmis)]]
    ms.etkinlikler = [ms_etkinlik("g1", "Eski", istanbul(gecmis, 10), istanbul(gecmis, 11), katilimcilar=[("", "a@mesam.org.tr")])]
    ms.dosyalar = [od("i1", "Eski.docx", istanbul(gecmis, 9), istanbul(gecmis, 9)), od("i2", "Yeni.docx", istanbul(GUN, 9), istanbul(GUN, 9))]
    c = istemci()
    d = c.get(f"/api/bugun?yenile=1&tarih={gecmis}").json()
    assert sorted(m["metin"] for m in d["bulunan"]) == [
        "'Eski' belgesi oluşturuldu", "'Eski' toplantısı yapıldı (MESAM ile)", "MESAM'a 'Eski' konulu e-posta gönderildi"]
    assert ms.yollar("sentitems")[0].url.params["$filter"] == \
        "sentDateTime ge 2026-09-12T21:00:00Z and sentDateTime lt 2026-09-13T21:00:00Z"
    p = ms.yollar("calendarView")[0].url.params
    assert (p["startDateTime"], p["endDateTime"]) == ("2026-09-12T21:00:00Z", "2026-09-13T21:00:00Z")
    ms.etkinlikler, ms.dosyalar = [], []
    c.get(f"/api/bugun?yenile=1&tarih={gecmis}")
    assert [m.gizli for m in bulunanlar(uid, "takvim", gecmis) + bulunanlar(uid, "onedrive", gecmis)] == [False, False]


def test_kapali_microsoft_kaynagi_taranmaz_ve_gorunmez(ms, gun):
    uid = kullanici_olustur()
    ms_bagla(uid)
    ms.dosyalar = [od("i1", "A.docx", istanbul(GUN, 9), istanbul(GUN, 9))]
    c = istemci()
    c.get("/api/bugun?yenile=1")
    assert len(bulunanlar(uid, "onedrive")) == 1
    c.put("/api/ayarlar", json={"kaynaklar": {"outlook": False, "onedrive": False}})
    ms.istekler.clear()
    d = c.get("/api/bugun?yenile=1").json()
    assert ms.yollar("calendarView") and not ms.yollar("sentitems") and not ms.yollar("drive/recent")
    assert not [m for m in d["bulunan"] if m["kaynak"] == "onedrive"]  # kapalı kaynağın maddesi gösterilmez


def test_microsoft_takvimi_acikken_google_takvimi_kapaliysa_takvim_gorunur(ms, gun):
    uid = kullanici_olustur()
    ms_bagla(uid, kaynaklar={"outlook": False, "outlook_takvim": True, "onedrive": False})
    ms.etkinlikler = ms_takvim_ornekleri()[:1]
    d = istemci().get("/api/bugun?yenile=1").json()
    assert [m["kaynak"] for m in d["bulunan"]] == ["takvim"]
    assert api.acik_bulunan_kaynaklari(ayar(uid)) == {"takvim"}


# ---------------------------------------------------------------- 11) arayüz

def test_microsoft_yoksa_arayuz_gizli(ms, monkeypatch):
    kullanici_olustur()
    c = istemci()
    for sayfa in ("/ayarlar", "/kurulum"):
        assert "Microsoft ile bağlan" in c.get(sayfa).text
    monkeypatch.delenv("MICROSOFT_CLIENT_SECRET")
    for sayfa in ("/ayarlar", "/kurulum"):
        html = c.get(sayfa).text
        assert 'id="msSatir"' not in html and 'id="msBaglan"' not in html and 'id="kaynak_outlook"' not in html
        assert "Microsoft ile bağlan" not in html and "Uygulama şifresiyle bağlan" not in html
    a = c.get("/api/ayarlar").json()
    assert a["microsoft"]["ayarli"] is False and set(a["kaynaklar"]) == {"gmail", "github", "medusa"}
    yol, q = yonlendirme(c.get("/oauth/microsoft/basla?donus=/kurulum"))
    assert (yol, q["microsoft"]) == ("/kurulum", "hata")


def test_ayarlar_kurulum_ve_bugun_sayfalarinda_microsoft_ogeleri(ms, monkeypatch):
    kullanici_olustur()
    c = istemci()
    ayarlar = c.get("/ayarlar").text
    for parca in ('id="msSatir"', 'id="kaynak_outlook"', 'id="kaynak_outlook_takvim"', 'id="kaynak_onedrive"',
                  "Şirket hesabında yönetici onayı istenebilir", "Bağlantıyı kaldır", "account.microsoft.com",
                  '["gmail", "outlook", "github", "medusa", "takvim", "onedrive"]', "outlook:'Outlook'", "onedrive:'OneDrive'"):
        assert parca in ayarlar, parca
    kurulum = c.get("/kurulum").text
    assert 'id="msBaglan"' in kurulum and "Uygulama şifresiyle bağlan" in kurulum and "Microsoft hesabını bağla" in kurulum
    bugun = c.get("/").text
    assert 'data-kaynak-cip="gmail outlook"' in bugun and 'data-kaynak-cip="drive onedrive"' in bugun
    assert 'id="microsoftSerit"' in bugun
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "g")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "s")
    ayarlar = c.get("/ayarlar").text
    assert '["gmail", "outlook", "github", "medusa", "takvim", "drive", "onedrive"]' in ayarlar
    assert 'Takvim<span class="saglayici">Google</span>' in ayarlar and 'Takvim<span class="saglayici">Microsoft</span>' in ayarlar
    kurulum = c.get("/kurulum").text
    assert 'id="googleBaglan"' in kurulum and 'id="msBaglan"' in kurulum and "Hesabını bağla" in kurulum


def test_sema_guncelle_ms_kolonlari_idempotent(tmp_path):
    from sqlalchemy import create_engine, text

    import veritabani
    eski = create_engine(f"sqlite:///{tmp_path}/eski.db")
    Temel.metadata.create_all(eski)
    kolonlar = ("ms_refresh_enc", "ms_eposta", "ms_baglanti", "ms_durum", "ms_kapsamlar")
    with eski.begin() as b:
        for kolon in kolonlar:
            b.execute(text(f"ALTER TABLE user_settings DROP COLUMN {kolon}"))
    assert veritabani.sema_guncelle(eski) == [f"user_settings.{k}" for k in kolonlar]
    assert veritabani.sema_guncelle(eski) == []
    eski.dispose()
