"""Configuración centralizada del servicio mediante valores por defecto y variables de entorno."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    cleaned = raw.strip().lower()
    if cleaned in {"1", "true", "yes", "si", "sí", "on"}:
        return True
    if cleaned in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"La variable de entorno {name} debe ser booleana (true/false): {raw!r}")


def _get_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"La variable de entorno {name} debe ser un entero válido: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"La variable de entorno {name} debe ser >= {minimum}: {value}")
    return value


def _get_optional_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"La variable de entorno {name} debe ser un entero válido: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"La variable de entorno {name} debe ser > 0: {value}")
    return value


def _get_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"La variable de entorno {name} debe ser un número válido: {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"La variable de entorno {name} debe ser >= {minimum}: {value}")
    return value


def _normalize_extension(ext: str) -> str:
    cleaned = ext.strip().lower()
    if not cleaned:
        return ".mp4"
    return cleaned if cleaned.startswith(".") else f".{cleaned}"


@dataclass(frozen=True, slots=True)
class Settings:
    """Parámetros de red, cámara, audio, almacenamiento y registro del servicio."""

    host: str = "0.0.0.0"
    port: int = 3421
    camera_index: int = 0
    camera_backend: str = "auto"
    recordings_dir: Path = BASE_DIR / "recordings"
    logs_dir: Path = BASE_DIR / "logs"
    codec: str = "mp4v"
    file_extension: str = ".mp4"
    fallback_codec: str = "XVID"
    fallback_extension: str = ".avi"
    default_fps: float = 20.0
    frame_width: int | None = None
    frame_height: int | None = None
    warmup_timeout: float = 5.0
    stop_timeout: float = 10.0
    max_consecutive_failures: int = 30
    log_level: str = "INFO"

    # Configuración de captura de audio y multiplexado con ffmpeg
    audio_enabled: bool = True
    audio_device: str = "TE-9072"
    audio_required: bool = True
    audio_channels: int = 2
    audio_sample_rate: int | None = None
    audio_bitrate: str = "192k"
    mux_timeout: float = 120.0

    @classmethod
    def from_env(cls) -> Settings:
        """Carga la configuración desde variables de entorno con prefijo CAMARA_."""
        rec_env = os.getenv("CAMARA_RECORDINGS_DIR")
        recordings_dir = Path(rec_env).expanduser() if rec_env and rec_env.strip() else BASE_DIR / "recordings"

        logs_env = os.getenv("CAMARA_LOGS_DIR")
        logs_dir = Path(logs_env).expanduser() if logs_env and logs_env.strip() else BASE_DIR / "logs"

        port = _get_int("CAMARA_PORT", 3421, minimum=1)
        if port > 65535:
            raise ValueError(f"CAMARA_PORT debe estar entre 1 y 65535: {port}")

        codec = os.getenv("CAMARA_CODEC", "mp4v").strip() or "mp4v"
        if len(codec) != 4:
            raise ValueError(f"CAMARA_CODEC debe ser un FourCC de 4 caracteres: {codec!r}")

        fallback_codec = os.getenv("CAMARA_FALLBACK_CODEC", "XVID").strip() or "XVID"
        if len(fallback_codec) != 4:
            raise ValueError(f"CAMARA_FALLBACK_CODEC debe ser un FourCC de 4 caracteres: {fallback_codec!r}")

        audio_device = os.getenv("CAMARA_AUDIO_DEVICE", "TE-9072").strip() or "TE-9072"
        audio_bitrate = os.getenv("CAMARA_AUDIO_BITRATE", "192k").strip() or "192k"

        return cls(
            host=os.getenv("CAMARA_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=port,
            camera_index=_get_int("CAMARA_INDEX", 0, minimum=0),
            camera_backend=(os.getenv("CAMARA_BACKEND", "auto").strip().lower() or "auto"),
            recordings_dir=recordings_dir,
            logs_dir=logs_dir,
            codec=codec,
            file_extension=_normalize_extension(os.getenv("CAMARA_EXTENSION", ".mp4")),
            fallback_codec=fallback_codec,
            fallback_extension=_normalize_extension(os.getenv("CAMARA_FALLBACK_EXTENSION", ".avi")),
            default_fps=_get_float("CAMARA_DEFAULT_FPS", 20.0, minimum=1.0),
            frame_width=_get_optional_int("CAMARA_FRAME_WIDTH"),
            frame_height=_get_optional_int("CAMARA_FRAME_HEIGHT"),
            warmup_timeout=_get_float("CAMARA_WARMUP_TIMEOUT", 5.0, minimum=0.5),
            stop_timeout=_get_float("CAMARA_STOP_TIMEOUT", 10.0, minimum=1.0),
            max_consecutive_failures=_get_int("CAMARA_MAX_CONSECUTIVE_FAILURES", 30, minimum=1),
            log_level=(os.getenv("CAMARA_LOG_LEVEL", "INFO").strip().upper() or "INFO"),
            audio_enabled=_get_bool("CAMARA_AUDIO_ENABLED", True),
            audio_device=audio_device,
            audio_required=_get_bool("CAMARA_AUDIO_REQUIRED", True),
            audio_channels=_get_int("CAMARA_AUDIO_CHANNELS", 2, minimum=1),
            audio_sample_rate=_get_optional_int("CAMARA_AUDIO_SAMPLE_RATE"),
            audio_bitrate=audio_bitrate,
            mux_timeout=_get_float("CAMARA_MUX_TIMEOUT", 120.0, minimum=5.0),
        )
