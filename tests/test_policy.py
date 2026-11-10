import pytest
from helpers import channel
from pydantic import ValidationError

from astria_notifier.policy import Channel, parse_channels, select_channels


def test_defaults_fail_closed() -> None:
    c = Channel.model_validate(
        {"channel_id": "email-soc", "type": "email", "label": "Email SOC", "event_types": ["card.verified"]}
    )
    assert (c.enabled, c.max_detail, c.external) == (False, "minimal", True)


def test_telegram_cannot_be_internal_and_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        channel(external=False)
    with pytest.raises(ValidationError):
        channel(max_detail="penuh")
    with pytest.raises(ValidationError):
        channel(label_tambahan="x")


def test_parse_channels_rejects_duplicate_ids() -> None:
    one = '{"channel_id": "a", "type": "email", "label": "A", "event_types": ["card.verified"]}'
    assert [c.channel_id for c in parse_channels(f"[{one}]")] == ["a"]
    with pytest.raises(ValueError, match="ganda"):
        parse_channels(f"[{one}, {one}]")


def test_select_channels_by_enabled_event_type_and_band_min() -> None:
    channels = (
        channel("tinggi", band_min="TINGGI"),
        channel("kritis", band_min="KRITIS"),
        channel("mati", enabled=False, band_min="RENDAH"),
        channel("digest-saja", band_min="RENDAH", event_types=["digest.published"]),
    )
    ids = lambda *args: [c.channel_id for c in select_channels(channels, *args)]  # noqa: E731
    assert ids("card.verified", "KRITIS") == ["tinggi", "kritis"]
    assert ids("card.verified", "TINGGI") == ["tinggi"]
    assert ids("card.verified", "SEDANG") == []
    # Digest tidak punya band: band_min tidak berlaku.
    assert ids("digest.published", None) == ["tinggi", "kritis", "digest-saja"]
