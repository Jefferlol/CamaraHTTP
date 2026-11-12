"""Definición de la aplicación FastAPI, rutas HTTP y ciclo de vida del servicio."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.audio import list_input_devices
from app.config import Settings
from app.exceptions import RecorderError
from app.recorder import CameraRecorder
from app.schemas import (
    AudioDeviceInfo,
    AudioDevicesResponse,
    ErrorResponse,
    HealthResponse,
    StatusResponse,
    ToggleResponse,
)

logger = logging.getLogger("camara_http.api")


def create_app(
    settings: Settings | None = None,
    recorder: CameraRecorder | None = None,
) -> FastAPI:
    """Crea y configura la instancia FastAPI con gestión de ciclo de vida headless."""
    active_settings = settings or Settings.from_env()
    active_recorder = recorder or CameraRecorder(active_settings)

    @asynccontextmanager
    async def lifespan(fastapi_app: FastAPI):
        active_settings.recordings_dir.mkdir(parents=True, exist_ok=True)
        fastapi_app.state.settings = active_settings
        fastapi_app.state.recorder = active_recorder
        logger.info(
            "Servicio CamaraHTTP iniciado en %s:%d (cámara=%d, audio=%s, directorio=%s)",
            active_settings.host,
            active_settings.port,
            active_settings.camera_index,
            active_settings.audio_device if active_settings.audio_enabled else "deshabilitado",
            active_settings.recordings_dir,
        )
        try:
            yield
        finally:
            logger.info("Deteniendo servicio CamaraHTTP; verificando grabaciones activas...")
            try:
                stopped = await asyncio.to_thread(active_recorder.stop)
                if stopped is not None:
                    logger.info("Grabación cerrada durante el apagado: %s", stopped.file)
            except Exception:
                logger.exception("Error al finalizar la grabación durante el apagado del servidor")

    app = FastAPI(
        title="CamaraHTTP Headless Recorder",
        description="Servicio local ligero y silencioso para grabación de video y audio por HTTP.",
        version=__version__,
        lifespan=lifespan,
    )

    @app.exception_handler(RecorderError)
    async def recorder_error_handler(_request: Request, exc: RecorderError) -> JSONResponse:
        payload = ErrorResponse(error_code=exc.error_code, message=exc.message)
        return JSONResponse(status_code=exc.status_code, content=payload.model_dump())

    @app.post(
        "/record/toggle",
        response_model=ToggleResponse,
        responses={
            500: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
            504: {"model": ErrorResponse},
        },
        summary="Iniciar o detener la grabación",
    )
    async def toggle_recording(request: Request) -> ToggleResponse:
        """Alterna la grabación en segundo plano (inicia si está en espera, detiene si graba)."""
        rec: CameraRecorder = request.app.state.recorder
        return await asyncio.to_thread(rec.toggle)

    @app.get(
        "/record/status",
        response_model=StatusResponse,
        summary="Consultar estado del grabador",
    )
    async def record_status(request: Request) -> StatusResponse:
        """Indica si el sistema está grabando o en espera."""
        rec: CameraRecorder = request.app.state.recorder
        return rec.get_status()

    @app.get(
        "/audio/devices",
        response_model=AudioDevicesResponse,
        summary="Listar micrófonos disponibles",
    )
    async def audio_devices(request: Request) -> AudioDevicesResponse:
        """Lista los dispositivos de entrada de audio detectados por PortAudio."""
        cfg: Settings = request.app.state.settings
        candidates = await asyncio.to_thread(list_input_devices)
        return AudioDevicesResponse(
            configured_query=cfg.audio_device,
            devices=[
                AudioDeviceInfo(
                    index=c.index,
                    name=c.name,
                    hostapi=c.hostapi_name,
                    max_input_channels=c.max_input_channels,
                    default_samplerate=c.default_samplerate,
                )
                for c in candidates
            ],
        )

    @app.get(
        "/health",
        response_model=HealthResponse,
        summary="Comprobación de salud del servidor",
    )
    async def healthcheck() -> HealthResponse:
        """Retorna el estado básico de disponibilidad HTTP."""
        return HealthResponse(status="ok", service="camara-http")

    return app
