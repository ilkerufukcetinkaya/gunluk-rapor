"""Günlük rapor için veri kaynakları: Gmail (gönderilenler), GitHub (proje commit'leri), Google Takvim ve Drive,
Microsoft Graph (Outlook, Takvim/Teams, OneDrive), Claude (iş diline çeviri)."""
from __future__ import annotations

import base64
import email
import hashlib
import imaplib
import json
import logging
import os
import re
import secrets
import smtplib
import threading
from datetime import date, datetime, time, timedelta, timezone
from email.header import Header, decode_header, make_header
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, getaddresses, parseaddr, parsedate_to_datetime
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pywebpush import WebPushException, webpush

ISTANBUL = ZoneInfo("Europe/Istanbul")
log = logging.getLogger("gunluk-rapor")

KURUMLAR = {
    "mesam.org.tr": "MESAM",
    "msg.org.tr": "MSG",
    "imro.ie": "IMRO",
    "coverz": "Coverz",
    "ilsvision.com": "şirket içi",
}
SIRKET_ICI = "şirket içi"
BIR_KISI = "bir kişi"  # genel sağlayıcıdaki, görünen adı olmayan alıcı
EKIP_ICI = "ekip içi"  # E2: tüm alıcıları kullanıcının kendi şirketinden olan e-posta
# Herkese açık posta sağlayıcıları (tek liste): kullanıcının adresi buradaysa alan adı "kendi şirketi" sayılmaz (E2);
# alıcının adresi buradaysa maddede alan adı yerine görünen adı yazılır, ad yoksa "bir kişi" (A2).
GENEL_SAGLAYICILAR = frozenset({
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.com.tr", "outlook.com", "outlook.com.tr", "live.com",
    "msn.com", "yahoo.com", "yahoo.com.tr", "icloud.com", "me.com", "mac.com", "yandex.com", "yandex.com.tr",
    "proton.me", "protonmail.com", "aol.com", "gmx.com", "mail.com",
})

CLAUDE_MODEL = "claude-sonnet-5"

# Kurulum sihirbazındaki hazır sürekli iş listeleri; " | " dönüşümlü ifadeleri ayırır.
SUREKLI_SABLONLAR = [
    {"ad": "Telif ve meslek birlikleri", "maddeler": [
        "MESAM / MSG / IMRO yazışmaları takip edildi | Meslek birlikleriyle günlük yazışma ve takip yapıldı",
        "CRD raporları kontrol edildi",
        "Telif takibi ve eşleşme kontrolleri yapıldı",
        "Eser bildirimi talepleri incelenip yanıtlandı",
    ]},
    {"ad": "Lisanslama", "maddeler": [
        "Gelen lisans talepleri değerlendirildi",
        "Lisans sözleşmeleri ve onaylar takip edildi",
        "Cue sheet ve kullanım bildirimleri kontrol edildi",
        "Müşteri/yapımcı yazışmaları yürütüldü",
    ]},
    {"ad": "Genel / idari", "maddeler": [
        "Gelen e-postalar yanıtlandı ve takip edildi",
        "Ekip içi koordinasyon sağlandı",
        "Günlük iş listesi güncellendi",
        "Bekleyen konular takip edildi",
    ]},
]


EPOSTA_KURALI = (
    "E-posta maddelerinde konu başlığını anlamını koruyarak cümlede tut; birden çok maddeyi birleştirme; "
    "sayı ekleme ya da çıkarma yapma."
)
GOOGLE_KURALI = (
    "Toplantı ('takvim') ve dosya ('drive') maddelerinde tırnak içindeki toplantı ve dosya adını aynen koru, "
    "katılımcı kurum adlarını değiştirme."
)


def kendi_sirket_kurali(kendi_sirket: list[str] | None) -> str:
    """kendi_sirket: kullanıcının kendi şirketinin adları ve alan adları; boşsa kural yazılmaz."""
    if not kendi_sirket:
        return ""
    return (
        f"Kullanıcının kendi şirketi ({', '.join(kendi_sirket)}) alıcı, toplantı katılımcısı, bilgilendirilen ya da paylaşılan taraf olarak "
        "ASLA yazılmaz; \"… ile paylaşıldı\", \"… de bilgilendirildi\" gibi kendi şirketine yapılan atıfları cümleden çıkar "
        "(olgu çıkarmama kuralının tek istisnası budur)."
    )


def claude_sistem(proje_adi: str = "", kendi_sirket: list[str] | None = None) -> str:
    urun = f"ürün adı her zaman {proje_adi}. " if proje_adi else ""
    kendi = kendi_sirket_kurali(kendi_sirket)
    return (
        "Bir müzik edisyon şirketinde çalışan bir danışmanın günlük raporu için maddeler yazıyorsun. "
        "Teknik terimleri (trigram, indeks, rollup, N+1, commit, endpoint vb.) yöneticinin anlayacağı iş diline çevir; "
        "her girdi için TEK cümle, geçmiş zaman, abartı yok, uydurma yok. "
        + EPOSTA_KURALI + " " + GOOGLE_KURALI + " " + TIRNAK_KURALI + " "
        + (kendi + " " if kendi else "")
        + urun
        + "Yalnız JSON dizi döndür: [{\"id\":..., \"metin\":...}]"
    )


def istanbul_bugun() -> date:
    return datetime.now(ISTANBUL).date()


def madde_id(kaynak: str, metin: str) -> str:
    return hashlib.sha1((kaynak + metin).encode("utf-8")).hexdigest()[:10]


def madde(kaynak: str, metin: str, zaman: datetime | None = None) -> dict:
    """zaman: kaynağın Istanbul saatiyle zamanı (e-postanın gönderildiği, commit'in yazıldığı an)."""
    return {"id": madde_id(kaynak, metin), "metin": metin, "kaynak": kaynak, "kaynak_zaman": zaman}


def tekille(maddeler: list[dict]) -> list[dict]:
    goruldu, sonuc = set(), []
    for m in maddeler:
        if m["id"] not in goruldu:
            goruldu.add(m["id"])
            sonuc.append(m)
    return sonuc


# ---------------------------------------------------------------- Türkçe ek

UNLULER = "aeıioöuü"
KALIN_UNLULER = "aıou"
# Harfle okunan kısaltmalarda (MSG, CRD) son harfin okunuşu; listede olmayan ünsüzler "+e" okunur.
HARF_OKUNUSU = {"Q": "kü", "W": "ve", "X": "iks"}
# Yazılışından farklı okunan yabancı adlar: son kelime → ek ünlüsü.
EK_ISTISNALARI = {"universal": "e"}


def _kucult(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def _okunus(ad: str) -> str:
    """Ek uyumu için adın son kelimesinin okunuşu (küçük harf): 'MSG' → 'ge', 'MESAM' → 'mesam'."""
    ad = ad.strip()
    kelimeler = [k for k in re.split(r"[\s.\-_/]+", ad) if k]
    son = kelimeler[-1] if kelimeler else ad
    harfler = "".join(h for h in son if h.isalpha())
    if harfler and harfler.isupper() and not any(h in UNLULER for h in _kucult(harfler)):
        return HARF_OKUNUSU.get(harfler[-1], _kucult(harfler[-1]) + "e")
    return _kucult(harfler)


def _son_unlu(okunus: str) -> str:
    return EK_ISTISNALARI.get(okunus) or next((h for h in reversed(okunus) if h in UNLULER), "e")


# A3: alan adının son parçasının okunuşu. Listede olmayan uzantı son harfinin okunuşuyla çekimlenir (uk → "ke").
ALAN_UZANTISI_OKUNUSU = {"tr": "tere", "com": "kom", "net": "net", "org": "org", "io": "io", "edu": "edu", "gov": "gov",
                         "info": "info", "biz": "biz", "co": "ko"}
ALAN_ADI = re.compile(r"^[\w-]+(\.[\w-]+)*\.[a-z]{2,24}$", re.IGNORECASE)


def _harf_okunusu(harf: str) -> str:
    """Tek harfin Türkçe okunuşu: ünlü kendisi, ünsüz '+e' (Q, W, X istisna)."""
    kucuk = _kucult(harf)
    if kucuk in UNLULER:
        return kucuk
    return _kucult(HARF_OKUNUSU.get(_buyut(kucuk), kucuk + "e"))


def alan_adi_okunusu(ad: str) -> str | None:
    """Alan adıysa son parçasının okunuşu ('ornek.com.tr' → 'tere', 'firma.com' → 'kom'); değilse None."""
    if not ALAN_ADI.match(ad):
        return None
    uzanti = _kucult(ad.rpartition(".")[2])
    return ALAN_UZANTISI_OKUNUSU.get(uzanti) or _harf_okunusu(uzanti[-1])


def yonelme_eki(ad: str) -> str:
    """'MSG' → "MSG'ye", 'MESAM' → "MESAM'a", 'IMRO' → "IMRO'ya", 'Coverz' → "Coverz'e"; 'bir kişi' → "bir kişiye".
    Alan adında ek son parçanın okunuşuna uyar: 'ornek.com.tr' → "ornek.com.tr'ye", 'firma.com' → "firma.com'a"."""
    ad = ad.strip()
    if ad == BIR_KISI:  # özel ad değil: kesme işareti yok
        return ad + "ye"
    okunus = alan_adi_okunusu(ad) or _okunus(ad)
    if okunus in EK_ISTISNALARI:
        return f"{ad}'{EK_ISTISNALARI[okunus]}"
    unlu = "a" if _son_unlu(okunus) in KALIN_UNLULER else "e"
    kaynastirma = "y" if okunus and okunus[-1] in UNLULER else ""
    return f"{ad}'{kaynastirma}{unlu}"


SERT_UNSUZLER = "fstkçşhp"
DORTLU_UNLU = {"a": "ı", "ı": "ı", "e": "i", "i": "i", "o": "u", "u": "u", "ö": "ü", "ü": "ü"}


def _buyut(s: str) -> str:
    return s.replace("i", "İ").replace("ı", "I").upper()


def ek_uyumu(ek: str, kaynak: str, hedef: str) -> str:
    """Kesme işaretinden sonraki eki kaynak addan hedef ada taşır: ünlü uyumu (a/e, ı/i/u/ü), sertleşme (da/ta) ve
    kaynaştırma harfi hedefin son sesine göre yeniden kurulur. Kaynağın kaynaştırma harfi (Medusa'nın, MSG'ye) atılır.
    Hedef iyelik ekli bir tamlamaysa (Edisyon uygulaması) durum ekleri n'li olur: 'da' → 'nda', 'nın' → 'nın', 'a' → 'na'.
    ek_uyumu('da', 'MEDUSA', 'Edisyon uygulaması') → 'nda'; ek_uyumu('ya', 'Medusa', 'Coverz') → 'e'."""
    kucuk = _kucult(ek)
    k_okunus = _okunus(kaynak)
    cekirdek = kucuk
    if (k_okunus and k_okunus[-1] in UNLULER and len(kucuk) >= 2 and kucuk[0] in "yn"
            and (kucuk[1] in UNLULER or (kucuk[0] == "n" and kucuk[1] in "dt") or (kucuk[0] == "y" and kucuk[1] == "l"))):
        cekirdek = kucuk[1:]
    if not cekirdek:
        return ek
    h_okunus = _okunus(hedef)
    h_unluyle = bool(h_okunus) and h_okunus[-1] in UNLULER
    tamlama = h_unluyle and len(hedef.split()) >= 2 and h_okunus[-1] in "ıiuü"
    onek = ""
    if tamlama and (cekirdek[0] in UNLULER or cekirdek[0] in "dt"):
        onek = "n"
    elif h_unluyle and cekirdek[0] in UNLULER:
        onek = "n" if re.match(r"[ıiuü]n", cekirdek) else "y"
    elif h_unluyle and cekirdek in ("la", "le"):
        onek = "y"
    unlu, onceki, sonuc = _son_unlu(h_okunus), (h_okunus[-1:] or "e"), []
    for n, h in enumerate(onek + cekirdek):
        if (onek + cekirdek)[n:] == "ki" and n:
            sonuc.append("ki")
            break
        if h in "dt":
            h = "t" if onceki in SERT_UNSUZLER else "d"
        elif h in "ae":
            h = "a" if unlu in KALIN_UNLULER else "e"
        elif h in "ıiuü":
            h = DORTLU_UNLU[unlu]
        if h in UNLULER:
            unlu = h
        sonuc.append(h)
        onceki = h
    cikti = "".join(sonuc)
    return _buyut(cikti) if ek.isupper() else cikti


# ---------------------------------------------------------------- ad eşlemeleri ve tırnak içi koruma

# Aynı sınırlar arayüzde (index.html adlariEsle) de kullanılır.
_ONCE_SINIR = r"(?<!\w)"
_SONRA_EK = r"(?:(?P<ap>['’])(?P<ek>[^\W\d_]+))?(?!\w)"


def _harf_deseni(h: str) -> str:
    """Büyük/küçük harf ve Türkçe İ/ı duyarsız tek karakter deseni."""
    if h in "iİıI":
        return "[iİıI]"
    if h.isspace():
        return r"\s+"
    esler = {x for x in (h, h.lower(), h.upper()) if len(x) == 1}
    if len(esler) == 1:
        return re.escape(h)
    return "[" + "".join(sorted(re.escape(x) for x in esler)) + "]"


def _esleme_deseni(eslemeler: tuple[tuple[str, str], ...]) -> re.Pattern:
    parcalar = [f"(?P<e{n}>{''.join(_harf_deseni(h) for h in kaynak)})" for n, (kaynak, _) in enumerate(eslemeler)]
    return re.compile(_ONCE_SINIR + "(?:" + "|".join(parcalar) + ")" + _SONRA_EK)


def _esleme_listesi(eslemeler) -> tuple[tuple[str, str], ...]:
    """[{kaynak, hedef}] → uzun kaynak önce sıralı (kaynak, hedef) çiftleri; bozuk satırlar atlanır."""
    ciftler = []
    for e in eslemeler or []:
        if isinstance(e, dict) and isinstance(e.get("kaynak"), str) and e["kaynak"].strip():
            ciftler.append((re.sub(r"\s+", " ", e["kaynak"]).strip(), str(e.get("hedef") or "").strip()))
    return tuple(sorted(ciftler, key=lambda c: -len(c[0])))


def _bosluklari_temizle(metin: str) -> str:
    """Silinen adın ardından: boş tırnak/parantez, çift boşluk, noktalamadan önceki ve satır başı/sonu boşlukları."""
    metin = re.sub(r"(?<!\w)'[ \t]*'(?!\w)", "", metin)
    metin = re.sub(r"\([ \t]*\)", "", metin)
    metin = re.sub(r"[ \t]{2,}", " ", metin)
    metin = re.sub(r"[ \t]+([,.;:!?)])", r"\1", metin)
    return re.sub(r"(?m)^[ \t]+|[ \t]+$", "", metin)


def ad_esle(metin: str | None, eslemeler) -> str:
    """Ad eşlemelerini uygular: kelime sınırıyla, büyük/küçük harf ve İ/ı duyarsız, uzun kaynak önce; kesme işaretli
    ek hedefe uydurulur (ek_uyumu). Hedef boşsa ad ekiyle birlikte silinir ve kalan boşluklar temizlenir.
    Tek geçişte uygulanır; hedefler kaynak içermediği için (Ayarlar'da denetlenir) ikinci uygulama bir şey değiştirmez.
    Tırnak içindeki metne de uygulanır: tırnak içi koruma kuralının tek istisnası budur."""
    if not metin:
        return metin or ""
    ciftler = _esleme_listesi(eslemeler)
    if not ciftler:
        return metin
    silindi = False

    def degistir(m: re.Match) -> str:
        nonlocal silindi
        n = next(i for i in range(len(ciftler)) if m.group(f"e{i}") is not None)
        hedef = ciftler[n][1]
        if not hedef:
            silindi = True
            return ""
        if m.group("ek"):
            return hedef + m.group("ap") + ek_uyumu(m.group("ek"), m.group(f"e{n}"), hedef)
        return hedef

    sonuc = _esleme_deseni(ciftler).sub(degistir, metin)
    return _bosluklari_temizle(sonuc) if silindi else sonuc


def esleme_iceriyor(metin: str, eslemeler) -> bool:
    """Metinde eşlenecek bir kaynak ad geçiyor mu."""
    ciftler = _esleme_listesi(eslemeler)
    return bool(ciftler) and bool(_esleme_deseni(ciftler).search(metin or ""))


TIRNAK_KURALI = (
    "Tek tırnak ('…') içindeki metni harfiyen koru: kelime, harf, büyük/küçük harf ve noktalama değişmez, "
    "proje adı kuralı uygulanmaz, tırnaklar silinmez."
)
_TIRNAK_ACILIS_ONCESI = "([{\"“«/—-"


def tirnak_parcalari(metin: str | None) -> list[str]:
    """Tek tırnak içindeki parçalar. Açılış tırnağı metin başında ya da boşluk/açılış işaretinden sonra gelir; ek
    kesme işareti (MSG'ye) açılış sayılmaz. Kapanış: bir sonraki açılıştan önceki, ardından harf gelmeyen ilk tırnak;
    yoksa ilk tırnak ('Ağustos raporu'nu → 'Ağustos raporu')."""
    metin = metin or ""
    parcalar, i = [], 0
    acilis = lambda j: metin[j] == "'" and (j == 0 or metin[j - 1].isspace() or metin[j - 1] in _TIRNAK_ACILIS_ONCESI)  # noqa: E731
    while True:
        bas = next((j for j in range(i, len(metin)) if acilis(j)), None)
        if bas is None:
            return parcalar
        adaylar = []
        for j in range(bas + 1, len(metin)):
            if acilis(j):
                break
            if metin[j] == "'":
                adaylar.append(j)
        if not adaylar:
            i = bas + 1
            continue
        son = next((j for j in adaylar if j + 1 == len(metin) or not (metin[j + 1].isalnum() or metin[j + 1] == "_")), adaylar[0])
        if metin[bas + 1:son].strip():
            parcalar.append(metin[bas + 1:son])
        i = son + 1


def tirnaklar_korundu(parcalar: list[str], cikti: str) -> bool:
    """Her parça çıktıda tırnaklarıyla aynen geçiyor mu."""
    return all(f"'{p}'" in (cikti or "") for p in parcalar)


def surekli_tirnaklari(metin: str) -> list[str]:
    """Sürekli işte '|' ile ayrılmış her ifadede ortak olan tırnaklı parçalar (Claude hangisini seçerse seçsin)."""
    ifadeler = [p for p in metin.split("|") if p.strip()] or [metin]
    ortak = set(tirnak_parcalari(ifadeler[0]))
    for p in ifadeler[1:]:
        ortak &= set(tirnak_parcalari(p))
    return sorted(ortak)


# ---------------------------------------------------------------- e-posta

ONEK = re.compile(r"^\s*((re|fwd?|ynt|ilt|İLT|aw|wg)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)
NOREPLY = re.compile(r"no-?reply|do-?not-?reply", re.IGNORECASE)


def basligi_coz(deger: str | None) -> str:
    if not deger:
        return ""
    try:
        metin = str(make_header(decode_header(deger)))
    except Exception:
        metin = deger
    return re.sub(r"\s+", " ", metin).strip()


def konu_temizle(konu: str) -> str:
    return ONEK.sub("", konu).strip()


def _alan_eslesir(alan: str, anahtar: str) -> bool:
    if "." in anahtar:
        return alan == anahtar or alan.endswith("." + anahtar)
    return anahtar in alan.split(".")


def kurum_adi(gorunen_ad: str, adres: str, sozluk: dict[str, str] | None = None) -> str:
    """Sözlükteki kurum; yoksa görünen ad, o da yoksa alan adı. Alan adı genel posta sağlayıcısıysa (gmail.com,
    hotmail.com …) alan adı yazılmaz: görünen ad, yoksa "bir kişi". Adres biçimindeki görünen ad (Outlook/Graph adı
    boşken adresi verir) yok sayılır."""
    alan = adres.rpartition("@")[2].lower()
    for anahtar, kurum in (KURUMLAR if sozluk is None else sozluk).items():
        if _alan_eslesir(alan, anahtar):
            return kurum
    ad = gorunen_ad.strip().strip('"').strip()
    if "@" in ad:
        ad = ""
    if alan in GENEL_SAGLAYICILAR:
        return ad or BIR_KISI
    return ad or alan or adres


def adres_alani(adres: str) -> str:
    return adres.rpartition("@")[2].strip().lower()


def kendi_alanlari(adresler: list[str], ekler: list[str] | None = None, sozluk: dict[str, str] | None = None) -> list[str]:
    """Kullanıcının kendi şirketinin alan adları: giriş ve Gmail adreslerinin alan adları (herkese açık sağlayıcılar
    hariç), sözlükte 'şirket içi' eşlenenler ve elle eklenenler. Alt alan adları eşleşmede kendiliğinden kapsanır."""
    otomatik = [adres_alani(a) for a in adresler if a and "@" in a]
    otomatik = [a for a in otomatik if a and a not in GENEL_SAGLAYICILAR]
    sirket_ici = [k for k, v in (KURUMLAR if sozluk is None else sozluk).items() if _kucult(v.strip()) == SIRKET_ICI]
    return list(dict.fromkeys(otomatik + sirket_ici + list(ekler or [])))


def kendi_mi(adres: str, kendi_alanlar: list[str]) -> bool:
    alan = adres_alani(adres)
    return any(_alan_eslesir(alan, k) for k in kendi_alanlar)


def kendi_sirket_adlari(kendi_alanlar: list[str], sozluk: dict[str, str] | None = None) -> list[str]:
    """Prompt'lar için: kendi alan adlarının sözlükteki kurum adları ('şirket içi' hariç) ve alan adlarının kendisi."""
    adlar = []
    for alan in kendi_alanlar:
        for anahtar, kurum in (KURUMLAR if sozluk is None else sozluk).items():
            if _alan_eslesir(alan, anahtar) and _kucult(kurum.strip()) != SIRKET_ICI:
                adlar.append(kurum.strip())
        adlar.append(alan)
    return list(dict.fromkeys(adlar))


def istanbul_zamani(date_basligi: str | None) -> datetime | None:
    """E-posta Date başlığı → Istanbul saatiyle zaman; okunamazsa None."""
    if not date_basligi:
        return None
    try:
        zaman = parsedate_to_datetime(date_basligi)
    except (TypeError, ValueError):
        return None
    if zaman.tzinfo is None:
        zaman = zaman.replace(tzinfo=timezone.utc)
    return zaman.astimezone(ISTANBUL)


def bugun_mu(date_basligi: str | None, bugun: date) -> bool:
    zaman = istanbul_zamani(date_basligi)
    return zaman is not None and zaman.date() == bugun


def _alicilar(
    ham: str | None, kendi_adres: str, sozluk: dict[str, str] | None = None, kendi_alanlar: list[str] | None = None,
) -> list[str]:
    """Alıcı kurumları; kendi_alanlar verilirse kendi şirketindeki alıcılar EKIP_ICI olarak döner."""
    kurumlar = []
    for ad, adres in getaddresses([ham or ""]):
        adres = adres.strip()
        if not adres or adres.lower() == kendi_adres.lower() or NOREPLY.search(adres):
            continue
        if kendi_alanlar is not None and kendi_mi(adres, kendi_alanlar):
            kurum = EKIP_ICI
        else:
            kurum = kurum_adi(basligi_coz(ad), adres, sozluk)
        if kurum not in kurumlar:
            kurumlar.append(kurum)
    return kurumlar


def _hedef(kurumlar: tuple[str, ...]) -> str:
    if kurumlar == (SIRKET_ICI,):
        return "Şirket içi"
    if kurumlar == (EKIP_ICI,):
        return "Ekip içi"
    adlar = list(kurumlar)
    hedef = yonelme_eki(adlar[0]) if len(adlar) == 1 else ", ".join(adlar[:-1]) + " ve " + yonelme_eki(adlar[-1])
    return "B" + hedef[1:] if hedef.startswith(BIR_KISI) else hedef  # cümle başı


def eposta_metni(kurumlar: tuple[str, ...], konular: list[str]) -> str:
    hedef = _hedef(kurumlar)
    farkli = list(dict.fromkeys(k or "(konusuz)" for k in konular))
    if len(konular) == 1:
        if not konular[0]:
            return f"{hedef} konusuz bir e-posta gönderildi"
        return f"{hedef} '{konular[0]}' konulu e-posta gönderildi"
    if len(farkli) == 1:
        return f"{hedef} '{farkli[0]}' konulu {len(konular)} e-posta gönderildi"
    return f"{hedef} {len(konular)} e-posta gönderildi (konular: {'; '.join(farkli)})"


GRUPLAMALAR = ("konu", "alici")


def konu_anahtari(konu: str) -> str:
    """Re:/Fwd:/YNT:/İLT: önekleri, büyük/küçük harf ve boşluk farkı yok sayılır."""
    return re.sub(r"\s+", " ", _kucult(konu_temizle(konu))).strip()


def konu_maddesi_id(kurum: str, konu: str, bugun: date) -> str:
    return hashlib.sha1((kurum + konu_anahtari(konu) + bugun.isoformat()).encode("utf-8")).hexdigest()[:10]


def konu_metni(kurum: str, konu: str, adet: int, digerleri: list[str]) -> str:
    hedef = _hedef((kurum,))
    ne = f"{adet} e-posta" if adet > 1 else "e-posta"
    metin = f"{hedef} '{konu}' konulu {ne} gönderildi" if konu else (
        f"{hedef} konusuz {adet} e-posta gönderildi" if adet > 1 else f"{hedef} konusuz bir e-posta gönderildi")
    return metin + (f" (ayrıca {', '.join(digerleri)})" if digerleri else "")


def _dis_kurumlar(kime: list[str], bilgi: list[str], ilk_dolu: bool = False) -> list[str]:
    """Kendi şirketi (EKIP_ICI) çıkarılır: önce To'daki, sonra Cc'deki kendi-olmayan kurumlar.
    ilk_dolu: To'da kendi-olmayan varsa Cc'ye bakılmaz. Hiç kendi-olmayan yoksa ve kendi şirketinden alıcı
    varsa [EKIP_ICI], hiç alıcı yoksa []."""
    disi_kime = [k for k in kime if k != EKIP_ICI]
    disi_bilgi = [k for k in bilgi if k != EKIP_ICI]
    kurumlar = disi_kime if ilk_dolu and disi_kime else list(dict.fromkeys(disi_kime + disi_bilgi))
    if kurumlar:
        return kurumlar
    return [EKIP_ICI] if EKIP_ICI in kime + bilgi else []


def epostalari_maddele(
    mailler: list[dict], kendi_adres: str, bugun: date, sozluk: dict[str, str] | None = None, gruplama: str = "konu",
    kendi_alanlar: list[str] | None = None, ekip_ici_atla: bool = True,
) -> list[dict]:
    """mailler: {"date","subject","from","to","cc"} ham başlık değerleri.
    gruplama 'konu': gün + ilk To kurumu + temizlenmiş konu başına bir madde; 'alici': alıcı kurum(lar) başına bir madde.
    kendi_alanlar verilirse (E2) bu alanlardaki alıcılar esas kurumda ve "(ayrıca …)" ekinde yer almaz; tüm alıcıları
    kendi şirketinden olan mail "Ekip içi …" maddesi olur, ekip_ici_atla ise hiç madde üretmez."""
    if gruplama != "alici":
        return _konu_basina_maddele(mailler, kendi_adres, bugun, sozluk, kendi_alanlar, ekip_ici_atla)
    gruplar: dict[tuple[str, ...], list[str]] = {}
    son_zaman: dict[tuple[str, ...], datetime] = {}
    for m in mailler:
        zaman = istanbul_zamani(m.get("date"))
        if zaman is None or zaman.date() != bugun:
            continue
        if NOREPLY.search(m.get("from") or ""):
            continue
        if kendi_alanlar is not None:
            kurumlar = _dis_kurumlar(_alicilar(m.get("to"), kendi_adres, sozluk, kendi_alanlar),
                                     _alicilar(m.get("cc"), kendi_adres, sozluk, kendi_alanlar), ilk_dolu=True)
            if kurumlar == [EKIP_ICI] and ekip_ici_atla:
                continue
        else:
            kurumlar = _alicilar(m.get("to"), kendi_adres, sozluk) or _alicilar(m.get("cc"), kendi_adres, sozluk)
        if len(kurumlar) > 1 and SIRKET_ICI in kurumlar:
            kurumlar.remove(SIRKET_ICI)
        if not kurumlar:
            continue  # kendine gönderilen veya yalnız noreply adreslerine giden
        anahtar = tuple(kurumlar)
        gruplar.setdefault(anahtar, []).append(konu_temizle(basligi_coz(m.get("subject"))))
        son_zaman[anahtar] = max(zaman, son_zaman.get(anahtar, zaman))  # grupta en son mailin saati
    return tekille([madde("eposta", eposta_metni(k, konular), son_zaman[k]) for k, konular in gruplar.items()])


def _konu_basina_maddele(
    mailler: list[dict], kendi_adres: str, bugun: date, sozluk: dict[str, str] | None,
    kendi_alanlar: list[str] | None = None, ekip_ici_atla: bool = True,
) -> list[dict]:
    gruplar: dict[tuple[str, str], dict] = {}
    for m in mailler:
        zaman = istanbul_zamani(m.get("date"))
        if zaman is None or zaman.date() != bugun:
            continue
        if NOREPLY.search(m.get("from") or ""):
            continue
        kime = _alicilar(m.get("to"), kendi_adres, sozluk, kendi_alanlar)
        bilgi = _alicilar(m.get("cc"), kendi_adres, sozluk, kendi_alanlar)
        if kendi_alanlar is not None:
            kurumlar = _dis_kurumlar(kime, bilgi)
            if kurumlar == [EKIP_ICI] and ekip_ici_atla:
                continue
        else:
            kurumlar = list(dict.fromkeys(kime + bilgi))
        if len(kurumlar) > 1 and SIRKET_ICI in kurumlar:
            kurumlar.remove(SIRKET_ICI)
        if not kurumlar:
            continue  # kendine gönderilen veya yalnız noreply adreslerine giden
        esas = kurumlar[0]  # ilk To alıcısının kurumu; To boşsa ilk Cc
        konu = konu_temizle(basligi_coz(m.get("subject")))
        g = gruplar.setdefault((esas, konu_anahtari(konu)), {"konu": konu, "ilk": zaman, "son": zaman, "adet": 0, "diger": []})
        g["adet"] += 1
        if zaman < g["ilk"]:  # konu yazımı gündeki ilk mailden alınır; tarama sırasından bağımsız
            g["konu"], g["ilk"] = konu, zaman
        g["son"] = max(g["son"], zaman)
        g["diger"] += [k for k in kurumlar[1:] if k not in g["diger"]]
    return [
        {"id": konu_maddesi_id(esas, g["konu"], bugun), "metin": konu_metni(esas, g["konu"], g["adet"], g["diger"]),
         "kaynak": "eposta", "kaynak_zaman": g["son"]}
        for (esas, _), g in gruplar.items()
    ]


NOT_ONEKI = re.compile(r"^\s*(rapor|not)\s*:\s*", re.IGNORECASE)
NOT_EN_COK_SATIR = 10


def _kendine_mi(m: dict, kendi_adres: str) -> bool:
    """Alıcıların (To + Cc) hepsi kullanıcının kendisi mi."""
    adresler = [a.strip().lower() for _, a in getaddresses([m.get("to") or "", m.get("cc") or ""]) if a.strip()]
    return bool(adresler) and all(a == kendi_adres.lower() for a in adresler)


def not_konusu(m: dict, kendi_adres: str) -> str | None:
    """Kendine gönderilen 'rapor:' / 'not:' konulu mailde önekten sonraki kısım (boş olabilir); not değilse None."""
    if not _kendine_mi(m, kendi_adres):
        return None
    eslesme = NOT_ONEKI.match(basligi_coz(m.get("subject")))
    return basligi_coz(m.get("subject"))[eslesme.end():].strip() if eslesme else None


def notlari_maddele(mailler: list[dict], kendi_adres: str, bugun: date) -> list[dict]:
    """mailler: {"date","subject","to","cc","message-id","govde"}. Konu önekten sonra doluysa tek madde,
    yalnız önekse gövdenin boş olmayan satırları (ilk 10). id, Message-ID'den türer; ikinci taramada aynı çıkar."""
    maddeler = []
    for m in mailler:
        zaman = istanbul_zamani(m.get("date"))
        if zaman is None or zaman.date() != bugun:
            continue
        konu = not_konusu(m, kendi_adres)
        if konu is None:
            continue
        satirlar = [konu] if konu else [x.strip() for x in (m.get("govde") or "").splitlines() if x.strip()][:NOT_EN_COK_SATIR]
        kimlik = (m.get("message-id") or "").strip() or f"{m.get('date')}|{m.get('subject')}"
        for sira, satir in enumerate(satirlar):
            maddeler.append({
                "id": "not-" + hashlib.sha1(f"{kimlik}#{sira}".encode("utf-8")).hexdigest()[:20],
                "metin": satir, "kaynak": "not", "kaynak_zaman": zaman,
            })
    return maddeler


def duz_metin_govde(msg: email.message.Message) -> str:
    """İlk text/plain parçası; yoksa boş."""
    for parca in msg.walk() if msg.is_multipart() else [msg]:
        if parca.get_content_type() == "text/plain" and not parca.get_filename():
            veri = parca.get_payload(decode=True) or b""
            return veri.decode(parca.get_content_charset() or "utf-8", "replace")
    return ""


def _mutf7_coz(s: str) -> str:
    def coz(m: re.Match) -> str:
        parca = m.group(1)
        if not parca:
            return "&"
        parca = parca.replace(",", "/")
        parca += "=" * (-len(parca) % 4)
        return base64.b64decode(parca).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", coz, s)


LIST_SATIRI = re.compile(r'^\((?P<bayraklar>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<ad>.+)$')


def klasorleri_ayristir(list_ciktisi: list) -> list[tuple[str, str]]:
    """IMAP LIST çıktısı → [(bayraklar, ham klasör adı)]."""
    sonuc = []
    for satir in list_ciktisi:
        if isinstance(satir, tuple):
            satir = satir[0] + b" " + satir[1]
        if not isinstance(satir, bytes):
            continue
        m = LIST_SATIRI.match(satir.decode("utf-8", "replace"))
        if not m:
            continue
        ad = m.group("ad").strip()
        if ad.startswith('"') and ad.endswith('"'):
            ad = ad[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        sonuc.append((m.group("bayraklar"), ad))
    return sonuc


class KaynakHatasi(Exception):
    pass


AYLAR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _gmail_gonderilmis_ac(M: imaplib.IMAP4_SSL, kullanici: str, sifre: str) -> str:
    """Giriş yapar, Gönderilmiş klasörünü salt-okunur seçer; klasörün okunur adını döner."""
    try:
        M.login(kullanici, sifre)
    except imaplib.IMAP4.error as e:
        raise KaynakHatasi("Gmail'e giriş yapılamadı (kullanıcı adı veya uygulama şifresi hatalı)") from e

    _, liste = M.list()
    klasorler = klasorleri_ayristir(liste)
    gonderilmis = next((ad for bayrak, ad in klasorler if "\\sent" in bayrak.lower()), None)
    if gonderilmis is None:
        adlar = ", ".join(_mutf7_coz(ad) for _, ad in klasorler)
        raise KaynakHatasi(f"Gmail'de Gönderilmiş klasörü bulunamadı. Klasörler: {adlar}")

    tur, _ = M.select('"' + gonderilmis.replace("\\", "\\\\").replace('"', '\\"') + '"', readonly=True)
    if tur != "OK":
        raise KaynakHatasi(f"Gmail klasörü açılamadı: {_mutf7_coz(gonderilmis)}")
    return _mutf7_coz(gonderilmis)


def _gmail_oturumu(kullanici: str, sifre: str, islem):
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    except OSError as e:
        raise KaynakHatasi(f"Gmail'e bağlanılamadı: {e}") from e
    try:
        _gmail_gonderilmis_ac(M, kullanici, sifre)
        return islem(M)
    except KaynakHatasi:
        raise
    except (imaplib.IMAP4.error, OSError) as e:
        raise KaynakHatasi(f"Gmail okunurken hata oluştu: {e}") from e
    finally:
        try:
            M.logout()
        except Exception:
            pass


def gmail_test(kullanici: str, sifre: str) -> str:
    _gmail_oturumu(kullanici, sifre, lambda M: None)
    return "Gmail: bağlandı, Gönderilmiş klasörü bulundu"


def imap_tarihi(gun: date) -> str:
    return f"{gun.day:02d}-{AYLAR[gun.month - 1]}-{gun.year}"


def gmail_tara(
    kullanici: str, sifre: str, bugun: date, sozluk: dict[str, str] | None = None, gruplama: str = "konu",
    kendi_alanlar: list[str] | None = None, ekip_ici_atla: bool = True,
) -> list[dict]:
    """Gönderilen e-posta maddeleri (kaynak 'eposta') + kendine atılan not maddeleri (kaynak 'not')."""
    def oku(M: imaplib.IMAP4_SSL) -> list[dict]:
        # IMAP tarihleri sunucu saatine göre: bir gün geriden başlanır, kesin süzgeç Date başlığının Istanbul günü.
        _, veri = M.search(None, "SINCE", imap_tarihi(bugun - timedelta(days=1)), "BEFORE", imap_tarihi(bugun + timedelta(days=1)))
        kimlikler = veri[0].split() if veri and veri[0] else []
        mailler = []
        if kimlikler:
            _, parcalar = M.fetch(b",".join(kimlikler).decode(), "(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT FROM TO CC MESSAGE-ID)])")
            for parca in parcalar:
                if not isinstance(parca, tuple):
                    continue
                msg = email.message_from_bytes(parca[1])
                m = {k: msg.get(k) for k in ("date", "subject", "from", "to", "cc", "message-id")}
                m["imap_id"] = parca[0].split()[0].decode()
                mailler.append(m)
        notlar, gonderilen = [], []
        for m in mailler:
            konu = not_konusu(m, kullanici)
            (gonderilen if konu is None else notlar).append(m)
            if konu == "" and bugun_mu(m.get("date"), bugun):  # yalnız önek: satırlar gövdede
                _, govde = M.fetch(m["imap_id"], "(BODY.PEEK[])")
                ham = next((g[1] for g in govde if isinstance(g, tuple)), b"")
                m["govde"] = duz_metin_govde(email.message_from_bytes(ham))
        return epostalari_maddele(gonderilen, kullanici, bugun, sozluk, gruplama, kendi_alanlar, ekip_ici_atla) \
            + notlari_maddele(notlar, kullanici, bugun)

    return _gmail_oturumu(kullanici, sifre, oku)


# ---------------------------------------------------------------- GitHub

def _github_commitleri(istemci: httpx.Client, token: str, repo: str, params: dict) -> list:
    yanit = istemci.get(
        f"https://api.github.com/repos/{repo}/commits",
        params=params,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if yanit.status_code == 401:
        raise KaynakHatasi("GitHub token'ı geçersiz veya süresi dolmuş")
    if yanit.status_code == 404:
        raise KaynakHatasi(f"GitHub reposu bulunamadı ya da token'ın erişimi yok: {repo}")
    if yanit.status_code >= 400:
        raise KaynakHatasi(f"GitHub hata döndürdü ({yanit.status_code}): {yanit.text[:200]}")
    return yanit.json()


def github_test(token: str, repo: str, istemci: httpx.Client | None = None) -> str:
    istemci = istemci or httpx.Client(timeout=20)
    try:
        commitler = _github_commitleri(istemci, token, repo, {"per_page": 100})
    except httpx.HTTPError as e:
        raise KaynakHatasi(f"GitHub'a bağlanılamadı: {e}") from e
    return f"GitHub: {len(commitler)}{'+' if len(commitler) == 100 else ''} commit görüldü"


def github_tara(token: str, repo: str, bugun: date, istemci: httpx.Client | None = None) -> list[dict]:
    """O günün Istanbul 00:00–24:00 aralığındaki commit'ler."""
    baslangic = datetime.combine(bugun, time.min, ISTANBUL).astimezone(timezone.utc)
    bitis = datetime.combine(bugun + timedelta(days=1), time.min, ISTANBUL).astimezone(timezone.utc)
    istemci = istemci or httpx.Client(timeout=20)
    params = {"since": baslangic.strftime("%Y-%m-%dT%H:%M:%SZ"), "until": bitis.strftime("%Y-%m-%dT%H:%M:%SZ"), "per_page": 100}
    commitler = []
    try:
        for sayfa in range(1, 6):
            parti = _github_commitleri(istemci, token, repo, {**params, "page": sayfa})
            commitler.extend(parti)
            if len(parti) < params["per_page"]:
                break
    except httpx.HTTPError as e:
        raise KaynakHatasi(f"GitHub'a bağlanılamadı: {e}") from e

    maddeler = []
    for c in reversed(commitler):  # API yeniden eskiye döner; raporda kronolojik olsun
        ilk_satir = (c.get("commit", {}).get("message") or "").splitlines()
        ilk_satir = ilk_satir[0].strip() if ilk_satir else ""
        if not ilk_satir or ilk_satir.startswith("Merge"):
            continue
        maddeler.append(madde("medusa", ilk_satir, commit_zamani(c)))
    return tekille(maddeler)


def commit_zamani(commit: dict) -> datetime | None:
    """Commit'in author tarihi (ISO 8601) → Istanbul saati; yoksa ya da okunamazsa None."""
    deger = ((commit.get("commit") or {}).get("author") or {}).get("date")
    if not isinstance(deger, str):
        return None
    try:
        zaman = datetime.fromisoformat(deger.replace("Z", "+00:00"))
    except ValueError:
        return None
    if zaman.tzinfo is None:
        zaman = zaman.replace(tzinfo=timezone.utc)
    return zaman.astimezone(ISTANBUL)


# ---------------------------------------------------------------- Google: OAuth, Gmail REST, Takvim, Drive

GOOGLE_YETKI_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_IPTAL_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
TAKVIM_API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
# kısa ad → scope; kaynaklar sözlüğündeki anahtarlarla aynı
GOOGLE_KAPSAMLARI = {
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "takvim": "https://www.googleapis.com/auth/calendar.readonly",
    "drive": "https://www.googleapis.com/auth/drive.metadata.readonly",
}
GOOGLE_SCOPE = "openid email " + " ".join(GOOGLE_KAPSAMLARI.values())
GOOGLE_TEST_SURESI = timedelta(days=7)  # test modundaki uygulamada refresh token 7 gün yaşar
GOOGLE_YENILE_MESAJI = "Google bağlantısı yenilenmeli"
GMAIL_BASLIKLARI = ("From", "To", "Cc", "Subject", "Date", "Message-ID")
SAYFA_SINIRI = GOOGLE_SAYFA_SINIRI = 10  # sayfalı listelerde en fazla sayfa (Google ve Graph)
DRIVE_SINIRI = 15  # Drive ve OneDrive


class YetkisizErisim(KaynakHatasi):
    """Access token reddedildi (401): o sağlayıcının önbellekteki token'ı atılmalı."""
    saglayici = ""


class GoogleHatasi(KaynakHatasi):
    pass


class GoogleYenilenmeli(GoogleHatasi):
    """Refresh token artık geçersiz (invalid_grant): kullanıcı yeniden bağlanmalı."""


class GoogleYetkisiz(GoogleHatasi, YetkisizErisim):
    saglayici = "google"


def google_istemci_bilgisi() -> tuple[str, str]:
    return (os.environ.get("GOOGLE_CLIENT_ID") or "").strip(), (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()


def google_ayarli() -> bool:
    """İki değişken de yoksa Google arayüzü hiç görünmez."""
    return all(google_istemci_bilgisi())


def google_test_modu() -> bool:
    """Varsayılan açık; uygulama Google'da yayın moduna geçince GOOGLE_TEST_MODU=0 yapılır."""
    return (os.environ.get("GOOGLE_TEST_MODU") or "1").strip() != "0"


def google_istemci() -> httpx.Client:
    return httpx.Client(timeout=20)


def google_kisa_kapsamlar(kapsamlar: list[str] | None) -> list[str]:
    verilen = set(kapsamlar or [])
    return [k for k, adres in GOOGLE_KAPSAMLARI.items() if adres in verilen]


def pkce_cifti() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge)."""
    dogrulayici = secrets.token_urlsafe(64)
    return dogrulayici, _b64url(hashlib.sha256(dogrulayici.encode()).digest())


def google_yetki_adresi(yonlendirme: str, state: str, challenge: str, login_hint: str = "") -> str:
    params = {
        "client_id": google_istemci_bilgisi()[0], "redirect_uri": yonlendirme, "response_type": "code",
        "scope": GOOGLE_SCOPE, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return GOOGLE_YETKI_URL + "?" + urlencode(params)


def _oauth_token_yaniti(url: str, veri: dict, istemci: httpx.Client, hata_sinifi: type, ad: str) -> tuple[int, dict]:
    """Token ucuna form isteği → (durum kodu, JSON gövde). Bağlantı hatası hata_sinifi olarak yükselir; gizli değer yazılmaz."""
    try:
        yanit = istemci.post(url, data=veri, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise hata_sinifi(f"{ad}'a bağlanılamadı ({e.__class__.__name__})") from e
    try:
        govde = yanit.json()
    except ValueError:
        govde = {}
    return yanit.status_code, govde if isinstance(govde, dict) else {}


def _google_token_istegi(veri: dict, istemci: httpx.Client) -> dict:
    """invalid_grant → GoogleYenilenmeli; diğer hatalar GoogleHatasi."""
    kod, govde = _oauth_token_yaniti(GOOGLE_TOKEN_URL, veri, istemci, GoogleHatasi, "Google")
    if kod >= 400 or not govde.get("access_token"):
        hata = govde.get("error") if isinstance(govde.get("error"), str) else ""
        if hata == "invalid_grant":
            raise GoogleYenilenmeli(GOOGLE_YENILE_MESAJI)
        raise GoogleHatasi(f"Google token vermedi ({hata or kod})")
    return govde


def id_token_epostasi(id_token: str | None, client_id: str) -> str:
    """Token ucundan TLS ile doğrudan gelen id_token'ın imzası ayrıca doğrulanmaz (OpenID Connect Core 3.1.3.7);
    aud bu uygulama olmalı."""
    try:
        yuk = (id_token or "").split(".")[1]
        veri = json.loads(base64.urlsafe_b64decode(yuk + "=" * (-len(yuk) % 4)))
    except (IndexError, ValueError) as e:
        raise GoogleHatasi("Google kimlik bilgisi okunamadı") from e
    if not isinstance(veri, dict) or (client_id and veri.get("aud") != client_id):
        raise GoogleHatasi("Google kimlik bilgisi bu uygulamaya ait değil")
    eposta = veri.get("email")
    if not isinstance(eposta, str) or "@" not in eposta:
        raise GoogleHatasi("Google e-posta adresini vermedi")
    return eposta.strip().lower()


def google_kod_takas(kod: str, dogrulayici: str, yonlendirme: str, istemci: httpx.Client | None = None) -> dict:
    """Yetki kodu → {access_token, expires_in, refresh_token, kapsamlar, eposta}."""
    client_id, secret = google_istemci_bilgisi()
    govde = _google_token_istegi({
        "code": kod, "client_id": client_id, "client_secret": secret, "redirect_uri": yonlendirme,
        "grant_type": "authorization_code", "code_verifier": dogrulayici,
    }, istemci or google_istemci())
    if not govde.get("refresh_token"):
        raise GoogleHatasi("Google yenileme anahtarı vermedi; yeniden bağlanın")
    return {
        "access_token": govde["access_token"], "expires_in": int(govde.get("expires_in") or 3600),
        "refresh_token": govde["refresh_token"], "kapsamlar": str(govde.get("scope") or "").split(),
        "eposta": id_token_epostasi(govde.get("id_token"), client_id),
    }


def google_yenile(refresh_token: str, istemci: httpx.Client | None = None) -> tuple[str, int]:
    """(access_token, saniye). Refresh token geçersizse GoogleYenilenmeli."""
    client_id, secret = google_istemci_bilgisi()
    govde = _google_token_istegi({
        "refresh_token": refresh_token, "client_id": client_id, "client_secret": secret, "grant_type": "refresh_token",
    }, istemci or google_istemci())
    return govde["access_token"], int(govde.get("expires_in") or 3600)


def google_iptal(token: str, istemci: httpx.Client | None = None) -> bool:
    """Google'daki izni geri alır; hata yükseltmez (bağlantı yine de bizde silinir)."""
    try:
        yanit = (istemci or google_istemci()).post(GOOGLE_IPTAL_URL, data={"token": token})
        return yanit.status_code < 400
    except Exception:
        return False


def _rest_get(istemci: httpx.Client, token: str, url: str, params, ad: str, saglayici: str, yetkisiz: type,
              basliklar: dict | None = None) -> dict:
    """Bearer token'lı GET → JSON sözlük. ad: 'Gmail' | 'Takvim' | 'Outlook' …, saglayici: 'Google' | 'Microsoft'
    (hata metinleri için). 401 → yetkisiz sınıfı; hata gövdesi {"error": {"message"}} (Google ve Graph aynı)."""
    try:
        yanit = istemci.get(url, params=params, headers={"Authorization": f"Bearer {token}", **(basliklar or {})})
    except httpx.HTTPError as e:
        raise KaynakHatasi(f"{ad} ({saglayici}) okunamadı: bağlantı hatası ({e.__class__.__name__})") from e
    if yanit.status_code == 401:
        raise yetkisiz(f"{ad} ({saglayici}) oturumu geçersiz; birazdan yeniden deneyin")
    if yanit.status_code >= 400:
        try:
            neden = yanit.json()["error"]["message"]
        except Exception:
            neden = yanit.text
        neden = re.sub(r"\s+", " ", str(neden or "")).strip()[:160]
        onek = f"{ad} ({saglayici}) erişimi reddedildi" if yanit.status_code == 403 else f"{ad} ({saglayici}) hata döndürdü"
        raise KaynakHatasi(f"{onek} ({yanit.status_code}{': ' + neden if neden else ''})")
    try:
        govde = yanit.json()
    except ValueError as e:
        raise KaynakHatasi(f"{ad} ({saglayici}) yanıtı okunamadı") from e
    return govde if isinstance(govde, dict) else {}


def _google_get(istemci: httpx.Client, token: str, url: str, params, ad: str) -> dict:
    """ad: 'Gmail' | 'Takvim' | 'Drive' (hata metinleri için)."""
    return _rest_get(istemci, token, url, params, ad, "Google", GoogleYetkisiz)


def gun_araligi(gun: date) -> tuple[datetime, datetime]:
    """Istanbul günü: [00:00, ertesi gün 00:00)."""
    return datetime.combine(gun, time.min, ISTANBUL), datetime.combine(gun + timedelta(days=1), time.min, ISTANBUL)


def rfc3339(zaman: datetime) -> str:
    return zaman.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_zaman(deger) -> datetime | None:
    """RFC 3339 → Istanbul saati; okunamazsa None."""
    if not isinstance(deger, str) or not deger:
        return None
    try:
        zaman = datetime.fromisoformat(deger.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (zaman if zaman.tzinfo else zaman.replace(tzinfo=timezone.utc)).astimezone(ISTANBUL)


def kaynak_kimligi(kimlik: str, gun: date) -> str:
    """Takvim etkinliği / Drive ya da OneDrive dosyası + gün: aynı gün her taramada aynı id."""
    return hashlib.sha1((kimlik + gun.isoformat()).encode("utf-8")).hexdigest()[:10]


def _google_sayfalari(istemci: httpx.Client, token: str, url: str, params: dict, ad: str, alan: str) -> list:
    ogeler, sayfa = [], None
    for _ in range(SAYFA_SINIRI):
        govde = _google_get(istemci, token, url, {**params, **({"pageToken": sayfa} if sayfa else {})}, ad)
        ogeler += [x for x in govde.get(alan) or [] if isinstance(x, dict)]
        sayfa = govde.get("nextPageToken")
        if not sayfa:
            break
    return ogeler


def gmail_sorgusu(gun: date) -> str:
    """IMAP yoluyla aynı pencere: bir gün önceden ertesi güne; kesin süzgeç Date başlığının Istanbul günü."""
    bas = datetime.combine(gun - timedelta(days=1), time.min, ISTANBUL)
    return f"in:sent after:{int(bas.timestamp())} before:{int(gun_araligi(gun)[1].timestamp())}"


def google_gmail_tara(
    token: str, kendi_adres: str, bugun: date, sozluk: dict[str, str] | None = None, gruplama: str = "konu",
    kendi_alanlar: list[str] | None = None, ekip_ici_atla: bool = True, istemci: httpx.Client | None = None,
) -> list[dict]:
    """gmail_tara'nın REST karşılığı: aynı başlıklar epostalari_maddele ve notlari_maddele'ye aynen gider."""
    istemci = istemci or google_istemci()
    kimlikler = [m.get("id") for m in _google_sayfalari(
        istemci, token, f"{GMAIL_API}/messages", {"q": gmail_sorgusu(bugun), "maxResults": 100}, "Gmail", "messages",
    ) if m.get("id")]
    mailler = []
    for kimlik in dict.fromkeys(kimlikler):
        govde = _google_get(istemci, token, f"{GMAIL_API}/messages/{kimlik}",
                            [("format", "metadata")] + [("metadataHeaders", b) for b in GMAIL_BASLIKLARI], "Gmail")
        basliklar = {str(b.get("name") or "").lower(): b.get("value")
                     for b in (govde.get("payload") or {}).get("headers") or [] if isinstance(b, dict)}
        m = {k: basliklar.get(k) for k in ("date", "subject", "from", "to", "cc", "message-id")}
        m["gmail_id"] = kimlik
        mailler.append(m)
    notlar, gonderilen = [], []
    for m in mailler:
        konu = not_konusu(m, kendi_adres)
        (gonderilen if konu is None else notlar).append(m)
        if konu == "" and bugun_mu(m.get("date"), bugun):  # yalnız önek: satırlar gövdede
            ham = _google_get(istemci, token, f"{GMAIL_API}/messages/{m['gmail_id']}", {"format": "raw"}, "Gmail").get("raw") or ""
            try:
                m["govde"] = duz_metin_govde(email.message_from_bytes(base64.urlsafe_b64decode(ham + "=" * (-len(ham) % 4))))
            except ValueError:
                m["govde"] = ""
    return epostalari_maddele(gonderilen, kendi_adres, bugun, sozluk, gruplama, kendi_alanlar, ekip_ici_atla) \
        + notlari_maddele(notlar, kendi_adres, bugun)


def _takvim_zamani(deger: dict) -> tuple[datetime | None, bool]:
    """Etkinliğin start/end alanı → (Istanbul zamanı, tüm gün mü)."""
    if deger.get("dateTime"):
        return iso_zaman(deger["dateTime"]), False
    try:
        return datetime.combine(date.fromisoformat(deger.get("date") or ""), time.min, ISTANBUL), True
    except ValueError:
        return None, False


# Toplantı odası ve grup takvimleri katılımcı sayılmaz.
TAKVIM_SISTEM_ALANLARI = ("resource.calendar.google.com", "group.calendar.google.com")


def toplanti_maddeleri(
    etkinlikler: list[dict], bugun: date, sozluk: dict[str, str] | None = None, kendi_alanlar: list[str] | None = None,
    an: datetime | None = None,
) -> list[dict]:
    """Sağlayıcıdan bağımsız: etkinlikler {"id", "baslik", "baslangic", "bitis" (Istanbul), "tum_gun", "teams",
    "katilimcilar": [(görünen ad, adres)]} — dahil/hariç süzgecini (yanıt, iptal, kendine blok) sağlayıcı yapmıştır.
    Bitiş saati henüz gelmemiş toplantı atlanır; tüm gün etkinlikleri o gün sayılır. Kendi şirketinden ve sözlükte
    'şirket içi' olan katılımcılar kurum olarak yazılmaz."""
    an = an or datetime.now(ISTANBUL)
    bas, son = gun_araligi(bugun)
    maddeler = []
    for e in etkinlikler:
        baslangic, bitis = e.get("baslangic"), e.get("bitis")
        if baslangic is None:
            continue
        baslik = re.sub(r"\s+", " ", str(e.get("baslik") or "")).strip() or "Başlıksız"
        if e.get("tum_gun"):
            if not baslangic.date() <= bugun < (bitis.date() if bitis else baslangic.date() + timedelta(days=1)):
                continue
            metin, zaman = f"'{baslik}' (tüm gün)", bas
        else:
            if not bas <= baslangic < son or (bitis is not None and bitis > an):
                continue
            kurumlar = []
            for ad, adres in e.get("katilimcilar") or []:
                if kendi_alanlar is not None and kendi_mi(adres, kendi_alanlar):
                    continue
                kurum = kurum_adi(ad, adres, sozluk)
                if _kucult(kurum.strip()) != SIRKET_ICI and kurum not in kurumlar:
                    kurumlar.append(kurum)
            ne = "Teams toplantısı" if e.get("teams") else "toplantısı"
            metin = f"'{baslik}' {ne} yapıldı" + (f" ({', '.join(kurumlar)} ile)" if kurumlar else "")
            zaman = baslangic
        maddeler.append({"id": kaynak_kimligi(str(e.get("id") or metin), bugun), "metin": metin, "kaynak": "takvim",
                         "kaynak_zaman": zaman})
    return tekille(maddeler)


def takvim_maddeleri(
    etkinlikler: list[dict], bugun: date, sozluk: dict[str, str] | None = None, kendi_alanlar: list[str] | None = None,
    an: datetime | None = None,
) -> list[dict]:
    """Google Takvim. Dahil: düzenleyeni kullanıcı olan ya da kabul/belki yanıtı verilen etkinlik. Hariç: reddedilen,
    iptal, başka katılımcısı ve açıklaması olmayan (kendine blok), bitiş saati henüz gelmemiş. Tüm gün etkinlikleri
    o gün sayılır."""
    uygun = []
    for e in etkinlikler:
        if e.get("status") == "cancelled":
            continue
        katilimcilar = [k for k in e.get("attendees") or [] if isinstance(k, dict)]
        ben = next((k for k in katilimcilar if k.get("self")), {})
        if ben.get("responseStatus") == "declined":
            continue
        duzenleyen = bool((e.get("organizer") or {}).get("self") or ben.get("organizer"))
        if not (duzenleyen or ben.get("responseStatus") in ("accepted", "tentative")):
            continue
        digerleri = [k for k in katilimcilar if not k.get("self") and not k.get("resource") and k.get("email")
                     and not str(k["email"]).lower().endswith(TAKVIM_SISTEM_ALANLARI)]
        if not digerleri and not str(e.get("description") or "").strip():
            continue
        baslangic, tum_gun = _takvim_zamani(e.get("start") or {})
        bitis, _ = _takvim_zamani(e.get("end") or {})
        uygun.append({"id": e.get("id"), "baslik": e.get("summary"), "baslangic": baslangic, "bitis": bitis,
                      "tum_gun": tum_gun, "katilimcilar": [(str(k.get("displayName") or ""), str(k["email"])) for k in digerleri]})
    return toplanti_maddeleri(uygun, bugun, sozluk, kendi_alanlar, an)


def takvim_tara(
    token: str, bugun: date, sozluk: dict[str, str] | None = None, kendi_alanlar: list[str] | None = None,
    istemci: httpx.Client | None = None, an: datetime | None = None,
) -> list[dict]:
    bas, son = gun_araligi(bugun)
    etkinlikler = _google_sayfalari(istemci or google_istemci(), token, TAKVIM_API, {
        "timeMin": rfc3339(bas), "timeMax": rfc3339(son), "singleEvents": "true", "orderBy": "startTime", "maxResults": 250,
    }, "Takvim", "items")
    return takvim_maddeleri(etkinlikler, bugun, sozluk, kendi_alanlar, an)


# mimeType → (tür, belirtme hâli): "'Katalog' tablosu güncellendi"
DRIVE_TURLERI = {
    "tablo": ("tablosu", ("application/vnd.google-apps.spreadsheet", "spreadsheetml", "application/vnd.ms-excel",
                          "application/vnd.oasis.opendocument.spreadsheet", "text/csv")),
    "belge": ("belgesi", ("application/vnd.google-apps.document", "wordprocessingml", "application/msword",
                          "application/vnd.oasis.opendocument.text", "application/rtf", "text/plain")),
    "sunum": ("sunumu", ("application/vnd.google-apps.presentation", "presentationml", "application/vnd.ms-powerpoint",
                         "application/vnd.oasis.opendocument.presentation")),
    "PDF": ("PDF'i", ("application/pdf",)),
}
UZANTI = re.compile(r"\.(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{2,5}$")


def drive_turu(mime: str | None) -> str:
    """tablo / belge / sunum / PDF / dosya."""
    mime = mime or ""
    return next((tur for tur, (_, eslesmeler) in DRIVE_TURLERI.items() if any(x in mime for x in eslesmeler)), "dosya")


def dosya_adi(ad: str | None) -> str:
    """'Katalog 2026.xlsx' → 'Katalog 2026'; uzantı yoksa ya da ad yalnız uzantıysa olduğu gibi."""
    ad = re.sub(r"\s+", " ", ad or "").strip()
    kisa = UZANTI.sub("", ad).strip()
    return kisa or ad or "Adsız"


def dosya_maddeleri(dosyalar: list[dict], bugun: date, kaynak: str, fazla_kimligi: str) -> list[dict]:
    """Sağlayıcıdan bağımsız: dosyalar {"kimlik", "ad", "tur", "olusturma", "degisme"} yalnız son değişikliği kullanıcının
    yaptığı dosyalardır. O gün oluşturulan 'oluşturuldu', diğerleri 'güncellendi'; en fazla DRIVE_SINIRI madde, fazlası
    tek "ve N dosya daha güncellendi" maddesi."""
    sirali = sorted(dosyalar, key=lambda d: (d["degisme"] or datetime.min.replace(tzinfo=ISTANBUL), d["kimlik"]))
    maddeler = []
    for d in sirali[:DRIVE_SINIRI]:
        ek = DRIVE_TURLERI[d["tur"]][0] if d["tur"] in DRIVE_TURLERI else "dosyası"
        ne = "oluşturuldu" if d["olusturma"] is not None and d["olusturma"].date() == bugun else "güncellendi"
        maddeler.append({"id": kaynak_kimligi(d["kimlik"], bugun), "metin": f"'{dosya_adi(d['ad'])}' {ek} {ne}",
                         "kaynak": kaynak, "kaynak_zaman": d["degisme"]})
    fazla = sirali[DRIVE_SINIRI:]
    if fazla:
        maddeler.append({"id": kaynak_kimligi(fazla_kimligi, bugun), "metin": f"ve {len(fazla)} dosya daha güncellendi",
                         "kaynak": kaynak, "kaynak_zaman": fazla[-1]["degisme"]})
    return maddeler


def drive_maddeleri(dosyalar: list[dict], bugun: date) -> list[dict]:
    """Google Drive: yalnız son değişikliği kullanıcının yaptığı dosyalar (lastModifyingUser.me)."""
    benim = [{"kimlik": str(d["id"]), "ad": d.get("name"), "tur": drive_turu(d.get("mimeType")),
              "olusturma": iso_zaman(d.get("createdTime")), "degisme": iso_zaman(d.get("modifiedTime"))}
             for d in dosyalar if (d.get("lastModifyingUser") or {}).get("me") is True and d.get("id")]
    return dosya_maddeleri(benim, bugun, "drive", "drive-fazla")


def drive_sorgusu(gun: date) -> str:
    bas, son = gun_araligi(gun)
    return (f"modifiedTime >= '{rfc3339(bas)}' and modifiedTime < '{rfc3339(son)}' and trashed=false "
            "and mimeType != 'application/vnd.google-apps.folder'")


def drive_tara(token: str, bugun: date, istemci: httpx.Client | None = None) -> list[dict]:
    dosyalar = _google_sayfalari(istemci or google_istemci(), token, DRIVE_API, {
        "q": drive_sorgusu(bugun), "corpora": "user", "pageSize": 100,
        "fields": "nextPageToken,files(id,name,mimeType,createdTime,modifiedTime,lastModifyingUser(me))",
    }, "Drive", "files")
    return drive_maddeleri(dosyalar, bugun)


# ---------------------------------------------------------------- Microsoft: OAuth (v2.0, tenant common), Graph

MICROSOFT_YETKI_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_API = "https://graph.microsoft.com/v1.0"
# kısa ad → Graph izni; kaynaklar sözlüğündeki anahtarlarla aynı
MICROSOFT_KAPSAMLARI = {"outlook": "Mail.Read", "outlook_takvim": "Calendars.Read", "onedrive": "Files.Read"}
MICROSOFT_SCOPE = "openid email offline_access User.Read " + " ".join(MICROSOFT_KAPSAMLARI.values())
MICROSOFT_YENILE_MESAJI = "Microsoft bağlantısı yenilenmeli"
MICROSOFT_YONETICI_MESAJI = (
    "Şirketinin Microsoft yöneticisinin bu uygulamaya bir kez onay vermesi gerekiyor. "
    "BT birimine 'Günlük Rapor uygulamasına kullanıcı onayı' için başvur."
)
MICROSOFT_KALDIR_NOTU = "Hesabından tamamen kaldırmak için account.microsoft.com → Gizlilik → Uygulamalar"
# Kiracı kullanıcı onayını kapatmışsa: AADSTS65001 (onay yok), 90094/900941 (yönetici izni gerekli)
YONETICI_ONAYI = re.compile(r"AADSTS(65001|90094|900941)\b|admin(istrator)?\s+(approval|consent|permission)", re.IGNORECASE)
OUTLOOK_ALANLARI = "subject,toRecipients,ccRecipients,sentDateTime,internetMessageId,from"
# organizer spesifikasyon listesine eklendi: davet edildiğim toplantıda düzenleyen katılımcılar arasında gelmez.
OUTLOOK_TAKVIM_ALANLARI = ("subject,start,end,isAllDay,isCancelled,isOrganizer,responseStatus,attendees,organizer,"
                           "bodyPreview,isOnlineMeeting,onlineMeetingProvider")
ISTANBUL_TERCIHI = {"Prefer": 'outlook.timezone="Europe/Istanbul"'}
DUZ_METIN_TERCIHI = {"Prefer": 'outlook.body-content-type="text"'}
# Uzantı → Drive'daki tür adı (belirtme hâli DRIVE_TURLERI'nden); diğerleri "dosya"
ONEDRIVE_TURLERI = {"xlsx": "tablo", "xlsm": "tablo", "xls": "tablo", "csv": "tablo", "docx": "belge", "doc": "belge",
                    "pptx": "sunum", "ppt": "sunum", "pdf": "PDF"}


class MicrosoftHatasi(KaynakHatasi):
    pass


class MicrosoftYenilenmeli(MicrosoftHatasi):
    """Refresh token artık geçersiz (invalid_grant / interaction_required): kullanıcı yeniden bağlanmalı."""


class MicrosoftYetkisiz(MicrosoftHatasi, YetkisizErisim):
    saglayici = "microsoft"


def microsoft_istemci_bilgisi() -> tuple[str, str]:
    return (os.environ.get("MICROSOFT_CLIENT_ID") or "").strip(), (os.environ.get("MICROSOFT_CLIENT_SECRET") or "").strip()


def microsoft_ayarli() -> bool:
    """İki değişken de yoksa Microsoft arayüzü hiç görünmez."""
    return all(microsoft_istemci_bilgisi())


def microsoft_istemci() -> httpx.Client:
    return httpx.Client(timeout=20)


def microsoft_kisa_kapsamlar(kapsamlar: list[str] | None) -> list[str]:
    """Token yanıtındaki scope'lar ("Mail.Read" ya da "https://graph.microsoft.com/Mail.Read") → kısa adlar."""
    verilen = {str(k).rsplit("/", 1)[-1].lower() for k in kapsamlar or []}
    return [k for k, izin in MICROSOFT_KAPSAMLARI.items() if izin.lower() in verilen]


def microsoft_yetki_adresi(yonlendirme: str, state: str, challenge: str, login_hint: str = "") -> str:
    params = {
        "client_id": microsoft_istemci_bilgisi()[0], "response_type": "code", "redirect_uri": yonlendirme,
        "response_mode": "query", "scope": MICROSOFT_SCOPE, "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return MICROSOFT_YETKI_URL + "?" + urlencode(params)


def _kisa_neden(hata: str, aciklama: str) -> str:
    """error_description'ın ilk cümlesi (AADSTS kodu dahil, iz/zaman satırları hariç), yoksa error kodu."""
    ilk = re.split(r"\r?\n|(?<=\.)\s", (aciklama or "").strip(), maxsplit=1)[0].strip()
    return (ilk[:120] if ilk else "") or hata or "bilinmeyen hata"


def microsoft_hata_mesaji(hata: str, aciklama: str = "") -> str:
    """Onay dönüşündeki error / error_description → kullanıcıya Türkçe neden."""
    hata, aciklama = hata or "", aciklama or ""
    if hata == "consent_required" or YONETICI_ONAYI.search(aciklama):
        return MICROSOFT_YONETICI_MESAJI
    if hata == "access_denied":
        return "İzin verilmedi"
    return f"Microsoft isteği reddetti ({_kisa_neden(hata, aciklama)})"


def _microsoft_token_istegi(veri: dict, istemci: httpx.Client, yenileme: bool) -> dict:
    """Yenilemede invalid_grant / interaction_required → MicrosoftYenilenmeli; yönetici onayı gerekiyorsa Türkçe
    mesaj; diğer hatalar kısa nedenle MicrosoftHatasi."""
    kod, govde = _oauth_token_yaniti(MICROSOFT_TOKEN_URL, veri, istemci, MicrosoftHatasi, "Microsoft")
    if kod >= 400 or not govde.get("access_token"):
        hata = govde.get("error") if isinstance(govde.get("error"), str) else ""
        aciklama = govde.get("error_description") if isinstance(govde.get("error_description"), str) else ""
        if yenileme and hata in ("invalid_grant", "interaction_required"):
            raise MicrosoftYenilenmeli(MICROSOFT_YENILE_MESAJI)
        if hata == "consent_required" or YONETICI_ONAYI.search(aciklama):
            raise MicrosoftHatasi(MICROSOFT_YONETICI_MESAJI)
        raise MicrosoftHatasi(f"Microsoft token vermedi ({_kisa_neden(hata, aciklama) if hata else kod})")
    return govde


def _graph_get(istemci: httpx.Client, token: str, url: str, ad: str, basliklar: dict | None = None) -> dict:
    """url sorgusu hazır (graph_adresi ya da @odata.nextLink)."""
    return _rest_get(istemci, token, url, None, ad, "Microsoft", MicrosoftYetkisiz, basliklar)


def graph_adresi(yol: str, params: dict | None = None) -> str:
    """OData sistem seçenekleri ($filter, $select…) '$' ile ve boşluklar %20 olarak yazılır."""
    sorgu = "&".join(f"{k}={quote(str(v), safe=',')}" for k, v in (params or {}).items())
    return GRAPH_API + yol + (f"?{sorgu}" if sorgu else "")


def _graph_sayfalari(istemci: httpx.Client, token: str, url: str, ad: str, basliklar: dict | None = None) -> list:
    """@odata.nextLink ile sayfalar; token yalnız Graph adresine gider."""
    ogeler = []
    for _ in range(SAYFA_SINIRI):
        govde = _graph_get(istemci, token, url, ad, basliklar)
        ogeler += [x for x in govde.get("value") or [] if isinstance(x, dict)]
        url = govde.get("@odata.nextLink")
        if not isinstance(url, str) or not url.startswith(GRAPH_API + "/"):
            break
    return ogeler


def microsoft_eposta(token: str, istemci: httpx.Client) -> str:
    """/me: mail, yoksa userPrincipalName."""
    try:
        govde = _graph_get(istemci, token, graph_adresi("/me", {"$select": "mail,userPrincipalName"}), "Microsoft hesabı")
    except KaynakHatasi as e:
        raise MicrosoftHatasi(str(e)) from e
    for alan in ("mail", "userPrincipalName"):
        eposta = govde.get(alan)
        if isinstance(eposta, str) and "@" in eposta:
            return eposta.strip().lower()
    raise MicrosoftHatasi("Microsoft e-posta adresini vermedi")


def microsoft_kod_takas(kod: str, dogrulayici: str, yonlendirme: str, istemci: httpx.Client | None = None) -> dict:
    """Yetki kodu → {access_token, expires_in, refresh_token, kapsamlar, eposta}."""
    client_id, secret = microsoft_istemci_bilgisi()
    istemci = istemci or microsoft_istemci()
    govde = _microsoft_token_istegi({
        "client_id": client_id, "client_secret": secret, "code": kod, "redirect_uri": yonlendirme,
        "grant_type": "authorization_code", "code_verifier": dogrulayici, "scope": MICROSOFT_SCOPE,
    }, istemci, yenileme=False)
    if not govde.get("refresh_token"):
        raise MicrosoftHatasi("Microsoft yenileme anahtarı vermedi; yeniden bağlanın")
    return {
        "access_token": govde["access_token"], "expires_in": int(govde.get("expires_in") or 3600),
        "refresh_token": govde["refresh_token"], "kapsamlar": str(govde.get("scope") or "").split(),
        "eposta": microsoft_eposta(govde["access_token"], istemci),
    }


def microsoft_yenile(refresh_token: str, istemci: httpx.Client | None = None) -> tuple[str, int, str | None]:
    """(access_token, saniye, yeni refresh token ya da None). Microsoft çoğu yenilemede yeni refresh token döner."""
    client_id, secret = microsoft_istemci_bilgisi()
    govde = _microsoft_token_istegi({
        "client_id": client_id, "client_secret": secret, "refresh_token": refresh_token,
        "grant_type": "refresh_token", "scope": MICROSOFT_SCOPE,
    }, istemci or microsoft_istemci(), yenileme=True)
    yeni = govde.get("refresh_token")
    return govde["access_token"], int(govde.get("expires_in") or 3600), yeni if isinstance(yeni, str) and yeni else None


def _graph_adresleri(alicilar) -> str:
    """Graph recipient listesi → "Ad <adres>, …" başlık değeri (IMAP/Gmail yoluyla aynı ayrıştırıcıya gider)."""
    parcalar = []
    for a in alicilar or []:
        adres = (a.get("emailAddress") or {}) if isinstance(a, dict) else {}
        eposta = str(adres.get("address") or "").strip()
        if not eposta:
            continue
        try:
            parcalar.append(formataddr((str(adres.get("name") or "").strip(), eposta), charset="utf-8"))
        except UnicodeError:  # ASCII dışı adres (EAI): görünen ad olmadan yazılır, tarama düşmez
            parcalar.append(eposta)
    return ", ".join(parcalar)


def outlook_mesaji(m: dict) -> dict:
    """Graph mesajı → epostalari_maddele / notlari_maddele'nin beklediği başlık sözlüğü."""
    zaman = iso_zaman(m.get("sentDateTime"))
    return {
        "date": format_datetime(zaman) if zaman else None, "subject": str(m.get("subject") or ""),
        "from": _graph_adresleri([m.get("from")]), "to": _graph_adresleri(m.get("toRecipients")),
        "cc": _graph_adresleri(m.get("ccRecipients")), "message-id": m.get("internetMessageId"), "graph_id": m.get("id"),
    }


def outlook_sorgusu(gun: date) -> dict:
    bas, son = gun_araligi(gun)
    return {"$filter": f"sentDateTime ge {rfc3339(bas)} and sentDateTime lt {rfc3339(son)}",
            "$select": OUTLOOK_ALANLARI, "$top": 50}


def outlook_tara(
    token: str, kendi_adres: str, bugun: date, sozluk: dict[str, str] | None = None, gruplama: str = "konu",
    kendi_alanlar: list[str] | None = None, ekip_ici_atla: bool = True, istemci: httpx.Client | None = None,
) -> list[dict]:
    """Gönderilmiş Öğeler → gmail_tara ile aynı maddeler (epostalari_maddele + notlari_maddele aynen). E-posta maddeleri
    kaynak 'outlook' olur ve id'leri 'ms-' önekini alır: aynı konu Gmail'den de gittiyse kaynak_id çakışmaz. Not
    maddelerinin id'si internetMessageId'den türer."""
    istemci = istemci or microsoft_istemci()
    mailler = [outlook_mesaji(m) for m in _graph_sayfalari(
        istemci, token, graph_adresi("/me/mailFolders/sentitems/messages", outlook_sorgusu(bugun)), "Outlook")]
    notlar, gonderilen = [], []
    for m in mailler:
        konu = not_konusu(m, kendi_adres)
        (gonderilen if konu is None else notlar).append(m)
        if konu == "" and bugun_mu(m.get("date"), bugun) and m.get("graph_id"):  # yalnız önek: satırlar gövdede
            govde = _graph_get(istemci, token, graph_adresi(f"/me/messages/{quote(str(m['graph_id']), safe='')}",
                                                           {"$select": "body"}), "Outlook", DUZ_METIN_TERCIHI)
            m["govde"] = str((govde.get("body") or {}).get("content") or "")
    maddeler = epostalari_maddele(gonderilen, kendi_adres, bugun, sozluk, gruplama, kendi_alanlar, ekip_ici_atla)
    return [{**m, "id": "ms-" + m["id"], "kaynak": "outlook"} for m in maddeler] + notlari_maddele(notlar, kendi_adres, bugun)


def graph_zamani(deger) -> datetime | None:
    """{"dateTime": "2026-09-16T10:00:00.0000000", "timeZone": "Europe/Istanbul"} → Istanbul saati."""
    if not isinstance(deger, dict) or not isinstance(deger.get("dateTime"), str):
        return None
    metin = re.sub(r"(\.\d{6})\d+", r"\1", deger["dateTime"].strip()).replace("Z", "+00:00")
    try:
        zaman = datetime.fromisoformat(metin)
    except ValueError:
        return None
    if zaman.tzinfo is None:
        try:
            bolge = timezone.utc if str(deger.get("timeZone") or "").upper() in ("UTC", "") else ZoneInfo(deger["timeZone"])
        except Exception:
            bolge = ISTANBUL  # Prefer ile Istanbul istendi; Windows saat dilimi adı gelirse de öyle sayılır
        zaman = zaman.replace(tzinfo=bolge)
    return zaman.astimezone(ISTANBUL)


def outlook_takvim_maddeleri(
    etkinlikler: list[dict], bugun: date, kendi_adres: str = "", sozluk: dict[str, str] | None = None,
    kendi_alanlar: list[str] | None = None, an: datetime | None = None,
) -> list[dict]:
    """Outlook/Teams takvimi, Google'la aynı kurallar: düzenleyen ya da kabul/belki yanıtı verilen dahil; reddedilen,
    iptal, katılımcısız ve bodyPreview'u boş (kendine blok), bitmemiş olan hariç. Oda/kaynak tipindeki katılımcılar
    sayılmaz. Teams toplantısı "'<konu>' Teams toplantısı yapıldı"."""
    kendi = (kendi_adres or "").strip().lower()
    uygun = []
    for e in etkinlikler:
        if e.get("isCancelled"):
            continue
        yanit = str((e.get("responseStatus") or {}).get("response") or "")
        if yanit == "declined":
            continue
        if not (e.get("isOrganizer") is True or yanit in ("organizer", "accepted", "tentativelyAccepted")):
            continue
        katilimcilar = []
        for k in [e.get("organizer")] + list(e.get("attendees") or []):
            if not isinstance(k, dict) or str(k.get("type") or "").lower() == "resource":
                continue
            adres = k.get("emailAddress") or {}
            eposta = str(adres.get("address") or "").strip()
            if "@" not in eposta or eposta.lower() == kendi or any(eposta.lower() == a.lower() for _, a in katilimcilar):
                continue
            katilimcilar.append((str(adres.get("name") or "").strip(), eposta))
        if not katilimcilar and not str(e.get("bodyPreview") or "").strip():
            continue
        uygun.append({
            "id": e.get("id"), "baslik": e.get("subject"), "baslangic": graph_zamani(e.get("start")),
            "bitis": graph_zamani(e.get("end")), "tum_gun": e.get("isAllDay") is True, "katilimcilar": katilimcilar,
            "teams": e.get("isOnlineMeeting") is True and e.get("onlineMeetingProvider") == "teamsForBusiness",
        })
    return toplanti_maddeleri(uygun, bugun, sozluk, kendi_alanlar, an)


def outlook_takvim_tara(
    token: str, kendi_adres: str, bugun: date, sozluk: dict[str, str] | None = None,
    kendi_alanlar: list[str] | None = None, istemci: httpx.Client | None = None, an: datetime | None = None,
) -> list[dict]:
    bas, son = gun_araligi(bugun)
    adres = graph_adresi("/me/calendarView", {"startDateTime": rfc3339(bas), "endDateTime": rfc3339(son),
                                               "$select": OUTLOOK_TAKVIM_ALANLARI, "$top": 100})
    etkinlikler = _graph_sayfalari(istemci or microsoft_istemci(), token, adres, "Takvim", ISTANBUL_TERCIHI)
    return outlook_takvim_maddeleri(etkinlikler, bugun, kendi_adres, sozluk, kendi_alanlar, an)


def onedrive_turu(ad: str | None) -> str:
    """Uzantıdan: xlsx → tablo, docx → belge, pptx → sunum, pdf → PDF, diğerleri dosya."""
    ad = (ad or "").strip()
    return ONEDRIVE_TURLERI.get(ad.rpartition(".")[2].lower(), "dosya") if "." in ad else "dosya"


def onedrive_maddeleri(ogeler: list[dict], bugun: date, kendi_adres: str) -> list[dict]:
    """/me/drive/recent: yalnız o gün değiştirilmiş ve son değiştireni kullanıcı (lastModifiedBy.user.email = ms_eposta)
    olan dosyalar; klasörler hariç. Paylaşılan dosyada remoteItem'ın alanları esas alınır."""
    bas, son = gun_araligi(bugun)
    kendi = (kendi_adres or "").strip().lower()
    dosyalar: dict[str, dict] = {}
    for o in ogeler:
        if isinstance(o.get("remoteItem"), dict):
            o = {**o, **o["remoteItem"]}
        if "folder" in o or not o.get("id") or not kendi:
            continue
        degisme = iso_zaman(o.get("lastModifiedDateTime"))
        if degisme is None or not bas <= degisme < son:
            continue
        kim = str(((o.get("lastModifiedBy") or {}).get("user") or {}).get("email") or "").strip().lower()
        if kim != kendi:
            continue
        kimlik = str((o.get("parentReference") or {}).get("driveId") or "") + str(o["id"])
        dosyalar.setdefault(kimlik, {"kimlik": kimlik, "ad": o.get("name"), "tur": onedrive_turu(o.get("name")),
                                     "olusturma": iso_zaman(o.get("createdDateTime")), "degisme": degisme})
    return dosya_maddeleri(list(dosyalar.values()), bugun, "onedrive", "onedrive-fazla")


def onedrive_tara(token: str, kendi_adres: str, bugun: date, istemci: httpx.Client | None = None) -> list[dict]:
    ogeler = _graph_sayfalari(istemci or microsoft_istemci(), token, GRAPH_API + "/me/drive/recent", "OneDrive")
    return onedrive_maddeleri(ogeler, bugun, kendi_adres)


# ---------------------------------------------------------------- Claude

def _json_dizi_ayikla(metin: str) -> list:
    bas, son = metin.find("["), metin.rfind("]")
    if bas == -1 or son <= bas:
        raise ValueError("yanıtta JSON dizi yok")
    veri = json.loads(metin[bas : son + 1])
    if not isinstance(veri, list):
        raise ValueError("yanıt JSON dizi değil")
    return veri


class ClaudeHatasi(Exception):
    pass


_yerel = threading.local()


def son_kullanim() -> dict:
    """Bu iş parçacığındaki son Claude yanıtının usage alanı: {"girdi": int, "cikti": int}; yoksa sıfır."""
    return getattr(_yerel, "kullanim", None) or {"girdi": 0, "cikti": 0}


def kullanimi_sifirla() -> None:
    _yerel.kullanim = None


def _claude_cagir(istek: dict, api_anahtari: str, istemci: httpx.Client | None = None) -> str:
    """Messages API'ye tek istek; yanıtın metin bloklarını döner. Her hata ClaudeHatasi olur."""
    istemci = istemci or httpx.Client(timeout=90)
    try:
        yanit = istemci.post(
            "https://api.anthropic.com/v1/messages",
            json=istek,
            headers={"x-api-key": api_anahtari, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )
        if yanit.status_code >= 400:
            try:
                neden = yanit.json()["error"]["message"]
            except Exception:
                neden = yanit.text[:200]
            raise ClaudeHatasi(f"API hatası ({yanit.status_code}: {neden})")
        govde = yanit.json()
    except httpx.HTTPError as e:
        raise ClaudeHatasi(f"bağlanılamadı ({e.__class__.__name__})") from e
    except ValueError as e:
        raise ClaudeHatasi("yanıt okunamadı") from e
    usage = govde.get("usage") if isinstance(govde.get("usage"), dict) else {}
    _yerel.kullanim = {
        "girdi": sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
        "cikti": int(usage.get("output_tokens") or 0),
    }
    if govde.get("stop_reason") == "refusal":
        raise ClaudeHatasi("istek reddedildi")
    return "".join(b.get("text", "") for b in govde.get("content", []) if b.get("type") == "text")


def _id_metin_eslesmesi(metin: str) -> dict[str, str]:
    return {
        str(x["id"]): x["metin"].strip()
        for x in _json_dizi_ayikla(metin)
        if isinstance(x, dict) and isinstance(x.get("metin"), str) and x["metin"].strip() and "id" in x
    }


# Microsoft maddeleri Claude'a Google karşılıklarının adıyla gider: aynı açıklama ve aynı kural geçerli.
CLAUDE_KAYNAK_ADI = {"outlook": "eposta", "onedrive": "drive"}


def claude_cevir(
    maddeler: list[dict], api_anahtari: str, istemci: httpx.Client | None = None, proje_adi: str = "",
    kendi_sirket: list[str] | None = None, eslemeler: list[dict] | None = None,
) -> tuple[list[dict], str | None]:
    """Maddeleri tek çağrıda iş diline çevirir. Hata olursa ham maddeler + hata mesajı döner.
    eslemeler: ad eşlemeleri girdiye ve çıktıya uygulanır. Girdideki tırnaklı bir parçayı aynen korumayan çeviri
    reddedilir; o maddenin metni eşlenmiş ham metin olur."""
    if not maddeler:
        return maddeler, None
    esli = {m["id"]: ad_esle(m["metin"], eslemeler) for m in maddeler}
    girdiler = [{"id": m["id"], "kaynak": CLAUDE_KAYNAK_ADI.get(m["kaynak"], m["kaynak"]), "metin": esli[m["id"]]}
                for m in maddeler]
    istek = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1500,
        "thinking": {"type": "disabled"},
        "system": claude_sistem(ad_esle(proje_adi, eslemeler), [ad_esle(k, eslemeler) for k in kendi_sirket or []]),
        "messages": [{
            "role": "user",
            "content": (
                f"Kaynağı 'medusa' olanlar {proje_adi + ' yazılımında' if proje_adi else 'yazılım projesinde'} "
                "bugün yapılan değişikliklerin commit mesajları, "
                "'eposta' olanlar bugün gönderilen e-postaların özetleri, 'takvim' olanlar bugün yapılan toplantılar, "
                "'drive' olanlar bugün üzerinde çalışılan dosyalar. Her birini çevir:\n"
                + json.dumps(girdiler, ensure_ascii=False)
            ),
        }],
    }
    try:
        ceviri = _id_metin_eslesmesi(_claude_cagir(istek, api_anahtari, istemci))
    except ClaudeHatasi as e:
        return maddeler, f"claude: {e}, ham metin kullanıldı"
    except (ValueError, KeyError, TypeError) as e:
        return maddeler, f"claude: yanıt JSON değil ({e}), ham metin kullanıldı"
    for kimlik, metin in list(ceviri.items()):
        if kimlik in esli and not tirnaklar_korundu(tirnak_parcalari(esli[kimlik]), metin):
            ceviri[kimlik] = esli[kimlik]
            log.info("claude cevirisi reddedildi: tirnak ici degisti")
        else:
            ceviri[kimlik] = ad_esle(metin, eslemeler)
    # id ham metinden türetildiği için korunur; böylece gün içinde tik durumu kaybolmaz.
    return [{**m, "metin": ceviri.get(m["id"], m["metin"])} for m in maddeler], None


KATEGORI_KURALI = (
    "- \"kategori_sec\": true olan maddeler için verilen rapor kategorilerinden konuya en uygun olanın id'sini "
    "\"kategori_id\" alanında döndür; emin değilsen \"kategori_id\" alanını hiç yazma.\n"
)


def duzelt_sistemi(proje_adi: str = "", kategorili: bool = False, kendi_sirket: list[str] | None = None) -> str:
    urun = f"Yazılım ürününün adı {proje_adi}; yazılımdan söz ederken bu adı kullan. " if proje_adi else ""
    return (
        "Bir müzik edisyon şirketinde çalışan bir danışmanın yöneticisine WhatsApp'tan gönderdiği günlük raporun "
        "maddelerini düzeltiyorsun.\n"
        "Kurallar:\n"
        "- Yazım, noktalama ve kesme işaretini düzelt: özel adlara gelen ekler kesme işaretiyle ayrılır ve ek "
        "bitişik yazılır (\"Köprü Film den\" → \"Köprü Film'den\", \"Ezgi hanım dan\" → \"Ezgi Hanım'dan\"). "
        "Kişi adından sonra gelen hanım/bey büyük harfle yazılır.\n"
        "- Her madde yönetici raporuna uygun TEK cümle olur; sonunda nokta olur. Zaman kipi -di'li geçmiş zamandır "
        "(\"görüşüldü\", \"gönderildi\", \"istendi\"); \"-mıştır\", \"-mıştı\" kullanma.\n"
        "- Olgu ekleme, çıkarma, yorum katma; \"tamamlandı\" gibi durum bilgileri de cümlede kalır. "
        "Kişi, kurum, ürün adları ve sayılar aynen korunur.\n"
        "- Teknik terimleri yöneticinin anlayacağı iş diline çevir. " + urun + "\n"
        "- " + EPOSTA_KURALI + "\n"
        "- " + GOOGLE_KURALI + "\n"
        "- " + TIRNAK_KURALI + "\n"
        + ("- " + kendi_sirket_kurali(kendi_sirket) + "\n" if kendi_sirket else "")
        + "- 'devam' türündeki maddelerde işin adı ve aşaması tek cümlede birleşir (örn. \"… için yanıt bekleniyor.\").\n"
        "- 'surekli' türündeki maddeler her gün tekrarlanan işlerdir: son raporlardaki cümlelerle aynı olmayan "
        "ama aynı anlama gelen, doğal bir ifade yaz. Metinde | ile ayrılmış seçenekler varsa hepsi aynı işin "
        "farklı söylenişidir.\n"
        + (KATEGORI_KURALI if kategorili else "")
        + "Yanıt olarak YALNIZ JSON dizi döndür, başka hiçbir şey yazma: [{\"id\": <sayı>, \"metin\": \"...\""
        + (", \"kategori_id\": <sayı, yalnız istenenlerde>" if kategorili else "") + "}]"
    )


def claude_duzelt(
    girdiler: list[dict], son_raporlar: list[str], api_anahtari: str, proje_adi: str = "",
    istemci: httpx.Client | None = None, kendi_sirket: list[str] | None = None,
) -> dict[int, str]:
    """girdiler: {id, tur, metin, asama?}. Dönen: id → düzeltilmiş metin. Hata → ClaudeHatasi."""
    return claude_duzelt_kategorili(girdiler, son_raporlar, api_anahtari, proje_adi, istemci, kendi_sirket=kendi_sirket)[0]


def _korunacak_tirnaklar(girdi: dict) -> list[str]:
    if girdi.get("tur") == "surekli":
        return surekli_tirnaklari(girdi.get("metin") or "")
    return tirnak_parcalari(girdi.get("metin")) + tirnak_parcalari(girdi.get("asama"))


def claude_duzelt_kategorili(
    girdiler: list[dict], son_raporlar: list[str], api_anahtari: str, proje_adi: str = "",
    istemci: httpx.Client | None = None, kategoriler: list[dict] | None = None, kendi_sirket: list[str] | None = None,
    eslemeler: list[dict] | None = None,
) -> tuple[dict[int, str], dict[int, int]]:
    """Tek çağrı. kategoriler [{id, ad}] verilirse "kategori_sec": true girdiler için önerilen kategori de döner.
    Dönen: (id → düzeltilmiş metin, id → kategori_id). Kategori id'lerinin geçerliliğini çağıran denetler.
    eslemeler: ad eşlemeleri girdiye, bağlama ve çıktıya uygulanır. Girdideki tırnaklı parçayı aynen korumayan
    madde sonuçtan çıkarılır (ham metin kalır)."""
    if not girdiler:
        return {}, {}
    girdiler = [{**g, **{k: ad_esle(g[k], eslemeler) for k in ("metin", "asama") if isinstance(g.get(k), str)}}
                for g in girdiler]
    son_raporlar = [ad_esle(r, eslemeler) for r in son_raporlar]
    kategoriler = [{**k, "ad": ad_esle(k.get("ad"), eslemeler)} for k in kategoriler] if kategoriler else kategoriler
    proje_adi = ad_esle(proje_adi, eslemeler)
    kendi_sirket = [ad_esle(k, eslemeler) for k in kendi_sirket] if kendi_sirket else kendi_sirket
    baglam = (
        "Son günlük raporlar (sürekli işlerde bu cümleleri tekrar etme):\n"
        + "\n---\n".join(son_raporlar) + "\n\n"
    ) if son_raporlar else ""
    if kategoriler:
        baglam += "Rapor kategorileri:\n" + json.dumps(kategoriler, ensure_ascii=False) + "\n\n"
    istek = {
        "model": CLAUDE_MODEL,
        "max_tokens": 2000,
        "thinking": {"type": "disabled"},
        "system": duzelt_sistemi(proje_adi, bool(kategoriler), kendi_sirket),
        "messages": [{
            "role": "user",
            "content": baglam + "Düzeltilecek maddeler:\n" + json.dumps(girdiler, ensure_ascii=False),
        }],
    }
    metin = _claude_cagir(istek, api_anahtari, istemci)
    try:
        eslesme = _id_metin_eslesmesi(metin)
        dizi = _json_dizi_ayikla(metin)
    except (ValueError, KeyError, TypeError) as e:
        raise ClaudeHatasi(f"yanıt JSON değil ({e})") from e
    gecerli = {str(g["id"]) for g in girdiler}
    isteyen = {str(g["id"]) for g in girdiler if g.get("kategori_sec")}
    oneriler = {}
    for x in dizi:
        if not isinstance(x, dict) or str(x.get("id")) not in isteyen:
            continue
        k = x.get("kategori_id")
        if isinstance(k, str) and k.strip().isdigit():
            k = int(k)
        if isinstance(k, int) and not isinstance(k, bool):
            oneriler[int(x["id"])] = k
    korunacak = {str(g["id"]): _korunacak_tirnaklar(g) for g in girdiler}
    reddedilen = {k for k, v in eslesme.items() if k in gecerli and not tirnaklar_korundu(korunacak[k], v)}
    if reddedilen:
        log.info("claude duzeltmesi reddedildi: tirnak ici degisti (%s madde)", len(reddedilen))
    return {int(k): ad_esle(v, eslemeler) for k, v in eslesme.items() if k in gecerli and k not in reddedilen}, oneriler


# ---------------------------------------------------------------- sesle madde ekleme

# Basit bölme: satır sonu, cümle sonu ve "ve sonra" / "sonra da" bağlaçları.
SES_AYIRICI = re.compile(r"\n+|(?<=[.!?…])\s+|\s*,?\s+(?:ve\s+sonra|sonra\s+da)\s+", re.IGNORECASE)
SES_BAS_BAGLAC = re.compile(r"^(?:ve\s+)?sonra(?:\s+da)?\s+", re.IGNORECASE)


def _bas_harf_buyut(metin: str) -> str:
    ilk = metin[:1]
    return {"i": "İ", "ı": "I"}.get(ilk, ilk.upper()) + metin[1:]


def sesi_basitce_bol(metin: str) -> list[dict]:
    """Claude'suz yedek: dikte metnini satır/cümle bazında maddelere böler; kategori boş, tür 'bugun'."""
    maddeler = []
    for parca in SES_AYIRICI.split(metin or ""):
        parca = SES_BAS_BAGLAC.sub("", parca.strip(" \t,;-•*"))
        if parca:
            maddeler.append({"metin": _bas_harf_buyut(parca), "kategori_id": None, "tur": "bugun", "asama": None})
    return maddeler


def sesli_not_sistemi(proje_adi: str = "", kendi_sirket: list[str] | None = None) -> str:
    urun = f"Yazılım ürününün adı {proje_adi}; yazılımdan söz ederken bu adı kullan.\n" if proje_adi else ""
    return (
        "Bir müzik edisyon şirketinde çalışan bir danışmanın sesle dikte ettiği notu günlük rapor maddelerine "
        "bölüyorsun. Metin konuşmadan yazıya dökülmüştür; noktalama eksik ya da hatalı olabilir.\n"
        "Kurallar:\n"
        "- Her ayrı işi ayrı madde yap. Her madde yönetici raporuna uygun TEK cümle olur; sonunda nokta olur. "
        "Zaman kipi -di'li geçmiş zamandır (\"gönderildi\", \"görüşüldü\"); \"-mıştır\" kullanma.\n"
        "- Olgu ekleme, yorum katma. Kişi, kurum, ürün adları ve sayılar aynen korunur.\n"
        "- " + TIRNAK_KURALI + "\n"
        "- \"şey\", \"yani\", \"işte\", \"hani\", \"ııı\" gibi dolgu ifadelerini, tekrarları ve yarım kalmış "
        "sözleri at.\n"
        + ("- " + kendi_sirket_kurali(kendi_sirket) + "\n" if kendi_sirket else "")
        + "- Bir iş için \"devam ediyor\", \"sürüyor\", \"bekliyor\", \"bekleniyor\" gibi bir ifade varsa o madde "
        "\"tur\": \"devam\" olur: \"metin\" işin adıdır, \"asama\" kısa aşamasıdır (örn. \"yanıt bekleniyor\"). "
        "Diğer maddeler \"tur\": \"bugun\" olur ve \"asama\" null'dır.\n"
        "- Verilen rapor kategorilerinden konuya en uygun olanın id'sini \"kategori_id\" alanında döndür; "
        "emin değilsen null yaz.\n"
        + urun
        + "Yanıt olarak YALNIZ JSON dizi döndür, başka hiçbir şey yazma: "
        "[{\"metin\": \"...\", \"kategori_id\": <sayı ya da null>, \"tur\": \"bugun\" | \"devam\", \"asama\": \"...\" | null}]"
    )


def claude_sesli_bol(
    metin: str, api_anahtari: str, kategoriler: list[dict] | None = None, proje_adi: str = "",
    kendi_sirket: list[str] | None = None, istemci: httpx.Client | None = None, eslemeler: list[dict] | None = None,
) -> list[dict]:
    """Dikte metnini tek çağrıda maddelere böler: [{metin, kategori_id, tur, asama}].
    Hata, JSON olmayan ya da boş yanıt → ClaudeHatasi. Kategori id'lerinin geçerliliğini çağıran denetler.
    eslemeler girdiye ve çıktıya uygulanır; girdideki tırnaklı bir parça hiçbir maddede aynen yoksa ClaudeHatasi."""
    metin = ad_esle(metin, eslemeler)
    kategoriler = [{**k, "ad": ad_esle(k.get("ad"), eslemeler)} for k in kategoriler or []]
    proje_adi = ad_esle(proje_adi, eslemeler)
    kendi_sirket = [ad_esle(k, eslemeler) for k in kendi_sirket] if kendi_sirket else kendi_sirket
    istek = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1500,
        "thinking": {"type": "disabled"},
        "system": sesli_not_sistemi(proje_adi, kendi_sirket),
        "messages": [{
            "role": "user",
            "content": "Rapor kategorileri:\n" + json.dumps(kategoriler or [], ensure_ascii=False)
            + "\n\nDikte edilen not:\n" + metin,
        }],
    }
    yanit = _claude_cagir(istek, api_anahtari, istemci)
    try:
        dizi = _json_dizi_ayikla(yanit)
    except ValueError as e:
        raise ClaudeHatasi(f"yanıt JSON değil ({e})") from e
    maddeler = []
    for x in dizi:
        if not isinstance(x, dict) or not isinstance(x.get("metin"), str) or not x["metin"].strip():
            continue
        devam = x.get("tur") == "devam"
        asama = x.get("asama") if devam and isinstance(x.get("asama"), str) else None
        k = x.get("kategori_id")
        if isinstance(k, str) and k.strip().isdigit():
            k = int(k)
        maddeler.append({
            "metin": x["metin"].strip(),
            "kategori_id": k if isinstance(k, int) and not isinstance(k, bool) else None,
            "tur": "devam" if devam else "bugun",
            "asama": (asama or "").strip() or None,
        })
    if not maddeler:
        raise ClaudeHatasi("boş yanıt")
    if not tirnaklar_korundu(tirnak_parcalari(metin), "\n".join(m["metin"] + " " + (m["asama"] or "") for m in maddeler)):
        raise ClaudeHatasi("tırnak içindeki metin korunmadı")
    for m in maddeler:
        m["metin"] = ad_esle(m["metin"], eslemeler)
        m["asama"] = ad_esle(m["asama"], eslemeler) or None
    return maddeler


TR_AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def hafta_basligi(baslangic: date, bitis: date) -> str:
    """'14–18 Eylül 2026', '28 Eylül – 2 Ekim 2026', '29 Aralık 2025 – 2 Ocak 2026'."""
    if baslangic.year != bitis.year:
        return (f"{baslangic.day} {TR_AYLAR[baslangic.month - 1]} {baslangic.year} – "
                f"{bitis.day} {TR_AYLAR[bitis.month - 1]} {bitis.year}")
    if baslangic.month != bitis.month:
        return f"{baslangic.day} {TR_AYLAR[baslangic.month - 1]} – {bitis.day} {TR_AYLAR[bitis.month - 1]} {bitis.year}"
    if baslangic == bitis:
        return f"{baslangic.day} {TR_AYLAR[baslangic.month - 1]} {baslangic.year}"
    return f"{baslangic.day}–{bitis.day} {TR_AYLAR[bitis.month - 1]} {bitis.year}"


TR_KISA_AYLAR = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]
BIRLER = ["sıfır", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz"]
ONLAR = ["", "on", "yirmi", "otuz", "kırk", "elli"]


def saat_yonelme(saat: str) -> str:
    """'14:05' → "14:05'e", '14:30' → "14:30'a", '09:06' → "09:06'ya": ek saatin okunuşunun son sözcüğüne uyar."""
    ss, dd = (int(x) for x in saat.split(":"))
    n = dd or ss
    okunus = BIRLER[n % 10] if n % 10 or n == 0 else ONLAR[n // 10]
    return saat + yonelme_eki(okunus)[len(okunus):]


def gecerlilik_metni(bitis: datetime) -> str:
    """'Bağlantı 26 Eyl 14:05'e kadar geçerli' (Istanbul saati)."""
    z = bitis.astimezone(ISTANBUL)
    return f"Bağlantı {z.day} {TR_KISA_AYLAR[z.month - 1]} {saat_yonelme(z.strftime('%H:%M'))} kadar geçerli"


def haftalik_sistemi() -> str:
    return (
        "Bir müzik edisyon şirketinde çalışan bir danışmanın bir haftalık günlük raporlarından, yöneticisine "
        "WhatsApp'tan gönderilecek haftalık özet yazıyorsun.\n"
        "Biçim (WhatsApp): ilk satır verilen başlık aynen, sonra boş satır, maddeler '• ' ile başlar, "
        "*yıldızla* kalın yazı yalnız başlıklarda kullanılır, madde içinde kullanılmaz.\n"
        "Kurallar:\n"
        "- Maddeleri güne göre değil konuya göre grupla; toplam 6-10 madde.\n"
        "- Aynı işin tekrarlarını tek maddede birleştir.\n"
        "- Her gün tekrarlanan sürekli işlerin HEPSİNİ tek bir maddede topla (\"Hafta boyunca … ve … takip edildi\").\n"
        "- Zaman kipi -di'li geçmiş zamandır (\"gönderildi\", \"görüşüldü\"); \"-mıştır\" kullanma.\n"
        "- Hafta sonunda hâlâ devam eden işleri en sonda \"*Devam eden*\" başlığı altında ver.\n"
        "- Günlük raporlar *Başlık:* biçiminde kategori başlıklarıyla yazılmışsa bu başlıkları konu gruplaması "
        "için ipucu say.\n"
        "- Raporlarda olmayan hiçbir bilgiyi ekleme; isimler ve sayılar aynen kalır; geçmiş zaman.\n"
        "- " + TIRNAK_KURALI + "\n"
        "Yalnız özet metnini döndür, açıklama yazma."
    )


def claude_haftalik(
    raporlar: list[tuple[date, str]], baslik: str, api_anahtari: str, istemci: httpx.Client | None = None,
    eslemeler: list[dict] | None = None,
) -> str:
    """eslemeler: ad eşlemeleri günlük raporlara (girdi) ve özete (çıktı) uygulanır."""
    govde = "\n\n".join(f"### {t.isoformat()}\n{ad_esle(m, eslemeler)}" for t, m in raporlar)
    istek = {
        "model": CLAUDE_MODEL,
        "max_tokens": 2000,
        "thinking": {"type": "disabled"},
        "system": haftalik_sistemi(),
        "messages": [{
            "role": "user",
            "content": f"Başlık: *Haftalık Özet – {baslik}*\n\nGünlük raporlar:\n\n{govde}",
        }],
    }
    metin = _claude_cagir(istek, api_anahtari, istemci).strip()
    if not metin:
        raise ClaudeHatasi("boş yanıt")
    return ad_esle(metin, eslemeler)


# ---------------------------------------------------------------- aylık / yıllık özet (A1)

OZET_BASLIKLARI = {
    ("aylik", "patron"): "*Aylık Özet – {donem}*", ("yillik", "patron"): "*Yıllık Özet – {donem}*",
    ("aylik", "basari"): "Başarı Dökümü – {donem}", ("yillik", "basari"): "Başarı Dökümü – {donem}",
}
BASARI_BOLUMLERI = ("Öne çıkanlar", "Sorumluluk alanlarına göre", "Tamamlanan işler", "Sürekli üstlenilen işler", "Sayılarla")
KATEGORI_BASLIGI = re.compile(r"^\*(?P<ad>[^*\n]+?):\*$")
BOLUM_BASLIGI = re.compile(r"^\*[^*\n]+\*$")
EPOSTA_HEDEFI = re.compile(r"^(?P<hedef>.+?)(?:'(?:ya|ye|a|e)|(?<=[Bb]ir kişi)ye) (?='|konusuz |\d+ e-posta)")
EPOSTA_ADEDI = re.compile(r"\b(\d+) e-posta\b")
TAMAMLANDI = re.compile(r"\btamamlandı[.!]?\s*$", re.IGNORECASE)


def donem_adi(tur: str, baslangic: date) -> str:
    """'Eylül 2026' (aylık) ya da '2026' (yıllık)."""
    return f"{TR_AYLAR[baslangic.month - 1]} {baslangic.year}" if tur == "aylik" else str(baslangic.year)


def ozet_basligi(tur: str, bicim: str, baslangic: date) -> str:
    return OZET_BASLIKLARI[(tur, bicim)].format(donem=donem_adi(tur, baslangic))


def madde_satiri(satir: str) -> bool:
    return satir.lstrip().startswith("•")


def rapor_kisalt(metin: str, en_cok: int = 12) -> str:
    """Rapordan ilk en_cok madde; başlık satırı ve maddesi kalan bölüm başlıkları korunur, boşalan bölüm atılır."""
    satirlar, bekleyen, adet = [], [], 0
    for satir in (metin or "").splitlines():
        if madde_satiri(satir):
            if adet < en_cok:
                satirlar += bekleyen + [satir]
                bekleyen, adet = [], adet + 1
        elif BOLUM_BASLIGI.match(satir.strip()) and satirlar:
            bekleyen = [satir]
        elif not satirlar:
            satirlar.append(satir)  # rapor başlığı
    return "\n".join(satirlar).strip()


def rapor_madde_sayisi(metin: str) -> int:
    """Rapordaki madde sayısı; 'Yarın' bölümündeki plan maddeleri sayılmaz."""
    adet, yarin = 0, False
    for satir in (metin or "").splitlines():
        satir = satir.strip()
        if BOLUM_BASLIGI.match(satir):
            yarin = _kucult(satir.strip("*: ")) == "yarın"
        elif madde_satiri(satir) and not yarin:
            adet += 1
    return adet


def kategori_sayilari(metinler: list[str]) -> dict[str, int]:
    """Kategorili günlük raporlarda '*Ad:*' başlığı altındaki madde sayıları ('Yarın' hariç)."""
    sayilar: dict[str, int] = {}
    for metin in metinler:
        bolum = None
        for satir in (metin or "").splitlines():
            satir = satir.strip()
            if BOLUM_BASLIGI.match(satir):
                m = KATEGORI_BASLIGI.match(satir)
                bolum = m.group("ad").strip() if m and _kucult(m.group("ad").strip()) != "yarın" else None
            elif bolum and madde_satiri(satir):
                sayilar[bolum] = sayilar.get(bolum, 0) + 1
    return sayilar


def eposta_hedefleri(metin: str) -> list[tuple[str, int]]:
    """E-posta maddesinin (ham metin) alıcı kurum/kişileri ve e-posta adedi: "MESAM'a 2 e-posta …" → [("MESAM", 2)].
    'Şirket içi', 'Ekip içi', adsız 'bir kişi' ve genel sağlayıcı alan adları (gmail.com …) sayılmaz; görünen adla
    yazılan kişi (A2) kendi adıyla sayılır. Biçim tanınmazsa (elle düzenlenmiş) boş."""
    m = EPOSTA_HEDEFI.match(metin or "")
    if not m:
        return []
    hedef = m.group("hedef")
    bas, _, son = hedef.rpartition(" ve ")
    adlar = (bas.split(", ") if bas else []) + [son]
    adet = int(a.group(1)) if (a := EPOSTA_ADEDI.search(metin)) else 1
    return [(ad.strip(), adet) for ad in adlar if ad.strip() and _kucult(ad.strip()) not in (BIR_KISI, SIRKET_ICI, EKIP_ICI)
            and _kucult(ad.strip()) not in GENEL_SAGLAYICILAR]


def istatistik_metni(ist: dict) -> str:
    """İstatistik sözlüğü → prompt'taki düz metin; 'Sayılarla' bölümü yalnız bu sayılardan yazılır."""
    satirlar = [
        f"- Rapor gönderilen gün: {ist['rapor_gunu']} (elle {ist['elle']}, otomatik {ist['otomatik']})",
        f"- İş günü: {ist['is_gunu']}" + (f"; kapsama: %{ist['kapsama']}" if ist.get("kapsama") is not None else ""),
        f"- Rapora giren toplam madde: {ist['toplam_madde']}",
        *([f"- Kapsam: {ist['kullanim']['metin']}"] if (ist.get("kullanim") or {}).get("metin") else []),
        "- Kaynağa göre madde: " + ", ".join(f"{k['ad'].lower()} {k['sayi']}" for k in ist["kaynaklar"]),
    ]
    if ist["kategoriler"]:
        satirlar.append("- En çok madde çıkan kategoriler: " + ", ".join(f"{k['ad']} ({k['sayi']})" for k in ist["kategoriler"]))
    if ist["kurumlar"]:
        satirlar.append("- En çok yazışılan kurum/kişiler: " + ", ".join(f"{k['ad']} ({k['sayi']})" for k in ist["kurumlar"]))
    satirlar.append(f"- Tamamlanan devam eden iş: {len(ist['tamamlanan'])}"
                    + (": " + "; ".join(t["metin"] for t in ist["tamamlanan"]) if ist["tamamlanan"] else ""))
    satirlar.append(f"- Hâlâ açık devam eden iş: {len(ist['acik'])}"
                    + (": " + "; ".join(f"{a['metin']} ({a['gun']} gündür)" for a in ist["acik"]) if ist["acik"] else ""))
    satirlar.append(f"- Önemli işaretlenen madde: {ist['onemli']}")
    return "\n".join(satirlar)


# A1v2: sentez Claude'dan yapılandırılmış JSON olarak gelir; sunucu doğrular, sayıları kendisi hesaplar ve düz metni
# (kopyalama / WhatsApp) yapıdan üretir.
OZET_KAYNAK_ETIKETI = {"elle": "elle", "ses": "elle", "not": "not", "eposta": "eposta", "outlook": "eposta",
                       "takvim": "toplanti", "drive": "dosya", "onedrive": "dosya", "medusa": "uygulama"}
ONE_CIKAN_EN_COK = 5
ETIKET_EN_COK = 5
DIGER = "Diğer"
PATRON_TAMAMLANAN = "Tamamlananlar"
PATRON_DEVAM = "Devam Eden İşler"


def ozet_sistemi(tur: str, bicim: str, kendi_sirket: list[str] | None = None) -> str:
    donem = "ay" if tur == "aylik" else "yıl"
    if tur == "aylik":
        girdi = ("Girdi JSON: \"maddeler\" dönemde rapora giren iş maddeleri {id, tarih, kategori, kaynak, metin}; "
                 "kategori maddenin günlük raporda girdiği bölümdür, gruplamada ipucu say. kaynak: eposta (gönderilen "
                 "e-posta), uygulama (yazılımda yapılan değişiklik), toplanti, dosya, elle, not. \"surekli\" her gün "
                 "tekrarlanan işler, \"devam_eden\" hâlâ açık işler, \"tamamlanan\" dönemde tamamlanan işler; varsa "
                 "\"haftalik_ozetler\" ve \"gunluk_raporlar\" yalnız bağlam içindir.\n")
    else:
        girdi = ("Girdi JSON: \"aylar\" her ayın dökümü; temalar {id, ad, ozet, etiketler, madde_sayisi} biçimindedir, "
                 "tema id'si \"ay-sıra\" dizesidir. Özeti olmayan aylar istatistik ve örnek maddelerle ({id, metin}) "
                 "gelir. madde_idleri alanına tema id'lerini ve örnek madde id'lerini yaz; sayıları sunucu hesaplar.\n")
    ortak = (
        "- Girdide olmayan hiçbir bilgiyi ekleme, uydurma; kişi, kurum, ürün ve eser adları aynen kalır.\n"
        "- Sayı yazacaksan yalnız verilen istatistikteki sayıları birebir kullan; hesaplama, yuvarlama, tahmin yapma.\n"
        "- Zaman kipi -di'li geçmiş zamandır (\"gönderildi\", \"tamamlandı\"); \"-mıştır\" ve birinci tekil şahıs kullanma.\n"
        "- Abartı ve övgü sıfatı yok (\"büyük başarı\", \"yoğun çaba\" gibi).\n"
        "- " + TIRNAK_KURALI + "\n"
        + ("- " + kendi_sirket_kurali(kendi_sirket) + "\n" if kendi_sirket else "")
        + "- Metinlerde yıldız (*), # ya da Markdown kullanma.\n"
    )
    if bicim == "patron":
        return (
            f"Bir müzik edisyon şirketinde çalışan bir danışmanın bir {donem}lık iş kayıtlarından, yöneticisine "
            f"WhatsApp'tan gönderilecek {'aylık' if tur == 'aylik' else 'yıllık'} özetin içeriğini çıkarıyorsun.\n"
            + girdi +
            "YALNIZ şu JSON nesnesini döndür, başka hiçbir şey yazma:\n"
            '{"bolumler":[{"ad":"…","maddeler":["…"]}],"tamamlanan":["…"],"devam_eden":["…"]}\n'
            "Kurallar:\n"
            "- bolumler konuya göre gruplanır (güne göre değil); bütün bölümlerde toplam 8-12 madde; benzer ve tekrar "
            "eden işler tek maddede birleşir; her madde tek cümle.\n"
            "- Her gün tekrarlanan sürekli işlerin hepsi tek maddede toplanır.\n"
            "- tamamlanan: dönemde tamamlanan işlerin kısa adları. devam_eden: hâlâ açık işler, aşaması varsa "
            "\" — \" ile (\"MSG Ağustos itirazı — yanıt bekleniyor\").\n"
            + ortak
        )
    return (
        f"Bir müzik edisyon şirketinde çalışan bir danışmanın bir {donem}lık iş kayıtlarından, danışmanın kendisi "
        "için yapılandırılmış bir başarı dökümü çıkarıyorsun (performans görüşmesi ve kendi kaydı için).\n"
        + girdi +
        "YALNIZ şu JSON nesnesini döndür, başka hiçbir şey yazma:\n"
        '{"one_cikanlar":[{"baslik":"…","aciklama":"…"}],'
        '"alanlar":[{"ad":"…","temalar":[{"ad":"…","ozet":"…","etiketler":["…"],"madde_idleri":[1,2]}]}],'
        '"tamamlanan":["…"],"devam_eden":["…"],"surekli":"…"}\n'
        "Kurallar:\n"
        f"- one_cikanlar: 3-5 öğe; baslik kısa bir sonuç cümlesi (\"Lisanslama modülü devreye alındı.\"), aciklama "
        "tek cümle.\n"
        "- alanlar: sorumluluk alanları (ör. \"Edisyon uygulaması\", \"Meslek birlikleri ve eser talepleri\"); her "
        "alanda 2-6 tema. Tema adı kısa iş başlığı; ozet 1-2 cümle; teknik ayrıntıyı iş sonucuna çevir, maddeleri "
        "tek tek sayma.\n"
        f"- etiketler: en çok {ETIKET_EN_COK}, kısa (1-4 kelime) somut konu, ürün ya da eser adı.\n"
        "- madde_idleri: temaya giren maddelerin id'leri; her madde yalnız bir temaya girer. Madde sayısı yazma.\n"
        "- tamamlanan: dönemde tamamlanan işlerin kısa adları; devam_eden: hâlâ açık işler, aşaması varsa \" · \" ile "
        "(\"Konser raporu işleme · canlı veri\").\n"
        "- surekli: her gün ya da düzenli yürütülen işlerin tek satırlık özeti, parçalar \" · \" ile ayrılır.\n"
        + ortak
    )


def _json_nesne_ayikla(metin: str) -> dict:
    bas, son = metin.find("{"), metin.rfind("}")
    if bas == -1 or son <= bas:
        raise ValueError("yanıtta JSON nesne yok")
    veri = json.loads(metin[bas: son + 1])
    if not isinstance(veri, dict):
        raise ValueError("yanıt JSON nesne değil")
    return veri


def ozet_json_gecerli(bicim: str, veri: dict) -> bool:
    """Sözleşmenin iskeleti: başarıda öne çıkanlar ve alanlar, patronda bölümler dizi olmalı."""
    if bicim == "patron":
        return isinstance(veri.get("bolumler"), list)
    return isinstance(veri.get("alanlar"), list) and isinstance(veri.get("one_cikanlar"), list)


def claude_ozet(
    tur: str, bicim: str, baslik: str, girdi: dict, istatistik: str, api_anahtari: str,
    kendi_sirket: list[str] | None = None, eslemeler: list[dict] | None = None, istemci: httpx.Client | None = None,
) -> dict:
    """Aylık/yıllık özet ya da başarı dökümünün yapılandırılmış hâli (doğrulanmamış ham JSON nesnesi). girdi: API'nin
    hazırladığı, ad eşlemesi uygulanmış JSON. Yanıt JSON değilse ya da iskeleti bozuksa bir kez yeniden sorulur; yine
    bozuksa ClaudeHatasi. İki çağrının kullanımı toplanır."""
    kendi_sirket = [ad_esle(k, eslemeler) for k in kendi_sirket] if kendi_sirket else kendi_sirket
    istek = {
        "model": CLAUDE_MODEL,
        "max_tokens": 4000,
        "thinking": {"type": "disabled"},
        "system": ozet_sistemi(tur, bicim, kendi_sirket),
        "messages": [{
            "role": "user",
            "content": f"Başlık: {baslik}\n\nİstatistik (sayılar yalnız buradan):\n{ad_esle(istatistik, eslemeler)}\n\n"
                       f"Girdi:\n{json.dumps(girdi, ensure_ascii=False)}",
        }],
    }
    toplam = {"girdi": 0, "cikti": 0}
    neden = ""
    for _ in range(2):
        metin = _claude_cagir(istek, api_anahtari, istemci)
        toplam = {k: toplam[k] + son_kullanim()[k] for k in toplam}
        _yerel.kullanim = toplam
        try:
            veri = _json_nesne_ayikla(metin)
        except (ValueError, TypeError) as e:
            neden = str(e)
            continue
        if ozet_json_gecerli(bicim, veri):
            return veri
        neden = "beklenen alanlar yok"
    raise ClaudeHatasi(f"yanıt JSON sözleşmesine uymuyor ({neden})")


def _yazi(deger, sinir: int = 600) -> str:
    """Serbest metin alanı: dize (ya da sayı) → tek satır, baştaki madde imi ve yıldızlar atılır."""
    if isinstance(deger, bool) or not isinstance(deger, (str, int, float)):
        return ""
    metin = re.sub(r"\s+", " ", str(deger)).replace("*", "").strip()
    return re.sub(r"^[•\-–]\s*", "", metin)[:sinir].strip()


def _yazilar(deger, sinir: int = 60) -> list[str]:
    liste = deger if isinstance(deger, list) else ([deger] if isinstance(deger, str) else [])
    return [y for y in (_yazi(x) for x in liste[:sinir]) if y]


def _anahtar(x):
    """madde_idleri öğesi: sayı (madde id'si) ya da yıllıkta "ay-sıra" tema anahtarı."""
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, float) and x.is_integer():
        return int(x)
    if isinstance(x, str):
        x = x.strip()
        return int(x) if x.isdigit() else x or None
    return None


def _esle_ve_temizle(metin: str, eslemeler) -> str:
    return re.sub(r"\s{2,}", " ", ad_esle(metin, eslemeler)).strip()


def yapi_esle(yapi, eslemeler):
    """Yapıdaki bütün metin alanlarına ad eşlemesi (madde id'leri ve sayılar dokunulmaz)."""
    if isinstance(yapi, str):
        return ad_esle(yapi, eslemeler)
    if isinstance(yapi, list):
        return [yapi_esle(x, eslemeler) for x in yapi]
    if isinstance(yapi, dict):
        return {k: v if k in ("madde_idleri", "sayilar", "bicim", "surum") else yapi_esle(v, eslemeler) for k, v in yapi.items()}
    return yapi


def ozet_yapisini_dogrula(
    bicim: str, ham: dict, bilinen: dict | None = None, eslemeler: list[dict] | None = None,
    kendi_sirket: list[str] | None = None, duzenleme: bool = False,
) -> dict:
    """Claude'un (ya da düzenlemenin) JSON'unu sözleşmeye indirger.
    bilinen: {anahtar: (madde id'leri, metin)}; aylıkta anahtar madde id'sidir, yıllıkta ayrıca "ay-sıra" tema anahtarı
    (o temanın madde id'lerine açılır). Bilinmeyen id atılır; bir madde yalnız ilk temasında sayılır; tema sayısı geçerli
    madde id'lerinin sayısıdır. Hiçbir temaya girmeyen madde "Diğer" temasına eklenir (düzenlemede eklenmez; boş temalar
    düzenlemede kalır). Ad eşlemeleri ve E2 (kendi şirketi, 'şirket içi' ve 'ekip içi' etiket olmaz) son adımda uygulanır."""
    bilinen = bilinen or {}
    kendi = {_kucult(ad_esle(k, eslemeler)) for k in kendi_sirket or []} | {SIRKET_ICI, EKIP_ICI}
    esle = lambda s: _esle_ve_temizle(s, eslemeler)  # noqa: E731
    if bicim == "patron":
        bolumler = []
        for b in ham.get("bolumler") or []:
            if not isinstance(b, dict):
                continue
            ad, maddeler = esle(_yazi(b.get("ad"), 120)).rstrip(":").strip(), [esle(m) for m in _yazilar(b.get("maddeler"))]
            if ad and (maddeler or duzenleme):
                bolumler.append({"ad": ad, "maddeler": [m for m in maddeler if m]})
        return {"bicim": "patron", "bolumler": bolumler,
                "tamamlanan": [esle(x) for x in _yazilar(ham.get("tamamlanan"))],
                "devam_eden": [esle(x) for x in _yazilar(ham.get("devam_eden"))]}

    kullanilan: set[int] = set()

    def ac(x) -> list[int]:
        k = _anahtar(x)
        return list(bilinen[k][0]) if k in bilinen else []

    alanlar = []
    for a in ham.get("alanlar") or []:
        if not isinstance(a, dict):
            continue
        temalar = []
        for t in a.get("temalar") or []:
            if not isinstance(t, dict):
                continue
            idler = []
            for x in t.get("madde_idleri") or []:
                for i in ac(x):
                    if i not in kullanilan:
                        kullanilan.add(i)
                        idler.append(i)
            tema = {
                "ad": esle(_yazi(t.get("ad"), 120)), "ozet": esle(_yazi(t.get("ozet"), 800)),
                "etiketler": [e for e in (esle(_yazi(e, 60)) for e in _yazilar(t.get("etiketler")))
                              if e and _kucult(e) not in kendi][:ETIKET_EN_COK],
                "madde_idleri": idler, "madde_sayisi": len(idler),
            }
            if (tema["ad"] or tema["ozet"]) and (idler or duzenleme):
                temalar.append(tema)
        ad = esle(_yazi(a.get("ad"), 120))
        if ad and (temalar or duzenleme):
            alanlar.append({"ad": ad, "temalar": temalar})
    if not duzenleme:
        atanmamis = [(k, ids, metin) for k, (ids, metin) in bilinen.items() if ids and not kullanilan & set(ids)]
        if atanmamis:
            idler = [i for _, ids, _ in atanmamis for i in ids if i not in kullanilan]
            ornek = list(dict.fromkeys(o for o in (esle(_yazi(m, 160)).rstrip(".") for _, _, m in atanmamis) if o))
            tema = {"ad": DIGER, "ozet": "; ".join(ornek[:3]) + ("; …" if len(ornek) > 3 else "."),
                    "etiketler": [], "madde_idleri": idler, "madde_sayisi": len(idler)}
            hedef = next((a for a in alanlar if _kucult(a["ad"]) == _kucult(DIGER)), None)
            if hedef is None:
                alanlar.append({"ad": DIGER, "temalar": [tema]})
            else:
                hedef["temalar"].append(tema)
    for a in alanlar:
        a["madde_sayisi"] = sum(t["madde_sayisi"] for t in a["temalar"])
    one = []
    for o in ham.get("one_cikanlar") or []:
        if isinstance(o, dict):
            baslik, aciklama = esle(_yazi(o.get("baslik"), 200)), esle(_yazi(o.get("aciklama"), 600))
        else:
            baslik, aciklama = esle(_yazi(o, 200)), ""
        if baslik or aciklama:
            one.append({"baslik": baslik, "aciklama": aciklama})
    surekli = ham.get("surekli")
    surekli = " · ".join(_yazilar(surekli)) if isinstance(surekli, list) else _yazi(surekli, 600)
    return {
        "bicim": "basari", "one_cikanlar": one if duzenleme else one[:ONE_CIKAN_EN_COK], "alanlar": alanlar,
        "tamamlanan": [esle(x) for x in _yazilar(ham.get("tamamlanan"))],
        "devam_eden": [esle(x) for x in _yazilar(ham.get("devam_eden"))],
        "surekli": esle(surekli),
    }


def cumle_sonu(metin: str) -> str:
    metin = metin.strip()
    return metin if not metin or metin[-1] in ".!?…:" else metin + "."


def one_cikan_metni(o: dict) -> str:
    return " ".join(x for x in (cumle_sonu(o.get("baslik") or ""), o.get("aciklama") or "") if x).strip()


def sayilar_satiri(s: dict) -> str:
    """'21/22 iş günü rapor · 287 madde · 64 kurumsal e-posta · 13 toplantı · 21 dosya · 9 tamamlanan iş'; sıfırlar yazılmaz."""
    parcalar = [f"{s.get('rapor_gunu', 0)}/{s.get('is_gunu', 0)} iş günü rapor"] if s.get("is_gunu") else []
    for anahtar, ad in (("toplam_madde", "madde"), ("eposta", "kurumsal e-posta"), ("uygulama", "uygulama çalışması"),
                        ("toplanti", "toplantı"), ("dosya", "dosya"), ("tamamlanan", "tamamlanan iş")):
        if s.get(anahtar):
            parcalar.append(f"{s[anahtar]} {ad}")
    return " · ".join(parcalar)


def patron_metni(baslik: str, yapi: dict) -> str:
    """Patron JSON'undan WhatsApp metni: başlık, '*Bölüm:*' + maddeler, sonda Tamamlananlar ve Devam Eden İşler."""
    satirlar = [baslik, ""]
    for b in yapi.get("bolumler") or []:
        if b.get("maddeler"):
            satirlar += [f"*{b['ad']}:*"] + [f"• {m}" for m in b["maddeler"]] + [""]
    if yapi.get("tamamlanan"):
        satirlar += [f"*{PATRON_TAMAMLANAN}:*", "• " + " · ".join(yapi["tamamlanan"]), ""]
    if yapi.get("devam_eden"):
        satirlar += [f"*{PATRON_DEVAM}:*"] + [f"• {d}" for d in yapi["devam_eden"]] + [""]
    return "\n".join(satirlar).strip()


def basari_metni(baslik: str, yapi: dict) -> str:
    """Başarı dökümünün düz metin karşılığı (kopyalama için); bölüm sırası PDF'le aynı."""
    satirlar = [baslik, ""]

    def bolum(ad: str, maddeler: list[str]):
        if maddeler:
            satirlar.extend([ad] + [f"• {m}" for m in maddeler] + [""])

    bolum(BASARI_BOLUMLERI[0], [one_cikan_metni(o) for o in yapi.get("one_cikanlar") or []])
    if yapi.get("alanlar"):
        satirlar.append(BASARI_BOLUMLERI[1])
        for a in yapi["alanlar"]:
            satirlar.append(f"{a['ad']} · {a.get('madde_sayisi', 0)} madde")
            for t in a.get("temalar") or []:
                ek = f" [{', '.join(t['etiketler'])}]" if t.get("etiketler") else ""
                satirlar.append(f"• {t['ad']} ({t.get('madde_sayisi', 0)} madde): {t.get('ozet') or ''}{ek}".rstrip(": "))
        satirlar.append("")
    bolum(BASARI_BOLUMLERI[2], yapi.get("tamamlanan") or [])
    bolum("Devam eden işler", yapi.get("devam_eden") or [])
    bolum(BASARI_BOLUMLERI[3], [yapi["surekli"]] if yapi.get("surekli") else [])
    bolum(BASARI_BOLUMLERI[4], [sayilar_satiri(yapi.get("sayilar") or {})] if sayilar_satiri(yapi.get("sayilar") or {}) else [])
    return "\n".join(satirlar).strip()


def ozet_metni(baslik: str, yapi: dict) -> str:
    return patron_metni(baslik, yapi) if yapi.get("bicim") == "patron" else basari_metni(baslik, yapi)


def surekli_varyant(metin: str, tarih: date) -> str:
    """'a | b | c' → yılın gününe göre biri (arayüzdeki eski variantOf ile aynı)."""
    parcalar = [p.strip() for p in metin.split("|") if p.strip()]
    if len(parcalar) <= 1:
        return metin.strip()
    return parcalar[tarih.timetuple().tm_yday % len(parcalar)]


# ---------------------------------------------------------------- birleştirme

def raporu_uret(
    ayarlar: dict, api_anahtari: str = "", haric_idler: set[str] | frozenset | dict[str, str | None] = frozenset(),
    tarih: date | None = None,
) -> dict:
    """ayarlar: gmail_kullanici, gmail_sifre, github_token, github_repo, proje_adi, alan_sozlugu, eposta_gruplama,
    kendi_alanlar, ekip_ici_atla, kendi_sirket (çözülmüş), ad_eslemeleri; Google bağlıysa google: {token, kapsamlar, eposta,
    yenile, hata} (token yoksa Google kaynakları atlanır); Microsoft bağlıysa microsoft: aynı biçimde.
    haric_idler: zaten kayıtlı maddeler; Claude'a yeniden gönderilmez. Sözlükse id → kayıtlı metin: metni
    değişen (ör. aynı konuya yeni mail gelen) madde yeniden çevrilir; değer None ise hiç gönderilmez.
    tarih: taranan gün (Istanbul); verilmezse bugün.
    Dönen "taranan": hatasız taranan OAuth kaynakları (outlook, takvim, drive, onedrive); takvim yalnız açık olan her
    sağlayıcı hatasız tarandıysa yazılır. Eski maddeleri gizleme kuralı buna bakar."""
    bugun = tarih or istanbul_bugun()
    sonuc = {"tarih": bugun.isoformat(), "eposta": [], "outlook": [], "medusa": [], "not": [], "takvim": [], "drive": [],
             "onedrive": [], "hatalar": [], "taranan": []}
    # Kapalı kaynak sessizce atlanır; açık ama ayarı eksik olan uyarı yazar.
    acik = ayarlar.get("kaynaklar") or {"gmail": True, "github": True}
    google = ayarlar.get("google") or {}
    token, kapsam = google.get("token"), set(google.get("kapsamlar") or [])
    if google.get("yenile"):
        sonuc["hatalar"].append({"kaynak": "google", "mesaj": GOOGLE_YENILE_MESAJI})
    elif google.get("hata"):
        sonuc["hatalar"].append({"kaynak": "google", "mesaj": google["hata"]})
    ms = ayarlar.get("microsoft") or {}
    ms_token, ms_kapsam, ms_adres = ms.get("token"), set(ms.get("kapsamlar") or []), ms.get("eposta") or ""
    if ms.get("yenile"):
        sonuc["hatalar"].append({"kaynak": "microsoft", "mesaj": MICROSOFT_YENILE_MESAJI})
    elif ms.get("hata"):
        sonuc["hatalar"].append({"kaynak": "microsoft", "mesaj": ms["hata"]})

    def tara(kaynak: str, ad: str, islem):
        """Kaynak hatası sonuca yazılır, None döner."""
        try:
            return islem()
        except YetkisizErisim as e:
            sonuc[f"{e.saglayici}_yetkisiz"] = True
            sonuc["hatalar"].append({"kaynak": kaynak, "mesaj": str(e)})
        except KaynakHatasi as e:
            sonuc["hatalar"].append({"kaynak": kaynak, "mesaj": str(e)})
        except Exception as e:
            sonuc["hatalar"].append({"kaynak": kaynak, "mesaj": f"{ad} taranamadı: {e.__class__.__name__}"})
        return None

    # Gmail: Google bağlıysa REST, değilse (ya da Google yenilenmeliyse) uygulama şifresiyle IMAP.
    bulunan = None
    eposta_ayari = dict(sozluk=ayarlar.get("alan_sozlugu"), gruplama=ayarlar.get("eposta_gruplama") or "konu",
                        kendi_alanlar=ayarlar.get("kendi_alanlar"), ekip_ici_atla=ayarlar.get("ekip_ici_atla", True))
    if acik.get("gmail") and token and "gmail" in kapsam:
        bulunan = tara("gmail", "Gmail", lambda: google_gmail_tara(
            token, google.get("eposta") or ayarlar.get("gmail_kullanici") or "", bugun, **eposta_ayari))
    elif acik.get("gmail") and ayarlar.get("gmail_kullanici") and ayarlar.get("gmail_sifre"):
        bulunan = tara("gmail", "Gmail", lambda: gmail_tara(
            ayarlar["gmail_kullanici"], ayarlar["gmail_sifre"], bugun, eposta_ayari["sozluk"], eposta_ayari["gruplama"],
            kendi_alanlar=eposta_ayari["kendi_alanlar"], ekip_ici_atla=eposta_ayari["ekip_ici_atla"],
        ))
    elif acik.get("gmail") and "gmail" not in kapsam:  # Google'la gelen Gmail'in hatası yukarıda yazıldı
        sonuc["hatalar"].append({"kaynak": "gmail", "mesaj": "Gmail ayarı girilmemiş (Ayarlar)"})
    if bulunan is not None:
        sonuc["eposta"] = [m for m in bulunan if m["kaynak"] != "not"]
        sonuc["not"] = [m for m in bulunan if m["kaynak"] == "not"]

    # Outlook: Gmail'den bağımsız; ikisi de açıksa ikisi de taranır.
    if acik.get("outlook") and ms_token and "outlook" in ms_kapsam:
        bulunan = tara("outlook", "Outlook", lambda: outlook_tara(ms_token, ms_adres, bugun, **eposta_ayari))
        if bulunan is not None:
            sonuc["outlook"] = [m for m in bulunan if m["kaynak"] != "not"]
            sonuc["not"] += [m for m in bulunan if m["kaynak"] == "not"]
            sonuc["taranan"].append("outlook")

    if acik.get("github") and ayarlar.get("github_token") and ayarlar.get("github_repo"):
        commitler = tara("github", "GitHub", lambda: github_tara(ayarlar["github_token"], ayarlar["github_repo"], bugun))
        sonuc["medusa"] = commitler or []
    elif acik.get("github"):
        sonuc["hatalar"].append({"kaynak": "github", "mesaj": "GitHub ayarı girilmemiş (Ayarlar)"})

    sozluk, kendi = ayarlar.get("alan_sozlugu"), ayarlar.get("kendi_alanlar")
    google_acik = lambda k: bool(acik.get(k) and token and k in kapsam)  # noqa: E731
    ms_acik = lambda k: bool(acik.get(k) and ms_token and k in ms_kapsam)  # noqa: E731
    for kaynak, isler in (
        ("takvim", [(google_acik("takvim"), "Takvim", lambda: takvim_tara(token, bugun, sozluk, kendi)),
                    (ms_acik("outlook_takvim"), "Takvim (Microsoft)",
                     lambda: outlook_takvim_tara(ms_token, ms_adres, bugun, sozluk, kendi))]),
        ("drive", [(google_acik("drive"), "Drive", lambda: drive_tara(token, bugun))]),
        ("onedrive", [(ms_acik("onedrive"), "OneDrive", lambda: onedrive_tara(ms_token, ms_adres, bugun))]),
    ):
        hatasiz = None
        for calisir, ad, islem in isler:
            if not calisir:
                continue
            maddeler = tara(kaynak, ad, islem)
            hatasiz = hatasiz is not False and maddeler is not None
            sonuc[kaynak] += maddeler or []
        if hatasiz:
            sonuc["taranan"].append(kaynak)

    def kayitli(m: dict) -> bool:
        if m["id"] not in haric_idler:
            return False
        return not isinstance(haric_idler, dict) or haric_idler[m["id"]] in (None, m["metin"])

    cevrilenler = ("eposta", "outlook", "medusa", "takvim", "drive", "onedrive")
    yeniler = [m for anahtar in cevrilenler for m in sonuc[anahtar] if not kayitli(m)]
    if api_anahtari and yeniler:
        cevrilmis, hata = claude_cevir(yeniler, api_anahtari, proje_adi=ayarlar.get("proje_adi") or "",
                                       kendi_sirket=ayarlar.get("kendi_sirket"), eslemeler=ayarlar.get("ad_eslemeleri"))
        if not hata:  # metin ham kalır; çeviri metin_ai'ye gider
            metinler = {m["id"]: m["metin"] for m in cevrilmis}
            for anahtar in cevrilenler:
                sonuc[anahtar] = [{**m, "metin_ai": metinler[m["id"]]} if m["id"] in metinler else m for m in sonuc[anahtar]]
        else:
            sonuc["hatalar"].append({"kaynak": "claude", "mesaj": hata})
    return sonuc


# ---------------------------------------------------------------- hatırlatma: e-posta ve web push

VARSAYILAN_GONDEREN = "rapor@medusarights.com"
GONDEREN_EKI = "Günlük Rapor"


def gonderen_adresi() -> str:
    """EPOSTA_GONDEREN'in yalnız adres kısmı ("Günlük Rapor <a@b.com>" → "a@b.com"); boş, ASCII dışı ya da
    '@'siz değerde varsayılan adres. From'a her zaman bu saf adres, görünen ad ayrıca eklenir."""
    ham = (os.environ.get("EPOSTA_GONDEREN") or "").strip()
    if not ham:
        return VARSAYILAN_GONDEREN
    adres = parseaddr(ham)[1].strip()
    if not adres.isascii() or "@" not in adres or "<" in adres or ">" in adres:
        log.warning("EPOSTA_GONDEREN geçerli bir ASCII adres içermiyor; varsayılan gönderen kullanılıyor")
        return VARSAYILAN_GONDEREN
    return adres


def gonderen_basligi(adres: str, ad: str | None = None) -> str:
    """From başlığı, tırnaksız: 'Ayşe Yılmaz - Günlük Rapor <rapor@…>'; ad boşsa 'Günlük Rapor <rapor@…>'.
    Resend tırnaklı görünen adı reddettiği için özel karakterler kaçışlanmaz, atılır (başlık enjeksiyonu da olmaz)."""
    ad = re.sub(r'[",<>;:@\\()\[\]]', " ", "".join(h if h.isprintable() else " " for h in (ad or "")))
    ad = re.sub(r"\s+", " ", ad).strip()[:60].strip()
    return f"{ad} - {GONDEREN_EKI} <{adres}>" if ad else f"{GONDEREN_EKI} <{adres}>"


def _resend_gonder(kime: str, konu: str, metin: str, yanit_adresi: str | None, anahtar: str,
                   istemci: httpx.Client | None = None, kopya: str | None = None,
                   gonderen_adi: str | None = None) -> str | None:
    """Render free planı giden SMTP portlarını kapatıyor; sistem e-postaları HTTPS ile gider."""
    govde = {"from": gonderen_basligi(gonderen_adresi(), gonderen_adi), "to": [kime], "subject": konu, "text": metin}
    if yanit_adresi:
        govde["reply_to"] = yanit_adresi
    if kopya:
        govde["cc"] = [kopya]
    istemci = istemci or httpx.Client(timeout=20)
    try:
        yanit = istemci.post(
            "https://api.resend.com/emails", json=govde,
            headers={"Authorization": f"Bearer {anahtar}", "content-type": "application/json"},
        )
    except httpx.HTTPError as e:
        return f"E-posta gönderilemedi ({e.__class__.__name__})"
    except Exception as e:
        return f"E-posta gönderilemedi: beklenmeyen hata ({e.__class__.__name__})"
    if 200 <= yanit.status_code < 300:
        return None
    try:
        neden = yanit.json().get("message") or yanit.text
    except Exception:
        neden = yanit.text
    ilk_hata = f"Resend {yanit.status_code}: {(neden or '').strip()[:120]}"
    if not (yanit.status_code == 422 and "from" in (neden or "").lower()):
        return ilk_hata
    # Güvenlik ağı: gönderen reddedilirse önce RFC 2047 kodlu görünen ad, sonra yalnız saf adres denenir (en fazla 3 deneme)
    adres = gonderen_adresi()
    gorunen = gonderen_basligi(adres, gonderen_adi).rsplit(" <", 1)[0]
    yedekler = [f"{Header(gorunen, 'utf-8').encode(maxlinelen=0)} <{adres}>", adres]
    for sira, gonderen in enumerate(yedekler, 2):
        log.warning("gönderen adı reddedildi, %d. deneme: %s", sira, gonderen)
        try:
            yanit = istemci.post(
                "https://api.resend.com/emails", json={**govde, "from": gonderen},
                headers={"Authorization": f"Bearer {anahtar}", "content-type": "application/json"},
            )
        except Exception:
            return ilk_hata
        if 200 <= yanit.status_code < 300:
            log.warning("gönderen adı reddedildi, varsayılana düşüldü (%d. deneme başarılı)", sira)
            return None
    return ilk_hata


def kullanilan_gonderen(gmail_kullanici: str = "", gonderen_adi: str | None = None) -> str:
    """Teşhis için: eposta_gonder'in ilk denemede kullanacağı From başlığı (gizli bilgi içermez)."""
    if (os.environ.get("RESEND_API_KEY") or "").strip():
        return gonderen_basligi(gonderen_adresi(), gonderen_adi)
    return gonderen_basligi(gmail_kullanici, gonderen_adi)


def _smtp_gonder(gmail_kullanici: str, gmail_sifre: str, kime: str, konu: str, metin: str,
                 yanit_adresi: str | None, kopya: str | None = None, gonderen_adi: str | None = None) -> str | None:
    """Yerel geliştirme yedeği: kullanıcının kendi Gmail'inden SMTP ile gönderir."""
    if not (gmail_kullanici and gmail_sifre):
        return "E-posta gönderilemedi (RESEND_API_KEY tanımlı değil, Gmail yedeği de yok)"
    mesaj = EmailMessage()
    mesaj["From"] = gonderen_basligi(gmail_kullanici, gonderen_adi)
    mesaj["To"] = kime
    mesaj["Subject"] = konu
    if yanit_adresi:
        mesaj["Reply-To"] = yanit_adresi
    if kopya:
        mesaj["Cc"] = kopya
    mesaj.set_content(metin, charset="utf-8")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as sunucu:
            sunucu.login(gmail_kullanici, gmail_sifre)
            sunucu.send_message(mesaj)
    except smtplib.SMTPAuthenticationError:
        return "Gmail'e giriş yapılamadı (kullanıcı adı veya uygulama şifresi hatalı)"
    except smtplib.SMTPRecipientsRefused:
        return "Alıcı adresi reddedildi"
    except (smtplib.SMTPException, OSError) as e:
        return f"E-posta gönderilemedi ({e.__class__.__name__})"
    except Exception as e:
        return f"E-posta gönderilemedi: beklenmeyen hata ({e.__class__.__name__})"
    return None


def eposta_gonder(kime: str, konu: str, metin: str, yanit_adresi: str | None = None,
                  gmail_kullanici: str = "", gmail_sifre: str = "",
                  istemci: httpx.Client | None = None, kopya: str | None = None,
                  gonderen_adi: str | None = None) -> str | None:
    """Başarıda None, hatada kısa Türkçe neden döner; yükseltmez. kopya: tek cc adresi.
    gonderen_adi: From'da görünen ad ("<ad> - Günlük Rapor"); adres değişmez.
    RESEND_API_KEY varsa HTTPS ile Resend, yoksa Gmail SMTP yedeği (yerel geliştirme)."""
    anahtar = (os.environ.get("RESEND_API_KEY") or "").strip()
    if anahtar:
        return _resend_gonder(kime, konu, metin, yanit_adresi, anahtar, istemci, kopya, gonderen_adi)
    return _smtp_gonder(gmail_kullanici, gmail_sifre, kime, konu, metin, yanit_adresi, kopya, gonderen_adi)


def kalin_isaretsiz(metin: str) -> str:
    """WhatsApp metnini düz e-postaya çevirir: '*Başlık:*' → 'Başlık:'. Başlıklar olduğu gibi kalır."""
    return re.sub(r"(?<![\w*])\*(?=\S)([^*\n]*?\S)\*(?![\w*])", r"\1", metin)


def saatte_eki(saat: str) -> str:
    """'18:30' → "18:30'da", '09:12' → "09:12'de", '17:00' → "17:00'de" (arayüzdeki saatteEki ile aynı kural)."""
    s, d = (int(x) for x in saat.split(":"))
    n = d or s
    ek = ("", "de", "de", "te", "te", "te", "da", "de", "de", "da")[n % 10] if n % 10 else ("da", "da", "de", "da", "ta", "de")[n // 10]
    return f"{saat}'{ek}"


# ---------------------------------------------------------------- davet / şifre sıfırlama e-postası

DAVET_KONU = "Günlük rapor aracına davet"
SIFIRLAMA_KONU = "Günlük rapor — şifren sıfırlandı"


def davet_eposta_metni(ad: str, eposta: str, gecici_sifre: str, giris_baglantisi: str,
                       yonetici_adi: str, sifirlama: bool = False) -> tuple[str, str]:
    """(konu, düz metin gövde). Davet ve şifre sıfırlama aynı yapıyı paylaşır."""
    acilis = ("Günlük rapor aracındaki şifren sıfırlandı; aşağıda yeni geçici şifren var."
              if sifirlama else f"{yonetici_adi} seni Günlük rapor aracına davet etti.")
    satirlar = [
        f"Merhaba {ad},",
        "",
        acilis,
        "",
        "Bu araç, günün sonunda patrona atılacak raporu hazırlar.",
        "E-postaların ve işlerin gün boyunca kendiliğinden listeye düşer.",
        "",
        f"Giriş: {giris_baglantisi}",
        f"E-posta: {eposta}",
        f"Geçici şifre: {gecici_sifre}",
        "",
        "İlk girişte yeni şifre belirleyeceksin.",
        "İlk açılışta 4 adımlık kurulum seni karşılar.",
        "",
        f"Bu e-postayı yanıtlarsan {yonelme_eki(yonetici_adi)} ulaşır.",
    ]
    return (SIFIRLAMA_KONU if sifirlama else DAVET_KONU), "\n".join(satirlar)


def _b64url(veri: bytes) -> str:
    return base64.urlsafe_b64encode(veri).rstrip(b"=").decode()


def vapid_cifti_uret() -> tuple[str, str]:
    """(public, private): public 65 baytlık sıkıştırmasız P-256 noktası, private 32 bayt ham; ikisi base64url."""
    anahtar = ec.generate_private_key(ec.SECP256R1())
    public = anahtar.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return _b64url(public), _b64url(anahtar.private_numbers().private_value.to_bytes(32, "big"))


_gecici_vapid: tuple[str, str] | None = None


def vapid_anahtarlari() -> tuple[str, str]:
    """Env'deki çift; yoksa süreç ömrü boyunca geçici çift (abonelikler yeniden başlatmada geçersizleşir)."""
    global _gecici_vapid
    public = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()
    private = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
    if public and private:
        return public, private
    if _gecici_vapid is None:
        log.warning("VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY tanımlı değil; geçici anahtar çifti kullanılıyor.")
        _gecici_vapid = vapid_cifti_uret()
    return _gecici_vapid


def push_gonder(abonelik: dict, veri: dict) -> tuple[int | None, str]:
    """abonelik: {endpoint, keys:{p256dh, auth}}. Başarıda (None, ""), hatada (HTTP durum kodu ya da 0, mesaj)."""
    _, private = vapid_anahtarlari()
    claim = (os.environ.get("VAPID_CLAIM_EMAIL") or "").strip() or "mailto:admin@example.com"
    try:
        webpush(
            subscription_info=abonelik, data=json.dumps(veri, ensure_ascii=False),
            vapid_private_key=private,
            vapid_claims={"sub": claim},  # her çağrıda yeni sözlük: pywebpush 'aud'u içine yazar
            ttl=12 * 60 * 60, timeout=10,
        )
    except WebPushException as e:
        kod = getattr(e.response, "status_code", None) or 0
        return kod, f"Push servisi {kod or 'yanıt vermedi'}"
    except Exception as e:
        return 0, f"Push gönderilemedi ({e.__class__.__name__})"
    return None, ""
