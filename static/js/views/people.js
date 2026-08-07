/** Personas conocidas: se reutilizan como sugerencia en cada clase nueva. */

import { speakers as speakersApi } from '../api.js';
import {
  confirmDialog, el, emptyState, loading, mount, notice, reportError, toast, withBusy,
} from '../ui.js';

const ROLE_LABELS = {
  teacher: 'Teacher', me: 'Yo', student: 'Compañero/a', other: 'Otro',
};

export async function renderPeople(container) {
  mount(container, loading('Cargando personas…'));
  let people;
  try {
    people = await speakersApi.people();
  } catch (error) {
    reportError(error);
    mount(container, notice('No se pudo cargar', error.message, 'notice-error'));
    return;
  }

  const nameInput = el('input', { type: 'text', placeholder: 'Nombre', 'aria-label': 'Nombre' });
  const roleSelect = el('select', { 'aria-label': 'Rol' });
  for (const [value, label] of Object.entries(ROLE_LABELS)) {
    roleSelect.append(el('option', { value, text: label }));
  }
  roleSelect.value = 'student';

  const addButton = el('button', { class: 'btn btn-primary', type: 'button' }, ['Añadir']);
  addButton.addEventListener('click', () => withBusy(addButton, async () => {
    const name = nameInput.value.trim();
    if (!name) return;
    try {
      await speakersApi.createPerson({ name, role: roleSelect.value });
      toast('Persona añadida', 'success');
      renderPeople(container);
    } catch (error) {
      reportError(error);
    }
  }));

  const parts = [
    el('div', { class: 'toolbar' }, [el('h2', { text: 'Personas del curso' })]),
    notice(
      '',
      'Cuando confirmas quién es quién en un cuaderno, la app guarda el nombre y una huella '
      + 'de su voz. En la siguiente clase propone el nombre automáticamente, y tú solo '
      + 'confirmas.',
      'notice-info',
    ),
    el('article', { class: 'card' }, [
      el('h3', { text: 'Añadir a mano' }),
      el('div', { class: 'btn-row' }, [nameInput, roleSelect, addButton]),
    ]),
  ];

  if (!people.length) {
    parts.push(emptyState('Sin personas guardadas',
      'Se irán añadiendo al confirmar los hablantes de cada clase.'));
  } else {
    const list = el('div', { class: 'card' });
    for (const person of people) {
      list.append(el('div', { class: 'speaker-row' }, [
        el('span', { class: 'speaker-swatch', style: 'background:var(--primary)' }),
        el('div', {}, [
          el('b', { text: person.name }),
          el('span', { class: 'mono', text: ` · ${ROLE_LABELS[person.role] || person.role}` }),
        ]),
        el('span', {
          class: 'mono',
          text: `${person.sessions} clase${person.sessions === 1 ? '' : 's'}`,
        }),
        el('button', {
          class: 'btn btn-sm btn-danger', type: 'button',
          onClick: async () => {
            const ok = await confirmDialog({
              title: `Eliminar a ${person.name}`,
              message: 'Los cuadernos donde ya lo confirmaste mantienen el nombre escrito, '
                + 'pero dejará de proponerse en clases futuras.',
              confirmLabel: 'Eliminar',
              danger: true,
            });
            if (!ok) return;
            try {
              await speakersApi.deletePerson(person.id);
              renderPeople(container);
            } catch (error) {
              reportError(error);
            }
          },
        }, ['Eliminar']),
      ]));
    }
    parts.push(list);
  }
  mount(container, ...parts);
}
