"""Punto de entrada principal para ejecutar el servicio HTTP headless con Uvicorn."""

from __future__ import annotations

import argparse
from dataclasses import replace
import os
from pathlib import Path

# Minimiza mensajes nativos de OpenCV en consola antes de importar cv2.
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

import uvicorn

from app.api import create_app
from app.config import Settings
from app.logging_config import setup_logging


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Servicio HTTP headless para grabación de cámara web.",
    )
    parser.add_argument("--host", type=str, default=None, help="IP de escucha (defecto: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="Puerto HTTP (defecto: 3421)")
    parser.add_argument("--camera-index", type=int, default=None, help="Índice de cámara (defecto: 0)")
    parser.add_argument(
        "--audio-device",
        type=str,
        default=None,
        help="Nombre parcial o índice del micrófono (defecto: TE-9072)",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Desactiva la captura de audio y graba solo video",
    )
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=None,
        help="Directorio donde se guardarán las grabaciones",
    )
    return parser.parse_args()


def main() -> None:
    """Carga configuración, prepara logging headless e inicia Uvicorn."""
    args = _parse_args()
    settings = Settings.from_env()

    overrides: dict[str, object] = {}
    if args.host is not None:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.camera_index is not None:
        overrides["camera_index"] = args.camera_index
    if args.audio_device is not None:
        overrides["audio_device"] = args.audio_device
    if args.no_audio:
        overrides["audio_enabled"] = False
    if args.recordings_dir is not None:
        overrides["recordings_dir"] = args.recordings_dir
    if overrides:
        settings = replace(settings, **overrides)

    log_config = setup_logging(settings.logs_dir, settings.log_level)
    fastapi_app = create_app(settings=settings)

    uvicorn.run(
        fastapi_app,
        host=settings.host,
        port=settings.port,
        log_config=log_config,
        log_level=settings.log_level.lower(),
        access_log=True,
    )


if __name__ == "__main__":
    main()
