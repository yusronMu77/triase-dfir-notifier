"""Jalankan layanan: `astria-notifier` atau `python -m astria_notifier`."""

from astria_common.app import run

from astria_notifier.main import build_app
from astria_notifier.settings import Settings


def main() -> None:
    settings = Settings()
    run(build_app(settings), settings)


if __name__ == "__main__":
    main()
