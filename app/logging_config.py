"""Configuración de logging segura para ejecución sin consola (pythonw.exe / daemon)."""

from __future__ import annotations

import logging
from logging.config import dictConfig
import os
from pathlib import Path
import sys
from typing import Any


def has_console_streams() -> bool:
    """Indica si el proceso dispone de stdout y stderr válidos."""
    return sys.stdout is not None and sys.stderr is not None


def ensure_headless_streams() -> None:
    """Redirige stdout/stderr a os.devnull cuando son None (p. ej. bajo pythonw.exe)."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def build_logging_config(
    logs_dir: Path,
    log_level: str = "INFO",
    *,
    include_console: bool | None = None,
) -> dict[str, Any]:
    """Construye una configuración dictConfig compatible con Uvicorn y entornos sin TTY."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(logs_dir / "camara_http.log")

    if include_console is None:
        include_console = has_console_streams()

    handlers: dict[str, dict[str, Any]] = {
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "standard",
            "filename": log_file,
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "encoding": "utf-8",
        }
    }
    active_handlers = ["file"]

    if include_console:
        handlers["console"] = {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "stream": "ext://sys.stderr",
        }
        active_handlers.append("console")

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": handlers,
        "loggers": {
            "camara_http": {
                "handlers": active_handlers,
                "level": log_level,
                "propagate": False,
            },
            "uvicorn": {
                "handlers": active_handlers,
                "level": log_level,
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": active_handlers,
                "level": log_level,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": active_handlers,
                "level": log_level,
                "propagate": False,
            },
        },
        "root": {
            "handlers": active_handlers,
            "level": log_level,
        },
    }


def setup_logging(logs_dir: Path, log_level: str = "INFO") -> dict[str, Any]:
    """Inicializa el logging de la aplicación y devuelve la config lista para Uvicorn."""
    include_console = has_console_streams()
    ensure_headless_streams()
    config = build_logging_config(logs_dir, log_level, include_console=include_console)
    dictConfig(config)
    logging.captureWarnings(True)
    return config
