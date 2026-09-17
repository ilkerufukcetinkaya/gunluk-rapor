"""PWA ikonlarını üretir: mavi zemin, ortada beyaz onay işareti. Çalıştırma: .venv/bin/python ikon_uret.py"""
from pathlib import Path

from PIL import Image, ImageDraw

ZEMIN = "#2d6cdf"
BEYAZ = "#ffffff"
KLASOR = Path(__file__).with_name("static")
BOYUTLAR = {"ikon-192.png": 192, "ikon-512.png": 512, "ikon-180.png": 180}


def ikon(boyut: int) -> Image.Image:
    # 4 kat büyük çizip küçültmek kenarları yumuşatır.
    b = boyut * 4
    resim = Image.new("RGB", (b, b), ZEMIN)
    ciz = ImageDraw.Draw(resim)
    kalinlik = round(b * 0.085)
    # Tasarımdaki tik yolu (24'lük kutuda M5 12.5 l4.5 4.5 L19 7.5), maskeleme payı için ortada %56 alana ölçeklenir.
    olcek, kay = b * 0.56 / 24, b * 0.22
    noktalar = [(kay + x * olcek, kay + y * olcek) for x, y in ((5, 12.5), (9.5, 17), (19, 7.5))]
    ciz.line(noktalar, fill=BEYAZ, width=kalinlik, joint="curve")
    r = kalinlik / 2
    for x, y in (noktalar[0], noktalar[-1]):
        ciz.ellipse([x - r, y - r, x + r, y + r], fill=BEYAZ)
    return resim.resize((boyut, boyut), Image.LANCZOS)


if __name__ == "__main__":
    KLASOR.mkdir(exist_ok=True)
    for ad, boyut in BOYUTLAR.items():
        ikon(boyut).save(KLASOR / ad, optimize=True)
        print(ad)
