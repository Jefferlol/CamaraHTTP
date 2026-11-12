"""Servicio de captura sincronizada de video y audio headless."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
from pathlib import Path
import threading
import time
from typing import Any

import cv2

from app.audio import AudioRecorder
from app.config import Settings
from app.exceptions import (
    AudioUnavailableError,
    CameraUnavailableError,
    MediaMuxError,
    RecorderError,
    RecorderTimeoutError,
    VideoWriterInitError,
)
from app.media import mux_video_and_audio
from app.schemas import StatusResponse, ToggleResponse

logger = logging.getLogger("camara_http.recorder")

_BACKEND_ATTRS: dict[str, str] = {
    "auto": "CAP_ANY",
    "any": "CAP_ANY",
    "dshow": "CAP_DSHOW",
    "msmf": "CAP_MSMF",
    "v4l2": "CAP_V4L2",
    "avfoundation": "CAP_AVFOUNDATION",
}


@dataclass(slots=True)
class _StartupState:
    """Resultado compartido entre el hilo de captura y el hilo solicitante durante el arranque."""

    filename: str
    file_path: Path
    error: RecorderError | None = None


def _default_capture_factory(index: int, backend: int) -> Any:
    if backend == cv2.CAP_ANY:
        return cv2.VideoCapture(index)
    return cv2.VideoCapture(index, backend)


def _default_writer_factory(
    filepath: str,
    fourcc: int,
    fps: float,
    frame_size: tuple[int, int],
) -> Any:
    return cv2.VideoWriter(filepath, fourcc, fps, frame_size)


class CameraRecorder:
    """Administra captura de cámara web (OpenCV), micrófono (sounddevice) y mux con ffmpeg."""

    def __init__(
        self,
        settings: Settings,
        *,
        capture_factory: Callable[[int, int], Any] | None = None,
        writer_factory: Callable[[str, int, float, tuple[int, int]], Any] | None = None,
        audio_factory: Callable[[Path, Settings], Any] | None = None,
        mux_fn: Callable[..., None] | None = None,
    ) -> None:
        self.settings = settings
        self._capture_factory = capture_factory or _default_capture_factory
        self._writer_factory = writer_factory or _default_writer_factory
        self._audio_factory = audio_factory
        self._mux_fn = mux_fn or mux_video_and_audio

        # _transition_lock serializa inicios/paradas concurrentes; _state_lock protege lecturas rápidas de estado.
        self._transition_lock = threading.Lock()
        self._state_lock = threading.Lock()

        self._recording = False
        self._current_file: str | None = None
        self._current_path: Path | None = None
        self._last_file: str | None = None
        self._started_at: datetime | None = None
        self._started_monotonic: float | None = None
        self._frames_written = 0
        self._active_audio_device: str | None = None
        self._last_error: str | None = None
        self._stop_error: RecorderError | None = None
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None

    @property
    def is_recording(self) -> bool:
        """Indica si hay una grabación activa en este momento."""
        with self._state_lock:
            return self._recording

    def toggle(self) -> ToggleResponse:
        """Alterna entre iniciar y detener la grabación de forma atómica."""
        with self._transition_lock:
            with self._state_lock:
                recording = self._recording
                current_file = self._current_file
                thread = self._thread
                stop_event = self._stop_event

            if recording:
                return self._stop_locked(current_file, thread, stop_event)
            return self._start_locked()

    def stop(self) -> ToggleResponse | None:
        """Detiene la grabación si está activa (útil durante el apagado ordenado del servidor)."""
        with self._transition_lock:
            with self._state_lock:
                if not self._recording:
                    return None
                current_file = self._current_file
                thread = self._thread
                stop_event = self._stop_event

            return self._stop_locked(current_file, thread, stop_event)

    def get_status(self) -> StatusResponse:
        """Retorna una instantánea no bloqueante del estado actual del grabador."""
        with self._state_lock:
            recording = self._recording
            current_file = self._current_file
            last_file = self._last_file
            started_at = self._started_at
            started_monotonic = self._started_monotonic
            frames_written = self._frames_written
            active_audio = self._active_audio_device
            last_error = self._last_error

        duration: float | None = None
        if recording and started_monotonic is not None:
            duration = round(max(0.0, time.monotonic() - started_monotonic), 2)

        configured_audio = self.settings.audio_device if self.settings.audio_enabled else None
        return StatusResponse(
            status="recording" if recording else "idle",
            recording=recording,
            message="Grabación en curso" if recording else "Sistema en espera",
            file=current_file,
            last_file=last_file,
            started_at=started_at.isoformat() if started_at else None,
            duration_seconds=duration,
            frames_written=frames_written,
            audio_enabled=self.settings.audio_enabled,
            audio_device=active_audio or configured_audio,
            recordings_dir=str(self.settings.recordings_dir),
            last_error=last_error,
        )

    def _create_audio_recorder(self, wav_path: Path) -> Any:
        if self._audio_factory is not None:
            return self._audio_factory(wav_path, self.settings)
        return AudioRecorder(
            wav_path,
            device_query=self.settings.audio_device,
            desired_channels=self.settings.audio_channels,
            desired_sample_rate=self.settings.audio_sample_rate,
        )

    def _start_locked(self) -> ToggleResponse:
        """Inicia el hilo secundario y espera confirmación de que cámara, audio y archivo están listos."""
        self.settings.recordings_dir.mkdir(parents=True, exist_ok=True)
        base_stem = f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        target_path = self._build_unique_path(base_stem, self.settings.file_extension)

        stop_event = threading.Event()
        ready_event = threading.Event()
        startup_state = _StartupState(filename=target_path.name, file_path=target_path)

        worker = threading.Thread(
            target=self._capture_loop,
            args=(base_stem, stop_event, ready_event, startup_state),
            name="camera-recorder",
            daemon=True,
        )

        with self._state_lock:
            self._thread = worker
            self._stop_event = stop_event
            self._frames_written = 0
            self._active_audio_device = None
            self._last_error = None
            self._stop_error = None

        worker.start()

        wait_limit = self.settings.warmup_timeout + 6.0
        if not ready_event.wait(timeout=wait_limit):
            stop_event.set()
            worker.join(timeout=2.0)
            self._clear_active_state()
            raise RecorderTimeoutError(
                f"La cámara (índice {self.settings.camera_index}) tardó demasiado en responder."
            )

        if startup_state.error is not None:
            worker.join(timeout=2.0)
            self._clear_active_state()
            raise startup_state.error

        logger.info("Grabación iniciada: %s", startup_state.filename)
        return ToggleResponse(
            status="recording",
            message="Grabación iniciada",
            file=startup_state.filename,
        )

    def _stop_locked(
        self,
        current_file: str | None,
        thread: threading.Thread | None,
        stop_event: threading.Event | None,
    ) -> ToggleResponse:
        """Señaliza detención al hilo secundario, cierra dispositivos y combina audio/video."""
        if stop_event is not None:
            stop_event.set()

        join_timeout = self.settings.stop_timeout + (
            self.settings.mux_timeout if self.settings.audio_enabled else 0.0
        )
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
            if thread.is_alive():
                raise RecorderTimeoutError(
                    "Tiempo de espera agotado al cerrar la cámara, el audio o combinar el video."
                )

        with self._state_lock:
            stop_error = self._stop_error
            self._stop_error = None
            final_file = current_file or self._last_file or ""
            self._thread = None
            self._stop_event = None

        if stop_error is not None:
            raise stop_error

        logger.info("Grabación finalizada y guardada: %s", final_file)
        return ToggleResponse(
            status="idle",
            message="Grabación finalizada y guardada",
            file=final_file,
        )

    def _clear_active_state(self) -> None:
        with self._state_lock:
            self._recording = False
            self._current_file = None
            self._current_path = None
            self._started_at = None
            self._started_monotonic = None
            self._active_audio_device = None
            self._thread = None
            self._stop_event = None

    def _resolve_backend(self) -> int:
        backend_key = self.settings.camera_backend.lower()
        attr_name = _BACKEND_ATTRS.get(backend_key)
        if attr_name is None:
            logger.warning(
                "Backend de cámara desconocido %r; usando detección automática (CAP_ANY).",
                self.settings.camera_backend,
            )
            return cv2.CAP_ANY
        return int(getattr(cv2, attr_name, cv2.CAP_ANY))

    def _build_unique_path(self, base_stem: str, extension: str) -> Path:
        def _is_taken(path: Path) -> bool:
            tmp_path = path.with_name(f".tmp_{path.name}")
            return path.exists() or tmp_path.exists()

        candidate = self.settings.recordings_dir / f"{base_stem}{extension}"
        counter = 1
        while _is_taken(candidate):
            candidate = self.settings.recordings_dir / f"{base_stem}_{counter}{extension}"
            counter += 1
        return candidate

    def _open_and_warmup(self, stop_event: threading.Event) -> tuple[Any, Any]:
        backend = self._resolve_backend()
        cap = self._capture_factory(self.settings.camera_index, backend)
        if cap is None or not cap.isOpened():
            if cap is not None:
                self._safe_release(cap, "VideoCapture")
            raise CameraUnavailableError(
                f"No se pudo abrir la cámara (índice {self.settings.camera_index}). "
                "Verifique que esté conectada y libre de uso."
            )

        if self.settings.frame_width is not None:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.settings.frame_width))
        if self.settings.frame_height is not None:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.settings.frame_height))

        deadline = time.monotonic() + self.settings.warmup_timeout
        while not stop_event.is_set():
            ret, frame = cap.read()
            if ret and frame is not None and frame.size > 0:
                return cap, frame

            if time.monotonic() >= deadline:
                self._safe_release(cap, "VideoCapture")
                raise CameraUnavailableError(
                    f"La cámara (índice {self.settings.camera_index}) está ocupada, "
                    "desconectada o no entrega fotogramas válidos."
                )
            stop_event.wait(0.05)

        self._safe_release(cap, "VideoCapture")
        raise CameraUnavailableError("Grabación cancelada durante la inicialización de la cámara.")

    def _resolve_fps(self, cap: Any) -> float:
        try:
            reported = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        except (TypeError, ValueError):
            reported = 0.0

        if math.isfinite(reported) and 1.0 <= reported <= 120.0:
            return reported
        return self.settings.default_fps

    def _open_writer(
        self,
        base_stem: str,
        fps: float,
        frame_size: tuple[int, int],
        *,
        use_temp: bool,
    ) -> tuple[Any, Path, Path]:
        attempts = [(self.settings.codec, self.settings.file_extension)]
        fallback = (self.settings.fallback_codec, self.settings.fallback_extension)
        if fallback != attempts[0]:
            attempts.append(fallback)

        for codec, ext in attempts:
            final_path = self._build_unique_path(base_stem, ext)
            write_path = final_path.with_name(f".tmp_{final_path.name}") if use_temp else final_path
            fourcc = cv2.VideoWriter_fourcc(*codec)
            writer = self._writer_factory(str(write_path), fourcc, fps, frame_size)
            if writer is not None and writer.isOpened():
                if codec != self.settings.codec:
                    logger.warning(
                        "Códec primario %s no disponible; grabando con códec alternativo %s (%s).",
                        self.settings.codec,
                        codec,
                        final_path.name,
                    )
                return writer, final_path, write_path

            if writer is not None:
                self._safe_release(writer, "VideoWriter")
            # Limpia archivos vacíos si el backend los creó antes de fallar isOpened().
            if write_path.exists() and write_path.stat().st_size == 0:
                write_path.unlink(missing_ok=True)

        raise VideoWriterInitError(
            f"No se pudo inicializar VideoWriter en {self.settings.recordings_dir} "
            f"con los códecs {attempts}."
        )

    def _prepare_audio(
        self,
        base_stem: str,
    ) -> tuple[Any | None, Path | None]:
        if not self.settings.audio_enabled:
            return None, None

        wav_path = self.settings.recordings_dir / f".tmp_{base_stem}.wav"
        audio_rec = self._create_audio_recorder(wav_path)
        try:
            audio_rec.open()
            return audio_rec, wav_path
        except AudioUnavailableError as exc:
            if self.settings.audio_required:
                raise
            logger.warning("Audio omitido (%s); grabando solo video.", exc.message)
            with self._state_lock:
                self._last_error = exc.message
            wav_path.unlink(missing_ok=True)
            return None, None

    def _finalize_media(
        self,
        audio_rec: Any | None,
        temp_video_path: Path | None,
        temp_audio_path: Path | None,
        final_path: Path,
        frames_written: int,
    ) -> None:
        if frames_written == 0:
            for path in (temp_video_path, temp_audio_path, final_path):
                if path is not None and path.exists() and path.stat().st_size == 0:
                    path.unlink(missing_ok=True)
            return

        if temp_video_path is None or not temp_video_path.exists() or temp_video_path.stat().st_size == 0:
            return

        if audio_rec is None:
            temp_video_path.replace(final_path)
            return

        has_audio_samples = (
            temp_audio_path is not None
            and temp_audio_path.exists()
            and temp_audio_path.stat().st_size > 44
        )
        if not has_audio_samples:
            if temp_audio_path is not None:
                temp_audio_path.unlink(missing_ok=True)
            temp_video_path.replace(final_path)
            logger.warning("No se capturaron muestras de audio; video guardado en %s", final_path.name)
            return

        try:
            self._mux_fn(
                temp_video_path,
                temp_audio_path,
                final_path,
                audio_bitrate=self.settings.audio_bitrate,
                timeout=self.settings.mux_timeout,
            )
            temp_video_path.unlink(missing_ok=True)
            temp_audio_path.unlink(missing_ok=True)
        except MediaMuxError as exc:
            logger.error("%s", exc.message)
            with self._state_lock:
                self._last_error = exc.message
                self._stop_error = exc
        except Exception as exc:
            msg = f"Error inesperado al combinar video y audio: {exc}"
            logger.exception(msg)
            err = MediaMuxError(msg)
            with self._state_lock:
                self._last_error = msg
                self._stop_error = err

    def _await_audio_origin(
        self,
        cap: Any,
        audio_rec: Any,
        stop_event: threading.Event,
        frame_holder: dict[str, Any],
    ) -> float | None:
        """Drena fotogramas de la cámara mientras espera la primera muestra real de audio."""
        deadline = time.monotonic() + max(4.0, self.settings.warmup_timeout)
        while not stop_event.is_set():
            origin = audio_rec.poll_active()
            if origin is not None:
                return origin

            if time.monotonic() >= deadline:
                if self.settings.audio_required:
                    raise AudioUnavailableError(
                        f"El micrófono {self.settings.audio_device!r} no entregó muestras de audio a tiempo."
                    )
                logger.warning("Tiempo agotado esperando muestras de audio; grabando solo video.")
                return None

            t_read = time.monotonic()
            ret, frame = cap.read()
            if ret and frame is not None and frame.size > 0:
                frame_holder["frame"] = frame
            if time.monotonic() - t_read < 0.005:
                stop_event.wait(0.005)

        raise CameraUnavailableError("Grabación cancelada durante la sincronización inicial.")

    def _video_writer_loop(
        self,
        out: Any,
        width: int,
        height: int,
        fps: float,
        capture_start: float,
        frame_lock: threading.Lock,
        frame_holder: dict[str, Any],
        writer_stop: threading.Event,
        writer_stats: dict[str, int],
    ) -> None:
        """Escribe fotogramas a cadencia fija alineada al reloj real de audio."""
        frame_interval = 1.0 / fps
        max_frame_repeat = max(1, int(round(fps)))
        frames_written = 0

        while not writer_stop.is_set():
            next_slot = capture_start + (frames_written * frame_interval)
            wait_sec = next_slot - time.monotonic()
            if wait_sec > 0 and writer_stop.wait(wait_sec):
                break

            with frame_lock:
                frame = frame_holder["frame"]

            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height))

            elapsed = time.monotonic() - capture_start
            target_total = max(frames_written + 1, int(elapsed * fps) + 1)
            copies = min(target_total - frames_written, max_frame_repeat)
            for _ in range(copies):
                out.write(frame)
            frames_written += copies
            writer_stats["frames_written"] = frames_written

            with self._state_lock:
                self._frames_written = frames_written

    def _capture_loop(
        self,
        base_stem: str,
        stop_event: threading.Event,
        ready_event: threading.Event,
        startup_state: _StartupState,
    ) -> None:
        """Ejecuta lectura continua de cámara y escritura sincronizada de video y audio."""
        cap: Any = None
        out: Any = None
        audio_rec: Any = None
        temp_audio_path: Path | None = None
        temp_video_path: Path | None = None
        final_path = startup_state.file_path
        writer_stop = threading.Event()
        writer_thread: threading.Thread | None = None
        writer_stats = {"frames_written": 0}

        try:
            cap, first_frame = self._open_and_warmup(stop_event)
            audio_rec, temp_audio_path = self._prepare_audio(base_stem)

            height, width = first_frame.shape[:2]
            fps = self._resolve_fps(cap)
            out, final_path, write_path = self._open_writer(
                base_stem,
                fps,
                (width, height),
                use_temp=(audio_rec is not None),
            )
            if audio_rec is not None:
                temp_video_path = write_path

            frame_lock = threading.Lock()
            frame_holder: dict[str, Any] = {"frame": first_frame}

            if audio_rec is not None:
                audio_rec.start()
                audio_origin = self._await_audio_origin(cap, audio_rec, stop_event, frame_holder)
                if audio_origin is None:
                    audio_rec.stop()
                    audio_rec = None
                    if temp_audio_path is not None:
                        temp_audio_path.unlink(missing_ok=True)
                        temp_audio_path = None
                    capture_start = time.monotonic()
                else:
                    capture_start = audio_origin
            else:
                capture_start = time.monotonic()

            writer_thread = threading.Thread(
                target=self._video_writer_loop,
                args=(
                    out,
                    width,
                    height,
                    fps,
                    capture_start,
                    frame_lock,
                    frame_holder,
                    writer_stop,
                    writer_stats,
                ),
                name="video-writer",
                daemon=True,
            )
            writer_thread.start()

            startup_state.file_path = final_path
            startup_state.filename = final_path.name
            with self._state_lock:
                self._recording = True
                self._current_file = final_path.name
                self._current_path = final_path
                self._started_at = datetime.now(timezone.utc)
                self._started_monotonic = capture_start
                self._frames_written = writer_stats["frames_written"]
                self._active_audio_device = audio_rec.device_name if audio_rec else None
            ready_event.set()

            consecutive_failures = 0
            while not stop_event.is_set():
                t_read = time.monotonic()
                ret, frame = cap.read()
                if stop_event.is_set():
                    break

                if not ret or frame is None or frame.size == 0:
                    consecutive_failures += 1
                    if consecutive_failures >= self.settings.max_consecutive_failures:
                        msg = (
                            f"La cámara (índice {self.settings.camera_index}) dejó de entregar "
                            "fotogramas durante la grabación."
                        )
                        logger.error(msg)
                        with self._state_lock:
                            self._last_error = msg
                        break
                    stop_event.wait(0.03)
                    continue

                consecutive_failures = 0
                with frame_lock:
                    frame_holder["frame"] = frame

                # Evita uso innecesario de CPU solo si el origen entrega fotogramas instantáneos (p. ej. mocks).
                if time.monotonic() - t_read < 0.005:
                    stop_event.wait(0.005)

        except RecorderError as exc:
            logger.error("Error del grabador: %s", exc.message)
            if not ready_event.is_set():
                startup_state.error = exc
                ready_event.set()
            else:
                with self._state_lock:
                    self._last_error = exc.message
        except Exception as exc:
            logger.exception("Error inesperado en el hilo de captura")
            if not ready_event.is_set():
                startup_state.error = CameraUnavailableError(
                    f"Error inesperado al iniciar la grabación: {exc}"
                )
                ready_event.set()
            else:
                with self._state_lock:
                    self._last_error = f"Error inesperado durante la captura: {exc}"
        finally:
            writer_stop.set()
            if writer_thread is not None:
                writer_thread.join(timeout=self.settings.stop_timeout)

            if audio_rec is not None:
                try:
                    audio_rec.stop()
                except Exception:
                    logger.exception("Error al detener el micrófono")

            if out is not None:
                self._safe_release(out, "VideoWriter")
            if cap is not None:
                self._safe_release(cap, "VideoCapture")

            frames_written = writer_stats["frames_written"]
            self._finalize_media(
                audio_rec,
                temp_video_path,
                temp_audio_path,
                final_path,
                frames_written,
            )

            with self._state_lock:
                if frames_written > 0 and final_path.exists() and final_path.stat().st_size > 0:
                    self._last_file = final_path.name
                self._recording = False
                self._current_file = None
                self._current_path = None
                self._started_at = None
                self._started_monotonic = None
                self._active_audio_device = None
                if self._thread is threading.current_thread():
                    self._thread = None
                    self._stop_event = None

    @staticmethod
    def _safe_release(resource: Any, label: str) -> None:
        try:
            resource.release()
        except Exception:
            logger.exception("Error al liberar %s", label)
