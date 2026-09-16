# Günlük rapor

Patrona WhatsApp'tan gönderilen günlük raporu hazırlayan küçük web servisi. Bugün gönderilen iş e-postalarını (Gmail) ve MEDUSA'nın bugünkü commit'lerini tarar, rapor maddesi önerir; tikle, düzenle, kopyala. Gönderim elle yapılır.

## Çalıştırma

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --port 8765     # http://localhost:8765 — kullanıcı: ufuk
.venv/bin/pytest -q
```

## Ortam değişkenleri (.env, git'e girmez)

| Anahtar | Açıklama |
|---|---|
| `RAPOR_SIFRE` | Basic Auth şifresi (zorunlu; yoksa uygulama açılmaz) |
| `GMAIL_KULLANICI`, `GMAIL_UYGULAMA_SIFRESI` | Gmail adresi ve uygulama şifresi (IMAP) |
| `GITHUB_TOKEN`, `GITHUB_REPO` | MEDUSA reposu (`sahip/ad`) ve okuma yetkili token |
| `ANTHROPIC_API_KEY` | İsteğe bağlı; varsa maddeler Claude ile iş diline çevrilir |

## Uçlar

- `GET /` arayüz
- `GET /api/bugun` bugünkü öneriler (günde bir kez üretilir, `?yenile=1` ile yenilenir)
- `GET /api/saglik`

Tüm tarihler Europe/Istanbul. Render'a `render.yaml` ile kurulur; anahtarlar Render panelinden girilir.
