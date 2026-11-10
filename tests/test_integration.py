"""Uji integrasi dengan PostgreSQL sungguhan (`ASTRIA_TEST_DATABASE_URL`, tersedia di kontainer py devenv)."""

from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta

import anyio
import httpx
import pytest
from astria_common import outbox
from astria_common.auth import ServicePrincipal
from astria_common.db import Database
from astria_common.hmac_auth import NonceCache, verify
from fastapi.testclient import TestClient
from helpers import BASE_URL, assert_no_real_names, channel, digest_event, kartu_event
from pydantic import SecretStr
from sqlalchemy import func, select, text, update

from astria_notifier.main import build_app
from astria_notifier.pipeline import Notifier, event_outbox, metadata
from astria_notifier.sender import EVENTS_PATH, Sender
from astria_notifier.settings import Settings

DB_URL = os.environ.get("ASTRIA_TEST_DATABASE_URL")
KEY = bytes.fromhex("ab" * 32)

pytestmark = [pytest.mark.anyio, pytest.mark.skipif(not DB_URL, reason="ASTRIA_TEST_DATABASE_URL tidak diisi")]

CHANNELS = (
    channel("telegram-tim", band_min="TINGGI"),
    channel(
        "email-internal",
        type="email",
        external=False,
        max_detail="ringkas",
        band_min="KRITIS",
        event_types=["card.verified"],
    ),
    channel("telegram-mati", enabled=False),
)


@pytest.fixture
def schema() -> str:
    return f"t_{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def db(schema: str):
    database = Database(SecretStr(DB_URL or ""), schema=schema, metadata=[outbox.metadata, metadata])
    await database.start()
    await database.wait_ready(10)
    yield database
    async with database.engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await database.stop()


class FakeGateway:
    """Gateway tiruan: memeriksa HMAC dengan `astria_common.hmac_auth.verify`, lalu membalas kode antrean."""

    def __init__(self, *codes: int | Exception) -> None:
        self.codes = list(codes)
        self.bodies: list[dict] = []
        self.nonces = NonceCache()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == EVENTS_PATH
        assert request.headers["x-astria-service"] == "notifier"
        assert request.headers["content-type"] == "application/json"
        verify(KEY, "POST", EVENTS_PATH, request.headers, request.content, self.nonces)
        self.bodies.append(json.loads(request.content))
        code = self.codes.pop(0)
        if isinstance(code, Exception):
            raise code
        return httpx.Response(code)


def _sender(db: Database, gateway: FakeGateway, **kwargs) -> Sender:
    http = httpx.AsyncClient(base_url="http://gateway:8080", transport=httpx.MockTransport(gateway))
    return Sender(db, http, KEY, **{"service": "notifier", "max_attempts": 10} | kwargs)


async def _consume(db: Database, handler, event) -> None:
    async with db.engine.begin() as conn:
        await handler(conn, *event)


async def _rows(db: Database) -> dict[str, dict]:
    async with db.engine.connect() as conn:
        rows = (await conn.execute(select(event_outbox).order_by(event_outbox.c.channel_id))).mappings().all()
    return {r["channel_id"]: dict(r) for r in rows}


def _notifier() -> Notifier:
    return Notifier(CHANNELS, BASE_URL + "/", card_ttl=timedelta(hours=24))


async def test_card_verified_writes_one_row_per_selected_channel_without_real_names(db: Database) -> None:
    event = kartu_event()
    await _consume(db, _notifier().on_kartu, event)
    await _consume(db, _notifier().on_kartu, event)  # dikirim ulang: PK mencegah baris ganda

    rows = await _rows(db)
    assert set(rows) == {"email-internal", "telegram-tim"}
    tim = rows["telegram-tim"]
    assert (tim["status"], tim["attempts"], tim["type"]) == ("pending", 0, "card.verified")
    assert tim["payload"]["tautan"] == f"{BASE_URL}/cards/IC-20261110-003"
    assert tim["expires_at"] - tim["occurred_at"] == timedelta(hours=24)
    assert len(tim["payload_hash"]) == 64
    assert "teknik" in rows["email-internal"]["payload"]
    for row in rows.values():
        assert_no_real_names(row["payload"])


async def test_card_below_band_min_or_with_unknown_band_writes_nothing(db: Database) -> None:
    await _consume(db, _notifier().on_kartu, kartu_event(kode={"band": "SEDANG", "skor": 40}))
    await _consume(db, _notifier().on_kartu, kartu_event(kode={"band": "DARURAT", "skor": 71}))
    assert await _rows(db) == {}


async def test_digest_never_expires(db: Database) -> None:
    await _consume(db, _notifier().on_digest, digest_event())
    rows = await _rows(db)
    assert set(rows) == {"telegram-tim"}
    assert rows["telegram-tim"]["expires_at"] is None
    assert rows["telegram-tim"]["payload"]["digest_id"] == "DG-20261111-0700"


@pytest.mark.parametrize(
    ("code", "status", "last_error"),
    [(202, "sent", None), (409, "sent", None), (422, "dead", "http_422"), (401, "dead", "http_401")],
)
async def test_final_responses(db: Database, code: int, status: str, last_error: str | None) -> None:
    await _consume(db, _notifier().on_kartu, kartu_event())
    gateway = FakeGateway(code)
    assert await _sender(db, gateway).send_once() == 1

    body = gateway.bodies[0]
    assert set(body) == {"event_id", "type", "occurred_at", "channels"}
    assert body["type"] == "card.verified" and body["occurred_at"].endswith("Z")
    assert sorted(c["channel_id"] for c in body["channels"]) == ["email-internal", "telegram-tim"]
    for row in (await _rows(db)).values():
        assert (row["status"], row["attempts"], row["last_error"]) == (status, 1, last_error)
        assert row["lease_until"] is None
        assert (row["sent_at"] is not None) == (status == "sent")
    assert await _sender(db, FakeGateway()).send_once() == 0


@pytest.mark.parametrize("failure", [503, 429, httpx.ConnectError("gateway mati")])
async def test_transient_failure_is_rescheduled_then_sent(db: Database, failure) -> None:
    await _consume(db, _notifier().on_digest, digest_event())
    gateway = FakeGateway(failure, 202)
    sender = _sender(db, gateway)
    await sender.send_once()

    row = (await _rows(db))["telegram-tim"]
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert row["last_error"] in {"http_503", "http_429", "ConnectError"}
    assert await sender.send_once() == 0  # belum jatuh tempo (jeda >= 4 detik)

    async with db.engine.begin() as conn:
        await conn.execute(update(event_outbox).values(next_attempt_at=func.now()))
    await sender.send_once()
    row = (await _rows(db))["telegram-tim"]
    assert (row["status"], row["attempts"]) == ("sent", 2)
    assert gateway.bodies[0]["event_id"] == gateway.bodies[1]["event_id"]


async def test_dead_after_max_attempts(db: Database) -> None:
    await _consume(db, _notifier().on_digest, digest_event())
    await _sender(db, FakeGateway(503), max_attempts=1).send_once()
    row = (await _rows(db))["telegram-tim"]
    assert (row["status"], row["last_error"]) == ("dead", "percobaan_habis:http_503")


async def test_card_past_ttl_expires_instead_of_sending(db: Database) -> None:
    await _consume(db, _notifier().on_kartu, kartu_event())
    async with db.engine.begin() as conn:
        await conn.execute(update(event_outbox).values(expires_at=func.now() - timedelta(seconds=1)))
    gateway = FakeGateway()
    assert await _sender(db, gateway).send_once() == 0
    assert {r["status"] for r in (await _rows(db)).values()} == {"expired"}
    assert gateway.bodies == []


async def test_expired_lease_returns_to_pending_and_is_sent(db: Database) -> None:
    await _consume(db, _notifier().on_digest, digest_event())
    async with db.engine.begin() as conn:
        await conn.execute(
            update(event_outbox).values(status="sending", attempts=1, lease_until=func.now() - timedelta(seconds=1))
        )
    await _sender(db, FakeGateway(202)).send_once()
    row = (await _rows(db))["telegram-tim"]
    assert (row["status"], row["attempts"]) == ("sent", 2)


class FakeTokens:
    async def verify(self, token: str, audience: str) -> ServicePrincipal:
        return ServicePrincipal(subject=token, audience=audience, act=None, claims={})


async def test_status_endpoint_counts_rows_per_status(db: Database, schema: str) -> None:
    await _consume(db, _notifier().on_kartu, kartu_event())
    await _sender(db, FakeGateway(422)).send_once()
    await _consume(db, _notifier().on_digest, digest_event())

    settings = Settings(env="test", database_url=SecretStr(DB_URL or ""), db_schema=schema, notifier_channels="[]")
    app = build_app(settings)
    app.state.token_verifier = FakeTokens()
    with TestClient(app) as client:
        for _ in range(50):  # basis data disiapkan di latar
            if client.get("/ready").status_code == 200:
                break
            await anyio.sleep(0.1)
        assert client.get("/internal/v1/status", headers={"Authorization": "Bearer svc:web"}).status_code == 403
        resp = client.get("/internal/v1/status", headers={"Authorization": "Bearer svc:api"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outbox"] == {"pending": 1, "sending": 0, "sent": 0, "dead": 2, "expired": 0}
    assert body["pending_tertua_detik"] >= 0
