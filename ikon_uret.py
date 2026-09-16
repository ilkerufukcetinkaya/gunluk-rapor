"""PWA ikonlarını üretir: koyu mavi zemin, ortada beyaz onay işaretli liste. Çalıştırma: .venv/bin/python ikon_uret.py"""
from pathlib import Path

from PIL import Image, ImageDraw

ZEMIN = "#1f5fa8"
BEYAZ = "#ffffff"
KLASOR = Path(__file__).with_name("static")
BOYUTLAR = {"ikon-192.png": 192, "ikon-512.png": 512, "ikon-180.png": 180}


def ikon(boyut: int) -> Image.Image:
    # 4 kat büyük çizip küçültmek kenarları yumuşatır.
    b = boyut * 4
    resim = Image.new("RGB", (b, b), ZEMIN)
    ciz = ImageDraw.Draw(resim)
    kalinlik = round(b * 0.045)
    sol, sag = b * 0.24, b * 0.76
    for i in range(3):
        y = b * (0.33 + i * 0.17)
        # onay işareti
        ciz.line(
            [(sol, y), (sol + b * 0.045, y + b * 0.045), (sol + b * 0.125, y - b * 0.045)],
            fill=BEYAZ, width=kalinlik, joint="curve",
        )
        # satır
        x = sol + b * 0.2
        ciz.rounded_rectangle([x, y - kalinlik / 2, sag, y + kalinlik / 2], radius=kalinlik / 2, fill=BEYAZ)
    return resim.resize((boyut, boyut), Image.LANCZOS)


if __name__ == "__main__":
    KLASOR.mkdir(exist_ok=True)
    for ad, boyut in BOYUTLAR.items():
        ikon(boyut).save(KLASOR / ad, optimize=True)
        print(ad)
