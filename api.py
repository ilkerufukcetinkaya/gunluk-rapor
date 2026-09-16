"""JSON uçları. Kullanıcı her zaman oturumdan gelir; tüm sorgular user_id ile süzülür."""
from __future__ import annotations

import hmac
import os
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
    GunlukIfade, HatirlatmaGonderimi, Kullanici, KullaniciAyari, Madde, PushAbonelik, Rapor, oturum, simdi,
)

router = APIRouter(prefix="/api")

REPO_BICIMI = re.compile(r"^[\w.-]+/[\w.-]+$")
EPOSTA_BICIMI = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SAAT_BICIMI = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
VARSAYILAN_SAAT = time(17, 0)
VARSAYILAN_GUNLER = "1,2,3,4,5"


def bugun() -> date:
    return servisler.istanbul_bugun()


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
        "gmail_kullanici": a.gmail_kullanici or "",
        "gmail_sifre_kayitli": bool(a.gmail_sifre_enc),
        "github_token_kayitli": bool(a.github_token_enc),
        "github_repo": a.github_repo or "",
        "proje_adi": a.proje_adi or "",
        "patron_telefon": a.patron_telefon or "",
        "rapor_basligi": a.rapor_basligi or "",
        "alan_sozlugu": servisler.KURUMLAR if a.alan_sozlugu is None else a.alan_sozlugu,
    }


def cozulmus_ayarlar(a: KullaniciAyari) -> dict:
    return {
        "gmail_kullanici": a.gmail_kullanici or "",
        "gmail_sifre": guvenlik.coz(a.gmail_sifre_enc),
        "github_token": guvenlik.coz(a.github_token_enc),
        "github_repo": a.github_repo or "",
        "proje_adi": a.proje_adi or "",
        "alan_sozlugu": servisler.KURUMLAR if a.alan_sozlugu is None else a.alan_sozlugu,
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
    return {**m.sozluk(), "gunun_ifadesi": ifade, "rapor_metni": madde_rapor_metni(m, ifade, tarih)}


def zaman_iso(z: datetime | None) -> str | None:
    if z is None:
        return None
    return (z if z.tzinfo else z.replace(tzinfo=timezone.utc)).isoformat()


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


# ---------------------------------------------------------------- durum

@router.get("/durum")
def durum(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    tarih = bugun()
    yapilanlari_bol(db, kullanici, tarih)
    maddeler = db.scalars(
        select(Madde)
        .where(Madde.user_id == kullanici.id, or_(Madde.tur.in_(("surekli", "devam")), Madde.tarih == tarih))
        .order_by(Madde.tur, Madde.sira, Madde.id)
    ).all()
    ifadeler = bugunku_ifadeler(db, kullanici.id, tarih)
    son_kopya = db.scalar(select(Rapor.olusturma).where(
        Rapor.user_id == kullanici.id, Rapor.tur == "gunluk", Rapor.tarih == tarih,
    ))
    return {
        "kullanici": {"ad": kullanici.ad, "rol": kullanici.rol},
        "tarih": tarih.isoformat(),
        "maddeler": [madde_json(m, ifadeler, tarih) for m in maddeler],
        "ayarlar": ayar_ozeti(ayar_satiri(db, kullanici), kullanici),
        "ai_anahtari": bool(ai_anahtari()),
        "son_kopya": zaman_iso(son_kopya),
    }


# ---------------------------------------------------------------- maddeler

class MaddeYeni(BaseModel):
    tur: Literal["surekli", "devam", "bugun"]
    metin: str = ""
    asama: str | None = None
    tikli: bool = True
    kaynak: Literal["elle", "yapilanlar", "yarin"] | None = None


class MaddeGuncelle(BaseModel):
    metin: str | None = None
    asama: str | None = None
    tikli: bool | None = None
    gizli: bool | None = None


class AiSecimi(BaseModel):
    kullan: bool | None = None
    yenile: bool = False


class Siralama(BaseModel):
    tur: Literal["surekli", "devam", "bulunan"]
    idler: list[int]


@router.post("/maddeler", status_code=201)
def madde_ekle(govde: MaddeYeni, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    tarih = bugun()
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


@router.patch("/maddeler/{madde_id}")
def madde_guncelle(
    madde_id: int, govde: MaddeGuncelle, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)
) -> dict:
    madde = kullanici_maddesi(db, kullanici, madde_id)
    tarih = bugun()
    serbest_metin = madde.tur == "bugun" and madde.kaynak in ("yarin", "yapilanlar")
    for alan, deger in govde.model_dump(exclude_unset=True).items():
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
    madde_id: int, govde: AiSecimi, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)
) -> dict:
    madde = kullanici_maddesi(db, kullanici, madde_id)
    tarih = bugun()
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
            hatalar = duzeltmeyi_uygula(db, kullanici, [madde], tarih, anahtar)["hatalar"]
    elif govde.kullan is not None:
        madde.ai_kullan = govde.kullan
        db.commit()
    return {**madde_json(madde, bugunku_ifadeler(db, kullanici.id, tarih, [madde.id]), tarih), "hatalar": hatalar}


@router.delete("/maddeler/{madde_id}")
def madde_sil(madde_id: int, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    db.delete(kullanici_maddesi(db, kullanici, madde_id))
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


def bugun_taramasi(db: Session, kullanici: Kullanici, tarih: date, yenile: bool = False) -> dict:
    """Kullanıcı başına günde bir tarama (kilit + önbellek); bulunanları maddelere yazar, önbellek özetini döner."""
    anahtar = (kullanici.id, tarih.isoformat())
    with _kullanici_kilidi(kullanici.id):
        if yenile or anahtar not in _onbellek:
            kayitli = set(db.scalars(select(Madde.kaynak_id).where(
                Madde.user_id == kullanici.id, Madde.tur == "bulunan", Madde.tarih == tarih,
            )))
            sonuc = servisler.raporu_uret(
                cozulmus_ayarlar(ayar_satiri(db, kullanici)), os.environ.get("ANTHROPIC_API_KEY", ""), kayitli,
            )
            sira = sonraki_sira(db, kullanici, "bulunan", tarih)
            for m in sonuc["eposta"] + sonuc["medusa"]:
                if m["id"] in kayitli:
                    continue
                db.add(Madde(
                    user_id=kullanici.id, tur="bulunan", metin=m["metin"], tarih=tarih,
                    kaynak=m["kaynak"], kaynak_id=m["id"], tikli=True, sira=sira,
                    metin_ai=m.get("metin_ai"), ai_tarih=tarih if m.get("metin_ai") else None,
                ))
                kayitli.add(m["id"])
                sira += 1
            db.commit()
            for eski in [k for k in _onbellek if k[0] == kullanici.id and k != anahtar]:
                del _onbellek[eski]
            _onbellek[anahtar] = {
                "hatalar": sonuc["hatalar"],
                "sayim": {"eposta": len(sonuc["eposta"]), "medusa": len(sonuc["medusa"])},
            }
        return _onbellek[anahtar]


@router.get("/bugun")
def bugun_bulunanlar(
    yenile: int = 0, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)
) -> dict:
    tarih = bugun()
    onbellek = bugun_taramasi(db, kullanici, tarih, bool(yenile))
    bulunanlar = db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, Madde.tur == "bulunan", Madde.tarih == tarih,
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
    paket = []
    for m in maddeler:
        if m.tur == "surekli":
            secilir = m.id not in ifadeler
        elif m.tur == "bugun":
            secilir = m.kaynak == "elle" and not m.kullanici_duzenledi and not m.metin_ai
        elif m.tur == "bulunan":
            secilir = not m.gizli and not m.kullanici_duzenledi and not m.metin_ai
        else:  # devam
            secilir = not m.kullanici_duzenledi and not m.metin_ai
        if secilir:
            paket.append(m)
    return paket


def duzeltmeyi_uygula(db: Session, kullanici: Kullanici, paket: list[Madde], tarih: date, anahtar: str) -> dict:
    """Tek Claude çağrısı; hata olursa hiçbir maddeye yazılmaz."""
    if not paket:
        return {"duzeltilen": 0, "gonderilen": 0, "hatalar": []}
    girdiler = []
    for m in paket:
        girdi = {"id": m.id, "tur": m.tur, "metin": m.metin}
        if m.tur == "devam" and m.asama:
            girdi["asama"] = m.asama
        girdiler.append(girdi)
    son_raporlar = list(db.scalars(select(Rapor.metin).where(
        Rapor.user_id == kullanici.id, Rapor.tur == "gunluk", Rapor.tarih < tarih,
    ).order_by(Rapor.tarih.desc()).limit(3)))
    try:
        sonuc = servisler.claude_duzelt(
            girdiler, son_raporlar, anahtar, ayar_satiri(db, kullanici).proje_adi or "",
        )
    except servisler.ClaudeHatasi as e:
        return {"duzeltilen": 0, "gonderilen": len(paket), "hatalar": [f"Claude: {e}; ham metin kullanılıyor"]}
    except Exception as e:
        return {"duzeltilen": 0, "gonderilen": len(paket), "hatalar": [f"Claude: beklenmeyen hata ({e.__class__.__name__}); ham metin kullanılıyor"]}
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
def duzelt(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    anahtar = ai_anahtari()
    if not anahtar:
        return {"atlandi": "anahtar yok", "duzeltilen": 0, "gonderilen": 0, "hatalar": []}
    tarih = bugun()
    with _kullanici_kilidi(kullanici.id, "duzelt"):
        paket = duzeltme_paketi(db, kullanici, tarih)
        sonuc = duzeltmeyi_uygula(db, kullanici, paket, tarih, anahtar)
    ifadeler = bugunku_ifadeler(db, kullanici.id, tarih, [m.id for m in paket])
    return {**sonuc, "maddeler": [madde_json(m, ifadeler, tarih) for m in paket]}


# ---------------------------------------------------------------- rapor geçmişi

class RaporYeni(BaseModel):
    metin: str
    tur: Literal["gunluk", "haftalik"] = "gunluk"
    hafta_baslangic: date | None = None


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
def rapor_kaydet(govde: RaporYeni, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    metin = govde.metin.strip()
    if not metin:
        raise HTTPException(status_code=422, detail="Rapor metni boş olamaz")
    if govde.tur == "haftalik":
        hafta = pazartesi_mi(govde.hafta_baslangic)
        tarih = hafta
    else:
        hafta, tarih = None, bugun()
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
    try:
        metin = servisler.claude_haftalik(
            [(r.tarih, r.metin) for r in raporlar], servisler.hafta_basligi(pazartesi, bitis), anahtar,
        )
    except servisler.ClaudeHatasi as e:
        raise HTTPException(status_code=502, detail=f"Claude: {e}") from e
    return {"metin": metin, "hafta_baslangic": pazartesi.isoformat(), "rapor_sayisi": len(raporlar)}


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
    for alan in ("hatirlatma_push", "hatirlatma_eposta"):
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
def baglantiyi_test_et(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    ayarlar = cozulmus_ayarlar(ayar_satiri(db, kullanici))
    parcalar, gmail_ok, github_ok = [], False, False

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


def kullaniciya_hatirlat(db: Session, k: Kullanici, an: datetime) -> dict:
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
    gmail_var = bool(ayarlar["gmail_kullanici"] and ayarlar["gmail_sifre"])
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
    elif not gmail_var:
        nedenler.append("Gmail ayarı yok")
    else:
        eposta_gerekli = True
    if not (push_gerekli or eposta_gerekli):
        sonuc["neden"] = "; ".join(nedenler)
        return sonuc

    try:
        bugun_taramasi(db, k, tarih)
    except Exception as e:  # tarama düşerse hatırlatma yine gider
        db.rollback()
        nedenler.append(f"tarama yapılamadı ({e.__class__.__name__})")
    bulunanlar = db.scalars(select(Madde).where(
        Madde.user_id == k.id, Madde.tur == "bulunan", Madde.tarih == tarih, Madde.gizli.is_(False),
    ).order_by(Madde.sira, Madde.id)).all()
    ozet = hatirlatma_ozeti(bulunanlar)

    # Kanallar bağımsız: biri düşerse diğeri yine gider. Hata da kaydedilir; aynı gün yeniden denenmez.
    if push_gerekli:
        kayit = kanal_talep_et(db, k.id, tarih, "push")
        if kayit is None:
            nedenler.append("push bugün gönderildi")
        else:
            try:
                basarili, toplam, _ = push_cihazlarina_gonder(db, k.id, {"baslik": "Günlük Rapor", "govde": ozet, "url": app_url()})
                sonuc["push"] = f"{basarili}/{toplam}"
                kayit.durum = "gonderildi" if basarili else "hata"
            except Exception as e:
                db.rollback()
                sonuc["push"] = "hata"
                nedenler.append(f"push: beklenmeyen hata ({e.__class__.__name__})")
                kayit.durum = "hata"
            db.commit()

    if eposta_gerekli:
        kayit = kanal_talep_et(db, k.id, tarih, "eposta")
        if kayit is None:
            nedenler.append("e-posta bugün gönderildi")
        else:
            try:
                hata = servisler.eposta_gonder(
                    ayarlar["gmail_kullanici"], ayarlar["gmail_sifre"], h["adres"] or k.eposta,
                    f"Günlük rapor hatırlatması – {tarih.strftime('%d.%m.%Y')}",
                    hatirlatma_epostasi(ozet, bulunanlar, tarih),
                )
            except Exception as e:
                hata = f"E-posta gönderilemedi ({e.__class__.__name__})"
            sonuc["eposta"] = "hata" if hata else "gönderildi"
            if hata:
                nedenler.append(hata)
            kayit.durum = "hata" if hata else "gonderildi"
            db.commit()
    sonuc["neden"] = "; ".join(nedenler)
    return sonuc


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
