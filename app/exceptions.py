"""Excepciones de dominio para el servicio de captura de video y audio."""


class RecorderError(Exception):
    """Excepción base del grabador con código HTTP asociado."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 500,
        error_code: str = "recorder_error",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


class CameraUnavailableError(RecorderError):
    """Se lanza cuando la cámara está desconectada, ocupada o no entrega fotogramas."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            status_code=503,
            error_code="camera_unavailable",
        )


class AudioUnavailableError(RecorderError):
    """Se lanza cuando el micrófono configurado está desconectado, ocupado o no existe."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            status_code=503,
            error_code="audio_unavailable",
        )


class VideoWriterInitError(RecorderError):
    """Se lanza cuando OpenCV no logra inicializar el archivo de salida de video."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            status_code=500,
            error_code="video_writer_error",
        )


class MediaMuxError(RecorderError):
    """Se lanza cuando ffmpeg falla al combinar las pistas de video y audio."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            status_code=500,
            error_code="media_mux_error",
        )


class RecorderTimeoutError(RecorderError):
    """Se lanza cuando la cámara o el hilo de grabación exceden el tiempo de espera."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            status_code=504,
            error_code="recorder_timeout",
        )
