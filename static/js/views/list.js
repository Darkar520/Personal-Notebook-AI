/** Pantalla "Mis cuadernos": lista, recuperación de sesiones huérfanas y borrado. */

import { sessions as sessionsApi, settings as settingsApi } from '../api.js';
import {
  confirmDialog, el, emptyState, formatDate, formatDuration, loading, mount, notice,
  reportError, statusPill, toast,
} from '../ui.js';

export async function renderList(container, { navigate }) {
  mount(container, loading('Cargando cuadernos…'));
  let items;
  let pending = [];
  try {
    [items, pending] = await Promise.all([
      sessionsApi.list(),
      sessionsApi.pending().catch(() => []),
    ]);
  } catch (error) {
    reportError(error);
    mount(container, notice('No se pudo cargar', error.message, 'notice-error'));
    return;
  }

  const parts = [
    el('div', { class: 'toolbar' }, [
      el('h2', { text: 'Mis cuadernos' }),
      el('span', { class: 'spacer' }),
      el('button', {
        class: 'btn', type: 'button', id: 'restore-backup',
        title: 'Restaurar un cuaderno exportado previamente',
      }, ['⬆ Restaurar backup']),
      el('button', { class: 'btn', type: 'button', id: 'new-empty' }, ['+ Cuaderno vacío']),
    ]),
  ];

  for (const session of pending) {
    parts.push(recoveryBanner(session, container, navigate));
  }

  if (!items.length) {
    parts.push(
      emptyState(
        'Todavía no hay cuadernos',
        'Pulsa «▶ Iniciar clase» arriba (o en el gadget flotante) cuando empiece la clase. '
        + 'Al detenerla se genera el cuaderno con notas, línea de tiempo y materiales.',
      ),
    );
  } else {
    const grid = el('div', { class: 'grid grid-2' });
    for (const session of items) grid.append(sessionCard(session, container, navigate));
    parts.push(grid);
  }

  mount(container, ...parts);

  container.querySelector('#new-empty').addEventListener('click', async () => {
    try {
      const created = await sessionsApi.create({});
      navigate(`#/session/${created.id}`);
    } catch (error) {
      reportError(error);
    }
  });
  container.querySelector('#restore-backup').addEventListener('click', () => {
    const input = el('input', { type: 'file', accept: '.zip' });
    input.addEventListener('change', async () => {
      const file = input.files?.[0];
      if (!file) return;
      try {
        toast('Restaurando el cuaderno…');
        const result = await settingsApi.restore(file);
        toast('Cuaderno restaurado', 'success');
        navigate(`#/session/${result.session_id}`);
      } catch (error) {
        reportError(error);
      }
    });
    input.click();
  });
}

function sessionCard(session, container, navigate) {
  const meta = el('div', { class: 'meta' }, [
    el('span', { text: `#${session.session_number}` }),
    el('span', { text: formatDate(session.started_at) }),
    el('span', { text: formatDuration(session.duration_sec) }),
    el('span', {
      text: session.topics_count
        ? `${session.topics_count} tema${session.topics_count === 1 ? '' : 's'}`
        : 'sin temas',
    }),
    session.account_tag ? el('span', { class: 'tag', text: session.account_tag }) : null,
  ]);

  const card = el('button', { class: 'card session-card', type: 'button' }, [
    el('div', { class: 'card-head' }, [
      el('h3', { text: session.title || `Sesión ${session.session_number}` }),
      statusPill(session.status, session.status_detail),
    ]),
    meta,
    session.speakers_pending && session.status === 'done'
      ? el('p', { class: 'mono', text: '⚠ Falta confirmar quién es quién' })
      : null,
    session.status === 'error' && session.status_detail
      ? el('p', { class: 'mono', text: session.status_detail })
      : null,
  ]);
  card.addEventListener('click', () => navigate(`#/session/${session.id}`));

  const actions = el('div', { class: 'btn-row', style: 'margin-top:.7rem' }, [
    el('a', {
      class: 'btn btn-sm', href: `/api/sessions/${session.id}/export`,
      title: 'Descargar un ZIP con las notas, el transcript y el audio',
      onClick: (event) => event.stopPropagation(),
    }, ['⬇ Exportar']),
    el('button', {
      class: 'btn btn-sm btn-danger', type: 'button',
      onClick: async (event) => {
        event.stopPropagation();
        const ok = await confirmDialog({
          title: 'Eliminar el cuaderno',
          message: 'Se borran las notas, el transcript y todo el audio de esta clase. '
            + 'Esta acción no se puede deshacer.',
          confirmLabel: 'Eliminar todo',
          danger: true,
        });
        if (!ok) return;
        try {
          await sessionsApi.remove(session.id);
          toast('Cuaderno eliminado', 'success');
          renderList(container, { navigate });
        } catch (error) {
          reportError(error);
        }
      },
    }, ['Eliminar']),
  ]);
  card.append(actions);
  return card;
}

function recoveryBanner(session, container, navigate) {
  const box = el('div', { class: 'notice' }, [
    el('h3', { text: `La clase «${session.title}» quedó grabando` }),
    el('p', {
      text: 'La aplicación se cerró antes de detenerla. Puedes finalizarla (se transcribe '
        + 'lo que se grabó y se genera el cuaderno) o descartarla.',
    }),
  ]);
  const row = el('div', { class: 'btn-row' }, [
    el('button', {
      class: 'btn btn-primary btn-sm', type: 'button',
      onClick: async () => {
        try {
          await sessionsApi.finalizeRecording(session.id);
          toast('Finalizando el cuaderno…', 'success');
          navigate(`#/session/${session.id}`);
        } catch (error) {
          reportError(error);
        }
      },
    }, ['Finalizar y generar cuaderno']),
    el('button', {
      class: 'btn btn-sm btn-danger', type: 'button',
      onClick: async () => {
        const ok = await confirmDialog({
          title: 'Descartar la grabación',
          message: 'Se borra el audio grabado y la sesión. No se puede recuperar.',
          confirmLabel: 'Descartar',
          danger: true,
        });
        if (!ok) return;
        try {
          await sessionsApi.discardRecording(session.id);
          renderList(container, { navigate });
        } catch (error) {
          reportError(error);
        }
      },
    }, ['Descartar']),
  ]);
  box.append(row);
  return box;
}
