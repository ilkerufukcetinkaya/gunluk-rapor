"""K1-ek5: yönetimde kullanıcı silme. Kullanıcıya bağlı yedi tablo da temizlenir; sqlite."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app as uygulama
import guvenlik
from veritabani import (
    KULLANICIYA_BAGLI, ClaudeKullanim, GunlukIfade, HatirlatmaGonderimi, Kullanici, KullaniciAyari, Madde,
    OturumYapici, PushAbonelik, Rapor, Temel, motor,
)

SIFRE = "dogru-sifre-123"
GUN = date(2026, 9, 16)


@pytest.fixture(autouse=True)
def ortam():
    Temel.metadata.drop_all(motor)
    Temel.metadata.create_all(motor)
    yield


def kullanici_olustur(eposta="yonetici@ornek.com", ad="Ufuk", rol="admin") -> int:
    with OturumYapici() as db:
        k = Kullanici(eposta=eposta, ad=ad, sifre_hash=guvenlik.sifre_ozeti(SIFRE), rol=rol,
                      aktif=True, sifre_degistirmeli=False)
        db.add(k)
        db.commit()
        db.add(KullaniciAyari(user_id=k.id, kurulum_tamam=True, proje_adi="Proje"))
        db.commit()
        return k.id


def veri_doldur(uid: int) -> None:
    """Kullanıcıya bağlı yedi tablonun her birine en az bir satır."""
    with OturumYapici() as db:
        m = Madde(user_id=uid, tur="bugun", tarih=GUN, metin="bir iş")
        db.add(m)
        db.commit()
        db.add_all([
            GunlukIfade(user_id=uid, item_id=m.id, tarih=GUN, metin_ai="bugünkü ifade"),
            Rapor(user_id=uid, tarih=GUN, tur="gunluk", metin="rapor"),
            PushAbonelik(user_id=uid, endpoint=f"https://push.ornek.com/{uid}", p256dh="p", auth="a", cihaz_adi="Mac"),
            HatirlatmaGonderimi(user_id=uid, tarih=GUN, kanal="eposta", durum="gonderildi"),
            ClaudeKullanim(user_id=uid, tarih=GUN, cagri=3, girdi_token=10, cikti_token=20),
        ])
        db.commit()


def sayimlar(uid: int) -> dict[str, int]:
    with OturumYapici() as db:
        return {m.__tablename__: db.scalar(select(func.count()).select_from(m).where(m.user_id == uid))
                for m in KULLANICIYA_BAGLI}


def istemci(eposta="yonetici@ornek.com") -> TestClient:
    c = TestClient(uygulama.app, follow_redirects=False)
    assert c.post("/giris", data={"eposta": eposta, "sifre": SIFRE}).status_code == 303
    return c


# ---------------------------------------------------------------- silme

def test_silme_bagli_tum_satirlari_kaldirir():
    kullanici_olustur()
    uye = kullanici_olustur("ayse@ornek.com", "Ayşe", rol="uye")
    veri_doldur(uye)
    assert all(n > 0 for n in sayimlar(uye).values())

    y = istemci().post(f"/yonetim/{uye}/sil")

    assert y.status_code == 200
    assert sayimlar(uye) == {tablo: 0 for tablo in sayimlar(uye)}
    with OturumYapici() as db:
        assert db.get(Kullanici, uye) is None


def test_silme_baska_kullanicinin_verisine_dokunmaz():
    kullanici_olustur()
    silinen = kullanici_olustur("ayse@ornek.com", "Ayşe", rol="uye")
    kalan = kullanici_olustur("can@ornek.com", "Can", rol="uye")
    veri_doldur(silinen)
    veri_doldur(kalan)

    assert istemci().post(f"/yonetim/{silinen}/sil").status_code == 200

    assert all(n > 0 for n in sayimlar(kalan).values())
    with OturumYapici() as db:
        assert db.get(Kullanici, kalan) is not None


def test_delete_metodu_da_calisir():
    kullanici_olustur()
    uye = kullanici_olustur("ayse@ornek.com", "Ayşe", rol="uye")
    veri_doldur(uye)

    assert istemci().delete(f"/yonetim/{uye}").status_code == 200

    with OturumYapici() as db:
        assert db.get(Kullanici, uye) is None
    assert sum(sayimlar(uye).values()) == 0


def test_silinen_eposta_ile_yeniden_davet_edilebilir():
    kullanici_olustur()
    uye = kullanici_olustur("ayse@ornek.com", "Ayşe", rol="uye")
    veri_doldur(uye)
    c = istemci()
    assert c.post(f"/yonetim/{uye}/sil").status_code == 200

    y = c.post("/yonetim/davet", data={"ad": "Ayşe", "eposta": "ayse@ornek.com"})

    assert y.status_code == 200 and "Ayşe davet edildi" in y.text
    with OturumYapici() as db:
        yeni = db.scalar(select(Kullanici).where(Kullanici.eposta == "ayse@ornek.com"))
        assert yeni is not None and yeni.sifre_degistirmeli is True
    assert sum(sayimlar(yeni.id).values()) == 0  # sqlite id'yi geri kullansa bile eski veri yok


def test_silinen_kullanicinin_oturumu_duser():
    kullanici_olustur()
    uye = kullanici_olustur("ayse@ornek.com", "Ayşe", rol="uye")
    uye_c = istemci("ayse@ornek.com")
    assert uye_c.get("/ayarlar").status_code == 200

    assert istemci().post(f"/yonetim/{uye}/sil").status_code == 200

    y = uye_c.get("/ayarlar")
    assert y.status_code == 303 and y.headers["location"] == "/giris"


# ---------------------------------------------------------------- korumalar

def test_kendini_silemez():
    ben = kullanici_olustur()
    kullanici_olustur("ikinci@ornek.com", "İkinci", rol="admin")  # son yönetici değilim

    y = istemci().post(f"/yonetim/{ben}/sil")

    assert y.status_code == 400 and "Kendi hesabınızı silemezsiniz" in y.text
    with OturumYapici() as db:
        assert db.get(Kullanici, ben) is not None


def test_son_yonetici_silinemez():
    ben = kullanici_olustur()
    kullanici_olustur("ayse@ornek.com", "Ayşe", rol="uye")

    y = istemci().post(f"/yonetim/{ben}/sil")

    assert y.status_code == 400 and "Son yöneticiyi silemezsin" in y.text
    with OturumYapici() as db:
        assert db.get(Kullanici, ben) is not None


def test_iki_yonetici_varken_digeri_silinebilir():
    kullanici_olustur()
    oteki = kullanici_olustur("ikinci@ornek.com", "İkinci", rol="admin")

    assert istemci().post(f"/yonetim/{oteki}/sil").status_code == 200

    with OturumYapici() as db:
        assert db.get(Kullanici, oteki) is None


def test_uye_rolu_403():
    kullanici_olustur()
    uye = kullanici_olustur("ayse@ornek.com", "Ayşe", rol="uye")
    kurban = kullanici_olustur("can@ornek.com", "Can", rol="uye")

    y = istemci("ayse@ornek.com").post(f"/yonetim/{kurban}/sil")

    assert y.status_code == 403
    with OturumYapici() as db:
        assert db.get(Kullanici, kurban) is not None
    assert uye  # üye hâlâ duruyor


def test_oturumsuz_giris_sayfasina():
    kullanici_olustur()
    uye = kullanici_olustur("ayse@ornek.com", "Ayşe", rol="uye")
    y = TestClient(uygulama.app, follow_redirects=False).post(f"/yonetim/{uye}/sil")
    assert y.status_code == 303 and y.headers["location"] == "/giris"
    with OturumYapici() as db:
        assert db.get(Kullanici, uye) is not None


def test_bilinmeyen_kullanici_404():
    kullanici_olustur()
    assert istemci().post("/yonetim/9999/sil").status_code == 404


# ---------------------------------------------------------------- arayüz

def test_satirda_onayli_sil_dugmesi_var_kendinde_yok():
    kullanici_olustur()
    uye = kullanici_olustur("ayse@ornek.com", "Ayşe", rol="uye")

    html = istemci().get("/yonetim").text

    assert f'action="/yonetim/{uye}/sil"' in html
    assert "Ayşe ve tüm raporları kalıcı olarak silinecek. Emin misin?" in html
    assert 'class="onay" hidden' in html and "Vazgeç" in html
    # tek tıkla gitmez: satırdaki ilk düğme submit değil
    assert '<button type="button" class="link tehlike" data-sil>Sil</button>' in html
    assert len(re.findall(r'action="/yonetim/\d+/sil"', html)) == 1  # kendi satırında yok
