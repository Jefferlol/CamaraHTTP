"""Utilidades silenciosas (sin consola) de multiplexado de video y audio con ffmpeg."""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess

import imageio_ffmpeg

from app.exceptions import MediaMuxError

logger = logging.getLogger("camara_http.media")


def _audio_codec_args(extension: str, audio_bitrate: str) -> list[str]:
    ext = extension.lower()
    if ext == ".avi":
        return ["-c:a", "pcm_s16le"]
    if ext in {".mp4", ".mov", ".m4v"}:
        return ["-c:a", "aac", "-b:a", audio_bitrate, "-movflags", "+faststart"]
    return ["-c:a", "aac", "-b:a", audio_bitrate]


def mux_video_and_audio(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    audio_bitrate: str = "192k",
    timeout: float = 120.0,
) -> None:
    """Combina video y audio en el archivo final sin recodificar la pista de video."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    audio_args = _audio_codec_args(output_path.suffix, audio_bitrate)

    cmd = [
        ffmpeg_exe,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-c:v",
        "copy",
        *audio_args,
        "-shortest",
        str(output_path),
    ]

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output_path.unlink(missing_ok=True)
        raise MediaMuxError(
            f"ffmpeg excedió el tiempo límite ({timeout:.0f}s) al unir video y audio."
        ) from exc
    except OSError as exc:
        output_path.unlink(missing_ok=True)
        raise MediaMuxError(f"No se pudo ejecutar ffmpeg: {exc}") from exc

    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        output_path.unlink(missing_ok=True)
        stderr = (result.stderr or "").strip() or f"código de salida {result.returncode}"
        raise MediaMuxError(f"Error al combinar video y audio con ffmpeg: {stderr}")

    logger.info("Video y audio combinados correctamente en %s", output_path.name)
