"""A1 (A1v2 sözleşmesine uyarlandı): aylık/yıllık döküm (patron özeti + başarı dökümü, istatistik), e-posta kuralları ayrı bölüm, alan adı eki.
sqlite; Claude sahte (conftest.sahte_claude); "bugün" 16 Eylül 2026."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError

import api
import app as uygulama
import guvenlik
import servisler
import veritabani
from veritabani import ClaudeKullanim, Kullanici, KullaniciAyari, Madde, OturumYapici, Rapor, Temel, motor

SIFRE = "dogru-sifre-123"
BUGUN = date(2026, 9, 16)  # Çarşamba
A = date(2026, 8, 1)  # Ağustos 2026: 1'i Cumartesi, 21 iş günü (Pzt–Cum)


@pytest.fixture(autouse=True)
def ortam(monkeypatch):
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    api.onbellegi_temizle()
    monkeypatch.setattr(api, "bugun", lambda: BUGUN)
    for anahtar in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET"):
        monkeypatch.setenv(anahtar, "")
    yield


def kullanici_olustur(eposta="ufuk@ilsvision.com", ad="Ufuk Çetinkaya", olusturma=datetime(2026, 7, 1, tzinfo=timezone.utc),
                      **ayar) -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad=ad, sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol="uye", aktif=True,
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


def rapor(uid, gun, metin, tur="gunluk", gonderim="elle", bicim=None):
    with OturumYapici() as db:
        db.add(Rapor(user_id=uid, tarih=gun, metin=metin, tur=tur, gonderim=gonderim, bicim=bicim,
                     hafta_baslangic=gun if tur == "haftalik" else None))
        db.commit()


def madde(uid, gun, metin, kaynak="elle", tur=None, **alan):
    tur = tur or ("bugun" if kaynak in ("elle", "ses", "not") else "bulunan")
    with OturumYapici() as db:
        db.add(Madde(user_id=uid, tur=tur, tarih=gun, kaynak=kaynak, metin=metin, tikli=alan.pop("tikli", True), **alan))
        db.commit()


def devam(uid, metin, asama, olusturma, onemli=False):
    with OturumYapici() as db:
        db.add(Madde(user_id=uid, tur="devam", metin=metin, asama=asama, tikli=True, onemli=onemli,
                     olusturma=datetime.combine(olusturma, datetime.min.time(), servisler.ISTANBUL)))
        db.commit()


def g(n: int) -> date:
    return date(2026, 8, n)


def agustos_verisi(uid):
    """Ağustos: 3 iş günü + 1 Cumartesi raporu; kapsama 3/21. Dönem dışı (31 Temmuz, 1 Eylül) veri de var."""
    rapor(uid, g(3), "*Günlük Rapor – 03.08.2026*\n\n*Yazışmalar:*\n• a\n• b\n\n*Genel İşler:*\n• c\n\n*Yarın:*\n• plan")
    rapor(uid, g(4), "*Günlük Rapor – 04.08.2026*\n\n*Yazışmalar:*\n• d\n\n*Medusa Çalışmaları:*\n• e\n• f")
    rapor(uid, g(5), "*Günlük Rapor – 05.08.2026*\n\n*Genel İşler:*\n• g", gonderim="otomatik")
    rapor(uid, g(8), "*Günlük Rapor – 08.08.2026*\n\n*Yapılanlar*\n• h")  # Cumartesi, başlıksız biçim
    rapor(uid, date(2026, 7, 31), "*Günlük Rapor – 31.07.2026*\n\n*Yazışmalar:*\n• TEMMUZ-MADDESI")
    rapor(uid, date(2026, 9, 1), "*Günlük Rapor – 01.09.2026*\n\n*Yazışmalar:*\n• EYLUL-MADDESI")
    madde(uid, g(3), "Sözleşme taslağı gönderildi")
    madde(uid, g(3), "Katalog düzeltmesi tamamlandı")
    madde(uid, g(3), "Sesle eklenen iş", "ses")
    madde(uid, g(3), "E-postayla gelen not", "not")
    madde(uid, g(3), "MESAM'a 'A' konulu e-posta gönderildi", "eposta")
    madde(uid, g(3), "MESAM'a 2 e-posta gönderildi (konular: B; C)", "outlook")
    madde(uid, g(3), "Ufuk Çetinkaya'ya 'T' konulu e-posta gönderildi", "eposta", onemli=True)
    madde(uid, g(4), "MSG'ye 'X' konulu e-posta gönderildi", "eposta")
    madde(uid, g(4), "MSG'ye 'Y' konulu e-posta gönderildi (ayrıca IMRO)", "outlook")
    madde(uid, g(4), "IMRO'ya 'Z' konulu e-posta gönderildi", "eposta")
    madde(uid, g(4), "Coverz'e konusuz bir e-posta gönderildi", "eposta")
    madde(uid, g(4), "ornek.com.tr'ye 'K' konulu e-posta gönderildi", "eposta")
    madde(uid, g(4), "Bir kişiye 'F' konulu e-posta gönderildi", "eposta")
    madde(uid, g(4), "'Görüşme' toplantısı yapıldı", "takvim")
    madde(uid, g(4), "'Bütçe' tablosu üzerinde çalışıldı", "drive")
    madde(uid, g(4), "'Liste' tablosu üzerinde çalışıldı", "onedrive")
    madde(uid, g(4), "Arama hızlandırıldı", "medusa")
    madde(uid, g(4), "GIZLI'ye 'G' konulu e-posta gönderildi", "eposta", gizli=True)
    madde(uid, g(4), "TIKSIZ'e 'H' konulu e-posta gönderildi", "eposta", tikli=False)
    madde(uid, g(6), "Rapor gönderilmeyen günün işi")  # o gün rapor yok: sayılmaz
    madde(uid, g(6), "Eser bildirimi tamamlandı")  # tamamlanan her gün sayılır
    madde(uid, date(2026, 7, 31), "Temmuz işi tamamlandı")
    devam(uid, "Katalog teslimi", "yanıt bekleniyor", g(10), onemli=True)
    devam(uid, "Eylülde açılan iş", "", date(2026, 9, 5))


# ---------------------------------------------------------------- C) istatistik

def test_aylik_istatistik_her_alan():
    uid = kullanici_olustur()
    agustos_verisi(uid)
    ist = istemci().get("/api/ozet?tur=aylik&donem=2026-08").json()["istatistik"]
    assert (ist["baslangic"], ist["bitis"]) == ("2026-08-01", "2026-08-31")
    assert (ist["rapor_gunu"], ist["elle"], ist["otomatik"]) == (4, 3, 1)
    assert (ist["is_gunu"], ist["kapsama"]) == (21, 14)  # 3 iş günü raporlu / 21 (Cumartesi raporu kapsamaya girmez)
    assert ist["toplam_madde"] == 8  # 3 + 3 + 1 + 1; Yarın planı sayılmaz
    assert {k["anahtar"]: k["sayi"] for k in ist["kaynaklar"]} == {
        "elle": 2, "ses": 1, "not": 1, "eposta": 9, "toplanti": 1, "dosya": 2, "commit": 1}
    assert [k["ad"] for k in ist["kaynaklar"]] == ["E-posta", "Uygulama çalışmaları", "Elle eklenen", "Sesle eklenen",
                                                   "Notlar", "Dosyalar", "Toplantılar"]
    assert ist["kategoriler"] == [{"ad": "Yazışmalar", "sayi": 3}, {"ad": "Genel İşler", "sayi": 2},
                                  {"ad": "Medusa Çalışmaları", "sayi": 2}]
    assert ist["kurumlar"] == [{"ad": "MESAM", "sayi": 3}, {"ad": "MSG", "sayi": 2}, {"ad": "Coverz", "sayi": 1},
                               {"ad": "IMRO", "sayi": 1}, {"ad": "Ufuk Çetinkaya", "sayi": 1}]
    assert ist["tamamlanan"] == [{"metin": "Katalog düzeltmesi tamamlandı", "tarih": "2026-08-03"},
                                 {"metin": "Eser bildirimi tamamlandı", "tarih": "2026-08-06"}]
    assert ist["acik"] == [{"metin": "Katalog teslimi", "asama": "yanıt bekleniyor", "gun": 21}]
    assert ist["onemli"] == 2  # e-posta maddesi + dönem içinde açılan önemli devam eden iş


def test_gmail_outlook_ve_drive_onedrive_birlesik_sayilir():
    uid = kullanici_olustur()
    rapor(uid, g(3), "• x")
    for kaynak in ("eposta", "outlook", "outlook", "drive", "onedrive", "onedrive"):
        madde(uid, g(3), f"MESAM'a '{kaynak}' konulu e-posta gönderildi", kaynak)
    ist = istemci().get("/api/ozet?tur=aylik&donem=2026-08").json()["istatistik"]
    sayi = {k["anahtar"]: k["sayi"] for k in ist["kaynaklar"]}
    assert (sayi["eposta"], sayi["dosya"]) == (3, 3)
    assert ist["kurumlar"] == [{"ad": "MESAM", "sayi": 3}]  # yalnız e-posta maddelerinden (Gmail + Outlook)


def test_is_gunu_hatirlatma_gunlerine_gore_ve_bu_ay_bugune_kadar():
    uid = kullanici_olustur(hatirlatma_gunler="1,3")  # Pazartesi, Çarşamba
    rapor(uid, date(2026, 9, 2), "• a")
    rapor(uid, date(2026, 9, 3), "• b")  # Perşembe: iş günü değil
    ist = istemci().get("/api/ozet?tur=aylik&donem=2026-09").json()["istatistik"]
    assert ist["bitis"] == "2026-09-16"
    assert (ist["rapor_gunu"], ist["is_gunu"], ist["kapsama"]) == (2, 5, 20)  # 2, 7, 9, 14, 16 Eylül


def test_is_gunu_hesap_acilisindan_itibaren():
    kullanici_olustur(olusturma=datetime(2026, 9, 14, 9, tzinfo=timezone.utc))
    ist = istemci().get("/api/ozet?tur=aylik&donem=2026-09").json()["istatistik"]
    assert (ist["rapor_gunu"], ist["is_gunu"], ist["kapsama"]) == (0, 3, 0)  # 14, 15, 16 Eylül


def test_yillik_istatistik_ve_bos_donem():
    uid = kullanici_olustur()
    agustos_verisi(uid)
    c = istemci()
    ist = c.get("/api/ozet?tur=yillik&donem=2026").json()["istatistik"]
    assert (ist["baslangic"], ist["bitis"], ist["rapor_gunu"]) == ("2026-01-01", "2026-09-16", 6)
    bos = c.get("/api/ozet?tur=aylik&donem=2026-06").json()["istatistik"]
    assert (bos["rapor_gunu"], bos["is_gunu"], bos["kapsama"], bos["toplam_madde"]) == (0, 0, None, 0)


@pytest.mark.parametrize("tur, donem", [("aylik", "2026-9"), ("aylik", "2026-13"), ("aylik", "2026"), ("yillik", "26"),
                                        ("haftalik", "2026-09"), ("aylik", "2026-10"), ("yillik", "2027")])
def test_donem_dogrulama(tur, donem):
    kullanici_olustur()
    assert istemci().get(f"/api/ozet?tur={tur}&donem={donem}").status_code == 422


def uzun_rapor(n: int) -> str:
    return "*Günlük Rapor*\n\n*Yazışmalar:*\n" + "\n".join(f"• madde-{i:02d} " + "x" * 40 for i in range(n))


# ---------------------------------------------------------------- D) Claude girdisi (A1v2: yapılandırılmış)

def patron_json(*maddeler, bolum="Genel"):
    return json.dumps({"bolumler": [{"ad": bolum, "maddeler": list(maddeler or ["özet"])}], "tamamlanan": [],
                       "devam_eden": []}, ensure_ascii=False)


def basari_json(idler=(), baslik="Sonuç alındı"):
    return json.dumps({"one_cikanlar": [{"baslik": baslik, "aciklama": "Ayrıntı."}],
                       "alanlar": [{"ad": "Alan", "temalar": [{"ad": "Tema", "ozet": "Özet.", "etiketler": [],
                                                                "madde_idleri": list(idler)}]}],
                       "tamamlanan": [], "devam_eden": [], "surekli": ""}, ensure_ascii=False)


def girdi_json(istek) -> dict:
    return json.loads(istek["messages"][0]["content"].split("Girdi:\n", 1)[1])


def test_aylik_girdi_yalniz_o_ayin_raporlu_gun_maddeleri(sahte_claude):
    uid = kullanici_olustur()
    agustos_verisi(uid)
    rapor(uid, g(10), "HAFTALIK-METIN", tur="haftalik")
    diger = kullanici_olustur("b@ornek.com", "B")
    rapor(diger, g(3), "• BASKASININ-RAPORU")
    madde(diger, g(3), "BASKASININ-MADDESI")
    sahte_claude.yanitlar = [patron_json()]
    r = istemci().post("/api/ozet", json={"tur": "aylik", "donem": "2026-08", "bicim": "patron"})
    assert r.status_code == 200 and r.json()["girdi"] == "gunluk"
    icerik = sahte_claude.istekler[0]["messages"][0]["content"]
    girdi = girdi_json(sahte_claude.istekler[0])
    metinler = [m["metin"] for m in girdi["maddeler"]]
    assert "Sözleşme taslağı gönderildi" in metinler and "Arama hızlandırıldı" in metinler
    for yok in ("Rapor gönderilmeyen günün işi", "Temmuz işi tamamlandı", "GIZLI'ye 'G' konulu e-posta gönderildi",
                "TIKSIZ'e 'H' konulu e-posta gönderildi"):
        assert yok not in metinler
    for yok in ("TEMMUZ-MADDESI", "EYLUL-MADDESI", "HAFTALIK-METIN", "BASKASININ"):
        assert yok not in icerik
    assert {m["kaynak"] for m in girdi["maddeler"]} == {"elle", "not", "eposta", "toplanti", "dosya", "uygulama"}
    assert all(set(m) == {"id", "tarih", "kategori", "kaynak", "metin"} for m in girdi["maddeler"])
    assert girdi["gunluk_raporlar"][0]["tarih"] == "2026-08-05"  # maddesi olmayan raporlu gün bağlam olarak
    assert girdi["devam_eden"] == [{"is": "Katalog teslimi", "asama": "yanıt bekleniyor", "gun": 21}]
    assert "- Rapor gönderilen gün: 4 (elle 3, otomatik 1)" in icerik and "kapsama: %14" in icerik
    assert "En çok yazışılan kurum/kişiler: MESAM (3), MSG (2)" in icerik
    istek = sahte_claude.istekler[0]
    assert (istek["model"], istek["max_tokens"], istek["thinking"]) == ("claude-sonnet-5", 4000, {"type": "disabled"})


def test_uzun_aylik_girdi_kisaltilir(sahte_claude, monkeypatch):
    monkeypatch.setattr(api, "OZET_METIN_SINIRI", 500)
    uid = kullanici_olustur()
    rapor(uid, g(3), uzun_rapor(20))
    rapor(uid, g(4), uzun_rapor(15))
    for gun, n in ((3, 20), (4, 15)):
        for i in range(n):
            madde(uid, g(gun), f"madde-{gun}-{i:02d}")
    sahte_claude.yanitlar = [patron_json()]
    r = istemci().post("/api/ozet", json={"tur": "aylik", "donem": "2026-08"})
    assert r.json()["girdi"] == "kisaltilmis"
    metinler = [m["metin"] for m in girdi_json(sahte_claude.istekler[0])["maddeler"]]
    assert len(metinler) == 24 and "madde-3-11" in metinler and "madde-3-12" not in metinler  # gün başına ilk 12


def test_uzun_aylik_girdide_haftalik_ozetler_kullanilir(sahte_claude, monkeypatch):
    monkeypatch.setattr(api, "OZET_METIN_SINIRI", 500)
    uid = kullanici_olustur()
    rapor(uid, g(3), uzun_rapor(20))
    for i in range(20):
        madde(uid, g(3), f"madde-{i:02d} " + "x" * 40)
    rapor(uid, date(2026, 7, 27), "HAFTA-27-TEMMUZ", tur="haftalik")  # 27 Temmuz – 2 Ağustos: aya taşan hafta
    rapor(uid, g(3), "HAFTA-3-AGUSTOS", tur="haftalik")
    rapor(uid, date(2026, 9, 7), "EYLUL-HAFTASI", tur="haftalik")
    sahte_claude.yanitlar = [patron_json()]
    assert istemci().post("/api/ozet", json={"tur": "aylik", "donem": "2026-08"}).json()["girdi"] == "haftalik"
    icerik = sahte_claude.istekler[0]["messages"][0]["content"]
    assert "HAFTA-27-TEMMUZ" in icerik and "HAFTA-3-AGUSTOS" in icerik and "EYLUL-HAFTASI" not in icerik


def test_aylik_rapor_yoksa_400_ve_kota_dusmez(sahte_claude):
    kullanici_olustur()
    r = istemci().post("/api/ozet", json={"tur": "aylik", "donem": "2026-08"})
    assert r.status_code == 400 and "günlük rapor yok" in r.json()["detail"]
    assert sahte_claude.istekler == []
    with OturumYapici() as db:
        assert db.scalar(select(func.count()).select_from(ClaudeKullanim)) == 0


def test_yillik_girdi_aylik_yapilardan_ve_ozetsiz_aylar(sahte_claude):
    uid = kullanici_olustur()
    yapi = {"bicim": "basari", "one_cikanlar": [{"baslik": "AGUSTOS-ONE", "aciklama": ""}],
            "alanlar": [{"ad": "Alan", "temalar": [{"ad": "AGUSTOS-TEMA", "ozet": "", "etiketler": [], "madde_idleri": [101, 102]}]}],
            "tamamlanan": [], "devam_eden": [], "surekli": ""}
    with OturumYapici() as db:
        db.add(Rapor(user_id=uid, tarih=g(1), metin="AGUSTOS-BASARI", tur="aylik", bicim="basari", yapi=yapi))
        db.commit()
    rapor(uid, g(1), "AGUSTOS-PATRON", tur="aylik", bicim="patron")
    rapor(uid, date(2026, 7, 1), "TEMMUZ-PATRON", tur="aylik", bicim="patron")
    rapor(uid, date(2026, 7, 14), "• temmuz günlüğü")
    madde(uid, date(2026, 7, 14), "temmuz maddesi")
    rapor(uid, date(2026, 3, 10), "*Günlük Rapor*\n\n*Yazışmalar:*\n• mart-1")
    madde(uid, date(2026, 3, 10), "mart-1")
    madde(uid, date(2026, 3, 10), "mart-1")  # tekrar örneğe girmez
    rapor(uid, date(2025, 12, 1), "ESKI-YIL", tur="aylik", bicim="basari")
    c = istemci()
    assert c.get("/api/ozet?tur=yillik&donem=2026").json()["ozetsiz_aylar"] == ["Mart 2026"]
    sahte_claude.yanitlar = [basari_json(["8-1", 999])]
    r = c.post("/api/ozet", json={"tur": "yillik", "donem": "2026", "bicim": "basari"}).json()
    assert r["ozetsiz_aylar"] == ["Mart 2026"] and r["girdi"] == "aylik"
    assert len(sahte_claude.istekler) == 1  # eksik aylar için aylık özet üretilmez
    aylar = {a["ay"]: a for a in girdi_json(sahte_claude.istekler[0])["aylar"]}
    assert list(aylar) == ["Mart 2026", "Temmuz 2026", "Ağustos 2026"]
    assert aylar["Ağustos 2026"]["alanlar"][0]["temalar"][0] == {
        "id": "8-1", "ad": "AGUSTOS-TEMA", "ozet": "", "etiketler": [], "madde_sayisi": 2}
    assert aylar["Temmuz 2026"]["ozet_metni"] == "TEMMUZ-PATRON" and [m["metin"] for m in aylar["Temmuz 2026"]["maddeler"]] == ["temmuz maddesi"]
    assert "- Rapor gönderilen gün: 1 (elle 1, otomatik 0)" in aylar["Mart 2026"]["istatistik"]
    assert [m["metin"] for m in aylar["Mart 2026"]["maddeler"]] == ["mart-1"]
    assert "ESKI-YIL" not in sahte_claude.istekler[0]["messages"][0]["content"]
    assert sahte_claude.istekler[0]["messages"][0]["content"].startswith("Başlık: Yıllık Performans Özeti – 2026")
    tema = r["ozet"]["yapi"]["alanlar"][0]["temalar"][0]
    assert (tema["madde_idleri"], tema["madde_sayisi"]) == ([101, 102], 2)  # tema anahtarı maddelerine açılır; 999 atılır
    diger = r["ozet"]["yapi"]["alanlar"][-1]
    assert diger["ad"] == "Diğer" and diger["madde_sayisi"] == 2  # temmuz ve mart örnekleri hiçbir temaya atanmadı


def test_yillik_hic_veri_yoksa_400():
    kullanici_olustur()
    assert istemci().post("/api/ozet", json={"tur": "yillik", "donem": "2025"}).status_code == 400


# ---------------------------------------------------------------- iki biçim: prompt ve başlık

def test_basliklar():
    assert servisler.ozet_basligi("aylik", "patron", date(2026, 9, 1)) == "*Aylık Yönetici Özeti – Eylül 2026*"
    assert servisler.ozet_basligi("yillik", "patron", date(2026, 1, 1)) == "*Yıllık Yönetici Özeti – 2026*"
    assert servisler.ozet_basligi("aylik", "basari", date(2026, 9, 1)) == "Aylık Performans Özeti – Eylül 2026"
    assert servisler.ozet_basligi("yillik", "basari", date(2026, 1, 1)) == "Yıllık Performans Özeti – 2026"


def test_patron_ve_basari_promptlari():
    patron = servisler.ozet_sistemi("aylik", "patron")
    assert "Yönetici Özeti" in patron and "2-5 madde" in patron and "25 kelime" in patron and "tek maddede" in patron
    assert '"bolumler"' in patron and '"devam_eden"' in patron and "Abartı" in patron and "YALNIZ" in patron
    basari = servisler.ozet_sistemi("yillik", "basari")
    for alan in ('"one_cikanlar"', '"alanlar"', '"temalar"', '"etiketler"', '"madde_idleri"', '"surekli"', "3-5", "2-6"):
        assert alan in basari, alan
    assert "WhatsApp" not in basari and "tema id'si" in basari
    for sistem in (patron, basari):
        assert servisler.TIRNAK_KURALI in sistem and "istatistikteki sayıları birebir" in sistem and "uydurma" in sistem


def test_iki_bicim_uctan_uca(sahte_claude):
    uid = kullanici_olustur()
    rapor(uid, g(3), "• iş")
    madde(uid, g(3), "İş yapıldı")
    c = istemci()
    sahte_claude.yanitlar = [patron_json("Bir iş yapıldı."), basari_json([], "Sonuç alındı")]
    p = c.post("/api/ozet", json={"tur": "aylik", "donem": "2026-08", "bicim": "patron"}).json()
    b = c.post("/api/ozet", json={"tur": "aylik", "donem": "2026-08", "bicim": "basari"}).json()
    assert p["metin"] == "*Aylık Yönetici Özeti – Ağustos 2026*\n\n*Genel:*\n• Bir iş yapıldı."
    assert b["metin"].startswith("Aylık Performans Özeti – Ağustos 2026\n\nÖne çıkanlar\n• Sonuç alındı. Ayrıntı.")
    s1, s2 = (i["system"] for i in sahte_claude.istekler)
    assert "Yönetici Özeti" in s1 and '"one_cikanlar"' in s2
    assert sahte_claude.istekler[1]["messages"][0]["content"].startswith("Başlık: Aylık Performans Özeti – Ağustos 2026")


# ---------------------------------------------------------------- upsert, düzenleme, kota

def ozet_satirlari(uid):
    with OturumYapici() as db:
        return [(r.tur, r.tarih, r.bicim, r.metin, r.istatistik is not None) for r in db.scalars(
            select(Rapor).where(Rapor.user_id == uid, Rapor.tur.in_(("aylik", "yillik"))).order_by(Rapor.id))]


def test_upsert_ayni_donem_bicim_tek_satir_farkli_bicim_iki_satir(sahte_claude):
    uid = kullanici_olustur()
    rapor(uid, g(3), "• iş")
    c = istemci()
    sahte_claude.yanitlar = [patron_json("ilk"), patron_json("ikinci"), basari_json(), patron_json("y")]
    for bicim in ("patron", "patron", "basari"):
        assert c.post("/api/ozet", json={"tur": "aylik", "donem": "2026-08", "bicim": bicim}).status_code == 200
    c.post("/api/ozet", json={"tur": "yillik", "donem": "2026", "bicim": "patron"})
    satirlar = ozet_satirlari(uid)
    assert [(s[0], s[1], s[2], s[4]) for s in satirlar] == [
        ("aylik", g(1), "patron", True), ("aylik", g(1), "basari", True), ("yillik", date(2026, 1, 1), "patron", True)]
    assert satirlar[0][3] == "*Aylık Yönetici Özeti – Ağustos 2026*\n\n*Genel:*\n• ikinci"
    ozetler = c.get("/api/ozet?tur=aylik&donem=2026-08").json()["ozetler"]
    assert ozetler["patron"]["metin"].endswith("ikinci") and ozetler["basari"]["bicim"] == "basari"
    assert ozetler["patron"]["istatistik"]["rapor_gunu"] == 1
    assert ozetler["patron"]["yapi"]["bolumler"] == [{"ad": "Genel", "maddeler": [{"vurgu": "", "metin": "ikinci"}],
                                                     "madde_idleri": [], "madde_sayisi": 0}]
    assert c.get("/api/raporlar?tur=aylik").json()["toplam"] == 2
    assert c.get("/api/raporlar?tur=gunluk").json()["toplam"] == 1


def test_duz_metin_duzenlemesi_kaydedilir_claude_cagrilmaz(sahte_claude):
    uid = kullanici_olustur()
    rapor(uid, g(3), "• iş")
    c = istemci()
    sahte_claude.yanitlar = [patron_json("ilk")]
    c.post("/api/ozet", json={"tur": "aylik", "donem": "2026-08"})
    r = c.put("/api/ozet", json={"tur": "aylik", "donem": "2026-08", "bicim": "patron", "metin": "  elle düzeltildi  "})
    assert r.status_code == 200 and r.json()["metin"] == "elle düzeltildi" and r.json()["yapi"] is None
    assert ozet_satirlari(uid) == [("aylik", g(1), "patron", "elle düzeltildi", True)]
    assert len(sahte_claude.istekler) == 1
    assert c.put("/api/ozet", json={"tur": "aylik", "donem": "2026-08", "bicim": "patron", "metin": " "}).status_code == 422


def test_kota_dolu_429_ve_basarili_cagri_bir_hak(sahte_claude):
    uid = kullanici_olustur()
    rapor(uid, g(3), "• iş")
    c = istemci()
    sahte_claude.yanitlar = [patron_json()]
    assert c.post("/api/ozet", json={"tur": "aylik", "donem": "2026-08"}).status_code == 200
    with OturumYapici() as db:
        satir = db.scalar(select(ClaudeKullanim).where(ClaudeKullanim.user_id == uid))
        assert (satir.tarih, satir.cagri) == (BUGUN, 1)
        satir.cagri = api.GUNLUK_CLAUDE_SINIRI
        db.commit()
    r = c.post("/api/ozet", json={"tur": "aylik", "donem": "2026-08", "bicim": "basari"})
    assert r.status_code == 429 and "Claude hakkın doldu" in r.json()["detail"]
    assert len(sahte_claude.istekler) == 1
    assert ozet_satirlari(uid)[0][3] == "*Aylık Yönetici Özeti – Ağustos 2026*\n\n*Genel:*\n• özet"


def test_claude_hatasinda_kayitli_ozet_bozulmaz(sahte_claude, monkeypatch):
    uid = kullanici_olustur()
    rapor(uid, g(3), "• iş")
    c = istemci()
    sahte_claude.yanitlar = [patron_json("sağlam"), servisler.ClaudeHatasi("API hatası (529)")]
    c.post("/api/ozet", json={"tur": "aylik", "donem": "2026-08"})
    r = c.post("/api/ozet", json={"tur": "aylik", "donem": "2026-08"})
    assert r.status_code == 502 and "kayıtlı özet değişmedi" in r.json()["detail"]
    assert ozet_satirlari(uid)[0][3].endswith("sağlam")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert c.post("/api/ozet", json={"tur": "aylik", "donem": "2026-08"}).status_code == 400


# ---------------------------------------------------------------- ad eşlemesi ve E2

def test_ad_eslemesi_ve_kendi_sirket_ozette_uygulanir(sahte_claude):
    uid = kullanici_olustur(ad_eslemeleri=[{"kaynak": "Medusa", "hedef": "Edisyon uygulaması"}],
                            kendi_alanlar=["medusarights.com"])
    rapor(uid, g(3), "*Günlük Rapor*\n\n*Medusa Çalışmaları:*\n• Medusa'da arama hızlandı\n• 'Medusa raporu' gönderildi")
    madde(uid, g(3), "Medusa'da arama hızlandı", "medusa")
    madde(uid, g(3), "'Medusa raporu' gönderildi")
    madde(uid, g(3), "Medusa'ya 'A' konulu e-posta gönderildi", "eposta")
    sahte_claude.yanitlar = [patron_json("Medusa'da arama hızlandırıldı")]
    c = istemci()
    r = c.post("/api/ozet", json={"tur": "aylik", "donem": "2026-08"}).json()
    istek = sahte_claude.istekler[0]
    esle = lambda m: servisler.ad_esle(m, [{"kaynak": "Medusa", "hedef": "Edisyon uygulaması"}])  # noqa: E731
    assert "Medusa" not in istek["messages"][0]["content"]
    metinler = [m["metin"] for m in girdi_json(istek)["maddeler"]]
    assert esle("Medusa'da arama hızlandı") in metinler
    assert "'Edisyon uygulaması raporu' gönderildi" in metinler  # tırnak içine de uygulanır (O1-ek istisnası)
    assert r["metin"] == esle("*Aylık Yönetici Özeti – Ağustos 2026*\n\n*Genel:*\n• Medusa'da arama hızlandırıldı") and "Medusa" not in r["metin"]
    assert "ASLA" in istek["system"] and "ilsvision.com" in istek["system"] and "medusarights.com" in istek["system"]
    ist = r["istatistik"]
    assert ist["kategoriler"] == [{"ad": "Edisyon uygulaması Çalışmaları", "sayi": 2}]
    assert ist["kurumlar"] == [{"ad": "Edisyon uygulaması", "sayi": 1}]


# ---------------------------------------------------------------- kullanıcı ayrımı

def test_kullanici_ayrimi(sahte_claude):
    a = kullanici_olustur()
    b = kullanici_olustur("b@ornek.com", "B")
    agustos_verisi(a)
    rapor(b, g(10), "• b'nin işi")
    ca, cb = istemci(), istemci("b@ornek.com")
    sahte_claude.yanitlar = [patron_json("A'NIN")]
    ca.post("/api/ozet", json={"tur": "aylik", "donem": "2026-08"})
    bi = cb.get("/api/ozet?tur=aylik&donem=2026-08").json()
    assert bi["ozetler"] == {"patron": None, "basari": None}
    assert (bi["istatistik"]["rapor_gunu"], bi["istatistik"]["acik"], bi["istatistik"]["kurumlar"]) == (1, [], [])
    cb.put("/api/ozet", json={"tur": "aylik", "donem": "2026-08", "bicim": "patron", "metin": "B'NIN"})
    assert ca.get("/api/ozet?tur=aylik&donem=2026-08").json()["ozetler"]["patron"]["metin"].endswith("A'NIN")
    assert [s[3] for s in ozet_satirlari(b)] == ["B'NIN"]
    assert cb.get("/api/raporlar?tur=aylik").json()["toplam"] == 1


# ---------------------------------------------------------------- B) şema

def test_sema_eski_indeks_genisletilir_ve_bicim_anahtarda(tmp_path):
    eski = create_engine(f"sqlite:///{tmp_path}/eski.db")
    with eski.begin() as b:
        b.execute(text("CREATE TABLE reports (id INTEGER PRIMARY KEY, user_id INTEGER, tarih DATE, metin TEXT, "
                       "olusturma DATETIME, tur VARCHAR(10) NOT NULL DEFAULT 'gunluk', hafta_baslangic DATE, "
                       "gonderim VARCHAR(10) NOT NULL DEFAULT 'elle')"))
        b.execute(text("CREATE UNIQUE INDEX uq_reports_user_tarih_tur ON reports (user_id, tarih, tur)"))
        b.execute(text("INSERT INTO reports (user_id, tarih, metin) VALUES (1, '2026-09-15', 'eski')"))
    assert veritabani.sema_guncelle(eski) == [
        "reports.bicim", "reports.istatistik", "reports.yapi", "uq_reports_user_tarih_tur_bicim", "-uq_reports_user_tarih_tur"]
    assert veritabani.sema_guncelle(eski) == []
    ekle = "INSERT INTO reports (user_id, tarih, metin, tur, bicim) VALUES (1, '2026-08-01', 'x', 'aylik', :b)"
    with eski.begin() as b:
        b.execute(text(ekle), {"b": "patron"})
        b.execute(text(ekle), {"b": "basari"})
    for tekrar, parametre in ((ekle, {"b": "patron"}),
                              ("INSERT INTO reports (user_id, tarih, metin) VALUES (1, '2026-09-15', 'ikinci')", {})):
        with pytest.raises(IntegrityError), eski.begin() as b:
            b.execute(text(tekrar), parametre)
    eski.dispose()


# ---------------------------------------------------------------- A) e-posta kuralları bölümü

def ms_bagla(uid, kapsamlar):
    with OturumYapici() as db:
        a = db.get(KullaniciAyari, uid)
        a.ms_refresh_enc, a.ms_eposta, a.ms_durum = guvenlik.sifrele("r"), "ufuk@medusarights.com", "bagli"
        a.ms_kapsamlar = kapsamlar
        db.commit()


def test_eposta_kurallari_yalniz_saglayici_bagliyken(monkeypatch):
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "ms-id")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "ms-sir")
    uid = kullanici_olustur()
    c = istemci()
    assert c.get("/api/ayarlar").json()["eposta_kurallari"] is False  # hiç sağlayıcı yok
    ms_bagla(uid, ["https://graph.microsoft.com/Calendars.Read"])
    assert c.get("/api/ayarlar").json()["eposta_kurallari"] is False  # Microsoft var ama Outlook izni yok
    ms_bagla(uid, ["https://graph.microsoft.com/Mail.Read"])
    a = c.get("/api/ayarlar").json()
    assert a["eposta_kurallari"] is True  # yalnız Outlook
    assert a["kendi_alanlar_otomatik"] == ["ilsvision.com", "medusarights.com"]  # giriş + Microsoft adresi (gri rozet)
    ikinci = kullanici_olustur("b@gmail.com", "B", gmail_kullanici="b@gmail.com", gmail_sifre_enc=guvenlik.sifrele("s"))
    b = istemci("b@gmail.com").get("/api/ayarlar").json()
    # genel sağlayıcı (gmail.com) kendi alanı sayılmaz; kalan, varsayılan sözlükteki 'şirket içi' eşlemesi
    assert b["eposta_kurallari"] is True and b["kendi_alanlar_otomatik"] == ["ilsvision.com"] and ikinci


def test_ayarlar_sayfasinda_eposta_kurallari_ayri_bolum():
    kullanici_olustur()
    html = istemci().get("/ayarlar").text
    bolum = html.split('<div id="epostaKurallari" hidden>')[1].split('<div class="sec"><h2>Rapor</h2>')[0]
    assert "<h2>E-posta kuralları</h2>" in bolum
    for parca in ('id="eposta_gruplama"', 'id="kendiOtomatik"', 'id="kendi_alanlar"', 'id="ekip_ici_atla"', 'id="sozluk"',
                  "Alan adı sözlüğü"):
        assert parca in bolum, parca
    gmail = html.split('id="gmailAyrinti"')[1].split('id="epostaKurallari"')[0]
    assert 'id="eposta_gruplama"' not in gmail and 'id="kendi_alanlar"' not in gmail
    assert html.count('id="sozluk"') == 1
    assert "$('epostaKurallari').hidden = !a.eposta_kurallari" in html


def test_eposta_kurallari_ayar_api_aynen():
    kullanici_olustur()
    c = istemci()
    r = c.put("/api/ayarlar", json={"eposta_gruplama": "alici", "kendi_alanlar": "medusarights.com", "ekip_ici_atla": False,
                                    "alan_sozlugu": "mesam.org.tr=MESAM\nimro.ie=IMRO"}).json()
    assert (r["eposta_gruplama"], r["kendi_alanlar"], r["ekip_ici_atla"]) == ("alici", ["medusarights.com"], False)
    assert r["alan_sozlugu"] == {"mesam.org.tr": "MESAM", "imro.ie": "IMRO"}


# ---------------------------------------------------------------- A3) alan adına gelen ek

@pytest.mark.parametrize("alan, beklenen", [
    ("ornek.com.tr", "ornek.com.tr'ye"), ("firma.com", "firma.com'a"), ("site.net", "site.net'e"),
    ("dernek.org", "dernek.org'a"), ("uygulama.io", "uygulama.io'ya"), ("mesam.org.tr", "mesam.org.tr'ye"),
    ("firma.co.uk", "firma.co.uk'ye"), ("firma.de", "firma.de'ye"), ("firma.fr", "firma.fr'ye"),
    # kurum / kişi adları: mevcut davranış
    ("MESAM", "MESAM'a"), ("MSG", "MSG'ye"), ("IMRO", "IMRO'ya"), ("Coverz", "Coverz'e"), ("Universal Music", "Universal Music'e"),
    ("bir kişi", "bir kişiye"),
])
def test_yonelme_eki_alan_adi(alan, beklenen):
    assert servisler.yonelme_eki(alan) == beklenen


def test_eposta_maddesinde_alan_adi_eki():
    mail = lambda to, konu: {"date": "Wed, 16 Sep 2026 10:00:00 +0300", "subject": konu, "from": "ufuk@ilsvision.com", "to": to}  # noqa: E731
    maddeler = servisler.epostalari_maddele([mail("a@firma.com", "Teklif"), mail("b@site.net", "Fatura")],
                                            "ufuk@ilsvision.com", BUGUN, sozluk={})
    assert [m["metin"] for m in maddeler] == ["firma.com'a 'Teklif' konulu e-posta gönderildi",
                                              "site.net'e 'Fatura' konulu e-posta gönderildi"]


# ---------------------------------------------------------------- E) sayfalar

def test_gecmis_sekmeleri_ve_bugun_ozet_seridi():
    kullanici_olustur()
    c = istemci()
    gecmis = c.get("/gecmis").text
    for parca in ('data-sekme="gunluk"', 'data-sekme="haftalik"', 'data-sekme="aylik"', 'data-sekme="yillik"',
                  'id="donem"', 'data-bicim="patron"', 'data-bicim="basari"', "Yönetici Özeti", "Performans Özeti",
                  'id="ayOzet"', "ozetUret", 'id="yenidenUret"', 'id="pdfIndir"', "PDF indir", "/api/ozet/pdf"):
        assert parca in gecmis, parca
    assert 'data-tur=""' not in gecmis  # eski "Tümü" süzgeci kalktı
    bugun = c.get("/").text
    assert 'id="ozetSerit"' in bugun and "Yönetici Özeti hazırlanabilir" in bugun and "/gecmis#aylik-" in bugun
