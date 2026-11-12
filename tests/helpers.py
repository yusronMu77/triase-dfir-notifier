"""Data uji: kanal dan event dari contoh kontrak (kartu svc-backup di WS-ADM-04)."""

from __future__ import annotations

import json
from typing import Any

from astria_common.events import decode, encode, new_event
from astria_contracts import example

from astria_notifier.policy import Channel

BASE_URL = "https://astria.example"
NAMA_ASLI = ("svc-backup", "WS-ADM-04", "WS-HR-11", "Dugaan", "kegagalan logon")


def assert_no_real_names(payload: dict) -> None:
    text = json.dumps(payload)
    for nama in NAMA_ASLI:
        assert nama not in text


def channel(channel_id: str = "telegram-tim", **overrides: Any) -> Channel:
    values: dict[str, Any] = {
        "channel_id": channel_id,
        "type": "telegram",
        "label": "Tim SOC",
        "enabled": True,
        "band_min": "TINGGI",
        "event_types": ["card.verified", "digest.published"],
    }
    return Channel.model_validate(values | overrides)


def kartu_event(*, kode: dict[str, Any] | None = None, **data: Any):
    raw = example("card.verified")
    body = raw["data"] | data
    if kode is not None:
        body["dokumen"] = body["dokumen"] | {"kode": kode}
    return decode(encode(new_event("card.verified", body, correlation=raw["correlation"])))


def digest_event(correlation: dict[str, Any] | None = None):
    raw = example("digest.published")
    return decode(encode(new_event("digest.published", raw["data"], correlation=correlation or raw["correlation"])))


def revoked_event(**data: Any):
    raw = example("identity.revoked")
    return decode(encode(new_event("identity.revoked", raw["data"] | data, correlation=raw["correlation"])))
