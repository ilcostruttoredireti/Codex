"""
logger.py — Logger colorato per console + file rotante.
"""

import logging
import logging.handlers
from pathlib import Path

try:
    import colorlog

    _HAS_COLORLOG = True
except ImportError:
    _HAS_COLORLOG = False


def get_logger(name: str, log_file: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # già configurato

    logger.setLevel(logging.DEBUG)

    # ── Console handler ────────────────────────────────────────────────────
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    if _HAS_COLORLOG:
        fmt = colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s",
            datefmt="%H:%M:%S",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
        )
    else:
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
        )

    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    # ── File handler ───────────────────────────────────────────────────────
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(file_handler)

    return logger
