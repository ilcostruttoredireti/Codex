"""
logger.py – configures a coloured console + rotating file logger
"""
from __future__ import annotations

import logging
import logging.handlers
import sys

try:
    import colorlog  # type: ignore

    _HAS_COLOR = True
except ImportError:
    _HAS_COLOR = False


def get_logger(name: str = "gmail_hubspot_sync", log_level: str = "INFO", log_file: str = "sync.log") -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger  # already configured

    logger.setLevel(getattr(logging, log_level, logging.INFO))

    # ── Console handler ──────────────────────────────────────
    if _HAS_COLOR:
        fmt = colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s [%(levelname)-8s]%(reset)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
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
            "%(asctime)s [%(levelname)-8s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # ── Rotating file handler ────────────────────────────────
    try:
        fh = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-8s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(fh)
    except OSError:
        logger.warning("Could not open log file %s – logging to console only", log_file)

    return logger
