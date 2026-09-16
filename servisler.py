"""Günlük rapor için veri kaynakları: Gmail (gönderilenler), GitHub (MEDUSA commit'leri), Claude (iş diline çeviri)."""
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
CLAUDE_SISTEM = (
    "Bir müzik edisyon şirketinde çalışan bir danışmanın günlük raporu için maddeler yazıyorsun. "
    "Teknik terimleri (trigram, indeks, rollup, N+1, commit, endpoint vb.) yöneticinin anlayacağı iş diline çevir; "
    "her girdi için TEK cümle, geçmiş zaman, abartı yok, uydurma yok; ürün adı her zaman MEDUSA. "
    "Yalnız JSON dizi döndür: [{\"id\":..., \"metin\":...}]"
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


def kurum_adi(gorunen_ad: str, adres: str) -> str:
    alan = adres.rpartition("@")[2].lower()
    for anahtar, kurum in KURUMLAR.items():
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


def _alicilar(ham: str | None, kendi_adres: str) -> list[str]:
    kurumlar = []
    for ad, adres in getaddresses([ham or ""]):
        adres = adres.strip()
        if not adres or adres.lower() == kendi_adres.lower() or NOREPLY.search(adres):
            continue
        kurum = kurum_adi(basligi_coz(ad), adres)
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


def epostalari_maddele(mailler: list[dict], kendi_adres: str, bugun: date) -> list[dict]:
    """mailler: {"date","subject","from","to","cc"} ham başlık değerleri."""
    gruplar: dict[tuple[str, ...], list[str]] = {}
    for m in mailler:
        if not bugun_mu(m.get("date"), bugun):
            continue
        if NOREPLY.search(m.get("from") or ""):
            continue
        kurumlar = _alicilar(m.get("to"), kendi_adres) or _alicilar(m.get("cc"), kendi_adres)
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


def gmail_tara(kullanici: str, sifre: str, bugun: date) -> list[dict]:
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    except OSError as e:
        raise KaynakHatasi(f"Gmail'e bağlanılamadı: {e}") from e
    try:
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
        return epostalari_maddele(mailler, kullanici, bugun)
    except KaynakHatasi:
        raise
    except (imaplib.IMAP4.error, OSError) as e:
        raise KaynakHatasi(f"Gmail okunurken hata oluştu: {e}") from e
    finally:
        try:
            M.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- GitHub

def github_tara(token: str, repo: str, bugun: date, istemci: httpx.Client | None = None) -> list[dict]:
    baslangic = datetime.combine(bugun, time.min, ISTANBUL).astimezone(timezone.utc)
    istemci = istemci or httpx.Client(timeout=20)
    url = f"https://api.github.com/repos/{repo}/commits"
    params = {"since": baslangic.strftime("%Y-%m-%dT%H:%M:%SZ"), "per_page": 100}
    basliklar = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    commitler = []
    try:
        for sayfa in range(1, 6):
            yanit = istemci.get(url, params={**params, "page": sayfa}, headers=basliklar)
            if yanit.status_code == 401:
                raise KaynakHatasi("GitHub token'ı geçersiz veya süresi dolmuş")
            if yanit.status_code == 404:
                raise KaynakHatasi(f"GitHub reposu bulunamadı ya da token'ın erişimi yok: {repo}")
            if yanit.status_code >= 400:
                raise KaynakHatasi(f"GitHub hata döndürdü ({yanit.status_code}): {yanit.text[:200]}")
            parti = yanit.json()
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


def claude_cevir(maddeler: list[dict], api_anahtari: str, istemci: httpx.Client | None = None) -> tuple[list[dict], str | None]:
    """Maddeleri tek çağrıda iş diline çevirir. Hata olursa ham maddeler + hata mesajı döner."""
    if not maddeler:
        return maddeler, None
    girdiler = [{"id": m["id"], "kaynak": m["kaynak"], "metin": m["metin"]} for m in maddeler]
    istek = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1500,
        "thinking": {"type": "disabled"},
        "system": CLAUDE_SISTEM,
        "messages": [{
            "role": "user",
            "content": (
                "Kaynağı 'medusa' olanlar MEDUSA yazılımında bugün yapılan değişikliklerin commit mesajları, "
                "'eposta' olanlar bugün gönderilen e-postaların özetleri. Her birini çevir:\n"
                + json.dumps(girdiler, ensure_ascii=False)
            ),
        }],
    }
    istemci = istemci or httpx.Client(timeout=60)
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
            return maddeler, f"claude: API hatası ({yanit.status_code}: {neden}), ham metin kullanıldı"
        govde = yanit.json()
        if govde.get("stop_reason") == "refusal":
            return maddeler, "claude: istek reddedildi, ham metin kullanıldı"
        metin = "".join(b.get("text", "") for b in govde.get("content", []) if b.get("type") == "text")
        ceviri = {
            str(x["id"]): x["metin"].strip()
            for x in _json_dizi_ayikla(metin)
            if isinstance(x, dict) and isinstance(x.get("metin"), str) and x["metin"].strip() and "id" in x
        }
    except httpx.HTTPError as e:
        return maddeler, f"claude: bağlanılamadı ({e.__class__.__name__}), ham metin kullanıldı"
    except (ValueError, KeyError, TypeError) as e:
        return maddeler, f"claude: yanıt JSON değil ({e}), ham metin kullanıldı"
    # id ham metinden türetildiği için korunur; böylece gün içinde tik durumu kaybolmaz.
    return [{**m, "metin": ceviri.get(m["id"], m["metin"])} for m in maddeler], None


# ---------------------------------------------------------------- birleştirme

def raporu_uret(ortam: dict) -> dict:
    bugun = istanbul_bugun()
    sonuc = {"tarih": bugun.isoformat(), "eposta": [], "medusa": [], "hatalar": []}

    if ortam.get("GMAIL_KULLANICI") and ortam.get("GMAIL_UYGULAMA_SIFRESI"):
        try:
            sonuc["eposta"] = gmail_tara(ortam["GMAIL_KULLANICI"], ortam["GMAIL_UYGULAMA_SIFRESI"], bugun)
        except KaynakHatasi as e:
            sonuc["hatalar"].append({"kaynak": "gmail", "mesaj": str(e)})
        except Exception as e:
            sonuc["hatalar"].append({"kaynak": "gmail", "mesaj": f"Gmail taranamadı: {e.__class__.__name__}"})
    else:
        sonuc["hatalar"].append({"kaynak": "gmail", "mesaj": "Gmail ayarları eksik (GMAIL_KULLANICI, GMAIL_UYGULAMA_SIFRESI)"})

    if ortam.get("GITHUB_TOKEN") and ortam.get("GITHUB_REPO"):
        try:
            sonuc["medusa"] = github_tara(ortam["GITHUB_TOKEN"], ortam["GITHUB_REPO"], bugun)
        except KaynakHatasi as e:
            sonuc["hatalar"].append({"kaynak": "github", "mesaj": str(e)})
        except Exception as e:
            sonuc["hatalar"].append({"kaynak": "github", "mesaj": f"GitHub taranamadı: {e.__class__.__name__}"})
    else:
        sonuc["hatalar"].append({"kaynak": "github", "mesaj": "GitHub ayarları eksik (GITHUB_TOKEN, GITHUB_REPO)"})

    if ortam.get("ANTHROPIC_API_KEY"):
        tumu, hata = claude_cevir(sonuc["eposta"] + sonuc["medusa"], ortam["ANTHROPIC_API_KEY"])
        sonuc["eposta"] = [m for m in tumu if m["kaynak"] == "eposta"]
        sonuc["medusa"] = [m for m in tumu if m["kaynak"] == "medusa"]
        if hata:
            sonuc["hatalar"].append({"kaynak": "claude", "mesaj": hata})
    return sonuc
