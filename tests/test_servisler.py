import json
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import servisler  # noqa: E402

BUGUN = date(2026, 9, 16)
BEN = "ufuk@ilsvision.com"


def mail(to, subject="Konu", date_="Wed, 16 Sep 2026 10:00:00 +0300", cc="", from_=BEN):
    return {"date": date_, "subject": subject, "from": from_, "to": to, "cc": cc}


# 1) yönelme eki
@pytest.mark.parametrize("ad, beklenen", [
    ("MESAM", "MESAM'a"),
    ("MSG", "MSG'ye"),
    ("IMRO", "IMRO'ya"),
    ("Coverz", "Coverz'e"),
    ("Universal", "Universal'e"),
])
def test_yonelme_eki(ad, beklenen):
    assert servisler.yonelme_eki(ad) == beklenen


# 2) konu temizleme
def test_konu_temizle():
    assert servisler.konu_temizle("Re: Fwd: Ağustos") == "Ağustos"
    assert servisler.konu_temizle("YNT: RE: FW: Ağustos") == "Ağustos"


def test_konu_mime_cozulur():
    kodlu = "=?utf-8?b?QcSfdXN0b3MgQ1JEIGl0aXJhesSx?="
    assert servisler.basligi_coz(kodlu) == "Ağustos CRD itirazı"


# 3) aynı alıcıya 3 mail gruplanır
def test_ayni_aliciya_uc_mail_gruplanir():
    mailler = [
        mail("Ayşe <ayse@msg.org.tr>", "Re: A"),
        mail("Mehmet <mehmet@msg.org.tr>", "B"),
        mail("crd@msg.org.tr", "Fwd: C"),
    ]
    sonuc = servisler.epostalari_maddele(mailler, BEN, BUGUN, gruplama="alici")
    assert [m["metin"] for m in sonuc] == ["MSG'ye 3 e-posta gönderildi (konular: A; B; C)"]
    assert sonuc[0]["kaynak"] == "eposta"
    assert sonuc[0]["id"] == servisler.madde_id("eposta", sonuc[0]["metin"])


def test_tek_mail_metni():
    sonuc = servisler.epostalari_maddele([mail("x@msg.org.tr", "Ağustos CRD itirazı")], BEN, BUGUN)
    assert sonuc[0]["metin"] == "MSG'ye 'Ağustos CRD itirazı' konulu e-posta gönderildi"


# 4) Date başlığına göre Istanbul günü süzülür
def test_dunku_mail_bugun_listesine_girmez():
    mailler = [
        mail("a@mesam.org.tr", "dün akşam", "Tue, 15 Sep 2026 23:59:00 +0300"),
        mail("a@imro.ie", "UTC'de dün ama Istanbul'da bugün", "Tue, 15 Sep 2026 21:30:00 +0000"),
        mail("a@imro.ie", "UTC'de bugün ama Istanbul'da yarın", "Wed, 16 Sep 2026 21:30:00 +0000"),
        mail("a@msg.org.tr", "Istanbul'da günün ilk dakikası", "Wed, 16 Sep 2026 00:00:00 +0300"),
    ]
    metinler = [m["metin"] for m in servisler.epostalari_maddele(mailler, BEN, BUGUN)]
    assert metinler == [
        "IMRO'ya 'UTC'de dün ama Istanbul'da bugün' konulu e-posta gönderildi",
        "MSG'ye 'Istanbul'da günün ilk dakikası' konulu e-posta gönderildi",
    ]


# 5) alan adı sözlüğü + noreply atlama
def test_alan_adi_sozlugu():
    assert servisler.kurum_adi("", "x@mesam.org.tr") == "MESAM"
    assert servisler.kurum_adi("", "x@imro.ie") == "IMRO"
    assert servisler.kurum_adi("", "destek@coverz.io") == "Coverz"
    assert servisler.kurum_adi("", "ali@ilsvision.com") == "şirket içi"
    assert servisler.kurum_adi("Universal Music", "a@umusic.com") == "Universal Music"
    assert servisler.kurum_adi("", "a@ornek.com.tr") == "ornek.com.tr"


def test_noreply_ve_kendine_gonderilen_atlanir():
    mailler = [
        mail("no-reply@ilsvision.com", "Otomatik bildirim"),
        mail("noreply@coverz.io", "Bildirim"),
        mail(BEN, "Not"),
        mail("Coverz <ops@coverz.io>", "Liste", from_="noreply@ilsvision.com"),
        mail("Ali <ali@ilsvision.com>", "İç yazışma"),
    ]
    metinler = [m["metin"] for m in servisler.epostalari_maddele(mailler, BEN, BUGUN)]
    assert metinler == ["Şirket içi 'İç yazışma' konulu e-posta gönderildi"]


# 6) Claude JSON dönmezse ham metin korunur
def _claude_istemcisi(yanit_metni: str) -> httpx.Client:
    def isleyici(istek: httpx.Request) -> httpx.Response:
        govde = json.loads(istek.content)
        assert govde["model"] == "claude-sonnet-5" and govde["max_tokens"] == 1500
        return httpx.Response(200, json={"content": [{"type": "text", "text": yanit_metni}], "stop_reason": "end_turn"})

    return httpx.Client(transport=httpx.MockTransport(isleyici))


def test_claude_json_donmezse_ham_metin_korunur():
    maddeler = [servisler.madde("medusa", "Add trigram index to works table")]
    sonuc, hata = servisler.claude_cevir(maddeler, "test", _claude_istemcisi("Üzgünüm, yardımcı olamam."))
    assert sonuc == maddeler
    assert hata and hata.startswith("claude:")


def test_claude_json_donerse_metin_degisir_id_korunur():
    m = servisler.madde("medusa", "Fix N+1 in rollup endpoint")
    yanit = json.dumps([{"id": m["id"], "metin": "MEDUSA raporları hızlandırıldı."}], ensure_ascii=False)
    sonuc, hata = servisler.claude_cevir([m], "test", _claude_istemcisi(yanit))
    assert hata is None
    assert sonuc == [{**m, "metin": "MEDUSA raporları hızlandırıldı."}]


def test_github_merge_atlanir():
    def isleyici(istek):
        assert istek.url.params["since"] == "2026-09-15T21:00:00Z"
        return httpx.Response(200, json=[
            {"commit": {"message": "Merge pull request #4"}},
            {"commit": {"message": "Add trigram index\n\nayrıntı"}},
        ])

    sonuc = servisler.github_tara("t", "ufuk/medusa", BUGUN, httpx.Client(transport=httpx.MockTransport(isleyici)))
    assert [(m["kaynak"], m["metin"]) for m in sonuc] == [("medusa", "Add trigram index")]


def test_gonderilmis_klasoru_turkce_ad():
    liste = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Sent) "/" "[Gmail]/G&APY-nderilmi&AV8- Postalar"',
    ]
    klasorler = servisler.klasorleri_ayristir(liste)
    gonderilmis = next(ad for bayrak, ad in klasorler if "\\Sent" in bayrak)
    assert servisler._mutf7_coz(gonderilmis) == "[Gmail]/Gönderilmiş Postalar"


# 7) R5: kaynağın saati — e-postada Date başlığı (grupta en son), commit'te author tarihi; Istanbul saati
def test_eposta_kaynak_zaman_gruplanan_maddede_en_son_saat():
    mailler = [
        mail("a@msg.org.tr", "A", "Wed, 16 Sep 2026 09:12:00 +0300"),
        mail("b@msg.org.tr", "B", "Wed, 16 Sep 2026 11:37:00 +0000"),  # Istanbul 14:37
        mail("c@msg.org.tr", "C", "Wed, 16 Sep 2026 10:00:00 +0300"),
        mail("a@imro.ie", "UTC'de dün", "Tue, 15 Sep 2026 21:30:00 +0000"),
    ]
    sonuc = {m["metin"].split("'")[0]: m for m in servisler.epostalari_maddele(mailler, BEN, BUGUN, gruplama="alici")}
    msg, imro = sonuc["MSG"], sonuc["IMRO"]
    assert msg["kaynak_zaman"].tzinfo == servisler.ISTANBUL
    assert msg["kaynak_zaman"].strftime("%Y-%m-%d %H:%M") == "2026-09-16 14:37"
    assert imro["kaynak_zaman"].strftime("%Y-%m-%d %H:%M") == "2026-09-16 00:30"


def test_github_commit_tarihi_kaynak_zaman():
    def isleyici(istek):
        return httpx.Response(200, json=[
            {"commit": {"message": "İkinci", "author": {"date": "2026-09-16T11:37:00Z"}}},
            {"commit": {"message": "İlk", "author": {"date": "2026-09-16T09:12:00+03:00"}}},
            {"commit": {"message": "Tarihsiz"}},
        ])

    sonuc = servisler.github_tara("t", "ufuk/medusa", BUGUN, httpx.Client(transport=httpx.MockTransport(isleyici)))
    zamanlar = [(m["metin"], m["kaynak_zaman"] and m["kaynak_zaman"].strftime("%H:%M")) for m in sonuc]
    assert zamanlar == [("Tarihsiz", None), ("İlk", "09:12"), ("İkinci", "14:37")]
    assert servisler.commit_zamani({"commit": {"author": {"date": "bozuk"}}}) is None
