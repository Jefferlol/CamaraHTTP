# CamaraHTTP

Servicio backend local ligero, modular y completamente silencioso (**headless**) para grabar video desde una cámara web junto con audio sincronizado de micrófono a través de peticiones HTTP en red local.

Construido con **FastAPI**, **Uvicorn**, **OpenCV (`opencv-python-headless`)**, **`sounddevice`** e **`imageio-ffmpeg`**, sin abrir ninguna ventana ni interfaz gráfica en el equipo anfitrión.

---

## Características principales

- **100% Headless y silencioso:** cero ventanas de OpenCV (`cv2.imshow` no existe en `opencv-python-headless`), compatible con ejecución oculta (`pythonw.exe` en Windows o `nohup`/`systemd` en Linux) y ejecución de `ffmpeg` sin consola (`CREATE_NO_WINDOW`).
- **Interruptor único por HTTP (`POST /record/toggle`):** inicia y detiene la grabación de forma atómica y segura ante peticiones concurrentes.
- **Audio y video sincronizados:** captura el micrófono configurado (por defecto **`TE-9072`** con prioridad **Windows WASAPI → DirectSound → MME**), compensa el retardo de arranque del controlador de audio drenando la cámara y alinea la cadencia de fotogramas al reloj real antes de multiplexar (`-c:v copy -c:a aac`) en `./recordings/rec_YYYYMMDD_HHMMSS.mp4`.
- **Manejo robusto de fallos:** devuelve errores JSON estructurados (`503`, `500`, `504`) si la cámara o el micrófono están desconectados u ocupados, recupera grabaciones parciales ante desconexiones inesperadas y finaliza grabaciones activas al detener el servidor (`lifespan`).
- **Configurable sin tocar código:** todas las opciones pueden ajustarse mediante variables de entorno `CAMARA_*` o argumentos CLI.

---

## Estructura del repositorio

```text
CamaraHTTP/
├── main.py                  # Punto de entrada Uvicorn (seguro para pythonw.exe sin stdout/stderr)
├── requirements.txt         # Dependencias mínimas de ejecución
├── app/
│   ├── __init__.py
│   ├── api.py               # Aplicación FastAPI, rutas HTTP y ciclo de vida (lifespan)
│   ├── audio.py             # Detección priorizada de micrófonos y captura WAV PCM16 en hilo dedicado
│   ├── config.py            # Configuración tipada (Settings) y variables de entorno CAMARA_*
│   ├── exceptions.py        # Excepciones de dominio con código HTTP asociado
│   ├── logging_config.py    # Logging rotativo seguro para procesos sin consola
│   ├── media.py             # Multiplexado silencioso de video y audio con ffmpeg
│   ├── recorder.py          # Coordinación multi-hilo de captura continua y escritura sincronizada
│   └── schemas.py           # Modelos Pydantic de respuesta
└── tests/
    └── test_recorder_api.py # Pruebas unitarias y de integración HTTP (15 tests)
```

---

## Requisitos e instalación

- **Python 3.11+** (probado en Windows con Python 3.15).
- En **Linux**, instala la librería del sistema para PortAudio antes de instalar las dependencias:
  ```bash
  sudo apt update && sudo apt install -y libportaudio2
  ```

### Pasos de instalación

1. Clonar el repositorio:
   ```bash
   git clone https://github.com/Jefferlol/CamaraHTTP.git
   cd CamaraHTTP
   ```

2. Crear un entorno virtual e instalar dependencias:

   **Windows (PowerShell):**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

   **Linux / macOS:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Ejecutar la suite de pruebas:
   ```powershell
   python -m unittest discover -s tests -v
   ```

---

## Uso y API HTTP

Al iniciar, el servidor escucha en **`0.0.0.0:3421`** para aceptar peticiones desde cualquier equipo de la red local (por ejemplo, `http://192.168.1.200:3421`).

### Endpoints

| Método | Ruta | Descripción |
| --- | --- | --- |
| `POST` | `/record/toggle` | Inicia la grabación si está en espera, o la detiene y guarda el archivo final |
| `GET` | `/record/status` | Retorna el estado actual (`recording` o `idle`), archivo, duración y micrófono |
| `GET` | `/audio/devices` | Lista los micrófonos detectados y el filtro configurado |
| `GET` | `/health` | Comprobación básica de disponibilidad del servicio |

### Ejemplos de respuesta

**Iniciar grabación (`POST /record/toggle`):**
```json
{
  "status": "recording",
  "message": "Grabación iniciada",
  "file": "rec_20261111_201500.mp4"
}
```

**Detener grabación (`POST /record/toggle`):**
```json
{
  "status": "idle",
  "message": "Grabación finalizada y guardada",
  "file": "rec_20261111_201500.mp4"
}
```

**Consultar estado (`GET /record/status`):**
```json
{
  "status": "recording",
  "recording": true,
  "message": "Grabación en curso",
  "file": "rec_20261111_201500.mp4",
  "last_file": null,
  "started_at": "2026-11-12T01:15:00+00:00",
  "duration_seconds": 12.45,
  "frames_written": 249,
  "audio_enabled": true,
  "audio_device": "Micrófono (TE-9072)",
  "recordings_dir": "./recordings",
  "last_error": null
}
```

**Error controlado (cámara o micrófono desconectado/ocupado, HTTP `503`):**
```json
{
  "status": "error",
  "error_code": "audio_unavailable",
  "message": "No se encontró ningún micrófono que coincida con 'TE-9072'. Verifique que esté conectado y habilitado en Windows."
}
```

### Ejemplos rápidos desde consola

```bash
curl -X POST http://192.168.1.200:3421/record/toggle
curl http://192.168.1.200:3421/record/status
curl http://192.168.1.200:3421/audio/devices
```

---

## Configuración (variables de entorno y CLI)

Opciones disponibles mediante argumentos (`python main.py --help`) o variables de entorno:

| Variable | Por defecto | Descripción |
| --- | --- | --- |
| `CAMARA_HOST` | `0.0.0.0` | IP de escucha del servidor |
| `CAMARA_PORT` | `3421` | Puerto HTTP |
| `CAMARA_INDEX` | `0` | Índice de la cámara web en OpenCV |
| `CAMARA_BACKEND` | `auto` | Backend OpenCV (`auto`, `dshow`, `msmf`, `v4l2`, `avfoundation`) |
| `CAMARA_AUDIO_ENABLED` | `true` | Habilita captura de audio junto con el video |
| `CAMARA_AUDIO_DEVICE` | `TE-9072` | Subcadena del nombre, índice numérico o `default` |
| `CAMARA_AUDIO_REQUIRED` | `true` | Si es `true`, exige micrófono disponible; si es `false`, graba solo video al fallar |
| `CAMARA_AUDIO_CHANNELS` | `2` | Canales solicitados (se ajusta al máximo soportado por el micrófono) |
| `CAMARA_AUDIO_SAMPLE_RATE` | *(nativo)* | Frecuencia en Hz (por defecto usa la nativa del dispositivo) |
| `CAMARA_AUDIO_BITRATE` | `192k` | Bitrate AAC al multiplexar archivos MP4 |
| `CAMARA_RECORDINGS_DIR` | `./recordings` | Directorio donde se almacenan los videos |
| `CAMARA_LOGS_DIR` | `./logs` | Directorio para los logs rotativos (`camara_http.log`) |
| `CAMARA_CODEC` | `mp4v` | Códec FourCC primario |
| `CAMARA_EXTENSION` | `.mp4` | Extensión de salida primaria |
| `CAMARA_FALLBACK_CODEC` | `XVID` | Códec alternativo si falla el primario |
| `CAMARA_FALLBACK_EXTENSION` | `.avi` | Extensión alternativa |
| `CAMARA_DEFAULT_FPS` | `20.0` | FPS objetivo cuando la cámara no reporta frecuencia |
| `CAMARA_FRAME_WIDTH` | *(nativo)* | Ancho deseado opcional en píxeles |
| `CAMARA_FRAME_HEIGHT` | *(nativo)* | Alto deseado opcional en píxeles |
| `CAMARA_WARMUP_TIMEOUT` | `5.0` | Tiempo máximo de espera al primer fotograma |
| `CAMARA_MUX_TIMEOUT` | `120.0` | Tiempo máximo para combinar video y audio con `ffmpeg` |

*(Si deseas grabar solo video en una ejecución puntual, puedes usar `python main.py --no-audio` o seleccionar otro micrófono con `--audio-device "Nombre"`.)*

---

## Ejecución oculta en segundo plano (sin ventana de terminal)

### Windows (`pythonw.exe`)

1. **Iniciar en segundo plano sin consola (PowerShell):**
   ```powershell
   Start-Process -FilePath ".\.venv\Scripts\pythonw.exe" -ArgumentList "main.py" -WorkingDirectory "$PWD" -WindowStyle Hidden
   ```

2. **Verificar estado y logs:**
   ```powershell
   Invoke-RestMethod http://127.0.0.1:3421/record/status
   Get-Content .\logs\camara_http.log -Encoding UTF8 -Tail 20
   ```

3. **Detener el proceso oculto:**
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
     Where-Object { $_.CommandLine -like '*main.py*' } |
     ForEach-Object { Stop-Process -Id $_.ProcessId }
   ```

4. **Permitir acceso desde la red local en el Firewall de Windows (como Administrador):**
   ```powershell
   Start-Process powershell -Verb RunAs -ArgumentList '-NoExit','-Command','New-NetFirewallRule -DisplayName "CamaraHTTP 3421" -Direction Inbound -Protocol TCP -LocalPort 3421 -Action Allow'
   ```

5. **Arranque automático oculto al iniciar sesión (`shell:startup`):**
   ```powershell
   $startup = [Environment]::GetFolderPath('Startup')
   $repo = (Get-Location).Path
   $ws = New-Object -ComObject WScript.Shell
   $lnk = $ws.CreateShortcut("$startup\CamaraHTTP.lnk")
   $lnk.TargetPath = "$repo\.venv\Scripts\pythonw.exe"
   $lnk.Arguments = "`"$repo\main.py`""
   $lnk.WorkingDirectory = $repo
   $lnk.WindowStyle = 7
   $lnk.Save()
   ```

### Linux (`nohup` o `systemd`)

- **Ejecución rápida en segundo plano:**
  ```bash
  nohup .venv/bin/python main.py >/dev/null 2>&1 &
  ```
- **Servicio `systemd` de usuario (`~/.config/systemd/user/camara-http.service`):**
  ```ini
  [Unit]
  Description=CamaraHTTP Headless Recorder
  After=network.target sound.target

  [Service]
  WorkingDirectory=/ruta/a/CamaraHTTP
  ExecStart=/ruta/a/CamaraHTTP/.venv/bin/python main.py
  Restart=on-failure
  RestartSec=3

  [Install]
  WantedBy=default.target
  ```
  Activar con:
  ```bash
  systemctl --user daemon-reload
  systemctl --user enable --now camara-http.service
  ```
