"""R2: maddeli bugün, Claude düzeltme, rapor geçmişi, haftalık özet. Claude çağrısı sahte; ağa çıkılmaz."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text

import api
import app as uygulama
import guvenlik
import servisler
import veritabani
from veritabani import GunlukIfade, Kullanici, KullaniciAyari, Madde, OturumYapici, Rapor, Temel, motor

SIFRE = "dogru-sifre-123"


@pytest.fixture(autouse=True)
def temiz_veritabani(monkeypatch):
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    api.onbellegi_temizle()
    monkeypatch.setattr(servisler, "gmail_tara", lambda *a, **k: [])
    monkeypatch.setattr(servisler, "github_tara", lambda *a, **k: [])
    yield


def kullanici_olustur(eposta="a@ornek.com", ad="A") -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad=ad, sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol="uye", aktif=True, sifre_degistirmeli=False)
        db.add(k)
        db.commit()
        db.add(KullaniciAyari(user_id=k.id, kurulum_tamam=True))
        db.commit()
        return k.id


def istemci(eposta="a@ornek.com") -> TestClient:
    c = TestClient(uygulama.app, follow_redirects=False)
    assert c.post("/giris", data={"eposta": eposta, "sifre": SIFRE}).status_code == 303
    return c


def ekle(**alanlar) -> int:
    with OturumYapici() as db:
        m = Madde(**alanlar)
        db.add(m)
        db.commit()
        return m.id


def madde(madde_id: int) -> Madde:
    with OturumYapici() as db:
        return db.get(Madde, madde_id)


def rapor_ekle(uid, tarih, metin, tur="gunluk"):
    with OturumYapici() as db:
        db.add(Rapor(user_id=uid, tarih=tarih, metin=metin, tur=tur, hafta_baslangic=tarih if tur == "haftalik" else None))
        db.commit()


def gonderilen_girdiler(istek: dict) -> list[dict]:
    icerik = istek["messages"][0]["content"]
    return json.loads(icerik.split("Düzeltilecek maddeler:\n", 1)[1])


def yankilayan(istek: dict) -> str:
    """Her maddeyi 'D:' önekiyle 'düzeltir'."""
    return json.dumps([{"id": g["id"], "metin": "D:" + g["metin"]} for g in gonderilen_girdiler(istek)], ensure_ascii=False)


def durum_maddeleri(c) -> dict[int, dict]:
    return {m["id"]: m for m in c.get("/api/durum").json()["maddeler"]}


BUGUN = servisler.istanbul_bugun()


# ---------------------------------------------------------------- 1) maddeli bugün

def test_yapilanlar_elle_maddelerine_bir_kez_bolunur():
    uid = kullanici_olustur()
    eski = ekle(user_id=uid, tur="bugun", tarih=BUGUN, kaynak="yapilanlar", kaynak_id="yapilanlar", metin="- MSG'ye itiraz\n\n• Coverz listesi \nIMRO yazışması")
    dunku = ekle(user_id=uid, tur="bugun", tarih=BUGUN - timedelta(days=1), kaynak="yapilanlar", kaynak_id="yapilanlar", metin="dün")
    with istemci() as c:
        ilk = [(m["kaynak"], m["metin"]) for m in c.get("/api/durum").json()["maddeler"] if m["tur"] == "bugun"]
        assert ilk == [("elle", "MSG'ye itiraz"), ("elle", "Coverz listesi"), ("elle", "IMRO yazışması")]
        ikinci = [(m["kaynak"], m["metin"]) for m in c.get("/api/durum").json()["maddeler"] if m["tur"] == "bugun"]
        assert ikinci == ilk
    assert madde(eski) is None
    assert madde(dunku) is not None  # yalnız bugüne ait satır çevrilir


def test_elle_madde_her_eklemede_yeni_satir():
    kullanici_olustur()
    with istemci() as c:
        a = c.post("/api/maddeler", json={"tur": "bugun", "kaynak": "elle", "metin": " bir "}).json()
        b = c.post("/api/maddeler", json={"tur": "bugun", "kaynak": "elle", "metin": "iki"}).json()
        assert a["id"] != b["id"] and a["metin"] == "bir" and a["rapor_metni"] == "bir"
        assert c.post("/api/maddeler", json={"tur": "bugun", "kaynak": "elle", "metin": "  "}).status_code == 422
        assert [m["metin"] for m in c.get("/api/durum").json()["maddeler"] if m["kaynak"] == "elle"] == ["bir", "iki"]


def test_ice_aktar_done_satir_satir_elle():
    kullanici_olustur()
    veri = {"recurring": [], "ongoing": [], "daily": {"date": BUGUN.isoformat(), "done": "a\n- b\n\nc", "plan": "yarın"}}
    with istemci() as c:
        assert c.post("/api/ice-aktar", json=veri).json()["bugun"] == 4
        bugun = [(m["kaynak"], m["metin"]) for m in c.get("/api/durum").json()["maddeler"] if m["tur"] == "bugun"]
    assert sorted(bugun) == [("elle", "a"), ("elle", "b"), ("elle", "c"), ("yarin", "yarın")]


# ---------------------------------------------------------------- 2) /api/duzelt

def test_duzelt_paket_secimi_yazim_ve_ikinci_cagri(sahte_claude):
    uid = kullanici_olustur()
    diger = kullanici_olustur("b@ornek.com", "B")
    ortak = {"user_id": uid, "tarih": BUGUN}
    gider = {
        "elle": ekle(**ortak, tur="bugun", kaynak="elle", metin="Köprü Film den talep geldi"),
        "bulunan": ekle(**ortak, tur="bulunan", kaynak="medusa", kaynak_id="k1", metin="Fix N+1"),
        "surekli": ekle(user_id=uid, tur="surekli", metin="Yazışmalar takip edildi | Birliklerle yazışıldı"),
        "devam": ekle(user_id=uid, tur="devam", metin="MSG itirazı", asama="yanıt bekleniyor"),
    }
    gitmez = [
        ekle(**ortak, tur="bugun", kaynak="elle", metin="tiksiz", tikli=False),
        ekle(**ortak, tur="bugun", kaynak="elle", metin="elle düzenlenmiş", kullanici_duzenledi=True),
        ekle(**ortak, tur="bugun", kaynak="elle", metin="zaten düzeltilmiş", metin_ai="Zaten düzeltilmiş."),
        ekle(**ortak, tur="bulunan", kaynak="eposta", kaynak_id="k2", metin="gizli", gizli=True),
        ekle(**ortak, tur="bulunan", kaynak="eposta", kaynak_id="k3", metin="çevrilmiş", metin_ai="Çevrilmiş."),
        ekle(user_id=uid, tur="surekli", metin="tiksiz sürekli", tikli=False),
        ekle(**ortak, tur="bugun", kaynak="yarin", kaynak_id="yarin", metin="yarın planı"),
        ekle(user_id=uid, tur="bugun", tarih=BUGUN - timedelta(days=1), kaynak="elle", metin="dünkü madde"),
        ekle(user_id=diger, tur="bugun", tarih=BUGUN, kaynak="elle", metin="B'nin maddesi"),
    ]
    for gun in range(1, 5):
        rapor_ekle(uid, BUGUN - timedelta(days=gun), f"RAPOR-{gun}")
    rapor_ekle(uid, BUGUN, "BUGUNKU-RAPOR")
    rapor_ekle(diger, BUGUN - timedelta(days=1), "B-RAPORU")

    sahte_claude.yanitlar = [yankilayan, yankilayan]
    with istemci() as c:
        c.put("/api/ayarlar", json={"proje_adi": "MEDUSA", "kaynaklar": {"gmail": True, "github": True}})
        r = c.post("/api/duzelt").json()
        assert (r["duzeltilen"], r["gonderilen"], r["hatalar"]) == (4, 4, [])

        istek = sahte_claude.istekler[0]
        assert istek["model"] == "claude-sonnet-5" and istek["max_tokens"] == 2000
        assert istek["thinking"] == {"type": "disabled"}
        assert "MEDUSA" in istek["system"] and "Köprü Film'den" in istek["system"]
        girdiler = gonderilen_girdiler(istek)
        assert sorted(g["id"] for g in girdiler) == sorted(gider.values())
        assert next(g for g in girdiler if g["id"] == gider["devam"])["asama"] == "yanıt bekleniyor"
        icerik = istek["messages"][0]["content"]
        assert all(f"RAPOR-{g}" in icerik for g in (1, 2, 3))
        assert "RAPOR-4" not in icerik and "BUGUNKU-RAPOR" not in icerik and "B-RAPORU" not in icerik

        maddeler = durum_maddeleri(c)
        assert maddeler[gider["elle"]]["metin_ai"] == "D:Köprü Film den talep geldi"
        assert maddeler[gider["elle"]]["metin"] == "Köprü Film den talep geldi"
        assert maddeler[gider["elle"]]["rapor_metni"] == "D:Köprü Film den talep geldi"
        assert maddeler[gider["bulunan"]]["rapor_metni"] == "D:Fix N+1"
        assert maddeler[gider["devam"]]["rapor_metni"] == "D:MSG itirazı"
        assert maddeler[gider["surekli"]]["gunun_ifadesi"] == "D:Yazışmalar takip edildi | Birliklerle yazışıldı"
        assert maddeler[gider["surekli"]]["metin_ai"] is None
        with OturumYapici() as db:
            ifade = db.scalar(select(GunlukIfade).where(GunlukIfade.item_id == gider["surekli"]))
            assert (ifade.tarih, ifade.user_id) == (BUGUN, uid)
        assert madde(gider["elle"]).ai_tarih == BUGUN

        yeni = c.post("/api/maddeler", json={"tur": "bugun", "kaynak": "elle", "metin": "Ezgi hanım dan test istendi"}).json()
        r = c.post("/api/duzelt").json()
        assert [g["id"] for g in gonderilen_girdiler(sahte_claude.istekler[1])] == [yeni["id"]]
        assert r["duzeltilen"] == 1

        r = c.post("/api/duzelt").json()
        assert len(sahte_claude.istekler) == 2 and r["gonderilen"] == 0  # boş paket: çağrı yok

    for m in gitmez:
        m = madde(m)
        assert m.metin_ai in (None, "Zaten düzeltilmiş.", "Çevrilmiş.")


def test_duzelt_anahtar_yoksa_atlanir(monkeypatch):
    uid = kullanici_olustur()
    ekle(user_id=uid, tur="bugun", tarih=BUGUN, kaynak="elle", metin="a")
    cagrildi = []
    monkeypatch.setattr(servisler, "_claude_cagir", lambda *a, **k: cagrildi.append(1))
    with istemci() as c:
        r = c.post("/api/duzelt")
        assert r.status_code == 200 and r.json()["atlandi"] == "anahtar yok"
        assert c.get("/api/durum").json()["ai_anahtari"] is False
    assert cagrildi == []


# ---------------------------------------------------------------- 3) hata

@pytest.mark.parametrize("yanit", [
    "Üzgünüm, bu isteğe yardımcı olamam.",
    servisler.ClaudeHatasi("API hatası (529: Overloaded)"),
    "[{\"id\": \"bozuk\"",
])
def test_duzelt_hatasinda_maddeler_degismez(sahte_claude, yanit):
    uid = kullanici_olustur()
    elle = ekle(user_id=uid, tur="bugun", tarih=BUGUN, kaynak="elle", metin="ham metin")
    surekli = ekle(user_id=uid, tur="surekli", metin="sürekli")
    sahte_claude.yanitlar = [yanit]
    with istemci() as c:
        r = c.post("/api/duzelt")
        assert r.status_code == 200
        govde = r.json()
        assert govde["duzeltilen"] == 0 and govde["hatalar"] and govde["hatalar"][0].startswith("Claude:")
        maddeler = durum_maddeleri(c)
    assert (maddeler[elle]["metin"], maddeler[elle]["metin_ai"], maddeler[elle]["rapor_metni"]) == ("ham metin", None, "ham metin")
    assert maddeler[surekli]["gunun_ifadesi"] is None and maddeler[surekli]["rapor_metni"] == "sürekli"
    with OturumYapici() as db:
        assert db.scalar(select(func.count()).select_from(GunlukIfade)) == 0


def test_duzelt_eksik_yanit_digerlerini_bozmaz(sahte_claude):
    uid = kullanici_olustur()
    a = ekle(user_id=uid, tur="bugun", tarih=BUGUN, kaynak="elle", metin="a")
    b = ekle(user_id=uid, tur="bugun", tarih=BUGUN, kaynak="elle", metin="b")
    sahte_claude.yanitlar = [json.dumps([{"id": a, "metin": "A."}, {"id": 999999, "metin": "başkasının"}])]
    with istemci() as c:
        r = c.post("/api/duzelt").json()
    assert r["duzeltilen"] == 1 and r["hatalar"]
    assert madde(a).metin_ai == "A." and madde(b).metin_ai is None


# ---------------------------------------------------------------- 4) kullanıcı düzenlemesi ve AI seçimi

def test_patch_metin_ve_ai_secimi(sahte_claude):
    uid = kullanici_olustur()
    kullanici_olustur("b@ornek.com", "B")
    elle = ekle(user_id=uid, tur="bugun", tarih=BUGUN, kaynak="elle", metin="ham")
    surekli = ekle(user_id=uid, tur="surekli", metin="sürekli iş")
    sahte_claude.yanitlar = [yankilayan, yankilayan, yankilayan]
    with istemci() as c:
        c.post("/api/duzelt")
        assert durum_maddeleri(c)[elle]["rapor_metni"] == "D:ham"

        r = c.patch(f"/api/maddeler/{elle}/ai", json={"kullan": False}).json()
        assert (r["ai_kullan"], r["metin_ai"], r["rapor_metni"]) == (False, "D:ham", "ham")
        assert c.patch(f"/api/maddeler/{elle}/ai", json={"kullan": True}).json()["rapor_metni"] == "D:ham"

        with istemci("b@ornek.com") as b:
            assert b.patch(f"/api/maddeler/{elle}/ai", json={"kullan": False}).status_code == 404

        r = c.patch(f"/api/maddeler/{elle}", json={"metin": "benim yazdığım"}).json()
        assert (r["kullanici_duzenledi"], r["metin_ai"], r["rapor_metni"]) == (True, None, "benim yazdığım")
        # aynı metni yeniden kaydetmek bayrağı değiştirmez; düzenlenen madde düzeltmeye gitmez
        assert c.post("/api/duzelt").json()["gonderilen"] == 0

        r = c.patch(f"/api/maddeler/{elle}/ai", json={"yenile": True}).json()
        assert [g["id"] for g in gonderilen_girdiler(sahte_claude.istekler[-1])] == [elle]
        assert (r["kullanici_duzenledi"], r["metin_ai"], r["rapor_metni"], r["hatalar"]) == (False, "D:benim yazdığım", "D:benim yazdığım", [])

        # sürekli işin metni değişirse günün ifadesi silinir, sonraki düzeltmede yeniden üretilir
        assert durum_maddeleri(c)[surekli]["gunun_ifadesi"] == "D:sürekli iş"
        r = c.patch(f"/api/maddeler/{surekli}", json={"metin": "yeni sürekli"}).json()
        assert r["gunun_ifadesi"] is None and r["rapor_metni"] == "yeni sürekli"
        c.post("/api/duzelt")
        assert [g["id"] for g in gonderilen_girdiler(sahte_claude.istekler[-1])] == [surekli]


def test_yenile_anahtar_yoksa_400():
    uid = kullanici_olustur()
    elle = ekle(user_id=uid, tur="bugun", tarih=BUGUN, kaynak="elle", metin="ham")
    with istemci() as c:
        assert c.patch(f"/api/maddeler/{elle}/ai", json={"yenile": True}).status_code == 400


# ---------------------------------------------------------------- 5) rapor metni önceliği

def test_rapor_metni_oncelik_sirasi():
    tarih = date(2026, 9, 16)  # yılın 259. günü
    m = Madde(tur="bugun", kaynak="elle", metin="ham", metin_ai="ai", kullanici_duzenledi=False, ai_kullan=True)
    assert api.madde_rapor_metni(m, None, tarih) == "ai"
    m.ai_kullan = False
    assert api.madde_rapor_metni(m, None, tarih) == "ham"
    m.ai_kullan, m.kullanici_duzenledi = True, True
    assert api.madde_rapor_metni(m, None, tarih) == "ham"
    m.kullanici_duzenledi, m.metin_ai = False, None
    assert api.madde_rapor_metni(m, None, tarih) == "ham"

    d = Madde(tur="devam", metin="MSG itirazı", asama="yanıt bekleniyor", kullanici_duzenledi=False, ai_kullan=True)
    assert api.madde_rapor_metni(d, None, tarih) == "MSG itirazı — yanıt bekleniyor"
    d.metin_ai = "MSG itirazı için yanıt bekleniyor."
    assert api.madde_rapor_metni(d, None, tarih) == "MSG itirazı için yanıt bekleniyor."

    s = Madde(tur="surekli", metin="a | b", kullanici_duzenledi=False, ai_kullan=True)
    assert api.madde_rapor_metni(s, "günün ifadesi", tarih) == "günün ifadesi"
    assert api.madde_rapor_metni(s, None, tarih) == "b"
    s.ai_kullan = False
    assert api.madde_rapor_metni(s, "günün ifadesi", tarih) == "b"


# ---------------------------------------------------------------- 6) rapor geçmişi

def test_raporlar_upsert_kullanici_ayrimi_ve_arama():
    kullanici_olustur()
    kullanici_olustur("b@ornek.com", "B")
    with istemci() as a, istemci("b@ornek.com") as b:
        assert a.get("/api/durum").json()["son_kopya"] is None
        ilk = a.post("/api/raporlar", json={"metin": "*Rapor*\n\n*Yapılanlar*\n• ilk"}).json()
        ikinci = a.post("/api/raporlar", json={"metin": "*Rapor – 16.09*\n\n*Yapılanlar*\n• MSG itirazı\n• Coverz 50% tamam"}).json()
        assert ilk["id"] == ikinci["id"] and ikinci["tur"] == "gunluk" and ikinci["tarih"] == BUGUN.isoformat()
        assert a.get("/api/durum").json()["son_kopya"] == ikinci["olusturma"]

        liste = a.get("/api/raporlar").json()
        assert liste["toplam"] == 1
        r = liste["raporlar"][0]
        assert (r["ilk_satir"], r["madde_sayisi"]) == ("*Rapor – 16.09*", 2) and "MSG itirazı" in r["metin"]

        assert b.get("/api/raporlar").json() == {"toplam": 0, "raporlar": []}
        b.post("/api/raporlar", json={"metin": "B'nin raporu MSG"})
        assert b.get("/api/raporlar?q=msg").json()["toplam"] == 1
        assert a.get("/api/raporlar?q=msg").json()["raporlar"][0]["id"] == ikinci["id"]
        assert a.get("/api/raporlar?q=B'nin").json()["toplam"] == 0
        assert a.get("/api/raporlar?q=50%25").json()["toplam"] == 1
        assert a.get("/api/raporlar?q=5%25%25").json()["toplam"] == 0  # % joker değil
        assert a.get("/api/raporlar?tur=haftalik").json()["toplam"] == 0
        assert a.get("/api/raporlar?tur=gunluk").json()["toplam"] == 1
        assert a.get("/api/raporlar?tur=yanlis").status_code == 422
        assert a.post("/api/raporlar", json={"metin": "  "}).status_code == 422
        assert a.post("/api/raporlar", json={"metin": "x", "tur": "haftalik", "hafta_baslangic": "2026-09-16"}).status_code == 422

    with OturumYapici() as db:
        assert db.scalar(select(func.count()).select_from(Rapor)) == 2


def test_gecmis_sayfasi_ve_ust_cubuk():
    kullanici_olustur()
    with istemci() as c:
        r = c.get("/gecmis")
        assert r.status_code == 200 and "Haftalık özet üret" in r.text
        assert 'href="/gecmis"' in c.get("/").text
    with TestClient(uygulama.app, follow_redirects=False) as c:
        assert c.get("/gecmis").headers["location"] == "/giris"


# ---------------------------------------------------------------- 7) haftalık özet

PAZARTESI = date(2025, 9, 1)


def test_haftalik_yalniz_o_haftanin_gunluk_raporlari(sahte_claude):
    uid = kullanici_olustur()
    diger = kullanici_olustur("b@ornek.com", "B")
    rapor_ekle(uid, PAZARTESI, "PAZARTESI-RAPORU")
    rapor_ekle(uid, PAZARTESI + timedelta(days=2), "CARSAMBA-RAPORU")
    rapor_ekle(uid, PAZARTESI + timedelta(days=6), "PAZAR-RAPORU")
    rapor_ekle(uid, PAZARTESI - timedelta(days=1), "ONCEKI-HAFTA")
    rapor_ekle(uid, PAZARTESI + timedelta(days=7), "SONRAKI-HAFTA")
    rapor_ekle(uid, PAZARTESI, "ESKI-HAFTALIK", tur="haftalik")
    rapor_ekle(diger, PAZARTESI + timedelta(days=1), "B-RAPORU")
    ozet = "*Haftalık Özet – 1–7 Eylül 2025*\n\n• MSG itirazları takip edildi"
    sahte_claude.yanitlar = [ozet]
    with istemci() as c:
        r = c.post("/api/haftalik", json={"hafta_baslangic": PAZARTESI.isoformat()})
        assert r.status_code == 200
        assert r.json() == {"metin": ozet, "hafta_baslangic": "2025-09-01", "rapor_sayisi": 3}
        istek = sahte_claude.istekler[0]
        icerik = istek["messages"][0]["content"]
        assert icerik.index("PAZARTESI-RAPORU") < icerik.index("CARSAMBA-RAPORU") < icerik.index("PAZAR-RAPORU")
        for yok in ("ONCEKI-HAFTA", "SONRAKI-HAFTA", "ESKI-HAFTALIK", "B-RAPORU"):
            assert yok not in icerik
        assert "*Haftalık Özet – 1–7 Eylül 2025*" in icerik  # pazar raporu olduğu için bitiş pazar
        assert istek["max_tokens"] == 2000 and "konuya göre" in istek["system"] and "*Devam eden*" in istek["system"]

        for _ in range(2):
            kayit = c.post("/api/raporlar", json={"metin": ozet, "tur": "haftalik", "hafta_baslangic": "2025-09-01"})
            assert kayit.status_code == 200
        haftaliklar = c.get("/api/raporlar?tur=haftalik").json()
        assert haftaliklar["toplam"] == 1
        assert (haftaliklar["raporlar"][0]["metin"], haftaliklar["raporlar"][0]["hafta_baslangic"]) == (ozet, "2025-09-01")
        assert c.get("/api/raporlar?tur=gunluk").json()["toplam"] == 5


def test_haftalik_hata_durumlari(sahte_claude, monkeypatch):
    uid = kullanici_olustur()
    with istemci() as c:
        r = c.post("/api/haftalik", json={"hafta_baslangic": PAZARTESI.isoformat()})
        assert r.status_code == 400 and r.json()["detail"] == "Bu hafta için kayıtlı günlük rapor yok"
        assert c.post("/api/haftalik", json={"hafta_baslangic": "2025-09-02"}).status_code == 422

        rapor_ekle(uid, PAZARTESI + timedelta(days=1), "rapor")
        sahte_claude.yanitlar = [servisler.ClaudeHatasi("API hatası (500: x)")]
        r = c.post("/api/haftalik", json={"hafta_baslangic": PAZARTESI.isoformat()})
        assert r.status_code == 502 and r.json()["detail"].startswith("Claude:")
        # 1–5 Eylül: son rapor salı, geçmiş hafta olduğu için bitiş cuma
        assert "*Haftalık Özet – 1–5 Eylül 2025*" in sahte_claude.istekler[0]["messages"][0]["content"]

        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        r = c.post("/api/haftalik", json={"hafta_baslangic": PAZARTESI.isoformat()})
        assert r.status_code == 400 and "ANTHROPIC_API_KEY" in r.json()["detail"]
    assert len(sahte_claude.istekler) == 1


@pytest.mark.parametrize("bas, bit, beklenen", [
    (date(2026, 9, 14), date(2026, 9, 18), "14–18 Eylül 2026"),
    (date(2026, 9, 28), date(2026, 10, 2), "28 Eylül – 2 Ekim 2026"),
    (date(2025, 12, 29), date(2026, 1, 2), "29 Aralık 2025 – 2 Ocak 2026"),
    (date(2026, 9, 14), date(2026, 9, 14), "14 Eylül 2026"),
])
def test_hafta_basligi(bas, bit, beklenen):
    assert servisler.hafta_basligi(bas, bit) == beklenen


# ---------------------------------------------------------------- 8) migration

def test_sema_guncelle_eski_semada_iki_kez(tmp_path):
    eski = create_engine(f"sqlite:///{tmp_path}/eski.db")
    with eski.begin() as b:
        b.execute(text("CREATE TABLE items (id INTEGER PRIMARY KEY, user_id INTEGER, tur VARCHAR(10), metin TEXT, tikli BOOLEAN)"))
        b.execute(text("CREATE TABLE reports (id INTEGER PRIMARY KEY, user_id INTEGER, tarih DATE, metin TEXT, olusturma DATETIME)"))
        b.execute(text("INSERT INTO items (user_id, tur, metin, tikli) VALUES (1, 'surekli', 'canlı satır', 1)"))
        b.execute(text("INSERT INTO reports (user_id, tarih, metin) VALUES (1, '2026-09-15', 'eski rapor')"))

    eklenen = veritabani.sema_guncelle(eski)
    assert eklenen == [
        "items.metin_ai", "items.ai_tarih", "items.kullanici_duzenledi", "items.ai_kullan", "items.kaynak_zaman",
        "reports.tur", "reports.hafta_baslangic", "items.kategori_id", "items.onemli", "reports.gonderim",
        "uq_reports_user_tarih_tur",
    ]
    assert veritabani.sema_guncelle(eski) == []
    with eski.connect() as b:
        assert b.execute(text("SELECT metin, metin_ai, kullanici_duzenledi, ai_kullan FROM items")).one() == ("canlı satır", None, 0, 1)
        assert b.execute(text("SELECT metin, tur, hafta_baslangic, gonderim FROM reports")).one() == ("eski rapor", "gunluk", None, "elle")
    eski.dispose()

    # güncel şemada hiçbir şey eklenmez
    assert veritabani.sema_guncelle() == []
    assert veritabani.sema_guncelle() == []


# ---------------------------------------------------------------- 9) R5: bekleme süresi, tarama zamanı

@pytest.mark.parametrize("olusturma, beklenen", [
    (datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc), 0),
    (datetime(2026, 9, 15, 21, 30, tzinfo=timezone.utc), 0),  # Istanbul'da 16 Eylül 00:30
    (datetime(2026, 9, 15, 20, 59, tzinfo=timezone.utc), 1),  # Istanbul'da 15 Eylül 23:59
    (datetime(2026, 9, 15, 9, 0), 1),  # sqlite'tan gelen naive değer UTC sayılır
    (datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc), 12),
    (None, 0),
])
def test_bekleme_gunu(olusturma, beklenen):
    assert api.bekleme_gunu(olusturma, date(2026, 9, 16)) == beklenen


def test_devam_olusturma_bekleme_ve_asama_degisince_sifirlanmaz():
    uid = kullanici_olustur()
    eski = ekle(user_id=uid, tur="devam", metin="MSG itirazı", asama="yanıt bekleniyor",
                olusturma=datetime.now(timezone.utc) - timedelta(days=12))
    with istemci() as c:
        yeni = c.post("/api/maddeler", json={"tur": "devam", "metin": "IMRO sözleşmesi"}).json()
        assert yeni["bekleme_gun"] == 0
        assert datetime.fromisoformat(yeni["olusturma"]).tzinfo is not None

        once = durum_maddeleri(c)[eski]
        assert once["bekleme_gun"] == 12 and once["olusturma"]
        sonra = c.patch(f"/api/maddeler/{eski}", json={"asama": "imza aşamasında"}).json()
        assert (sonra["bekleme_gun"], sonra["olusturma"]) == (12, once["olusturma"])
        # süre yalnız ekranda; rapor metnine girmez
        assert sonra["rapor_metni"] == "MSG itirazı — imza aşamasında"
        # diğer türlerde bekleme alanı yok, olusturma var
        elle = c.post("/api/maddeler", json={"tur": "bugun", "kaynak": "elle", "metin": "x"}).json()
        assert "bekleme_gun" not in elle and elle["olusturma"] and elle["kaynak_zaman"] is None


def test_tarama_zamani_bugun_ve_durumda():
    kullanici_olustur()
    with istemci() as c:
        assert c.get("/api/durum").json()["tarama_zamani"] is None
        ilk = c.get("/api/bugun").json()["tarama_zamani"]
        assert datetime.fromisoformat(ilk) <= datetime.now(timezone.utc)
        assert c.get("/api/durum").json()["tarama_zamani"] == ilk
        assert c.get("/api/bugun").json()["tarama_zamani"] == ilk  # önbellekten, yeniden taranmadı
        yenilenen = c.get("/api/bugun?yenile=1").json()["tarama_zamani"]
        assert datetime.fromisoformat(yenilenen) >= datetime.fromisoformat(ilk)
        assert c.get("/api/durum").json()["tarama_zamani"] == yenilenen


def test_sema_guncelle_kaynak_zaman_kolonu(tmp_path):
    eski = create_engine(f"sqlite:///{tmp_path}/eski.db")
    Temel.metadata.create_all(eski)
    with eski.begin() as b:
        b.execute(text("ALTER TABLE items DROP COLUMN kaynak_zaman"))
        b.execute(text("INSERT INTO items (user_id, tur, metin, tikli, gizli, sira, olusturma, kullanici_duzenledi, ai_kullan) "
                       "VALUES (1, 'bulunan', 'eski satır', 1, 0, 1, '2026-09-15 09:00:00', 0, 1)"))

    assert veritabani.sema_guncelle(eski) == ["items.kaynak_zaman"]
    assert veritabani.sema_guncelle(eski) == []
    with eski.connect() as b:
        assert b.execute(text("SELECT metin, kaynak_zaman FROM items")).one() == ("eski satır", None)
    eski.dispose()
