/**
 * Cliente HTTP + WebSocket de la API local.
 *
 * Detalles que importan:
 * - Un único punto para los errores: el backend devuelve `{detail, code}` con mensajes
 *   escritos para el usuario, así que la interfaz nunca muestra trazas técnicas.
 * - El WebSocket reconecta con backoff: una clase dura 3,5 h y el navegador puede
 *   suspender la pestaña; sin reconexión, la vista en vivo se queda congelada y parece
 *   que la app dejó de grabar.
 */

const BASE = '/api';

export class ApiError extends Error {
  constructor(message, { status = 0, code = '' } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

async function request(path, { method = 'GET', body, signal, raw = false } = {}) {
  const options = { method, signal, headers: {} };
  if (body instanceof FormData) {
    options.body = body;
  } else if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(BASE + path, options);
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError('No se pudo contactar con el servidor local. ¿Sigue abierto?', {
      code: 'offline',
    });
  }

  if (response.status === 204) return null;
  if (raw) {
    if (!response.ok) throw new ApiError(`HTTP ${response.status}`, { status: response.status });
    return response;
  }

  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text.slice(0, 300) };
    }
  }
  if (!response.ok) {
    const detail = (payload && payload.detail) || `Error ${response.status}`;
    throw new ApiError(detail, { status: response.status, code: payload?.code || '' });
  }
  return payload;
}

export const api = {
  get: (path, options) => request(path, options),
  post: (path, body, options) => request(path, { ...options, method: 'POST', body }),
  put: (path, body, options) => request(path, { ...options, method: 'PUT', body }),
  patch: (path, body, options) => request(path, { ...options, method: 'PATCH', body }),
  del: (path, options) => request(path, { ...options, method: 'DELETE' }),
  raw: (path, options) => request(path, { ...options, raw: true }),
};

/* ------------------------------------------------------------------ endpoints */

export const sessions = {
  list: () => api.get('/sessions'),
  get: (id) => api.get(`/sessions/${id}`),
  create: (payload) => api.post('/sessions', payload || {}),
  start: (payload) => api.post('/sessions/start', payload || {}),
  stop: (id, payload) => api.post(`/sessions/${id}/stop`, payload || {}),
  patch: (id, payload) => api.patch(`/sessions/${id}`, payload),
  remove: (id) => api.del(`/sessions/${id}`),
  status: () => api.get('/sessions/status'),
  pending: () => api.get('/sessions/pending-recording'),
  finalizeRecording: (id) => api.post(`/sessions/${id}/finalize-recording`, {}),
  discardRecording: (id) => api.del(`/sessions/${id}/discard-recording`),
  repolish: (id) => api.post(`/sessions/${id}/repolish`, {}),
  retryTranscription: (id) => api.post(`/sessions/${id}/retry-transcription`, {}),
  queue: (id) => api.get(`/sessions/${id}/queue`),
};

export const content = {
  topics: (id, status) => api.get(`/sessions/${id}/topics${status ? `?status=${status}` : ''}`),
  patchTopic: (id, topicId, payload) => api.patch(`/sessions/${id}/topics/${topicId}`, payload),
  deleteTopic: (id, topicId) => api.del(`/sessions/${id}/topics/${topicId}`),
  timeline: (id) => api.get(`/sessions/${id}/timeline`),
  roleplays: (id) => api.get(`/sessions/${id}/roleplays`),
  transcript: (id, afterId = 0) => api.get(`/sessions/${id}/transcript?after_id=${afterId}`),
  search: (id, q) => api.get(`/sessions/${id}/search?q=${encodeURIComponent(q)}`),
  usage: (id) => api.get(`/sessions/${id}/usage`),
};

export const speakers = {
  list: (id) => api.get(`/sessions/${id}/speakers`),
  confirm: (id, payload) => api.put(`/sessions/${id}/speakers`, payload),
  people: () => api.get('/people'),
  createPerson: (payload) => api.post('/people', payload),
  deletePerson: (personId) => api.del(`/people/${personId}`),
};

export const chat = {
  messages: (id) => api.get(`/sessions/${id}/messages`),
  send: (id, message, reset = false) => api.post(`/sessions/${id}/chat`, { message, reset }),
  clear: (id) => api.del(`/sessions/${id}/messages`),
};

export const study = {
  quiz: (id) => api.get(`/sessions/${id}/quiz`),
  makeQuiz: (id, n) => api.post(`/sessions/${id}/quiz`, { n }),
  flashcards: (id) => api.get(`/sessions/${id}/flashcards`),
  makeFlashcards: (id, n) => api.post(`/sessions/${id}/flashcards`, { n }),
  review: (id, cardId, correct) =>
    api.post(`/sessions/${id}/flashcards/${cardId}/review`, { correct }),
  conceptMap: (id) => api.get(`/sessions/${id}/concept-map`),
  makeConceptMap: (id) => api.post(`/sessions/${id}/concept-map`, {}),
  podcast: (id) => api.get(`/sessions/${id}/podcast`),
  makePodcast: (id) => api.post(`/sessions/${id}/podcast`, {}),
};

export const settings = {
  get: () => api.get('/settings'),
  save: (payload) => api.put('/settings', payload),
  clearKey: (provider) => api.del(`/settings/keys/${provider}`),
  test: () => api.post('/settings/test', {}),
  devices: () => api.get('/settings/devices'),
  models: (refresh = false) => api.get(`/settings/models${refresh ? '?refresh=true' : ''}`),
  system: () => api.get('/system'),
  acknowledge: (kind) => api.post(`/system/acknowledge?kind=${kind}`, {}),
  restore: (file) => {
    const form = new FormData();
    form.append('file', file);
    return api.post('/backup/restore', form);
  },
};

export const mediaUrl = {
  session: (id) => `${BASE}/sessions/${id}/media/session`,
  podcast: (id) => `${BASE}/sessions/${id}/media/podcast`,
  export: (id) => `${BASE}/sessions/${id}/export`,
};

/* ------------------------------------------------------------------ WebSocket */

class LiveBus {
  constructor() {
    this.socket = null;
    this.handlers = new Map();
    this.attempt = 0;
    this.closed = false;
  }

  on(type, handler) {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set());
    this.handlers.get(type).add(handler);
    return () => this.handlers.get(type).delete(handler);
  }

  emit(event) {
    for (const handler of this.handlers.get(event.type) || []) {
      try {
        handler(event);
      } catch (error) {
        console.error('Fallo en el manejador de', event.type, error);
      }
    }
    for (const handler of this.handlers.get('*') || []) handler(event);
  }

  connect() {
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) return;
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.socket = new WebSocket(`${protocol}//${location.host}/ws`);

    this.socket.addEventListener('open', () => {
      this.attempt = 0;
      this.emit({ type: 'connection', online: true });
    });
    this.socket.addEventListener('message', (event) => {
      try {
        this.emit(JSON.parse(event.data));
      } catch {
        /* mensaje no-JSON: se ignora */
      }
    });
    this.socket.addEventListener('close', () => {
      this.emit({ type: 'connection', online: false });
      if (this.closed) return;
      // Backoff hasta 15 s: no queremos martillear al backend si está reiniciando.
      this.attempt += 1;
      const delay = Math.min(15000, 500 * 2 ** Math.min(this.attempt, 5));
      setTimeout(() => this.connect(), delay);
    });
    this.socket.addEventListener('error', () => this.socket?.close());
  }

  close() {
    this.closed = true;
    this.socket?.close();
  }
}

export const live = new LiveBus();
