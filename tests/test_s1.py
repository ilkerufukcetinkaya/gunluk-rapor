"""S1: sesle madde ekleme — dikte metni Claude ile maddelere bölünür, onaylananlar yazılır.
sqlite; Claude sahte."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import api
import app as uygulama
import guvenlik
import servisler
from veritabani import ClaudeKullanim, Kullanici, KullaniciAyari, Madde, OturumYapici, Temel, motor

SIFRE = "dogru-sifre-123"
GUN = date(2026, 9, 23)
DIKTE = ("şey bugün MESAM'la 12 eser için görüştüm yani ve sonra Köprü Film'e sözleşme taslağını gönderdim "
         "MSG ağustos itirazı da devam ediyor yanıt bekliyoruz")


@pytest.fixture(autouse=True)
def ortam(monkeypatch):
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    api.onbellegi_temizle()
    monkeypatch.setattr(api, "bugun", lambda: GUN)
    monkeypatch.setattr(servisler, "gmail_tara", lambda *a, **k: [])
    monkeypatch.setattr(servisler, "github_tara", lambda *a, **k: [])
    yield


def kullanici_olustur(eposta="a@ilsvision.com", ad="A") -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad=ad, sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol="uye", aktif=True,
                      sifre_degistirmeli=False, olusturma=datetime(2026, 9, 1, tzinfo=timezone.utc))
        db.add(k)
        db.commit()
        db.add(KullaniciAyari(user_id=k.id, kurulum_tamam=True, proje_adi="MEDUSA", rapor_basligi="Günlük Rapor",
                              kaynaklar={"gmail": False, "github": False, "medusa": False},
                              alan_sozlugu={"ilsvision.com": "ILS Vision"}))
        db.commit()
        return k.id


def istemci(eposta="a@ilsvision.com") -> TestClient:
    c = TestClient(uygulama.app, follow_redirects=False)
    assert c.post("/giris", data={"eposta": eposta, "sifre": SIFRE}).status_code == 303
    return c


def kategoriler(c) -> dict[str, dict]:
    return {k["ad"]: k for k in c.get("/api/kategoriler").json()["kategoriler"]}


def cagri_sayisi(uid: int) -> int:
    with OturumYapici() as db:
        return db.scalar(select(ClaudeKullanim.cagri).where(ClaudeKullanim.user_id == uid, ClaudeKullanim.tarih == GUN)) or 0


def satirlar(uid: int) -> list[Madde]:
    with OturumYapici() as db:
        return db.scalars(select(Madde).where(Madde.user_id == uid).order_by(Madde.id)).all()


# ---------------------------------------------------------------- bölme: Claude

def test_claude_boler_kategori_ve_devam_onerir(sahte_claude):
    uid = kullanici_olustur()
    with istemci() as c:
        k = kategoriler(c)

        def yanit(istek):
            assert istek["model"] == "claude-sonnet-5" and istek["thinking"] == {"type": "disabled"}
            assert istek["max_tokens"] == 1500
            icerik = istek["messages"][0]["content"]
            liste = json.loads(icerik.split("Rapor kategorileri:\n", 1)[1].split("\n\n", 1)[0])
            assert [x["ad"] for x in liste] == ["Yazışmalar", "MEDUSA Çalışmaları", "Genel İşler"]  # sistem yok
            assert DIKTE in icerik
            sistem = istek["system"]
            assert "TEK cümle" in sistem and "geçmiş zaman" in sistem and "Olgu ekleme" in sistem
            assert "sayılar aynen korunur" in sistem and "\"şey\"" in sistem and "\"yani\"" in sistem
            assert "devam ediyor" in sistem and "ILS Vision" in sistem and "ASLA yazılmaz" in sistem  # E2
            return "```json\n" + json.dumps([
                {"metin": "MESAM ile 12 eser için görüşüldü.", "kategori_id": k["Yazışmalar"]["id"], "tur": "bugun", "asama": None},
                {"metin": "Köprü Film'e sözleşme taslağı gönderildi.", "kategori_id": str(k["Genel İşler"]["id"]), "tur": "bugun"},
                {"metin": "MSG Ağustos itirazı", "kategori_id": None, "tur": "devam", "asama": "yanıt bekleniyor"},
                {"metin": "  ", "tur": "bugun"},
            ], ensure_ascii=False) + "\n```"

        sahte_claude.yanitlar = [yanit]
        r = c.post("/api/sesli-not", json={"metin": DIKTE})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["hatalar"] == [] and "atlandi" not in d and d["tarih"] == GUN.isoformat()
        assert d["maddeler"] == [
            {"metin": "MESAM ile 12 eser için görüşüldü.", "kategori_id": k["Yazışmalar"]["id"], "tur": "bugun", "asama": None},
            {"metin": "Köprü Film'e sözleşme taslağı gönderildi.", "kategori_id": k["Genel İşler"]["id"], "tur": "bugun", "asama": None},
            {"metin": "MSG Ağustos itirazı", "kategori_id": None, "tur": "devam", "asama": "yanıt bekleniyor"},
        ]
    assert len(sahte_claude.istekler) == 1 and cagri_sayisi(uid) == 1  # düzeltme sayacından 1 çağrı
    assert satirlar(uid) == []  # bölme hiçbir şey yazmaz


def test_sistem_ve_yabanci_kategori_onerisi_reddedilir(sahte_claude):
    uid = kullanici_olustur()
    kullanici_olustur("b@ornek.com", "B")
    with istemci("b@ornek.com") as c:
        yabanci = kategoriler(c)["Genel İşler"]["id"]
    with istemci() as c:
        k = kategoriler(c)
        sahte_claude.yanitlar = [json.dumps([
            {"metin": "A yapıldı.", "kategori_id": k["Önemli Konular"]["id"], "tur": "bugun"},
            {"metin": "B yapıldı.", "kategori_id": k["Devam Eden İşler"]["id"], "tur": "bugun"},
            {"metin": "C yapıldı.", "kategori_id": yabanci, "tur": "bugun"},
            {"metin": "D yapıldı.", "kategori_id": 99999, "tur": "bugun"},
            {"metin": "E yapıldı.", "kategori_id": True, "tur": "bugun"},
            {"metin": "F yapıldı.", "kategori_id": k["Yazışmalar"]["id"], "tur": "garip", "asama": "x"},
        ])]
        d = c.post("/api/sesli-not", json={"metin": "a b c d e f"}).json()
        assert [m["kategori_id"] for m in d["maddeler"]] == [None] * 5 + [k["Yazışmalar"]["id"]]
        assert d["maddeler"][-1]["tur"] == "bugun" and d["maddeler"][-1]["asama"] is None  # bilinmeyen tür → bugün

        # ekleme ucu da sistem ve yabancı kategoriyi reddeder, hiçbir şey yazmaz
        r = c.post("/api/sesli-not/ekle", json={"maddeler": [{"metin": "X"}, {"metin": "Y", "kategori_id": k["Önemli Konular"]["id"]}]})
        assert r.status_code == 422
        r = c.post("/api/sesli-not/ekle", json={"maddeler": [{"metin": "Y", "kategori_id": yabanci}]})
        assert r.status_code == 404
    assert satirlar(uid) == []


# ---------------------------------------------------------------- bölme: yedek

@pytest.mark.parametrize("bozuk", ["Tabii, işte maddeler: MESAM görüşüldü", "[{bozuk json", "[]", '[{"metin": ""}]'])
def test_json_bozuk_basit_bolme(sahte_claude, bozuk):
    uid = kullanici_olustur()
    sahte_claude.yanitlar = [bozuk]
    with istemci() as c:
        d = c.post("/api/sesli-not", json={"metin": "MESAM'la görüştüm. MSG'ye mail attım ve sonra ıslak imza aldım\nirade beyanı düzenlendi"}).json()
    assert [m["metin"] for m in d["maddeler"]] == [
        "MESAM'la görüştüm.", "MSG'ye mail attım", "Islak imza aldım", "İrade beyanı düzenlendi",
    ]
    assert all(m["kategori_id"] is None and m["tur"] == "bugun" and m["asama"] is None for m in d["maddeler"])
    assert "basitçe bölündü" in d["hatalar"][0] and "atlandi" not in d
    assert cagri_sayisi(uid) == 1


def test_claude_hatasi_basit_bolme(sahte_claude):
    kullanici_olustur()
    sahte_claude.yanitlar = [servisler.ClaudeHatasi("API hatası (529: overloaded)")]
    with istemci() as c:
        d = c.post("/api/sesli-not", json={"metin": "bir iş yaptım! sonra da ikincisini."}).json()
    assert [m["metin"] for m in d["maddeler"]] == ["Bir iş yaptım!", "İkincisini."]
    assert "529" in d["hatalar"][0]


def test_kota_dolu_basit_bolme_ve_atlandi(sahte_claude):
    uid = kullanici_olustur()
    with OturumYapici() as db:
        db.add(ClaudeKullanim(user_id=uid, tarih=GUN, cagri=api.GUNLUK_CLAUDE_SINIRI, girdi_token=0, cikti_token=0))
        db.commit()
    with istemci() as c:
        d = c.post("/api/sesli-not", json={"metin": "Birinci iş. İkinci iş"}).json()
    assert d["atlandi"] == "günlük sınır" and d["hatalar"] == []
    assert [m["metin"] for m in d["maddeler"]] == ["Birinci iş.", "İkinci iş"]
    assert sahte_claude.istekler == [] and cagri_sayisi(uid) == api.GUNLUK_CLAUDE_SINIRI


def test_anahtar_yoksa_basit_bolme(monkeypatch):
    kullanici_olustur()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with istemci() as c:
        d = c.post("/api/sesli-not", json={"metin": "tek iş"}).json()
    assert d["atlandi"] == "anahtar yok" and d["maddeler"][0]["metin"] == "Tek iş"


def test_bos_ve_uzun_metin_422(sahte_claude):
    kullanici_olustur()
    with istemci() as c:
        assert c.post("/api/sesli-not", json={"metin": "   "}).status_code == 422
        assert c.post("/api/sesli-not", json={"metin": "a" * (api.SESLI_NOT_SINIRI + 1)}).status_code == 422
        assert c.post("/api/sesli-not/ekle", json={"maddeler": []}).status_code == 422
        assert c.post("/api/sesli-not/ekle", json={"maddeler": [{"metin": " "}]}).status_code == 422
    assert sahte_claude.istekler == []


# ---------------------------------------------------------------- ekleme

def test_ekle_bugun_ses_ve_devam_duzeltme_paketine_girer(sahte_claude):
    uid = kullanici_olustur()
    with istemci() as c:
        k = kategoriler(c)
        r = c.post("/api/sesli-not/ekle", json={"maddeler": [
            {"metin": " MESAM ile görüşüldü. ", "tur": "bugun", "kategori_id": k["Yazışmalar"]["id"]},
            {"metin": "Taslak gönderildi.", "tur": "bugun", "asama": "yok sayılır"},
            {"metin": "MSG Ağustos itirazı", "tur": "devam", "asama": " yanıt bekleniyor "},
        ]})
        assert r.status_code == 201, r.text
        yeni = r.json()["maddeler"]
        assert [(m["tur"], m["kaynak"], m["metin"], m["asama"], m["metin_ai"]) for m in yeni] == [
            ("bugun", "ses", "MESAM ile görüşüldü.", "", None),
            ("bugun", "ses", "Taslak gönderildi.", "", None),
            ("devam", "ses", "MSG Ağustos itirazı", "yanıt bekleniyor", None),
        ]
        assert yeni[0]["kategori_id"] == k["Yazışmalar"]["id"] and yeni[0]["tarih"] == GUN.isoformat()
        assert yeni[2]["tarih"] is None and all(m["tikli"] for m in yeni)

        d = c.get("/api/durum").json()
        assert {m["id"] for m in d["maddeler"]} >= {m["id"] for m in yeni}
        rapor = d["rapor_metni"]
        assert "*Yazışmalar:*\n• MESAM ile görüşüldü." in rapor
        assert "*Genel İşler:*\n• Taslak gönderildi." in rapor
        assert "*Devam Eden İşler:*\n• MSG Ağustos itirazı — yanıt bekleniyor" in rapor

        def yanit(istek):
            girdiler = json.loads(istek["messages"][0]["content"].split("Düzeltilecek maddeler:\n", 1)[1])
            assert {g["id"] for g in girdiler} == {m["id"] for m in yeni}  # metin_ai boş: pakete girer
            assert {g["id"] for g in girdiler if g.get("kategori_sec")} == {yeni[1]["id"]}  # kategorisi olan istemez
            return json.dumps([{"id": g["id"], "metin": g["metin"] + " (d)"} for g in girdiler])

        sahte_claude.yanitlar = [yanit]
        assert c.post("/api/duzelt").json()["duzeltilen"] == 3
        assert "• MESAM ile görüşüldü. (d)" in c.get("/api/durum").json()["rapor_metni"]


def test_tarih_parametresi_gecmis_gune_yazar(sahte_claude):
    uid = kullanici_olustur()
    dun = GUN - timedelta(days=3)
    with istemci() as c:
        sahte_claude.yanitlar = ['[{"metin": "Eski iş yapıldı.", "tur": "bugun", "kategori_id": null}]']
        d = c.post("/api/sesli-not", json={"metin": "eski iş", "tarih": dun.isoformat()}).json()
        assert d["tarih"] == dun.isoformat()
        r = c.post("/api/sesli-not/ekle", json={"tarih": dun.isoformat(), "maddeler": d["maddeler"]})
        assert r.status_code == 201 and r.json()["maddeler"][0]["tarih"] == dun.isoformat()
        assert "• Eski iş yapıldı." in c.get(f"/api/durum?tarih={dun.isoformat()}").json()["rapor_metni"]
        assert "Eski iş" not in c.get("/api/durum").json()["rapor_metni"]
        # kota gün seçiminden bağımsız olarak bugünün sayacından düşer
        with OturumYapici() as db:
            assert [(s.tarih, s.cagri) for s in db.scalars(select(ClaudeKullanim))] == [(GUN, 1)]
        # aralık dışı gün reddedilir
        eski = (GUN - timedelta(days=api.GECMIS_GUN + 1)).isoformat()
        assert c.post("/api/sesli-not", json={"metin": "x", "tarih": eski}).status_code == 400
        assert c.post("/api/sesli-not/ekle", json={"tarih": eski, "maddeler": [{"metin": "x"}]}).status_code == 400
    assert [m.tarih for m in satirlar(uid)] == [dun]


def test_kullanici_ayrimi(sahte_claude):
    a = kullanici_olustur()
    b = kullanici_olustur("b@ornek.com", "B")
    with istemci("b@ornek.com") as c:
        kat_b = kategoriler(c)["Genel İşler"]["id"]
        assert c.post("/api/sesli-not/ekle", json={"maddeler": [{"metin": "B'nin işi", "kategori_id": kat_b}]}).status_code == 201
    with istemci() as c:
        k = kategoriler(c)

        def yanit(istek):
            liste = json.loads(istek["messages"][0]["content"].split("Rapor kategorileri:\n", 1)[1].split("\n\n", 1)[0])
            assert kat_b not in {x["id"] for x in liste}  # yalnız kendi kategorileri
            return '[{"metin": "A\'nın işi yapıldı.", "tur": "bugun", "kategori_id": %d}]' % kat_b

        sahte_claude.yanitlar = [yanit]
        d = c.post("/api/sesli-not", json={"metin": "a'nın işi"}).json()
        assert d["maddeler"][0]["kategori_id"] is None
        assert c.post("/api/sesli-not/ekle", json={"maddeler": [{"metin": "A'nın işi", "kategori_id": k["Genel İşler"]["id"]}]}).status_code == 201
        metin = c.get("/api/durum").json()["rapor_metni"]
        assert "A'nın işi" in metin and "B'nin işi" not in metin
    assert cagri_sayisi(a) == 1 and cagri_sayisi(b) == 0
    assert [m.metin for m in satirlar(a)] == ["A'nın işi"] and [m.metin for m in satirlar(b)] == ["B'nin işi"]


def test_oturumsuz_401():
    with TestClient(uygulama.app, follow_redirects=False) as c:
        assert c.post("/api/sesli-not", json={"metin": "x"}).status_code == 401
        assert c.post("/api/sesli-not/ekle", json={"maddeler": [{"metin": "x"}]}).status_code == 401


# ---------------------------------------------------------------- arayüz

def test_sayfada_mikrofon_ve_panel():
    kullanici_olustur()
    with istemci() as c:
        html = c.get("/").text
    assert html.count('data-eylem="ses"') == 2  # giriş satırı + telefon alt eylem alanı
    assert 'data-ad="mikrofon"' in html and 'id="sesPanel"' in html
    assert "Klavyenin mikrofon simgesine basıp konuş" in html
    assert "webkitSpeechRecognition" in html and "'tr-TR'" in html
    assert "Maddelere böl" in html and "Vazgeç" in html and "Durdur" in html
