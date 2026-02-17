#!/usr/bin/env python3
"""
Centralized logging configuration for STRATUS scripts.

Every action taken by the application — startup, API calls, data processing,
Smartsheet operations, file I/O, etc. — is recorded in a detailed running log
suitable for troubleshooting.

Usage in any script::

    from stratus_logger import get_logger
    logger = get_logger(__name__)
    logger.info("Starting sync...")
    logger.debug("Loaded %d rows", len(rows))

Log output goes to **both** the console and a timestamped file under the
``logs/`` directory (created automatically).  The file log captures every
level from DEBUG upward; the console shows INFO and above so that normal
user-facing output is not cluttered with low-level detail.
"""

import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Directory where log files are stored (relative to this module's location)
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# Maximum size per log file before rotation (5 MB)
_MAX_BYTES = 5 * 1024 * 1024

# Number of rotated backup files to keep
_BACKUP_COUNT = 10

# Format strings
_FILE_FMT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s"
)
_CONSOLE_FMT = "%(asctime)s | %(levelname)-8s | %(message)s"

# Date format used in log lines
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# Internal state — handlers are set up once and shared across all loggers
# ---------------------------------------------------------------------------
_initialized = False
_file_handler = None
_console_handler = None


def _ensure_log_dir():
    """Create the logs directory if it does not exist."""
    if not os.path.isdir(_LOG_DIR):
        os.makedirs(_LOG_DIR, exist_ok=True)


def _setup_handlers():
    """Create the shared file and console handlers (called once)."""
    global _initialized, _file_handler, _console_handler

    if _initialized:
        return
    _initialized = True

    _ensure_log_dir()

    # Timestamped log file for this run
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(_LOG_DIR, f"stratus_{timestamp}.log")

    # Rotating file handler — captures DEBUG and above
    _file_handler = RotatingFileHandler(
        log_file, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATE_FMT))

    # Console handler — captures INFO and above
    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setLevel(logging.INFO)
    _console_handler.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt=_DATE_FMT))


def get_logger(name: str) -> logging.Logger:
    """Return a logger configured with the shared file and console handlers.

    :param name: Logger name — typically ``__name__`` of the calling module.
    :return: A :class:`logging.Logger` instance ready for use.
    """
    _setup_handlers()

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Avoid adding duplicate handlers if called multiple times
    if not logger.handlers:
        logger.addHandler(_file_handler)
        logger.addHandler(_console_handler)
        # Prevent propagation to the root logger to avoid duplicate output
        logger.propagate = False

    return logger
