"""JSON uçları. Kullanıcı her zaman oturumdan gelir; tüm sorgular user_id ile süzülür."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import random
import re
import threading
import time as saat_
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import guvenlik
import servisler
from kimlik import aktif_kullanici
from veritabani import (
    ClaudeKullanim, GunlukIfade, HatirlatmaGonderimi, Kategori, Kullanici, KullaniciAyari, Madde, PushAbonelik, Rapor,
    RaporDuzeni, oturum, simdi,
)

router = APIRouter(prefix="/api")
log = logging.getLogger("gunluk-rapor")

REPO_BICIMI = re.compile(r"^[\w.-]+/[\w.-]+$")
EPOSTA_BICIMI = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SAAT_BICIMI = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
ALAN_BICIMI = re.compile(r"^[\w-]+(\.[\w-]+)+$")
VARSAYILAN_SAAT = time(17, 0)
VARSAYILAN_GUNLER = "1,2,3,4,5"
KAYNAKLAR = ("gmail", "github", "medusa")
YAKINDA = {"medusa"}  # arayüzde "yakında"; açılamaz
# Bulunan maddenin kaynak alanı → hangi kaynak modülünden geldiği ('medusa' tarihsel olarak GitHub commit'leridir).
BULUNAN_KAYNAGI = {"eposta": "gmail", "medusa": "github"}
GUNLUK_CLAUDE_SINIRI = 8
KOTA_MESAJI = "Bugünkü düzeltme hakkı doldu, yarın devam"
GECMIS_GUN = 30  # geçmiş gün düzenleme: bugün … 30 gün önce
RAPOR_BICIMLERI = ("kategorili", "duz")
# Rapora elle yazılmış gibi giren bugün satırları: elle, e-postayla gelen not, sesle eklenen.
ELLE_KAYNAKLARI = ("elle", "not", "ses")
SESLI_NOT_SINIRI = 5000  # karakter


def bugun() -> date:
    return servisler.istanbul_bugun()


def gun_sec(tarih: str | date | None) -> date:
    """Gün kapsamlı uçların ?tarih= değeri; boşsa bugün (Istanbul). Aralık dışı ya da okunamayan tarih 400."""
    bugun_ = bugun()
    if tarih is None or (isinstance(tarih, str) and not tarih.strip()):
        return bugun_
    if isinstance(tarih, str):
        try:
            tarih = date.fromisoformat(tarih.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="Tarih YYYY-AA-GG biçiminde olmalı") from None
    if not bugun_ - timedelta(days=GECMIS_GUN) <= tarih <= bugun_:
        raise HTTPException(status_code=400, detail=f"Yalnız bugün ve son {GECMIS_GUN} gün düzenlenebilir")
    return tarih


def ai_anahtari() -> str:
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


# ---------------------------------------------------------------- yardımcılar

def ayar_satiri(db: Session, kullanici: Kullanici) -> KullaniciAyari:
    return db.get(KullaniciAyari, kullanici.id) or KullaniciAyari(user_id=kullanici.id)


def gunleri_ayristir(deger: str | list) -> list[int]:
    parcalar = deger.split(",") if isinstance(deger, str) else deger
    gunler = set()
    for g in parcalar:
        if isinstance(g, str):
            g = g.strip()
            if not g:
                continue
            if not g.isdigit():
                raise HTTPException(status_code=422, detail="Hatırlatma günleri 1–7 arası olmalı")
            g = int(g)
        if isinstance(g, bool) or not isinstance(g, int) or not 1 <= g <= 7:
            raise HTTPException(status_code=422, detail="Hatırlatma günleri 1–7 arası olmalı")
        gunler.add(g)
    return sorted(gunler)


def hatirlatma_ayari(a: KullaniciAyari) -> dict:
    """Satır yoksa ya da kolon boşsa varsayılanlar."""
    return {
        "saat": a.hatirlatma_saat or VARSAYILAN_SAAT,
        "gunler": gunleri_ayristir(VARSAYILAN_GUNLER if a.hatirlatma_gunler is None else a.hatirlatma_gunler),
        "push": a.hatirlatma_push is not False,
        "eposta": a.hatirlatma_eposta is not False,
        "adres": a.hatirlatma_eposta_adres or "",
    }


def kaynak_durumu(a: KullaniciAyari) -> dict[str, bool]:
    """Kaydedilmiş seçim; kaynak için seçim yoksa şifresi/token'ı kayıtlıysa açık sayılır."""
    secim = a.kaynaklar if isinstance(a.kaynaklar, dict) else {}
    varsayilan = {"gmail": bool(a.gmail_sifre_enc), "github": bool(a.github_token_enc)}
    return {k: k not in YAKINDA and bool(secim.get(k, varsayilan.get(k, False))) for k in KAYNAKLAR}


def acik_bulunan_kaynaklari(a: KullaniciAyari) -> set[str]:
    acik = kaynak_durumu(a)
    return {kaynak for kaynak, modul in BULUNAN_KAYNAGI.items() if acik[modul]}


def ayar_ozeti(a: KullaniciAyari, kullanici: Kullanici | None = None) -> dict:
    """Şifre ve token hiçbir zaman dönmez; yalnız kayıtlı olup olmadıkları."""
    h = hatirlatma_ayari(a)
    return {
        "hatirlatma_saat": h["saat"].strftime("%H:%M"),
        "hatirlatma_gunler": h["gunler"],
        "hatirlatma_push": h["push"],
        "hatirlatma_eposta": h["eposta"],
        "hatirlatma_eposta_adres": h["adres"],
        "giris_eposta": kullanici.eposta if kullanici else "",
        "eposta_gonderen": servisler.gonderen_adresi(),
        "gmail_kullanici": a.gmail_kullanici or "",
        "gmail_sifre_kayitli": bool(a.gmail_sifre_enc),
        "github_token_kayitli": bool(a.github_token_enc),
        "github_repo": a.github_repo or "",
        "proje_adi": a.proje_adi or "",
        "patron_telefon": a.patron_telefon or "",
        "rapor_basligi": a.rapor_basligi or "",
        "alan_sozlugu": alan_sozlugu(a),
        "kaynaklar": kaynak_durumu(a),
        "kurulum_tamam": bool(a.kurulum_tamam),
        "eposta_gruplama": eposta_gruplama(a),
        "rapor_bicimi": rapor_bicimi(a),
        "karistir": a.karistir is not False,
        "kendi_alanlar_otomatik": otomatik_kendi_alanlar(a, kullanici),
        "kendi_alanlar": list(a.kendi_alanlar or []),
        "ekip_ici_atla": a.ekip_ici_atla is not False,
    }


def alan_sozlugu(a: KullaniciAyari) -> dict[str, str]:
    return servisler.KURUMLAR if a.alan_sozlugu is None else a.alan_sozlugu


def otomatik_kendi_alanlar(a: KullaniciAyari, kullanici: Kullanici | None) -> list[str]:
    """Giriş ve Gmail adresinin alan adları + sözlükte 'şirket içi' eşlenenler (Ayarlar'da gri rozet)."""
    adresler = [kullanici.eposta if kullanici else "", a.gmail_kullanici or ""]
    return servisler.kendi_alanlari(adresler, sozluk=alan_sozlugu(a))


def kendi_alanlar(a: KullaniciAyari, kullanici: Kullanici | None) -> list[str]:
    return list(dict.fromkeys(otomatik_kendi_alanlar(a, kullanici) + list(a.kendi_alanlar or [])))


def eposta_gruplama(a: KullaniciAyari) -> str:
    return a.eposta_gruplama if a.eposta_gruplama in servisler.GRUPLAMALAR else "konu"


def cozulmus_ayarlar(a: KullaniciAyari, kullanici: Kullanici | None = None) -> dict:
    kendi = kendi_alanlar(a, kullanici)
    return {
        "kendi_alanlar": kendi,
        "kendi_sirket": servisler.kendi_sirket_adlari(kendi, alan_sozlugu(a)),
        "ekip_ici_atla": a.ekip_ici_atla is not False,
        "eposta_gruplama": eposta_gruplama(a),
        "kaynaklar": kaynak_durumu(a),
        "gmail_kullanici": a.gmail_kullanici or "",
        "gmail_sifre": guvenlik.coz(a.gmail_sifre_enc),
        "github_token": guvenlik.coz(a.github_token_enc),
        "github_repo": a.github_repo or "",
        "proje_adi": a.proje_adi or "",
        "alan_sozlugu": alan_sozlugu(a),
    }


def kullanici_maddesi(db: Session, kullanici: Kullanici, madde_id: int) -> Madde:
    madde = db.get(Madde, madde_id)
    if madde is None or madde.user_id != kullanici.id:
        raise HTTPException(status_code=404, detail="Madde bulunamadı")
    return madde


def sonraki_sira(db: Session, kullanici: Kullanici, tur: str, tarih: date | None = None) -> int:
    sorgu = select(func.max(Madde.sira)).where(Madde.user_id == kullanici.id, Madde.tur == tur)
    if tarih is not None:
        sorgu = sorgu.where(Madde.tarih == tarih)
    return (db.scalar(sorgu) or 0) + 1


_kilitler: dict[tuple[int, str], threading.Lock] = {}
_kilitler_kilidi = threading.Lock()


def _kullanici_kilidi(user_id: int, is_: str = "tarama") -> threading.Lock:
    with _kilitler_kilidi:
        return _kilitler.setdefault((user_id, is_), threading.Lock())


def satirlara_bol(metin: str) -> list[str]:
    satirlar = (re.sub(r"^[-•*]\s*", "", x.strip()) for x in (metin or "").splitlines())
    return [x for x in satirlar if x]


def bugunku_ifadeler(db: Session, user_id: int, tarih: date, item_idler=None) -> dict[int, str]:
    sorgu = select(GunlukIfade).where(GunlukIfade.user_id == user_id, GunlukIfade.tarih == tarih)
    if item_idler is not None:
        sorgu = sorgu.where(GunlukIfade.item_id.in_(list(item_idler)))
    return {i.item_id: i.metin_ai for i in db.scalars(sorgu)}


def madde_rapor_metni(m: Madde, ifade: str | None, tarih: date) -> str:
    """Rapora giren metin: kullanıcının düzenlediği > Claude'un düzelttiği > ham. Sürekli işte günün ifadesi > '|' varyantı."""
    ai_acik = m.ai_kullan is not False
    if m.tur == "surekli":
        return ifade if (ifade and ai_acik) else servisler.surekli_varyant(m.metin, tarih)
    ham = m.metin + (f" — {m.asama}" if m.tur == "devam" and m.asama else "")
    if m.kullanici_duzenledi:
        return ham
    if ai_acik and m.metin_ai:
        return m.metin_ai
    return ham


def madde_json(m: Madde, ifadeler: dict[int, str], tarih: date) -> dict:
    ifade = ifadeler.get(m.id) if m.tur == "surekli" else None
    veri = {
        **m.sozluk(), "gunun_ifadesi": ifade, "rapor_metni": madde_rapor_metni(m, ifade, tarih),
        "olusturma": zaman_iso(m.olusturma), "kaynak_zaman": zaman_iso(m.kaynak_zaman),
    }
    if m.tur == "devam":  # yalnız ekranda gösterilir; rapor metnine girmez
        veri["bekleme_gun"] = bekleme_gunu(m.olusturma, tarih)
    return veri


def utc(z: datetime) -> datetime:
    """sqlite saat dilimini saklamaz; naive değerler UTC kabul edilir."""
    return z.astimezone(timezone.utc) if z.tzinfo else z.replace(tzinfo=timezone.utc)


def zaman_iso(z: datetime | None) -> str | None:
    return None if z is None else utc(z).isoformat()


def bekleme_gunu(olusturma: datetime | None, tarih: date) -> int:
    """Devam eden işin kaç gündür beklediği: bugün − eklendiği gün (Istanbul takvimiyle)."""
    if olusturma is None:
        return 0
    return max(0, (tarih - utc(olusturma).astimezone(servisler.ISTANBUL).date()).days)


def yapilanlari_bol(db: Session, kullanici: Kullanici, tarih: date) -> None:
    """R1'in tek parça 'yapılanlar' metni bir kez satır satır 'elle' maddelerine çevrilir."""
    eskiler = db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, Madde.tur == "bugun", Madde.tarih == tarih, Madde.kaynak == "yapilanlar",
    ).order_by(Madde.id)).all()
    if not eskiler:
        return
    sira = sonraki_sira(db, kullanici, "bugun", tarih)
    for eski in eskiler:
        for satir in satirlara_bol(eski.metin):
            db.add(Madde(user_id=kullanici.id, tur="bugun", tarih=tarih, kaynak="elle", metin=satir, tikli=eski.tikli, sira=sira))
            sira += 1
        db.delete(eski)
    db.commit()


# ---------------------------------------------------------------- rapor kategorileri ve günün düzeni

def baslangic_kategorileri(a: KullaniciAyari) -> list[dict]:
    return [
        {"ad": "Yazışmalar", "kaynaklar": ["gmail"]},
        {"ad": f"{a.proje_adi or 'Uygulama'} Çalışmaları"[:80], "kaynaklar": ["github", "medusa"]},
        {"ad": "Genel İşler", "kaynaklar": []},
        {"ad": "Devam Eden İşler", "kaynaklar": [], "sistem": "devam"},
        {"ad": "Önemli Konular", "kaynaklar": [], "sistem": "onemli"},
    ]


def kategorileri_hazirla(db: Session, kullanici: Kullanici) -> list[Kategori]:
    """Kullanıcının kategorileri (sıralı). Hiç yoksa başlangıç kategorileri bir kez açılır."""
    sorgu = select(Kategori).where(Kategori.user_id == kullanici.id).order_by(Kategori.sira, Kategori.id)
    kategoriler = db.scalars(sorgu).all()
    if kategoriler:
        return list(kategoriler)
    with _kullanici_kilidi(kullanici.id, "kategori"):
        if not db.scalar(select(func.count()).select_from(Kategori).where(Kategori.user_id == kullanici.id)):
            for sira, k in enumerate(baslangic_kategorileri(ayar_satiri(db, kullanici)), start=1):
                db.add(Kategori(user_id=kullanici.id, sira=sira, **k))
            db.commit()
    return list(db.scalars(sorgu).all())


class KategoriBaglami:
    """Etkin kategori kuralı için kullanıcının kategorileri üzerinde hazır aramalar."""

    def __init__(self, kategoriler: list[Kategori]):
        self.kategoriler = kategoriler
        self.idler = {k.id for k in kategoriler}
        self.sistem = {k.sistem: k.id for k in kategoriler if k.sistem}
        self.secilebilir = {k.id for k in kategoriler if not k.sistem}
        genel = next((k for k in kategoriler if not k.sistem and k.ad.strip().lower() == "genel işler"), None)
        genel = genel or next((k for k in kategoriler if not k.sistem and not k.kaynaklar), None)
        genel = genel or next((k for k in kategoriler if not k.sistem), None) or (kategoriler[0] if kategoriler else None)
        self.genel = genel.id if genel else None

    def kaynagin_kategorisi(self, modul: str) -> int | None:
        return next((k.id for k in self.kategoriler if modul in (k.kaynaklar or [])), None)

    def dogal(self, m: Madde) -> int | None:
        """Günün düzeni hariç kural: önemli > devam > maddenin kategorisi > bulunanın kaynağı > Genel İşler."""
        if m.onemli and "onemli" in self.sistem:
            return self.sistem["onemli"]
        if m.tur == "devam" and "devam" in self.sistem:
            return self.sistem["devam"]
        if m.kategori_id in self.idler:  # elle seçilen ya da Claude'un önerdiği
            return m.kategori_id
        if m.tur == "bulunan":
            k = self.kaynagin_kategorisi(BULUNAN_KAYNAGI.get(m.kaynak, m.kaynak))
            if k is not None:
                return k
        return self.genel

    def etkin(self, m: Madde, duzen: RaporDuzeni | None) -> int | None:
        if duzen is not None and duzen.kategori_id in self.idler:
            return duzen.kategori_id
        return self.dogal(m)


def rapor_adaylari(db: Session, kullanici: Kullanici, tarih: date, a: KullaniciAyari) -> list[Madde]:
    """O günün raporuna girebilecek maddeler (tik durumundan bağımsız): sürekli, devam, elle/not/ses, açık kaynağın bulunanları."""
    acik = acik_bulunan_kaynaklari(a)
    maddeler = db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, or_(Madde.tur.in_(("surekli", "devam")), Madde.tarih == tarih),
    ).order_by(Madde.id)).all()
    return [m for m in maddeler if m.tur in ("surekli", "devam")
            or (m.tur == "bugun" and m.kaynak in ELLE_KAYNAKLARI and not m.gizli)
            or (m.tur == "bulunan" and m.kaynak in acik and not m.gizli)]


def _tohum(tarih: date, item_id: int, tuz: str = "") -> float:
    ozet = hashlib.sha256(f"{tarih.isoformat()}:{item_id}:{tuz}".encode()).digest()
    return int.from_bytes(ozet[:8], "big") / 2 ** 64


def serpistir(temel: list[Madde], surekli: list[Madde], tarih: date) -> list[Madde]:
    """Sürekli işleri tarih + madde id tohumlu yerlere dağıtır: gün içinde sabit, günden güne farklı."""
    yerler = sorted(surekli, key=lambda m: (int(_tohum(tarih, m.id, "yer") * (len(temel) + 1)), _tohum(tarih, m.id)))
    sonuc, i = [], 0
    for yer in range(len(temel) + 1):
        while i < len(yerler) and int(_tohum(tarih, yerler[i].id, "yer") * (len(temel) + 1)) == yer:
            sonuc.append(yerler[i])
            i += 1
        if yer < len(temel):
            sonuc.append(temel[yer])
    return sonuc


def kategori_ici_sira(maddeler: list[Madde], satirlar: dict[int, RaporDuzeni], tarih: date, karistir: bool) -> list[Madde]:
    """Günün düzeninde sırası olanlar önce; kalanlarda bugün/bulunan eklenme sırasıyla, sürekli işler arada ya da sonda."""
    elle = sorted((m for m in maddeler if m.id in satirlar), key=lambda m: (satirlar[m.id].sira, m.id))
    kalan = [m for m in maddeler if m.id not in satirlar]
    surekli = sorted((m for m in kalan if m.tur == "surekli"), key=lambda m: (m.sira, m.id))
    temel = sorted((m for m in kalan if m.tur != "surekli"),
                   key=lambda m: (m.tur == "devam", m.sira if m.tur == "devam" else 0, m.id))
    return elle + (serpistir(temel, surekli, tarih) if karistir else temel + surekli)


class GunDuzeni:
    """Bir günün rapor düzeni: kategori sırasıyla bölümler, maddelerin etkin kategorisi ve düz biçim sırası."""

    def __init__(self, db: Session, kullanici: Kullanici, tarih: date, a: KullaniciAyari | None = None):
        a = a or ayar_satiri(db, kullanici)
        self.tarih = tarih
        self.bicim = rapor_bicimi(a)
        self.kategoriler = kategorileri_hazirla(db, kullanici)
        self.baglam = KategoriBaglami(self.kategoriler)
        self.adaylar = rapor_adaylari(db, kullanici, tarih, a)
        self.satirlar = {r.item_id: r for r in db.scalars(select(RaporDuzeni).where(
            RaporDuzeni.user_id == kullanici.id, RaporDuzeni.tarih == tarih,
        ))}
        self.etkin = {m.id: self.baglam.etkin(m, self.satirlar.get(m.id)) for m in self.adaylar}
        karistir = a.karistir is not False
        self.bolumler: list[tuple[Kategori, list[Madde]]] = [
            (k, kategori_ici_sira([m for m in self.adaylar if self.etkin[m.id] == k.id], self.satirlar, tarih, karistir))
            for k in self.kategoriler
        ]

    @property
    def ozel(self) -> bool:
        return any(m.id in self.satirlar for m in self.adaylar)

    def duz(self) -> tuple[list[Madde], list[Madde]]:
        """(Yapılanlar, Devam eden). Eski sıra: elle, bulunan, sürekli; günün düzeninde sırası olanlar o sırayla öne geçer."""
        def tur_sirasi(*turler: str) -> list[Madde]:
            eski = [m for tur in turler for m in sorted((m for m in self.adaylar if m.tur == tur), key=lambda m: (m.sira, m.id))]
            yer = {m.id: i for i, m in enumerate(eski)}
            return sorted(eski, key=lambda m: (0, self.satirlar[m.id].sira) if m.id in self.satirlar else (1, yer[m.id]))
        return tur_sirasi("bugun", "bulunan", "surekli"), tur_sirasi("devam")

    def json(self) -> dict:
        yapilanlar, devam = self.duz()
        return {
            "kategoriler": [k.sozluk() for k in self.kategoriler],
            "bolumler": [{"kategori_id": k.id, "maddeler": [m.id for m in liste]} for k, liste in self.bolumler],
            "duz": {"yapilanlar": [m.id for m in yapilanlar], "devam": [m.id for m in devam]},
            "ozel": self.ozel,
            "genel": self.baglam.genel,
        }


def rapor_bicimi(a: KullaniciAyari) -> str:
    return a.rapor_bicimi if a.rapor_bicimi in RAPOR_BICIMLERI else "kategorili"


def rapor_metni_olustur(
    baslik: str, tarih: date, bicim: str, bolumler: list[tuple[str, list[str]]], duz: tuple[list[str], list[str]],
    yarin: list[str],
) -> str:
    """WhatsApp metni. kategorili: her dolu bölüm '*Ad:*' + maddeler; duz: Yapılanlar / Devam eden. Yarın en sonda."""
    satirlar = [f"*{baslik or 'Günlük Rapor'} – {tarih.strftime('%d.%m.%Y')}*", ""]
    if bicim == "duz":
        parcalar = [("Yapılanlar", duz[0]), ("Devam eden", duz[1]), ("Yarın", yarin)]
    else:
        parcalar = [(f"{ad}:", liste) for ad, liste in bolumler] + [("Yarın:", yarin)]
    for ad, liste in parcalar:
        if liste:
            satirlar += [f"*{ad}*"] + [f"• {x}" for x in liste] + [""]
    return "\n".join(satirlar).strip()


def gunun_rapor_metni(db: Session, kullanici: Kullanici, tarih: date, duzen: GunDuzeni | None = None) -> str:
    """Tikli maddelerden günün rapor metni; arayüzdeki önizlemeyle aynı kural."""
    a = ayar_satiri(db, kullanici)
    duzen = duzen or GunDuzeni(db, kullanici, tarih, a)
    ifadeler = bugunku_ifadeler(db, kullanici.id, tarih)

    def metin(m: Madde) -> str:
        return madde_rapor_metni(m, ifadeler.get(m.id), tarih)

    def kat_metin(m: Madde) -> str:  # kategorili biçimde devam eden işler "iş — aşama" olarak yazılır
        return m.metin + (f" — {m.asama}" if m.asama else "") if m.tur == "devam" else metin(m)

    yapilanlar, devam = duzen.duz()
    yarin = db.scalar(select(Madde.metin).where(
        Madde.user_id == kullanici.id, Madde.tur == "bugun", Madde.tarih == tarih, Madde.kaynak == "yarin",
    ))
    return rapor_metni_olustur(
        a.rapor_basligi or "", tarih, duzen.bicim,
        [(k.ad, [kat_metin(m) for m in liste if m.tikli]) for k, liste in duzen.bolumler],
        ([metin(m) for m in yapilanlar if m.tikli], [metin(m) for m in devam if m.tikli]),
        satirlara_bol(yarin or ""),
    )


def kacirilan_gun(db: Session, kullanici: Kullanici, a: KullaniciAyari, bugun_: date) -> date | None:
    """Hatırlatma günlerine göre bir önceki iş günü (en fazla 7 gün geri); günlük raporu yoksa o gün."""
    gunler = hatirlatma_ayari(a)["gunler"]
    onceki = next((g for g in (bugun_ - timedelta(days=i) for i in range(1, 8)) if g.isoweekday() in gunler), None)
    if onceki is None:
        return None
    if kullanici.olusturma and utc(kullanici.olusturma).astimezone(servisler.ISTANBUL).date() > onceki:
        return None  # hesap o günden sonra açılmış
    if db.scalar(select(Rapor.id).where(Rapor.user_id == kullanici.id, Rapor.tur == "gunluk", Rapor.tarih == onceki)):
        return None
    return onceki


# ---------------------------------------------------------------- durum

@router.get("/durum")
def durum(tarih: str | None = None, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    bugun_, tarih = bugun(), gun_sec(tarih)
    yapilanlari_bol(db, kullanici, tarih)
    a = ayar_satiri(db, kullanici)
    duzen = GunDuzeni(db, kullanici, tarih, a)
    maddeler = db.scalars(
        select(Madde)
        .where(Madde.user_id == kullanici.id, or_(Madde.tur.in_(("surekli", "devam")), Madde.tarih == tarih))
        .order_by(Madde.tur, Madde.sira, Madde.id)
    ).all()
    ifadeler = bugunku_ifadeler(db, kullanici.id, tarih)
    son_kopya = db.scalar(select(Rapor.olusturma).where(
        Rapor.user_id == kullanici.id, Rapor.tur == "gunluk", Rapor.tarih == tarih,
    ))
    onbellek = _onbellek.get((kullanici.id, tarih.isoformat())) or {}
    acik = acik_bulunan_kaynaklari(a)
    kacirilan = kacirilan_gun(db, kullanici, a, bugun_) if tarih == bugun_ else None
    return {
        "kullanici": {"ad": kullanici.ad, "rol": kullanici.rol},
        "tarih": tarih.isoformat(),
        "bugun": bugun_.isoformat(),
        "kacirilan_gun": kacirilan.isoformat() if kacirilan else None,
        "maddeler": [
            {**madde_json(m, ifadeler, tarih), "etkin_kategori_id": duzen.etkin.get(m.id)}
            for m in maddeler if m.tur != "bulunan" or m.kaynak in acik
        ],
        "duzen": duzen.json(),
        "rapor_metni": gunun_rapor_metni(db, kullanici, tarih, duzen),
        "ayarlar": ayar_ozeti(a, kullanici),
        "ai_anahtari": bool(ai_anahtari()),
        "son_kopya": zaman_iso(son_kopya),
        "tarama_zamani": onbellek.get("tarama_zamani"),
    }


# ---------------------------------------------------------------- maddeler

class MaddeYeni(BaseModel):
    tur: Literal["surekli", "devam", "bugun"]
    metin: str = ""
    asama: str | None = None
    tikli: bool = True
    kaynak: Literal["elle", "yapilanlar", "yarin"] | None = None
    tarih: date | None = None  # bugün/yarın satırlarının günü; boşsa bugün


class MaddeGuncelle(BaseModel):
    metin: str | None = None
    asama: str | None = None
    tikli: bool | None = None
    gizli: bool | None = None
    kategori_id: int | None = None  # null: otomatik kurala dön
    onemli: bool | None = None


class AiSecimi(BaseModel):
    kullan: bool | None = None
    yenile: bool = False


class Siralama(BaseModel):
    tur: Literal["surekli", "devam", "bulunan"]
    idler: list[int]


@router.post("/maddeler", status_code=201)
def madde_ekle(govde: MaddeYeni, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    tarih = gun_sec(govde.tarih)
    if govde.tur == "bugun" and govde.kaynak == "elle":
        metin = govde.metin.strip()
        if not metin:
            raise HTTPException(status_code=422, detail="Metin boş olamaz")
        madde = Madde(
            user_id=kullanici.id, tur="bugun", tarih=tarih, kaynak="elle", metin=metin, tikli=govde.tikli,
            sira=sonraki_sira(db, kullanici, "bugun", tarih),
        )
        db.add(madde)
    elif govde.tur == "bugun":
        # "Yarın" (ve eski "yapılanlar") metni gün başına tek satırdır; ikinci ekleme mevcut satırı günceller.
        if govde.kaynak is None:
            raise HTTPException(status_code=422, detail="bugün maddesi için kaynak gerekli (elle | yarin)")
        madde = db.scalar(select(Madde).where(
            Madde.user_id == kullanici.id, Madde.tur == "bugun", Madde.tarih == tarih, Madde.kaynak_id == govde.kaynak,
        ))
        if madde is None:
            madde = Madde(user_id=kullanici.id, tur="bugun", tarih=tarih, kaynak=govde.kaynak, kaynak_id=govde.kaynak)
            db.add(madde)
        madde.metin = govde.metin
    else:
        metin = govde.metin.strip()
        if not metin:
            raise HTTPException(status_code=422, detail="Metin boş olamaz")
        madde = Madde(
            user_id=kullanici.id, tur=govde.tur, metin=metin, asama=(govde.asama or "").strip() or None,
            tikli=govde.tikli, sira=sonraki_sira(db, kullanici, govde.tur),
        )
        db.add(madde)
    db.commit()
    return madde_json(madde, {}, tarih)


def kullanici_kategorisi(db: Session, kullanici: Kullanici, kategori_id: int) -> Kategori:
    kategori = db.get(Kategori, kategori_id)
    if kategori is None or kategori.user_id != kullanici.id:
        raise HTTPException(status_code=404, detail="Kategori bulunamadı")
    return kategori


def gunun_duzen_satiri(db: Session, kullanici: Kullanici, tarih: date, madde_id: int) -> RaporDuzeni | None:
    return db.scalar(select(RaporDuzeni).where(
        RaporDuzeni.user_id == kullanici.id, RaporDuzeni.tarih == tarih, RaporDuzeni.item_id == madde_id,
    ))


@router.patch("/maddeler/{madde_id}")
def madde_guncelle(
    madde_id: int, govde: MaddeGuncelle, tarih: str | None = None,
    kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    madde = kullanici_maddesi(db, kullanici, madde_id)
    tarih = gun_sec(tarih)
    serbest_metin = madde.tur == "bugun" and madde.kaynak in ("yarin", "yapilanlar")
    veri = govde.model_dump(exclude_unset=True)
    if "kategori_id" in veri:
        kategori_id = veri.pop("kategori_id")
        if kategori_id is not None and kullanici_kategorisi(db, kullanici, kategori_id).sistem:
            raise HTTPException(status_code=422, detail="Sistem kategorisi maddeye atanamaz")
        madde.kategori_id = kategori_id
        satir = gunun_duzen_satiri(db, kullanici, tarih, madde.id)
        if satir is not None and satir.kategori_id is not None:  # o günün düzeninde kategori varsa o da izler
            satir.kategori_id = kategori_id
    if veri.get("onemli") is not None and bool(veri["onemli"]) != bool(madde.onemli):
        satir = gunun_duzen_satiri(db, kullanici, tarih, madde.id)
        if satir is not None:  # yıldız, günün düzenindeki elle kategoriden önce gelsin
            satir.kategori_id = None
    for alan, deger in veri.items():
        if deger is None:
            continue
        if alan == "metin" and not serbest_metin:
            deger = deger.strip()
            if not deger:
                raise HTTPException(status_code=422, detail="Metin boş olamaz")
            if deger != madde.metin:
                if madde.tur == "surekli":  # şablon değişti; günün ifadesi yeniden üretilsin
                    db.execute(delete(GunlukIfade).where(GunlukIfade.item_id == madde.id, GunlukIfade.tarih == tarih))
                else:  # kullanıcı ne yazdıysa rapora o girer
                    madde.kullanici_duzenledi = True
                    madde.metin_ai = None
                    madde.ai_tarih = None
        if alan == "asama" and madde.tur == "devam" and (deger or "") != (madde.asama or ""):
            madde.metin_ai = None
            madde.ai_tarih = None
        setattr(madde, alan, deger)
    db.commit()
    return madde_json(madde, bugunku_ifadeler(db, kullanici.id, tarih, [madde.id]), tarih)


@router.patch("/maddeler/{madde_id}/ai")
def madde_ai(
    madde_id: int, govde: AiSecimi, tarih: str | None = None,
    kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    madde = kullanici_maddesi(db, kullanici, madde_id)
    tarih = gun_sec(tarih)
    hatalar: list[str] = []
    if govde.yenile:
        anahtar = ai_anahtari()
        if not anahtar:
            raise HTTPException(status_code=400, detail="Claude anahtarı tanımlı değil (ANTHROPIC_API_KEY)")
        with _kullanici_kilidi(kullanici.id, "duzelt"):
            if madde.tur == "surekli":
                db.execute(delete(GunlukIfade).where(GunlukIfade.item_id == madde.id, GunlukIfade.tarih == tarih))
            else:
                madde.metin_ai = None
                madde.ai_tarih = None
                madde.kullanici_duzenledi = False
            madde.ai_kullan = True
            db.commit()
            sonuc = duzeltmeyi_uygula(db, kullanici, [madde], tarih, anahtar)
            hatalar = [KOTA_MESAJI] if sonuc.get("atlandi") else sonuc["hatalar"]
    elif govde.kullan is not None:
        madde.ai_kullan = govde.kullan
        db.commit()
    return {**madde_json(madde, bugunku_ifadeler(db, kullanici.id, tarih, [madde.id]), tarih), "hatalar": hatalar}


@router.delete("/maddeler/{madde_id}")
def madde_sil(madde_id: int, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    madde = kullanici_maddesi(db, kullanici, madde_id)
    if madde.kaynak == "not":  # silinirse sonraki taramada geri gelirdi; gizlenir
        madde.gizli = True
    else:
        db.execute(delete(RaporDuzeni).where(RaporDuzeni.item_id == madde.id))
        db.delete(madde)
    db.commit()
    return {"ok": True}


@router.post("/maddeler/sira")
def madde_sirala(govde: Siralama, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    maddeler = {m.id: m for m in db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, Madde.tur == govde.tur, Madde.id.in_(govde.idler),
    ))}
    if len(maddeler) != len(set(govde.idler)):
        raise HTTPException(status_code=404, detail="Madde bulunamadı")
    for sira, madde_id in enumerate(govde.idler, start=1):
        maddeler[madde_id].sira = sira
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- bugün bulunanlar

_onbellek: dict[tuple[int, str], dict] = {}


def onbellegi_temizle() -> None:
    _onbellek.clear()


def eposta_eskilerini_gizle(mevcut: dict, sonuc: dict) -> None:
    """Bu taramada artık üretilmeyen bugünkü e-posta maddeleri (ör. E1 öncesi alıcı başına gruplu madde ya da
    gruplama ayarı değişince eski biçim) gizlenir, silinmez; kullanıcının düzenlediğine dokunulmaz.
    Gmail hata verdiyse ya da hiç e-posta bulunmadıysa hiçbir şey gizlenmez."""
    if not sonuc["eposta"] or any(h["kaynak"] == "gmail" for h in sonuc["hatalar"]):
        return
    guncel = {m["id"] for m in sonuc["eposta"]}
    for kaynak_id, m in mevcut.items():
        if m.kaynak == "eposta" and kaynak_id not in guncel and not m.kullanici_duzenledi:
            m.gizli = True


def bugun_taramasi(db: Session, kullanici: Kullanici, tarih: date, yenile: bool = False) -> dict:
    """Kullanıcı başına günde bir tarama (kilit + önbellek); bulunanları maddelere yazar, önbellek özetini döner."""
    anahtar = (kullanici.id, tarih.isoformat())
    with _kullanici_kilidi(kullanici.id):
        if yenile or anahtar not in _onbellek:
            mevcut = {m.kaynak_id: m for m in db.scalars(select(Madde).where(
                Madde.user_id == kullanici.id, Madde.tur == "bulunan", Madde.tarih == tarih,
            ))}
            # kullanıcının düzenlediği madde Claude'a hiç gitmez; metni değişen düzenlenmemiş madde yeniden çevrilir
            haric = {k: None if m.kullanici_duzenledi else m.metin for k, m in mevcut.items()}
            sonuc = servisler.raporu_uret(
                cozulmus_ayarlar(ayar_satiri(db, kullanici), kullanici), os.environ.get("ANTHROPIC_API_KEY", ""), haric, tarih,
            )
            notlar = set(db.scalars(select(Madde.kaynak_id).where(
                Madde.user_id == kullanici.id, Madde.tur == "bugun", Madde.tarih == tarih, Madde.kaynak == "not",
            )))
            not_sirasi = sonraki_sira(db, kullanici, "bugun", tarih)
            for m in sonuc.get("not", []):  # kendine atılan notlar bugünün yapılanlarına düşer
                if m["id"] in notlar:
                    continue
                zaman = m.get("kaynak_zaman")
                db.add(Madde(
                    user_id=kullanici.id, tur="bugun", metin=m["metin"], tarih=tarih, kaynak="not", kaynak_id=m["id"],
                    tikli=True, sira=not_sirasi, kaynak_zaman=utc(zaman) if zaman else None,
                ))
                notlar.add(m["id"])
                not_sirasi += 1
            sira = sonraki_sira(db, kullanici, "bulunan", tarih)
            for m in sonuc["eposta"] + sonuc["medusa"]:
                zaman = m.get("kaynak_zaman")
                eski = mevcut.get(m["id"])
                if eski is not None:
                    # aynı konuya gün içinde yeni mail: madde güncellenir, çoğalmaz; kullanıcı düzenlediyse dokunulmaz
                    if not eski.kullanici_duzenledi and eski.metin != m["metin"]:
                        eski.metin = m["metin"]
                        eski.metin_ai = m.get("metin_ai")
                        eski.ai_tarih = tarih if m.get("metin_ai") else None
                    if zaman and not eski.kullanici_duzenledi:
                        eski.kaynak_zaman = utc(zaman)
                    continue
                mevcut[m["id"]] = Madde(
                    user_id=kullanici.id, tur="bulunan", metin=m["metin"], tarih=tarih,
                    kaynak=m["kaynak"], kaynak_id=m["id"], tikli=True, sira=sira,
                    metin_ai=m.get("metin_ai"), ai_tarih=tarih if m.get("metin_ai") else None,
                    kaynak_zaman=utc(zaman) if zaman else None,
                )
                db.add(mevcut[m["id"]])
                sira += 1
            eposta_eskilerini_gizle(mevcut, sonuc)
            db.commit()
            # geçmiş günler de önbellekte kalır; düzenlenebilir aralığın dışına düşenler atılır
            sinir = (bugun() - timedelta(days=GECMIS_GUN)).isoformat()
            for eski in [k for k in _onbellek if k[0] == kullanici.id and k[1] < sinir]:
                del _onbellek[eski]
            _onbellek[anahtar] = {
                "hatalar": sonuc["hatalar"],
                "sayim": {"eposta": len(sonuc["eposta"]), "medusa": len(sonuc["medusa"])},
                "tarama_zamani": zaman_iso(simdi()),
            }
        return _onbellek[anahtar]


@router.get("/bugun")
def bugun_bulunanlar(
    yenile: int = 0, tarih: str | None = None, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)
) -> dict:
    tarih = gun_sec(tarih)
    onbellek = bugun_taramasi(db, kullanici, tarih, bool(yenile))
    bulunanlar = db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, Madde.tur == "bulunan", Madde.tarih == tarih,
        Madde.kaynak.in_(acik_bulunan_kaynaklari(ayar_satiri(db, kullanici))),
    ).order_by(Madde.sira, Madde.id)).all()
    return {"tarih": tarih.isoformat(), "bulunan": [madde_json(m, {}, tarih) for m in bulunanlar], **onbellek}


# ---------------------------------------------------------------- Claude ile düzeltme

def duzeltme_paketi(db: Session, kullanici: Kullanici, tarih: date) -> list[Madde]:
    """Bugünkü rapora girecek ve henüz düzeltilmemiş maddeler."""
    maddeler = db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, Madde.tikli.is_(True),
        or_(Madde.tur.in_(("surekli", "devam")), Madde.tarih == tarih),
    ).order_by(Madde.tur, Madde.sira, Madde.id)).all()
    ifadeler = bugunku_ifadeler(db, kullanici.id, tarih)
    acik = acik_bulunan_kaynaklari(ayar_satiri(db, kullanici))
    paket = []
    for m in maddeler:
        if m.tur == "surekli":
            secilir = m.id not in ifadeler
        elif m.tur == "bugun":  # e-postayla gelen notlar ve sesle eklenenler elle maddeler gibi düzeltilir
            secilir = m.kaynak in ELLE_KAYNAKLARI and not m.gizli and not m.kullanici_duzenledi and not m.metin_ai
        elif m.tur == "bulunan":
            secilir = m.kaynak in acik and not m.gizli and not m.kullanici_duzenledi and not m.metin_ai
        else:  # devam
            secilir = not m.kullanici_duzenledi and not m.metin_ai
        if secilir:
            paket.append(m)
    return paket


def claude_hakki_al(db: Session, user_id: int, tarih: date) -> ClaudeKullanim | None:
    """Çağrıdan önce günün sayacını bir artırır; sınır dolduysa None. Satır yoksa açılır (aynı anda açılırsa yeniden okunur)."""
    with _kullanici_kilidi(user_id, "kota"):
        for deneme in range(2):
            satir = db.scalar(select(ClaudeKullanim).where(ClaudeKullanim.user_id == user_id, ClaudeKullanim.tarih == tarih))
            if satir is None:
                satir = ClaudeKullanim(user_id=user_id, tarih=tarih, cagri=0, girdi_token=0, cikti_token=0)
                db.add(satir)
            if (satir.cagri or 0) >= GUNLUK_CLAUDE_SINIRI:
                return None
            satir.cagri = (satir.cagri or 0) + 1
            try:
                db.commit()
                return satir
            except IntegrityError:
                db.rollback()
                if deneme:
                    raise
    return None


def claude_kullanimini_yaz(db: Session, satir: ClaudeKullanim) -> None:
    """Son yanıtın usage alanını günün satırına ekler."""
    kullanim = servisler.son_kullanim()
    db.execute(ClaudeKullanim.__table__.update().where(ClaudeKullanim.id == satir.id).values(
        girdi_token=ClaudeKullanim.girdi_token + kullanim["girdi"],
        cikti_token=ClaudeKullanim.cikti_token + kullanim["cikti"],
    ))
    db.commit()


def aylik_claude_cagrilari(db: Session, tarih: date) -> dict[int, int]:
    """user_id → bu ay yapılan Claude çağrısı."""
    satirlar = db.execute(select(ClaudeKullanim.user_id, func.sum(ClaudeKullanim.cagri)).where(
        ClaudeKullanim.tarih >= tarih.replace(day=1), ClaudeKullanim.tarih <= tarih,
    ).group_by(ClaudeKullanim.user_id))
    return {uid: int(toplam or 0) for uid, toplam in satirlar}


def duzeltmeyi_uygula(db: Session, kullanici: Kullanici, paket: list[Madde], tarih: date, anahtar: str) -> dict:
    """Tek Claude çağrısı; hata olursa hiçbir maddeye yazılmaz. Günlük sınır dolduysa çağrılmaz, ham metin kalır.
    tarih düzeltilen gündür; kota her zaman çağrının yapıldığı günün (bugünün) sayacından düşer.
    Kategorisi olmayan elle/not/ses maddeleri için Claude'dan kategori önerisi de istenir (aynı çağrıda)."""
    if not paket:
        return {"duzeltilen": 0, "gonderilen": 0, "hatalar": []}
    hak = claude_hakki_al(db, kullanici.id, bugun())
    if hak is None:
        return {"atlandi": "günlük sınır", "duzeltilen": 0, "gonderilen": len(paket), "hatalar": []}
    secilebilir = [k for k in kategorileri_hazirla(db, kullanici) if not k.sistem]
    girdiler = []
    for m in paket:
        girdi = {"id": m.id, "tur": m.tur, "metin": m.metin}
        if m.tur == "devam" and m.asama:
            girdi["asama"] = m.asama
        if secilebilir and m.tur == "bugun" and m.kaynak in ELLE_KAYNAKLARI and m.kategori_id is None:
            girdi["kategori_sec"] = True
        girdiler.append(girdi)
    kategori_listesi = [{"id": k.id, "ad": k.ad} for k in secilebilir] if any(g.get("kategori_sec") for g in girdiler) else None
    son_raporlar = list(db.scalars(select(Rapor.metin).where(
        Rapor.user_id == kullanici.id, Rapor.tur == "gunluk", Rapor.tarih < tarih,
    ).order_by(Rapor.tarih.desc()).limit(3)))
    ayar = ayar_satiri(db, kullanici)
    proje_adi = ayar.proje_adi or ""
    kendi = kendi_alanlar(ayar, kullanici)
    servisler.kullanimi_sifirla()
    try:
        sonuc, oneriler = servisler.claude_duzelt_kategorili(
            girdiler, son_raporlar, anahtar, proje_adi, kategoriler=kategori_listesi,
            kendi_sirket=servisler.kendi_sirket_adlari(kendi, alan_sozlugu(ayar)))
    except servisler.ClaudeHatasi as e:
        return {"duzeltilen": 0, "gonderilen": len(paket), "hatalar": [f"Claude: {e}; ham metin kullanılıyor"]}
    except Exception as e:
        return {"duzeltilen": 0, "gonderilen": len(paket), "hatalar": [f"Claude: beklenmeyen hata ({e.__class__.__name__}); ham metin kullanılıyor"]}
    finally:
        claude_kullanimini_yaz(db, hak)
    gecerli_kategori = {k.id for k in secilebilir}
    istenen = {g["id"] for g in girdiler if g.get("kategori_sec")}
    for m in paket:  # sistem ya da başkasının kategorisi yok sayılır
        if m.id in istenen and oneriler.get(m.id) in gecerli_kategori:
            m.kategori_id = oneriler[m.id]
    duzeltilen = 0
    for m in paket:
        metin = sonuc.get(m.id)
        if not metin:
            continue
        duzeltilen += 1
        if m.tur == "surekli":
            db.add(GunlukIfade(user_id=kullanici.id, item_id=m.id, tarih=tarih, metin_ai=metin))
        else:
            m.metin_ai = metin
            m.ai_tarih = tarih
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"duzeltilen": 0, "gonderilen": len(paket), "hatalar": ["Düzeltme aynı anda iki kez çalıştı; sayfayı yenileyin"]}
    hatalar = [] if duzeltilen == len(paket) else [f"{len(paket) - duzeltilen} madde için Claude yanıt vermedi; ham metin kullanılıyor"]
    return {"duzeltilen": duzeltilen, "gonderilen": len(paket), "hatalar": hatalar}


@router.post("/duzelt")
def duzelt(tarih: str | None = None, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    tarih = gun_sec(tarih)
    anahtar = ai_anahtari()
    if not anahtar:
        return {"atlandi": "anahtar yok", "duzeltilen": 0, "gonderilen": 0, "hatalar": []}
    with _kullanici_kilidi(kullanici.id, "duzelt"):
        paket = duzeltme_paketi(db, kullanici, tarih)
        sonuc = duzeltmeyi_uygula(db, kullanici, paket, tarih, anahtar)
    ifadeler = bugunku_ifadeler(db, kullanici.id, tarih, [m.id for m in paket])
    return {**sonuc, "maddeler": [madde_json(m, ifadeler, tarih) for m in paket]}


# ---------------------------------------------------------------- sesle madde ekleme

class SesliNot(BaseModel):
    metin: str
    tarih: date | None = None


class SesliMadde(BaseModel):
    metin: str
    tur: Literal["bugun", "devam"] = "bugun"
    asama: str | None = None
    kategori_id: int | None = None


class SesliEkle(BaseModel):
    maddeler: list[SesliMadde]
    tarih: date | None = None


@router.post("/sesli-not")
def sesli_not(govde: SesliNot, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Dikte metnini maddelere böler, hiçbir şey yazmaz. Claude varsa (düzeltme kotasından 1 çağrı) böler, kategori ve
    'devam' önerir; anahtar yoksa, kota doluysa ya da yanıt bozuksa metin basitçe bölünür, kategori boş kalır."""
    tarih = gun_sec(govde.tarih)
    metin = govde.metin.strip()
    if not metin:
        raise HTTPException(status_code=422, detail="Metin boş olamaz")
    if len(metin) > SESLI_NOT_SINIRI:
        raise HTTPException(status_code=422, detail=f"Metin en fazla {SESLI_NOT_SINIRI} karakter olabilir")
    secilebilir = [k for k in kategorileri_hazirla(db, kullanici) if not k.sistem]
    sonuc: dict = {"tarih": tarih.isoformat(), "hatalar": []}
    maddeler = None
    anahtar = ai_anahtari()
    hak = claude_hakki_al(db, kullanici.id, bugun()) if anahtar else None
    if not anahtar:
        sonuc["atlandi"] = "anahtar yok"
    elif hak is None:
        sonuc["atlandi"] = "günlük sınır"
    else:
        ayar = ayar_satiri(db, kullanici)
        servisler.kullanimi_sifirla()
        try:
            maddeler = servisler.claude_sesli_bol(
                metin, anahtar, [{"id": k.id, "ad": k.ad} for k in secilebilir], ayar.proje_adi or "",
                servisler.kendi_sirket_adlari(kendi_alanlar(ayar, kullanici), alan_sozlugu(ayar)))
        except servisler.ClaudeHatasi as e:
            sonuc["hatalar"].append(f"Claude: {e}; metin basitçe bölündü")
        except Exception as e:
            sonuc["hatalar"].append(f"Claude: beklenmeyen hata ({e.__class__.__name__}); metin basitçe bölündü")
        finally:
            claude_kullanimini_yaz(db, hak)
    if maddeler is None:
        maddeler = servisler.sesi_basitce_bol(metin)
    gecerli = {k.id for k in secilebilir}
    for m in maddeler:  # sistem ya da başkasının kategorisi yok sayılır
        if m["kategori_id"] not in gecerli:
            m["kategori_id"] = None
    return {**sonuc, "maddeler": maddeler}


@router.post("/sesli-not/ekle", status_code=201)
def sesli_not_ekle(govde: SesliEkle, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Onaylanan maddeler: 'bugun' → seçili günün kaynak='ses' satırı (düzeltme paketine girer), 'devam' → devam eden iş."""
    tarih = gun_sec(govde.tarih)
    if not govde.maddeler:
        raise HTTPException(status_code=422, detail="Eklenecek madde yok")
    for m in govde.maddeler:
        if not m.metin.strip():
            raise HTTPException(status_code=422, detail="Metin boş olamaz")
        if m.kategori_id is not None and kullanici_kategorisi(db, kullanici, m.kategori_id).sistem:
            raise HTTPException(status_code=422, detail="Sistem kategorisi maddeye atanamaz")
    sira = {"bugun": sonraki_sira(db, kullanici, "bugun", tarih), "devam": sonraki_sira(db, kullanici, "devam")}
    yeniler = []
    for m in govde.maddeler:
        madde = Madde(user_id=kullanici.id, tur=m.tur, kaynak="ses", metin=m.metin.strip(), kategori_id=m.kategori_id,
                      tikli=True, sira=sira[m.tur])
        if m.tur == "bugun":
            madde.tarih = tarih
        else:
            madde.asama = (m.asama or "").strip() or None
        sira[m.tur] += 1
        db.add(madde)
        yeniler.append(madde)
    db.commit()
    return {"maddeler": [madde_json(m, {}, tarih) for m in yeniler]}


# ---------------------------------------------------------------- rapor geçmişi

class RaporYeni(BaseModel):
    metin: str
    tur: Literal["gunluk", "haftalik"] = "gunluk"
    hafta_baslangic: date | None = None
    tarih: date | None = None  # günlük raporun günü; ?tarih= ile de verilebilir, boşsa bugün


class HaftalikIstek(BaseModel):
    hafta_baslangic: date


def pazartesi_mi(tarih: date | None) -> date:
    if tarih is None or tarih.weekday() != 0:
        raise HTTPException(status_code=422, detail="hafta_baslangic bir Pazartesi olmalı")
    return tarih


def rapor_json(r: Rapor) -> dict:
    satirlar = r.metin.splitlines()
    return {
        "id": r.id, "tarih": r.tarih.isoformat(), "tur": r.tur,
        "hafta_baslangic": r.hafta_baslangic.isoformat() if r.hafta_baslangic else None,
        "metin": r.metin,
        "ilk_satir": next((x.strip() for x in satirlar if x.strip()), ""),
        "madde_sayisi": sum(1 for x in satirlar if x.lstrip().startswith("•")),
        "olusturma": zaman_iso(r.olusturma),
    }


@router.post("/raporlar")
def rapor_kaydet(
    govde: RaporYeni, tarih: str | None = None,
    kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    metin = govde.metin.strip()
    if not metin:
        raise HTTPException(status_code=422, detail="Rapor metni boş olamaz")
    if govde.tur == "haftalik":
        hafta = pazartesi_mi(govde.hafta_baslangic)
        tarih = hafta
    else:
        hafta, tarih = None, gun_sec(govde.tarih or tarih)
    kosul = (Rapor.user_id == kullanici.id, Rapor.tarih == tarih, Rapor.tur == govde.tur)
    with _kullanici_kilidi(kullanici.id, "rapor"):
        for deneme in range(2):
            rapor = db.scalar(select(Rapor).where(*kosul))
            if rapor is None:
                rapor = Rapor(user_id=kullanici.id, tarih=tarih, tur=govde.tur, hafta_baslangic=hafta, metin=metin)
                db.add(rapor)
            rapor.metin = metin
            rapor.olusturma = simdi()  # son kopyalanan kazanır
            try:
                db.commit()
                break
            except IntegrityError:
                db.rollback()
                if deneme:
                    raise
    return rapor_json(rapor)


@router.get("/raporlar")
def raporlari_listele(
    q: str = "", tur: str = "", limit: int = 50, offset: int = 0,
    kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    kosullar = [Rapor.user_id == kullanici.id]
    if tur:
        if tur not in ("gunluk", "haftalik"):
            raise HTTPException(status_code=422, detail="tur gunluk ya da haftalik olmalı")
        kosullar.append(Rapor.tur == tur)
    q = q.strip()
    if q:
        kacisli = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        kosullar.append(Rapor.metin.ilike(f"%{kacisli}%", escape="\\"))
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    toplam = db.scalar(select(func.count()).select_from(Rapor).where(*kosullar))
    raporlar = db.scalars(
        select(Rapor).where(*kosullar).order_by(Rapor.tarih.desc(), Rapor.id.desc()).limit(limit).offset(offset)
    ).all()
    return {"toplam": toplam, "raporlar": [rapor_json(r) for r in raporlar]}


@router.post("/haftalik")
def haftalik_ozet(govde: HaftalikIstek, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    pazartesi = pazartesi_mi(govde.hafta_baslangic)
    raporlar = db.scalars(select(Rapor).where(
        Rapor.user_id == kullanici.id, Rapor.tur == "gunluk",
        Rapor.tarih >= pazartesi, Rapor.tarih <= pazartesi + timedelta(days=6),
    ).order_by(Rapor.tarih)).all()
    if not raporlar:
        raise HTTPException(status_code=400, detail="Bu hafta için kayıtlı günlük rapor yok")
    anahtar = ai_anahtari()
    if not anahtar:
        raise HTTPException(status_code=400, detail="Claude anahtarı tanımlı değil (ANTHROPIC_API_KEY); haftalık özet üretilemez")
    bitis = max(raporlar[-1].tarih, min(pazartesi + timedelta(days=4), bugun()))
    hak = claude_hakki_al(db, kullanici.id, bugun())
    if hak is None:
        raise HTTPException(status_code=429, detail=f"Bugünkü Claude hakkın doldu (günde {GUNLUK_CLAUDE_SINIRI}); yarın yeniden dene")
    servisler.kullanimi_sifirla()
    try:
        metin = servisler.claude_haftalik(
            [(r.tarih, r.metin) for r in raporlar], servisler.hafta_basligi(pazartesi, bitis), anahtar,
        )
    except servisler.ClaudeHatasi as e:
        raise HTTPException(status_code=502, detail=f"Claude: {e}") from e
    finally:
        claude_kullanimini_yaz(db, hak)
    return {"metin": metin, "hafta_baslangic": pazartesi.isoformat(), "rapor_sayisi": len(raporlar)}


# ---------------------------------------------------------------- günün rapor düzeni (sıra + kategori)

class DuzenSatiri(BaseModel):
    item_id: int
    kategori_id: int | None = None


class DuzenYaz(BaseModel):
    tarih: date | None = None
    sirali: list[DuzenSatiri]


class DuzenKaristir(BaseModel):
    tarih: date | None = None


def duzen_yanit(db: Session, kullanici: Kullanici, tarih: date) -> dict:
    duzen = GunDuzeni(db, kullanici, tarih)
    return {"tarih": tarih.isoformat(), **duzen.json(), "etkin": {str(k): v for k, v in duzen.etkin.items()},
            "rapor_metni": gunun_rapor_metni(db, kullanici, tarih, duzen)}


def duzeni_yaz(db: Session, kullanici: Kullanici, tarih: date, satirlar: list[tuple[int, int | None]]) -> None:
    """Günün düzenini baştan yazar: (madde, elle kategori) sırayla; kategori None ise kural belirler."""
    db.execute(delete(RaporDuzeni).where(RaporDuzeni.user_id == kullanici.id, RaporDuzeni.tarih == tarih))
    for sira, (item_id, kategori_id) in enumerate(satirlar, start=1):
        db.add(RaporDuzeni(user_id=kullanici.id, tarih=tarih, item_id=item_id, kategori_id=kategori_id, sira=sira))
    db.commit()


@router.get("/rapor-duzeni")
def rapor_duzeni_getir(tarih: str | None = None, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    return duzen_yanit(db, kullanici, gun_sec(tarih))


@router.post("/rapor-duzeni")
def rapor_duzeni_yaz(govde: DuzenYaz, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Düzenle modunda sürükle-bırak sonrası günün tam sırası. Kategori, maddenin kuralla bulunan kategorisinden
    farklıysa saklanır; aynıysa saklanmaz ki sonradan yıldız ya da seçici kuralı işlesin."""
    tarih = gun_sec(govde.tarih)
    idler = [s.item_id for s in govde.sirali]
    if len(set(idler)) != len(idler):
        raise HTTPException(status_code=422, detail="Bir madde sırada iki kez geçemez")
    maddeler = {m.id: m for m in db.scalars(select(Madde).where(Madde.user_id == kullanici.id, Madde.id.in_(idler)))}
    if len(maddeler) != len(idler):
        raise HTTPException(status_code=404, detail="Madde bulunamadı")
    baglam = KategoriBaglami(kategorileri_hazirla(db, kullanici))
    with _kullanici_kilidi(kullanici.id, "duzen"):
        satirlar = []
        for s in govde.sirali:
            if s.kategori_id is not None and s.kategori_id not in baglam.idler:
                raise HTTPException(status_code=404, detail="Kategori bulunamadı")
            elle = s.kategori_id if s.kategori_id is not None and s.kategori_id != baglam.dogal(maddeler[s.item_id]) else None
            satirlar.append((s.item_id, elle))
        duzeni_yaz(db, kullanici, tarih, satirlar)
    return duzen_yanit(db, kullanici, tarih)


@router.post("/rapor-duzeni/karistir")
def rapor_duzeni_karistir(
    govde: DuzenKaristir | None = None, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    """Her kategorinin içini yeniden rastgele sıralar; maddeler kategorilerinde kalır."""
    tarih = gun_sec(govde.tarih if govde else None)
    with _kullanici_kilidi(kullanici.id, "duzen"):
        duzen = GunDuzeni(db, kullanici, tarih)
        satirlar = []
        for _, liste in duzen.bolumler:
            liste = list(liste)
            random.shuffle(liste)
            satirlar += [(m.id, duzen.satirlar[m.id].kategori_id if m.id in duzen.satirlar else None) for m in liste]
        duzeni_yaz(db, kullanici, tarih, satirlar)
    return duzen_yanit(db, kullanici, tarih)


@router.delete("/rapor-duzeni")
def rapor_duzeni_sifirla(tarih: str | None = None, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    tarih = gun_sec(tarih)
    db.execute(delete(RaporDuzeni).where(RaporDuzeni.user_id == kullanici.id, RaporDuzeni.tarih == tarih))
    db.commit()
    return duzen_yanit(db, kullanici, tarih)


# ---------------------------------------------------------------- rapor kategorileri

class KategoriYeni(BaseModel):
    ad: str
    kaynaklar: list[str] = []


class KategoriGuncelle(BaseModel):
    ad: str | None = None
    kaynaklar: list[str] | None = None
    sira: int | None = None


class KategoriSirasi(BaseModel):
    idler: list[int]


def kategori_adi(ad: str) -> str:
    ad = re.sub(r"\s+", " ", ad or "").strip()
    if not ad:
        raise HTTPException(status_code=422, detail="Kategori adı boş olamaz")
    if len(ad) > 80:
        raise HTTPException(status_code=422, detail="Kategori adı en fazla 80 karakter olabilir")
    return ad


def kategori_kaynaklari(kaynaklar: list[str]) -> list[str]:
    bilinmeyen = set(kaynaklar) - set(KAYNAKLAR)
    if bilinmeyen:
        raise HTTPException(status_code=422, detail=f"Bilinmeyen kaynak: {', '.join(sorted(bilinmeyen))}")
    return list(dict.fromkeys(kaynaklar))


def kategori_listesi(db: Session, kullanici: Kullanici) -> dict:
    return {"kategoriler": [k.sozluk() for k in kategorileri_hazirla(db, kullanici)]}


@router.get("/kategoriler")
def kategorileri_getir(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    return kategori_listesi(db, kullanici)


@router.post("/kategoriler", status_code=201)
def kategori_ekle(govde: KategoriYeni, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    kategorileri_hazirla(db, kullanici)
    sira = (db.scalar(select(func.max(Kategori.sira)).where(Kategori.user_id == kullanici.id)) or 0) + 1
    kategori = Kategori(user_id=kullanici.id, ad=kategori_adi(govde.ad), kaynaklar=kategori_kaynaklari(govde.kaynaklar), sira=sira)
    db.add(kategori)
    db.commit()
    return kategori.sozluk()


@router.patch("/kategoriler/{kategori_id}")
def kategori_guncelle(
    kategori_id: int, govde: KategoriGuncelle, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    """Sistem kategorisinin yalnız adı ve sırası değişir."""
    kategori = kullanici_kategorisi(db, kullanici, kategori_id)
    if govde.ad is not None:
        kategori.ad = kategori_adi(govde.ad)
    if govde.kaynaklar is not None:
        kaynaklar = kategori_kaynaklari(govde.kaynaklar)
        if kategori.sistem and kaynaklar:
            raise HTTPException(status_code=422, detail="Sistem kategorisine kaynak bağlanamaz")
        kategori.kaynaklar = kaynaklar
    if govde.sira is not None:
        kategori.sira = govde.sira
    db.commit()
    return kategori.sozluk()


@router.delete("/kategoriler/{kategori_id}")
def kategori_sil(kategori_id: int, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Maddeler kategorisiz kalır (kurala göre Genel İşler'e düşer); sistem kategorisi silinemez."""
    kategori = kullanici_kategorisi(db, kullanici, kategori_id)
    if kategori.sistem:
        raise HTTPException(status_code=400, detail="Sistem kategorisi silinemez")
    # sqlite ON DELETE SET NULL'u zorlamadığı için elle
    db.execute(Madde.__table__.update().where(Madde.user_id == kullanici.id, Madde.kategori_id == kategori.id).values(kategori_id=None))
    db.execute(RaporDuzeni.__table__.update().where(
        RaporDuzeni.user_id == kullanici.id, RaporDuzeni.kategori_id == kategori.id).values(kategori_id=None))
    db.delete(kategori)
    db.commit()
    return kategori_listesi(db, kullanici)


@router.post("/kategoriler/sira")
def kategori_sirala(govde: KategoriSirasi, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Verilen sıra başa, listede olmayanlar mevcut sıralarıyla sona."""
    kategoriler = kategorileri_hazirla(db, kullanici)
    bizim = {k.id: k for k in kategoriler}
    if len(set(govde.idler)) != len(govde.idler) or any(i not in bizim for i in govde.idler):
        raise HTTPException(status_code=404, detail="Kategori bulunamadı")
    sirali = [bizim[i] for i in govde.idler] + [k for k in kategoriler if k.id not in set(govde.idler)]
    for sira, k in enumerate(sirali, start=1):
        k.sira = sira
    db.commit()
    return kategori_listesi(db, kullanici)


# ---------------------------------------------------------------- ayarlar

class AyarGuncelle(BaseModel):
    gmail_kullanici: str | None = None
    gmail_sifre: str | None = None
    github_token: str | None = None
    github_repo: str | None = None
    proje_adi: str | None = None
    patron_telefon: str | None = None
    rapor_basligi: str | None = None
    alan_sozlugu: str | dict[str, str] | None = None
    hatirlatma_saat: str | None = None
    hatirlatma_gunler: list[int] | str | None = None
    hatirlatma_push: bool | None = None
    hatirlatma_eposta: bool | None = None
    hatirlatma_eposta_adres: str | None = None
    kaynaklar: dict[str, bool] | None = None
    eposta_gruplama: Literal["konu", "alici"] | None = None
    rapor_bicimi: Literal["kategorili", "duz"] | None = None
    karistir: bool | None = None
    kendi_alanlar: str | list[str] | None = None
    ekip_ici_atla: bool | None = None


def kendi_alanlari_ayristir(deger: str | list[str]) -> list[str]:
    """Virgül, boşluk ya da satırla ayrılmış alan adları; '@' öneki ve büyük harf yok sayılır."""
    parcalar = re.split(r"[\s,;]+", deger) if isinstance(deger, str) else deger
    sonuc = []
    for p in parcalar:
        alan = (p or "").strip().lstrip("@").lower().rstrip(".")
        if not alan:
            continue
        if not ALAN_BICIMI.match(alan):
            raise HTTPException(status_code=422, detail=f"Alan adı anlaşılamadı: {alan!r} (ör. ilsvision.com)")
        if alan not in sonuc:
            sonuc.append(alan)
    return sonuc


def sozlugu_ayristir(deger: str | dict[str, str]) -> dict[str, str]:
    if isinstance(deger, dict):
        satirlar = [f"{k}={v}" for k, v in deger.items()]
    else:
        satirlar = deger.splitlines()
    sonuc = {}
    for satir in satirlar:
        if not satir.strip():
            continue
        alan, esit, kurum = satir.partition("=")
        if not esit or not alan.strip() or not kurum.strip():
            raise HTTPException(status_code=422, detail=f"Alan adı sözlüğünde satır anlaşılamadı: {satir.strip()!r} (biçim: alan=Kurum)")
        sonuc[alan.strip().lower()] = kurum.strip()
    return sonuc


@router.get("/ayarlar")
def ayarlari_getir(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    return ayar_ozeti(ayar_satiri(db, kullanici), kullanici)


@router.put("/ayarlar")
def ayarlari_kaydet(govde: AyarGuncelle, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    a = ayar_satiri(db, kullanici)
    db.add(a)
    veri = govde.model_dump(exclude_unset=True)
    secim = veri.pop("kaynaklar", None)
    if secim is not None:
        bilinmeyen = set(secim) - set(KAYNAKLAR)
        if bilinmeyen:
            raise HTTPException(status_code=422, detail=f"Bilinmeyen kaynak: {', '.join(sorted(bilinmeyen))}")
        # yeni sözlük atanır (JSON kolonunda yerinde değişiklik izlenmez); gelmeyen kaynak olduğu gibi kalır
        a.kaynaklar = {**kaynak_durumu(a), **{k: bool(v) and k not in YAKINDA for k, v in secim.items()}}
    for alan in ("gmail_sifre", "github_token"):
        deger = (veri.pop(alan, None) or "").strip()
        if deger:  # boş bırakılan yazma-yalnız alan kaydı değiştirmez
            setattr(a, alan + "_enc", guvenlik.sifrele(deger))
    if "alan_sozlugu" in veri:
        deger = veri.pop("alan_sozlugu")
        a.alan_sozlugu = None if deger is None else sozlugu_ayristir(deger)
    saat = veri.pop("hatirlatma_saat", None)
    if saat is not None:
        eslesme = SAAT_BICIMI.match(saat.strip())
        if not eslesme:
            raise HTTPException(status_code=422, detail="Hatırlatma saati SS:DD biçiminde olmalı")
        a.hatirlatma_saat = time(int(eslesme.group(1)), int(eslesme.group(2)))
    gunler = veri.pop("hatirlatma_gunler", None)
    if gunler is not None:
        a.hatirlatma_gunler = ",".join(map(str, gunleri_ayristir(gunler)))
    gruplama = veri.pop("eposta_gruplama", None)
    if gruplama is not None:
        a.eposta_gruplama = gruplama
    if "kendi_alanlar" in veri:
        deger = veri.pop("kendi_alanlar")
        a.kendi_alanlar = kendi_alanlari_ayristir(deger) if deger is not None else None
    for alan in ("hatirlatma_push", "hatirlatma_eposta", "rapor_bicimi", "karistir", "ekip_ici_atla"):
        deger = veri.pop(alan, None)
        if deger is not None:
            setattr(a, alan, deger)
    if "hatirlatma_eposta_adres" in veri:
        adres = (veri.pop("hatirlatma_eposta_adres") or "").strip().lower() or None
        if adres and not EPOSTA_BICIMI.match(adres):
            raise HTTPException(status_code=422, detail="Hatırlatma e-posta adresi geçerli değil")
        a.hatirlatma_eposta_adres = adres
    for alan, deger in veri.items():
        deger = (deger or "").strip() or None
        if alan == "github_repo" and deger and not REPO_BICIMI.match(deger):
            raise HTTPException(status_code=422, detail="GitHub repo 'kullanici/repo' biçiminde olmalı")
        if alan == "gmail_kullanici" and deger:
            deger = deger.lower()
        setattr(a, alan, deger)
    db.commit()
    return ayar_ozeti(a, kullanici)


@router.post("/ayarlar/test")
def baglantiyi_test_et(
    kaynak: Literal["", "gmail", "github"] = "", kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    """kaynak verilirse yalnız o kaynak denenir (kurulum sihirbazı Gmail adımı)."""
    ayarlar = cozulmus_ayarlar(ayar_satiri(db, kullanici))
    parcalar, gmail_ok, github_ok = [], False, False

    if kaynak != "github":
        if ayarlar["gmail_kullanici"] and ayarlar["gmail_sifre"]:
            try:
                parcalar.append(servisler.gmail_test(ayarlar["gmail_kullanici"], ayarlar["gmail_sifre"]))
                gmail_ok = True
            except servisler.KaynakHatasi as e:
                parcalar.append(f"Gmail: {e}")
            except Exception as e:
                parcalar.append(f"Gmail: beklenmeyen hata ({e.__class__.__name__})")
        else:
            parcalar.append("Gmail: ayar girilmemiş")

    if kaynak != "gmail":
        if ayarlar["github_token"] and ayarlar["github_repo"]:
            try:
                parcalar.append(servisler.github_test(ayarlar["github_token"], ayarlar["github_repo"]))
                github_ok = True
            except servisler.KaynakHatasi as e:
                parcalar.append(f"GitHub: {e}")
            except Exception as e:
                parcalar.append(f"GitHub: beklenmeyen hata ({e.__class__.__name__})")
        else:
            parcalar.append("GitHub: ayar girilmemiş")

    return {"sonuc": " · ".join(parcalar), "gmail_ok": gmail_ok, "github_ok": github_ok}


# ---------------------------------------------------------------- ilk kurulum sihirbazı

class KurulumProfil(BaseModel):
    ad: str | None = None
    rapor_basligi: str | None = None
    patron_telefon: str | None = None


class KurulumSurekli(BaseModel):
    metinler: list[str]


def _metin_anahtari(metin: str) -> str:
    return servisler._kucult(re.sub(r"\s+", " ", metin).strip())


def surekli_metinleri(db: Session, kullanici: Kullanici) -> list[str]:
    return list(db.scalars(select(Madde.metin).where(
        Madde.user_id == kullanici.id, Madde.tur == "surekli",
    ).order_by(Madde.sira, Madde.id)))


@router.get("/kurulum")
def kurulum_bilgisi(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    return {
        "ad": kullanici.ad, "eposta": kullanici.eposta,
        "ayarlar": ayar_ozeti(ayar_satiri(db, kullanici), kullanici),
        "sablonlar": servisler.SUREKLI_SABLONLAR,
        "surekli": surekli_metinleri(db, kullanici),
    }


@router.post("/kurulum/profil")
def kurulum_profili(govde: KurulumProfil, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    veri = govde.model_dump(exclude_unset=True)
    if "ad" in veri:
        ad = (veri["ad"] or "").strip()
        if not ad:
            raise HTTPException(status_code=422, detail="Ad boş olamaz")
        kullanici.ad = ad[:120]
    a = ayar_satiri(db, kullanici)
    db.add(a)
    for alan in ("rapor_basligi", "patron_telefon"):
        if alan in veri:
            setattr(a, alan, (veri[alan] or "").strip() or None)
    db.commit()
    return {"ad": kullanici.ad, **ayar_ozeti(a, kullanici)}


@router.post("/kurulum/surekli")
def kurulum_surekli(govde: KurulumSurekli, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Seçilen satırlar listenin sonuna eklenir; mevcut sürekli işler korunur, aynı metin ikinci kez eklenmez."""
    with _kullanici_kilidi(kullanici.id, "kurulum"):
        gorulen = {_metin_anahtari(m) for m in surekli_metinleri(db, kullanici)}
        sira = sonraki_sira(db, kullanici, "surekli")
        eklenen = 0
        for metin in govde.metinler:
            metin = re.sub(r"\s+", " ", metin or "").strip()
            if not metin or _metin_anahtari(metin) in gorulen:
                continue
            db.add(Madde(user_id=kullanici.id, tur="surekli", metin=metin, tikli=True, sira=sira))
            gorulen.add(_metin_anahtari(metin))
            sira += 1
            eklenen += 1
        db.commit()
    return {"eklenen": eklenen, "surekli": surekli_metinleri(db, kullanici)}


@router.post("/kurulum/bitir")
def kurulum_bitir(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    a = ayar_satiri(db, kullanici)
    db.add(a)
    a.kurulum_tamam = True
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- eski localStorage verisini içe aktarma

def _metin(deger) -> str:
    return deger.strip() if isinstance(deger, str) else ""


@router.post("/ice-aktar")
def ice_aktar(
    govde: dict = Body(...), kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)
) -> dict:
    """gunluk-rapor-v2 biçimi: recurring[{text,on}], ongoing[{text,stage,on}], daily{date,done,plan}, settings{phone,title}."""
    # Yalnız kalıcı maddeler engeller; sayfa açılışında oluşan bulunan/bugün satırları taşımayı durdurmaz.
    if db.scalar(select(func.count()).select_from(Madde).where(
        Madde.user_id == kullanici.id, Madde.tur.in_(("surekli", "devam")),
    )):
        raise HTTPException(status_code=409, detail="Bu hesapta zaten sürekli/devam eden iş var; içe aktarma yapılmadı")

    sayim = {"surekli": 0, "devam": 0, "bugun": 0}
    for tur, liste in (("surekli", govde.get("recurring")), ("devam", govde.get("ongoing"))):
        for x in liste if isinstance(liste, list) else []:
            if not isinstance(x, dict) or not _metin(x.get("text")):
                continue
            sayim[tur] += 1
            db.add(Madde(
                user_id=kullanici.id, tur=tur, metin=_metin(x["text"]),
                asama=(_metin(x.get("stage")) or None) if tur == "devam" else None,
                tikli=x.get("on") is not False, sira=sayim[tur],
            ))

    tarih = bugun()
    gunluk = govde.get("daily")
    if isinstance(gunluk, dict) and gunluk.get("date") == tarih.isoformat():
        mevcut = set(db.scalars(select(Madde.kaynak).where(
            Madde.user_id == kullanici.id, Madde.tur == "bugun", Madde.tarih == tarih,
        )))
        # mevcut satırlara dokunulmaz
        if isinstance(gunluk.get("done"), str) and not mevcut & {"elle", "yapilanlar"}:
            for sira, satir in enumerate(satirlara_bol(gunluk["done"]), start=1):
                sayim["bugun"] += 1
                db.add(Madde(user_id=kullanici.id, tur="bugun", tarih=tarih, kaynak="elle", metin=satir, sira=sira))
        plan = gunluk.get("plan")
        if isinstance(plan, str) and plan.strip() and "yarin" not in mevcut:
            sayim["bugun"] += 1
            db.add(Madde(user_id=kullanici.id, tur="bugun", tarih=tarih, kaynak="yarin", kaynak_id="yarin", metin=plan))

    ayar = govde.get("settings")
    if isinstance(ayar, dict):
        a = ayar_satiri(db, kullanici)
        db.add(a)
        if _metin(ayar.get("phone")):
            a.patron_telefon = _metin(ayar["phone"])
        if _metin(ayar.get("title")):
            a.rapor_basligi = _metin(ayar["title"])

    db.commit()
    return {"ok": True, **sayim}


# ---------------------------------------------------------------- web push abonelikleri

def app_url() -> str:
    return (os.environ.get("APP_URL") or "").strip() or "https://gunluk-rapor.onrender.com"


def cihaz_adi_bul(ua: str) -> str:
    """User-Agent'tan kısa ad: 'iPhone Safari', 'Mac Chrome'."""
    ua = ua or ""
    cihaz = next((ad for anahtar, ad in (
        ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
        ("Windows", "Windows"), ("Linux", "Linux"),
    ) if anahtar in ua), "Cihaz")
    if "Edg" in ua:
        tarayici = "Edge"
    elif "Firefox/" in ua or "FxiOS/" in ua:
        tarayici = "Firefox"
    elif "Chrome/" in ua or "CriOS/" in ua:
        tarayici = "Chrome"
    elif "Safari/" in ua or cihaz in ("iPhone", "iPad", "Mac"):  # ana ekran uygulamasının UA'sında 'Safari' yok
        tarayici = "Safari"
    else:
        tarayici = "Tarayıcı"
    return f"{cihaz} {tarayici}"


def abonelik_json(a: PushAbonelik) -> dict:
    return {
        "id": a.id, "cihaz_adi": a.cihaz_adi, "olusturma": zaman_iso(a.olusturma),
        "son_basari": zaman_iso(a.son_basari), "son_hata": a.son_hata,
    }


class PushAnahtarlari(BaseModel):
    p256dh: str
    auth: str


class PushAbonelikBilgisi(BaseModel):
    endpoint: str
    keys: PushAnahtarlari


class PushAboneIstek(BaseModel):
    subscription: PushAbonelikBilgisi
    cihaz_adi: str | None = None


def push_cihazlarina_gonder(db: Session, user_id: int, veri: dict) -> tuple[int, int, list[dict]]:
    """404/410 → abonelik silinir; diğer hata son_hata'ya yazılır. (başarılı, toplam, cihaz başına ayrıntı)."""
    abonelikler = db.scalars(select(PushAbonelik).where(PushAbonelik.user_id == user_id).order_by(PushAbonelik.id)).all()
    basarili, ayrinti = 0, []
    for a in abonelikler:
        kod, mesaj = servisler.push_gonder({"endpoint": a.endpoint, "keys": {"p256dh": a.p256dh, "auth": a.auth}}, veri)
        satir = {"id": a.id, "cihaz_adi": a.cihaz_adi}
        if kod is None:
            basarili += 1
            a.son_basari, a.son_hata = simdi(), None
            satir.update(durum="ok", mesaj="")
        elif kod in (404, 410):
            db.delete(a)
            satir.update(durum="silindi", mesaj="Abonelik artık geçerli değil; kaldırıldı")
        else:
            a.son_hata = mesaj
            satir.update(durum="hata", mesaj=mesaj)
        ayrinti.append(satir)
        db.commit()
    return basarili, len(abonelikler), ayrinti


@router.get("/push/anahtar")
def push_anahtari(kullanici: Kullanici = Depends(aktif_kullanici)) -> dict:
    return {"anahtar": servisler.vapid_anahtarlari()[0]}


@router.get("/push/abone")
def push_abonelikleri(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    abonelikler = db.scalars(select(PushAbonelik).where(PushAbonelik.user_id == kullanici.id).order_by(PushAbonelik.id))
    return {"cihazlar": [abonelik_json(a) for a in abonelikler]}


@router.post("/push/abone")
def push_abone_ol(
    govde: PushAboneIstek, request: Request,
    kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum),
) -> dict:
    abonelik = govde.subscription
    if not abonelik.endpoint.startswith("https://"):
        raise HTTPException(status_code=422, detail="Geçersiz abonelik adresi")
    cihaz = ((govde.cihaz_adi or "").strip() or cihaz_adi_bul(request.headers.get("user-agent", "")))[:60]
    for deneme in range(2):
        a = db.scalar(select(PushAbonelik).where(PushAbonelik.endpoint == abonelik.endpoint))
        if a is None:
            a = PushAbonelik(endpoint=abonelik.endpoint)
            db.add(a)
        a.user_id, a.cihaz_adi, a.son_hata = kullanici.id, cihaz, None
        a.p256dh, a.auth = abonelik.keys.p256dh, abonelik.keys.auth
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            if deneme:
                raise
    return abonelik_json(a)


@router.delete("/push/abone/{abonelik_id}")
def push_abonelik_sil(abonelik_id: int, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    a = db.get(PushAbonelik, abonelik_id)
    if a is None or a.user_id != kullanici.id:
        raise HTTPException(status_code=404, detail="Abonelik bulunamadı")
    db.delete(a)
    db.commit()
    return {"ok": True}


@router.post("/push/dene")
def push_dene(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    basarili, toplam, ayrinti = push_cihazlarina_gonder(
        db, kullanici.id, {"baslik": "Günlük Rapor", "govde": "Bildirimler çalışıyor", "url": app_url()},
    )
    return {"basarili": basarili, "toplam": toplam, "cihazlar": ayrinti}


# ---------------------------------------------------------------- 17:00 hatırlatması (cron ucu)

SURE_SINIRI = 20  # sn; aşılırsa kalan kullanıcılar sonraki ping'e kalır
son_hatirlat_ping: str | None = None
_hatirlat_kilidi = threading.Lock()


def istanbul_simdi() -> datetime:
    return datetime.now(servisler.ISTANBUL)


def cron_tokeni_dogru(request: Request, token: str) -> bool:
    beklenen = (os.environ.get("CRON_TOKEN") or "").strip()
    if not beklenen:
        return False
    yetki = request.headers.get("authorization", "")
    gelen = token or (yetki[7:].strip() if yetki[:7].lower() == "bearer " else "")
    return hmac.compare_digest(gelen.encode(), beklenen.encode())


def hatirlatma_ozeti(bulunanlar: list[Madde]) -> str:
    eposta = sum(1 for m in bulunanlar if m.kaynak == "eposta")
    commit = len(bulunanlar) - eposta
    parcalar = ([f"{eposta} e-posta"] if eposta else []) + ([f"{commit} commit"] if commit else [])
    if not parcalar:
        return "Bugün için bulunan yok, yapılanları ekle"
    return "Bugünün raporu hazır bekliyor · " + ", ".join(parcalar) + " bulundu"


def hatirlatma_epostasi(ozet: str, bulunanlar: list[Madde], tarih: date) -> str:
    satirlar = [ozet, ""]
    if bulunanlar:
        satirlar += [f"• {madde_rapor_metni(m, None, tarih)}" for m in bulunanlar] + [""]
    return "\n".join(satirlar + [app_url(), "", "Bu hatırlatma, raporu kopyaladığın gün gelmez."])


KANAL_ADI = {"push": "push", "eposta": "e-posta"}


def son_hatirlatmalar(db: Session) -> dict[int, dict]:
    """Kullanıcı → en son gönderim günü ve o günün kanal sonuçları (yönetim ekranı için)."""
    son = dict(db.execute(select(
        HatirlatmaGonderimi.user_id, func.max(HatirlatmaGonderimi.tarih),
    ).group_by(HatirlatmaGonderimi.user_id)).all())
    if not son:
        return {}
    kosullar = [(HatirlatmaGonderimi.user_id == uid) & (HatirlatmaGonderimi.tarih == t) for uid, t in son.items()]
    ozet: dict[int, dict] = {uid: {"tarih": t, "kanallar": []} for uid, t in son.items()}
    for g in db.scalars(select(HatirlatmaGonderimi).where(or_(*kosullar)).order_by(
        HatirlatmaGonderimi.kanal, HatirlatmaGonderimi.id,
    )):
        ozet[g.user_id]["kanallar"].append({
            "ad": KANAL_ADI.get(g.kanal, g.kanal),
            "ok": g.durum == "gonderildi",
            "neden": "" if g.durum == "gonderildi" else (g.hata_metni or g.durum or "")[:60],
        })
    return ozet


def kanal_talep_et(db: Session, user_id: int, tarih: date, kanal: str) -> HatirlatmaGonderimi | None:
    """Gönderimden önce satırı yazar; aynı gün başka bir ping bu kanalı aldıysa None."""
    kayit = HatirlatmaGonderimi(user_id=user_id, tarih=tarih, kanal=kanal, durum="gonderiliyor")
    db.add(kayit)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return kayit


def kanal_hatasi_yaz(kayit: HatirlatmaGonderimi, user_id: int, kanal: str, neden: str) -> None:
    """Gönderim satırını 'hata' olarak kapatır ve nedeni WARNING olarak loglar (adres/şifre yazılmaz)."""
    kayit.durum = "hata"
    kayit.hata_metni = neden[:200]
    log.warning("hatirlat kanal hatasi user=%s kanal=%s neden=%s", user_id, kanal, neden[:200])


def kullaniciya_hatirlat(db: Session, k: Kullanici, an: datetime) -> dict:
    """Sonucu döner ve kullanıcı başına tek satır INFO logu yazar."""
    sonuc = _kullaniciya_hatirlat(db, k, an)
    log.info("hatirlat user=%s push=%s eposta=%s neden=%s",
             sonuc["user_id"], sonuc["push"], sonuc["eposta"], sonuc["neden"] or "-")
    return sonuc


def _kullaniciya_hatirlat(db: Session, k: Kullanici, an: datetime) -> dict:
    tarih = an.date()
    sonuc = {"user_id": k.id, "push": "atlandı", "eposta": "atlandı", "neden": ""}
    a = ayar_satiri(db, k)
    h = hatirlatma_ayari(a)
    if tarih.isoweekday() not in h["gunler"]:
        sonuc["neden"] = "bugün hatırlatma günü değil"
        return sonuc
    if an.time() < h["saat"]:
        sonuc["neden"] = f"saat {h['saat'].strftime('%H:%M')} olmadı"
        return sonuc
    if db.scalar(select(Rapor.id).where(Rapor.user_id == k.id, Rapor.tur == "gunluk", Rapor.tarih == tarih)):
        sonuc["neden"] = "bugünün raporu kopyalanmış"
        return sonuc

    islenmis = set(db.scalars(select(HatirlatmaGonderimi.kanal).where(
        HatirlatmaGonderimi.user_id == k.id, HatirlatmaGonderimi.tarih == tarih,
    )))
    cihaz_sayisi = db.scalar(select(func.count()).select_from(PushAbonelik).where(PushAbonelik.user_id == k.id))
    ayarlar = cozulmus_ayarlar(a)
    nedenler = []
    push_gerekli = eposta_gerekli = False
    if not h["push"]:
        nedenler.append("push kapalı")
    elif "push" in islenmis:
        nedenler.append("push bugün gönderildi")
    elif not cihaz_sayisi:
        sonuc["push"] = "0/0"  # kayıt açılmaz: gün içinde cihaz eklenirse sonraki ping gönderir
    else:
        push_gerekli = True
    if not h["eposta"]:
        nedenler.append("e-posta kapalı")
    elif "eposta" in islenmis:
        nedenler.append("e-posta bugün gönderildi")
    else:  # Resend ile gönderildiği için kullanıcının Gmail ayarı olmasa da gider
        eposta_gerekli = True
    if not (push_gerekli or eposta_gerekli):
        sonuc["neden"] = "; ".join(nedenler)
        return sonuc

    try:
        bugun_taramasi(db, k, tarih)
    except Exception as e:  # tarama düşerse hatırlatma yine gider
        db.rollback()
        nedenler.append(f"tarama yapılamadı ({e.__class__.__name__})")
    bulunanlar = db.scalars(select(Madde).where(  # kapalı kaynağın satırları özete ve e-postaya girmez
        Madde.user_id == k.id, Madde.tur == "bulunan", Madde.tarih == tarih, Madde.gizli.is_(False),
        Madde.kaynak.in_(acik_bulunan_kaynaklari(a)),
    ).order_by(Madde.sira, Madde.id)).all()
    ozet = hatirlatma_ozeti(bulunanlar)

    # Kanallar bağımsız: biri düşerse diğeri yine gider. Hata da kaydedilir; aynı gün yeniden denenmez.
    if push_gerekli:
        kayit = kanal_talep_et(db, k.id, tarih, "push")
        if kayit is None:
            nedenler.append("push bugün gönderildi")
        else:
            try:
                basarili, toplam, ayrinti = push_cihazlarina_gonder(db, k.id, {"baslik": "Günlük Rapor", "govde": ozet, "url": app_url()})
                sonuc["push"] = f"{basarili}/{toplam}"
                if basarili:
                    kayit.durum = "gonderildi"
                else:
                    kanal_hatasi_yaz(kayit, k.id, "push", "; ".join(
                        c["mesaj"] for c in ayrinti if c["durum"] != "ok") or "gönderilebilen cihaz yok")
            except Exception as e:
                db.rollback()
                sonuc["push"] = "hata"
                neden = f"beklenmeyen hata ({e.__class__.__name__}: {e})"
                nedenler.append(f"push: {neden}")
                kanal_hatasi_yaz(kayit, k.id, "push", neden)
            db.commit()

    if eposta_gerekli:
        kayit = kanal_talep_et(db, k.id, tarih, "eposta")
        if kayit is None:
            nedenler.append("e-posta bugün gönderildi")
        else:
            try:
                hata = servisler.eposta_gonder(
                    h["adres"] or k.eposta,
                    f"Günlük rapor hatırlatması – {tarih.strftime('%d.%m.%Y')}",
                    hatirlatma_epostasi(ozet, bulunanlar, tarih),
                    yanit_adresi=k.eposta,
                    gmail_kullanici=ayarlar["gmail_kullanici"], gmail_sifre=ayarlar["gmail_sifre"],
                )
            except Exception as e:
                hata = f"E-posta gönderilemedi ({e.__class__.__name__})"
            sonuc["eposta"] = "hata" if hata else "gönderildi"
            if hata:
                nedenler.append(hata)
                kanal_hatasi_yaz(kayit, k.id, "eposta", hata)
            else:
                kayit.durum = "gonderildi"
            db.commit()
    sonuc["neden"] = "; ".join(nedenler)
    return sonuc


@router.post("/hatirlat/eposta-dene")
def hatirlatma_eposta_dene(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    """Hatırlatma adresine gerçek bir test maili gönderir."""
    a = ayar_satiri(db, kullanici)
    ayarlar = cozulmus_ayarlar(a)
    hedef = hatirlatma_ayari(a)["adres"] or kullanici.eposta
    metin = "\n".join([
        f"Bu bir test e-postasıdır; hatırlatmalar da {servisler.gonderen_adresi()} adresinden böyle gelir.",
        "", app_url(),
    ])
    try:
        hata = servisler.eposta_gonder(
            hedef, "Günlük rapor — e-posta testi", metin, yanit_adresi=kullanici.eposta,
            gmail_kullanici=ayarlar["gmail_kullanici"], gmail_sifre=ayarlar["gmail_sifre"])
    except Exception as e:
        hata = f"E-posta gönderilemedi ({e.__class__.__name__}: {e})"
    if hata:
        log.warning("hatirlat eposta testi user=%s neden=%s", kullanici.id, hata[:200])
        return {"ok": False, "neden": hata}
    log.info("hatirlat eposta testi user=%s neden=gönderildi", kullanici.id)
    return {"ok": True}


@router.post("/hatirlat")
def hatirlat(request: Request, token: str = "", db: Session = Depends(oturum)) -> dict:
    """Oturumsuz; cron-job.org çağırır. Kullanıcılar id sırasıyla, süre sınırı aşılırsa kalanlar sonraki ping'e."""
    global son_hatirlat_ping
    if not cron_tokeni_dogru(request, token):
        raise HTTPException(status_code=401, detail="Geçersiz token")
    an = istanbul_simdi()
    son_hatirlat_ping = an.isoformat(timespec="seconds")
    if not _hatirlat_kilidi.acquire(blocking=False):
        return {"zaman": son_hatirlat_ping, "mesgul": True, "kullanicilar": [], "kalan": 0}
    try:
        baslangic = saat_.monotonic()
        idler = list(db.scalars(select(Kullanici.id).where(Kullanici.aktif.is_(True)).order_by(Kullanici.id)))
        sonuclar = []
        for sira, uid in enumerate(idler):
            if saat_.monotonic() - baslangic >= SURE_SINIRI:
                return {"zaman": son_hatirlat_ping, "kullanicilar": sonuclar, "kalan": len(idler) - sira}
            try:
                sonuclar.append(kullaniciya_hatirlat(db, db.get(Kullanici, uid), istanbul_simdi()))
            except Exception as e:
                db.rollback()
                sonuclar.append({"user_id": uid, "push": "hata", "eposta": "hata", "neden": f"beklenmeyen hata ({e.__class__.__name__})"})
        return {"zaman": son_hatirlat_ping, "kullanicilar": sonuclar, "kalan": 0}
    finally:
        _hatirlat_kilidi.release()
