# Günlük rapor

Patrona WhatsApp'tan gönderilen günlük raporu hazırlayan küçük web servisi. Her kullanıcının kendi hesabı, Gmail/GitHub ayarları ve maddeleri sunucuda durur. Bugün gönderilen iş e-postalarını (Gmail) ve projenin bugünkü commit'lerini (GitHub) tarar, rapor maddesi önerir; tikle, düzenle, kopyala. Maddeler tek Claude çağrısıyla yazım ve üslup yönünden düzeltilir (orijinal saklanır, geri alınabilir). Kopyalanan raporlar geçmişte birikir; geçmişten haftalık özet üretilir. Gönderim elle yapılır.

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
| `ANTHROPIC_API_KEY` | İsteğe bağlı, tüm kullanıcılar için ortak; bulunan maddelerin çevirisi, "Claude ile düzelt" ve haftalık özet için. Yoksa ham metin kullanılır, haftalık özet üretilemez |

Tablolar açılışta otomatik oluşur; sonradan eklenen kolon ve indeksler de açılışta idempotent olarak eklenir (`veritabani.sema_guncelle`, ayrı migration aracı yok).

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

- `GET /` arayüz · `/gecmis` rapor geçmişi ve haftalık özet · `/giris` · `/sifre` · `/ayarlar` · `/yonetim` (yalnız yönetici)
- `GET /api/durum`, `POST/PATCH/DELETE /api/maddeler`, `POST /api/maddeler/sira`
- `POST /api/duzelt` bugünkü düzeltilmemiş tikli maddeleri tek Claude çağrısıyla düzeltir (sürekli işler için günün ifadesi)
- `PATCH /api/maddeler/{id}/ai` `{"kullan": true|false}` AI metni / orijinal, `{"yenile": true}` yeniden düzelt
- `POST /api/raporlar` kopyalanan raporu kaydeder (kullanıcı + gün + tür başına tek satır), `GET /api/raporlar?q=&tur=&limit=&offset=`
- `POST /api/haftalik` `{"hafta_baslangic": "YYYY-MM-DD"}` (Pazartesi) o haftanın günlük raporlarından özet
- `GET /api/bugun` bugünkü öneriler (kullanıcı başına günde bir tarama, `?yenile=1` ile yeniden)
- `GET/PUT /api/ayarlar`, `POST /api/ayarlar/test`, `POST /api/ice-aktar`
- `GET /api/saglik` (korumasız)

Tüm tarihler Europe/Istanbul.
