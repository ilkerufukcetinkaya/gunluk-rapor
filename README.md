# Günlük rapor

Patrona WhatsApp'tan gönderilen günlük raporu hazırlayan küçük web servisi. Her kullanıcının kendi hesabı, Gmail/GitHub ayarları ve maddeleri sunucuda durur. Bugün gönderilen iş e-postalarını (Gmail) ve projenin bugünkü commit'lerini (GitHub) tarar, rapor maddesi önerir; tikle, düzenle, kopyala. Gönderim elle yapılır.

## Çalıştırma

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --port 8765     # http://localhost:8765
.venv/bin/pytest -q                       # sqlite + geçici anahtar, ağa çıkmaz
```

## Ortam değişkenleri (.env, git'e girmez)

| Anahtar | Açıklama |
|---|---|
| `DATABASE_URL` | Supabase Postgres adresi; yoksa yerel `rapor.db` (sqlite) |
| `GIZLI_ANAHTAR` | Fernet anahtarı (zorunlu). Gmail şifresi ve GitHub token'ı bununla şifrelenir; değişirse kayıtlı şifreler okunamaz. Üretmek: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `ADMIN_EPOSTA`, `ADMIN_SIFRE` | Tablo boşken ilk açılışta yönetici hesabı bunlarla oluşur |
| `ANTHROPIC_API_KEY` | İsteğe bağlı, tüm kullanıcılar için ortak; maddeler Claude ile iş diline çevrilir |

Tablolar açılışta otomatik oluşur (ayrı migration yok).

## Kurulum ve davet akışı

1. Render'da yukarıdaki değişkenleri girin; `GIZLI_ANAHTAR` yereldekiyle aynı olmalı (aynı veritabanı).
2. İlk açılışta `ADMIN_EPOSTA` / `ADMIN_SIFRE` ile yönetici oluşur; `/giris`'ten girin.
3. **Ayarlar**: Gmail adresi + uygulama şifresi, GitHub repo + token, proje adı, patron numarası → "Bağlantıyı test et".
4. **Yönetim › Davet et**: ad + e-posta girin; 12 karakterlik geçici şifre bir kez gösterilir.
5. Geçici şifreyi kişiye WhatsApp'la iletin (e-posta ile davet henüz yok).
6. Kişi ilk girişte kendi şifresini belirler, sonra kendi Ayarlar sayfasını doldurur.
7. Şifresini unutan için Yönetim › Şifre sıfırla; ayrılan için Pasife al (oturumu hemen düşer).
8. Eski sürümü kullanan tarayıcıda sayfa ilk açıldığında localStorage verileri hesaba otomatik taşınır.

## Uçlar

- `GET /` arayüz · `/giris` · `/sifre` · `/ayarlar` · `/yonetim` (yalnız yönetici)
- `GET /api/durum`, `POST/PATCH/DELETE /api/maddeler`, `POST /api/maddeler/sira`
- `GET /api/bugun` bugünkü öneriler (kullanıcı başına günde bir tarama, `?yenile=1` ile yeniden)
- `GET/PUT /api/ayarlar`, `POST /api/ayarlar/test`, `POST /api/ice-aktar`
- `GET /api/saglik` (korumasız)

Tüm tarihler Europe/Istanbul.
