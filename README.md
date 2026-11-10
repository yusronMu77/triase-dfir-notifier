<!-- astria-logo -->
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/astria-logo-reverse.svg">
    <img src=".github/assets/astria-logo.svg" alt="Astria" width="320">
  </picture>
</p>

# Astria notifier

Pengiriman notifikasi lewat kanal.

> Status: Tahap 1 (notifier#2). Mengonsumsi `card.verified` dan `digest.published`, memilih kanal dari kebijakan kanal, merender payload allowlist per kanal, menulis `event_outbox`, lalu mendorongnya ke listener internal Gateway dengan HMAC. Notifier tidak pernah menghubungi SMTP atau Telegram sendiri. Repo ini adalah submodule dari workspace `triase-dfir` (folder `notifier/`). Rancangan: `notes/2026-11-06-triase-dfir-app-notifier.md` di repo root. Lajur developer: 2.

## Tech stack

Python 3.14, FastAPI, PostgreSQL 18, NATS JetStream, httpx, uv

## Perintah

```text
uv sync                                   # butuh akses ke repo privat contracts dan common (tag git)
uv run ruff format --check . && uv run ruff check .
uv run pytest
sh ../common/template/localtest.sh        # di kontainer py devenv: uji terhadap salinan kerja common dan contracts
```

Menjalankan bersama layanan lain: `pwsh ../devenv/scripts/dev.ps1 up`, lalu `dev.ps1 smoke`.

Uji integrasi (`tests/test_integration.py`) butuh `ASTRIA_TEST_DATABASE_URL`; di kontainer `py` devenv isi dengan
`postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/postgres`. Tanpa variabel itu uji tersebut dilewati.
Gateway selalu ditiru dengan `httpx.MockTransport`.

## Cara kerja

| Bagian | Isi |
|---|---|
| `policy.py` | Kebijakan kanal dari konfigurasi saat start: `channel_id`, `type` (`email`/`telegram`), `label`, `enabled` (bawaan `false`), `max_detail` (`minimal`/`ringkas`, bawaan `minimal`), `band_min`, `event_types`, `external` (bawaan `true`; Telegram selalu eksternal). Kanal dipilih bila aktif, berlangganan jenis event, dan band kartu >= `band_min` (digest tanpa band). `/ready` gagal sampai kebijakan dimuat |
| `render.py` | Payload allowlist `extra=forbid`. Kartu `minimal`: `card_id, version, band, skor, status, tautan`. `card.verified` belum membawa `tampilan` tersamarkan, jadi judul, ringkasan, entitas, host, dan akun **tidak pernah** keluar (fail closed). `ringkas` hanya untuk kanal internal: tambah `teknik` (ID ATT&CK), `klaim_total`, `klaim_lolos`; kanal eksternal ber-`ringkas` dikirim `minimal` (`notifier_dropped_total{reason="masking_missing"}`). Digest: `digest_id, jenis, periode_mulai, periode_selesai, tautan`. Band di luar empat nilai atau digest tanpa `digest_id` ditolak (`reason="invalid"`) |
| `pipeline.py` | Konsumen durable `notifier-card-verified` dan `notifier-digest-published`; baris `event_outbox` (PK `event_id, channel_id`, `payload_hash` SHA-256 JSON kanonik) ditulis di transaksi konsumen. Kartu kedaluwarsa 24 jam setelah `occurred_at`; digest tidak |
| `sender.py` | Loop pengirim: kartu lewat `expires_at` menjadi `expired`, lease `sending` yang habis kembali `pending`, lalu maksimal 10 event jatuh tempo disewa (lease 60 detik) dan dikirim berurutan, satu permintaan per event. Jeda naik 5 detik x 2^n (maks 15 menit, jitter 20%), `dead` setelah 10 percobaan |
| `api.py` | `GET /internal/v1/status` (token layanan, pemanggil `svc:api` atau `svc:config`): jumlah baris per status dan umur `pending` tertua |

### Kontrak push ke Gateway

`POST {ASTRIA_NOTIFIER_GATEWAY_URL}/internal/v1/events`, batas waktu 5 detik, tanpa redirect.

Header (skema `astria_common.hmac_auth`, kunci = kunci layanan notifier):

| Header | Isi |
|---|---|
| `Content-Type` | `application/json` |
| `X-Astria-Service` | `notifier` |
| `X-Astria-Ts` | Unix detik |
| `X-Astria-Nonce` | 32 karakter hex acak |
| `X-Astria-Sig` | hex(HMAC-SHA256(kunci, `POST\n/internal/v1/events\n{ts}\n{nonce}\n{sha256_hex(body)}`)) |

Penerima memeriksa dengan `astria_common.hmac_auth.verify(key, "POST", "/internal/v1/events", headers, body, nonces)`
(jendela 300 detik, nonce sekali pakai).

Body (satu event, semua kanalnya; `event_id` adalah kunci dedupe, pengiriman ulang membawa kanal yang sama):

```json
{
  "event_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
  "type": "card.verified",
  "occurred_at": "2026-11-10T02:37:05Z",
  "channels": [
    {"channel_id": "telegram-tim", "payload": {"card_id": "IC-20261110-003", "version": 1, "band": "KRITIS", "skor": 71, "status": "LULUS", "tautan": "https://<PUBLIC_BASE_URL>/cards/IC-20261110-003"}}
  ]
}
```

Untuk `digest.published`, `payload` = `{"digest_id", "jenis", "periode_mulai", "periode_selesai", "tautan"}` dengan tautan `/digests/{digest_id}`.

| Respons | Status baris |
|---|---|
| `202`, `409` | `sent` |
| `429`, `5xx`, galat jaringan, batas waktu | dicoba ulang (jeda naik), `dead` setelah percobaan habis |
| `400`, `401`, `403`, `404`, `422`, kode lain | `dead` (tidak dicoba ulang) |

### Konfigurasi

| Env | Bawaan | Catatan |
|---|---|---|
| `ASTRIA_NOTIFIER_CHANNELS` / `ASTRIA_NOTIFIER_CHANNELS_FILE` | kosong | Daftar kanal JSON (berkas diutamakan). Wajib bila basis data dan NATS diisi (tanpa kebijakan event akan hilang); kosong tanpa konsumen = tanpa pemeriksaan kebijakan; isi tidak valid = gagal start |
| `ASTRIA_NOTIFIER_PUBLIC_BASE_URL` | kosong | Basis tautan dasbor; wajib bila basis data dan NATS diisi |
| `ASTRIA_SERVICE_KEY` / `ASTRIA_SERVICE_KEY_FILE` | kosong | Kunci HMAC (hex) untuk tanda tangan push; wajib bila basis data dan NATS diisi |
| `ASTRIA_NOTIFIER_GATEWAY_URL` | `http://gateway:8080` | Origin listener internal Gateway (tanpa path) |
| `ASTRIA_NOTIFIER_CARD_TTL_SECONDS` | `86400` | Umur event kartu sebelum `expired` |
| `ASTRIA_NOTIFIER_MAX_ATTEMPTS` | `10` | Percobaan sebelum `dead` |

Contoh kebijakan:

```json
[{"channel_id": "telegram-tim", "type": "telegram", "label": "Tim SOC", "enabled": true, "max_detail": "minimal",
  "band_min": "TINGGI", "event_types": ["card.verified", "digest.published"], "external": true}]
```

Belum dibangun: penggabungan kartu 5 menit, detail `penuh`, `config.changed`, `ticket.*`, `system.degraded`,
`identity.revoked`, endpoint retry dan daftar outbox, retensi, dan migrasi Alembic (tabel dibuat `create_all`).

## Alur git

Branch `{type}/{yyyyMMdd}-{taskId}-{nama}`; commit `{type}({yyyyMMddHHmm}): [{taskId}] {pesan}`, satu baris, tanpa trailer. `main` hanya berubah lewat pull request (merge rebase); jangan push langsung ke `main` dan jangan `--no-verify`.
