"""JSON uçları. Kullanıcı her zaman oturumdan gelir; tüm sorgular user_id ile süzülür."""
from __future__ import annotations

import os
import re
import threading
from datetime import date
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

import guvenlik
import servisler
from kimlik import aktif_kullanici
from veritabani import Kullanici, KullaniciAyari, Madde, oturum

router = APIRouter(prefix="/api")

REPO_BICIMI = re.compile(r"^[\w.-]+/[\w.-]+$")


def bugun() -> date:
    return servisler.istanbul_bugun()


# ---------------------------------------------------------------- yardımcılar

def ayar_satiri(db: Session, kullanici: Kullanici) -> KullaniciAyari:
    return db.get(KullaniciAyari, kullanici.id) or KullaniciAyari(user_id=kullanici.id)


def ayar_ozeti(a: KullaniciAyari) -> dict:
    """Şifre ve token hiçbir zaman dönmez; yalnız kayıtlı olup olmadıkları."""
    return {
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


# ---------------------------------------------------------------- durum

@router.get("/durum")
def durum(kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    tarih = bugun()
    maddeler = db.scalars(
        select(Madde)
        .where(Madde.user_id == kullanici.id, or_(Madde.tur.in_(("surekli", "devam")), Madde.tarih == tarih))
        .order_by(Madde.tur, Madde.sira, Madde.id)
    ).all()
    return {
        "kullanici": {"ad": kullanici.ad, "rol": kullanici.rol},
        "tarih": tarih.isoformat(),
        "maddeler": [m.sozluk() for m in maddeler],
        "ayarlar": ayar_ozeti(ayar_satiri(db, kullanici)),
    }


# ---------------------------------------------------------------- maddeler

class MaddeYeni(BaseModel):
    tur: Literal["surekli", "devam", "bugun"]
    metin: str = ""
    asama: str | None = None
    tikli: bool = True
    kaynak: Literal["yapilanlar", "yarin"] | None = None


class MaddeGuncelle(BaseModel):
    metin: str | None = None
    asama: str | None = None
    tikli: bool | None = None
    gizli: bool | None = None


class Siralama(BaseModel):
    tur: Literal["surekli", "devam", "bulunan"]
    idler: list[int]


@router.post("/maddeler", status_code=201)
def madde_ekle(govde: MaddeYeni, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)) -> dict:
    if govde.tur == "bugun":
        # Günün "yapılanlar" ve "yarın" metinleri gün başına tek satırdır; ikinci ekleme mevcut satırı günceller.
        if govde.kaynak is None:
            raise HTTPException(status_code=422, detail="bugün maddesi için kaynak gerekli (yapilanlar | yarin)")
        tarih = bugun()
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
    return madde.sozluk()


@router.patch("/maddeler/{madde_id}")
def madde_guncelle(
    madde_id: int, govde: MaddeGuncelle, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)
) -> dict:
    madde = kullanici_maddesi(db, kullanici, madde_id)
    for alan, deger in govde.model_dump(exclude_unset=True).items():
        if deger is None:
            continue
        if alan == "metin" and madde.tur != "bugun":
            deger = deger.strip()
            if not deger:
                raise HTTPException(status_code=422, detail="Metin boş olamaz")
        setattr(madde, alan, deger)
    db.commit()
    return madde.sozluk()


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
_kilitler: dict[int, threading.Lock] = {}
_kilitler_kilidi = threading.Lock()


def _kullanici_kilidi(user_id: int) -> threading.Lock:
    with _kilitler_kilidi:
        return _kilitler.setdefault(user_id, threading.Lock())


def onbellegi_temizle() -> None:
    _onbellek.clear()


@router.get("/bugun")
def bugun_bulunanlar(
    yenile: int = 0, kullanici: Kullanici = Depends(aktif_kullanici), db: Session = Depends(oturum)
) -> dict:
    tarih = bugun()
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
        onbellek = _onbellek[anahtar]
    bulunanlar = db.scalars(select(Madde).where(
        Madde.user_id == kullanici.id, Madde.tur == "bulunan", Madde.tarih == tarih,
    ).order_by(Madde.sira, Madde.id)).all()
    return {"tarih": tarih.isoformat(), "bulunan": [m.sozluk() for m in bulunanlar], **onbellek}


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
    return ayar_ozeti(ayar_satiri(db, kullanici))


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
    for alan, deger in veri.items():
        deger = (deger or "").strip() or None
        if alan == "github_repo" and deger and not REPO_BICIMI.match(deger):
            raise HTTPException(status_code=422, detail="GitHub repo 'kullanici/repo' biçiminde olmalı")
        if alan == "gmail_kullanici" and deger:
            deger = deger.lower()
        setattr(a, alan, deger)
    db.commit()
    return ayar_ozeti(a)


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
        mevcut = set(db.scalars(select(Madde.kaynak_id).where(
            Madde.user_id == kullanici.id, Madde.tur == "bugun", Madde.tarih == tarih,
        )))
        for kaynak, anahtar in (("yapilanlar", "done"), ("yarin", "plan")):
            metin = gunluk.get(anahtar)
            if isinstance(metin, str) and metin.strip() and kaynak not in mevcut:  # mevcut satıra dokunulmaz
                sayim["bugun"] += 1
                db.add(Madde(user_id=kullanici.id, tur="bugun", tarih=tarih, kaynak=kaynak, kaynak_id=kaynak, metin=metin))

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
