"""Konsumsi `card.verified` dan `digest.published`: pilih kanal, render, tulis `event_outbox` (note notifier 4, 5.2).

Baris outbox ditulis di transaksi konsumen (bersama `processed_events`), satu baris per (event, kanal). Pengiriman
ke Gateway dikerjakan `sender.Sender`. Event yang ditolak (band tidak dikenal, digest tanpa ID) di-ack tanpa baris
dan dihitung di `notifier_dropped_total{reason="invalid"}`, bukan dikirim ulang.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import orjson
from astria_common.log import get_logger
from astria_contracts import Envelope
from astria_contracts.events.card_verified import CardVerified
from astria_contracts.events.digest_published import DigestPublished
from pydantic import BaseModel, ValidationError
from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, MetaData, Table, Text, func
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncConnection

from astria_notifier.metrics import DROPPED
from astria_notifier.policy import Channel, select_channels
from astria_notifier.render import KodeKartu, render_digest, render_kartu

log = get_logger(__name__)

STATUSES = ("pending", "sending", "sent", "dead", "expired")

metadata = MetaData()

event_outbox = Table(
    "event_outbox",
    metadata,
    Column("event_id", Text, primary_key=True),
    Column("channel_id", Text, primary_key=True),
    Column("type", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("payload_hash", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("lease_until", DateTime(timezone=True), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("sent_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(f"status IN {STATUSES}", name="event_outbox_status_valid"),
)
Index("event_outbox_due", event_outbox.c.next_attempt_at, postgresql_where=event_outbox.c.status == "pending")


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()


class Notifier:
    """Handler dua konsumen. Kebijakan kanal dan basis tautan tetap selama proses hidup."""

    def __init__(self, channels: tuple[Channel, ...], public_base_url: str, *, card_ttl: timedelta) -> None:
        self._channels = channels
        self._base_url = public_base_url.rstrip("/")
        self._card_ttl = card_ttl

    async def on_kartu(self, conn: AsyncConnection, envelope: Envelope, data: BaseModel) -> None:
        assert isinstance(data, CardVerified)
        try:
            kode = KodeKartu.model_validate(data.dokumen.kode)
        except ValidationError:
            self._drop(envelope, "kode_tidak_valid")
            return
        payloads = {
            c.channel_id: render_kartu(data, kode, c, self._base_url)
            for c in select_channels(self._channels, envelope.type, kode.band)
        }
        await self._write(conn, envelope, payloads, expires_at=envelope.occurred_at + self._card_ttl)

    async def on_digest(self, conn: AsyncConnection, envelope: Envelope, data: BaseModel) -> None:
        assert isinstance(data, DigestPublished)
        try:
            payload = render_digest(envelope, data, self._base_url)
        except ValueError:  # ValidationError turunan ValueError
            self._drop(envelope, "digest_tanpa_id")
            return
        channels = select_channels(self._channels, envelope.type, None)
        await self._write(conn, envelope, {c.channel_id: payload for c in channels}, expires_at=None)

    @staticmethod
    def _drop(envelope: Envelope, alasan: str) -> None:
        DROPPED.labels("invalid").inc()
        log.warning("event_ditolak", event_id=envelope.id, type=envelope.type, alasan=alasan)

    @staticmethod
    async def _write(
        conn: AsyncConnection, envelope: Envelope, payloads: dict[str, BaseModel], *, expires_at: datetime | None
    ) -> None:
        if not payloads:
            log.info("tanpa_kanal", event_id=envelope.id, type=envelope.type)
            return
        rows = []
        for channel_id, model in payloads.items():
            payload = model.model_dump(mode="json")
            rows.append(
                {
                    "event_id": envelope.id,
                    "channel_id": channel_id,
                    "type": envelope.type,
                    "occurred_at": envelope.occurred_at,
                    "payload": payload,
                    "payload_hash": payload_hash(payload),
                    "expires_at": expires_at,
                }
            )
        await conn.execute(insert(event_outbox).on_conflict_do_nothing(), rows)
        log.info("outbox_ditulis", event_id=envelope.id, type=envelope.type, kanal=sorted(payloads))
