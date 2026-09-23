"""E2: kendi şirketi e-posta maddelerinden çıkarılır, ekip içi e-posta ayarı. sqlite; ağa çıkılmaz."""
import json
from datetime import date, datetime, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import api
import app as uygulama
import guvenlik
import servisler
from veritabani import Kullanici, KullaniciAyari, Madde, OturumYapici, Temel, motor

BUGUN = date(2026, 9, 16)
BEN = "ufuk@ilsvision.com.tr"
SIFRE = "dogru-sifre-123"
KENDI = servisler.kendi_alanlari([BEN])


def mail(to, subject="Tanıtım", cc="", saat="10:00"):
    return {"date": f"{BUGUN.strftime('%a, %d %b %Y')} {saat}:00 +0300", "subject": subject, "from": BEN, "to": to, "cc": cc}


def metinler(mailler, kendi=KENDI, **k):
    return [m["metin"] for m in servisler.epostalari_maddele(mailler, BEN, BUGUN, kendi_alanlar=kendi, **k)]


# ---------------------------------------------------------------- 1) kendi alan adları

def test_kendi_alanlar_otomatik_ve_ekler():
    assert servisler.kendi_alanlari([BEN, "ufuk@gmail.com"]) == ["ilsvision.com.tr", "ilsvision.com"]  # gmail.com sayılmaz
    assert servisler.kendi_alanlari([BEN], ["ils.vision"], sozluk={}) == ["ilsvision.com.tr", "ils.vision"]
    assert servisler.kendi_mi("ali@x.ilsvision.com.tr", ["ilsvision.com.tr"])  # alt alan adı
    assert not servisler.kendi_mi("ali@ilsvision.com.tr.kotu.com", ["ilsvision.com.tr"])


# ---------------------------------------------------------------- 2) epostalari_maddele

@pytest.mark.parametrize("gruplama", ["konu", "alici"])
def test_to_mesam_cc_ils_ayrica_yok(gruplama):
    m = mail("MESAM <a@mesam.org.tr>", cc="Ali <ali@ilsvision.com.tr>, ayse@ilsvision.com.tr")
    assert metinler([m], gruplama=gruplama) == ["MESAM'a 'Tanıtım' konulu e-posta gönderildi"]


@pytest.mark.parametrize("gruplama", ["konu", "alici"])
def test_to_yalniz_ils_cc_msg_esas_kurum_msg(gruplama):
    m = mail("ILS Vision <ali@ilsvision.com.tr>", cc="b@msg.org.tr")
    assert metinler([m], gruplama=gruplama) == ["MSG'ye 'Tanıtım' konulu e-posta gönderildi"]


def test_esas_kurum_to_icindeki_ilk_kendi_olmayan():
    m = mail("ali@ilsvision.com.tr, b@msg.org.tr", cc="c@imro.ie, d@ilsvision.com.tr")
    assert metinler([m]) == ["MSG'ye 'Tanıtım' konulu e-posta gönderildi (ayrıca IMRO)"]


@pytest.mark.parametrize("gruplama", ["konu", "alici"])
def test_tumu_ils_ekip_ici(gruplama):
    m = mail("ali@ilsvision.com.tr", cc="ayse@x.ilsvision.com.tr", subject="Haftalık plan")
    assert metinler([m], gruplama=gruplama) == []  # varsayılan: ekip içi atlanır
    assert metinler([m], gruplama=gruplama, ekip_ici_atla=False) == ["Ekip içi 'Haftalık plan' konulu e-posta gönderildi"]


def test_alt_alan_adi_kendi_sayilir():
    m = mail("a@mesam.org.tr", cc="Destek <destek@mail.ilsvision.com.tr>")
    assert metinler([m]) == ["MESAM'a 'Tanıtım' konulu e-posta gönderildi"]


def test_kendi_alanlar_ekiyle_ilsvision_com_kendi():
    m = mail("a@mesam.org.tr", cc="Ali <ali@ilsvision.com>")
    sozluksuz = servisler.kendi_alanlari([BEN], sozluk={})  # sözlükte şirket içi eşlemesi yok
    assert metinler([m], kendi=sozluksuz, sozluk={"mesam.org.tr": "MESAM"}) == ["MESAM'a 'Tanıtım' konulu e-posta gönderildi (ayrıca Ali)"]
    ekli = servisler.kendi_alanlari([BEN], ["ilsvision.com"], sozluk={})
    assert metinler([m], kendi=ekli, sozluk={"mesam.org.tr": "MESAM"}) == ["MESAM'a 'Tanıtım' konulu e-posta gönderildi"]
    # sözlükteki "şirket içi" eşlemesi de kendi sayılır
    assert metinler([m], kendi=servisler.kendi_alanlari([BEN])) == ["MESAM'a 'Tanıtım' konulu e-posta gönderildi"]


# ---------------------------------------------------------------- 3) prompt kuralı

def test_promptlarda_kendi_sirket_kurali(sahte_claude):
    adlar = servisler.kendi_sirket_adlari(["ilsvision.com.tr", "ils.co"], {"ils.co": "ILS Vision", "ilsvision.com": "şirket içi"})
    assert adlar == ["ilsvision.com.tr", "ILS Vision", "ils.co"]
    for sistem in (servisler.claude_sistem("MEDUSA", adlar), servisler.duzelt_sistemi("MEDUSA", True, adlar)):
        assert "ASLA yazılmaz" in sistem and "ile paylaşıldı" in sistem and "de bilgilendirildi" in sistem
        assert "ILS Vision" in sistem and "ilsvision.com.tr" in sistem
    assert "ASLA" not in servisler.claude_sistem("MEDUSA")  # şirket bilinmiyorsa kural yok

    servisler.claude_cevir([servisler.madde("eposta", "MESAM'a 'X' konulu e-posta gönderildi")], "t", kendi_sirket=adlar)
    sahte_claude.yanitlar = ["[]"]
    servisler.claude_duzelt([{"id": 1, "tur": "bugun", "metin": "x"}], [], "t", kendi_sirket=adlar)
    assert all("ILS Vision" in i["system"] and "ASLA yazılmaz" in i["system"] for i in sahte_claude.istekler)


# ---------------------------------------------------------------- 4) API: ayar, tarama, geçiş

MAILLER: list[dict] = []


@pytest.fixture(autouse=True)
def ortam(monkeypatch):
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    api.onbellegi_temizle()
    MAILLER.clear()
    monkeypatch.setattr(servisler, "gmail_tara", lambda k, s, bugun, sozluk=None, gruplama="konu", **kw:
                        servisler.epostalari_maddele(MAILLER, k, bugun, sozluk, gruplama, **kw))
    monkeypatch.setattr(servisler, "github_tara", lambda *a, **k: [])
    yield


def kullanici_olustur(**ayar) -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta="ufuk@ilsvision.com.tr", ad="U", sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol="uye",
                      aktif=True, sifre_degistirmeli=False)
        db.add(k)
        db.commit()
        db.add(KullaniciAyari(user_id=k.id, kurulum_tamam=True, gmail_kullanici="ufuk@gmail.com",
                              gmail_sifre_enc=guvenlik.sifrele("s"), **ayar))
        db.commit()
        return k.id


def istemci() -> TestClient:
    c = TestClient(uygulama.app, follow_redirects=False)
    c.__enter__()
    assert c.post("/giris", data={"eposta": "ufuk@ilsvision.com.tr", "sifre": SIFRE}).status_code == 303
    return c


def bugun_maili(to, subject, cc="", saat=(10, 0)):
    zaman = datetime.combine(servisler.istanbul_bugun(), time(*saat), servisler.ISTANBUL)
    return {"date": zaman.strftime("%a, %d %b %Y %H:%M:%S +0300"), "subject": subject, "from": "ufuk@gmail.com",
            "to": to, "cc": cc}


def eposta_satirlari(uid):
    with OturumYapici() as db:
        return db.scalars(select(Madde).where(Madde.user_id == uid, Madde.kaynak == "eposta").order_by(Madde.id)).all()


def test_ayarlar_kendi_alanlar_ve_ekip_ici():
    kullanici_olustur()
    c = istemci()
    a = c.get("/api/ayarlar").json()
    assert a["kendi_alanlar_otomatik"] == ["ilsvision.com.tr", "ilsvision.com"]  # giriş adresi + sözlük; gmail.com değil
    assert a["kendi_alanlar"] == [] and a["ekip_ici_atla"] is True
    a = c.put("/api/ayarlar", json={"kendi_alanlar": " @ILS.co, ils.co\nmedusarights.com ", "ekip_ici_atla": False}).json()
    assert a["kendi_alanlar"] == ["ils.co", "medusarights.com"] and a["ekip_ici_atla"] is False
    assert c.put("/api/ayarlar", json={"kendi_alanlar": "ils vision"}).status_code == 422
    assert c.put("/api/ayarlar", json={"kendi_alanlar": ""}).json()["kendi_alanlar"] == []


def test_ayarlar_sayfasinda_alanlar():
    kullanici_olustur()
    html = istemci().get("/ayarlar").text
    assert 'id="kendi_alanlar"' in html and 'id="kendiOtomatik"' in html and 'id="ekip_ici_atla"' in html
    assert "Kendi şirketimin alan adları" in html and "Ekip içi e-postaları rapora alma" in html


def test_taramada_kendi_sirket_cikarilir_ve_ekip_ici_ayari():
    uid = kullanici_olustur()
    c = istemci()
    MAILLER[:] = [
        bugun_maili("a@mesam.org.tr", "Tanıtım", cc="ali@ilsvision.com.tr, veli@ilsvision.com.tr"),
        bugun_maili("ali@ilsvision.com.tr", "Plan"),
    ]
    assert [m["metin"] for m in c.get("/api/bugun?yenile=1").json()["bulunan"]] == [
        "MESAM'a 'Tanıtım' konulu e-posta gönderildi"]
    c.put("/api/ayarlar", json={"ekip_ici_atla": False})
    assert [m["metin"] for m in c.get("/api/bugun?yenile=1").json()["bulunan"]] == [
        "MESAM'a 'Tanıtım' konulu e-posta gönderildi", "Ekip içi 'Plan' konulu e-posta gönderildi"]
    assert len(eposta_satirlari(uid)) == 2


def test_taramada_prompta_kendi_sirket_gider(sahte_claude):
    kullanici_olustur()
    MAILLER[:] = [bugun_maili("a@mesam.org.tr", "Tanıtım")]
    istemci().get("/api/bugun?yenile=1")
    assert "ilsvision.com.tr" in sahte_claude.istekler[0]["system"] and "ASLA" in sahte_claude.istekler[0]["system"]


def test_bugunku_eski_madde_guncellenir_metin_ai_sifirlanir():
    uid = kullanici_olustur()
    bugun = servisler.istanbul_bugun()
    # E2 öncesi: ILS alıcıları "(ayrıca …)" ekinde ve esas kurumda yer alıyordu
    ayni_id = servisler.konu_maddesi_id("MESAM", "Tanıtım", bugun)
    kurum_degisen_id = servisler.konu_maddesi_id("ILS Vision", "Liste", bugun)
    duzenlenen_id = servisler.konu_maddesi_id("IMRO", "Katalog", bugun)
    with OturumYapici() as db:
        for kid, metin, ai, duzenledi in [
            (ayni_id, "MESAM'a 'Tanıtım' konulu e-posta gönderildi (ayrıca ILS Vision)", "MESAM ve ILS ile paylaşıldı.", False),
            (kurum_degisen_id, "ILS Vision'a 'Liste' konulu e-posta gönderildi (ayrıca MSG)", "Liste ILS'ye gönderildi.", False),
            (duzenlenen_id, "IMRO'ya katalog gönderildi, ILS de bilgilendirildi", None, True),
        ]:
            db.add(Madde(user_id=uid, tur="bulunan", tarih=bugun, kaynak="eposta", kaynak_id=kid, metin=metin,
                         metin_ai=ai, ai_tarih=bugun if ai else None, kullanici_duzenledi=duzenledi))
        db.commit()
    MAILLER[:] = [
        bugun_maili("a@mesam.org.tr", "Tanıtım", cc="ILS Vision <ali@ilsvision.com.tr>"),
        bugun_maili("ILS Vision <ali@ilsvision.com.tr>", "Liste", cc="b@msg.org.tr"),
        bugun_maili("c@imro.ie", "Katalog", cc="ali@ilsvision.com.tr"),
    ]
    istemci().get("/api/bugun?yenile=1")
    satirlar = {m.kaynak_id: m for m in eposta_satirlari(uid)}
    assert satirlar[ayni_id].metin == "MESAM'a 'Tanıtım' konulu e-posta gönderildi"
    assert satirlar[ayni_id].metin_ai is None and satirlar[ayni_id].ai_tarih is None
    assert satirlar[kurum_degisen_id].gizli is True  # kurum değişti: E1 kuralıyla gizlenir
    yeni = satirlar[servisler.konu_maddesi_id("MSG", "Liste", bugun)]
    assert yeni.metin == "MSG'ye 'Liste' konulu e-posta gönderildi" and not yeni.gizli
    assert satirlar[duzenlenen_id].metin == "IMRO'ya katalog gönderildi, ILS de bilgilendirildi"  # düzenlenene dokunulmaz
    assert satirlar[duzenlenen_id].gizli is False

    # metin_ai'si sıfırlanan madde bir sonraki düzeltme paketine girer
    with OturumYapici() as db:
        paket = api.duzeltme_paketi(db, db.get(Kullanici, uid), bugun)
        assert ayni_id in {m.kaynak_id for m in paket}


def test_guncellenen_madde_yeniden_cevrilir(sahte_claude):
    uid = kullanici_olustur()
    bugun = servisler.istanbul_bugun()
    kid = servisler.konu_maddesi_id("MESAM", "Tanıtım", bugun)
    with OturumYapici() as db:
        db.add(Madde(user_id=uid, tur="bulunan", tarih=bugun, kaynak="eposta", kaynak_id=kid,
                     metin="MESAM'a 'Tanıtım' konulu e-posta gönderildi (ayrıca ILS Vision)", metin_ai="eski"))
        db.commit()
    sahte_claude.yanitlar = [lambda istek: json.dumps([
        {"id": g["id"], "metin": "AI: " + g["metin"]} for g in json.loads(istek["messages"][0]["content"].split("\n", 1)[1])
    ], ensure_ascii=False)]
    MAILLER[:] = [bugun_maili("a@mesam.org.tr", "Tanıtım", cc="ali@ilsvision.com.tr")]
    istemci().get("/api/bugun?yenile=1")
    assert eposta_satirlari(uid)[0].metin_ai == "AI: MESAM'a 'Tanıtım' konulu e-posta gönderildi"
