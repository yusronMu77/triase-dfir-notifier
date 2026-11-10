"""Render payload per kanal (note app-notifier 5.3). Setiap payload adalah model allowlist `extra=forbid`.

Fail closed: `card.verified` membawa nama asli (judul, entitas, teks klaim) dan belum membawa `tampilan`
tersamarkan, jadi tidak satu pun teks bebas kartu keluar lewat notifier. Kanal eksternal selalu menerima
`minimal`; `ringkas` (tambahan ID ATT&CK dan hitungan klaim) hanya untuk kanal internal.
"""

from __future__ import annotations

from typing import Literal

from astria_contracts import Envelope
from astria_contracts.events.card_verified import CardVerified
from astria_contracts.events.digest_published import DigestPublished
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from astria_notifier.metrics import DROPPED
from astria_notifier.policy import Band, Channel


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KartuMinimal(_Payload):
    card_id: str = Field(pattern=r"^IC-[0-9]{8}-[0-9]{3,}$")
    version: int = Field(ge=1)
    band: Band
    skor: int = Field(ge=0, le=100)
    status: Literal["LULUS", "SEBAGIAN", "TEMPLAT"]
    tautan: str


class KartuRingkas(KartuMinimal):
    teknik: list[str]
    klaim_total: int = Field(ge=0)
    klaim_lolos: int = Field(ge=0)


class Digest(_Payload):
    digest_id: str = Field(pattern=r"^DG-[0-9]{8}-[0-9]{4}$")
    jenis: str
    periode_mulai: AwareDatetime
    periode_selesai: AwareDatetime
    tautan: str


class _Teknik(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(pattern=r"^T[0-9]{4}(\.[0-9]{3})?$")


class KodeKartu(BaseModel):
    """Bagian `dokumen.kode` (dict bebas di kontrak) yang boleh dibaca. Band di luar empat nilai = ditolak."""

    model_config = ConfigDict(extra="ignore")
    band: Band
    skor: int = Field(ge=0, le=100)
    teknik_attack: list[_Teknik] | None = None


def render_kartu(data: CardVerified, kode: KodeKartu, channel: Channel, base_url: str) -> KartuMinimal:
    dok = data.dokumen
    minimal = KartuMinimal(
        card_id=dok.card_id,
        version=dok.version,
        band=kode.band,
        skor=kode.skor,
        status=data.status,
        tautan=f"{base_url}/cards/{dok.card_id}",
    )
    if channel.max_detail == "minimal":
        return minimal
    if channel.external:
        DROPPED.labels("masking_missing").inc()
        return minimal
    return KartuRingkas(
        **minimal.model_dump(),
        teknik=[t.id for t in kode.teknik_attack or []],
        klaim_total=data.klaim_total,
        klaim_lolos=data.klaim_lolos,
    )


def render_digest(envelope: Envelope, data: DigestPublished, base_url: str) -> Digest:
    """Sama untuk semua `max_detail`: isi digest dibaca di dasbor, bukan di pesan."""
    digest_id = envelope.correlation.digest_id
    if digest_id is None:
        raise ValueError("digest.published tanpa correlation.digest_id")
    return Digest(
        digest_id=digest_id,
        jenis=data.jenis,
        periode_mulai=data.periode_mulai,
        periode_selesai=data.periode_selesai,
        tautan=f"{base_url}/digests/{digest_id}",
    )
