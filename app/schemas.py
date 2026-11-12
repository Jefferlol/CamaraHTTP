"""Modelos Pydantic para las respuestas HTTP del servicio."""

from typing import Literal

from pydantic import BaseModel, Field


class ToggleResponse(BaseModel):
    """Respuesta del endpoint interruptor POST /record/toggle."""

    status: Literal["recording", "idle"]
    message: str
    file: str


class StatusResponse(BaseModel):
    """Respuesta del endpoint de estado GET /record/status."""

    status: Literal["recording", "idle"]
    recording: bool
    message: str
    file: str | None = Field(default=None, description="Archivo en grabación activa, si existe.")
    last_file: str | None = Field(default=None, description="Último archivo finalizado.")
    started_at: str | None = Field(default=None, description="Marca temporal ISO-8601 UTC de inicio.")
    duration_seconds: float | None = Field(default=None, description="Duración transcurrida en segundos.")
    frames_written: int = Field(default=0, description="Fotogramas escritos en la sesión actual o previa.")
    audio_enabled: bool = Field(default=True, description="Indica si la captura de audio está habilitada.")
    audio_device: str | None = Field(default=None, description="Dispositivo de audio activo o configurado.")
    recordings_dir: str
    last_error: str | None = Field(default=None, description="Último error registrado en segundo plano.")


class AudioDeviceInfo(BaseModel):
    """Información resumida de un dispositivo de entrada de audio disponible."""

    index: int
    name: str
    hostapi: str
    max_input_channels: int
    default_samplerate: int


class AudioDevicesResponse(BaseModel):
    """Listado de micrófonos detectados por el sistema."""

    configured_query: str
    devices: list[AudioDeviceInfo]


class HealthResponse(BaseModel):
    """Respuesta básica de verificación de salud del servidor."""

    status: Literal["ok"]
    service: str


class ErrorResponse(BaseModel):
    """Respuesta estructurada para errores controlados de cámara, audio o mux."""

    status: Literal["error"] = "error"
    error_code: str
    message: str
