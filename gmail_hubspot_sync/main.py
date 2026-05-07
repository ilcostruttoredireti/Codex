"""Entry point for the Gmail → HubSpot contact sync service."""
import logging
import sys

from config import Config
from sync import SyncEngine


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("sync.log"),
        ],
    )
    # Silence noisy libraries
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main() -> None:
    _setup_logging()
    config = Config()
    try:
        config.validate()
    except (ValueError, FileNotFoundError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    engine = SyncEngine(config)
    engine.run_forever()


if __name__ == "__main__":
    main()
