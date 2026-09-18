# Günlük rapor

Patrona WhatsApp'tan gönderilen günlük raporu hazırlayan küçük web servisi. Her kullanıcının kendi hesabı, Gmail/GitHub ayarları ve maddeleri sunucuda durur. Bugün gönderilen iş e-postalarını (Gmail) ve projenin bugünkü commit'lerini (GitHub) tarar, rapor maddesi önerir; tikle, düzenle, kopyala. Maddeler tek Claude çağrısıyla yazım ve üslup yönünden düzeltilir (orijinal saklanır, geri alınabilir). Kopyalanan raporlar geçmişte birikir; geçmişten haftalık özet üretilir. Gönderim elle yapılır; rapor kopyalanmamışsa ayarlanan saatte (varsayılan hafta içi 17:00) telefona bildirim ve e-posta ile hatırlatılır.

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
| `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` | Web push anahtar çifti (base64url). Render ve yerel aynı olmalı: abonelikler ortak veritabanında, anahtar değişirse telefonlarda bildirimler yeniden açılmalı. Üretmek: `python -c "import servisler; print(servisler.vapid_cifti_uret())"` |
| `VAPID_CLAIM_EMAIL` | `mailto:adres` biçiminde; push servisleri sorun olursa buna ulaşır |
| `APP_URL` | Bildirim ve e-postadaki bağlantı, örn. `https://gunluk-rapor.onrender.com` |
| `CRON_TOKEN` | `/api/hatirlat` ucunun parolası (32 bayt rastgele). Üretmek: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `RESEND_API_KEY` | Sistem e-postalarının gönderildiği Resend anahtarı. Yoksa yerel geliştirmede Gmail SMTP yedeğine düşülür |
| `EPOSTA_GONDEREN` | Gönderen adresi, varsayılan `rapor@medusarights.com`; Resend'de doğrulanmış alan adından olmalı |

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

## Hatırlatma (17:00)

Render free uyuduğu için zamanlayıcı dışarıdadır. cron-job.org kurulumu:

1. cron-job.org'da hesap açın → **Create cronjob**.
2. URL: `https://gunluk-rapor.onrender.com/api/hatirlat`, Schedule: her 5 dakika (saat dilimi fark etmez; karar Europe/Istanbul'a göre sunucuda verilir).
3. **Advanced** → Request method `POST`, Headers: `Authorization: Bearer <CRON_TOKEN>` (ya da URL sonuna `?token=<CRON_TOKEN>`; bu biçim Render erişim loglarına düşer, başlık tercih edilir).
4. Kaydedip **Test run** → yanıt 200 ve kullanıcı başına `push` / `eposta` / `neden`.
5. `GET /api/saglik` yanıtındaki `son_hatirlat_ping` cron'un geldiğini gösterir (bellekte tutulur; sık ping servisi uyanık tutar).

Kural: aktif kullanıcı için bugün hatırlatma günüyse, saat geçtiyse ve bugün günlük rapor kopyalanmadıysa tarama yapılır, telefon bildirimi ve e-posta (`EPOSTA_GONDEREN` adresinden, Yanıtla kullanıcının kendi adresine) gider. Kanal başına günde bir kez; geç gelen ping (17:04) sorun değildir. Tek istek 20 saniyeyi aşarsa kalan kullanıcılar bir sonraki ping'e kalır.

## E-posta gönderimi (Resend)

Render free planı giden SMTP portlarını kapatıyor (`OSError`), bu yüzden sistem e-postaları HTTPS ile Resend üzerinden gider.

1. [resend.com](https://resend.com)'da hesap açın → **Domains** → `medusarights.com` ekleyin.
2. Resend'in verdiği SPF/DKIM (ve istenirse DMARC) kayıtlarını alan adının DNS'ine girin, doğrulanmasını bekleyin.
3. **API Keys** → yeni anahtar (Sending access yeter) → değeri Render'da `RESEND_API_KEY` olarak tanımlayın.
4. `EPOSTA_GONDEREN`'i doğrulanmış alan adındaki bir adrese ayarlayın (varsayılan `rapor@medusarights.com`).
5. Ayarlar › Hatırlatma › **E-posta testi gönder** ile doğrulayın; hata olursa neden ekranda ve logda görünür.

`RESEND_API_KEY` tanımlı değilse kullanıcının kendi Gmail'i üzerinden SMTP yedeği denenir — yalnız yerel geliştirme içindir.

**iPhone:** bildirimler yalnız ana ekrana eklenmiş uygulamada çalışır (iOS 16.4+). Safari'de siteyi açın → Paylaş → **Ana Ekrana Ekle** → ana ekrandaki "Rapor" simgesinden açıp Ayarlar › Hatırlatma › **Bu cihazda bildirimleri aç**. Android/masaüstü Chrome'da doğrudan Ayarlar'dan açılır.

## Uçlar

- `GET /` arayüz · `/gecmis` rapor geçmişi ve haftalık özet · `/giris` · `/sifre` · `/ayarlar` · `/yonetim` (yalnız yönetici)
- `GET /api/durum`, `POST/PATCH/DELETE /api/maddeler`, `POST /api/maddeler/sira`
- `POST /api/duzelt` bugünkü düzeltilmemiş tikli maddeleri tek Claude çağrısıyla düzeltir (sürekli işler için günün ifadesi)
- `PATCH /api/maddeler/{id}/ai` `{"kullan": true|false}` AI metni / orijinal, `{"yenile": true}` yeniden düzelt
- `POST /api/raporlar` kopyalanan raporu kaydeder (kullanıcı + gün + tür başına tek satır), `GET /api/raporlar?q=&tur=&limit=&offset=`
- `POST /api/haftalik` `{"hafta_baslangic": "YYYY-MM-DD"}` (Pazartesi) o haftanın günlük raporlarından özet
- `GET /api/bugun` bugünkü öneriler (kullanıcı başına günde bir tarama, `?yenile=1` ile yeniden)
- `GET/PUT /api/ayarlar`, `POST /api/ayarlar/test`, `POST /api/ice-aktar`
- `GET /api/push/anahtar`, `GET/POST /api/push/abone`, `DELETE /api/push/abone/{id}`, `POST /api/push/dene` bu kullanıcının cihazlarına test bildirimi
- `POST /api/hatirlat?token=` ya da `Authorization: Bearer` — oturumsuz cron ucu, yanlış token 401
- `GET /api/saglik` (korumasız) `{"ok", "son_hatirlat_ping"}` · `/sw.js` ve `/static/*` (PWA; ikonlar `ikon_uret.py` ile üretilir)

Tüm tarihler Europe/Istanbul.
