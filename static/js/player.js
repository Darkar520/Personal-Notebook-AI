/**
 * Reproductor único de la sesión.
 *
 * Aquí se materializa la decisión de diseño de `app/audio/session_audio.py`: en vez de
 * generar un MP3 por tramo de la línea de tiempo (lo que proponía el plan), se sirve un
 * solo `session.mp3` con soporte de HTTP Range y se salta al instante pedido. Con eso:
 *
 * - se puede reproducir **cualquier** punto (un tema, un receso, una frase textual o un
 *   turno concreto del transcript), no solo los tramos que la IA decidió cortar;
 * - `playRange` para automáticamente al final del tramo, que es el comportamiento que el
 *   spec pedía ("click en el tramo reproduce exactamente ese intervalo");
 * - no hay que esperar a que ffmpeg genere decenas de clips ni duplicar el audio.
 */

import { formatClock } from './ui.js';

const nodes = {};
let stopAt = null;
let currentSessionId = null;

export function initPlayer() {
  nodes.root = document.getElementById('player');
  nodes.audio = document.getElementById('player-audio');
  nodes.toggle = document.getElementById('player-toggle');
  nodes.seek = document.getElementById('player-seek');
  nodes.time = document.getElementById('player-time');
  nodes.label = document.getElementById('player-label');
  nodes.rate = document.getElementById('player-rate');
  nodes.close = document.getElementById('player-close');
  if (!nodes.audio) return;

  nodes.toggle.addEventListener('click', () => {
    if (nodes.audio.paused) nodes.audio.play().catch(() => {});
    else nodes.audio.pause();
  });
  nodes.close.addEventListener('click', hidePlayer);
  nodes.rate.addEventListener('change', () => {
    nodes.audio.playbackRate = Number(nodes.rate.value) || 1;
  });
  nodes.seek.addEventListener('input', () => {
    stopAt = null;                       // arrastrar la barra cancela el "para en X"
    const duration = nodes.audio.duration;
    if (Number.isFinite(duration)) {
      nodes.audio.currentTime = (Number(nodes.seek.value) / 100) * duration;
    }
  });

  nodes.audio.addEventListener('timeupdate', () => {
    if (stopAt !== null && nodes.audio.currentTime >= stopAt) {
      nodes.audio.pause();
      stopAt = null;
    }
    render();
  });
  nodes.audio.addEventListener('loadedmetadata', render);
  nodes.audio.addEventListener('play', () => {
    nodes.toggle.textContent = '⏸';
  });
  nodes.audio.addEventListener('pause', () => {
    nodes.toggle.textContent = '▶';
  });
  nodes.audio.addEventListener('error', () => {
    nodes.label.textContent = 'No se pudo cargar el audio';
  });

  document.addEventListener('keydown', (event) => {
    if (event.target.matches('input, textarea, select')) return;
    if (event.code === 'Space' && !nodes.root.hidden) {
      event.preventDefault();
      nodes.toggle.click();
    }
    if (event.key === 'ArrowLeft' && event.altKey) skip(-5);
    if (event.key === 'ArrowRight' && event.altKey) skip(5);
  });
}

function render() {
  const { audio, seek, time } = nodes;
  const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
  if (duration) seek.value = String((audio.currentTime / duration) * 100);
  time.textContent = `${formatClock(audio.currentTime)} / ${formatClock(duration)}`;
}

function skip(seconds) {
  if (nodes.root.hidden) return;
  stopAt = null;
  nodes.audio.currentTime = Math.max(0, nodes.audio.currentTime + seconds);
}

export function loadSession(sessionId, url, label) {
  if (!nodes.audio) return;
  if (currentSessionId !== sessionId || !nodes.audio.src) {
    nodes.audio.src = url;
    currentSessionId = sessionId;
  }
  nodes.label.textContent = label || 'Audio de la clase';
  nodes.root.hidden = false;
}

export function hidePlayer() {
  if (!nodes.audio) return;
  nodes.audio.pause();
  nodes.root.hidden = true;
  stopAt = null;
}

/** Salta a `start` y (opcionalmente) para en `end`. */
export function playRange(start, end = null) {
  if (!nodes.audio || !nodes.audio.src) return false;
  nodes.root.hidden = false;
  stopAt = end !== null && end > start ? Number(end) : null;
  const seek = () => {
    nodes.audio.currentTime = Math.max(0, Number(start) || 0);
    nodes.audio.play().catch(() => {});
  };
  if (nodes.audio.readyState >= 1) seek();
  else nodes.audio.addEventListener('loadedmetadata', seek, { once: true });
  return true;
}

export function isLoaded() {
  return Boolean(nodes.audio && nodes.audio.src);
}

export function loadExternal(url, label) {
  if (!nodes.audio) return;
  nodes.audio.src = url;
  currentSessionId = null;
  nodes.label.textContent = label || 'Audio';
  nodes.root.hidden = false;
  stopAt = null;
  nodes.audio.play().catch(() => {});
}
