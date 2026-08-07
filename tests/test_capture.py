"""Captura: WAV, remuestreo, VAD y troceado con solape."""

from __future__ import annotations

import numpy as np
import pytest

from app.capture import loopback, silence, wavio
from tests.conftest import two_speaker_signal, write_wav


# ---------------------------------------------------------------------------
# wavio
# ---------------------------------------------------------------------------


def test_pcm_roundtrip_preserves_signal():
    original = (np.sin(np.linspace(0, 40, 16000)) * 0.5).astype(np.float32)
    restored = wavio.pcm16_to_float(wavio.float_to_pcm16(original))
    assert restored.shape == original.shape
    assert np.max(np.abs(restored - original)) < 1e-3


def test_pcm16_downmixes_channels():
    stereo = np.array([1000, -1000, 2000, -2000], dtype="<i2").tobytes()
    mono = wavio.pcm16_to_float(stereo, channels=2)
    assert mono.size == 2
    assert abs(mono[0]) < 1e-6


def test_resample_integer_ratio_is_exact_decimation():
    x = np.arange(48000, dtype=np.float32) / 48000
    out = wavio.resample(x, 48000, 16000)
    assert out.size == 16000


def test_resample_non_integer_ratio():
    x = np.random.default_rng(0).normal(0, 0.2, 44100).astype(np.float32)
    out = wavio.resample(x, 44100, 16000)
    assert 15900 < out.size < 16100


def test_resample_noop_when_rates_match():
    x = np.zeros(10, dtype=np.float32)
    assert wavio.resample(x, 16000, 16000) is x


def test_write_and_read_wav(tmp_path):
    signal = two_speaker_signal(3.0)
    path = wavio.write_wav(tmp_path / "a.wav", signal)
    restored, rate = wavio.read_wav(path)
    assert rate == 16000
    assert abs(restored.size - signal.size) <= 1
    assert pytest.approx(wavio.wav_duration(path), abs=0.05) == 3.0


def test_write_wav_is_atomic(tmp_path):
    path = wavio.write_wav(tmp_path / "b.wav", two_speaker_signal(1.0))
    assert path.exists()
    assert not list(tmp_path.glob("*.part"))


def test_mix_applies_gain_and_headroom():
    a = np.full(100, 0.8, dtype=np.float32)
    b = np.full(100, 0.8, dtype=np.float32)
    mixed = wavio.mix([(a, 1.0), (b, 1.0)])
    assert float(np.max(np.abs(mixed))) <= 0.9 + 1e-6


def test_mix_pads_shorter_track():
    mixed = wavio.mix([(np.ones(100, dtype=np.float32), 1.0),
                       (np.ones(10, dtype=np.float32), 1.0)])
    assert mixed.size == 100


def test_iter_wav_windows_streams(tmp_path):
    path = write_wav(tmp_path / "long.wav", two_speaker_signal(200.0))
    windows = list(wavio.iter_wav_windows(path, window_s=90))
    assert len(windows) == 3
    assert pytest.approx(windows[1][0], abs=0.1) == 90.0
    assert windows[-1][1].size < 90 * 16000


# ---------------------------------------------------------------------------
# silence / VAD
# ---------------------------------------------------------------------------


def test_silence_ranges_finds_the_gap():
    sr = 16000
    rng = np.random.default_rng(0)
    x = np.full(sr * 10, 0.0005, dtype=np.float32)
    x[sr * 2 : sr * 4] = rng.normal(0, 0.3, sr * 2).astype(np.float32)
    ranges = silence.silence_ranges(x, sr=sr, min_silence_s=1.0)
    assert any(end <= 2.2 for _, end in ranges)
    assert any(start >= 3.8 for start, _ in ranges)


def test_segment_silences_only_reports_long_breaks():
    sr = 16000
    loud = np.full(sr * 300, 0.3, dtype=np.float32)
    loud[sr * 100 : sr * 103] = 0.0005
    assert silence.segment_silences(loud, sr=sr, min_break_s=45) == []

    with_break = np.full(sr * 300, 0.3, dtype=np.float32)
    with_break[sr * 100 : sr * 200] = 0.0005
    breaks = silence.segment_silences(with_break, sr=sr, min_break_s=45)
    assert len(breaks) == 1
    assert 95 < breaks[0][0] < 105


def test_adaptive_threshold_works_with_quiet_audio():
    """Con audio bajito (volumen del sistema al 20 %) el umbral fijo del plan fallaba."""
    sr = 16000
    rng = np.random.default_rng(3)
    quiet = np.full(sr * 20, 0.0002, dtype=np.float32)
    quiet[sr * 5 : sr * 10] = rng.normal(0, 0.02, sr * 5).astype(np.float32)
    ranges = silence.silence_ranges(quiet, sr=sr, min_silence_s=2.0)
    assert ranges, "el umbral adaptativo debe detectar el silencio con señal débil"


def test_voiced_ranges_marks_speech():
    sr = 16000
    x = np.full(sr * 10, 0.0004, dtype=np.float32)
    x[sr * 3 : sr * 6] = 0.25
    voiced = silence.voiced_ranges(x, sr=sr)
    assert len(voiced) == 1
    assert 2.5 < voiced[0][0] < 3.5
    assert 5.5 < voiced[0][1] < 6.5


def test_merge_ranges_joins_close_and_overlapping():
    merged = silence.merge_ranges([(0, 2), (2.5, 4), (10, 12)], gap_s=1.0)
    assert merged == [(0.0, 4.0), (10.0, 12.0)]


def test_overlap_fraction():
    assert silence.overlap_fraction(0, 10, [(0, 5)]) == 0.5
    assert silence.overlap_fraction(0, 10, []) == 0.0
    assert silence.overlap_fraction(5, 5, [(0, 10)]) == 0.0


def test_shift_ranges():
    assert silence.shift_ranges([(1.0, 2.0)], 10) == [(11.0, 12.0)]


def test_signal_level_detects_near_silence():
    assert silence.signal_level(np.zeros(1000, dtype=np.float32)) == 0.0
    assert silence.signal_level(np.full(1000, 0.5, dtype=np.float32)) > 0.4


def test_rms_profile_is_vectorized_and_fast():
    """3,5 h de audio deben perfilarse en menos de un segundo."""
    import time

    sr = 16000
    x = np.random.default_rng(0).normal(0, 0.1, sr * 600).astype(np.float32)
    started = time.perf_counter()
    profile, step = silence.rms_profile(x, sr)
    assert profile.size > 5000
    assert step == pytest.approx(0.1)
    assert time.perf_counter() - started < 1.0


# ---------------------------------------------------------------------------
# Troceado (WavFileRecorder)
# ---------------------------------------------------------------------------


def _record(tmp_path, seconds=300.0, chunk_seconds=90.0, overlap=8.0):
    source = write_wav(tmp_path / "src.wav", two_speaker_signal(seconds))
    out_dir = tmp_path / "audio"
    chunks: list[loopback.ChunkResult] = []
    recorder = loopback.WavFileRecorder(
        source, out_dir, chunk_seconds=chunk_seconds, overlap_seconds=overlap,
        realtime=False, on_chunk=chunks.append,
    )
    recorder.run()
    return chunks, out_dir


def test_recorder_splits_into_chunks(tmp_path):
    chunks, out_dir = _record(tmp_path)
    assert len(chunks) == 4                      # 300 s / 90 s → 4 (el último parcial)
    assert len(list(out_dir.glob("chunk_*.wav"))) == 4
    assert [c.index for c in chunks] == [0, 1, 2, 3]


def test_chunk_start_times_are_contiguous(tmp_path):
    chunks, _ = _record(tmp_path)
    starts = [c.start_t for c in chunks]
    assert starts == pytest.approx([0.0, 90.0, 180.0, 270.0], abs=0.05)
    assert chunks[-1].duration == pytest.approx(30.0, abs=0.05)


def test_overlap_is_prepended_but_not_counted_in_time(tmp_path):
    """El fichero incluye pre-roll; `start_t` sigue siendo el tiempo real de sesión."""
    chunks, _ = _record(tmp_path, overlap=8.0)
    assert chunks[0].overlap_pre == 0.0
    assert chunks[1].overlap_pre == pytest.approx(8.0, abs=0.01)
    assert chunks[1].file_start_t == pytest.approx(82.0, abs=0.05)
    duration = wavio.wav_duration(chunks[1].path)
    assert duration == pytest.approx(98.0, abs=0.1)


def test_no_overlap_when_disabled(tmp_path):
    chunks, _ = _record(tmp_path, overlap=0.0)
    assert all(c.overlap_pre == 0.0 for c in chunks)
    assert wavio.wav_duration(chunks[1].path) == pytest.approx(90.0, abs=0.1)


def test_recorder_reports_breaks_in_absolute_time(tmp_path):
    chunks, _ = _record(tmp_path, chunk_seconds=300.0)
    breaks = [b for chunk in chunks for b in chunk.silences]
    assert breaks, "el receso sintético de 60 s debe detectarse"
    start, end = breaks[0]
    assert 115 < start < 125
    assert end - start > 45


def test_recorder_level_is_reported(tmp_path):
    chunks, _ = _record(tmp_path)
    assert all(c.level > 0 for c in chunks)


def test_recorder_stop_is_safe_without_start(tmp_path):
    source = write_wav(tmp_path / "s.wav", two_speaker_signal(5.0))
    recorder = loopback.WavFileRecorder(source, tmp_path / "out")
    recorder.stop()      # no debe lanzar
    assert recorder.elapsed == 0.0


def test_missing_source_raises_audio_error(tmp_path):
    from app.errors import AudioDeviceError

    recorder = loopback.WavFileRecorder(tmp_path / "nope.wav", tmp_path / "out")
    with pytest.raises(AudioDeviceError):
        recorder.start()


def test_fake_recorder_alias_exists():
    assert loopback.FakeRecorder is loopback.WavFileRecorder
