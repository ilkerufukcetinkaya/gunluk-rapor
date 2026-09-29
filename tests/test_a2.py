"""A2: genel posta sağlayıcısındaki alıcı — alan adı yerine görünen ad, ad yoksa "bir kişi"; E2 ile tek liste."""
from datetime import date, datetime, time

import pytest

import servisler

GUN = date(2026, 9, 16)
BEN = "ufuk@ilsvision.com"
GENEL = ("gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com", "msn.com", "outlook.com.tr", "icloud.com",
         "me.com", "yahoo.com", "yandex.com", "yandex.com.tr", "proton.me", "protonmail.com")


def mail(to: str, subject: str, saat: str = "10:00", cc: str = "") -> dict:
    return {"date": f"Wed, 16 Sep 2026 {saat}:00 +0300", "subject": subject, "from": BEN, "to": to, "cc": cc}


def metinler(mailler: list[dict], gruplama: str = "konu") -> list[str]:
    kendi = servisler.kendi_alanlari([BEN])
    return [m["metin"] for m in servisler.epostalari_maddele(mailler, BEN, GUN, gruplama=gruplama, kendi_alanlar=kendi)]


@pytest.mark.parametrize("gorunen, adres, beklenen", [
    ("Ufuk Çetinkaya", "ufuk.c@gmail.com", "Ufuk Çetinkaya"),
    ('"Ayşe Yılmaz"', "ayse@hotmail.com", "Ayşe Yılmaz"),
    ("", "x@outlook.com", "bir kişi"),
    ("x@outlook.com", "x@outlook.com", "bir kişi"),  # Outlook/Graph ad yokken adresi verir
    ("", "a@ornek.com.tr", "ornek.com.tr"),  # kurumsal, sözlükte yok: alan adı sürer
    ("Universal Music", "a@umusic.com", "Universal Music"),  # kurumsal, görünen adlı: değişmedi
    ("a@umusic.com", "a@umusic.com", "umusic.com"),  # kurumsal, adres biçimli ad: ham adres değil alan adı
    ("", "x@mesam.org.tr", "MESAM"),
])
def test_kurum_adi(gorunen, adres, beklenen):
    assert servisler.kurum_adi(gorunen, adres) == beklenen


@pytest.mark.parametrize("alan", GENEL)
def test_listedeki_her_saglayici_ad_yoksa_bir_kisi(alan):
    assert servisler.kurum_adi("", f"k@{alan}") == "bir kişi"


def test_sozlukte_eslenen_genel_saglayici_sozlukten():
    assert servisler.kurum_adi("Ali", "ali@gmail.com", {"gmail.com": "Serbest çalışanlar"}) == "Serbest çalışanlar"


def test_gorunen_adli_ve_adsiz_maddeler():
    assert metinler([
        mail("Ufuk Çetinkaya <ufuk.c@gmail.com>", "Teklif"),
        mail("\"Ayşe Yılmaz\" <ayse@hotmail.com>", "Sözleşme", "11:00"),
        mail("x@outlook.com", "Fatura", "12:00"),
        mail("a@ornek.com.tr", "Katalog", "13:00"),
    ]) == [
        "Ufuk Çetinkaya'ya 'Teklif' konulu e-posta gönderildi",
        "Ayşe Yılmaz'a 'Sözleşme' konulu e-posta gönderildi",
        "Bir kişiye 'Fatura' konulu e-posta gönderildi",
        "ornek.com.tr'ye 'Katalog' konulu e-posta gönderildi",  # alan adı; ek son parçanın okunuşuna göre (A3)
    ]


def test_ayrica_ve_alici_gruplamasi():
    assert metinler([mail("y@gmail.com", "Teklif", cc="MESAM <a@mesam.org.tr>, Veli Kaya <veli@yandex.com>")]) == [
        "Bir kişiye 'Teklif' konulu e-posta gönderildi (ayrıca MESAM, Veli Kaya)"]
    assert metinler([mail("a@mesam.org.tr, y@icloud.com", "Duyuru")], "alici") == [
        "MESAM ve bir kişiye 'Duyuru' konulu e-posta gönderildi"]
    assert metinler([mail("Ufuk Çetinkaya <u@gmail.com>", "A"), mail("Ufuk Çetinkaya <u@gmail.com>", "B", "11:00")], "alici") == [
        "Ufuk Çetinkaya'ya 2 e-posta gönderildi (konular: A; B)"]


def test_outlook_yolunda_adres_bicimli_ad():
    zaman = datetime.combine(GUN, time(10), servisler.ISTANBUL).isoformat()
    kisi = lambda ad, adres: {"emailAddress": {"name": ad, "address": adres}}  # noqa: E731
    mailler = [servisler.outlook_mesaji({"subject": k, "sentDateTime": zaman, "from": kisi("Ufuk", BEN), "toRecipients": [a]})
               for k, a in (("Teklif", kisi("Ufuk Çetinkaya", "u@gmail.com")), ("Fatura", kisi("x@outlook.com", "x@outlook.com")))]
    assert metinler(mailler) == ["Ufuk Çetinkaya'ya 'Teklif' konulu e-posta gönderildi",
                                 "Bir kişiye 'Fatura' konulu e-posta gönderildi"]


def test_toplanti_katilimcisi_da_ayni_kural():
    etkinlik = {"id": "e1", "summary": "Görüşme", "status": "confirmed", "organizer": {"self": True},
                "start": {"dateTime": "2026-09-16T10:00:00+03:00"}, "end": {"dateTime": "2026-09-16T11:00:00+03:00"},
                "attendees": [{"email": "a@mesam.org.tr"}, {"email": "ayse@gmail.com", "displayName": "Ayşe Yılmaz"},
                              {"email": "k@hotmail.com"}]}
    an = datetime.combine(GUN, time(23), servisler.ISTANBUL)
    assert servisler.takvim_maddeleri([etkinlik], GUN, an=an)[0]["metin"] == \
        "'Görüşme' toplantısı yapıldı (MESAM, Ayşe Yılmaz, bir kişi ile)"


def test_e2_ile_tek_liste_hotmail_kendi_sirketi_sayilmaz():
    assert set(GENEL) <= servisler.GENEL_SAGLAYICILAR
    adresler = [BEN] + [f"ufuk@{a}" for a in GENEL]
    assert servisler.kendi_alanlari(adresler, sozluk={}) == ["ilsvision.com"]
    assert "hotmail.com" not in servisler.kendi_alanlari(["ufuk@hotmail.com"], sozluk={})
