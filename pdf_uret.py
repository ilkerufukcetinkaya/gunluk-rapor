"""A1v2: aylık/yıllık özetin sunucuda üretilen PDF'i (ReportLab, saf Python).

Ölçüler tasarim/a1/A1-PDF-Kapak ve A1-PDF-Ayrinti artboard'larının CSS'inden gelir: artboard 794×1123 px (A4 @96 dpi),
bütün yerleşim px cinsinden yapılır, çizimde 0,75 ile pt'ye çevrilir. Yazı tipi IBM Plex Sans (static/fonts, OFL).
Performans Özeti ('basari'): sayfa 1 kapak (bant, göstergeler, öne çıkanlar, işin dağılımı, durum), sayfa 2+ ayrıntılar
(alanlar > temalar, sürekli işler, sayılarla). İçerik taşarsa bloklar sonraki sayfaya akar; bir tema bölünmez. Yönetici
Özeti ('patron', A1v3-PatronPDF): bant + göstergeler + ikonlu bölümler + tamamlanan / devam eden kutuları, tek sayfa."""
from __future__ import annotations

import io
import re
from datetime import date
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

PX = 0.75  # 1 px (96 dpi) = 0,75 pt
SAYFA_G, SAYFA_Y = 794, 1123  # px
KENAR = 56  # yan kenar (px); artboard'daki padding
ICERIK_G = SAYFA_G - 2 * KENAR
ALT_SINIR = 1052  # içerik bu çizgiyi geçmez (alt bilgi çizgisi 1072 civarı)

KOYU, MAVI, YESIL, TURUNCU, CIZGI = "#12151a", "#2d6cdf", "#1f8a4c", "#b7791f", "#eceef1"
GRI, GRI_2, GRI_3, METIN, METIN_2 = "#8a919c", "#6b7280", "#9aa3b0", "#262b33", "#353b44"
# A1v3: "İşin dağılımı" çubukları ekrandaki "İş nereden geldi" donut'ıyla aynı renkte (alanın baskın kaynak grubu)
GRUP_RENKLERI = {"uygulama": "#2d6cdf", "eposta": "#d9433d", "elle": "#12151a", "dosya": "#c98a12", "yesil": "#22b866"}
TR_AYLAR = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık")

YAZI_KLASORU = Path(__file__).resolve().parent / "static" / "fonts"
YAZILAR = {"R": "IBMPlexSans-Regular", "M": "IBMPlexSans-Medium", "S": "IBMPlexSans-SemiBold", "B": "IBMPlexSans-Bold"}
# IBM Plex Sans: ascender 1025, descender 275 (em 1000); CSS'teki "normal" satır yüksekliği 1,3.
YUKARI, ASAGI = 1.025, 0.275
_kayitli = False


def yazilari_kaydet() -> None:
    global _kayitli
    if _kayitli:
        return
    for dosya in YAZILAR.values():
        pdfmetrics.registerFont(TTFont(dosya, str(YAZI_KLASORU / f"{dosya}.ttf")))
    _kayitli = True


def buyuk(metin: str) -> str:
    """Türkçe büyük harf: i → İ, ı → I."""
    return (metin or "").replace("i", "İ").replace("ı", "I").upper()


def tarih_yazisi(g: date) -> str:
    return f"{g.day} {TR_AYLAR[g.month - 1]} {g.year}"


def genislik(metin: str, yazi: str, boyut: float, aralik: float = 0.0) -> float:
    return pdfmetrics.stringWidth(metin, YAZILAR[yazi], boyut) + aralik * boyut * len(metin)


# ---------------------------------------------------------------- satır sarma (karışık yazı tipli metin)

class Parca:
    __slots__ = ("metin", "yazi", "renk")

    def __init__(self, metin: str, yazi: str = "R", renk: str = METIN):
        self.metin, self.yazi, self.renk = metin, yazi, renk


def sar(parcalar: list[Parca], boyut: float, en: float) -> list[list[Parca]]:
    """Kelimelerden açgözlü satır doldurma; tek kelime satırdan uzunsa harf harf bölünür."""
    kelimeler: list[Parca] = []
    for p in parcalar:
        for k in re.findall(r"\S+\s*", p.metin):
            kelimeler.append(Parca(k, p.yazi, p.renk))
    satirlar: list[list[Parca]] = []
    satir: list[Parca] = []
    dolu = 0.0
    for k in kelimeler:
        w = genislik(k.metin.rstrip(), k.yazi, boyut)
        tam = genislik(k.metin, k.yazi, boyut)
        if satir and dolu + w > en:
            satirlar.append(satir)
            satir, dolu = [], 0.0
        if not satir and w > en:  # bölünemeyen uzun kelime
            parca = ""
            for h in k.metin:
                if genislik(parca + h, k.yazi, boyut) > en and parca:
                    satirlar.append([Parca(parca, k.yazi, k.renk)])
                    parca = ""
                parca += h
            satir, dolu = [Parca(parca, k.yazi, k.renk)], genislik(parca, k.yazi, boyut)
            continue
        satir.append(k)
        dolu += tam
    if satir:
        satirlar.append(satir)
    return satirlar or [[]]


# ---------------------------------------------------------------- çizim

class Tuval:
    """px koordinatlı (sol üst köşe) ince ReportLab sarmalayıcısı."""

    def __init__(self, c: canvas.Canvas):
        self.c = c
        self.yukseklik = A4[1]

    def _y(self, y: float) -> float:
        return self.yukseklik - y * PX

    def renk(self, renk: str):
        self.c.setFillColor(renk)
        self.c.setStrokeColor(renk)

    def dikdortgen(self, x, y, g, y_, renk, yaricap=0.0, cizgi: str | None = None, kalinlik=1.0):
        self.c.saveState()
        if cizgi:
            self.c.setStrokeColor(cizgi)
            self.c.setLineWidth(kalinlik * PX)
        if renk:
            self.c.setFillColor(renk)
        args = (x * PX, self._y(y + y_), g * PX, y_ * PX)
        if yaricap:
            self.c.roundRect(*args, min(yaricap, g / 2, y_ / 2) * PX, stroke=1 if cizgi else 0, fill=1 if renk else 0)
        else:
            self.c.rect(*args, stroke=1 if cizgi else 0, fill=1 if renk else 0)
        self.c.restoreState()

    def cizgi(self, x1, y, x2, renk, kalinlik=1.0):
        # CSS border-top: çizgi kutunun üstünden aşağı doğru kalınlık kadar yer kaplar
        self.dikdortgen(x1, y, x2 - x1, kalinlik, renk)

    def yaz(self, x, taban, metin, yazi="R", boyut=12.0, renk=METIN, aralik=0.0, sag=False):
        if not metin:
            return
        if sag:
            x -= genislik(metin, yazi, boyut, aralik)
        t = self.c.beginText()
        t.setTextOrigin(x * PX, self._y(taban))
        t.setFont(YAZILAR[yazi], boyut * PX)
        t.setCharSpace(aralik * boyut * PX)  # Tc metin durumunda kalıcıdır: her yazıda yeniden verilir
        t.setFillColor(renk)
        t.textOut(metin)
        self.c.drawText(t)

    def satir_yaz(self, x, taban, satir: list[Parca], boyut):
        # aynı yazı tipli ardışık kelimeler tek çizim: PDF'ten metin çıkarımı kelime kelime bölünmez
        gruplar: list[Parca] = []
        for p in satir:
            if gruplar and (gruplar[-1].yazi, gruplar[-1].renk) == (p.yazi, p.renk):
                gruplar[-1] = Parca(gruplar[-1].metin + p.metin, p.yazi, p.renk)
            else:
                gruplar.append(p)
        for p in gruplar:
            self.yaz(x, taban, p.metin, p.yazi, boyut, p.renk)
            x += genislik(p.metin, p.yazi, boyut)


def taban(ust: float, satir_y: float, boyut: float) -> float:
    """CSS satır kutusunda metnin taban çizgisi: yarım boşluk + ascender."""
    return ust + (satir_y - (YUKARI + ASAGI) * boyut) / 2 + YUKARI * boyut


class Blok:
    """Akıştaki bölünmez parça: yükseklik, üst boşluk, çizim (x, üst)."""

    def __init__(self, yukseklik: float, ciz, ust_bosluk: float = 0.0, yeni_sayfa: bool = False):
        self.yukseklik, self.ciz, self.ust_bosluk, self.yeni_sayfa = yukseklik, ciz, ust_bosluk, yeni_sayfa


def paragraf(parcalar: list[Parca], boyut: float, en: float, satir_carpani: float) -> tuple[float, callable]:
    satirlar = sar(parcalar, boyut, en)
    sy = boyut * satir_carpani

    def ciz(t: Tuval, x, y):
        for i, s in enumerate(satirlar):
            t.satir_yaz(x, taban(y + i * sy, sy, boyut), s, boyut)
    return len(satirlar) * sy, ciz


def dizil(bloklar: list[Blok], ilk_y: float, devam_y: float = 40.0) -> list[list[tuple[float, Blok]]]:
    """Blokları sayfalara yerleştirir: sığmayan blok sonraki sayfaya geçer (üst boşluğu sayfa başında düşer)."""
    sayfalar: list[list[tuple[float, Blok]]] = [[]]
    y = ilk_y
    for b in bloklar:
        bas = y + (b.ust_bosluk if sayfalar[-1] else 0)
        if b.yeni_sayfa or (sayfalar[-1] and bas + b.yukseklik > ALT_SINIR):
            sayfalar.append([])
            bas = devam_y
        sayfalar[-1].append((bas, b))
        y = bas + b.yukseklik
    return [s for s in sayfalar if s]


# ---------------------------------------------------------------- ortak parçalar

def bant(ad_unvan: str, etiket: str, donem_adi: str, kapsam: str, hazirlanma: date) -> Blok:
    """Koyu kapak bandı (padding 44 56 36): etiket + hazırlanma, dönem 34px, ad · unvan, kapsam."""
    ust_satir = 10 * 1.3
    ad_satirlari = sar([Parca(ad_unvan, "R", "#c9ced6")], 14, ICERIK_G) if ad_unvan else []
    yukseklik = 44 + ust_satir + 18 + 34 * 1.1 + (8 + len(ad_satirlari) * 14 * 1.3 if ad_satirlari else 0) \
        + (4 + 11 * 1.3 if kapsam else 0) + 36

    def ciz(t: Tuval, x, y):
        t.dikdortgen(0, y, SAYFA_G, yukseklik, KOYU)
        yy = y + 44
        t.yaz(KENAR, taban(yy, ust_satir, 10), etiket, "B", 10, GRI_3, aralik=0.12)
        t.yaz(SAYFA_G - KENAR, taban(yy, ust_satir, 10), f"Hazırlanma: {tarih_yazisi(hazirlanma)}", "R", 10, GRI_3, sag=True)
        yy += ust_satir + 18
        t.yaz(KENAR, taban(yy, 34 * 1.1, 34), donem_adi, "B", 34, "#ffffff", aralik=-0.03)
        yy += 34 * 1.1
        if ad_satirlari:
            yy += 8
            for s in ad_satirlari:
                t.satir_yaz(KENAR, taban(yy, 14 * 1.3, 14), s, 14)
                yy += 14 * 1.3
        if kapsam:
            yy += 4
            t.yaz(KENAR, taban(yy, 11 * 1.3, 11), f"Kapsam: {kapsam}", "R", 11, GRI)
    return Blok(yukseklik, ciz)


def bolum_etiketi(metin: str, cizgi: bool = True) -> tuple[float, callable]:
    """.sec: 2px koyu üst çizgi, 12px iç boşluk, 10px büyük harf etiket (harf aralığı .12em)."""
    yukseklik = (2 + 12 if cizgi else 0) + 10 * 1.3

    def ciz(t: Tuval, x, y):
        yy = y
        if cizgi:
            t.cizgi(KENAR, y, SAYFA_G - KENAR, KOYU, 2)
            yy += 14
        t.yaz(KENAR, taban(yy, 13, 10), buyuk(metin), "B", 10, GRI, aralik=0.12)
    return yukseklik, ciz


def birlesik(*parcalar: tuple[float, callable], araliklar: tuple[float, ...] = ()) -> tuple[float, callable]:
    """Parçaları alt alta tek bloğa toplar; araliklar[i]: i. parçadan önceki boşluk."""
    ofsetler, y = [], 0.0
    for i, (h, _) in enumerate(parcalar):
        y += araliklar[i] if i < len(araliklar) else 0
        ofsetler.append(y)
        y += h

    def ciz(t: Tuval, x, yy):
        for (h, c), o in zip(parcalar, ofsetler):
            c(t, x, yy + o)
    return y, ciz


def iki_sutun(sol_baslik: str, sol: list[str], sag_baslik: str, sag: list[str]) -> tuple[float, callable]:
    """Durum: Tamamlanan (yeşil) / Devam eden (turuncu); 11.5px, satır 1.6, sütun arası 16."""
    en = (ICERIK_G - 16) / 2
    sy = 11.5 * 1.6

    def satirlar(liste):
        return [s for m in liste for s in sar([Parca(m, "R", METIN)], 11.5, en)]
    sol_s, sag_s = satirlar(sol or ["—"]), satirlar(sag or ["—"])
    yukseklik = sy * (1 + max(len(sol_s), len(sag_s)))

    def ciz(t: Tuval, x, y):
        for sx, baslik, renk, sat in ((KENAR, sol_baslik, YESIL, sol_s), (KENAR + en + 16, sag_baslik, TURUNCU, sag_s)):
            t.yaz(sx, taban(y, sy, 11.5), baslik, "S", 11.5, renk)
            for i, s in enumerate(sat):
                t.satir_yaz(sx, taban(y + (i + 1) * sy, sy, 11.5), s, 11.5)
    return yukseklik, ciz


def alt_bilgi(t: Tuval, sol: str, n: int, toplam: int):
    """.foot: sol/sağ 56, alt 28, üst çizgi #eceef1, 10px iç boşluk, 9.5px gri."""
    sy = 9.5 * 1.3
    ust = SAYFA_Y - 28 - sy - 10
    t.cizgi(KENAR, ust, SAYFA_G - KENAR, CIZGI, 1)
    t.yaz(KENAR, taban(ust + 10, sy, 9.5), sol, "R", 9.5, GRI)
    t.yaz(SAYFA_G - KENAR, taban(ust + 10, sy, 9.5), f"{n} / {toplam}", "R", 9.5, GRI, sag=True)


def belgeyi_ciz(sayfalar: list[list[tuple[float, Blok]]], alt_yazi: str, baslik: str, yazar: str) -> bytes:
    tampon = io.BytesIO()
    c = canvas.Canvas(tampon, pagesize=A4, pageCompression=1)
    c.setTitle(baslik)
    c.setAuthor(yazar)
    c.setCreator("Günlük Rapor")
    t = Tuval(c)
    for n, sayfa in enumerate(sayfalar, start=1):
        for y, b in sayfa:
            b.ciz(t, KENAR, y)
        alt_bilgi(t, alt_yazi, n, len(sayfalar))
        c.showPage()
    c.save()
    return tampon.getvalue()


# ---------------------------------------------------------------- Performans Özeti

def gosterge_kutulari(kutular: list[tuple[str, str]]) -> tuple[float, callable]:
    """4 gösterge: kenarlık #eceef1, köşe 12, iç boşluk 14/14/12; değer 28px (satır 1), açıklama 10.5px satır 1.35."""
    ara = 10
    en = (ICERIK_G - ara * (len(kutular) - 1)) / len(kutular)
    ic = en - 28
    aciklamalar = [sar([Parca(a, "R", GRI_2)], 10.5, ic) for _, a in kutular]
    yukseklik = 14 + 28 + 6 + max(len(a) for a in aciklamalar) * 10.5 * 1.35 + 12 + 2

    def ciz(t: Tuval, x, y):
        for i, ((deger, _), satirlar) in enumerate(zip(kutular, aciklamalar)):
            kx = KENAR + i * (en + ara)
            t.dikdortgen(kx + 0.5, y + 0.5, en - 1, yukseklik - 1, None, 12, cizgi=CIZGI)
            t.yaz(kx + 15, taban(y + 15, 28, 28), deger, "B", 28, KOYU, aralik=-0.03)
            for j, s in enumerate(satirlar):
                t.satir_yaz(kx + 15, taban(y + 15 + 28 + 6 + j * 10.5 * 1.35, 10.5 * 1.35, 10.5), s, 10.5)
    return yukseklik, ciz


def one_cikan(n: int, baslik: str, aciklama: str, ilk: bool) -> tuple[float, callable]:
    """.hl: numara kutucuğu 22×22 (#e9eefb, #1f54b8), 12px aralık; metin 12px satır 1.5, başlık kalın."""
    baslik = baslik.strip()
    if baslik and baslik[-1] not in ".!?…:":
        baslik += "."
    parcalar = [Parca(baslik + (" " if aciklama else ""), "B", KOYU)] if baslik else []
    parcalar += [Parca(aciklama, "R", METIN)] if aciklama else []
    en = ICERIK_G - 22 - 12
    h, metin_ciz = paragraf(parcalar, 12, en, 1.5)
    ust = 0 if ilk else 11
    yukseklik = ust + max(h, 22) + 11

    def ciz(t: Tuval, x, y):
        if not ilk:
            t.cizgi(KENAR, y, SAYFA_G - KENAR, CIZGI, 1)
        yy = y + ust
        t.dikdortgen(KENAR, yy, 22, 22, "#e9eefb", 7)
        t.yaz(KENAR + 11 - genislik(str(n), "B", 11) / 2, taban(yy, 22, 11), str(n), "B", 11, "#1f54b8")
        metin_ciz(t, KENAR + 34, yy)
    return yukseklik, ciz


def dagilim(alanlar: list[dict]) -> tuple[float, callable]:
    """.area: etiket 170px, çubuk 10px (köşe 5, en çok 250px), not gri; 11px, satır arası 8. Renk alanın "grup"undan
    (baskın kaynak; bilinmiyorsa gri)."""
    en_cok = max([a.get("madde_sayisi", 0) for a in alanlar] + [1])
    satirlar = []
    for a in alanlar:
        n, k = a.get("madde_sayisi", 0), len(a.get("temalar") or [])
        not_ = f"{n} madde" + (f" · {k} iş başlığı" if k > 1 else "")
        etiket = sar([Parca(a["ad"], "R", METIN)], 11, 170)
        satirlar.append((etiket, max(3.0, 250 * n / en_cok) if n else 0, GRUP_RENKLERI.get(a.get("grup"), GRI), not_))
    sy = 11 * 1.3
    yukseklik = sum(8 + max(len(e) * sy, 10) for e, *_ in satirlar)

    def ciz(t: Tuval, x, y):
        yy = y
        for etiket, w, renk, not_ in satirlar:
            yy += 8
            h = max(len(etiket) * sy, 10)
            for j, s in enumerate(etiket):
                t.satir_yaz(KENAR, taban(yy + j * sy, sy, 11), s, 11)
            orta = yy + h / 2
            if w:
                t.dikdortgen(KENAR + 180, orta - 5, w, 10, renk, 5)
            t.yaz(KENAR + 180 + w + (10 if w else 0), taban(orta - sy / 2, sy, 11), not_, "R", 11, GRI_2)
            yy += h
    return yukseklik, ciz


def rozet_genisligi(metin: str) -> float:
    return genislik(metin, "S", 9.5) + 14


def tema(t_: dict) -> tuple[float, callable]:
    """.theme (üst boşluk 12): başlık 12px kalın + "N madde" rozeti; özet 11.2px satır 1.5; gri etiketler."""
    baslik = sar([Parca(t_.get("ad") or "", "B", KOYU)], 12, ICERIK_G - rozet_genisligi("999 madde") - 8)
    rozet = f"{t_.get('madde_sayisi', 0)} madde"
    ozet_h, ozet_ciz = paragraf([Parca(t_.get("ozet") or "", "R", METIN_2)], 11.2, ICERIK_G, 1.5) if t_.get("ozet") else (0, None)
    # etiketler: satır içi kutular, sağ ve üst boşluk 4
    etiketler, sat, dolu = [], [], 0.0
    for e in t_.get("etiketler") or []:
        w = min(genislik(e, "S", 9.5) + 14, ICERIK_G)
        if sat and dolu + w > ICERIK_G:
            etiketler.append(sat)
            sat, dolu = [], 0.0
        sat.append((e, w))
        dolu += w + 4
    if sat:
        etiketler.append(sat)
    kutu_h = 9.5 * 1.3 + 4
    bs = 12 * 1.3
    yukseklik = len(baslik) * bs + 3 + ozet_h + len(etiketler) * (kutu_h + 4)

    def ciz(t: Tuval, x, y):
        for j, s in enumerate(baslik):
            t.satir_yaz(KENAR, taban(y + j * bs, bs, 12), s, 12)
        son = baslik[-1]
        rx = KENAR + sum(genislik(p.metin, p.yazi, 12) for p in son) + 8
        ry = y + (len(baslik) - 1) * bs + (bs - (9.5 * 1.3 + 2)) / 2
        t.dikdortgen(rx, ry, rozet_genisligi(rozet), 9.5 * 1.3 + 2, "#f1f3f6", 99)
        t.yaz(rx + 7, taban(ry + 1, 9.5 * 1.3, 9.5), rozet, "S", 9.5, GRI_2)
        yy = y + len(baslik) * bs + 3
        if ozet_ciz:
            ozet_ciz(t, KENAR, yy)
            yy += ozet_h
        for sat in etiketler:
            yy += 4
            ex = KENAR
            for e, w in sat:
                t.dikdortgen(ex, yy, w, kutu_h, "#f1f3f6", 6)
                t.yaz(ex + 7, taban(yy + 2, 9.5 * 1.3, 9.5), e, "S", 9.5, "#4b5563")
                ex += w + 4
            yy += kutu_h
    return yukseklik, ciz


def sayilar_tablosu(sayilar: dict, sutun: str) -> tuple[float, callable]:
    """table: başlık 9.5px kalın (aralık .08em) alt çizgi #eceef1; hücre 11px, 7px dikey boşluk, alt çizgi #f3f4f6."""
    satirlar = [("Rapor gönderilen gün / iş günü", f"{sayilar.get('rapor_gunu', 0)} / {sayilar.get('is_gunu', 0)}"),
                ("Raporlanan madde", str(sayilar.get("toplam_madde", 0)))]
    if sayilar.get("eposta"):
        satirlar.append(("Kurumlara giden e-posta", str(sayilar["eposta"])))
    if sayilar.get("uygulama"):
        satirlar.append(("Uygulama çalışması (kayıtlı değişiklik)", str(sayilar["uygulama"])))
    if sayilar.get("toplanti") or sayilar.get("dosya"):
        satirlar.append(("Toplantı · belge", f"{sayilar.get('toplanti', 0)} · {sayilar.get('dosya', 0)}"))
    if sayilar.get("tamamlanan") or sayilar.get("acik"):
        satirlar.append(("Tamamlanan iş · açık kalan", f"{sayilar.get('tamamlanan', 0)} · {sayilar.get('acik', 0)}"))
    bas_h = 6 + 9.5 * 1.3 + 6 + 1
    sat_h = 7 + 11 * 1.3 + 7 + 1
    yukseklik = 6 + bas_h + len(satirlar) * sat_h

    def ciz(t: Tuval, x, y):
        yy = y + 6
        t.yaz(KENAR, taban(yy + 6, 9.5 * 1.3, 9.5), "Gösterge", "B", 9.5, GRI, aralik=0.08)
        t.yaz(SAYFA_G - KENAR, taban(yy + 6, 9.5 * 1.3, 9.5), sutun, "B", 9.5, GRI, aralik=0.08, sag=True)
        yy += bas_h - 1
        t.cizgi(KENAR, yy, SAYFA_G - KENAR, CIZGI, 1)
        yy += 1
        for ad, deger in satirlar:
            t.yaz(KENAR, taban(yy + 7, 11 * 1.3, 11), ad, "R", 11, METIN)
            t.yaz(SAYFA_G - KENAR, taban(yy + 7, 11 * 1.3, 11), deger, "R", 11, METIN, sag=True)
            yy += sat_h - 1
            t.cizgi(KENAR, yy, SAYFA_G - KENAR, "#f3f4f6", 1)
            yy += 1
    return yukseklik, ciz


def _blok(parca: tuple[float, callable], ust_bosluk: float = 0.0, yeni_sayfa: bool = False) -> Blok:
    return Blok(parca[0], parca[1], ust_bosluk, yeni_sayfa)


def basari_bloklari(tur: str, donem_adi: str, yapi: dict, ad_unvan: str, hazirlanma: date) -> list[Blok]:
    s = yapi.get("sayilar") or {}
    donem = "AYIN" if tur == "aylik" else "YILIN"
    bloklar = [bant(ad_unvan, "AYLIK PERFORMANS ÖZETİ" if tur == "aylik" else "YILLIK PERFORMANS ÖZETİ", donem_adi,
                    s.get("kapsam") or "", hazirlanma)]
    kurum = s.get("kurum")
    kutular = [
        (f"%{s['kapsama']}" if s.get("kapsama") is not None else "—",
         f"iş günlerinin raporlandığı oran · {s.get('raporlu_is_gunu', 0)} / {s.get('is_gunu', 0)} gün"),
        (str(s.get("toplam_madde", 0)), "raporlanan iş maddesi"),
        (str(s.get("tamamlanan", 0)), "tamamlanan iş"),
        (str(kurum["sayi"]), f"{kurum['ad']} ile yürütülen yazışma") if kurum else (str(s.get("eposta", 0)), "kurumlara giden e-posta"),
    ]
    bloklar.append(_blok(gosterge_kutulari(kutular), 26))
    one = yapi.get("one_cikanlar") or []
    if one:
        etiket = bolum_etiketi(f"{donem} ÖNE ÇIKANLARI")
        ilk = one_cikan(1, one[0].get("baslik") or "", one[0].get("aciklama") or "", True)
        bloklar.append(_blok(birlesik(etiket, ilk, araliklar=(0, 12)), 26))
        for i, o in enumerate(one[1:], start=2):
            bloklar.append(_blok(one_cikan(i, o.get("baslik") or "", o.get("aciklama") or "", False)))
    alanlar = [a for a in yapi.get("alanlar") or [] if a.get("madde_sayisi")]
    if alanlar:
        bloklar.append(_blok(birlesik(bolum_etiketi("İŞİN DAĞILIMI"), dagilim(alanlar), araliklar=(0, 6)), 22 if one else 26))
    if yapi.get("tamamlanan") or yapi.get("devam_eden"):
        bloklar.append(_blok(birlesik(bolum_etiketi("DURUM"), iki_sutun("Tamamlanan", yapi.get("tamamlanan") or [],
                                                                        "Devam eden", yapi.get("devam_eden") or []),
                                      araliklar=(0, 10)), 22))

    # sayfa 2+: Ayrıntılar
    def ayrinti_basligi(t: Tuval, x, y):
        t.yaz(KENAR, taban(y, 17 * 1.3, 17), "Ayrıntılar", "B", 17, KOYU, aralik=-0.02)
        t.yaz(SAYFA_G - KENAR, taban(y, 17 * 1.3, 17) - 0.5, donem_adi, "R", 10, GRI, sag=True)
    bloklar.append(Blok(17 * 1.3, ayrinti_basligi, 0, yeni_sayfa=True))
    ilk_bolum = True
    for a in yapi.get("alanlar") or []:
        temalar = a.get("temalar") or []
        etiket = bolum_etiketi(f"{a['ad']} · {a.get('madde_sayisi', 0)} madde")
        if not temalar:
            bloklar.append(_blok(etiket, 14 if ilk_bolum else 22))
        else:
            bloklar.append(_blok(birlesik(etiket, tema(temalar[0]), araliklar=(0, 12)), 14 if ilk_bolum else 22))
            for t_ in temalar[1:]:
                bloklar.append(_blok(tema(t_), 12))
        ilk_bolum = False
    if yapi.get("surekli"):
        bloklar.append(_blok(birlesik(bolum_etiketi("SÜREKLİ ÜSTLENİLEN İŞLER"),
                                      paragraf([Parca(yapi["surekli"], "R", METIN_2)], 11.2, ICERIK_G, 1.5),
                                      araliklar=(0, 8)), 14 if ilk_bolum else 22))
        ilk_bolum = False
    sutun = donem_adi.split()[0] if tur == "aylik" else donem_adi
    bloklar.append(_blok(birlesik(bolum_etiketi("SAYILARLA"), sayilar_tablosu(s, sutun)), 14 if ilk_bolum else 22))
    return bloklar


# ---------------------------------------------------------------- Yönetici Özeti (A1v3, tasarim/a1v3/A1v3-PatronPDF)

KIRMIZI, YESIL_2 = "#d9433d", "#22b866"
# bölüm ikonu: (zemin, iç kare); 'tur' api.renk_gruplarini_isle'den
BOLUM_RENKLERI = {"uygulama": ("#e9eefb", MAVI), "yazisma": ("#fdecec", KIRMIZI), "toplanti": ("#e6f6ec", YESIL_2),
                  "diger": ("#f1f3f6", GRI)}
BANT_SONRASI = 32  # px = 24 pt: koyu banttan sonra ilk gösterge kutusuna kadar boşluk


def yonetici_bandi(ad_unvan: str, etiket: str, donem_adi: str, hazirlanma: date) -> Blok:
    """.pg bandı (padding 36 56 30): etiket 11px + sağda tarih 10px; dönem 30px (üst 14); ad · unvan 13px (üst 6)."""
    ust_satir = 11 * 1.3
    ad_satirlari = sar([Parca(ad_unvan, "R", "#c9ced6")], 13, ICERIK_G) if ad_unvan else []
    yukseklik = 36 + ust_satir + 14 + 30 * 1.3 + (6 + len(ad_satirlari) * 13 * 1.3 if ad_satirlari else 0) + 30

    def ciz(t: Tuval, x, y):
        t.dikdortgen(0, y, SAYFA_G, yukseklik, KOYU)
        yy = y + 36
        t.yaz(KENAR, taban(yy, ust_satir, 11), etiket, "B", 11, GRI_3, aralik=0.1)
        t.yaz(SAYFA_G - KENAR, taban(yy, ust_satir, 10), tarih_yazisi(hazirlanma), "R", 10, GRI_3, sag=True)
        yy += ust_satir + 14
        t.yaz(KENAR, taban(yy, 30 * 1.3, 30), donem_adi, "B", 30, "#ffffff", aralik=-0.03)
        yy += 30 * 1.3
        if ad_satirlari:
            yy += 6
            for s_ in ad_satirlari:
                t.satir_yaz(KENAR, taban(yy, 13 * 1.3, 13), s_, 13)
                yy += 13 * 1.3
    return Blok(yukseklik, ciz)


def yonetici_gostergeleri(kutular: list[tuple[str, str, str, str]]) -> tuple[float, callable]:
    """4 kutu (etiket, değer, değer rengi, açıklama): kenarlık #eceef1, köşe 12, iç boşluk 12/14; etiket 9px, değer
    24px (üst 6), açıklama 10px (üst 3)."""
    ara = 10
    en = (ICERIK_G - ara * (len(kutular) - 1)) / len(kutular)
    aciklamalar = [sar([Parca(a, "R", GRI_2)], 10, en - 28) for *_, a in kutular]
    yukseklik = 1 + 12 + 9 * 1.3 + 6 + 24 * 1.3 + 3 + max(len(a) for a in aciklamalar) * 10 * 1.3 + 12 + 1

    def ciz(t: Tuval, x, y):
        for i, ((etiket, deger, renk, _), satirlar) in enumerate(zip(kutular, aciklamalar)):
            kx = KENAR + i * (en + ara)
            t.dikdortgen(kx + 0.5, y + 0.5, en - 1, yukseklik - 1, None, 12, cizgi=CIZGI)
            yy = y + 13
            t.yaz(kx + 15, taban(yy, 9 * 1.3, 9), buyuk(etiket), "B", 9, GRI, aralik=0.1)
            yy += 9 * 1.3 + 6
            t.yaz(kx + 15, taban(yy, 24 * 1.3, 24), deger, "B", 24, renk, aralik=-0.03)
            yy += 24 * 1.3 + 3
            for j, s_ in enumerate(satirlar):
                t.satir_yaz(kx + 15, taban(yy + j * 13, 13, 10), s_, 10)
    return yukseklik, ciz


def _madde_parcalari(m) -> list[Parca]:
    """{vurgu, metin} → kalın vurgu + metin; düz dize (eski kayıt) yalnız metin."""
    if isinstance(m, dict):
        vurgu, metin = (m.get("vurgu") or "").strip(), (m.get("metin") or "").strip()
        bitisik = metin[:1] in (":", ",", ";", ".")  # "Veri güvenliği: …"
        return ([Parca(vurgu + ("" if bitisik else " "), "B", KOYU)] if vurgu else []) + ([Parca(metin, "R", METIN)] if metin else [])
    return [Parca(str(m or ""), "R", METIN)]


def yonetici_basligi(ad: str, sayi: str, tur: str) -> tuple[float, callable]:
    """.sech: 26×26 köşe 8 renkli kare (içte 10×10 köşe 3), 10px ara, ad 15px kalın; sağda 'N madde' 10.5px gri."""
    zemin, ic = BOLUM_RENKLERI.get(tur, BOLUM_RENKLERI["diger"])
    sag_en = genislik(sayi, "R", 10.5) + 12 if sayi else 0
    satirlar = sar([Parca(ad, "B", KOYU)], 15, ICERIK_G - 36 - sag_en)
    sy = 15 * 1.3
    yukseklik = max(26, len(satirlar) * sy)

    def ciz(t: Tuval, x, y):
        t.dikdortgen(KENAR, y + (yukseklik - 26) / 2, 26, 26, zemin, 8)
        t.dikdortgen(KENAR + 8, y + (yukseklik - 26) / 2 + 8, 10, 10, ic, 3)
        ust = y + (yukseklik - len(satirlar) * sy) / 2
        for j, s_ in enumerate(satirlar):
            t.satir_yaz(KENAR + 36, taban(ust + j * sy, sy, 15), s_, 15)
        if sayi:
            t.yaz(SAYFA_G - KENAR, taban(y + (yukseklik - 13.65) / 2, 13.65, 10.5), sayi, "R", 10.5, GRI, sag=True)
    return yukseklik, ciz


def yonetici_satiri(m) -> tuple[float, callable]:
    """.it: iç boşluk 8 üst/alt, sol 36; 5px gri nokta (#b7bec8), 10px ara; 12px satır 1.5; alt çizgi #f3f4f6."""
    girinti = 36 + 5 + 10
    h, metin_ciz = paragraf(_madde_parcalari(m), 12, ICERIK_G - girinti, 1.5)
    yukseklik = 8 + h + 8 + 1

    def ciz(t: Tuval, x, y):
        t.dikdortgen(KENAR + 36, y + 8 + 7, 5, 5, "#b7bec8", 2.5)
        metin_ciz(t, KENAR + girinti, y + 8)
        t.cizgi(KENAR, y + yukseklik - 1, SAYFA_G - KENAR, "#f3f4f6", 1)
    return yukseklik, ciz


def durum_kutulari(tamamlanan: list[str], devam: list[str]) -> tuple[float, callable]:
    """Yan yana yeşil TAMAMLANAN (#eef8f2) ve turuncu DEVAM EDEN (#fff7ea) kutuları: köşe 14, iç boşluk 14/16, etiket
    11px, metin 12px satır 1.55 (üst 8); boş kutu gri tek cümle."""
    en = (ICERIK_G - 14) / 2
    sy = 12 * 1.55

    def satirlar(liste, bos):
        if not liste:
            return [s_ for s_ in sar([Parca(bos, "R", GRI_2)], 12, en - 32)]
        return [s_ for m in liste for s_ in sar([Parca(m, "R", METIN)], 12, en - 32)]
    kutular = [("TAMAMLANAN", "#eef8f2", YESIL, satirlar(tamamlanan, "Bu dönemde tamamlanan iş yok.")),
               ("DEVAM EDEN", "#fff7ea", TURUNCU, satirlar(devam, "Açık iş bulunmuyor."))]
    yukseklik = 14 + 11 * 1.3 + 8 + max(len(k[3]) for k in kutular) * sy + 14

    def ciz(t: Tuval, x, y):
        for i, (etiket, zemin, renk, sat) in enumerate(kutular):
            kx = KENAR + i * (en + 14)
            t.dikdortgen(kx, y, en, yukseklik, zemin, 14)
            t.yaz(kx + 16, taban(y + 14, 11 * 1.3, 11), etiket, "B", 11, renk, aralik=0.1)
            for j, s_ in enumerate(sat):
                t.satir_yaz(kx + 16, taban(y + 14 + 11 * 1.3 + 8 + j * sy, sy, 12), s_, 12)
    return yukseklik, ciz


def patron_bloklari(tur: str, donem_adi: str, yapi: dict, ad_unvan: str, hazirlanma: date,
                    istatistik: dict | None = None) -> list[Blok]:
    """Bant → 24 pt boşluk → 4 gösterge → bölümler (ikon + ad + 'N madde', ince ayraçlı maddeler; başlık ilk maddeyle
    bölünmez) → Toplantılar → Tamamlanan / Devam eden kutuları. Taşan içerik sonraki sayfaya akar."""
    s, ist = yapi.get("sayilar") or {}, istatistik or {}
    bloklar = [yonetici_bandi(ad_unvan, "AYLIK YÖNETİCİ ÖZETİ" if tur == "aylik" else "YILLIK YÖNETİCİ ÖZETİ",
                              donem_adi, hazirlanma)]
    kurumlar = s.get("kurum_adlari") or [k["ad"] for k in (ist.get("kurumlar") or [])[:2]]
    yazisma = s.get("yazisma", ist.get("yazisma", sum(k["sayi"] for k in ist.get("kurumlar") or []) or s.get("eposta", 0)))
    kutular = [
        ("Rapor düzeni", f"{s.get('raporlu_is_gunu', 0)} / {s['is_gunu']}" if s.get("is_gunu") else "—", KOYU, "iş günü raporlandı"),
        ("Raporlanan iş", str(s.get("toplam_madde", 0)), KOYU, "madde"),
        ("Yazışma", str(yazisma), KIRMIZI, " ve ".join(kurumlar) if kurumlar else "kurumsal e-posta"),
        ("Tamamlanan", str(s.get("tamamlanan", 0)), YESIL, f"iş · {s.get('acik', 0)} açık"),
    ]
    bloklar.append(_blok(yonetici_gostergeleri(kutular), BANT_SONRASI))
    bolumler = [b for b in yapi.get("bolumler") or [] if b.get("maddeler")]
    if yapi.get("toplanti"):
        bolumler.append({"ad": "Toplantılar", "maddeler": yapi["toplanti"], "tur": "toplanti",
                         "madde_sayisi": s.get("toplanti") or 0})
    for b in bolumler:
        n = b.get("madde_sayisi") or 0
        baslik = yonetici_basligi(b["ad"], f"{n} madde" if n else "", b.get("tur") or "diger")
        maddeler = b["maddeler"]
        bloklar.append(_blok(birlesik(baslik, yonetici_satiri(maddeler[0]), araliklar=(0, 6)), 26))
        for m in maddeler[1:]:
            bloklar.append(_blok(yonetici_satiri(m)))
    bloklar.append(_blok(durum_kutulari(yapi.get("tamamlanan") or [], yapi.get("devam_eden") or []), 28))
    return bloklar


def madde_listesi(maddeler: list[str], boyut: float = 12.0) -> tuple[float, callable]:
    """'•' ile asılı girintili maddeler; satır 1.5, maddeler arası 3 (eski düz metin kayıtları)."""
    girinti = 14
    parcalar = [paragraf([Parca(m, "R", METIN)], boyut, ICERIK_G - girinti, 1.5) for m in maddeler]
    yukseklik = sum(h for h, _ in parcalar) + 3 * max(0, len(parcalar) - 1)

    def ciz(t: Tuval, x, y):
        yy = y
        for h, c in parcalar:
            t.yaz(KENAR + 2, taban(yy, boyut * 1.5, boyut), "•", "B", boyut, GRI_2)
            c(t, KENAR + girinti, yy)
            yy += h + 3
    return yukseklik, ciz


def duz_metin_bloklari(donem_adi: str, etiket: str, metin: str, istatistik: dict, ad_unvan: str,
                       hazirlanma: date) -> list[Blok]:
    """Yapısı olmayan (A1 döneminden kalma) özet: bant + metnin satırları; '*…*' ve madde imsiz satırlar başlık olur."""
    kapsam = (istatistik.get("kullanim") or {}).get("metin") or ""
    bloklar = [bant(ad_unvan, etiket, donem_adi, kapsam, hazirlanma)]
    satirlar = [x.strip() for x in (metin or "").splitlines()[1:]]
    ilk = True
    for x in satirlar:
        if not x:
            continue
        if x.startswith("•"):
            bloklar.append(_blok(madde_listesi([x.lstrip("• ").strip()]), 3))
        else:
            bloklar.append(_blok(bolum_etiketi(x.strip("*").rstrip(":")), 26 if ilk else 18))
            bloklar[-1].yukseklik += 8
        ilk = False
    return bloklar


def ozet_pdf(tur: str, bicim: str, donem_adi: str, yapi: dict | None, metin: str, istatistik: dict, ad: str,
             unvan: str = "", hazirlanma: date | None = None) -> bytes:
    """Kayıtlı özetin PDF'i. yapi yoksa (eski düz metin kaydı) metin satırlarından basit sürüm üretilir."""
    yazilari_kaydet()
    hazirlanma = hazirlanma or date.today()
    ad_unvan = " · ".join(x for x in (ad, unvan) if x)
    tip = ("Aylık " if tur == "aylik" else "Yıllık ") + ("Performans Özeti" if bicim == "basari" else "Yönetici Özeti")
    if yapi:
        bloklar = (basari_bloklari(tur, donem_adi, yapi, ad_unvan, hazirlanma) if bicim == "basari"
                   else patron_bloklari(tur, donem_adi, yapi, ad_unvan, hazirlanma, istatistik))
    else:
        etiket = buyuk(tip)
        bloklar = duz_metin_bloklari(donem_adi, etiket, metin, istatistik, ad_unvan, hazirlanma)
    sayfalar = dizil(bloklar, 0)
    return belgeyi_ciz(sayfalar, f"{ad} · {donem_adi} {tip}", f"{donem_adi} {tip}", ad)
