"""A1v2: dökümde yapılandırılmış sentez (JSON sözleşmesi ve sunucu doğrulaması), istatistik düzeltmeleri, unvan,
sunucuda PDF (ReportLab + IBM Plex Sans), alan bazlı düzenleme. sqlite; Claude sahte; "bugün" 29 Eylül 2026."""
import io
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pypdf
import pytest
from fastapi.testclient import TestClient
from reportlab.pdfbase.ttfonts import TTFont
from sqlalchemy import select

import api
import app as uygulama
import guvenlik
import pdf_uret
import servisler
from veritabani import ClaudeKullanim, Kullanici, KullaniciAyari, Madde, OturumYapici, Rapor, Temel, motor

SIFRE = "dogru-sifre-123"
BUGUN = date(2026, 9, 29)
ESLEME = [{"kaynak": "Medusa", "hedef": "Edisyon uygulaması"}]


@pytest.fixture(autouse=True)
def ortam(monkeypatch):
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    api.onbellegi_temizle()
    monkeypatch.setattr(api, "bugun", lambda: BUGUN)
    yield


def kullanici_olustur(eposta="ufuk@ilsvision.com", ad="Ufuk Çetinkaya", unvan=None,
                      olusturma=datetime(2026, 9, 17, 6, tzinfo=timezone.utc), **ayar) -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad=ad, unvan=unvan, sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol="uye", aktif=True,
                      sifre_degistirmeli=False, olusturma=olusturma)
        db.add(k)
        db.commit()
        db.add(KullaniciAyari(user_id=k.id, kurulum_tamam=True, **ayar))
        db.commit()
        return k.id


def istemci(eposta="ufuk@ilsvision.com") -> TestClient:
    c = TestClient(uygulama.app, follow_redirects=False)
    c.__enter__()
    assert c.post("/giris", data={"eposta": eposta, "sifre": SIFRE}).status_code == 303
    return c


def rapor(uid, gun, metin="• iş"):
    with OturumYapici() as db:
        db.add(Rapor(user_id=uid, tarih=gun, metin=metin, tur="gunluk"))
        db.commit()


def madde(uid, gun, metin, kaynak="elle", **alan) -> int:
    tur = "bugun" if kaynak in ("elle", "ses", "not") else "bulunan"
    with OturumYapici() as db:
        m = Madde(user_id=uid, tur=tur, tarih=gun, kaynak=kaynak, metin=metin, tikli=alan.pop("tikli", True), **alan)
        db.add(m)
        db.commit()
        return m.id


def e(n: int) -> date:
    return date(2026, 9, n)


def veri(uid) -> list[int]:
    """17, 18, 21 Eylül raporlu; 4 madde (biri 21'de)."""
    for g in (e(17), e(18), e(21)):
        rapor(uid, g)
    return [madde(uid, e(17), "MESAM'a 'A' konulu e-posta gönderildi", "eposta"),
            madde(uid, e(17), "Arama hızlandırıldı", "medusa"),
            madde(uid, e(18), "Katalog düzeltildi"),
            madde(uid, e(21), "'Bütçe' tablosu üzerinde çalışıldı", "drive")]


def basari(alanlar, one=None, **ek) -> str:
    return json.dumps({"one_cikanlar": one if one is not None else [{"baslik": "Sonuç", "aciklama": "Ayrıntı."}],
                       "alanlar": alanlar, "tamamlanan": ek.get("tamamlanan", []), "devam_eden": ek.get("devam_eden", []),
                       "surekli": ek.get("surekli", "")}, ensure_ascii=False)


def uret(c, bicim="basari", tur="aylik", donem="2026-09"):
    return c.post("/api/ozet", json={"tur": tur, "donem": donem, "bicim": bicim})


# ---------------------------------------------------------------- 1) JSON sözleşmesi ve sunucu doğrulaması

def test_bilinmeyen_id_atilir_sayilar_sunucudan_atanmamislar_digere(sahte_claude):
    uid = kullanici_olustur()
    a, b, c_, d = veri(uid)
    sahte_claude.yanitlar = [basari([
        {"ad": "Yazışma", "temalar": [{"ad": "MESAM", "ozet": "Yazışıldı.", "etiketler": ["x"], "madde_idleri": [a, 99999, "abc", a],
                                       "madde_sayisi": 40}]},
        {"ad": "Uygulama", "temalar": [{"ad": "Arama", "ozet": "Hızlandı.", "etiketler": [], "madde_idleri": [str(b), a]},
                                       {"ad": "Boş tema", "ozet": "Hiç geçerli madde yok.", "etiketler": [], "madde_idleri": [123456]}]},
    ])]
    r = uret(istemci())
    assert r.status_code == 200
    alanlar = r.json()["ozet"]["yapi"]["alanlar"]
    assert [(x["ad"], x["madde_sayisi"]) for x in alanlar] == [("Yazışma", 1), ("Uygulama", 1), ("Diğer", 2)]
    assert alanlar[0]["temalar"][0]["madde_idleri"] == [a] and alanlar[0]["temalar"][0]["madde_sayisi"] == 1
    assert alanlar[1]["temalar"][0]["madde_idleri"] == [b]  # "b" dize olarak geldi; a zaten ilk temada sayıldı
    assert [t["ad"] for t in alanlar[1]["temalar"]] == ["Arama"]  # geçerli maddesi olmayan tema atılır
    diger = alanlar[2]["temalar"][0]
    assert diger["ad"] == "Diğer" and sorted(diger["madde_idleri"]) == sorted([c_, d])
    assert "Katalog düzeltildi" in diger["ozet"]


def test_patron_json_whatsapp_metnine_cevrilir(sahte_claude):
    uid = kullanici_olustur()
    veri(uid)
    sahte_claude.yanitlar = [json.dumps({
        "bolumler": [{"ad": "Edisyon Çalışmaları:", "maddeler": ["• MESAM ile *yazışıldı*.", "Katalog düzeltildi."]},
                     {"ad": "Boş", "maddeler": []}],
        "tamamlanan": ["Köprü Film lisans talebi", "Lisanslama modülü ilk sürüm"],
        "devam_eden": ["MSG Ağustos itirazı — yanıt bekleniyor"]}, ensure_ascii=False)]
    r = uret(istemci(), "patron").json()
    assert r["metin"] == (
        "*Aylık Özet – Eylül 2026*\n\n*Edisyon Çalışmaları:*\n• MESAM ile yazışıldı.\n• Katalog düzeltildi.\n\n"
        "*Tamamlananlar:*\n• Köprü Film lisans talebi · Lisanslama modülü ilk sürüm\n\n"
        "*Devam Eden İşler:*\n• MSG Ağustos itirazı — yanıt bekleniyor")
    assert r["ozet"]["yapi"]["bolumler"] == [{"ad": "Edisyon Çalışmaları", "maddeler": ["MESAM ile yazışıldı.", "Katalog düzeltildi."]}]


def test_bozuk_json_bir_kez_yeniden_denenir(sahte_claude):
    uid = kullanici_olustur()
    a = veri(uid)[0]
    sahte_claude.yanitlar = ["Elbette! İşte döküm:", "```json\n" + basari([{"ad": "A", "temalar": [
        {"ad": "T", "ozet": "", "etiketler": [], "madde_idleri": [a]}]}]) + "\n```"]
    c = istemci()
    r = uret(c)
    assert r.status_code == 200 and len(sahte_claude.istekler) == 2
    assert r.json()["ozet"]["yapi"]["alanlar"][0]["temalar"][0]["madde_idleri"] == [a]
    with OturumYapici() as db:  # iki deneme tek hak
        assert db.scalar(select(ClaudeKullanim.cagri).where(ClaudeKullanim.user_id == uid)) == 1


def test_iki_kez_bozuk_json_502_ve_kayit_korunur(sahte_claude):
    uid = kullanici_olustur()
    veri(uid)
    c = istemci()
    sahte_claude.yanitlar = [basari([], one=[{"baslik": "SAĞLAM", "aciklama": ""}])]
    assert uret(c).status_code == 200
    sahte_claude.yanitlar = ["{bozuk", json.dumps({"alanlar": "dizi değil", "one_cikanlar": []})]
    r = uret(c)
    assert r.status_code == 502 and "kayıtlı özet değişmedi" in r.json()["detail"] and len(sahte_claude.istekler) == 3
    kayit = c.get("/api/ozet?tur=aylik&donem=2026-09").json()["ozetler"]["basari"]
    assert kayit["yapi"]["one_cikanlar"][0]["baslik"] == "SAĞLAM"


def test_girdi_etkin_kategori_kaynak_ve_rapor_metniyle(sahte_claude):
    uid = kullanici_olustur(ad_eslemeleri=ESLEME)
    rapor(uid, e(17))
    mid = madde(uid, e(17), "Medusa'da arama hizlandi", "medusa", metin_ai="Medusa'da arama hızlandırıldı.")
    madde(uid, e(17), "Tiksiz iş", tikli=False)
    madde(uid, e(17), "Gizli iş", gizli=True)
    istemci().get("/api/kategoriler")  # başlangıç kategorileri açılır
    sahte_claude.yanitlar = [basari([])]
    uret(istemci())
    girdi = json.loads(sahte_claude.istekler[0]["messages"][0]["content"].split("Girdi:\n", 1)[1])
    assert girdi["maddeler"] == [{"id": mid, "tarih": "2026-09-17", "kategori": "Uygulama Çalışmaları",
                                  "kaynak": "uygulama", "metin": "Edisyon uygulaması'nda arama hızlandırıldı."}]


def test_metin_alanlarina_ad_eslemesi_ve_e2_son_adimda(sahte_claude):
    uid = kullanici_olustur(ad_eslemeleri=ESLEME, kendi_alanlar=["medusarights.com"])
    a = veri(uid)[0]
    sahte_claude.yanitlar = [basari(
        [{"ad": "Medusa işleri", "temalar": [{"ad": "Medusa'da arama", "ozet": "Medusa'ya eklendi.",
                                              "etiketler": ["Medusa", "ilsvision.com", "şirket içi", "Arama"], "madde_idleri": [a]}]}],
        one=[{"baslik": "Medusa'da gelişme", "aciklama": "Medusa'nın araması."}], tamamlanan=["Medusa sürümü"], surekli="Medusa takibi")]
    r = uret(istemci()).json()
    yapi = r["ozet"]["yapi"]
    assert "Medusa" not in json.dumps(yapi, ensure_ascii=False) and "Medusa" not in r["metin"]
    tema = yapi["alanlar"][0]["temalar"][0]
    assert tema["ad"] == "Edisyon uygulaması'nda arama" and tema["etiketler"] == ["Edisyon uygulaması", "Arama"]
    assert yapi["one_cikanlar"][0]["baslik"] == "Edisyon uygulaması'nda gelişme"


def test_basari_duz_metni_ve_hesaplanan_sayilar(sahte_claude):
    uid = kullanici_olustur()
    a, b, *_ = veri(uid)
    sahte_claude.yanitlar = [basari([{"ad": "Alan", "temalar": [{"ad": "Tema", "ozet": "Özet.", "etiketler": ["e1"], "madde_idleri": [a, b]}]}],
                                    one=[{"baslik": "Başlık", "aciklama": "Açıklama."}], tamamlanan=["İş A"], devam_eden=["İş B"], surekli="Takip")]
    r = uret(istemci()).json()
    s = r["ozet"]["yapi"]["sayilar"]
    assert (s["rapor_gunu"], s["is_gunu"], s["toplam_madde"], s["eposta"], s["uygulama"], s["dosya"], s["tamamlanan"]) == (3, 9, 3, 1, 1, 1, 1)
    assert s["kapsam"] == "17–21 Eylül (aracın kullanıldığı dönem)"
    assert r["metin"].startswith("Başarı Dökümü – Eylül 2026\n\nÖne çıkanlar\n• Başlık. Açıklama.\n\n"
                                 "Sorumluluk alanlarına göre\nAlan · 2 madde\n• Tema (2 madde): Özet. [e1]")
    for parca in ("Tamamlanan işler\n• İş A", "Devam eden işler\n• İş B", "Sürekli üstlenilen işler\n• Takip",
                  "Sayılarla\n• 3/9 iş günü rapor · 3 madde · 1 kurumsal e-posta · 1 uygulama çalışması · 1 dosya · 1 tamamlanan iş"):
        assert parca in r["metin"], parca


# ---------------------------------------------------------------- 2) istatistik düzeltmeleri

def test_kaynak_etiketleri_ve_sifir_gizleme():
    uid = kullanici_olustur()
    veri(uid)
    ist = istemci().get("/api/ozet?tur=aylik&donem=2026-09").json()["istatistik"]
    assert ist["kaynaklar"] == [{"anahtar": "eposta", "ad": "E-posta", "sayi": 1},
                                {"anahtar": "commit", "ad": "Uygulama çalışmaları", "sayi": 1},
                                {"anahtar": "elle", "ad": "Elle eklenen", "sayi": 1},
                                {"anahtar": "dosya", "ad": "Dosyalar", "sayi": 1}]  # ses, not, toplantı sıfır: yok


def test_uygulama_etiketi_proje_kategorisinin_adi():
    uid = kullanici_olustur(proje_adi="Medusa", ad_eslemeleri=ESLEME)
    veri(uid)
    c = istemci()
    c.get("/api/kategoriler")
    ist = c.get("/api/ozet?tur=aylik&donem=2026-09").json()["istatistik"]
    assert next(k for k in ist["kaynaklar"] if k["anahtar"] == "commit")["ad"] == "Edisyon uygulaması Çalışmaları"


def test_en_cok_yazisilan_genel_saglayici_ve_adsizlar_disarida():
    uid = kullanici_olustur()
    rapor(uid, e(17))
    for metin in ("gmail.com'a 'A' konulu e-posta gönderildi", "Bir kişiye 'B' konulu e-posta gönderildi",
                  "Ekip içi 'C' konulu e-posta gönderildi", "Şirket içi 'D' konulu e-posta gönderildi",
                  "Ayşe Yılmaz'a 'E' konulu e-posta gönderildi", "hotmail.com ve MESAM'a 'F' konulu e-posta gönderildi"):
        madde(uid, e(17), metin, "eposta")
    ist = istemci().get("/api/ozet?tur=aylik&donem=2026-09").json()["istatistik"]
    assert ist["kurumlar"] == [{"ad": "Ayşe Yılmaz", "sayi": 1}, {"ad": "MESAM", "sayi": 1}]


def test_tamamlanan_ve_acik_isler_rapor_metniyle():
    uid = kullanici_olustur(ad_eslemeleri=ESLEME)
    rapor(uid, e(17))
    madde(uid, e(17), "medusa surumu tamamlandı", metin_ai="Medusa sürümü tamamlandı.")
    with OturumYapici() as db:
        db.add(Madde(user_id=uid, tur="devam", metin="medusa testi", asama="bekliyor", tikli=True,
                     metin_ai="Medusa testi için yanıt bekleniyor.", olusturma=datetime(2026, 9, 20, tzinfo=timezone.utc)))
        db.add(Madde(user_id=uid, tur="devam", metin="Elle iş", asama="sürüyor", tikli=True, kullanici_duzenledi=True,
                     metin_ai="Yok sayılır.", olusturma=datetime(2026, 9, 20, tzinfo=timezone.utc)))
        db.commit()
    ist = istemci().get("/api/ozet?tur=aylik&donem=2026-09").json()["istatistik"]
    assert ist["tamamlanan"] == [{"metin": "Edisyon uygulaması sürümü tamamlandı.", "tarih": "2026-09-17"}]
    assert [(a["metin"], a["asama"]) for a in ist["acik"]] == [
        ("Edisyon uygulaması testi için yanıt bekleniyor.", ""), ("Elle iş", "sürüyor")]


def test_kapsama_araligi_gercek_kullanim():
    uid = kullanici_olustur(olusturma=datetime(2026, 8, 1, tzinfo=timezone.utc))
    c = istemci()
    assert c.get("/api/ozet?tur=aylik&donem=2026-09").json()["istatistik"]["kullanim"] is None
    rapor(uid, e(17))
    rapor(uid, e(29))
    k = c.get("/api/ozet?tur=aylik&donem=2026-09").json()["istatistik"]["kullanim"]
    assert k["metin"] == "17–29 Eylül (aracın kullanıldığı dönem)" and k["arac"] is True
    rapor(uid, e(1))
    assert c.get("/api/ozet?tur=aylik&donem=2026-09").json()["istatistik"]["kullanim"]["metin"] == "1–29 Eylül"
    assert api.tarih_araligi(date(2026, 9, 28), date(2026, 10, 2)) == "28 Eylül – 2 Ekim"


# ---------------------------------------------------------------- 3) unvan

def test_unvan_patch_ve_kullanici_ayrimi():
    uid = kullanici_olustur()
    kullanici_olustur("b@ornek.com", "Burak")
    c = istemci()
    r = c.patch("/api/profil", json={"unvan": "  Edisyon   ve Meslek Birlikleri Danışmanı "})
    assert r.status_code == 200 and r.json() == {"ad": "Ufuk Çetinkaya", "ad_yer_tutucu": False,
                                                 "unvan": "Edisyon ve Meslek Birlikleri Danışmanı"}
    assert c.patch("/api/profil", json={"unvan": "x" * 81}).status_code == 422
    assert c.patch("/api/profil", json={"ad": "Ufuk Ç."}).json()["unvan"] == "Edisyon ve Meslek Birlikleri Danışmanı"
    with OturumYapici() as db:
        assert [(k.ad, k.unvan) for k in db.scalars(select(Kullanici).order_by(Kullanici.id))] == [
            ("Ufuk Ç.", "Edisyon ve Meslek Birlikleri Danışmanı"), ("Burak", None)]
    assert c.patch("/api/profil", json={"unvan": "  "}).json()["unvan"] == ""
    with OturumYapici() as db:
        assert db.get(Kullanici, uid).unvan is None
    assert 'id="unvanDugme"' in c.get("/ayarlar").text and "Unvan ekle" in c.get("/ayarlar").text


# ---------------------------------------------------------------- 4) PDF

def pdf_metni(icerik: bytes) -> tuple[int, str]:
    okuyucu = pypdf.PdfReader(io.BytesIO(icerik))
    return len(okuyucu.pages), "\n".join(s.extract_text() for s in okuyucu.pages)


def tr_kucuk(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def test_yazi_tipleri_ve_turkce_karakterler():
    klasor = Path(pdf_uret.YAZI_KLASORU)
    assert (klasor / "OFL.txt").read_text().count("SIL Open Font License") >= 1
    for dosya in pdf_uret.YAZILAR.values():
        cmap = TTFont(dosya + "-test", str(klasor / f"{dosya}.ttf")).face.charToGlyph
        assert all(ord(h) in cmap for h in "ÇçĞğİıÖöŞşÜü–·•"), dosya


def test_basari_pdf_uctan_uca(sahte_claude):
    uid = kullanici_olustur(unvan="Edisyon Danışmanı", ad_eslemeleri=ESLEME)
    ids = veri(uid)
    madde(uid, e(21), "Medusa'da rapor düzeldi", "medusa")
    temalar = [{"ad": f"Medusa teması {i}", "ozet": "Medusa'da uzun bir özet cümlesi. " * 4, "etiketler": ["Medusa", "Etiket"],
                "madde_idleri": [ids[i % 4]] if i < 4 else []} for i in range(4)]
    sahte_claude.yanitlar = [basari([{"ad": "Medusa alanı", "temalar": temalar}],
                                    one=[{"baslik": f"Öne çıkan {i}", "aciklama": "Medusa açıklaması."} for i in range(5)],
                                    tamamlanan=["Medusa sürümü"], devam_eden=["Medusa testi · bekliyor"], surekli="Medusa takibi")]
    c = istemci()
    assert uret(c).status_code == 200
    r = c.get("/api/ozet/pdf?tur=aylik&donem=2026-09&bicim=basari")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content[:4] == b"%PDF"
    assert 'filename="Basari-Dokumu-Eylul-2026-Ufuk-Cetinkaya.pdf"' in r.headers["content-disposition"]
    sayfa, metin = pdf_metni(r.content)
    assert sayfa >= 2
    assert "Çetinkaya" in metin and "öne çıkanlar" in tr_kucuk(metin) and "Ufuk Çetinkaya · Edisyon Danışmanı" in metin
    assert "Ayrıntılar" in metin and "Sürekli" in metin.title() and "Eylül 2026" in metin and f"1 / {sayfa}" in metin
    assert "Medusa" not in metin and "Edisyon uygulaması" in metin  # ad eşlemesi PDF'te de


def test_patron_pdf_tek_sayfa(sahte_claude):
    uid = kullanici_olustur()
    veri(uid)
    sahte_claude.yanitlar = [json.dumps({"bolumler": [{"ad": "Edisyon", "maddeler": [f"Madde {i} yürütüldü." for i in range(10)]}],
                                         "tamamlanan": ["A"], "devam_eden": ["B — bekliyor"]})]
    c = istemci()
    uret(c, "patron")
    r = c.get("/api/ozet/pdf?tur=aylik&donem=2026-09&bicim=patron")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    assert 'filename="Aylik-Ozet-Eylul-2026-Ufuk-Cetinkaya.pdf"' in r.headers["content-disposition"]
    sayfa, metin = pdf_metni(r.content)
    assert sayfa == 1 and "Madde 9 yürütüldü." in metin and "Tamamlanan" in metin and "Devam eden" in metin


def test_pdf_ozet_yoksa_404_ve_kullanici_ayrimi(sahte_claude):
    uid = kullanici_olustur()
    kullanici_olustur("b@ornek.com", "B")
    veri(uid)
    c, cb = istemci(), istemci("b@ornek.com")
    assert c.get("/api/ozet/pdf?tur=aylik&donem=2026-09&bicim=basari").status_code == 404
    sahte_claude.yanitlar = [basari([])]
    uret(c)
    assert c.get("/api/ozet/pdf?tur=aylik&donem=2026-09&bicim=basari").status_code == 200
    assert cb.get("/api/ozet/pdf?tur=aylik&donem=2026-09&bicim=basari").status_code == 404
    assert c.get("/api/ozet/pdf?tur=aylik&donem=2026-09&bicim=x").status_code == 422
    assert TestClient(uygulama.app).get("/api/ozet/pdf?tur=aylik&donem=2026-09").status_code == 401


def test_eski_duz_metin_kaydinin_pdfi():
    uid = kullanici_olustur()
    with OturumYapici() as db:
        db.add(Rapor(user_id=uid, tarih=e(1), tur="aylik", bicim="patron",
                     metin="*Aylık Özet – Eylül 2026*\n\n*Yazışmalar:*\n• ESKİ MADDE"))
        db.commit()
    sayfa, metin = pdf_metni(istemci().get("/api/ozet/pdf?tur=aylik&donem=2026-09&bicim=patron").content)
    assert sayfa == 1 and "ESKİ MADDE" in metin


def test_tema_sayfalar_arasinda_bolunmez():
    temalar = [{"ad": f"Tema {i}", "ozet": "Uzun özet cümlesi. " * 30, "etiketler": ["a", "b"], "madde_idleri": [i],
                "madde_sayisi": 1} for i in range(14)]
    yapi = {"bicim": "basari", "one_cikanlar": [], "alanlar": [{"ad": "Alan", "madde_sayisi": 14, "temalar": temalar}],
            "tamamlanan": [], "devam_eden": [], "surekli": "", "sayilar": {}}
    bloklar = pdf_uret.basari_bloklari("aylik", "Eylül 2026", yapi, "Ad", date(2026, 9, 29))
    pdf_uret.yazilari_kaydet()
    sayfalar = pdf_uret.dizil(bloklar, 0)
    assert len(sayfalar) >= 3
    for sayfa in sayfalar:
        for y, b in sayfa:
            assert y + b.yukseklik <= pdf_uret.ALT_SINIR  # her blok (tema) tek sayfaya sığar
    _, metin = pdf_metni(pdf_uret.ozet_pdf("aylik", "basari", "Eylül 2026", yapi, "", {}, "Ad"))
    assert all(f"Tema {i}" in metin for i in range(14))


# ---------------------------------------------------------------- 5) alan bazlı düzenleme

def test_duzenleme_put_yapiyi_gunceller_claude_cagrilmaz(sahte_claude):
    uid = kullanici_olustur()
    a, b, *_ = veri(uid)
    sahte_claude.yanitlar = [basari([{"ad": "Alan", "temalar": [{"ad": "Tema", "ozet": "", "etiketler": [], "madde_idleri": [a, b]}]}],
                                    tamamlanan=["İş A"])]
    c = istemci()
    yapi = uret(c).json()["ozet"]["yapi"]
    yapi["one_cikanlar"][0]["baslik"] = "Elle yazılan başlık"
    yapi["alanlar"][0]["temalar"][0]["madde_idleri"] = [a, 424242]  # kayıtta olmayan id atılır
    yapi["alanlar"][0]["temalar"].append({"ad": "Yeni tema", "ozet": "Elle.", "etiketler": ["x", " ", "y"], "madde_idleri": []})
    yapi["tamamlanan"] += ["İş B", "İş C"]
    r = c.put("/api/ozet", json={"tur": "aylik", "donem": "2026-09", "bicim": "basari", "yapi": yapi})
    assert r.status_code == 200 and len(sahte_claude.istekler) == 1
    y = r.json()["yapi"]
    assert y["one_cikanlar"][0]["baslik"] == "Elle yazılan başlık" and "Elle yazılan başlık" in r.json()["metin"]
    assert [(t["ad"], t["madde_sayisi"]) for t in y["alanlar"][0]["temalar"]] == [("Tema", 1), ("Yeni tema", 0)]
    assert y["alanlar"][0]["temalar"][1]["etiketler"] == ["x", "y"]
    assert y["sayilar"]["tamamlanan"] == 3 and "İş C" in r.json()["metin"]
    with OturumYapici() as db:
        kayit = db.scalar(select(Rapor).where(Rapor.user_id == uid, Rapor.tur == "aylik"))
        assert kayit.yapi["one_cikanlar"][0]["baslik"] == "Elle yazılan başlık" and "Elle yazılan başlık" in kayit.metin


def test_duzenleme_kullanici_ayrimi(sahte_claude):
    a = kullanici_olustur()
    kullanici_olustur("b@ornek.com", "B")
    veri(a)
    sahte_claude.yanitlar = [basari([])]
    ca, cb = istemci(), istemci("b@ornek.com")
    uret(ca)
    yapi = {"one_cikanlar": [{"baslik": "B'NİN", "aciklama": ""}], "alanlar": [], "tamamlanan": [], "devam_eden": [], "surekli": ""}
    assert cb.put("/api/ozet", json={"tur": "aylik", "donem": "2026-09", "bicim": "basari", "yapi": yapi}).status_code == 200
    assert ca.get("/api/ozet?tur=aylik&donem=2026-09").json()["ozetler"]["basari"]["yapi"]["one_cikanlar"][0]["baslik"] == "Sonuç"


# ---------------------------------------------------------------- 6) ekran

def test_gecmis_sayfasi_yeni_duzen():
    kullanici_olustur()
    html = istemci().get("/gecmis").text
    for parca in ('class="gUst"', 'id="kpiTel"', 'id="belgeKart"', "yalnız sen görürsün", 'id="duzenleBtn"',
                  'id="duzenKaydet"', "Maddeler nereden geldi", "En çok yazışılan", "Açık kalan işler",
                  "PDF indir", "WhatsApp'ta aç", "grid-template-columns:430px"):
        assert parca in html, parca
    assert "window.print" not in html
