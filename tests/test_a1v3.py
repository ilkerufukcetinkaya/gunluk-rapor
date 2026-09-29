"""A1v3: "Performans Özeti" / "Yönetici Özeti" adları (iç anahtarlar 'basari' / 'patron' aynen), "Ayın özeti" istatistik
alanları (gün gün / ay ay dağılım), Yönetici Özeti JSON sözleşmesi (vurgu + metin, sınırlar, bir kez yeniden istem,
kırpma), WhatsApp metni ve yeni Yönetici Özeti PDF'i. sqlite; Claude sahte; "bugün" 29 Eylül 2026 (Salı)."""
import io
import json
from datetime import date, datetime, timezone

import pypdf
import pytest
from fastapi.testclient import TestClient

import api
import app as uygulama
import guvenlik
import pdf_uret
import servisler
from veritabani import Kullanici, KullaniciAyari, Madde, OturumYapici, Rapor, Temel, motor

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


def kullanici_olustur(eposta="ufuk@ilsvision.com", ad="Ufuk Çetinkaya", unvan="Edisyon Danışmanı",
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


def rapor(uid, gun, metin="• iş", **alan):
    with OturumYapici() as db:
        db.add(Rapor(user_id=uid, tarih=gun, metin=metin, tur="gunluk", **alan))
        db.commit()


def madde(uid, gun, metin, kaynak="elle") -> int:
    tur = "bugun" if kaynak in ("elle", "ses", "not") else "bulunan"
    with OturumYapici() as db:
        m = Madde(user_id=uid, tur=tur, tarih=gun, kaynak=kaynak, metin=metin, tikli=True)
        db.add(m)
        db.commit()
        return m.id


def e(n: int) -> date:
    return date(2026, 9, n)


def maddeler_metni(n: int) -> str:
    return "*Günlük Rapor*\n\n*İşler:*\n" + "\n".join(f"• iş {i}" for i in range(n))


def veri(uid) -> dict:
    """17 (3 madde), 18 (1), 23 (5; en yoğun), 24 (2, otomatik) Eylül raporlu; 21, 22, 25, 28, 29 iş günü raporsuz."""
    rapor(uid, e(17), maddeler_metni(3))
    rapor(uid, e(18), maddeler_metni(1))
    rapor(uid, e(23), maddeler_metni(5))
    rapor(uid, e(24), maddeler_metni(2), gonderim="otomatik")
    return {
        "eposta": madde(uid, e(17), "MESAM'a 2 e-posta gönderildi (konular: A; B)", "eposta"),
        "eposta2": madde(uid, e(23), "IMRO'ya 'Dağıtım' konulu e-posta gönderildi", "eposta"),
        "uygulama": madde(uid, e(17), "Medusa'da arama hızlandırıldı", "medusa"),
        "elle": madde(uid, e(18), "Köprü Film lisans talebi tamamlandı"),
        "elle2": madde(uid, e(23), "Katalog düzeltildi"),
        "toplanti": madde(uid, e(23), "'Geliştirme' toplantısı yapıldı", "takvim"),
    }


def yonetici(bolumler, **ek) -> str:
    return json.dumps({"bolumler": bolumler, "tamamlanan": ek.get("tamamlanan", []), "devam_eden": ek.get("devam_eden", []),
                       "toplanti": ek.get("toplanti", [])}, ensure_ascii=False)


def uret(c, bicim="patron", tur="aylik", donem="2026-09"):
    return c.post("/api/ozet", json={"tur": tur, "donem": donem, "bicim": bicim})


def pdf_metni(icerik: bytes) -> tuple[int, str]:
    okuyucu = pypdf.PdfReader(io.BytesIO(icerik))
    return len(okuyucu.pages), "\n".join(s.extract_text() for s in okuyucu.pages)


# ---------------------------------------------------------------- 1) adlar

def test_baslik_ve_bicim_adlari():
    assert servisler.BICIM_ADLARI == {"patron": "Yönetici Özeti", "basari": "Performans Özeti"}
    assert servisler.ozet_basligi("aylik", "patron", e(1)) == "*Aylık Yönetici Özeti – Eylül 2026*"
    assert servisler.ozet_basligi("yillik", "patron", date(2026, 1, 1)) == "*Yıllık Yönetici Özeti – 2026*"
    assert servisler.ozet_basligi("aylik", "basari", e(1)) == "Aylık Performans Özeti – Eylül 2026"
    assert api.ozet_dosya_adi("aylik", "basari", e(1), "Ufuk Çetinkaya") == "Performans-Ozeti-Eylul-2026-Ufuk-Cetinkaya.pdf"
    assert api.ozet_dosya_adi("aylik", "patron", e(1), "Ufuk Çetinkaya") == "Yonetici-Ozeti-Eylul-2026-Ufuk-Cetinkaya.pdf"
    assert api.ozet_dosya_adi("yillik", "patron", date(2026, 1, 1), "Ufuk Çetinkaya") == "Yonetici-Ozeti-2026-Ufuk-Cetinkaya.pdf"


def test_arayuz_metinleri_yeni_adlarla():
    kullanici_olustur()
    c = istemci()
    gecmis = c.get("/gecmis").text
    for parca in ("Yönetici Özeti", "Performans Özeti", 'data-bicim="patron"', 'data-bicim="basari"', 'id="ayOzet"',
                  "Ayın özeti", "GÜN GÜN", "İŞ NEREDEN GELDİ", "RAPOR DÜZENİ", "RAPORLANAN İŞ", "KURUMLARLA YAZIŞMA",
                  "İŞ DURUMU", "EN YOĞUN GÜN", "EN ÇOK YAZIŞILAN", 'id="ustPdf"'):
        assert parca in gecmis, parca
    for eski in ("Patrona özet", "Başarı dökümü", "Döküm üret", "Başarı<span"):
        assert eski not in gecmis, eski
    bugun = c.get("/").text
    assert "Yönetici Özeti hazırlanabilir" in bugun and "özetin hazırlanabilir" not in bugun


def test_ic_anahtarlar_ayni_eski_kayitlar_aciliyor():
    """A1v2 kaydı (bicim 'patron', madde düz dize, madde_idleri yok) veri göçü olmadan açılır, PDF'i ve düzenlemesi çalışır."""
    uid = kullanici_olustur()
    rapor(uid, e(17))
    eski_yapi = {"surum": 2, "bicim": "patron", "baslik": "*Aylık Özet – Eylül 2026*",
                 "bolumler": [{"ad": "Edisyon Çalışmaları", "maddeler": ["MESAM ile yazışıldı.", "Katalog düzeltildi."]}],
                 "tamamlanan": ["Köprü Film"], "devam_eden": []}
    with OturumYapici() as db:
        db.add(Rapor(user_id=uid, tarih=e(1), tur="aylik", bicim="patron", yapi=eski_yapi,
                     metin="*Aylık Özet – Eylül 2026*\n\n*Edisyon Çalışmaları:*\n• MESAM ile yazışıldı."))
        db.commit()
    c = istemci()
    d = c.get("/api/ozet?tur=aylik&donem=2026-09").json()
    assert set(d["ozetler"]) == {"patron", "basari"} and d["ozetler"]["basari"] is None
    assert d["ozetler"]["patron"]["yapi"]["bolumler"][0]["maddeler"] == ["MESAM ile yazışıldı.", "Katalog düzeltildi."]
    assert d["basliklar"]["patron"] == "*Aylık Yönetici Özeti – Eylül 2026*"
    r = c.get("/api/ozet/pdf?tur=aylik&donem=2026-09&bicim=patron")
    sayfa, metin = pdf_metni(r.content)
    assert r.status_code == 200 and sayfa == 1 and "MESAM ile yazışıldı." in metin and "AYLIK YÖNETİCİ ÖZETİ" in metin
    # düzenleme eski düz dizeyi {vurgu, metin}'e çevirir; başlık yeni adla
    r = c.put("/api/ozet", json={"tur": "aylik", "donem": "2026-09", "bicim": "patron", "yapi": eski_yapi}).json()
    assert r["yapi"]["bolumler"][0]["maddeler"][0] == {"vurgu": "", "metin": "MESAM ile yazışıldı."}
    assert r["metin"].startswith("*Aylık Yönetici Özeti – Eylül 2026*\n\n*Edisyon Çalışmaları:*\n• MESAM ile yazışıldı.")


# ---------------------------------------------------------------- 2) istatistik: Ayın özeti alanları

def test_gunluk_dagilim_ve_isaretler():
    uid = kullanici_olustur()
    veri(uid)
    ist = istemci().get("/api/ozet?tur=aylik&donem=2026-09").json()["istatistik"]
    g = {x["tarih"]: x for x in ist["gunluk_dagilim"]}
    assert len(ist["gunluk_dagilim"]) == 30 and "aylik_dagilim" not in ist
    assert g["2026-09-23"] == {"tarih": "2026-09-23", "madde": 5, "rapor_var": True, "is_gunu": True,
                               "kullanim_oncesi": False, "gelecek": False}
    assert g["2026-09-25"]["is_gunu"] and not g["2026-09-25"]["rapor_var"] and g["2026-09-25"]["madde"] == 0  # pembe
    assert not g["2026-09-26"]["is_gunu"] and not g["2026-09-27"]["is_gunu"]  # hafta sonu
    assert g["2026-09-16"]["kullanim_oncesi"] and not g["2026-09-17"]["kullanim_oncesi"]  # hesap 17 Eylül'de açıldı
    assert g["2026-09-30"]["gelecek"] and not g["2026-09-29"]["gelecek"]
    assert ist["en_yogun_gun"] == {"tarih": "2026-09-23", "madde": 5}
    assert ist["ortalama"] == 2.8 and ist["toplam_madde"] == 11 == sum(x["madde"] for x in ist["gunluk_dagilim"])
    assert ist["otomatik_gun_sayisi"] == 1
    assert ist["son_tamamlanan"] == {"metin": "Köprü Film lisans talebi tamamlandı", "tarih": "2026-09-18"}
    assert ist["yazisma"] == 3 and ist["kurumlar"][:2] == [{"ad": "MESAM", "sayi": 2}, {"ad": "IMRO", "sayi": 1}]
    # şubat: gün sayısı ayın uzunluğu; bütün günler kullanım öncesi
    subat = istemci().get("/api/ozet?tur=aylik&donem=2026-02").json()["istatistik"]
    assert len(subat["gunluk_dagilim"]) == 28 and all(x["kullanim_oncesi"] for x in subat["gunluk_dagilim"])
    assert subat["en_yogun_gun"] is None and subat["ortalama"] is None and subat["son_tamamlanan"] is None


def test_yillik_aylik_dagilim():
    uid = kullanici_olustur(olusturma=datetime(2026, 8, 10, 6, tzinfo=timezone.utc))
    veri(uid)
    rapor(uid, date(2026, 8, 12), maddeler_metni(4))
    ist = istemci().get("/api/ozet?tur=yillik&donem=2026").json()["istatistik"]
    aylar = ist["aylik_dagilim"]
    assert len(aylar) == 12 and [a["ay"] for a in aylar][:2] == ["2026-01", "2026-02"] and "gunluk_dagilim" not in ist
    assert aylar[8] == {"ay": "2026-09", "madde": 11, "rapor_gunu": 4, "is_gunu": 21, "kullanim_oncesi": False, "gelecek": False}
    assert aylar[7]["madde"] == 4 and aylar[7]["rapor_gunu"] == 1 and not aylar[7]["kullanim_oncesi"]
    assert aylar[6]["kullanim_oncesi"] and aylar[6]["madde"] == 0
    assert aylar[9]["gelecek"] and aylar[11]["gelecek"] and aylar[9]["is_gunu"] == 0
    assert ist["en_yogun_ay"] == {"ay": "2026-09", "madde": 11} and ist["en_yogun_gun"]["tarih"] == "2026-09-23"


# ---------------------------------------------------------------- 3) Yönetici Özeti JSON doğrulaması

def test_sinir_asimlari_ve_kirpma():
    uzun = " ".join(f"k{i}" for i in range(40))
    ham = {"bolumler": [{"ad": "A", "maddeler": [{"vurgu": "bir iki üç dört beş altı yedi", "metin": "devam etti."}]
                         + [{"vurgu": "V", "metin": f"madde {i}"} for i in range(6)]},
                        {"ad": "B", "maddeler": [{"vurgu": "Kısa", "metin": uzun}]}], "toplanti": []}
    asimlar = servisler.yonetici_sinir_asimlari(ham)
    assert any("7 madde" in a for a in asimlar) and any("vurgu" in a for a in asimlar) and any("41 kelime" in a for a in asimlar)
    y = servisler.ozet_yapisini_dogrula("patron", ham)
    a, b = y["bolumler"]
    assert len(a["maddeler"]) == 5  # en çok 5 madde
    assert a["maddeler"][0] == {"vurgu": "bir iki üç dört beş", "metin": "altı yedi devam etti."}  # vurgu en çok 5 kelime
    m = b["maddeler"][0]
    kelime = len(m["vurgu"].split()) + len(m["metin"].split())
    assert kelime == 25 and m["metin"].endswith("…") and m["metin"].startswith("k0 k1")  # 25 kelimede "…"
    assert servisler.yonetici_sinir_asimlari({"bolumler": [{"ad": "A", "maddeler": [{"vurgu": "Kısa", "metin": "iş."}]}]}) == []


def test_sinir_asilirsa_bir_kez_yeniden_istenir(sahte_claude):
    uid = kullanici_olustur()
    ids = veri(uid)
    fazla = [{"vurgu": "V", "metin": f"madde {i} yürütüldü."} for i in range(8)]
    duzgun = [{"vurgu": "Lisanslama modülü", "metin": "devreye alındı."}]
    sahte_claude.yanitlar = [yonetici([{"ad": "Uygulama", "maddeler": fazla}]),
                             yonetici([{"ad": "Uygulama", "maddeler": duzgun, "madde_idleri": [ids["uygulama"], 999]}])]
    r = uret(istemci()).json()
    assert len(sahte_claude.istekler) == 2
    ikinci = sahte_claude.istekler[1]["messages"]
    assert [m["role"] for m in ikinci] == ["user", "assistant", "user"] and "Sınırlar aşıldı" in ikinci[2]["content"]
    assert "'Uygulama' bölümünde 8 madde" in ikinci[2]["content"]
    b = r["ozet"]["yapi"]["bolumler"][0]
    assert b["maddeler"] == duzgun and b["madde_idleri"] == [ids["uygulama"]] and b["madde_sayisi"] == 1  # bilinmeyen id atılır
    with OturumYapici() as db:  # iki istem tek Claude hakkı
        from veritabani import ClaudeKullanim
        assert db.query(ClaudeKullanim).filter(ClaudeKullanim.user_id == uid).one().cagri == 1


def test_yine_asilirsa_kirpilir_ve_ucuncu_istem_yok(sahte_claude):
    uid = kullanici_olustur()
    veri(uid)
    fazla = [{"vurgu": "V", "metin": f"madde {i}"} for i in range(9)]
    sahte_claude.yanitlar = [yonetici([{"ad": "A", "maddeler": fazla}]), yonetici([{"ad": "A", "maddeler": fazla[:7]}])]
    r = uret(istemci())
    assert r.status_code == 200 and len(sahte_claude.istekler) == 2
    assert [m["metin"] for m in r.json()["ozet"]["yapi"]["bolumler"][0]["maddeler"]] == [f"madde {i}" for i in range(5)]


def test_sinira_uyan_yanitta_yeniden_istem_yok(sahte_claude):
    uid = kullanici_olustur()
    veri(uid)
    sahte_claude.yanitlar = [yonetici([{"ad": "A", "maddeler": [{"vurgu": "Kısa", "metin": "iş yapıldı."}]}])]
    assert uret(istemci()).status_code == 200 and len(sahte_claude.istekler) == 1


def test_prompt_sozlesmesi():
    s = servisler.ozet_sistemi("aylik", "patron")
    for parca in ('"vurgu"', '"metin"', '"toplanti"', '"madde_idleri"', "2-5 madde", "en çok 5 kelime", "25 kelime",
                  "Paragraf yok", "en fazla 3 örnek ad", "birleşir", "YALNIZ"):
        assert parca in s, parca
    assert "Performans Özeti" in servisler.ozet_sistemi("aylik", "basari")


# ---------------------------------------------------------------- 4) WhatsApp metni

def test_whatsapp_metni_bicimi(sahte_claude):
    uid = kullanici_olustur(ad_eslemeleri=ESLEME)
    ids = veri(uid)
    sahte_claude.yanitlar = [yonetici(
        [{"ad": "Edisyon uygulaması", "maddeler": [{"vurgu": "Medusa arama", "metin": "hızlandırıldı."}],
          "madde_idleri": [ids["uygulama"]]},
         {"ad": "Meslek birlikleri", "maddeler": [{"vurgu": "", "metin": "MESAM ve IMRO ile yazışıldı."}]}],
        toplanti=[{"vurgu": "Geliştirme toplantısı", "metin": "yapıldı."}], tamamlanan=["Köprü Film lisans talebi"],
        devam_eden=["MSG itirazı — yanıt bekleniyor"])]
    r = uret(istemci()).json()
    assert r["metin"] == (
        "*Aylık Yönetici Özeti – Eylül 2026*\n\n"
        "*Edisyon uygulaması:*\n• *Edisyon uygulaması arama* hızlandırıldı.\n\n"
        "*Meslek birlikleri:*\n• MESAM ve IMRO ile yazışıldı.\n\n"
        "*Toplantılar:*\n• *Geliştirme toplantısı* yapıldı.\n\n"
        "*Tamamlananlar:*\n• Köprü Film lisans talebi\n\n"
        "*Devam Eden İşler:*\n• MSG itirazı — yanıt bekleniyor")


# ---------------------------------------------------------------- 5) PDF

def yonetici_yapisi(adetler=(5, 4, 2)) -> dict:
    """Normal içerik: referanstaki gibi (5 + 3 + toplantı) üstüne bir bölüm daha; 3 bölüm 11 madde + 1 toplantı."""
    return {"bicim": "patron", "bolumler": [
        {"ad": ad, "maddeler": [{"vurgu": "Lisanslama modülü", "metin": f"devreye alındı ve iki kullanıcıyla teste açıldı {i}."}
                                for i in range(n)], "madde_sayisi": 10, "tur": tur}
        for (ad, tur), n in zip((("Edisyon uygulaması", "uygulama"), ("Meslek birlikleri ve eser talepleri", "yazisma"),
                                 ("Diğer işler", "diger")), adetler)],
        "toplanti": [{"vurgu": "Geliştirme", "metin": "toplantısı yapıldı."}], "tamamlanan": ["Köprü Film lisans talebi"],
        "devam_eden": [], "sayilar": {"raporlu_is_gunu": 9, "is_gunu": 10, "toplam_madde": 113, "tamamlanan": 1, "acik": 0,
                                      "yazisma": 12, "kurum_adlari": ["MESAM", "IMRO"], "toplanti": 1}}


def test_yonetici_pdf_bant_sonrasi_bosluk_ve_tek_sayfa():
    pdf_uret.yazilari_kaydet()
    bloklar = pdf_uret.patron_bloklari("aylik", "Eylül 2026", yonetici_yapisi(), "Ufuk Çetinkaya · Danışman", BUGUN)
    sayfalar = pdf_uret.dizil(bloklar, 0)
    assert len(sayfalar) == 1  # normal içerik (3 bölüm, 11 madde + toplantı + durum) tek sayfa
    (bant_y, bant), (kutu_y, _) = sayfalar[0][0], sayfalar[0][1]
    bosluk_pt = (kutu_y - (bant_y + bant.yukseklik)) * pdf_uret.PX
    assert bosluk_pt >= 20 and bosluk_pt == 24  # banttan sonra 24 pt; ilk gösterge kutusu bant altından ≥ 20 pt
    # çizilen PDF'te de: "RAPOR DÜZENİ" etiketinin taban çizgisi bant altından aşağıda
    konumlar = {}

    def yakala(metin, cm, tm, *_):
        if metin.strip():
            konumlar.setdefault(metin.strip(), tm[5])
    icerik = pdf_uret.ozet_pdf("aylik", "patron", "Eylül 2026", yonetici_yapisi(), "", {}, "Ufuk Çetinkaya", "Danışman", BUGUN)
    sayfa = pypdf.PdfReader(io.BytesIO(icerik)).pages[0]
    sayfa.extract_text(visitor_text=yakala)
    bant_alti_pt = pdf_uret.A4[1] - bant.yukseklik * pdf_uret.PX
    assert bant_alti_pt - konumlar["RAPOR DÜZENİ"] >= 20


def test_yonetici_pdf_icerigi_turkce_ve_tasarsa_ikinci_sayfa():
    icerik = pdf_uret.ozet_pdf("aylik", "patron", "Eylül 2026", yonetici_yapisi(), "", {}, "Ufuk Çetinkaya", "Danışman", BUGUN)
    sayfa, metin = pdf_metni(icerik)
    assert sayfa == 1
    for parca in ("AYLIK YÖNETİCİ ÖZETİ", "29 Eylül 2026", "Ufuk Çetinkaya · Danışman", "RAPOR DÜZENİ", "9 / 10",
                  "YAZIŞMA", "MESAM ve IMRO", "TAMAMLANAN", "iş · 0 açık", "Edisyon uygulaması", "10 madde",
                  "Lisanslama modülü", "Toplantılar", "DEVAM EDEN", "Açık iş bulunmuyor.", "Köprü Film lisans talebi",
                  "Eylül 2026 Aylık Yönetici Özeti", "1 / 1", "Meslek birlikleri ve eser talepleri"):
        assert parca in metin, parca
    kalabalik = yonetici_yapisi((5, 5, 5))
    kalabalik["bolumler"] = kalabalik["bolumler"] * 2
    assert pdf_metni(pdf_uret.ozet_pdf("aylik", "patron", "Eylül 2026", kalabalik, "", {}, "Ad"))[0] == 2


def test_yonetici_pdf_uctan_uca_ad_eslemesi_ve_bolum_renkleri(sahte_claude):
    uid = kullanici_olustur(ad_eslemeleri=ESLEME)
    ids = veri(uid)
    sahte_claude.yanitlar = [yonetici(
        [{"ad": "Medusa çalışmaları", "maddeler": [{"vurgu": "Medusa'da arama", "metin": "hızlandırıldı."}],
          "madde_idleri": [ids["uygulama"]]},
         {"ad": "Kurum yazışmaları", "maddeler": [{"vurgu": "MESAM'a", "metin": "e-posta gönderildi."}],
          "madde_idleri": [ids["eposta"], ids["eposta2"]]},
         {"ad": "Katalog", "maddeler": [{"vurgu": "Katalog", "metin": "düzeltildi."}], "madde_idleri": [ids["elle2"]]}],
        toplanti=[{"vurgu": "Geliştirme", "metin": "toplantısı yapıldı."}], tamamlanan=["Köprü Film lisans talebi"])]
    c = istemci()
    uret(c)
    r = c.get("/api/ozet/pdf?tur=aylik&donem=2026-09&bicim=patron")
    assert 'filename="Yonetici-Ozeti-Eylul-2026-Ufuk-Cetinkaya.pdf"' in r.headers["content-disposition"]
    sayfa, metin = pdf_metni(r.content)
    assert sayfa == 1 and "Medusa" not in metin and "Edisyon uygulaması çalışmaları" in metin
    assert "2 madde" in metin and "AYLIK YÖNETİCİ ÖZETİ" in metin and "3" in metin
    with OturumYapici() as db:
        k = db.get(Kullanici, uid)
        yapi = servisler.yapi_esle(db.query(Rapor).filter(Rapor.bicim == "patron").one().yapi, ESLEME)
        api.renk_gruplarini_isle(db, k, yapi, ESLEME)
    assert [b["tur"] for b in yapi["bolumler"]] == ["uygulama", "yazisma", "diger"]


def test_performans_pdf_adi_ve_dagilim_renkleri(sahte_claude):
    uid = kullanici_olustur(ad_eslemeleri=ESLEME)
    ids = veri(uid)
    sahte_claude.yanitlar = [json.dumps({"one_cikanlar": [{"baslik": "Sonuç", "aciklama": "Ayrıntı."}], "alanlar": [
        {"ad": "Uygulama", "temalar": [{"ad": "T1", "ozet": "", "etiketler": [], "madde_idleri": [ids["uygulama"]]}]},
        {"ad": "Birlikler", "temalar": [{"ad": "T2", "ozet": "", "etiketler": [], "madde_idleri": [ids["eposta"], ids["eposta2"]]}]},
        {"ad": "Toplantı", "temalar": [{"ad": "T3", "ozet": "", "etiketler": [], "madde_idleri": [ids["toplanti"]]}]}],
        "tamamlanan": [], "devam_eden": [], "surekli": ""})]
    c = istemci()
    uret(c, "basari")
    r = c.get("/api/ozet/pdf?tur=aylik&donem=2026-09&bicim=basari")
    assert 'filename="Performans-Ozeti-Eylul-2026-Ufuk-Cetinkaya.pdf"' in r.headers["content-disposition"]
    _, metin = pdf_metni(r.content)
    assert "AYLIK PERFORMANS ÖZETİ" in metin and "Eylül 2026 Aylık Performans Özeti" in metin
    assert "BAŞARI" not in metin and "Başarı" not in metin and "Medusa" not in metin
    with OturumYapici() as db:
        yapi = db.query(Rapor).filter(Rapor.bicim == "basari").one().yapi
        api.renk_gruplarini_isle(db, db.get(Kullanici, uid), yapi, ESLEME)
    # ekrandaki donut'la aynı renk grupları: uygulama mavi, e-posta kırmızı, toplantı/not/ses yeşil, elle koyu
    assert [a["grup"] for a in yapi["alanlar"]] == ["uygulama", "eposta", "yesil", "elle"]
    assert [pdf_uret.GRUP_RENKLERI[a["grup"]] for a in yapi["alanlar"]] == ["#2d6cdf", "#d9433d", "#22b866", "#12151a"]


def test_bolum_turu_adi_ve_kaynaktan():
    assert servisler.yonetici_bolum_turu("Edisyon uygulaması") == "uygulama"
    assert servisler.yonetici_bolum_turu("Meslek birlikleri ve eser talepleri") == "yazisma"
    assert servisler.yonetici_bolum_turu("Toplantılar") == "toplanti"
    assert servisler.yonetici_bolum_turu("MEDUSA ÇALIŞMALARI", [], "MEDUSA Çalışmaları") == "uygulama"
    assert servisler.yonetici_bolum_turu("Genel", ["eposta", "outlook", "elle"]) == "yazisma"
    assert servisler.yonetici_bolum_turu("Genel", ["elle", "drive"]) == "diger"
