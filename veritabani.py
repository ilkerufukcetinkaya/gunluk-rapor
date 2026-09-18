"""Veritabanı: bağlantı, tablolar. DATABASE_URL yoksa yerel sqlite (rapor.db)."""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, Time, UniqueConstraint, create_engine, inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

TURLER = ("surekli", "devam", "bugun", "bulunan")
RAPOR_TURLERI = ("gunluk", "haftalik")

log = logging.getLogger("gunluk-rapor")


def simdi() -> datetime:
    return datetime.now(timezone.utc)


def baglanti_adresi() -> str:
    url = os.environ.get("DATABASE_URL") or "sqlite:///rapor.db"
    # SQLAlchemy çıplak postgresql:// için psycopg2 arar; biz psycopg 3 kullanıyoruz.
    for onek in ("postgres://", "postgresql://"):
        if url.startswith(onek):
            return "postgresql+psycopg://" + url[len(onek):]
    return url


def motor_olustur(url: str):
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


motor = motor_olustur(baglanti_adresi())
OturumYapici = sessionmaker(bind=motor, expire_on_commit=False)


class Temel(DeclarativeBase):
    pass


class Kullanici(Temel):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    eposta: Mapped[str] = mapped_column(String(254), unique=True)
    ad: Mapped[str] = mapped_column(String(120))
    sifre_hash: Mapped[str] = mapped_column(String(255))
    rol: Mapped[str] = mapped_column(String(10), default="uye")  # 'admin' | 'uye'
    aktif: Mapped[bool] = mapped_column(Boolean, default=True)
    sifre_degistirmeli: Mapped[bool] = mapped_column(Boolean, default=True)
    son_giris: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    olusturma: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=simdi)


class KullaniciAyari(Temel):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    gmail_kullanici: Mapped[str | None] = mapped_column(String(254), nullable=True)
    gmail_sifre_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_repo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    proje_adi: Mapped[str | None] = mapped_column(String(80), nullable=True)
    patron_telefon: Mapped[str | None] = mapped_column(String(30), nullable=True)
    rapor_basligi: Mapped[str | None] = mapped_column(String(120), nullable=True)
    alan_sozlugu: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    hatirlatma_saat: Mapped[time] = mapped_column(Time, default=time(17, 0))
    hatirlatma_gunler: Mapped[str] = mapped_column(String(20), default="1,2,3,4,5")  # ISO haftanın günü
    hatirlatma_push: Mapped[bool] = mapped_column(Boolean, default=True)
    hatirlatma_eposta: Mapped[bool] = mapped_column(Boolean, default=True)
    hatirlatma_eposta_adres: Mapped[str | None] = mapped_column(String(254), nullable=True)  # boş → giriş e-postası
    # {"gmail": bool, "github": bool, "medusa": bool}; boşsa ayarı girilmiş kaynaklar açık sayılır.
    kaynaklar: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    kurulum_tamam: Mapped[bool] = mapped_column(Boolean, default=False)
    guncelleme: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=simdi, onupdate=simdi)


class Madde(Temel):
    __tablename__ = "items"
    __table_args__ = (
        Index("ix_items_user_tur_tarih", "user_id", "tur", "tarih"),
        UniqueConstraint("user_id", "tur", "tarih", "kaynak_id", name="uq_items_kaynak"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    tur: Mapped[str] = mapped_column(String(10))
    metin: Mapped[str] = mapped_column(Text, default="")
    asama: Mapped[str | None] = mapped_column(Text, nullable=True)
    tikli: Mapped[bool] = mapped_column(Boolean, default=True)
    gizli: Mapped[bool] = mapped_column(Boolean, default=False)
    tarih: Mapped[date | None] = mapped_column(Date, nullable=True)
    kaynak: Mapped[str | None] = mapped_column(String(20), nullable=True)
    kaynak_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sira: Mapped[int] = mapped_column(Integer, default=0)
    olusturma: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=simdi)
    # Claude düzeltmesi: metin kullanıcının yazdığıdır, metin_ai ayrı saklanır.
    metin_ai: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_tarih: Mapped[date | None] = mapped_column(Date, nullable=True)
    kullanici_duzenledi: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_kullan: Mapped[bool] = mapped_column(Boolean, default=True)
    # Bulunanlarda kaynağın zamanı: e-postanın Date başlığı ya da commit'in author tarihi.
    kaynak_zaman: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def sozluk(self) -> dict:
        return {
            "id": self.id, "tur": self.tur, "metin": self.metin, "asama": self.asama or "",
            "tikli": self.tikli, "gizli": self.gizli, "tarih": self.tarih.isoformat() if self.tarih else None,
            "kaynak": self.kaynak, "kaynak_id": self.kaynak_id, "sira": self.sira,
            "metin_ai": self.metin_ai, "kullanici_duzenledi": bool(self.kullanici_duzenledi),
            "ai_kullan": self.ai_kullan is not False,
        }


class GunlukIfade(Temel):
    """Sürekli işin o güne özel ifadesi."""
    __tablename__ = "gunluk_ifadeler"
    __table_args__ = (UniqueConstraint("item_id", "tarih", name="uq_gunluk_ifadeler_item_tarih"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    tarih: Mapped[date] = mapped_column(Date)
    metin_ai: Mapped[str] = mapped_column(Text)


class Rapor(Temel):
    __tablename__ = "reports"
    # Haftalık raporda tarih = hafta_baslangic; böylece tek anahtar iki türü de kapsar.
    __table_args__ = (Index("uq_reports_user_tarih_tur", "user_id", "tarih", "tur", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tarih: Mapped[date] = mapped_column(Date)
    metin: Mapped[str] = mapped_column(Text)
    olusturma: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=simdi)
    tur: Mapped[str] = mapped_column(String(10), default="gunluk")
    hafta_baslangic: Mapped[date | None] = mapped_column(Date, nullable=True)


class PushAbonelik(Temel):
    __tablename__ = "push_abonelikleri"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    cihaz_adi: Mapped[str] = mapped_column(String(60), default="")
    olusturma: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=simdi)
    son_basari: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    son_hata: Mapped[str | None] = mapped_column(Text, nullable=True)


class HatirlatmaGonderimi(Temel):
    """Kullanıcı + gün + kanal başına tek satır: aynı gün ikinci kez gönderilmez."""
    __tablename__ = "hatirlatma_gonderimleri"
    __table_args__ = (UniqueConstraint("user_id", "tarih", "kanal", name="uq_hatirlatma_gonderimleri"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    tarih: Mapped[date] = mapped_column(Date)
    kanal: Mapped[str] = mapped_column(String(10))  # 'push' | 'eposta'
    durum: Mapped[str] = mapped_column(String(20), default="gonderiliyor")
    hata_metni: Mapped[str | None] = mapped_column(Text, nullable=True)  # durum 'hata' ise kısa neden
    olusturma: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=simdi)


class ClaudeKullanim(Temel):
    """Kullanıcı + gün başına Claude çağrı sayısı ve token toplamı; günlük kota buradan okunur."""
    __tablename__ = "claude_kullanim"
    __table_args__ = (UniqueConstraint("user_id", "tarih", name="uq_claude_kullanim_user_tarih"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    tarih: Mapped[date] = mapped_column(Date)
    cagri: Mapped[int] = mapped_column(Integer, default=0)
    girdi_token: Mapped[int] = mapped_column(Integer, default=0)
    cikti_token: Mapped[int] = mapped_column(Integer, default=0)


# create_all mevcut tabloya kolon eklemez; eklenen kolonlar burada (tablo, kolon, Postgres tipi, sqlite tipi).
EK_KOLONLAR = [
    ("items", "metin_ai", "TEXT", "TEXT"),
    ("items", "ai_tarih", "DATE", "DATE"),
    ("items", "kullanici_duzenledi", "BOOLEAN NOT NULL DEFAULT false", "BOOLEAN NOT NULL DEFAULT 0"),
    ("items", "ai_kullan", "BOOLEAN NOT NULL DEFAULT true", "BOOLEAN NOT NULL DEFAULT 1"),
    ("items", "kaynak_zaman", "TIMESTAMP WITH TIME ZONE", "DATETIME"),
    ("reports", "tur", "VARCHAR(10) NOT NULL DEFAULT 'gunluk'", "VARCHAR(10) NOT NULL DEFAULT 'gunluk'"),
    ("reports", "hafta_baslangic", "DATE", "DATE"),
    ("user_settings", "hatirlatma_saat", "TIME NOT NULL DEFAULT '17:00'", "TIME NOT NULL DEFAULT '17:00:00'"),
    ("user_settings", "hatirlatma_gunler", "VARCHAR(20) NOT NULL DEFAULT '1,2,3,4,5'", "VARCHAR(20) NOT NULL DEFAULT '1,2,3,4,5'"),
    ("user_settings", "hatirlatma_push", "BOOLEAN NOT NULL DEFAULT true", "BOOLEAN NOT NULL DEFAULT 1"),
    ("user_settings", "hatirlatma_eposta", "BOOLEAN NOT NULL DEFAULT true", "BOOLEAN NOT NULL DEFAULT 1"),
    ("user_settings", "hatirlatma_eposta_adres", "VARCHAR(254)", "VARCHAR(254)"),
    ("user_settings", "kaynaklar", "JSON", "JSON"),
    ("user_settings", "kurulum_tamam", "BOOLEAN NOT NULL DEFAULT false", "BOOLEAN NOT NULL DEFAULT 0"),
    ("hatirlatma_gonderimleri", "hata_metni", "TEXT", "TEXT"),
]
# Sonradan eklenen tablolar; users tablosu olan şemada eksikse oluşturulur.
EK_TABLOLAR = ["push_abonelikleri", "hatirlatma_gonderimleri", "claude_kullanim"]
EK_INDEKSLER = [
    ("uq_reports_user_tarih_tur", "reports", "CREATE UNIQUE INDEX IF NOT EXISTS uq_reports_user_tarih_tur ON reports (user_id, tarih, tur)"),
]


def _kolonlar(b, tablo: str, sqlite: bool) -> set[str]:
    if sqlite:
        return {satir[1] for satir in b.execute(text(f"PRAGMA table_info({tablo})"))}
    return set(b.scalars(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = :t"
    ), {"t": tablo}))


def _dolu(kolon: str, mevcut: set[str]) -> str:
    return f"({kolon} IS NOT NULL AND {kolon} <> '')" if kolon in mevcut else "(1 = 0)"


def _kaynaklari_geri_doldur(b, sqlite: bool) -> None:
    """Kolon yeni eklendiyse: gmail/github, şifresi/token'ı kayıtlı olanlarda açık; medusa kapalı."""
    mevcut = _kolonlar(b, "user_settings", sqlite)
    gmail, github = _dolu("gmail_sifre_enc", mevcut), _dolu("github_token_enc", mevcut)
    if sqlite:
        dogruluk = lambda k: f"CASE WHEN {k} THEN json('true') ELSE json('false') END"  # noqa: E731
        b.execute(text(f"UPDATE user_settings SET kaynaklar = json_object('gmail', {dogruluk(gmail)}, "
                       f"'github', {dogruluk(github)}, 'medusa', json('false'))"))
    else:
        b.execute(text(f"UPDATE user_settings SET kaynaklar = json_build_object('gmail', {gmail}, 'github', {github}, 'medusa', false)"))


def _kurulumu_geri_doldur(b, sqlite: bool) -> None:
    """Kolon yeni eklendiyse: Gmail ayarı olan mevcut kullanıcılar sihirbazı görmez."""
    mevcut = _kolonlar(b, "user_settings", sqlite)
    if "gmail_sifre_enc" in mevcut:
        b.execute(text(f"UPDATE user_settings SET kurulum_tamam = {'1' if sqlite else 'true'} WHERE {_dolu('gmail_sifre_enc', mevcut)}"))


def sema_guncelle(motor_=None) -> list[str]:
    """İdempotent: eksik tablo, kolon ve indeksleri ekler, eklenenlerin adlarını döner."""
    motor_ = motor_ or motor
    eklenen = []
    with motor_.begin() as b:
        sqlite = motor_.dialect.name == "sqlite"
        tablolar = set(inspect(b).get_table_names())
        if "users" in tablolar:
            for ad in EK_TABLOLAR:
                if ad not in tablolar:
                    Temel.metadata.tables[ad].create(b)
                    tablolar.add(ad)
                    eklenen.append(ad)
        for tablo, kolon, pg_tipi, sqlite_tipi in EK_KOLONLAR:
            if tablo not in tablolar:
                continue
            if sqlite:
                mevcut = _kolonlar(b, tablo, sqlite)
                if kolon in mevcut:
                    continue
                b.execute(text(f"ALTER TABLE {tablo} ADD COLUMN {kolon} {sqlite_tipi}"))
            else:
                var = b.scalar(text(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                    "AND table_name = :t AND column_name = :k"
                ), {"t": tablo, "k": kolon})
                if var:
                    continue
                b.execute(text(f"ALTER TABLE {tablo} ADD COLUMN IF NOT EXISTS {kolon} {pg_tipi}"))
            eklenen.append(f"{tablo}.{kolon}")
            if (tablo, kolon) == ("user_settings", "kaynaklar"):
                _kaynaklari_geri_doldur(b, sqlite)
            elif (tablo, kolon) == ("user_settings", "kurulum_tamam"):
                _kurulumu_geri_doldur(b, sqlite)
        for ad, tablo, ddl in EK_INDEKSLER:
            if tablo not in tablolar:
                continue
            if sqlite:
                var = b.scalar(text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:a"), {"a": ad})
            else:
                var = b.scalar(text("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :a"), {"a": ad})
            if not var:
                b.execute(text(ddl))
                eklenen.append(ad)
    return eklenen


def tablolari_olustur() -> None:
    Temel.metadata.create_all(motor)
    eklenen = sema_guncelle()
    if eklenen:
        log.warning("Şema güncellendi: %s", ", ".join(eklenen))


def oturum():
    """FastAPI bağımlılığı: istek başına bir Session."""
    with OturumYapici() as db:
        yield db


__all__ = ["ClaudeKullanim", "GunlukIfade", "HatirlatmaGonderimi", "Kullanici", "KullaniciAyari", "Madde", "PushAbonelik", "Rapor", "Session", "motor", "oturum", "tablolari_olustur"]
