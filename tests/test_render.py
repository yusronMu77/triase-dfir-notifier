import pytest
from helpers import BASE_URL, assert_no_real_names, channel, digest_event, kartu_event
from pydantic import ValidationError

from astria_notifier.render import KartuMinimal, KartuRingkas, KodeKartu, render_digest, render_kartu

KARTU_FIELDS = {"card_id", "version", "band", "skor", "status", "tautan"}
TEKNIK = [{"id": "T1110.001", "taktik": "credential-access"}]


def _render(ch, **kode_extra):
    envelope, data = kartu_event()
    data.dokumen.kode |= kode_extra
    return render_kartu(data, KodeKartu.model_validate(data.dokumen.kode), ch, BASE_URL)


@pytest.mark.parametrize("max_detail", ["minimal", "ringkas"])
@pytest.mark.parametrize("type_", ["telegram", "email"])
def test_external_channels_get_only_minimal_fields_and_no_real_names(max_detail: str, type_: str) -> None:
    payload = _render(channel(type=type_, max_detail=max_detail), teknik_attack=TEKNIK).model_dump(mode="json")
    assert payload == {
        "card_id": "IC-20261110-003",
        "version": 1,
        "band": "KRITIS",
        "skor": 71,
        "status": "LULUS",
        "tautan": f"{BASE_URL}/cards/IC-20261110-003",
    }
    assert_no_real_names(payload)


def test_internal_ringkas_adds_only_attack_ids_and_claim_counts() -> None:
    ch = channel("email-internal", type="email", external=False, max_detail="ringkas")
    rendered = _render(ch, teknik_attack=TEKNIK)
    assert isinstance(rendered, KartuRingkas)
    payload = rendered.model_dump(mode="json")
    assert set(payload) == KARTU_FIELDS | {"teknik", "klaim_total", "klaim_lolos"}
    assert payload["teknik"] == ["T1110.001"]
    assert_no_real_names(payload)


def test_internal_minimal_stays_minimal() -> None:
    ch = channel("email-internal", type="email", external=False)
    assert type(_render(ch)) is KartuMinimal


def test_payload_models_forbid_extra_fields() -> None:
    payload = _render(channel()).model_dump()
    with pytest.raises(ValidationError):
        KartuMinimal.model_validate(payload | {"judul": "Dugaan pengambilalihan akun svc-backup"})


@pytest.mark.parametrize("kode", [{"band": "DARURAT", "skor": 71}, {"skor": 71}, {"band": "KRITIS", "skor": 999}])
def test_unknown_band_or_bad_score_is_rejected(kode: dict) -> None:
    with pytest.raises(ValidationError):
        KodeKartu.model_validate(kode)


def test_digest_payload() -> None:
    envelope, data = digest_event()
    payload = render_digest(envelope, data, BASE_URL).model_dump(mode="json")
    assert payload == {
        "digest_id": "DG-20261111-0700",
        "jenis": "harian",
        "periode_mulai": "2026-11-10T07:00:00+07:00",
        "periode_selesai": "2026-11-11T07:00:00+07:00",
        "tautan": f"{BASE_URL}/digests/DG-20261111-0700",
    }


def test_digest_without_id_is_rejected() -> None:
    envelope, data = digest_event(correlation={"job_id": "j1"})
    with pytest.raises(ValueError, match="digest_id"):
        render_digest(envelope, data, BASE_URL)
