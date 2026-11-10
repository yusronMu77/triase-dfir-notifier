"""Konfigurasi notifier dari env `ASTRIA_*`."""

from pathlib import Path

from astria_common.settings import AstriaSettings

from astria_notifier.policy import Channel, parse_channels


class Settings(AstriaSettings):
    service_name: str = "notifier"

    notifier_gateway_url: str = "http://gateway:8080"
    """Listener internal Gateway (origin saja, tanpa path). Notifier tidak pernah menghubungi SMTP atau Telegram."""
    notifier_public_base_url: str | None = None
    """Basis tautan dasbor di payload, mis. https://astria.example. Wajib bila konsumen aktif; tidak pernah dari
    isi event."""
    notifier_channels: str | None = None
    """Kebijakan kanal, daftar JSON (lihat `policy.Channel`)."""
    notifier_channels_file: Path | None = None
    """Berkas JSON kebijakan kanal; diutamakan atas `notifier_channels`."""
    notifier_card_ttl_seconds: int = 86400
    """Event kartu yang belum terkirim setelah ini menjadi `expired`. Digest tidak kedaluwarsa."""
    notifier_max_attempts: int = 10

    def channels(self) -> tuple[Channel, ...] | None:
        """Kebijakan kanal, atau None bila belum diisi. Isi yang tidak valid menggagalkan start."""
        if self.notifier_channels_file is not None:
            return parse_channels(self.notifier_channels_file.read_bytes())
        if self.notifier_channels is not None:
            return parse_channels(self.notifier_channels)
        return None
