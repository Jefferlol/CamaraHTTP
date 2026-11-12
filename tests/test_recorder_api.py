"""Pruebas unitarias y de integración HTTP para el servicio CamaraHTTP."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import wave

import cv2
import imageio_ffmpeg
import numpy as np
import uvicorn

from app.api import create_app
from app.audio import AudioRecorder, resolve_input_devices
from app.config import Settings
from app.exceptions import AudioUnavailableError, CameraUnavailableError
from app.logging_config import build_logging_config, setup_logging
from app.recorder import CameraRecorder


class FakeVideoCapture:
    """Simulador de cv2.VideoCapture para pruebas sin hardware físico."""

    def __init__(
        self,
        *,
        opened: bool = True,
        fps: float = 20.0,
        width: int = 160,
        height: int = 120,
        fail_after_frames: int | None = None,
        always_fail_read: bool = False,
    ) -> None:
        self._opened = opened
        self._fps = fps
        self._width = width
        self._height = height
        self._fail_after_frames = fail_after_frames
        self._always_fail_read = always_fail_read
        self.frames_served = 0
        self.released = False

    def isOpened(self) -> bool:
        return self._opened

    def get(self, prop_id: int) -> float:
        if prop_id == cv2.CAP_PROP_FPS:
            return self._fps
        return 0.0

    def set(self, _prop_id: int, _value: float) -> bool:
        return True

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self._opened or self._always_fail_read:
            return False, None
        if self._fail_after_frames is not None and self.frames_served >= self._fail_after_frames:
            return False, None

        self.frames_served += 1
        frame = np.zeros((self._height, self._width, 3), dtype=np.uint8)
        frame[:, :, 1] = (self.frames_served * 15) % 255
        return True, frame

    def release(self) -> None:
        self.released = True
        self._opened = False


class FakeVideoWriter:
    """Simulador mínimo de cv2.VideoWriter para probar fallback de códecs."""

    def __init__(self, *, opened: bool = True) -> None:
        self._opened = opened
        self.written_frames = 0
        self.released = False

    def isOpened(self) -> bool:
        return self._opened

    def write(self, _frame: np.ndarray) -> None:
        self.written_frames += 1

    def release(self) -> None:
        self.released = True


class FakePortAudioStream:
    """Simulador de sounddevice.RawInputStream que emite bloques PCM16 sintéticos."""

    def __init__(self, *, samplerate: int, channels: int, callback, initial_delay: float = 0.0) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.callback = callback
        self.initial_delay = initial_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.closed = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._emit_loop, daemon=True)
        self._thread.start()

    def _emit_loop(self) -> None:
        if self.initial_delay > 0 and self._stop.wait(self.initial_delay):
            return
        frames_per_chunk = max(160, self.samplerate // 20)  # ~50ms por bloque
        t = np.arange(frames_per_chunk, dtype=np.float32) / self.samplerate
        tone = (np.sin(2.0 * np.pi * 440.0 * t) * 4000).astype(np.int16)
        if self.channels > 1:
            pcm = np.repeat(tone[:, None], self.channels, axis=1).tobytes()
        else:
            pcm = tone.tobytes()

        while not self._stop.is_set():
            self.callback(pcm, frames_per_chunk, None, None)
            self._stop.wait(0.04)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def close(self) -> None:
        self.closed = True


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_json(method: str, url: str) -> tuple[int, dict]:
    req = Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def _probe_streams_with_ffmpeg(media_path: Path) -> str:
    """Inspecciona pistas de video y audio usando el binario ffmpeg incluido."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(media_path)],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    return res.stderr or res.stdout


class AudioResolutionAndStreamTests(unittest.TestCase):
    """Pruebas de selección del micrófono TE-9072 y escritura WAV sin hardware."""

    def test_resolve_te9072_prioritizes_wasapi_over_directsound_mme_and_wdmks(self) -> None:
        hostapis = [
            {"name": "MME"},
            {"name": "Windows DirectSound"},
            {"name": "Windows WASAPI"},
            {"name": "Windows WDM-KS"},
        ]
        devices = [
            {"index": 0, "name": "Otro Mic", "hostapi": 2, "max_input_channels": 2, "default_samplerate": 48000.0},
            {"index": 2, "name": "Micrófono (TE-9072)", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 44100.0},
            {"index": 9, "name": "Micrófono (TE-9072)", "hostapi": 1, "max_input_channels": 2, "default_samplerate": 44100.0},
            {"index": 18, "name": "Micrófono (TE-9072)", "hostapi": 2, "max_input_channels": 2, "default_samplerate": 48000.0},
            {"index": 19, "name": "Micrófono (TE-9072)", "hostapi": 3, "max_input_channels": 2, "default_samplerate": 44100.0},
        ]

        matches = resolve_input_devices(
            "TE-9072",
            devices_fn=lambda: devices,
            hostapis_fn=lambda: hostapis,
        )
        self.assertEqual([m.index for m in matches], [18, 9, 2, 19])
        self.assertEqual(matches[0].hostapi_name, "Windows WASAPI")
        self.assertEqual(matches[0].default_samplerate, 48000)

    def test_audio_recorder_falls_back_when_first_hostapi_fails_and_writes_wav(self) -> None:
        hostapis = [{"name": "Windows WASAPI"}, {"name": "Windows DirectSound"}]
        devices = [
            {"index": 18, "name": "Micrófono (TE-9072)", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 48000.0},
            {"index": 9, "name": "Micrófono (TE-9072)", "hostapi": 1, "max_input_channels": 2, "default_samplerate": 44100.0},
        ]
        created_streams: list[FakePortAudioStream] = []

        def stream_factory(**kwargs):
            if kwargs["device"] == 18:
                raise RuntimeError("WASAPI ocupado")
            stream = FakePortAudioStream(
                samplerate=kwargs["samplerate"],
                channels=kwargs["channels"],
                callback=kwargs["callback"],
            )
            created_streams.append(stream)
            return stream

        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = Path(tmpdir) / "test.wav"
            rec = AudioRecorder(
                wav_path,
                device_query="TE-9072",
                desired_channels=2,
                stream_factory=stream_factory,
                devices_fn=lambda: devices,
                hostapis_fn=lambda: hostapis,
            )
            selected = rec.open()
            self.assertEqual(selected.index, 9)
            rec.start()
            time.sleep(0.2)
            rec.stop()

            self.assertTrue(created_streams[0].closed)
            self.assertGreater(rec.samples_written, 0)
            with wave.open(str(wav_path), "rb") as wf:
                self.assertEqual(wf.getnchannels(), 2)
                self.assertEqual(wf.getframerate(), 44100)
                self.assertGreater(wf.getnframes(), 0)


class CameraRecorderTests(unittest.TestCase):
    """Pruebas del ciclo de grabación, códecs reales, muxing A/V y manejo de errores."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name)
        self.settings = Settings(
            host="127.0.0.1",
            port=3421,
            recordings_dir=self.base_dir / "recordings",
            logs_dir=self.base_dir / "logs",
            warmup_timeout=0.4,
            stop_timeout=3.0,
            max_consecutive_failures=3,
            default_fps=15.0,
            audio_enabled=False,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_audio_factory(self, *, fail_open: bool = False, initial_delay: float = 0.0):
        hostapis = [{"name": "Windows WASAPI"}]
        devices = [] if fail_open else [
            {"index": 18, "name": "Micrófono (TE-9072)", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 48000.0},
        ]

        def factory(wav_path: Path, cfg: Settings) -> AudioRecorder:
            return AudioRecorder(
                wav_path,
                device_query=cfg.audio_device,
                desired_channels=cfg.audio_channels,
                desired_sample_rate=cfg.audio_sample_rate,
                stream_factory=lambda **kw: FakePortAudioStream(
                    samplerate=kw["samplerate"],
                    channels=kw["channels"],
                    callback=kw["callback"],
                    initial_delay=initial_delay,
                ),
                devices_fn=lambda: devices,
                hostapis_fn=lambda: hostapis,
            )

        return factory

    def test_audio_startup_delay_does_not_desync_video_duration(self) -> None:
        audio_settings = Settings(
            recordings_dir=self.base_dir / "recordings",
            logs_dir=self.base_dir / "logs",
            warmup_timeout=0.5,
            stop_timeout=3.0,
            default_fps=20.0,
            audio_enabled=True,
            audio_device="TE-9072",
            audio_required=True,
        )
        fake_cap = FakeVideoCapture(fps=20.0, width=160, height=120)
        recorder = CameraRecorder(
            audio_settings,
            capture_factory=lambda _idx, _backend: fake_cap,
            audio_factory=self._make_audio_factory(initial_delay=0.55),
        )

        recorder.toggle()
        time.sleep(0.6)
        stop_res = recorder.toggle()
        saved_path = audio_settings.recordings_dir / stop_res.file

        verify_cap = cv2.VideoCapture(str(saved_path))
        try:
            frame_count = verify_cap.get(cv2.CAP_PROP_FRAME_COUNT)
            fps = verify_cap.get(cv2.CAP_PROP_FPS)
        finally:
            verify_cap.release()

        video_duration = frame_count / fps
        # Sin compensación de arranque de audio (~0.55s), el video habría quedado >1.1s mientras el audio ~0.6s.
        self.assertGreaterEqual(video_duration, 0.45)
        self.assertLessEqual(video_duration, 0.85)

    def test_toggle_start_and_stop_with_audio_muxes_playable_mp4_with_video_and_aac(self) -> None:
        audio_settings = Settings(
            host="127.0.0.1",
            port=3421,
            recordings_dir=self.base_dir / "recordings",
            logs_dir=self.base_dir / "logs",
            warmup_timeout=0.4,
            stop_timeout=3.0,
            default_fps=15.0,
            audio_enabled=True,
            audio_device="TE-9072",
            audio_required=True,
        )
        fake_cap = FakeVideoCapture(fps=15.0, width=160, height=120)
        recorder = CameraRecorder(
            audio_settings,
            capture_factory=lambda _idx, _backend: fake_cap,
            audio_factory=self._make_audio_factory(fail_open=False),
        )

        start_res = recorder.toggle()
        self.assertEqual(start_res.status, "recording")
        self.assertEqual(start_res.message, "Grabación iniciada")
        self.assertTrue(start_res.file.endswith(".mp4"))

        time.sleep(0.35)
        active = recorder.get_status()
        self.assertTrue(active.recording)
        self.assertTrue(active.audio_enabled)
        self.assertEqual(active.audio_device, "Micrófono (TE-9072)")

        stop_res = recorder.toggle()
        self.assertEqual(stop_res.status, "idle")
        self.assertEqual(stop_res.message, "Grabación finalizada y guardada")
        self.assertEqual(stop_res.file, start_res.file)
        self.assertTrue(fake_cap.released)

        # No deben quedar archivos temporales .tmp_*
        remaining_files = [p.name for p in audio_settings.recordings_dir.iterdir()]
        self.assertEqual(remaining_files, [stop_res.file])

        saved_path = audio_settings.recordings_dir / stop_res.file
        self.assertGreater(saved_path.stat().st_size, 0)

        # Verifica pista de video con OpenCV
        verify_cap = cv2.VideoCapture(str(saved_path))
        try:
            self.assertTrue(verify_cap.isOpened())
            ok, frame = verify_cap.read()
            self.assertTrue(ok)
            self.assertEqual(frame.shape[:2], (120, 160))
        finally:
            verify_cap.release()

        # Verifica que el MP4 final contiene tanto Video como Audio (AAC)
        probe_output = _probe_streams_with_ffmpeg(saved_path)
        self.assertIn("Video:", probe_output)
        self.assertIn("Audio: aac", probe_output)

    def test_missing_required_microphone_raises_503_and_releases_camera(self) -> None:
        audio_settings = Settings(
            recordings_dir=self.base_dir / "recordings",
            logs_dir=self.base_dir / "logs",
            warmup_timeout=0.4,
            audio_enabled=True,
            audio_device="TE-9072",
            audio_required=True,
        )
        fake_cap = FakeVideoCapture(fps=15.0)
        recorder = CameraRecorder(
            audio_settings,
            capture_factory=lambda _idx, _backend: fake_cap,
            audio_factory=self._make_audio_factory(fail_open=True),
        )

        with self.assertRaises(AudioUnavailableError) as ctx:
            recorder.toggle()

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertTrue(fake_cap.released)
        self.assertFalse(recorder.is_recording)
        self.assertEqual(list(audio_settings.recordings_dir.glob("*")), [])

    def test_missing_optional_microphone_falls_back_to_video_only(self) -> None:
        audio_settings = Settings(
            recordings_dir=self.base_dir / "recordings",
            logs_dir=self.base_dir / "logs",
            warmup_timeout=0.4,
            audio_enabled=True,
            audio_device="TE-9072",
            audio_required=False,
        )
        fake_cap = FakeVideoCapture(fps=15.0)
        recorder = CameraRecorder(
            audio_settings,
            capture_factory=lambda _idx, _backend: fake_cap,
            audio_factory=self._make_audio_factory(fail_open=True),
        )

        start_res = recorder.toggle()
        time.sleep(0.2)
        stop_res = recorder.toggle()
        self.assertEqual(stop_res.file, start_res.file)
        self.assertTrue((audio_settings.recordings_dir / stop_res.file).exists())

    def test_toggle_start_and_stop_video_only_writes_playable_video(self) -> None:
        fake_cap = FakeVideoCapture(fps=15.0, width=160, height=120)
        recorder = CameraRecorder(
            self.settings,
            capture_factory=lambda _idx, _backend: fake_cap,
        )

        initial = recorder.get_status()
        self.assertEqual(initial.status, "idle")
        self.assertFalse(initial.recording)

        start_res = recorder.toggle()
        self.assertEqual(start_res.status, "recording")
        self.assertEqual(start_res.message, "Grabación iniciada")
        self.assertTrue(start_res.file.startswith("rec_"))

        time.sleep(0.25)
        active = recorder.get_status()
        self.assertEqual(active.status, "recording")
        self.assertTrue(active.recording)
        self.assertEqual(active.file, start_res.file)
        self.assertGreaterEqual(active.frames_written, 2)

        stop_res = recorder.toggle()
        self.assertEqual(stop_res.status, "idle")
        self.assertEqual(stop_res.message, "Grabación finalizada y guardada")
        self.assertEqual(stop_res.file, start_res.file)
        self.assertTrue(fake_cap.released)

        saved_path = self.settings.recordings_dir / stop_res.file
        self.assertTrue(saved_path.exists())
        self.assertGreater(saved_path.stat().st_size, 0)

    def test_disconnected_camera_raises_503_and_leaves_no_empty_files(self) -> None:
        fake_cap = FakeVideoCapture(opened=False)
        recorder = CameraRecorder(
            self.settings,
            capture_factory=lambda _idx, _backend: fake_cap,
        )

        with self.assertRaises(CameraUnavailableError) as ctx:
            recorder.toggle()

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertFalse(recorder.is_recording)
        self.assertEqual(list(self.settings.recordings_dir.glob("*")), [])

    def test_busy_camera_without_frames_fails_warmup_cleanly(self) -> None:
        fake_cap = FakeVideoCapture(opened=True, always_fail_read=True)
        recorder = CameraRecorder(
            self.settings,
            capture_factory=lambda _idx, _backend: fake_cap,
        )

        with self.assertRaises(CameraUnavailableError):
            recorder.toggle()

        self.assertTrue(fake_cap.released)
        self.assertFalse(recorder.is_recording)

    def test_mid_recording_disconnect_saves_partial_video_and_returns_idle(self) -> None:
        fake_cap = FakeVideoCapture(fps=30.0, fail_after_frames=4)
        recorder = CameraRecorder(
            self.settings,
            capture_factory=lambda _idx, _backend: fake_cap,
        )

        start_res = recorder.toggle()
        deadline = time.monotonic() + 3.0
        while recorder.is_recording and time.monotonic() < deadline:
            time.sleep(0.05)

        status = recorder.get_status()
        self.assertEqual(status.status, "idle")
        self.assertFalse(status.recording)
        self.assertEqual(status.last_file, start_res.file)
        self.assertIsNotNone(status.last_error)
        self.assertTrue(fake_cap.released)
        self.assertTrue((self.settings.recordings_dir / start_res.file).exists())

    def test_codec_fallback_uses_xvid_avi_when_primary_fails(self) -> None:
        fake_cap = FakeVideoCapture(fps=20.0)
        writers: list[FakeVideoWriter] = []

        def writer_factory(filepath: str, _fourcc: int, _fps: float, _size: tuple[int, int]):
            if filepath.endswith(".mp4"):
                Path(filepath).touch()
                w = FakeVideoWriter(opened=False)
            else:
                w = FakeVideoWriter(opened=True)
            writers.append(w)
            return w

        recorder = CameraRecorder(
            self.settings,
            capture_factory=lambda _idx, _backend: fake_cap,
            writer_factory=writer_factory,
        )

        start_res = recorder.toggle()
        self.assertTrue(start_res.file.endswith(".avi"))
        self.assertFalse(any(p.suffix == ".mp4" for p in self.settings.recordings_dir.iterdir()))

        stop_res = recorder.toggle()
        self.assertEqual(stop_res.file, start_res.file)
        self.assertTrue(writers[-1].released)
        self.assertGreaterEqual(writers[-1].written_frames, 1)


class HttpApiIntegrationTests(unittest.TestCase):
    """Pruebas de extremo a extremo sobre HTTP real con Uvicorn y FastAPI."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name)
        self.settings = Settings(
            host="127.0.0.1",
            port=_get_free_port(),
            recordings_dir=self.base_dir / "recordings",
            logs_dir=self.base_dir / "logs",
            warmup_timeout=0.5,
            stop_timeout=3.0,
            audio_enabled=True,
            audio_device="TE-9072",
            audio_required=True,
        )
        self.captures: list[FakeVideoCapture] = []
        self.allow_camera = True
        self.allow_audio = True

        def capture_factory(_idx: int, _backend: int) -> FakeVideoCapture:
            cap = FakeVideoCapture(opened=self.allow_camera, fps=15.0)
            self.captures.append(cap)
            return cap

        def audio_factory(wav_path: Path, cfg: Settings) -> AudioRecorder:
            hostapis = [{"name": "Windows WASAPI"}]
            devices = (
                [{"index": 18, "name": "Micrófono (TE-9072)", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 48000.0}]
                if self.allow_audio
                else []
            )
            return AudioRecorder(
                wav_path,
                device_query=cfg.audio_device,
                desired_channels=cfg.audio_channels,
                stream_factory=lambda **kw: FakePortAudioStream(
                    samplerate=kw["samplerate"],
                    channels=kw["channels"],
                    callback=kw["callback"],
                ),
                devices_fn=lambda: devices,
                hostapis_fn=lambda: hostapis,
            )

        self.recorder = CameraRecorder(
            self.settings,
            capture_factory=capture_factory,
            audio_factory=audio_factory,
        )
        self.app = create_app(settings=self.settings, recorder=self.recorder)
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=self.settings.port,
                log_config=None,
                access_log=False,
            )
        )
        self.server_thread = threading.Thread(target=self.server.run, daemon=True)
        self.server_thread.start()

        deadline = time.monotonic() + 5.0
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        self.base_url = f"http://127.0.0.1:{self.settings.port}"

    def tearDown(self) -> None:
        self.server.should_exit = True
        self.server_thread.join(timeout=5.0)
        self.tmp.cleanup()

    def test_http_endpoints_toggle_status_audio_devices_and_errors(self) -> None:
        code, health = _http_json("GET", f"{self.base_url}/health")
        self.assertEqual(code, 200)
        self.assertEqual(health, {"status": "ok", "service": "camara-http"})

        code, audio_list = _http_json("GET", f"{self.base_url}/audio/devices")
        self.assertEqual(code, 200)
        self.assertEqual(audio_list["configured_query"], "TE-9072")
        self.assertTrue(any("TE-9072" in d["name"] for d in audio_list["devices"]))

        code, status = _http_json("GET", f"{self.base_url}/record/status")
        self.assertEqual(code, 200)
        self.assertEqual(status["status"], "idle")
        self.assertFalse(status["recording"])
        self.assertTrue(status["audio_enabled"])

        # Inicia grabación con audio + video
        code, toggle_start = _http_json("POST", f"{self.base_url}/record/toggle")
        self.assertEqual(code, 200)
        self.assertEqual(set(toggle_start.keys()), {"status", "message", "file"})
        self.assertEqual(toggle_start["status"], "recording")
        self.assertEqual(toggle_start["message"], "Grabación iniciada")

        time.sleep(0.25)
        code, active_status = _http_json("GET", f"{self.base_url}/record/status")
        self.assertEqual(code, 200)
        self.assertEqual(active_status["status"], "recording")
        self.assertTrue(active_status["recording"])
        self.assertEqual(active_status["audio_device"], "Micrófono (TE-9072)")

        # Detiene grabación y ejecuta muxing real
        code, toggle_stop = _http_json("POST", f"{self.base_url}/record/toggle")
        self.assertEqual(code, 200)
        self.assertEqual(
            toggle_stop,
            {
                "status": "idle",
                "message": "Grabación finalizada y guardada",
                "file": toggle_start["file"],
            },
        )
        saved_file = self.settings.recordings_dir / toggle_start["file"]
        self.assertTrue(saved_file.exists())
        self.assertIn("Audio: aac", _probe_streams_with_ffmpeg(saved_file))

        # Error 503 cuando el micrófono no está disponible
        self.allow_audio = False
        code, mic_error = _http_json("POST", f"{self.base_url}/record/toggle")
        self.assertEqual(code, 503)
        self.assertEqual(mic_error["error_code"], "audio_unavailable")

        # Error 503 cuando la cámara está desconectada
        self.allow_audio = True
        self.allow_camera = False
        code, cam_error = _http_json("POST", f"{self.base_url}/record/toggle")
        self.assertEqual(code, 503)
        self.assertEqual(cam_error["error_code"], "camera_unavailable")

    def test_concurrent_toggles_transition_cleanly(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_http_json, "POST", f"{self.base_url}/record/toggle")
            f2 = pool.submit(_http_json, "POST", f"{self.base_url}/record/toggle")
            r1 = f1.result()
            r2 = f2.result()

        self.assertEqual(r1[0], 200)
        self.assertEqual(r2[0], 200)
        statuses = {r1[1]["status"], r2[1]["status"]}
        self.assertEqual(statuses, {"recording", "idle"})
        self.assertEqual(r1[1]["file"], r2[1]["file"])
        self.assertFalse(self.recorder.is_recording)

    def test_lifespan_shutdown_finalizes_active_recording(self) -> None:
        code, start_res = _http_json("POST", f"{self.base_url}/record/toggle")
        self.assertEqual(code, 200)
        time.sleep(0.2)

        self.server.should_exit = True
        self.server_thread.join(timeout=5.0)

        self.assertFalse(self.recorder.is_recording)
        self.assertTrue(self.captures[-1].released)
        saved_file = self.settings.recordings_dir / start_res["file"]
        self.assertTrue(saved_file.exists())
        self.assertGreater(saved_file.stat().st_size, 0)


class HeadlessLoggingTests(unittest.TestCase):
    """Verifica que el logging no falle cuando stdout/stderr son None (pythonw.exe)."""

    def test_logging_without_console_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "logs"
            with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
                config = build_logging_config(logs_dir, "INFO")
                self.assertNotIn("console", config["handlers"])

                setup_config = setup_logging(logs_dir, "INFO")
                self.assertIn("file", setup_config["handlers"])
                self.assertIsNotNone(sys.stdout)
                self.assertIsNotNone(sys.stderr)

                for logger_name in ("", "camara_http", "uvicorn", "uvicorn.error", "uvicorn.access"):
                    lg = logging.getLogger(logger_name)
                    for handler in list(lg.handlers):
                        handler.close()
                        lg.removeHandler(handler)
                sys.stdout.close()
                sys.stderr.close()


if __name__ == "__main__":
    unittest.main()
