/**
 * Utilidades de interfaz: DOM, formato, markdown mínimo, avisos y modales.
 *
 * Todo el texto que viene de la IA o del transcript se inserta con `textContent` o pasa
 * por `escapeHtml`. Nada de `innerHTML` con datos del modelo: una respuesta con
 * `<img onerror=...>` no debe poder ejecutar nada en la app que guarda las clases.
 */

/* ------------------------------------------------------------------ DOM */

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function mount(container, ...children) {
  clear(container);
  container.append(...children.filter(Boolean));
  return container;
}

export function escapeHtml(text) {
  return String(text ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

/* ------------------------------------------------------------------ formato */

export function formatClock(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value) => String(value).padStart(2, '0');
  return hours ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
}

export function formatDuration(seconds) {
  const total = Math.round(Number(seconds) || 0);
  if (!total) return '—';
  const hours = Math.floor(total / 3600);
  const minutes = Math.round((total % 3600) / 60);
  if (hours) return `${hours} h ${minutes} min`;
  return `${minutes} min`;
}

const DATE_FORMAT = new Intl.DateTimeFormat('es', {
  weekday: 'short', day: 'numeric', month: 'short', year: 'numeric',
});

export function formatDate(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso).slice(0, 10);
  return DATE_FORMAT.format(date);
}

export function formatMoney(usd) {
  const value = Number(usd) || 0;
  return value < 0.01 && value > 0 ? '< $0,01' : `$${value.toFixed(2).replace('.', ',')}`;
}

export const STATUS_LABELS = {
  recording: 'Grabando',
  processing: 'Procesando',
  done: 'Completa',
  error: 'Error',
  empty: 'Sin contenido',
};

export function statusPill(status, detail) {
  return el('span', {
    class: `pill pill-${status || 'idle'}`,
    text: STATUS_LABELS[status] || status || 'Sin estado',
    title: detail || '',
  });
}

/**
 * Markdown mínimo y seguro: negrita, cursiva, `código`, enlaces, listas y saltos.
 * Suficiente para las respuestas del chat y el reverso de las flashcards, sin traer
 * una librería de 40 KB ni exponerse a HTML arbitrario del modelo.
 */
export function markdown(text) {
  const escaped = escapeHtml(text || '');
  const inline = (line) =>
    line
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|\s)\*([^*]+)\*/g, '$1<em>$2</em>')
      .replace(/(^|\s)_([^_]+)_/g, '$1<em>$2</em>')
      .replace(/\[(\d{1,2}:\d{2}(?::\d{2})?)\]/g, '<span class="tag">$1</span>');

  const blocks = escaped.split(/\n{2,}/);
  return blocks
    .map((block) => {
      const lines = block.split('\n');
      if (lines.every((line) => /^\s*[-*•]\s+/.test(line))) {
        const items = lines.map((line) => `<li>${inline(line.replace(/^\s*[-*•]\s+/, ''))}</li>`);
        return `<ul>${items.join('')}</ul>`;
      }
      if (lines.every((line) => /^\s*\d+[.)]\s+/.test(line))) {
        const items = lines.map((line) => `<li>${inline(line.replace(/^\s*\d+[.)]\s+/, ''))}</li>`);
        return `<ol>${items.join('')}</ol>`;
      }
      return `<p>${lines.map(inline).join('<br>')}</p>`;
    })
    .join('');
}

/* ------------------------------------------------------------------ avisos */

const toastRoot = () => document.getElementById('toasts');

export function toast(message, kind = 'info', { timeout = 6000 } = {}) {
  const root = toastRoot();
  if (!root) return;
  const node = el('div', { class: `toast toast-${kind}`, role: 'status' }, [
    el('div', { text: message }),
  ]);
  node.addEventListener('click', () => node.remove());
  root.append(node);
  if (timeout) setTimeout(() => node.remove(), timeout);
  return node;
}

export function reportError(error) {
  console.error(error);
  if (error?.name === 'AbortError') return;
  toast(error?.message || 'Algo no funcionó', 'error', { timeout: 9000 });
}

/* ------------------------------------------------------------------ modales */

export function modal({ title, body, actions = [], dismissible = true }) {
  const root = document.getElementById('modal-root');
  const previousFocus = document.activeElement;
  const dialog = el('div', {
    class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': title,
  });
  const backdrop = el('div', { class: 'backdrop' }, [dialog]);

  const close = () => {
    backdrop.remove();
    document.removeEventListener('keydown', onKey);
    if (previousFocus instanceof HTMLElement) previousFocus.focus();
  };
  const onKey = (event) => {
    if (event.key === 'Escape' && dismissible) close();
  };

  dialog.append(el('h2', { text: title }));
  for (const part of [].concat(body)) {
    dialog.append(part instanceof Node ? part : el('p', { text: String(part) }));
  }
  const row = el('div', { class: 'modal-actions' });
  for (const action of actions) {
    row.append(
      el('button', {
        class: `btn ${action.class || ''}`,
        type: 'button',
        onClick: async () => {
          if (action.onClick) await action.onClick();
          if (action.keepOpen !== true) close();
        },
      }, [action.label]),
    );
  }
  if (actions.length) dialog.append(row);
  if (dismissible) {
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) close();
    });
  }
  document.addEventListener('keydown', onKey);
  root.append(backdrop);
  (dialog.querySelector('button, input, select, textarea') || dialog).focus();
  return { close, dialog };
}

export function confirmDialog({ title, message, confirmLabel = 'Confirmar', danger = false }) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    modal({
      title,
      body: [el('p', { text: message })],
      actions: [
        { label: 'Cancelar', class: 'btn-ghost', onClick: () => finish(false) },
        {
          label: confirmLabel,
          class: danger ? 'btn-danger' : 'btn-primary',
          onClick: () => finish(true),
        },
      ],
    });
  });
}

/* ------------------------------------------------------------------ varios */

export function loading(text = 'Cargando…') {
  return el('div', { class: 'loading', text });
}

export function emptyState(title, message, action) {
  return el('div', { class: 'empty' }, [
    el('h3', { text: title }),
    el('p', { text: message }),
    action || null,
  ]);
}

export function notice(title, message, kind = '') {
  return el('div', { class: `notice ${kind}` }, [
    title ? el('h3', { text: title }) : null,
    el('p', { text: message }),
  ]);
}

export function debounce(fn, delay = 250) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

export async function withBusy(button, task) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Trabajando…';
  try {
    return await task();
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}
