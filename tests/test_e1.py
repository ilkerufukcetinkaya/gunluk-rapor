"""E1: e-postalar konu başına ayrı madde, gruplama ayarı, eski gruplu maddenin gizlenmesi. sqlite; ağa çıkılmaz."""
import hashlib
import json
from datetime import date, datetime, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import api
import app as uygulama
import guvenlik
import servisler
from veritabani import Kullanici, KullaniciAyari, Madde, OturumYapici, Temel, motor

BUGUN = date(2026, 9, 16)
BEN = "ufuk@ilsvision.com"
SIFRE = "dogru-sifre-123"


def mail(to, subject="Konu", saat="10:00", cc="", gun=BUGUN):
    return {"date": f"{gun.strftime('%a, %d %b %Y')} {saat}:00 +0300", "subject": subject, "from": BEN, "to": to, "cc": cc}


def metinler(mailler, **k):
    return [m["metin"] for m in servisler.epostalari_maddele(mailler, BEN, BUGUN, **k)]


# ---------------------------------------------------------------- 1) konu başına madde

def test_yedi_farkli_konu_yedi_madde():
    konular = [
        "Tete ve Masal Rüyalar Diyarı Tanıtımı Hk.", "Ağustos CRD itirazı", "Eylül dağıtım listesi",
        "Sözleşme taslağı", "Fatura", "Katalog güncellemesi", "Toplantı notları",
    ]
    sonuc = servisler.epostalari_maddele([mail("a@mesam.org.tr", k) for k in konular], BEN, BUGUN)
    assert [m["metin"] for m in sonuc] == [f"MESAM'a '{k}' konulu e-posta gönderildi" for k in konular]
    assert len({m["id"] for m in sonuc}) == 7 and all(m["kaynak"] == "eposta" for m in sonuc)


def test_ayni_konunun_yanit_zinciri_tek_madde():
    mailler = [
        mail("Ayşe <ayse@msg.org.tr>", "Ağustos CRD itirazı", "09:12"),
        mail("ayse@msg.org.tr", "Re: ağustos crd İTİRAZI ", "14:37"),
        mail("crd@msg.org.tr", "YNT: Ynt:  Ağustos  CRD itirazı", "11:05"),
    ]
    sonuc = servisler.epostalari_maddele(mailler, BEN, BUGUN)
    assert [m["metin"] for m in sonuc] == ["MSG'ye 'Ağustos CRD itirazı' konulu 3 e-posta gönderildi"]
    assert sonuc[0]["kaynak_zaman"].strftime("%H:%M") == "14:37"  # en son mailin saati


def test_onekler_normalize_edilir():
    for konu in ("Fwd: X", "FW: x", "İLT: X", "ilt: x", "  RE:  YNT: x  "):
        assert servisler.konu_anahtari(konu) == "x"


def test_farkli_kurumlara_ayni_konu_kurum_basina_ayri():
    mailler = [mail("a@mesam.org.tr", "Tanıtım"), mail("b@msg.org.tr", "Re: Tanıtım"), mail("c@imro.ie", "Tanıtım")]
    assert metinler(mailler) == [
        "MESAM'a 'Tanıtım' konulu e-posta gönderildi",
        "MSG'ye 'Tanıtım' konulu e-posta gönderildi",
        "IMRO'ya 'Tanıtım' konulu e-posta gönderildi",
    ]


def test_cok_alicili_mail_ayrica_ekiyle_tek_madde():
    mailler = [mail("MESAM <a@mesam.org.tr>, b@msg.org.tr", "Tanıtım", cc="c@imro.ie, d@mesam.org.tr")]
    assert metinler(mailler) == ["MESAM'a 'Tanıtım' konulu e-posta gönderildi (ayrıca MSG, IMRO)"]
    # esas kurum ilk To alıcısıdır; sırası değişirse madde de değişir
    assert metinler([mail("b@msg.org.tr, a@mesam.org.tr", "Tanıtım")]) == [
        "MSG'ye 'Tanıtım' konulu e-posta gönderildi (ayrıca MESAM)"]


def test_konusuz_mail():
    assert metinler([mail("a@mesam.org.tr", "")]) == ["MESAM'a konusuz bir e-posta gönderildi"]
    assert metinler([mail("a@mesam.org.tr", ""), mail("a@mesam.org.tr", "Re:")]) == ["MESAM'a konusuz 2 e-posta gönderildi"]


def test_alici_ayari_eski_ciktiyla_birebir():
    mailler = [
        mail("Ayşe <ayse@msg.org.tr>", "Re: A"), mail("mehmet@msg.org.tr", "B"), mail("crd@msg.org.tr", "Fwd: C"),
        mail("a@mesam.org.tr", "Tanıtım"), mail("a@mesam.org.tr", "Re: Tanıtım"),
        mail("a@imro.ie, b@msg.org.tr", "Ortak"),
        mail("ali@ilsvision.com", "İç yazışma"),
    ]
    sonuc = servisler.epostalari_maddele(mailler, BEN, BUGUN, gruplama="alici")
    assert [m["metin"] for m in sonuc] == [
        "MSG'ye 3 e-posta gönderildi (konular: A; B; C)",
        "MESAM'a 'Tanıtım' konulu 2 e-posta gönderildi",
        "IMRO ve MSG'ye 'Ortak' konulu e-posta gönderildi",
        "Şirket içi 'İç yazışma' konulu e-posta gönderildi",
    ]
    assert all(m["id"] == servisler.madde_id("eposta", m["metin"]) for m in sonuc)


def test_kaynak_id_kararli():
    ilk = servisler.epostalari_maddele([mail("a@mesam.org.tr", "Tanıtım", "09:00")], BEN, BUGUN)[0]
    beklenen = hashlib.sha1(("MESAM" + "tanıtım" + BUGUN.isoformat()).encode()).hexdigest()[:10]
    assert ilk["id"] == beklenen
    sonra = servisler.epostalari_maddele(
        [mail("a@mesam.org.tr", "Tanıtım", "09:00"), mail("a@mesam.org.tr", "RE: tanıtım", "16:00")], BEN, BUGUN)[0]
    assert sonra["id"] == ilk["id"] and sonra["metin"].endswith("konulu 2 e-posta gönderildi")
    ertesi = servisler.epostalari_maddele([mail("a@mesam.org.tr", "Tanıtım", gun=date(2026, 9, 17))], BEN, date(2026, 9, 17))[0]
    assert ertesi["id"] != ilk["id"]


# ---------------------------------------------------------------- 2) prompt kuralı

def test_promptlarda_eposta_kurali_gecer(sahte_claude):
    assert servisler.EPOSTA_KURALI in servisler.claude_sistem()
    assert servisler.EPOSTA_KURALI in servisler.duzelt_sistemi("MEDUSA")
    for parca in ("konu başlığını anlamını koruyarak", "birleştirme", "sayı ekleme ya da çıkarma"):
        assert parca in servisler.EPOSTA_KURALI
    servisler.claude_cevir([servisler.madde("eposta", "MESAM'a 'X' konulu e-posta gönderildi")], "test")
    assert servisler.EPOSTA_KURALI in sahte_claude.istekler[0]["system"]


# ---------------------------------------------------------------- 3) API: ayar, tarama, geçiş

MAILLER: list[dict] = []


@pytest.fixture(autouse=True)
def ortam(monkeypatch):
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    api.onbellegi_temizle()
    MAILLER.clear()
    monkeypatch.setattr(servisler, "gmail_tara", lambda k, s, bugun, sozluk=None, gruplama="konu":
                        servisler.epostalari_maddele(MAILLER, k, bugun, sozluk, gruplama))
    monkeypatch.setattr(servisler, "github_tara", lambda *a, **k: [])
    yield


def kullanici_olustur() -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta="a@ornek.com", ad="A", sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol="uye", aktif=True,
                      sifre_degistirmeli=False)
        db.add(k)
        db.commit()
        db.add(KullaniciAyari(user_id=k.id, kurulum_tamam=True, gmail_kullanici=BEN, gmail_sifre_enc=guvenlik.sifrele("s")))
        db.commit()
        return k.id


def istemci() -> TestClient:
    c = TestClient(uygulama.app, follow_redirects=False)
    c.__enter__()
    assert c.post("/giris", data={"eposta": "a@ornek.com", "sifre": SIFRE}).status_code == 303
    return c


def bugun_maili(to, subject, saat=(10, 0)):
    zaman = datetime.combine(servisler.istanbul_bugun(), time(*saat), servisler.ISTANBUL)
    return {"date": zaman.strftime("%a, %d %b %Y %H:%M:%S +0300"), "subject": subject, "from": BEN, "to": to, "cc": ""}


def eposta_satirlari(uid):
    with OturumYapici() as db:
        return db.scalars(select(Madde).where(Madde.user_id == uid, Madde.kaynak == "eposta").order_by(Madde.id)).all()


def test_gruplama_ayari_varsayilan_konu_ve_degisir():
    kullanici_olustur()
    c = istemci()
    assert c.get("/api/ayarlar").json()["eposta_gruplama"] == "konu"
    assert c.put("/api/ayarlar", json={"eposta_gruplama": "alici"}).json()["eposta_gruplama"] == "alici"
    assert c.put("/api/ayarlar", json={"eposta_gruplama": "kisi"}).status_code == 422
    assert c.get("/api/ayarlar").json()["eposta_gruplama"] == "alici"


def test_ayarlar_sayfasinda_gruplama_secimi():
    kullanici_olustur()
    html = istemci().get("/ayarlar").text
    assert 'id="eposta_gruplama"' in html
    assert "Her konu ayrı madde (önerilen)" in html and "Alıcı başına tek madde" in html


def test_ikinci_tarama_yeni_satir_acmaz_ayni_konu_guncellenir():
    uid = kullanici_olustur()
    c = istemci()
    MAILLER[:] = [bugun_maili("a@mesam.org.tr", "Tanıtım", (9, 0)), bugun_maili("b@msg.org.tr", "Liste", (9, 30))]
    c.get("/api/bugun?yenile=1")
    ilk = {m.kaynak_id: m.id for m in eposta_satirlari(uid)}
    c.get("/api/bugun?yenile=1")
    assert {m.kaynak_id: m.id for m in eposta_satirlari(uid)} == ilk

    MAILLER.append(bugun_maili("a@mesam.org.tr", "Re: Tanıtım", (15, 45)))
    d = c.get("/api/bugun?yenile=1").json()
    satirlar = eposta_satirlari(uid)
    assert {m.kaynak_id: m.id for m in satirlar} == ilk
    assert [m["metin"] for m in d["bulunan"]] == [
        "MESAM'a 'Tanıtım' konulu 2 e-posta gönderildi", "MSG'ye 'Liste' konulu e-posta gönderildi"]
    assert datetime.fromisoformat(next(m for m in d["bulunan"] if m["metin"].startswith("MESAM"))["kaynak_zaman"]) \
        .astimezone(servisler.ISTANBUL).strftime("%H:%M") == "15:45"


def test_metni_degisen_madde_yeniden_cevrilir_duzenlenen_korunur(sahte_claude):
    uid = kullanici_olustur()
    c = istemci()
    cevir = lambda istek: json.dumps([  # noqa: E731
        {"id": g["id"], "metin": "AI: " + g["metin"]} for g in json.loads(istek["messages"][0]["content"].split("\n", 1)[1])
    ], ensure_ascii=False)
    sahte_claude.yanitlar = [cevir, cevir]
    MAILLER[:] = [bugun_maili("a@mesam.org.tr", "Tanıtım"), bugun_maili("b@msg.org.tr", "Liste")]
    c.get("/api/bugun?yenile=1")
    msg = next(m for m in eposta_satirlari(uid) if m.metin.startswith("MSG"))
    c.patch(f"/api/maddeler/{msg.id}", json={"metin": "MSG'ye liste iletildi"})

    MAILLER.extend([bugun_maili("a@mesam.org.tr", "Re: Tanıtım", (11, 0)), bugun_maili("b@msg.org.tr", "Re: Liste", (11, 0))])
    c.get("/api/bugun?yenile=1")
    ikinci = json.loads(sahte_claude.istekler[1]["messages"][0]["content"].split("\n", 1)[1])
    assert [g["metin"] for g in ikinci] == ["MESAM'a 'Tanıtım' konulu 2 e-posta gönderildi"]  # düzenlenen gitmez
    satirlar = {m.metin[:4]: m for m in eposta_satirlari(uid)}
    assert satirlar["MESA"].metin_ai == "AI: MESAM'a 'Tanıtım' konulu 2 e-posta gönderildi"
    assert satirlar["MSG'"].metin == "MSG'ye liste iletildi" and satirlar["MSG'"].kullanici_duzenledi


def test_eski_gruplu_madde_gizlenir_duzenlenen_korunur():
    uid = kullanici_olustur()
    bugun = servisler.istanbul_bugun()
    eski_metin = "MSG'ye 2 e-posta gönderildi (konular: A; B)"
    duzenlenen_metin = "MESAM'a 2 e-posta gönderildi (konular: C; D)"
    with OturumYapici() as db:
        for metin, duzenledi, tarih in [
            (eski_metin, False, bugun),
            (duzenlenen_metin, True, bugun),
            ("IMRO'ya 3 e-posta gönderildi (konular: X; Y; Z)", False, date(2026, 9, 1)),  # geçmiş gün
        ]:
            db.add(Madde(user_id=uid, tur="bulunan", tarih=tarih, kaynak="eposta", metin=metin,
                         kaynak_id=servisler.madde_id("eposta", metin), kullanici_duzenledi=duzenledi))
        db.add(Madde(user_id=uid, tur="bulunan", tarih=bugun, kaynak="medusa", kaynak_id="c1", metin="commit"))
        db.commit()
    MAILLER[:] = [bugun_maili("a@msg.org.tr", "A"), bugun_maili("a@msg.org.tr", "B"),
                  bugun_maili("b@mesam.org.tr", "C"), bugun_maili("b@mesam.org.tr", "D")]
    c = istemci()
    d = c.get("/api/bugun?yenile=1").json()

    with OturumYapici() as db:
        satirlar = {m.metin: m for m in db.scalars(select(Madde).where(Madde.user_id == uid))}
        assert satirlar[eski_metin].gizli is True  # silinmez, gizlenir
        assert satirlar[duzenlenen_metin].gizli is False
        assert satirlar["IMRO'ya 3 e-posta gönderildi (konular: X; Y; Z)"].gizli is False
        assert satirlar["commit"].gizli is False
        assert db.scalar(select(func.count()).select_from(Madde).where(Madde.user_id == uid)) == 8
    gorunen = [m["metin"] for m in d["bulunan"] if not m["gizli"]]  # GitHub kapalı: commit listelenmez
    assert gorunen == [
        duzenlenen_metin,
        "MSG'ye 'A' konulu e-posta gönderildi", "MSG'ye 'B' konulu e-posta gönderildi",
        "MESAM'a 'C' konulu e-posta gönderildi", "MESAM'a 'D' konulu e-posta gönderildi",
    ]


def test_gmail_hatasinda_hicbir_sey_gizlenmez(monkeypatch):
    uid = kullanici_olustur()
    metin = "MSG'ye 2 e-posta gönderildi (konular: A; B)"
    with OturumYapici() as db:
        db.add(Madde(user_id=uid, tur="bulunan", tarih=servisler.istanbul_bugun(), kaynak="eposta", metin=metin,
                     kaynak_id=servisler.madde_id("eposta", metin)))
        db.commit()

    def patla(*a, **k):
        raise servisler.KaynakHatasi("Gmail'e bağlanılamadı")
    monkeypatch.setattr(servisler, "gmail_tara", patla)
    istemci().get("/api/bugun?yenile=1")
    assert [m.gizli for m in eposta_satirlari(uid)] == [False]
