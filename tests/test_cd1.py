"""CD1: rapor kategorileri, önemli işareti, günün düzeni (sıra/karıştırma), kategorili rapor metni, geçmiş gün.
sqlite; Claude, Gmail ve GitHub sahte."""
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import api
import app as uygulama
import guvenlik
import servisler
from veritabani import (
    ClaudeKullanim, GunlukIfade, Kategori, Kullanici, KullaniciAyari, Madde, OturumYapici, Rapor, RaporDuzeni, Temel, motor,
)

SIFRE = "dogru-sifre-123"
GUN = date(2026, 9, 23)  # Çarşamba
TARAMALAR: list[tuple[str, date]] = []
GERCEK_GMAIL_TARA, GERCEK_GITHUB_TARA = servisler.gmail_tara, servisler.github_tara  # fixture sahtelemeden önce


@pytest.fixture(autouse=True)
def ortam(monkeypatch):
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    api.onbellegi_temizle()
    TARAMALAR.clear()
    monkeypatch.setattr(api, "bugun", lambda: GUN)
    monkeypatch.setattr(servisler, "gmail_tara", lambda k, s, gun, *a, **kw: TARAMALAR.append(("gmail", gun)) or [])
    monkeypatch.setattr(servisler, "github_tara", lambda t, r, gun, *a, **kw: TARAMALAR.append(("github", gun)) or [])
    yield


def kullanici_olustur(eposta="a@ornek.com", ad="A", proje_adi="MEDUSA", olusturma=None) -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad=ad, sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol="uye", aktif=True,
                      sifre_degistirmeli=False, olusturma=olusturma or datetime(2026, 9, 1, tzinfo=timezone.utc))
        db.add(k)
        db.commit()
        db.add(KullaniciAyari(user_id=k.id, kurulum_tamam=True, proje_adi=proje_adi, rapor_basligi="Günlük Rapor",
                              kaynaklar={"gmail": True, "github": True, "medusa": False},
                              gmail_kullanici="a@gmail.com", gmail_sifre_enc=guvenlik.sifrele("s"),
                              github_token_enc=guvenlik.sifrele("t"), github_repo="a/medusa"))
        db.commit()
        return k.id


def istemci(eposta="a@ornek.com") -> TestClient:
    c = TestClient(uygulama.app, follow_redirects=False)
    assert c.post("/giris", data={"eposta": eposta, "sifre": SIFRE}).status_code == 303
    return c


def ekle(**alanlar) -> int:
    with OturumYapici() as db:
        m = Madde(**{"tikli": True, **alanlar})
        db.add(m)
        db.commit()
        return m.id


def madde(madde_id: int) -> Madde:
    with OturumYapici() as db:
        return db.get(Madde, madde_id)


def kategoriler(c) -> dict[str, dict]:
    return {k["ad"]: k for k in c.get("/api/kategoriler").json()["kategoriler"]}


def etkin(c, tarih: date | None = None) -> dict[int, int]:
    yol = "/api/durum" + (f"?tarih={tarih.isoformat()}" if tarih else "")
    return {m["id"]: m["etkin_kategori_id"] for m in c.get(yol).json()["maddeler"]}


def bolumler(c, tarih: date = GUN) -> dict[int, list[int]]:
    d = c.get(f"/api/rapor-duzeni?tarih={tarih.isoformat()}").json()
    return {b["kategori_id"]: b["maddeler"] for b in d["bolumler"]}


# ---------------------------------------------------------------- A) başlangıç kategorileri

def test_baslangic_kategorileri_ilk_durumda_bir_kez_acilir():
    uid = kullanici_olustur()
    kullanici_olustur("b@ornek.com", "B", proje_adi=None)
    with istemci() as c:
        with OturumYapici() as db:
            assert db.scalars(select(Kategori).where(Kategori.user_id == uid)).all() == []
        c.get("/api/durum")
        c.get("/api/durum")
        liste = c.get("/api/kategoriler").json()["kategoriler"]
    assert [(k["ad"], k["kaynaklar"], k["sistem"], k["sira"]) for k in liste] == [
        ("Yazışmalar", ["gmail"], None, 1),
        ("MEDUSA Çalışmaları", ["github", "medusa"], None, 2),
        ("Genel İşler", [], None, 3),
        ("Devam Eden İşler", [], "devam", 4),
        ("Önemli Konular", [], "onemli", 5),
    ]
    with istemci("b@ornek.com") as c:
        assert [k["ad"] for k in c.get("/api/kategoriler").json()["kategoriler"]][1] == "Uygulama Çalışmaları"
    with OturumYapici() as db:
        assert len(db.scalars(select(Kategori).where(Kategori.user_id == uid)).all()) == 5


def test_durum_kategoriler_duzen_ve_ayarlar_doner():
    kullanici_olustur()
    with istemci() as c:
        d = c.get("/api/durum").json()
        assert d["tarih"] == d["bugun"] == GUN.isoformat()
        assert [k["ad"] for k in d["duzen"]["kategoriler"]][0] == "Yazışmalar"
        assert d["ayarlar"]["rapor_bicimi"] == "kategorili" and d["ayarlar"]["karistir"] is True
        a = c.put("/api/ayarlar", json={"rapor_bicimi": "duz", "karistir": False}).json()
        assert (a["rapor_bicimi"], a["karistir"]) == ("duz", False)
        assert c.put("/api/ayarlar", json={"rapor_bicimi": "baska"}).status_code == 422


# ---------------------------------------------------------------- B) etkin kategori: öncelikler a–g

def test_oncelik_g_genel_isler_ve_yedegi():
    uid = kullanici_olustur()
    elle = ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="elle", metin="Köprü Film'e teklif")
    surekli = ekle(user_id=uid, tur="surekli", metin="CRD kontrol")
    with istemci() as c:
        k = kategoriler(c)
        assert etkin(c)[elle] == etkin(c)[surekli] == k["Genel İşler"]["id"]
        # "Genel İşler" yoksa ilk kaynaksız sistem-dışı kategori
        diger = c.post("/api/kategoriler", json={"ad": "Diğer"}).json()
        c.delete(f"/api/kategoriler/{k['Genel İşler']['id']}")
        assert etkin(c)[elle] == diger["id"]


def test_oncelik_e_bulunan_kaynaginin_kategorisi():
    uid = kullanici_olustur()
    eposta = ekle(user_id=uid, tur="bulunan", tarih=GUN, kaynak="eposta", kaynak_id="e1", metin="MESAM'a e-posta")
    commit = ekle(user_id=uid, tur="bulunan", tarih=GUN, kaynak="medusa", kaynak_id="c1", metin="Fix index")
    with istemci() as c:
        k = kategoriler(c)
        e = etkin(c)
        assert (e[eposta], e[commit]) == (k["Yazışmalar"]["id"], k["MEDUSA Çalışmaları"]["id"])


def test_oncelik_d_maddenin_kategorisi_bulunandan_once():
    uid = kullanici_olustur()
    eposta = ekle(user_id=uid, tur="bulunan", tarih=GUN, kaynak="eposta", kaynak_id="e1", metin="MESAM'a e-posta")
    elle = ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="elle", metin="Teklif")
    with istemci() as c:
        k = kategoriler(c)
        assert c.patch(f"/api/maddeler/{eposta}", json={"kategori_id": k["Genel İşler"]["id"]}).status_code == 200
        assert c.patch(f"/api/maddeler/{elle}", json={"kategori_id": k["Yazışmalar"]["id"]}).json()["kategori_id"] == k["Yazışmalar"]["id"]
        e = etkin(c)
        assert (e[eposta], e[elle]) == (k["Genel İşler"]["id"], k["Yazışmalar"]["id"])
        # null: otomatik kurala dönüş; sistem kategorisi elle atanamaz
        c.patch(f"/api/maddeler/{eposta}", json={"kategori_id": None})
        assert etkin(c)[eposta] == k["Yazışmalar"]["id"]
        assert c.patch(f"/api/maddeler/{elle}", json={"kategori_id": k["Önemli Konular"]["id"]}).status_code == 422


def test_oncelik_c_devam_maddenin_kategorisinden_once():
    uid = kullanici_olustur()
    devam = ekle(user_id=uid, tur="devam", metin="MSG itirazı", asama="yanıt bekleniyor")
    with istemci() as c:
        k = kategoriler(c)
        c.patch(f"/api/maddeler/{devam}", json={"kategori_id": k["Genel İşler"]["id"]})
        assert etkin(c)[devam] == k["Devam Eden İşler"]["id"]


def test_oncelik_b_onemli_devamdan_once():
    uid = kullanici_olustur()
    devam = ekle(user_id=uid, tur="devam", metin="MSG itirazı")
    elle = ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="elle", metin="Sözleşme")
    with istemci() as c:
        k = kategoriler(c)
        for mid in (devam, elle):
            assert c.patch(f"/api/maddeler/{mid}", json={"onemli": True}).json()["onemli"] is True
        e = etkin(c)
        assert e[devam] == e[elle] == k["Önemli Konular"]["id"]
        c.patch(f"/api/maddeler/{elle}", json={"onemli": False})
        assert etkin(c)[elle] == k["Genel İşler"]["id"]


def test_oncelik_a_gunun_duzeni_hepsinden_once_ve_yalniz_o_gun():
    uid = kullanici_olustur()
    devam = ekle(user_id=uid, tur="devam", metin="MSG itirazı", onemli=True)
    with istemci() as c:
        k = kategoriler(c)
        c.post("/api/rapor-duzeni", json={"tarih": GUN.isoformat(), "sirali": [{"item_id": devam, "kategori_id": k["Yazışmalar"]["id"]}]})
        assert etkin(c)[devam] == k["Yazışmalar"]["id"]
        assert etkin(c, GUN - timedelta(days=1))[devam] == k["Önemli Konular"]["id"]
        # seçiciyle kategori değişince o günün düzenindeki kategori de izler
        c.patch(f"/api/maddeler/{devam}?tarih={GUN}", json={"kategori_id": k["Genel İşler"]["id"]})
        assert etkin(c)[devam] == k["Genel İşler"]["id"]
        # yıldız değişince günün elle kategorisi bırakılır, kural işler
        c.patch(f"/api/maddeler/{devam}?tarih={GUN}", json={"onemli": False})
        assert etkin(c)[devam] == k["Devam Eden İşler"]["id"]


def test_oncelik_f_claude_onerisi_yazilir_gecersizler_yok_sayilir(sahte_claude):
    uid = kullanici_olustur()
    kullanici_olustur("b@ornek.com", "B")
    ids = {ad: ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="elle", metin=ad) for ad in ("yazisma", "sistem", "yabanci", "bos")}
    not_ = ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="not", kaynak_id="not-1", metin="not maddesi")
    secili = ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="elle", metin="secili")
    bulunan = ekle(user_id=uid, tur="bulunan", tarih=GUN, kaynak="eposta", kaynak_id="e1", metin="e-posta")
    with istemci("b@ornek.com") as c:
        yabanci_kat = kategoriler(c)["Genel İşler"]["id"]
    with istemci() as c:
        k = kategoriler(c)
        c.patch(f"/api/maddeler/{secili}", json={"kategori_id": k["Genel İşler"]["id"]})
        oneriler = {ids["yazisma"]: k["Yazışmalar"]["id"], ids["sistem"]: k["Önemli Konular"]["id"],
                    ids["yabanci"]: yabanci_kat, not_: k["MEDUSA Çalışmaları"]["id"],
                    secili: k["Yazışmalar"]["id"], bulunan: k["Genel İşler"]["id"]}

        def yanit(istek):
            icerik = istek["messages"][0]["content"]
            liste = json.loads(icerik.split("Rapor kategorileri:\n", 1)[1].split("\n\n", 1)[0])
            assert [x["ad"] for x in liste] == ["Yazışmalar", "MEDUSA Çalışmaları", "Genel İşler"]  # sistem yok
            girdiler = json.loads(icerik.split("Düzeltilecek maddeler:\n", 1)[1])
            isteyen = {g["id"] for g in girdiler if g.get("kategori_sec")}
            assert isteyen == {*ids.values(), not_}  # kategorisi olan ve bulunan istemez
            assert "kategori_id" in istek["system"]
            return json.dumps([{"id": g["id"], "metin": g["metin"] + ".", "kategori_id": oneriler.get(g["id"])} for g in girdiler])

        sahte_claude.yanitlar = [yanit]
        r = c.post("/api/duzelt").json()
        assert r["hatalar"] == [] and len(sahte_claude.istekler) == 1
        assert madde(ids["yazisma"]).kategori_id == k["Yazışmalar"]["id"]
        assert madde(not_).kategori_id == k["MEDUSA Çalışmaları"]["id"]
        assert madde(ids["sistem"]).kategori_id is None and madde(ids["yabanci"]).kategori_id is None
        assert madde(ids["bos"]).kategori_id is None and madde(secili).kategori_id == k["Genel İşler"]["id"]
        assert madde(bulunan).kategori_id is None
        e = etkin(c)
        assert e[ids["yazisma"]] == k["Yazışmalar"]["id"] and e[ids["sistem"]] == k["Genel İşler"]["id"]


def test_claude_hatasinda_kategori_yazilmaz_ham_metin(sahte_claude):
    uid = kullanici_olustur()
    elle = ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="elle", metin="teklif")
    sahte_claude.yanitlar = [servisler.ClaudeHatasi("API hatası (500)")]
    with istemci() as c:
        r = c.post("/api/duzelt").json()
        assert r["duzeltilen"] == 0 and "ham metin" in r["hatalar"][0]
        assert "• teklif" in c.get("/api/durum").json()["rapor_metni"]
    assert madde(elle).kategori_id is None


# ---------------------------------------------------------------- C) sıralama ve karıştırma

def test_serpistirme_deterministik_gunden_gune_farkli():
    temel = [Madde(id=i, tur="bugun") for i in (1, 2, 3)]
    surekli = [Madde(id=i, tur="surekli") for i in range(10, 16)]
    bir = [m.id for m in api.serpistir(temel, surekli, GUN)]
    assert bir == [m.id for m in api.serpistir(temel, surekli, GUN)]
    assert [i for i in bir if i < 10] == [1, 2, 3]  # temel maddeler kendi sırasında
    assert bir != [m.id for m in api.serpistir(temel, surekli, GUN - timedelta(days=1))]
    assert bir[-6:] != list(range(10, 16))  # hepsi sona yığılmaz


def test_karistir_acikken_surekli_serpistirilir_kapaliyken_sonda():
    uid = kullanici_olustur()
    elle = [ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="elle", metin=f"elle {i}") for i in range(3)]
    surekli = [ekle(user_id=uid, tur="surekli", metin=f"sürekli {i}", sira=i) for i in range(5)]
    with istemci() as c:
        genel = kategoriler(c)["Genel İşler"]["id"]
        acik = bolumler(c)[genel]
        assert acik == bolumler(c)[genel]  # gün içinde sabit
        assert [i for i in acik if i in elle] == elle and acik[-5:] != surekli
        c.put("/api/ayarlar", json={"karistir": False})
        assert bolumler(c)[genel] == elle + surekli


def test_rapor_duzeni_sira_ve_kategori_yazar_karistir_ve_sifirla():
    uid = kullanici_olustur()
    a, b, cc = (ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="elle", metin=x) for x in ("a", "b", "c"))
    e = ekle(user_id=uid, tur="bulunan", tarih=GUN, kaynak="eposta", kaynak_id="e1", metin="e-posta")
    with istemci() as c:
        k = kategoriler(c)
        genel, yazisma = k["Genel İşler"]["id"], k["Yazışmalar"]["id"]
        assert bolumler(c)[genel] == [a, b, cc]
        d = c.post("/api/rapor-duzeni", json={"tarih": GUN.isoformat(), "sirali": [
            {"item_id": e, "kategori_id": yazisma}, {"item_id": b, "kategori_id": yazisma},
            {"item_id": cc, "kategori_id": genel}, {"item_id": a, "kategori_id": genel},
        ]}).json()
        assert d["ozel"] is True and d["etkin"][str(b)] == yazisma
        assert bolumler(c)[yazisma] == [e, b] and bolumler(c)[genel] == [cc, a]
        with OturumYapici() as db:
            satirlar = {r.item_id: (r.sira, r.kategori_id) for r in db.scalars(select(RaporDuzeni))}
        # kuralla aynı kategoride kalanlara elle kategori yazılmaz
        assert satirlar == {e: (1, None), b: (2, yazisma), cc: (3, None), a: (4, None)}
        # sonradan eklenen madde kendi kategorisinin sonuna düşer
        yeni = c.post("/api/maddeler", json={"tur": "bugun", "kaynak": "elle", "metin": "yeni"}).json()["id"]
        assert bolumler(c)[genel] == [cc, a, yeni]

        k2 = c.post("/api/rapor-duzeni/karistir", json={"tarih": GUN.isoformat()}).json()
        assert k2["ozel"] is True
        assert sorted(bolumler(c)[genel]) == sorted([cc, a, yeni]) and sorted(bolumler(c)[yazisma]) == sorted([e, b])

        s = c.delete(f"/api/rapor-duzeni?tarih={GUN.isoformat()}").json()
        assert s["ozel"] is False
        assert bolumler(c)[genel] == [a, b, cc, yeni] and bolumler(c)[yazisma] == [e]

        assert c.post("/api/rapor-duzeni", json={"sirali": [{"item_id": a}, {"item_id": a}]}).status_code == 422
        assert c.post("/api/rapor-duzeni", json={"sirali": [{"item_id": 9999}]}).status_code == 404
        assert c.post("/api/rapor-duzeni", json={"sirali": [{"item_id": a, "kategori_id": 9999}]}).status_code == 404


# ---------------------------------------------------------------- D) rapor metni

def ornek_gun(uid: int) -> dict[str, int]:
    return {
        "eposta": ekle(user_id=uid, tur="bulunan", tarih=GUN, kaynak="eposta", kaynak_id="e1",
                       metin="MESAM'a 'Tanıtım' konulu e-posta gönderildi"),
        "commit": ekle(user_id=uid, tur="bulunan", tarih=GUN, kaynak="medusa", kaynak_id="c1", metin="Arama hızlandırıldı"),
        "elle": ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="elle", metin="Köprü Film'e teklif gönderildi", sira=1),
        "tiksiz": ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="elle", metin="rapora girmez", tikli=False, sira=2),
        "surekli": ekle(user_id=uid, tur="surekli", metin="CRD raporları kontrol edildi"),
        "devam": ekle(user_id=uid, tur="devam", metin="MSG itirazı", asama="yanıt bekleniyor",
                      metin_ai="MSG itirazı için yanıt bekleniyor."),
        "yarin": ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="yarin", kaynak_id="yarin", metin="IMRO görüşmesi\n- Coverz listesi"),
    }


def test_kategorili_rapor_metni_birebir():
    uid = kullanici_olustur()
    ornek_gun(uid)
    with istemci() as c:
        c.put("/api/ayarlar", json={"karistir": False})
        assert c.get("/api/durum").json()["rapor_metni"] == (
            "*Günlük Rapor – 23.09.2026*\n"
            "\n"
            "*Yazışmalar:*\n"
            "• MESAM'a 'Tanıtım' konulu e-posta gönderildi\n"
            "\n"
            "*MEDUSA Çalışmaları:*\n"
            "• Arama hızlandırıldı\n"
            "\n"
            "*Genel İşler:*\n"
            "• Köprü Film'e teklif gönderildi\n"
            "• CRD raporları kontrol edildi\n"
            "\n"
            "*Devam Eden İşler:*\n"
            "• MSG itirazı — yanıt bekleniyor\n"
            "\n"
            "*Yarın:*\n"
            "• IMRO görüşmesi\n"
            "• Coverz listesi"
        )


def test_onemli_madde_onemli_bolumune_gecer():
    uid = kullanici_olustur()
    ids = ornek_gun(uid)
    with istemci() as c:
        c.put("/api/ayarlar", json={"karistir": False})
        c.patch(f"/api/maddeler/{ids['elle']}", json={"onemli": True})
        metin = c.get("/api/durum").json()["rapor_metni"]
    assert "*Genel İşler:*\n• CRD raporları kontrol edildi\n\n" in metin
    assert metin.endswith("*Önemli Konular:*\n• Köprü Film'e teklif gönderildi\n\n*Yarın:*\n• IMRO görüşmesi\n• Coverz listesi")


def test_duz_bicim_eski_metin_ve_gunun_duzenine_uyar():
    uid = kullanici_olustur()
    ids = ornek_gun(uid)
    with istemci() as c:
        c.put("/api/ayarlar", json={"rapor_bicimi": "duz"})
        assert c.get("/api/durum").json()["rapor_metni"] == (
            "*Günlük Rapor – 23.09.2026*\n"
            "\n"
            "*Yapılanlar*\n"
            "• Köprü Film'e teklif gönderildi\n"
            "• MESAM'a 'Tanıtım' konulu e-posta gönderildi\n"
            "• Arama hızlandırıldı\n"
            "• CRD raporları kontrol edildi\n"
            "\n"
            "*Devam eden*\n"
            "• MSG itirazı için yanıt bekleniyor.\n"
            "\n"
            "*Yarın*\n"
            "• IMRO görüşmesi\n"
            "• Coverz listesi"
        )
        c.post("/api/rapor-duzeni", json={"sirali": [
            {"item_id": ids["surekli"]}, {"item_id": ids["commit"]}, {"item_id": ids["elle"]}, {"item_id": ids["eposta"]},
        ]})
        metin = c.get("/api/durum").json()["rapor_metni"]
    assert "*Yapılanlar*\n• CRD raporları kontrol edildi\n• Arama hızlandırıldı\n• Köprü Film'e teklif gönderildi\n" \
           "• MESAM'a 'Tanıtım' konulu e-posta gönderildi\n\n" in metin


def test_haftalik_prompt_kategori_basliklarini_ipucu_sayar():
    assert "*Başlık:*" in servisler.haftalik_sistemi() and "ipucu" in servisler.haftalik_sistemi()


# ---------------------------------------------------------------- kategori CRUD

def test_kategori_crud_sistem_silinemez_silinenin_maddeleri_genele_duser():
    uid = kullanici_olustur()
    elle = ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="elle", metin="teklif")
    with istemci() as c:
        k = kategoriler(c)
        yeni = c.post("/api/kategoriler", json={"ad": "  Lisans   işleri ", "kaynaklar": ["gmail", "gmail"]})
        assert yeni.status_code == 201 and (yeni.json()["ad"], yeni.json()["kaynaklar"], yeni.json()["sira"]) == ("Lisans işleri", ["gmail"], 6)
        yid = yeni.json()["id"]
        assert c.post("/api/kategoriler", json={"ad": " "}).status_code == 422
        assert c.post("/api/kategoriler", json={"ad": "x", "kaynaklar": ["dropbox"]}).status_code == 422

        c.patch(f"/api/maddeler/{elle}", json={"kategori_id": yid})
        c.post("/api/rapor-duzeni", json={"sirali": [{"item_id": elle, "kategori_id": yid}]})
        assert etkin(c)[elle] == yid

        for sistem in ("Devam Eden İşler", "Önemli Konular"):
            sid = k[sistem]["id"]
            assert c.delete(f"/api/kategoriler/{sid}").status_code == 400
            assert c.patch(f"/api/kategoriler/{sid}", json={"kaynaklar": ["gmail"]}).status_code == 422
        assert c.patch(f"/api/kategoriler/{k['Önemli Konular']['id']}", json={"ad": "Öncelikli"}).json()["ad"] == "Öncelikli"

        assert c.delete(f"/api/kategoriler/{yid}").status_code == 200
        assert madde(elle).kategori_id is None
        with OturumYapici() as db:
            assert db.scalar(select(RaporDuzeni.kategori_id).where(RaporDuzeni.item_id == elle)) is None
        assert etkin(c)[elle] == k["Genel İşler"]["id"]

        sira = [k["Önemli Konular"]["id"], k["Genel İşler"]["id"]]
        liste = c.post("/api/kategoriler/sira", json={"idler": sira}).json()["kategoriler"]
        assert [x["ad"] for x in liste] == ["Öncelikli", "Genel İşler", "Yazışmalar", "MEDUSA Çalışmaları", "Devam Eden İşler"]
        assert [x["sira"] for x in liste] == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------- E) geçmiş gün

def test_tarih_parametresi_tum_uclarda_o_gune_yazar_okur(sahte_claude):
    uid = kullanici_olustur()
    dun = GUN - timedelta(days=1)
    surekli = ekle(user_id=uid, tur="surekli", metin="CRD kontrol")
    with istemci() as c:
        d = c.get(f"/api/durum?tarih={dun}").json()
        assert (d["tarih"], d["bugun"], d["kacirilan_gun"]) == (dun.isoformat(), GUN.isoformat(), None)

        m = c.post("/api/maddeler", json={"tur": "bugun", "kaynak": "elle", "metin": "dünkü iş", "tarih": dun.isoformat()}).json()
        assert m["tarih"] == dun.isoformat()
        y = c.post("/api/maddeler", json={"tur": "bugun", "kaynak": "yarin", "metin": "plan", "tarih": dun.isoformat()}).json()
        assert y["tarih"] == dun.isoformat()
        assert m["id"] in {x["id"] for x in c.get(f"/api/durum?tarih={dun}").json()["maddeler"]}
        assert m["id"] not in {x["id"] for x in c.get("/api/durum").json()["maddeler"]}
        assert c.patch(f"/api/maddeler/{m['id']}?tarih={dun}", json={"tikli": False}).json()["tikli"] is False
        c.patch(f"/api/maddeler/{m['id']}?tarih={dun}", json={"tikli": True})

        assert c.get(f"/api/bugun?tarih={dun}").json()["tarih"] == dun.isoformat()
        assert TARAMALAR == [("gmail", dun), ("github", dun)]

        sahte_claude.yanitlar = [lambda istek: json.dumps([
            {"id": g["id"], "metin": "D:" + g["metin"]}
            for g in json.loads(istek["messages"][0]["content"].split("Düzeltilecek maddeler:\n", 1)[1])])]
        assert c.post(f"/api/duzelt?tarih={dun}").json()["duzeltilen"] == 2
        with OturumYapici() as db:
            assert db.scalar(select(GunlukIfade.tarih).where(GunlukIfade.item_id == surekli)) == dun
            assert [(s.tarih, s.cagri) for s in db.scalars(select(ClaudeKullanim))] == [(GUN, 1)]  # kota bugünün
        assert "• D:dünkü iş" in c.get(f"/api/durum?tarih={dun}").json()["rapor_metni"]

        # geçmiş günün raporu upsert: gövdede ya da ?tarih= ile
        r1 = c.post(f"/api/raporlar?tarih={dun}", json={"metin": "ilk", "tur": "gunluk"}).json()
        r2 = c.post("/api/raporlar", json={"metin": "güncel", "tur": "gunluk", "tarih": dun.isoformat()}).json()
        assert r1["id"] == r2["id"] and r2["tarih"] == dun.isoformat()
        with OturumYapici() as db:
            assert [(r.tarih, r.metin) for r in db.scalars(select(Rapor))] == [(dun, "güncel")]
        assert c.get(f"/api/durum?tarih={dun}").json()["son_kopya"] is not None
        assert c.get("/api/durum").json()["son_kopya"] is None

        k = kategoriler(c)
        c.post("/api/rapor-duzeni", json={"tarih": dun.isoformat(), "sirali": [{"item_id": m["id"], "kategori_id": k["Yazışmalar"]["id"]}]})
        assert etkin(c, dun)[m["id"]] == k["Yazışmalar"]["id"]


@pytest.mark.parametrize("deger", [(GUN - timedelta(days=31)).isoformat(), (GUN + timedelta(days=1)).isoformat(), "23-09-2026"])
def test_tarih_araligi_disi_400(deger):
    uid = kullanici_olustur()
    elle = ekle(user_id=uid, tur="bugun", tarih=GUN, kaynak="elle", metin="x")
    with istemci() as c:
        assert c.get(f"/api/durum?tarih={GUN - timedelta(days=30)}").status_code == 200  # sınır dahil
        for yontem, yol, govde in [
            ("get", f"/api/durum?tarih={deger}", None),
            ("get", f"/api/bugun?tarih={deger}", None),
            ("post", f"/api/duzelt?tarih={deger}", None),
            ("patch", f"/api/maddeler/{elle}?tarih={deger}", {"tikli": True}),
            ("post", f"/api/raporlar?tarih={deger}", {"metin": "x"}),
            ("get", f"/api/rapor-duzeni?tarih={deger}", None),
            ("delete", f"/api/rapor-duzeni?tarih={deger}", None),
        ]:
            y = c.request(yontem, yol, json=govde)
            assert y.status_code == 400, (yol, y.status_code)
        if deger[4] == "-":  # gövdedeki tarih ISO biçimindeyse aralık denetimi 400
            assert c.post("/api/maddeler", json={"tur": "bugun", "kaynak": "elle", "metin": "x", "tarih": deger}).status_code == 400
            assert c.post("/api/rapor-duzeni", json={"tarih": deger, "sirali": []}).status_code == 400
            assert c.post("/api/rapor-duzeni/karistir", json={"tarih": deger}).status_code == 400


def test_tarama_o_gunun_araligiyla_cagrilir(monkeypatch):
    aramalar = []

    class SahteImap:
        def search(self, *args):
            aramalar.append(args)
            return "OK", [b""]

    monkeypatch.setattr(servisler, "_gmail_oturumu", lambda k, s, islem: islem(SahteImap()))
    assert GERCEK_GMAIL_TARA("a@gmail.com", "s", date(2026, 9, 1)) == []
    # bir gün geriden (sunucu saati payı) o günün ertesine kadar; kesin süzgeç Date başlığının Istanbul günü
    assert aramalar == [(None, "SINCE", "31-Aug-2026", "BEFORE", "02-Sep-2026")]

    parametreler = []

    def isleyici(istek):
        parametreler.append(dict(istek.url.params))
        return httpx.Response(200, json=[])

    GERCEK_GITHUB_TARA("t", "a/medusa", date(2026, 9, 22), httpx.Client(transport=httpx.MockTransport(isleyici)))
    assert (parametreler[0]["since"], parametreler[0]["until"]) == ("2026-09-21T21:00:00Z", "2026-09-22T21:00:00Z")


def test_raporu_uret_verilen_gunle_tarar():
    sonuc = servisler.raporu_uret({"kaynaklar": {"gmail": True, "github": True}, "gmail_kullanici": "a", "gmail_sifre": "s",
                                   "github_token": "t", "github_repo": "a/b"}, "", frozenset(), date(2026, 9, 10))
    assert sonuc["tarih"] == "2026-09-10" and TARAMALAR == [("gmail", date(2026, 9, 10)), ("github", date(2026, 9, 10))]


def test_gecmis_gun_taramasi_bugunun_onbellegini_silmez():
    kullanici_olustur()
    with istemci() as c:
        c.get("/api/bugun")
        c.get(f"/api/bugun?tarih={GUN - timedelta(days=2)}")
        c.get("/api/bugun")
    assert TARAMALAR == [("gmail", GUN), ("github", GUN), ("gmail", GUN - timedelta(days=2)), ("github", GUN - timedelta(days=2))]


# ---------------------------------------------------------------- kaçırılan gün

@pytest.mark.parametrize("bugun, beklenen", [
    (date(2026, 9, 23), "2026-09-22"),  # Çarşamba → Salı
    (date(2026, 9, 21), "2026-09-18"),  # Pazartesi → Cuma (hafta sonu atlanır)
    (date(2026, 9, 20), "2026-09-18"),  # Pazar → Cuma
])
def test_kacirilan_gun(monkeypatch, bugun, beklenen):
    monkeypatch.setattr(api, "bugun", lambda: bugun)
    uid = kullanici_olustur()
    with istemci() as c:
        assert c.get("/api/durum").json()["kacirilan_gun"] == beklenen
        assert c.get(f"/api/durum?tarih={bugun - timedelta(days=1)}").json()["kacirilan_gun"] is None  # yalnız bugün
        c.post(f"/api/raporlar?tarih={beklenen}", json={"metin": "• x"})
        assert c.get("/api/durum").json()["kacirilan_gun"] is None
    with OturumYapici() as db:
        assert db.scalar(select(Rapor.tarih).where(Rapor.user_id == uid)).isoformat() == beklenen


def test_kacirilan_gun_hesap_yeniyse_ya_da_gun_hatirlatma_disindaysa_yok(monkeypatch):
    kullanici_olustur(olusturma=datetime(2026, 9, 23, 6, tzinfo=timezone.utc))
    kullanici_olustur("b@ornek.com", "B")
    with istemci() as c:
        assert c.get("/api/durum").json()["kacirilan_gun"] is None
    with istemci("b@ornek.com") as c:
        c.put("/api/ayarlar", json={"hatirlatma_gunler": [1, 3]})  # Salı hatırlatma günü değil → Pazartesi
        assert c.get("/api/durum").json()["kacirilan_gun"] == "2026-09-21"
        c.put("/api/ayarlar", json={"hatirlatma_gunler": []})
        assert c.get("/api/durum").json()["kacirilan_gun"] is None


# ---------------------------------------------------------------- kullanıcı ayrımı

def test_kullanici_ayrimi_kategori_ve_duzen():
    a = kullanici_olustur()
    kullanici_olustur("b@ornek.com", "B")
    a_madde = ekle(user_id=a, tur="bugun", tarih=GUN, kaynak="elle", metin="A'nın işi")
    with istemci() as c:
        a_kat = kategoriler(c)["Genel İşler"]["id"]
        c.post("/api/rapor-duzeni", json={"sirali": [{"item_id": a_madde}]})
    with istemci("b@ornek.com") as c:
        b_madde = c.post("/api/maddeler", json={"tur": "bugun", "kaynak": "elle", "metin": "B'nin işi"}).json()["id"]
        assert a_kat not in {k["id"] for k in kategoriler(c).values()}
        assert c.patch(f"/api/kategoriler/{a_kat}", json={"ad": "x"}).status_code == 404
        assert c.delete(f"/api/kategoriler/{a_kat}").status_code == 404
        assert c.post("/api/kategoriler/sira", json={"idler": [a_kat]}).status_code == 404
        assert c.patch(f"/api/maddeler/{b_madde}", json={"kategori_id": a_kat}).status_code == 404
        assert c.patch(f"/api/maddeler/{a_madde}", json={"onemli": True}).status_code == 404
        assert c.post("/api/rapor-duzeni", json={"sirali": [{"item_id": a_madde}]}).status_code == 404
        assert c.post("/api/rapor-duzeni", json={"sirali": [{"item_id": b_madde, "kategori_id": a_kat}]}).status_code == 404
        c.delete("/api/rapor-duzeni")
        assert a_madde not in {i for liste in bolumler(c).values() for i in liste}
    with OturumYapici() as db:
        assert db.scalar(select(RaporDuzeni.item_id).where(RaporDuzeni.user_id == a)) == a_madde  # B'nin sıfırlaması dokunmaz
        assert db.get(Kategori, a_kat).ad == "Genel İşler"
