"""Günlük rapor için veri kaynakları: Gmail (gönderilenler), GitHub (proje commit'leri), Claude (iş diline çeviri)."""
from __future__ import annotations

import base64
import email
import hashlib
import imaplib
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from zoneinfo import ZoneInfo

import httpx

ISTANBUL = ZoneInfo("Europe/Istanbul")

KURUMLAR = {
    "mesam.org.tr": "MESAM",
    "msg.org.tr": "MSG",
    "imro.ie": "IMRO",
    "coverz": "Coverz",
    "ilsvision.com": "şirket içi",
}
SIRKET_ICI = "şirket içi"

CLAUDE_MODEL = "claude-sonnet-5"


def claude_sistem(proje_adi: str = "") -> str:
    urun = f"ürün adı her zaman {proje_adi}. " if proje_adi else ""
    return (
        "Bir müzik edisyon şirketinde çalışan bir danışmanın günlük raporu için maddeler yazıyorsun. "
        "Teknik terimleri (trigram, indeks, rollup, N+1, commit, endpoint vb.) yöneticinin anlayacağı iş diline çevir; "
        "her girdi için TEK cümle, geçmiş zaman, abartı yok, uydurma yok. "
        + urun
        + "Yalnız JSON dizi döndür: [{\"id\":..., \"metin\":...}]"
    )


def istanbul_bugun() -> date:
    return datetime.now(ISTANBUL).date()


def madde_id(kaynak: str, metin: str) -> str:
    return hashlib.sha1((kaynak + metin).encode("utf-8")).hexdigest()[:10]


def madde(kaynak: str, metin: str) -> dict:
    return {"id": madde_id(kaynak, metin), "metin": metin, "kaynak": kaynak}


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


def yonelme_eki(ad: str) -> str:
    """'MSG' → "MSG'ye", 'MESAM' → "MESAM'a", 'IMRO' → "IMRO'ya", 'Coverz' → "Coverz'e"."""
    ad = ad.strip()
    kelimeler = [k for k in re.split(r"[\s.\-_/]+", ad) if k]
    son = kelimeler[-1] if kelimeler else ad
    harfler = "".join(h for h in son if h.isalpha())

    if _kucult(harfler) in EK_ISTISNALARI:
        unlu = EK_ISTISNALARI[_kucult(harfler)]
        return f"{ad}'{unlu}"

    if harfler and harfler.isupper() and not any(h in UNLULER for h in _kucult(harfler)):
        okunus = HARF_OKUNUSU.get(harfler[-1], _kucult(harfler[-1]) + "e")
    else:
        okunus = _kucult(harfler)

    son_unlu = next((h for h in reversed(okunus) if h in UNLULER), "e")
    unlu = "a" if son_unlu in KALIN_UNLULER else "e"
    kaynastirma = "y" if okunus and okunus[-1] in UNLULER else ""
    return f"{ad}'{kaynastirma}{unlu}"


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
    alan = adres.rpartition("@")[2].lower()
    for anahtar, kurum in (KURUMLAR if sozluk is None else sozluk).items():
        if _alan_eslesir(alan, anahtar):
            return kurum
    return gorunen_ad.strip().strip('"') or alan or adres


def bugun_mu(date_basligi: str | None, bugun: date) -> bool:
    if not date_basligi:
        return False
    try:
        zaman = parsedate_to_datetime(date_basligi)
    except (TypeError, ValueError):
        return False
    if zaman.tzinfo is None:
        zaman = zaman.replace(tzinfo=timezone.utc)
    return zaman.astimezone(ISTANBUL).date() == bugun


def _alicilar(ham: str | None, kendi_adres: str, sozluk: dict[str, str] | None = None) -> list[str]:
    kurumlar = []
    for ad, adres in getaddresses([ham or ""]):
        adres = adres.strip()
        if not adres or adres.lower() == kendi_adres.lower() or NOREPLY.search(adres):
            continue
        kurum = kurum_adi(basligi_coz(ad), adres, sozluk)
        if kurum not in kurumlar:
            kurumlar.append(kurum)
    return kurumlar


def _hedef(kurumlar: tuple[str, ...]) -> str:
    if kurumlar == (SIRKET_ICI,):
        return "Şirket içi"
    adlar = list(kurumlar)
    if len(adlar) == 1:
        return yonelme_eki(adlar[0])
    return ", ".join(adlar[:-1]) + " ve " + yonelme_eki(adlar[-1])


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


def epostalari_maddele(mailler: list[dict], kendi_adres: str, bugun: date, sozluk: dict[str, str] | None = None) -> list[dict]:
    """mailler: {"date","subject","from","to","cc"} ham başlık değerleri."""
    gruplar: dict[tuple[str, ...], list[str]] = {}
    for m in mailler:
        if not bugun_mu(m.get("date"), bugun):
            continue
        if NOREPLY.search(m.get("from") or ""):
            continue
        kurumlar = _alicilar(m.get("to"), kendi_adres, sozluk) or _alicilar(m.get("cc"), kendi_adres, sozluk)
        if len(kurumlar) > 1 and SIRKET_ICI in kurumlar:
            kurumlar.remove(SIRKET_ICI)
        if not kurumlar:
            continue  # kendine gönderilen veya yalnız noreply adreslerine giden
        anahtar = tuple(kurumlar)
        gruplar.setdefault(anahtar, []).append(konu_temizle(basligi_coz(m.get("subject"))))
    return tekille([madde("eposta", eposta_metni(k, konular)) for k, konular in gruplar.items()])


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


def gmail_tara(kullanici: str, sifre: str, bugun: date, sozluk: dict[str, str] | None = None) -> list[dict]:
    def oku(M: imaplib.IMAP4_SSL) -> list[dict]:
        dun = bugun - timedelta(days=1)
        _, veri = M.search(None, "SINCE", f"{dun.day:02d}-{AYLAR[dun.month - 1]}-{dun.year}")
        kimlikler = veri[0].split() if veri and veri[0] else []
        mailler = []
        if kimlikler:
            _, parcalar = M.fetch(b",".join(kimlikler).decode(), "(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT FROM TO CC)])")
            for parca in parcalar:
                if not isinstance(parca, tuple):
                    continue
                msg = email.message_from_bytes(parca[1])
                mailler.append({k: msg.get(k) for k in ("date", "subject", "from", "to", "cc")})
        return epostalari_maddele(mailler, kullanici, bugun, sozluk)

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
    baslangic = datetime.combine(bugun, time.min, ISTANBUL).astimezone(timezone.utc)
    istemci = istemci or httpx.Client(timeout=20)
    params = {"since": baslangic.strftime("%Y-%m-%dT%H:%M:%SZ"), "per_page": 100}
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
        maddeler.append(madde("medusa", ilk_satir))
    return tekille(maddeler)


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
    if govde.get("stop_reason") == "refusal":
        raise ClaudeHatasi("istek reddedildi")
    return "".join(b.get("text", "") for b in govde.get("content", []) if b.get("type") == "text")


def _id_metin_eslesmesi(metin: str) -> dict[str, str]:
    return {
        str(x["id"]): x["metin"].strip()
        for x in _json_dizi_ayikla(metin)
        if isinstance(x, dict) and isinstance(x.get("metin"), str) and x["metin"].strip() and "id" in x
    }


def claude_cevir(
    maddeler: list[dict], api_anahtari: str, istemci: httpx.Client | None = None, proje_adi: str = ""
) -> tuple[list[dict], str | None]:
    """Maddeleri tek çağrıda iş diline çevirir. Hata olursa ham maddeler + hata mesajı döner."""
    if not maddeler:
        return maddeler, None
    girdiler = [{"id": m["id"], "kaynak": m["kaynak"], "metin": m["metin"]} for m in maddeler]
    istek = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1500,
        "thinking": {"type": "disabled"},
        "system": claude_sistem(proje_adi),
        "messages": [{
            "role": "user",
            "content": (
                f"Kaynağı 'medusa' olanlar {proje_adi + ' yazılımında' if proje_adi else 'yazılım projesinde'} "
                "bugün yapılan değişikliklerin commit mesajları, "
                "'eposta' olanlar bugün gönderilen e-postaların özetleri. Her birini çevir:\n"
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
    # id ham metinden türetildiği için korunur; böylece gün içinde tik durumu kaybolmaz.
    return [{**m, "metin": ceviri.get(m["id"], m["metin"])} for m in maddeler], None


def duzelt_sistemi(proje_adi: str = "") -> str:
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
        "- 'devam' türündeki maddelerde işin adı ve aşaması tek cümlede birleşir (örn. \"… için yanıt bekleniyor.\").\n"
        "- 'surekli' türündeki maddeler her gün tekrarlanan işlerdir: son raporlardaki cümlelerle aynı olmayan "
        "ama aynı anlama gelen, doğal bir ifade yaz. Metinde | ile ayrılmış seçenekler varsa hepsi aynı işin "
        "farklı söylenişidir.\n"
        "Yanıt olarak YALNIZ JSON dizi döndür, başka hiçbir şey yazma: [{\"id\": <sayı>, \"metin\": \"...\"}]"
    )


def claude_duzelt(
    girdiler: list[dict], son_raporlar: list[str], api_anahtari: str, proje_adi: str = "",
    istemci: httpx.Client | None = None,
) -> dict[int, str]:
    """girdiler: {id, tur, metin, asama?}. Dönen: id → düzeltilmiş metin. Hata → ClaudeHatasi."""
    if not girdiler:
        return {}
    baglam = (
        "Son günlük raporlar (sürekli işlerde bu cümleleri tekrar etme):\n"
        + "\n---\n".join(son_raporlar) + "\n\n"
    ) if son_raporlar else ""
    istek = {
        "model": CLAUDE_MODEL,
        "max_tokens": 2000,
        "thinking": {"type": "disabled"},
        "system": duzelt_sistemi(proje_adi),
        "messages": [{
            "role": "user",
            "content": baglam + "Düzeltilecek maddeler:\n" + json.dumps(girdiler, ensure_ascii=False),
        }],
    }
    metin = _claude_cagir(istek, api_anahtari, istemci)
    try:
        eslesme = _id_metin_eslesmesi(metin)
    except (ValueError, KeyError, TypeError) as e:
        raise ClaudeHatasi(f"yanıt JSON değil ({e})") from e
    gecerli = {str(g["id"]) for g in girdiler}
    return {int(k): v for k, v in eslesme.items() if k in gecerli}


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
        "- Raporlarda olmayan hiçbir bilgiyi ekleme; isimler ve sayılar aynen kalır; geçmiş zaman.\n"
        "Yalnız özet metnini döndür, açıklama yazma."
    )


def claude_haftalik(
    raporlar: list[tuple[date, str]], baslik: str, api_anahtari: str, istemci: httpx.Client | None = None
) -> str:
    govde = "\n\n".join(f"### {t.isoformat()}\n{m}" for t, m in raporlar)
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
    return metin


def surekli_varyant(metin: str, tarih: date) -> str:
    """'a | b | c' → yılın gününe göre biri (arayüzdeki eski variantOf ile aynı)."""
    parcalar = [p.strip() for p in metin.split("|") if p.strip()]
    if len(parcalar) <= 1:
        return metin.strip()
    return parcalar[tarih.timetuple().tm_yday % len(parcalar)]


# ---------------------------------------------------------------- birleştirme

def raporu_uret(ayarlar: dict, api_anahtari: str = "", haric_idler: set[str] | frozenset = frozenset()) -> dict:
    """ayarlar: gmail_kullanici, gmail_sifre, github_token, github_repo, proje_adi, alan_sozlugu (çözülmüş).
    haric_idler: zaten kayıtlı maddeler; Claude'a yeniden gönderilmez."""
    bugun = istanbul_bugun()
    sonuc = {"tarih": bugun.isoformat(), "eposta": [], "medusa": [], "hatalar": []}

    if ayarlar.get("gmail_kullanici") and ayarlar.get("gmail_sifre"):
        try:
            sonuc["eposta"] = gmail_tara(ayarlar["gmail_kullanici"], ayarlar["gmail_sifre"], bugun, ayarlar.get("alan_sozlugu"))
        except KaynakHatasi as e:
            sonuc["hatalar"].append({"kaynak": "gmail", "mesaj": str(e)})
        except Exception as e:
            sonuc["hatalar"].append({"kaynak": "gmail", "mesaj": f"Gmail taranamadı: {e.__class__.__name__}"})
    else:
        sonuc["hatalar"].append({"kaynak": "gmail", "mesaj": "Gmail ayarı girilmemiş (Ayarlar)"})

    if ayarlar.get("github_token") and ayarlar.get("github_repo"):
        try:
            sonuc["medusa"] = github_tara(ayarlar["github_token"], ayarlar["github_repo"], bugun)
        except KaynakHatasi as e:
            sonuc["hatalar"].append({"kaynak": "github", "mesaj": str(e)})
        except Exception as e:
            sonuc["hatalar"].append({"kaynak": "github", "mesaj": f"GitHub taranamadı: {e.__class__.__name__}"})
    else:
        sonuc["hatalar"].append({"kaynak": "github", "mesaj": "GitHub ayarı girilmemiş (Ayarlar)"})

    yeniler = [m for m in sonuc["eposta"] + sonuc["medusa"] if m["id"] not in haric_idler]
    if api_anahtari and yeniler:
        cevrilmis, hata = claude_cevir(yeniler, api_anahtari, proje_adi=ayarlar.get("proje_adi") or "")
        if not hata:  # metin ham kalır; çeviri metin_ai'ye gider
            metinler = {m["id"]: m["metin"] for m in cevrilmis}
            for anahtar in ("eposta", "medusa"):
                sonuc[anahtar] = [{**m, "metin_ai": metinler[m["id"]]} if m["id"] in metinler else m for m in sonuc[anahtar]]
        else:
            sonuc["hatalar"].append({"kaynak": "claude", "mesaj": hata})
    return sonuc
