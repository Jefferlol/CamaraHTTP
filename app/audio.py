"""Captura de audio headless basada en sounddevice (PortAudio) y wave PCM16."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import logging
from pathlib import Path
import queue
import threading
import time
from typing import Any
import wave

import sounddevice as sd

from app.exceptions import AudioUnavailableError

logger = logging.getLogger("camara_http.audio")

_HOSTAPI_PRIORITY: dict[str, int] = {
    "windows wasapi": 0,
    "core audio": 0,
    "alsa": 0,
    "pulse": 0,
    "windows directsound": 1,
    "jack": 1,
    "mme": 2,
    "windows wdm-ks": 99,
}


@dataclass(frozen=True, slots=True)
class AudioDeviceCandidate:
    """Representa un dispositivo de entrada compatible ordenado por prioridad de API."""

    index: int
    name: str
    hostapi_name: str
    max_input_channels: int
    default_samplerate: int


def _refresh_portaudio() -> None:
    """Re-inicializa PortAudio para detectar micrófonos USB conectados recientemente."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        logger.debug("No se pudo refrescar la lista interna de PortAudio", exc_info=True)


def list_input_devices(
    *,
    devices_fn: Callable[[], Sequence[dict[str, Any]]] | None = None,
    hostapis_fn: Callable[[], Sequence[dict[str, Any]]] | None = None,
) -> list[AudioDeviceCandidate]:
    """Lista los dispositivos de entrada ordenados por estabilidad de API de audio."""
    devices = (devices_fn or sd.query_devices)()
    hostapis = (hostapis_fn or sd.query_hostapis)()

    candidates: list[AudioDeviceCandidate] = []
    for idx, dev in enumerate(devices):
        max_in = int(dev.get("max_input_channels", 0))
        if max_in <= 0:
            continue
        host_idx = int(dev.get("hostapi", 0))
        host_name = (
            str(hostapis[host_idx].get("name", "unknown"))
            if 0 <= host_idx < len(hostapis)
            else "unknown"
        )
        samplerate = int(round(float(dev.get("default_samplerate") or 44100.0)))
        candidates.append(
            AudioDeviceCandidate(
                index=int(dev.get("index", idx)),
                name=str(dev.get("name", f"Device {idx}")),
                hostapi_name=host_name,
                max_input_channels=max_in,
                default_samplerate=max(8000, samplerate),
            )
        )

    return sorted(
        candidates,
        key=lambda c: (_HOSTAPI_PRIORITY.get(c.hostapi_name.lower(), 10), c.index),
    )


def resolve_input_devices(
    device_query: str,
    *,
    devices_fn: Callable[[], Sequence[dict[str, Any]]] | None = None,
    hostapis_fn: Callable[[], Sequence[dict[str, Any]]] | None = None,
    default_input_index: int | None = None,
) -> list[AudioDeviceCandidate]:
    """Busca dispositivos de entrada que coincidan con un nombre, índice o 'default'."""
    query = device_query.strip()
    candidates = list_input_devices(devices_fn=devices_fn, hostapis_fn=hostapis_fn)

    # Si no se especificaron mocks y no apareció el dispositivo, refresca PortAudio por si se conectó tras arrancar.
    if devices_fn is None and query and not query.isdigit() and query.lower() != "default":
        needle = query.lower()
        if not any(needle in c.name.lower() for c in candidates):
            _refresh_portaudio()
            candidates = list_input_devices()

    if query.isdigit():
        target_idx = int(query)
        return [c for c in candidates if c.index == target_idx]

    if query.lower() == "default":
        default_idx = default_input_index if default_input_index is not None else sd.default.device[0]
        if default_idx is None or int(default_idx) < 0:
            return []
        return [c for c in candidates if c.index == int(default_idx)]

    needle = query.lower()
    return [c for c in candidates if needle in c.name.lower()]


def _default_stream_factory(**kwargs: Any) -> Any:
    return sd.RawInputStream(**kwargs)


class AudioRecorder:
    """Graba audio PCM16 en un archivo WAV temporal usando un hilo escritor separado del callback."""

    def __init__(
        self,
        wav_path: Path,
        *,
        device_query: str = "TE-9072",
        desired_channels: int = 2,
        desired_sample_rate: int | None = None,
        stream_factory: Callable[..., Any] | None = None,
        devices_fn: Callable[[], Sequence[dict[str, Any]]] | None = None,
        hostapis_fn: Callable[[], Sequence[dict[str, Any]]] | None = None,
    ) -> None:
        self.wav_path = wav_path
        self.device_query = device_query
        self.desired_channels = max(1, desired_channels)
        self.desired_sample_rate = desired_sample_rate
        self._stream_factory = stream_factory or _default_stream_factory
        self._devices_fn = devices_fn
        self._hostapis_fn = hostapis_fn

        self._queue: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._stream: Any = None
        self._wav_file: wave.Wave_write | None = None
        self._writer_thread: threading.Thread | None = None
        self._stopping = False
        self._channels = 1
        self._sample_rate = 44100
        self._samples_written = 0
        self._first_callback_seen = False
        self._first_callback_origin: float | None = None
        self._first_callback_event = threading.Event()
        self.selected_device: AudioDeviceCandidate | None = None

    @property
    def device_name(self) -> str | None:
        """Nombre amigable del dispositivo abierto, si existe."""
        return self.selected_device.name if self.selected_device else None

    @property
    def samples_written(self) -> int:
        """Cantidad aproximada de frames de audio volcados al WAV."""
        return self._samples_written

    def open(self) -> AudioDeviceCandidate:
        """Localiza el micrófono solicitado y prepara el stream y el archivo WAV."""
        candidates = resolve_input_devices(
            self.device_query,
            devices_fn=self._devices_fn,
            hostapis_fn=self._hostapis_fn,
        )
        if not candidates:
            raise AudioUnavailableError(
                f"No se encontró ningún micrófono que coincida con {self.device_query!r}. "
                "Verifique que esté conectado y habilitado en Windows."
            )

        errors: list[str] = []
        for candidate in candidates:
            channels = min(self.desired_channels, candidate.max_input_channels)
            sample_rate = self.desired_sample_rate or candidate.default_samplerate
            wav_file: wave.Wave_write | None = None
            stream: Any = None

            try:
                wav_file = wave.open(str(self.wav_path), "wb")
                wav_file.setnchannels(channels)
                wav_file.setsampwidth(2)  # PCM 16-bit
                wav_file.setframerate(sample_rate)

                stream = self._stream_factory(
                    device=candidate.index,
                    samplerate=sample_rate,
                    channels=channels,
                    dtype="int16",
                    callback=self._audio_callback,
                )
                self._wav_file = wav_file
                self._stream = stream
                self._channels = channels
                self._sample_rate = sample_rate
                self.selected_device = candidate
                logger.info(
                    "Micrófono abierto: [%d] %s (%s, %d Hz, %d ch)",
                    candidate.index,
                    candidate.name,
                    candidate.hostapi_name,
                    sample_rate,
                    channels,
                )
                return candidate
            except Exception as exc:
                errors.append(f"{candidate.hostapi_name} ({candidate.index}): {exc}")
                self._close_stream_quietly(stream)
                self._close_wav_quietly(wav_file)
                if self.wav_path.exists():
                    self.wav_path.unlink(missing_ok=True)

        raise AudioUnavailableError(
            f"El micrófono {self.device_query!r} no pudo abrirse ({'; '.join(errors)})."
        )

    def start(self) -> None:
        """Inicia la captura de audio en tiempo real y el hilo de escritura a disco."""
        if self._stream is None or self._wav_file is None:
            raise AudioUnavailableError("El flujo de audio no fue inicializado antes de iniciar.")

        self._stopping = False
        self._samples_written = 0
        self._first_callback_seen = False
        self._first_callback_origin = None
        self._first_callback_event.clear()
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="audio-writer",
            daemon=True,
        )
        self._writer_thread.start()

        try:
            self._stream.start()
        except Exception as exc:
            self.stop()
            raise AudioUnavailableError(f"Error al iniciar la captura del micrófono: {exc}") from exc

    def poll_active(self) -> float | None:
        """Retorna el instante t0 de la primera muestra si ya llegó, o None en caso contrario."""
        if self._first_callback_event.is_set():
            return self._first_callback_origin
        return None

    def wait_until_active(
        self,
        *,
        timeout: float = 4.0,
        stop_event: threading.Event | None = None,
    ) -> float:
        """Espera al primer bloque real del micrófono y retorna el instante t0 de la muestra 0."""
        deadline = time.monotonic() + timeout
        while not self._first_callback_event.is_set():
            if stop_event is not None and stop_event.is_set():
                self.stop()
                raise AudioUnavailableError("Captura de audio cancelada durante la inicialización.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.stop()
                raise AudioUnavailableError(
                    f"El micrófono {self.device_query!r} no entregó muestras de audio a tiempo."
                )
            self._first_callback_event.wait(min(0.05, remaining))

        assert self._first_callback_origin is not None
        return self._first_callback_origin

    def stop(self) -> None:
        """Detiene el micrófono, vacía la cola de audio pendiente y cierra el WAV."""
        self._stopping = True
        self._close_stream_quietly(self._stream)
        self._stream = None

        if self._writer_thread is not None:
            self._queue.put(None)
            self._writer_thread.join(timeout=5.0)
            self._writer_thread = None

        self._close_wav_quietly(self._wav_file)
        self._wav_file = None

    def _audio_callback(
        self,
        indata: Any,
        frames: int,
        _time_info: Any,
        status: Any,
    ) -> None:
        now = time.monotonic()
        if status:
            logger.debug("Estado de PortAudio en captura: %s", status)
        if not self._stopping:
            if not self._first_callback_seen:
                self._first_callback_seen = True
                self._first_callback_origin = now - (frames / self._sample_rate)
                self._first_callback_event.set()
            self._queue.put(bytes(indata))

    def _writer_loop(self) -> None:
        bytes_per_frame = self._channels * 2
        while True:
            chunk = self._queue.get()
            if chunk is None:
                break
            if self._wav_file is not None and chunk:
                try:
                    self._wav_file.writeframes(chunk)
                    self._samples_written += len(chunk) // bytes_per_frame
                except Exception:
                    logger.exception("Error escribiendo bloques de audio WAV")

    @staticmethod
    def _close_stream_quietly(stream: Any) -> None:
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    @staticmethod
    def _close_wav_quietly(wav_file: wave.Wave_write | None) -> None:
        if wav_file is None:
            return
        try:
            wav_file.close()
        except Exception:
            pass
