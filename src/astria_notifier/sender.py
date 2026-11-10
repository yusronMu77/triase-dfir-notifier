"""Pengirim `event_outbox` ke listener internal Gateway (note app-notifier 3.2, 4, 5.2).

Satu permintaan per event, berisi semua kanalnya; semua baris satu event berpindah status bersama, karena
Gateway mendedupe per `event_id` (409 = sudah diterima). Tanda tangan memakai `astria_common.hmac_auth`.

| Respons Gateway | Status baris |
|---|---|
| 202, 409 | `sent` |
| 429, 5xx, galat jaringan atau batas waktu | `pending` lagi dengan jeda naik + jitter; `dead` setelah percobaan habis |
| 4xx lain (400, 401, 403, 404, 422) dan kode tak terduga | `dead` |

Kartu yang lewat `expires_at` sebelum terkirim menjadi `expired`; lease `sending` yang habis kembali `pending`.
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx
import orjson
from astria_common.component import Check
from astria_common.db import Database
from astria_common.hmac_auth import signed_headers
from astria_common.log import get_logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from astria_notifier.metrics import DROPPED
from astria_notifier.pipeline import STATUSES
from astria_notifier.pipeline import event_outbox as t

log = get_logger(__name__)

EVENTS_PATH = "/internal/v1/events"
LEASE = timedelta(seconds=60)
# Push berurutan dengan batas waktu 5 detik: 10 event x 5 detik tetap di bawah lease 60 detik.
BATCH_EVENTS = 10
BACKOFF_BASE_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 900.0


def backoff(attempts: int) -> timedelta:
    """5, 10, 20, ... detik, maksimal 15 menit, jitter 20 persen."""
    base = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0))
    return timedelta(seconds=base * random.uniform(0.8, 1.2))  # noqa: S311 - jitter, bukan kriptografi


def classify(status_code: int) -> str:
    if status_code in (202, 409):
        return "sent"
    if status_code == 429 or status_code >= 500:
        return "retry"
    return "dead"


async def outbox_counts(conn: AsyncConnection) -> dict:
    """Jumlah baris per status dan umur `pending` tertua (detik), untuk /internal/v1/status dan /health."""
    rows = (await conn.execute(select(t.c.status, func.count(), func.min(t.c.created_at)).group_by(t.c.status))).all()
    counts = dict.fromkeys(STATUSES, 0) | {status: n for status, n, _ in rows}
    oldest = next((first for status, _, first in rows if status == "pending"), None)
    age = int((datetime.now(UTC) - oldest).total_seconds()) if oldest else 0
    return {"outbox": counts, "pending_tertua_detik": age}


class Sender:
    """Komponen loop pengirim. `degraded` bila ada baris `dead` (perlu tindakan admin)."""

    name = "sender"
    required = False

    def __init__(
        self,
        db: Database,
        http: httpx.AsyncClient,
        key: bytes,
        *,
        service: str,
        max_attempts: int,
        interval: float = 1.0,
    ) -> None:
        self._db = db
        self._http = http
        self._key = key
        self._service = service
        self._max_attempts = max_attempts
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="notifier-sender")

    async def _run(self) -> None:
        await self._db.wait_ready()
        while True:
            try:
                sent = await self.send_once()
                self._last_error = None
            except Exception as exc:  # noqa: BLE001 - dicoba lagi pada putaran berikutnya
                self._last_error = type(exc).__name__
                log.warning("pengirim_gagal", error=self._last_error)
                sent = 0
            if sent < BATCH_EVENTS:
                await asyncio.sleep(self._interval)

    async def send_once(self) -> int:
        """Satu putaran: rapikan status, sewa event jatuh tempo, kirim. Mengembalikan jumlah event dicoba."""
        await self._housekeep()
        events: dict[str, list] = defaultdict(list)
        for row in await self._lease():
            events[row.event_id].append(row)
        for event_id, rows in events.items():
            result, error = await self._push(rows)
            await self._record(event_id, rows[0].attempts, result, error)
        return len(events)

    async def _housekeep(self) -> None:
        async with self._db.engine.begin() as conn:
            expired = await conn.execute(
                update(t).where(t.c.status == "pending", t.c.expires_at < func.now()).values(status="expired")
            )
            await conn.execute(
                update(t).where(t.c.status == "sending", t.c.lease_until < func.now()).values(status="pending")
            )
        if expired.rowcount:
            DROPPED.labels("expired").inc(expired.rowcount)
            log.info("outbox_kedaluwarsa", jumlah=expired.rowcount)

    async def _lease(self) -> list:
        due = (
            select(t.c.event_id)
            .where(t.c.status == "pending", t.c.next_attempt_at <= func.now())
            .group_by(t.c.event_id)
            .order_by(func.min(t.c.next_attempt_at))
            .limit(BATCH_EVENTS)
        )
        async with self._db.engine.begin() as conn:
            result = await conn.execute(
                update(t)
                .where(t.c.event_id.in_(due), t.c.status == "pending")
                .values(status="sending", lease_until=func.now() + LEASE, attempts=t.c.attempts + 1)
                .returning(t.c.event_id, t.c.channel_id, t.c.type, t.c.occurred_at, t.c.payload, t.c.attempts)
            )
            return list(result.all())

    async def _push(self, rows: list) -> tuple[str, str | None]:
        first = rows[0]
        body = orjson.dumps(
            {
                "event_id": first.event_id,
                "type": first.type,
                "occurred_at": first.occurred_at.astimezone(UTC),
                "channels": [{"channel_id": r.channel_id, "payload": r.payload} for r in rows],
            },
            option=orjson.OPT_UTC_Z,
        )
        headers = signed_headers(self._service, self._key, "POST", EVENTS_PATH, body)
        try:
            resp = await self._http.post(
                EVENTS_PATH, content=body, headers=headers | {"Content-Type": "application/json"}
            )
        except httpx.HTTPError as exc:
            return "retry", type(exc).__name__
        result = classify(resp.status_code)
        return result, None if result == "sent" else f"http_{resp.status_code}"

    async def _record(self, event_id: str, attempts: int, result: str, error: str | None) -> None:
        values: dict = {"lease_until": None}
        if result == "sent":
            values |= {"status": "sent", "sent_at": func.now(), "last_error": None}
        elif result == "retry" and attempts < self._max_attempts:
            values |= {"status": "pending", "next_attempt_at": func.now() + backoff(attempts), "last_error": error}
        else:
            reason = error if result == "dead" else f"percobaan_habis:{error}"
            values |= {"status": "dead", "last_error": reason}
        async with self._db.engine.begin() as conn:
            await conn.execute(update(t).where(t.c.event_id == event_id, t.c.status == "sending").values(**values))
        log_fn = log.error if values["status"] == "dead" else log.info
        log_fn("push_gateway", event_id=event_id, status=values["status"], attempts=attempts, error=error)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        await self._http.aclose()

    async def check(self) -> Check:
        if not self._db.is_ready:
            return Check("down", "menunggu basis data")
        async with self._db.engine.connect() as conn:
            summary = await outbox_counts(conn)
        counts = summary["outbox"]
        detail = f"pending={counts['pending']} dead={counts['dead']} oldest_s={summary['pending_tertua_detik']}"
        if self._last_error:
            detail += f" error={self._last_error}"
        return Check("degraded" if counts["dead"] or self._last_error else "ok", detail)
