"""Kebijakan kanal (note app-notifier 4, 5.5): kanal mana yang menerima event apa, dan seberapa rinci.

Tahap ini kebijakan dibaca sekali dari konfigurasi saat start (`ASTRIA_NOTIFIER_CHANNELS` atau `_FILE`), belum
dari `config.changed`. Nilai bawaan fail closed: kanal mati, detail `minimal`, dianggap eksternal.
"""

from __future__ import annotations

from typing import Literal, get_args

from astria_common.component import Check
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

Band = Literal["RENDAH", "SEDANG", "TINGGI", "KRITIS"]
BANDS: tuple[Band, ...] = get_args(Band)
EventType = Literal["card.verified", "digest.published"]


class Channel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    type: Literal["email", "telegram"]
    label: str = Field(min_length=1, max_length=100)
    enabled: bool = False
    max_detail: Literal["minimal", "ringkas"] = "minimal"
    band_min: Band = "TINGGI"
    event_types: list[EventType] = Field(min_length=1)
    external: bool = True
    """Kanal keluar organisasi. Telegram selalu eksternal; hanya email internal yang boleh `false`."""

    @model_validator(mode="after")
    def _telegram_selalu_eksternal(self) -> Channel:
        if self.type == "telegram" and not self.external:
            raise ValueError("kanal telegram selalu external")
        return self


_CHANNELS = TypeAdapter(list[Channel])


def parse_channels(raw: str | bytes) -> tuple[Channel, ...]:
    """Urai daftar kanal JSON. ID ganda ditolak karena ID kanal adalah bagian kunci outbox."""
    channels = _CHANNELS.validate_json(raw)
    ids = [c.channel_id for c in channels]
    if len(ids) != len(set(ids)):
        raise ValueError("channel_id ganda di kebijakan kanal")
    return tuple(channels)


def select_channels(channels: tuple[Channel, ...], event_type: str, band: Band | None) -> list[Channel]:
    """Kanal aktif yang berlangganan `event_type`; `band_min` hanya berlaku untuk event ber-band (kartu)."""
    return [
        c
        for c in channels
        if c.enabled and event_type in c.event_types and (band is None or BANDS.index(band) >= BANDS.index(c.band_min))
    ]


class PolicyStatus:
    """Komponen /ready: `down` sampai kebijakan kanal dimuat (mencegah kanal aktif tanpa kebijakan terbaca)."""

    name = "policy"
    required = True

    def __init__(self, channels: tuple[Channel, ...] | None) -> None:
        self.channels = channels

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def check(self) -> Check:
        if self.channels is None:
            return Check("down", "kebijakan kanal belum diisi (ASTRIA_NOTIFIER_CHANNELS atau _FILE)")
        aktif = sum(c.enabled for c in self.channels)
        return Check("ok", f"kanal={len(self.channels)} aktif={aktif}")
