/**
 * Arranque y enrutado de la SPA.
 *
 * Enrutado por hash (sin servidor de rutas), un solo `render` por vista y una función de
 * limpieza opcional por vista para dar de baja las suscripciones al WebSocket. Sin esa
 * limpieza, navegar entre cuadernos durante una clase de 3,5 h acumula manejadores y la
 * pestaña acaba repintando el transcript varias veces por segmento.
 */

import { live, sessions as sessionsApi, settings as settingsApi } from './api.js';
import { initPlayer } from './player.js';
import {
  el, formatClock, modal, mount, reportError, toast,
} from './ui.js';
import { renderList } from './views/list.js';
import { renderNotebook } from './views/notebook.js';
import { renderPeople } from './views/people.js';
import { renderSettings } from './views/settings.js';

const view = document.getElementById('view');
const statusPill = document.getElementById('status-pill');
const captureButton = document.getElementById('capture-toggle');

let cleanup = null;
let captureState = { state: 'idle', session_id: null, elapsed: 0 };
let elapsedTimer = null;

/* ------------------------------------------------------------------ enrutado */

const navigate = (hash) => {
  if (location.hash === hash) router();
  else location.hash = hash;
};

function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, '');
  const parts = hash.split('/').filter(Boolean);
  if (!parts.length) return { name: 'sessions' };
  if (parts[0] === 'session' && parts[1]) {
    return { name: 'notebook', sessionId: Number(parts[1]), tab: parts[2] || 'notes' };
  }
  if (parts[0] === 'settings') return { name: 'settings' };
  if (parts[0] === 'people') return { name: 'people' };
  return { name: 'sessions' };
}

async function router() {
  if (cleanup) {
    try {
      cleanup();
    } catch (error) {
      console.error(error);
    }
    cleanup = null;
  }
  const route = parseRoute();
  for (const link of document.querySelectorAll('[data-nav]')) {
    const active = (route.name === 'notebook' && link.dataset.nav === 'sessions')
      || link.dataset.nav === route.name;
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }

  try {
    if (route.name === 'notebook') {
      cleanup = await renderNotebook(view, { ...route, navigate });
    } else if (route.name === 'settings') {
      await renderSettings(view);
    } else if (route.name === 'people') {
      await renderPeople(view);
    } else {
      await renderList(view, { navigate });
    }
  } catch (error) {
    reportError(error);
  }
}

/* ------------------------------------------------------------------ captura */

function renderCaptureState() {
  const { state, elapsed, detail, queue } = captureState;
  captureButton.disabled = state === 'processing';
  if (state === 'recording') {
    captureButton.textContent = '⏹ Detener y finalizar';
    captureButton.classList.remove('btn-primary');
    captureButton.classList.add('btn-danger');
    statusPill.className = 'pill pill-recording';
    statusPill.textContent = `Grabando ${formatClock(elapsed)}`;
  } else if (state === 'processing') {
    captureButton.textContent = '▶ Iniciar clase';
    captureButton.classList.add('btn-primary');
    captureButton.classList.remove('btn-danger');
    statusPill.className = 'pill pill-processing';
    statusPill.textContent = detail || 'Procesando el cuaderno…';
  } else {
    captureButton.textContent = '▶ Iniciar clase';
    captureButton.classList.add('btn-primary');
    captureButton.classList.remove('btn-danger');
    statusPill.className = 'pill pill-idle';
    const pending = queue?.pending || 0;
    statusPill.textContent = pending
      ? `${pending} fragmentos por transcribir`
      : 'Sin sesión activa';
  }
}

function startElapsedTimer() {
  clearInterval(elapsedTimer);
  elapsedTimer = setInterval(() => {
    if (captureState.state !== 'recording') return;
    captureState.elapsed += 1;
    renderCaptureState();
  }, 1000);
}

async function refreshCaptureState() {
  try {
    captureState = await sessionsApi.status();
  } catch {
    captureState = { state: 'idle', session_id: null, elapsed: 0 };
  }
  renderCaptureState();
}

captureButton.addEventListener('click', async () => {
  captureButton.disabled = true;
  try {
    if (captureState.state === 'recording' && captureState.session_id) {
      await sessionsApi.stop(captureState.session_id, { finalize: true });
      toast('Clase detenida. Generando el cuaderno…', 'success');
      navigate(`#/session/${captureState.session_id}/notes`);
    } else {
      const started = await sessionsApi.start({});
      toast('Grabando la clase', 'success');
      navigate(`#/session/${started.id}/transcript`);
    }
  } catch (error) {
    reportError(error);
  } finally {
    await refreshCaptureState();
  }
});

/* ------------------------------------------------------------------ eventos */

live.on('heartbeat', (event) => {
  captureState = { ...captureState, ...event };
  renderCaptureState();
});
live.on('session/status', (event) => {
  if (event.status === 'done') toast('Cuaderno listo', 'success');
  if (event.status === 'error') toast(event.detail || 'La sesión terminó con error', 'error');
  refreshCaptureState();
});
live.on('warn', (event) => toast(event.message, 'warn', { timeout: 12000 }));
live.on('generated', (event) => {
  if (event.artifacts?.length) {
    toast(`Materiales generados: ${event.artifacts.join(', ')}`, 'success');
  }
});
live.on('connection', (event) => {
  if (!event.online) statusPill.title = 'Sin conexión con el servidor local';
  else statusPill.title = '';
});

/* ------------------------------------------------------------------ primera vez */

async function firstRunChecks() {
  let system;
  try {
    system = await settingsApi.system();
  } catch {
    return;
  }
  if (!system.legal_notice_seen) {
    modal({
      title: 'Antes de grabar tu primera clase',
      dismissible: false,
      body: [
        el('p', {
          text: 'Esta herramienta graba el audio que suena en tu equipo y lo guarda solo en '
            + 'tu disco. Nada se publica ni se comparte.',
        }),
        el('ol', {}, [
          el('li', {
            text: 'Grabar una clase puede requerir permiso de la empresa o del proveedor '
              + 'del curso. Asegúrate de tenerlo.',
          }),
          el('li', {
            text: 'El audio se envía a Deepgram para transcribirlo y el texto a OpenCode Go '
              + 'para redactar las notas. Ambos servicios declaran no entrenar con estos datos.',
          }),
          el('li', {
            text: 'Puedes borrar un cuaderno completo (notas y audio) con un clic, y desactivar '
              + 'la conservación del audio crudo en Ajustes.',
          }),
        ]),
      ],
      actions: [{
        label: 'Entendido',
        class: 'btn-primary',
        onClick: () => settingsApi.acknowledge('legal').catch(() => {}),
      }],
    });
    return;
  }
  if (!system.onboarding_done) {
    modal({
      title: 'Cómo usar Personal Notebook AI',
      body: [
        el('ol', {}, [
          el('li', {
            text: 'Pega tus llaves de OpenCode Go y Deepgram en Ajustes y pulsa '
              + '«Probar conexión».',
          }),
          el('li', {
            text: 'Deja el modo de captura en «Sistema + micrófono»: así se graba también tu '
              + 'voz y la app sabe qué dijiste tú, sin tocar la configuración de Zoom.',
          }),
          el('li', {
            text: 'Pulsa «▶ Iniciar clase» (aquí o en el gadget flotante) cuando empiece la '
              + 'clase. Verás la transcripción y las notas en borrador en vivo.',
          }),
          el('li', {
            text: 'Al terminar, pulsa «⏹ Detener y finalizar». En unos minutos tendrás el '
              + 'cuaderno con línea de tiempo, frases, vocabulario, roleplays y materiales.',
          }),
        ]),
        (() => {
          const warnings = [];
          if (!system.keys.opencode || !system.keys.deepgram) {
            warnings.push('Todavía faltan llaves por configurar.');
          }
          if (!system.audio_capture) warnings.push('Falta PyAudioWPatch para capturar audio.');
          if (!system.ffmpeg) warnings.push('Falta ffmpeg para el MP3 y el podcast.');
          return warnings.length
            ? el('div', { class: 'notice' }, [el('p', { text: warnings.join(' ') })])
            : null;
        })(),
      ],
      actions: [
        {
          label: 'Ir a Ajustes',
          class: 'btn-primary',
          onClick: async () => {
            await settingsApi.acknowledge('onboarding').catch(() => {});
            navigate('#/settings');
          },
        },
        {
          label: 'Más tarde',
          class: 'btn-ghost',
          onClick: () => settingsApi.acknowledge('onboarding').catch(() => {}),
        },
      ],
    });
  }
}

/* ------------------------------------------------------------------ arranque */

window.addEventListener('hashchange', router);
window.addEventListener('DOMContentLoaded', async () => {
  initPlayer();
  live.connect();
  startElapsedTimer();
  await refreshCaptureState();
  await router();
  firstRunChecks();
});

// Si el backend se cae y vuelve, el estado se re-sincroniza al recuperar el foco.
window.addEventListener('focus', refreshCaptureState);
