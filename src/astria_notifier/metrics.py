"""Metrik Prometheus milik notifier (metrik bersama ada di `astria_common.metrics`)."""

from prometheus_client import Counter

DROPPED = Counter("notifier_dropped_total", "Event atau field yang tidak dikirim ke kanal", ["reason"])
