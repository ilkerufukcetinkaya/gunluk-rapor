# Günlük rapor — proje kuralları

## Dil
Bu projede tüm iletişim, plan, rapor ve commit mesajları **Türkçe** yazılır. `/clear` sonrasında da geçerlidir.

## Proje
Patrona WhatsApp'tan gönderilen günlük raporu hazırlayan çok kullanıcılı FastAPI aracı.
- Veritabanı: Supabase Postgres; yerel geliştirme ve testlerde sqlite.
- Barındırma: Render (free).
- Tek dosya modülleri: `app.py`, `api.py`, `veritabani.py`, `guvenlik.py`, `kimlik.py`, `servisler.py`.
- Şablonlar `templates/`, testler `tests/` → `.venv/bin/pytest -q`.

## Gizli bilgiler
- `.env` git dışıdır; her talimatın başında `git check-ignore .env` ile doğrulanır.
- `DATABASE_URL`, şifre, token ve API anahtarı değerleri hiçbir çıktıya, dosyaya ya da commit'e yazılmaz.

## Git
- `git add -A` yasak; yalnız adı verilen dosyalar eklenir.
- `sayfa-taslak.html` ve `.claude/` commit'e girmez.
- Commit + `git push origin main` talimatın parçasıdır.

## Şema
Şema değişiklikleri `veritabani.sema_guncelle` ile idempotent migration olarak yapılır (açılışta çalışır); ayrı migration aracı yok.

## Claude çağrıları
- Model `claude-sonnet-5`, thinking kapalı.
- Yanıt sözleşmesi yalnız JSON ya da düz metin.
- Hata olursa ham metinle devam edilir; hiçbir madde bozulmaz.

## Görsel dil
Ortak stil `templates/_stil.html`'dedir. Tasarım değişikliği yalnız açıkça istenirse yapılır.
