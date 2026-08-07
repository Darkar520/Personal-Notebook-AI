"""Grabadores de audio: loopback WASAPI (+ micrófono) y reproducción de un WAV.

Mejoras deliberadas sobre el plan original (Task 2.2 / 3.5):

1. **Pista de micrófono separada (`capture_mode="loopback+mic"`).** El plan dependía de
   que el usuario activara *"Hear my own voice"* en Zoom (Solución A) o renunciaba a
   transcribir su propia voz (Solución B, `[tu voz no capturada]`). Grabar el micrófono
   en paralelo resuelve el problema de raíz: la voz propia entra siempre, sin tocar la
   configuración de Zoom, y además sabemos **con certeza local** en qué instantes habló
   el usuario. Esa máscara temporal (`mic_ranges`) marca `is_me` en el transcript sin
   pagar diarización extra ni depender de que la IA acierte.
2. **Solape entre chunks (`overlap_seconds`).** Cortar en seco cada 90 s parte palabras
   y hace que Deepgram pierda la frase del borde. Cada chunk arrastra unos segundos del
   anterior y la fusión descarta lo duplicado por punto medio.
3. **Reapertura automática del stream.** Si Zoom cambia de dispositivo o Windows suspende
   el endpoint, el plan moría con excepción. Aquí se reintenta y el hueco se rellena con
   silencio para que la línea de tiempo no se desplace.
4. **Escritura atómica de cada chunk** (`.part` + `replace`): el worker de transcripción
   nunca puede leer un WAV a medio escribir.
5. **Un único contrato `on_chunk(ChunkResult)`** compartido por el grabador real y el de
   pruebas, así el pipeline no tiene ramas `if modo_prueba`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from app.capture import devices, silence, wavio
from app.errors import AudioDeviceError

log = logging.getLogger(__name__)

RATE = wavio.TARGET_RATE
_READ_FRAMES = 2048
_REOPEN_DELAY_S = 2.0

# PortAudio **no es reentrante** al crear su contexto ni al abrir/cerrar streams. Abrir el
# loopback y el micrófono a la vez desde dos hilos (que es exactamente lo que hace el modo
# `loopback+mic`) provoca una violación de acceso nativa y mata el proceso entero —
# comprobado en Windows 11 con PyAudioWPatch 0.2.12.8. La lectura de streams ya abiertos sí
# puede ir en paralelo, así que basta serializar init / open / close con este cerrojo y
# compartir **una sola** instancia de PyAudio por grabador.
_PA_LOCK = threading.Lock()


@dataclass(slots=True)
class ChunkResult:
    """Un chunk de audio ya escrito en disco y listo para transcribir."""

    index: int
    path: Path
    start_t: float                 # segundos absolutos de sesión (inicio útil)
    duration: float                # segundos útiles (sin contar el solape)
    overlap_pre: float             # segundos de pre-roll incluidos en el fichero
    level: float = 0.0             # RMS del tramo útil
    mic_ranges: list[tuple[float, float]] = field(default_factory=list)
    silences: list[tuple[float, float]] = field(default_factory=list)
    final: bool = False

    @property
    def file_start_t(self) -> float:
        """Instante de sesión que corresponde al segundo 0 del fichero."""
        return self.start_t - self.overlap_pre


ChunkCallback = Callable[[ChunkResult], None]
ErrorCallback = Callable[[str], None]


class _Track:
    """Buffer de una pista de audio, alimentado por un hilo lector."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._blocks: list[np.ndarray] = []
        self._size = 0
        self.healthy = True
        self.detail = ""

    def push(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        with self._lock:
            self._blocks.append(samples)
            self._size += samples.size

    def pad(self, n_samples: int) -> None:
        if n_samples > 0:
            self.push(np.zeros(int(n_samples), dtype=np.float32))

    @property
    def size(self) -> int:
        with self._lock:
            return self._size

    def take(self, n_samples: int, *, pad: bool = False) -> np.ndarray:
        """Extrae los primeros `n_samples`. Con `pad`, completa con ceros si falta."""
        n_samples = int(n_samples)
        with self._lock:
            if self._size < n_samples and not pad:
                n_samples = self._size
            out = np.zeros(n_samples, dtype=np.float32)
            filled = 0
            while filled < n_samples and self._blocks:
                block = self._blocks[0]
                need = n_samples - filled
                if block.size <= need:
                    out[filled : filled + block.size] = block
                    filled += block.size
                    self._blocks.pop(0)
                    self._size -= block.size
                else:
                    out[filled:] = block[:need]
                    self._blocks[0] = block[need:]
                    self._size -= need
                    filled = n_samples
            return out


class _StreamReader(threading.Thread):
    """Lee un dispositivo WASAPI y vuelca float32 mono @16 kHz en un `_Track`.

    Nota: el evento de parada se llama `_stop_event` y no `_stop` a propósito.
    `threading.Thread` ya usa `self._stop` internamente (`_wait_for_tstate_lock` lo llama
    como método); sobrescribirlo con un `Event` hace que `join()` lance
    `TypeError: 'Event' object is not callable` justo al detener la grabación.
    """

    def __init__(self, track: _Track, device: devices.DeviceInfo, *, kind: str,
                 stop_event: threading.Event, pa, pyaudio_module,
                 on_error: ErrorCallback | None = None) -> None:
        super().__init__(name=f"audio-{kind}", daemon=True)
        self.track = track
        self.device = device
        self.kind = kind
        self._stop_event = stop_event
        self._on_error = on_error
        self._stream = None
        self._pa = pa
        self._pyaudio = pyaudio_module

    def run(self) -> None:  # pragma: no cover - requiere hardware
        last_ok = time.monotonic()
        try:
            while not self._stop_event.is_set():
                try:
                    if self._stream is None:
                        self._open()
                        gap = time.monotonic() - last_ok
                        if gap > 0.5:
                            # Rellenamos el hueco con silencio para que el resto de la
                            # sesión no se desplace en el tiempo.
                            self.track.pad(int(gap * RATE))
                        self.track.healthy = True
                        self.track.detail = ""
                    raw = self._stream.read(_READ_FRAMES, exception_on_overflow=False)
                    samples = wavio.pcm16_to_float(raw, self.device.channels)
                    if self.device.sample_rate != RATE:
                        samples = wavio.resample(samples, self.device.sample_rate, RATE)
                    self.track.push(samples)
                    last_ok = time.monotonic()
                except Exception as exc:
                    self.track.healthy = False
                    self.track.detail = str(exc)[:200]
                    log.warning("Pista %s: error de lectura (%s); reintentando", self.kind, exc)
                    if self._on_error:
                        self._on_error(f"{self.kind}: {exc}")
                    self._close_stream()
                    self._stop_event.wait(_REOPEN_DELAY_S)
        finally:
            self._close_stream()

    def _open(self) -> None:  # pragma: no cover - requiere hardware
        with _PA_LOCK:
            self._stream = self._pa.open(
                format=self._pyaudio.paInt16,
                channels=self.device.channels,
                rate=self.device.sample_rate,
                input=True,
                frames_per_buffer=_READ_FRAMES,
                input_device_index=self.device.index,
            )
        log.info(
            "Pista %s abierta: %s (%d ch @ %d Hz)",
            self.kind, self.device.name, self.device.channels, self.device.sample_rate,
        )

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        with _PA_LOCK:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass


class _Chunker:
    """Lógica común de troceado: solape, silencios, máscara de micro y escritura."""

    def __init__(self, out_dir: Path, *, chunk_seconds: float, overlap_seconds: float,
                 break_min_seconds: float, mic_gain: float) -> None:
        self.out_dir = Path(out_dir)
        self.chunk_seconds = float(chunk_seconds)
        self.overlap_seconds = max(0.0, float(overlap_seconds))
        self.break_min_seconds = float(break_min_seconds)
        self.mic_gain = float(mic_gain)
        self.index = 0
        self.position = 0.0                       # segundos útiles ya emitidos
        self._carry = np.zeros(0, dtype=np.float32)

    def emit(self, main: np.ndarray, mic: np.ndarray | None, *, final: bool = False
             ) -> ChunkResult | None:
        if main.size == 0:
            return None
        mixed = main
        if mic is not None and mic.size and self.mic_gain > 0:
            mixed = wavio.mix([(main, 1.0), (mic, self.mic_gain)])
        payload = np.concatenate([self._carry, mixed]) if self._carry.size else mixed
        overlap_pre = self._carry.size / RATE
        path = self.out_dir / f"chunk_{self.index:06d}.wav"
        wavio.write_wav(path, payload, RATE)

        start_t = self.position
        duration = mixed.size / RATE
        sil = silence.shift_ranges(
            silence.segment_silences(mixed, RATE, min_break_s=self.break_min_seconds), start_t
        )
        mic_ranges: list[tuple[float, float]] = []
        if mic is not None and mic.size:
            mic_ranges = silence.shift_ranges(silence.voiced_ranges(mic, RATE), start_t)

        result = ChunkResult(
            index=self.index,
            path=path,
            start_t=round(start_t, 3),
            duration=round(duration, 3),
            overlap_pre=round(overlap_pre, 3),
            level=silence.signal_level(mixed),
            mic_ranges=mic_ranges,
            silences=sil,
            final=final,
        )
        keep = int(self.overlap_seconds * RATE)
        self._carry = mixed[-keep:].copy() if keep and mixed.size > keep else (
            mixed.copy() if keep else np.zeros(0, dtype=np.float32)
        )
        self.index += 1
        self.position += duration
        return result


class LoopbackRecorder:
    """Graba lo que suena en el equipo (y opcionalmente el micrófono) en chunks WAV."""

    def __init__(
        self,
        out_dir: Path,
        *,
        chunk_seconds: float = 90.0,
        overlap_seconds: float = 8.0,
        capture_mode: str = "loopback+mic",
        output_device_index: int | None = None,
        mic_device_index: int | None = None,
        mic_gain: float = 1.0,
        break_min_seconds: float = 45.0,
        on_chunk: ChunkCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.capture_mode = capture_mode
        self.output_device_index = output_device_index
        self.mic_device_index = mic_device_index
        self.on_chunk = on_chunk
        self.on_error = on_error
        self._chunker = _Chunker(
            self.out_dir,
            chunk_seconds=chunk_seconds,
            overlap_seconds=overlap_seconds,
            break_min_seconds=break_min_seconds,
            mic_gain=mic_gain,
        )
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._main = _Track("main")
        self._mic: _Track | None = None
        self._started_at = 0.0
        self._pa = None
        self.device_names: dict[str, str] = {}

    # -- ciclo de vida -----------------------------------------------------
    def start(self) -> None:
        pyaudio = devices._import_pyaudio()
        with _PA_LOCK:
            pa = pyaudio.PyAudio()
        self._pa = pa
        try:
            want_loopback = self.capture_mode in ("loopback", "loopback+mic")
            want_mic = self.capture_mode in ("mic", "loopback+mic")
            main_dev: devices.DeviceInfo | None = None
            mic_dev: devices.DeviceInfo | None = None
            if want_loopback:
                main_dev = devices.resolve_loopback(pa, pyaudio, self.output_device_index)
            if want_mic:
                mic_dev = devices.resolve_microphone(pa, self.mic_device_index)
                if mic_dev is None and not want_loopback:
                    raise AudioDeviceError(
                        "Sin micrófono disponible",
                        user_message="No se encontró micrófono para el modo 'mic'.",
                    )
            if main_dev is None and mic_dev is not None:
                main_dev, mic_dev = mic_dev, None   # solo micro: pasa a ser la pista principal
            if main_dev is None:  # pragma: no cover - defensivo
                raise AudioDeviceError("No se pudo resolver ningún dispositivo de captura")
        except Exception:
            self._terminate_pa()
            raise

        self.device_names = {"main": main_dev.name}
        self._threads.append(
            _StreamReader(self._main, main_dev, kind="main", stop_event=self._stop,
                          pa=pa, pyaudio_module=pyaudio, on_error=self.on_error)
        )
        if mic_dev is not None:
            self._mic = _Track("mic")
            self.device_names["mic"] = mic_dev.name
            self._threads.append(
                _StreamReader(self._mic, mic_dev, kind="mic", stop_event=self._stop,
                              pa=pa, pyaudio_module=pyaudio, on_error=self.on_error)
            )
        self._started_at = time.monotonic()
        for t in self._threads:
            t.start()
        chunker = threading.Thread(target=self._loop, name="audio-chunker", daemon=True)
        self._threads.append(chunker)
        chunker.start()
        log.info("Grabación iniciada (%s) → %s", self.capture_mode, self.out_dir)

    def _loop(self) -> None:  # pragma: no cover - requiere hardware
        needed = int(self._chunker.chunk_seconds * RATE)
        while not self._stop.is_set():
            if self._main.size >= needed:
                self._emit(needed)
            else:
                self._stop.wait(0.25)

    def _emit(self, n_samples: int, *, final: bool = False) -> None:
        main = self._main.take(n_samples, pad=False)
        if main.size == 0:
            return
        mic = self._mic.take(main.size, pad=True) if self._mic is not None else None
        result = self._chunker.emit(main, mic, final=final)
        if result and self.on_chunk:
            try:
                self.on_chunk(result)
            except Exception:  # pragma: no cover - el callback no puede matar la grabación
                log.exception("on_chunk falló para el chunk %s", result.index)

    def stop(self) -> None:
        """Detiene la captura y vuelca el resto del buffer como último chunk."""
        self._stop.set()
        for t in self._threads:
            if t.is_alive() and t is not threading.current_thread():
                t.join(timeout=6.0)
        remaining = self._main.size
        if remaining > int(0.5 * RATE):
            self._emit(remaining, final=True)
        self._terminate_pa()
        log.info("Grabación detenida (%.1f s útiles)", self._chunker.position)

    def _terminate_pa(self) -> None:
        pa, self._pa = self._pa, None
        if pa is None:
            return
        with _PA_LOCK:
            try:
                pa.terminate()
            except Exception:  # pragma: no cover - cierre del back-end nativo
                log.debug("PyAudio.terminate falló", exc_info=True)

    # -- estado ------------------------------------------------------------
    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self._started_at) if self._started_at else 0.0

    @property
    def healthy(self) -> bool:
        return self._main.healthy and (self._mic is None or self._mic.healthy)

    @property
    def status_detail(self) -> str:
        if not self._main.healthy:
            return f"Audio del sistema: {self._main.detail}"
        if self._mic is not None and not self._mic.healthy:
            return f"Micrófono: {self._mic.detail}"
        return ""


class WavFileRecorder:
    """Reproduce un WAV existente como si fuera una clase en directo.

    Sirve para tres cosas: pruebas automáticas sin hardware, ensayar el pipeline
    completo antes de la primera clase real, y re-procesar una grabación externa.
    Con `realtime=False` corre a la velocidad del disco (tests); con `realtime=True`
    respeta el reloj para poder ver la SPA actualizándose en vivo.
    """

    def __init__(
        self,
        source: Path,
        out_dir: Path,
        *,
        chunk_seconds: float = 90.0,
        overlap_seconds: float = 8.0,
        break_min_seconds: float = 45.0,
        realtime: bool = False,
        on_chunk: ChunkCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self.source = Path(source)
        self.realtime = realtime
        self.on_chunk = on_chunk
        self.on_error = on_error
        self._chunker = _Chunker(
            Path(out_dir),
            chunk_seconds=chunk_seconds,
            overlap_seconds=overlap_seconds,
            break_min_seconds=break_min_seconds,
            mic_gain=0.0,
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.device_names = {"main": f"WAV: {self.source.name}"}
        self.healthy = True
        self.status_detail = ""
        self._elapsed = 0.0

    def start(self) -> None:
        if not self.source.exists():
            raise AudioDeviceError(
                f"El WAV de origen no existe: {self.source}",
                user_message="No encuentro el archivo WAV de práctica indicado.",
            )
        self._thread = threading.Thread(target=self.run, name="wav-recorder", daemon=True)
        self._thread.start()

    def run(self) -> None:
        chunk_s = self._chunker.chunk_seconds
        try:
            for _, block in wavio.iter_wav_windows(self.source, window_s=chunk_s):
                if self._stop.is_set():
                    break
                started = time.monotonic()
                result = self._chunker.emit(block, None)
                self._elapsed = self._chunker.position
                if result and self.on_chunk:
                    self.on_chunk(result)
                if self.realtime:
                    wait = (block.size / RATE) - (time.monotonic() - started)
                    if wait > 0:
                        self._stop.wait(wait)
        except Exception as exc:  # pragma: no cover - WAV corrupto
            self.healthy = False
            self.status_detail = str(exc)[:200]
            log.exception("WavFileRecorder falló")
            if self.on_error:
                self.on_error(str(exc))

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=10.0)

    @property
    def elapsed(self) -> float:
        return self._elapsed


# Alias histórico del plan (Task 2.2).
FakeRecorder = WavFileRecorder
