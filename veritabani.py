"""Veritabanı: bağlantı, tablolar. DATABASE_URL yoksa yerel sqlite (rapor.db)."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

TURLER = ("surekli", "devam", "bugun", "bulunan")


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

    def sozluk(self) -> dict:
        return {
            "id": self.id, "tur": self.tur, "metin": self.metin, "asama": self.asama or "",
            "tikli": self.tikli, "gizli": self.gizli, "tarih": self.tarih.isoformat() if self.tarih else None,
            "kaynak": self.kaynak, "kaynak_id": self.kaynak_id, "sira": self.sira,
        }


class Rapor(Temel):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tarih: Mapped[date] = mapped_column(Date)
    metin: Mapped[str] = mapped_column(Text)
    olusturma: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=simdi)


def tablolari_olustur() -> None:
    Temel.metadata.create_all(motor)


def oturum():
    """FastAPI bağımlılığı: istek başına bir Session."""
    with OturumYapici() as db:
        yield db


__all__ = ["Kullanici", "KullaniciAyari", "Madde", "Rapor", "Session", "motor", "oturum", "tablolari_olustur"]
