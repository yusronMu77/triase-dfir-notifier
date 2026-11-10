"""Rakit aplikasi notifier: komponen standar, kebijakan kanal, konsumen kartu dan digest, pengirim ke Gateway.

Dev memakai `uvicorn astria_notifier.main:build_app --factory --reload`; image memakai `astria-notifier`.
"""

from datetime import timedelta

from astria_common.app import create_app
from astria_common.consumer import Consumer
from astria_common.http import make_client
from astria_common.service import standard_runtime
from fastapi import FastAPI

from astria_notifier import api
from astria_notifier.pipeline import Notifier, metadata
from astria_notifier.policy import PolicyStatus
from astria_notifier.sender import Sender
from astria_notifier.settings import Settings

PUSH_TIMEOUT_SECONDS = 5.0


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    channels = settings.channels()
    runtime = standard_runtime(settings, metadata=[metadata])
    if channels is not None:
        runtime.add(PolicyStatus(channels))
    if runtime.db is not None and runtime.bus is not None:
        key = settings.service_key_bytes()
        # Tanpa kebijakan, konsumen akan meng-ack event tanpa baris outbox (hilang): gagal start saja.
        if key is None or not settings.notifier_public_base_url or channels is None:
            raise RuntimeError(
                "notifier: ASTRIA_SERVICE_KEY atau _FILE (tanda tangan push ke Gateway), "
                "ASTRIA_NOTIFIER_PUBLIC_BASE_URL, dan ASTRIA_NOTIFIER_CHANNELS atau _FILE wajib diisi"
            )
        notifier = Notifier(
            channels,
            settings.notifier_public_base_url,
            card_ttl=timedelta(seconds=settings.notifier_card_ttl_seconds),
        )
        for durable, event_type, handler in (
            ("notifier-card-verified", "card.verified", notifier.on_kartu),
            ("notifier-digest-published", "digest.published", notifier.on_digest),
        ):
            runtime.add(Consumer(runtime.bus, runtime.db, durable=durable, event_type=event_type, handler=handler))
        http = make_client(settings.notifier_gateway_url, timeout=PUSH_TIMEOUT_SECONDS)
        runtime.add(
            Sender(runtime.db, http, key, service=settings.service_name, max_attempts=settings.notifier_max_attempts)
        )
    return create_app(settings, components=runtime.components, routers=[api.router])
