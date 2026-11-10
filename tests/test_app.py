import pytest
from astria_common.envguard import EnvironmentGuardError
from fastapi.testclient import TestClient
from pydantic import SecretStr

from astria_notifier.main import build_app
from astria_notifier.settings import Settings

KANAL = '[{"channel_id": "telegram-tim", "type": "telegram", "label": "Tim", "event_types": ["card.verified"]}]'


def test_health_and_ready_with_policy_without_dependencies() -> None:
    with TestClient(build_app(Settings(env="test", notifier_channels=KANAL))) as client:
        health = client.get("/health").json()
        assert health["service"] == "notifier"
        assert health["env"] == "test"
        assert health["checks"]["policy"] == {"status": "ok", "detail": "kanal=1 aktif=0"}
        assert client.get("/ready").status_code == 200
        assert "astria_service_info" in client.get("/metrics").text


def test_not_ready_until_channel_policy_is_loaded() -> None:
    with TestClient(build_app(Settings(env="test"))) as client:
        assert client.get("/ready").status_code == 503


def test_channel_policy_from_file(tmp_path) -> None:
    path = tmp_path / "kanal.json"
    path.write_text(KANAL, encoding="utf-8")
    channels = Settings(env="test", notifier_channels_file=path).channels()
    assert channels is not None and channels[0].channel_id == "telegram-tim"


def test_invalid_policy_fails_startup() -> None:
    with pytest.raises(ValueError):
        build_app(Settings(env="test", notifier_channels='[{"channel_id": "x"}]'))


DEV_DB = SecretStr("postgresql+psycopg://u:p@postgres:5432/notifier_dev")


def _dev(**overrides) -> Settings:
    """Dibuat di dalam uji, sesudah conftest membersihkan env ASTRIA_* kontainer dev."""
    values = {
        "env": "dev",
        "database_url": DEV_DB,
        "nats_url": "nats://nats:4222",
        "service_key": SecretStr("ab" * 32),
        "notifier_public_base_url": "https://astria.example",
        "notifier_channels": KANAL,
    }
    return Settings(**values | overrides)


def test_consumers_and_sender_are_wired_when_db_and_nats_configured() -> None:
    app = build_app(_dev())
    expected = {"policy", "consumer:notifier-card-verified", "consumer:notifier-digest-published", "sender"}
    assert expected <= set(app.state.components)


@pytest.mark.parametrize("missing", ["service_key", "notifier_public_base_url", "notifier_channels"])
def test_key_base_url_and_policy_are_required_when_consumers_run(missing: str) -> None:
    with pytest.raises(RuntimeError):
        build_app(_dev(**{missing: None}))


def test_status_requires_service_token() -> None:
    with TestClient(build_app(Settings(env="test", notifier_channels=KANAL))) as client:
        assert client.get("/internal/v1/status").status_code == 401


def test_refuses_to_start_with_non_dev_database_in_dev() -> None:
    url = SecretStr("postgresql+psycopg://u:p@postgres:5432/astria")
    with pytest.raises(EnvironmentGuardError):
        build_app(Settings(env="dev", database_url=url))
