/**
 * Lógica del gadget flotante.
 *
 * Se carga desde `http://127.0.0.1:8787/gadget/gadget.html`, no desde `file://`. Así el
 * gadget es *same-origin* con la API: no hay `Origin: null` que la guardia local tenga
 * que tratar como caso especial, y el WebSocket funciona sin más.
 *
 * Diferencias con el borrador del plan: allí el gadget creaba una sesión con `POST
 * /api/sessions`, luego listaba todas y arrancaba "la primera", y el cronómetro era un
 * `"0:00:00"` fijo. Aquí hay un único endpoint (`POST /api/sessions/start`), el estado
 * real llega por WebSocket con latidos, y el cronómetro cuenta de verdad.
 */

const API = '';   // mismo origen
const bubble = document.getElementById('bubble');
const menu = document.getElementById('menu');
const icon = document.getElementById('icon');
const timer = document.getElementById('timer');
const detail = document.getElementById('detail');
const mainButton = document.getElementById('btn-main');

let state = { state: 'idle', session_id: null, elapsed: 0, detail: '' };
let online = true;
let busy = false;

const ICONS = { idle: '●', recording: '⏺', processing: '◐', error: '!' };

function formatClock(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value) => String(value).padStart(2, '0');
  return hours ? `${hours}:${pad(minutes)}` : `${minutes}:${pad(secs)}`;
}

function render() {
  const kind = online ? (state.state === 'done' || state.state === 'empty' ? 'idle' : state.state) : 'offline';
  bubble.className = kind;
  icon.textContent = ICONS[kind] || '●';
  timer.textContent = formatClock(state.elapsed);

  if (!online) {
    detail.textContent = 'Sin conexión con el servidor local.';
  } else if (state.state === 'recording') {
    const devices = state.devices ? Object.values(state.devices).join(' + ') : '';
    const queue = state.queue?.pending ? ` · ${state.queue.pending} en cola` : '';
    detail.textContent = state.healthy === false
      ? (state.detail || 'Problema con el dispositivo de audio')
      : `Grabando ${formatClock(state.elapsed)}${queue}\n${devices}`;
  } else if (state.state === 'processing') {
    detail.textContent = state.detail || 'Generando el cuaderno…';
  } else {
    detail.textContent = 'Sin sesión activa';
  }

  mainButton.disabled = busy || state.state === 'processing';
  if (state.state === 'recording') {
    mainButton.textContent = '⏹ Detener y finalizar';
    mainButton.className = 'stop';
  } else {
    mainButton.textContent = '▶ Iniciar clase';
    mainButton.className = 'primary';
  }
  bubble.title = detail.textContent;
}

async function callApi(path, options = {}) {
  const response = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Error ${response.status}`);
  return payload;
}

async function refresh() {
  try {
    state = await callApi('/api/sessions/status');
    online = true;
  } catch {
    online = false;
  }
  render();
}

/* ------------------------------------------------------------------ acciones */

mainButton.addEventListener('click', async () => {
  busy = true;
  render();
  try {
    if (state.state === 'recording' && state.session_id) {
      await callApi(`/api/sessions/${state.session_id}/stop`, {
        method: 'POST', body: JSON.stringify({ finalize: true }),
      });
    } else {
      await callApi('/api/sessions/start', { method: 'POST', body: '{}' });
    }
    menu.hidden = true;
  } catch (error) {
    detail.textContent = error.message;
  } finally {
    busy = false;
    await refresh();
  }
});

document.getElementById('btn-web').addEventListener('click', () => {
  openExternal('http://127.0.0.1:8787/');
});
document.getElementById('btn-settings').addEventListener('click', () => {
  openExternal('http://127.0.0.1:8787/#/settings');
});
document.getElementById('btn-hide').addEventListener('click', () => {
  if (window.pywebview?.api?.hide) window.pywebview.api.hide();
  else menu.hidden = true;
});

function openExternal(url) {
  if (window.pywebview?.api?.open_web) window.pywebview.api.open_web(url);
  else window.open(url, '_blank');
}

/* ------------------------------------------------------------------ interacción */

const toggleMenu = () => {
  menu.hidden = !menu.hidden;
  if (!menu.hidden) refresh();
};
bubble.addEventListener('click', toggleMenu);
bubble.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    toggleMenu();
  }
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') menu.hidden = true;
});

/* ------------------------------------------------------------------ tiempo real */

function connect() {
  let socket;
  try {
    socket = new WebSocket(`ws://${location.host}/ws`);
  } catch {
    setTimeout(connect, 3000);
    return;
  }
  socket.addEventListener('open', () => {
    online = true;
    render();
  });
  socket.addEventListener('message', (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.type === 'heartbeat' || message.type === 'hello') {
      state = { ...state, ...message };
      render();
    } else if (message.type === 'session/status') {
      refresh();
    } else if (message.type === 'warn') {
      detail.textContent = message.message;
      render();
    }
  });
  socket.addEventListener('close', () => {
    online = false;
    render();
    setTimeout(connect, 3000);
  });
}

// El cronómetro corre en local para no depender de la frecuencia de los latidos.
setInterval(() => {
  if (state.state === 'recording') {
    state.elapsed = (state.elapsed || 0) + 1;
    render();
  }
}, 1000);
setInterval(refresh, 15000);

refresh();
connect();
