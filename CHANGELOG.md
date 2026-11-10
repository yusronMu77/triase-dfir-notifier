# Changelog

## [Unreleased]

### Added

- Kanal `whatsapp` (lewat jembatan WA di Gateway, gateway `wa-bridge`), selalu eksternal seperti Telegram.
- notifier#2: konsumen `card.verified` dan `digest.published`, kebijakan kanal dari `ASTRIA_NOTIFIER_CHANNELS`/`_FILE` (`/ready` menunggu kebijakan), renderer allowlist `minimal` dan `ringkas` (fail closed: tanpa nama asli), tabel `event_outbox`, pengirim ke Gateway `POST /internal/v1/events` bertanda `astria_common.hmac_auth` dengan jeda naik, `dead`, dan `expired`, `GET /internal/v1/status`, metrik `notifier_dropped_total{reason}`. Butuh `astria-contracts` v0.3.0 (`digest.published`).
- Kerangka layanan Tahap 0 dari templat `common` v0.1.0: `/health`, `/ready`, `/metrics`, token layanan, Dockerfile non-root, CI (lint, uji, gitleaks, build image, smoke `/ready`).
- Logo Astria di kepala README (`.github/assets/`, dari kit logo di repo `triase-dfir-uiux`).
