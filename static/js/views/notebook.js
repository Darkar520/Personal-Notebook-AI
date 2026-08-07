/** Pantalla de un cuaderno: cabecera, pestañas, notas, línea de tiempo, transcript. */

import { content, live, mediaUrl, sessions as sessionsApi, speakers as speakersApi } from '../api.js';
import { isLoaded, loadSession, playRange } from '../player.js';
import {
  confirmDialog, debounce, el, emptyState, formatClock, formatDate, formatDuration,
  loading, mount, notice, reportError, statusPill, toast, withBusy,
} from '../ui.js';
import { renderStudyTab } from './study.js';

const TABS = [
  { id: 'notes', label: '📝 Notas' },
  { id: 'timeline', label: '🕒 Línea de tiempo' },
  { id: 'transcript', label: '📜 Transcripción' },
  { id: 'speakers', label: '🧑‍🤝‍🧑 ¿Quién es quién?' },
  { id: 'chat', label: '💬 Chat' },
  { id: 'podcast', label: '🎙️ Resumen de audio' },
  { id: 'quiz', label: '❓ Quiz' },
  { id: 'cards', label: '🃏 Flashcards' },
  { id: 'map', label: '🗺️ Mapa' },
  { id: 'roleplays', label: '🎭 Roleplays' },
];

export async function renderNotebook(container, { sessionId, tab = 'notes', navigate }) {
  mount(container, loading('Abriendo el cuaderno…'));
  let session;
  try {
    session = await sessionsApi.get(sessionId);
  } catch (error) {
    mount(container, notice('Cuaderno no disponible', error.message, 'notice-error'));
    return () => {};
  }

  const panel = el('section', { id: 'tab-panel' });
  const header = buildHeader(session, container, navigate);
  const tabsBar = el('div', { class: 'tabs', role: 'tablist' });
  for (const item of TABS) {
    tabsBar.append(
      el('button', {
        type: 'button', role: 'tab', dataset: { tab: item.id },
        'aria-selected': String(item.id === tab),
        onClick: () => navigate(`#/session/${sessionId}/${item.id}`),
      }, [item.label]),
    );
  }
  mount(container, header, tabsBar, panel);

  if (session.has_audio) {
    loadSession(session.id, mediaUrl.session(session.id), session.title);
  }

  await renderTab(panel, session, tab, navigate);

  // Refresco en vivo mientras la clase está en marcha o procesándose.
  const unsubscribers = [
    live.on('segments', (event) => {
      if (event.session_id === session.id && tab === 'transcript') {
        appendLiveSegments(panel, session, event.segments);
      }
    }),
    live.on('structure', (event) => {
      if (event.session_id === session.id && tab === 'notes') renderTab(panel, session, tab, navigate);
    }),
    live.on('session/status', async (event) => {
      if (event.session_id !== session.id) return;
      const fresh = await sessionsApi.get(session.id).catch(() => null);
      if (!fresh) return;
      const rebuilt = buildHeader(fresh, container, navigate);
      header.replaceWith(rebuilt);
      if (['done', 'empty', 'error'].includes(event.status)) {
        renderTab(panel, fresh, tab, navigate);
      }
    }),
    live.on('generated', (event) => {
      if (event.session_id === session.id && ['podcast', 'quiz', 'cards', 'map'].includes(tab)) {
        renderTab(panel, session, tab, navigate);
      }
    }),
  ];
  return () => unsubscribers.forEach((off) => off());
}

/* ------------------------------------------------------------------ cabecera */

function buildHeader(session, container, navigate) {
  const title = el('h2', { text: session.title || `Sesión ${session.session_number}` });
  title.contentEditable = 'true';
  title.spellcheck = false;
  title.title = 'Haz clic para renombrar (tu título tiene prioridad sobre el de la IA)';
  const save = debounce(async () => {
    const value = title.textContent.trim();
    if (!value || value === session.title) return;
    try {
      await sessionsApi.patch(session.id, { title: value });
      session.title = value;
      toast('Título actualizado', 'success', { timeout: 2500 });
    } catch (error) {
      reportError(error);
    }
  }, 900);
  title.addEventListener('input', save);
  title.addEventListener('blur', save);
  title.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      title.blur();
    }
  });

  const meta = el('div', { class: 'meta' }, [
    el('span', { text: `#${session.session_number}` }),
    el('span', { text: formatDate(session.started_at) }),
    el('span', { text: formatDuration(session.duration_sec) }),
    session.polish_model ? el('span', { class: 'mono', text: session.polish_model }) : null,
  ]);

  const actions = el('div', { class: 'btn-row' }, [
    el('button', {
      class: 'btn btn-ghost btn-sm', type: 'button',
      onClick: () => navigate('#/'),
    }, ['← Cuadernos']),
    session.has_audio
      ? el('button', {
        class: 'btn btn-sm', type: 'button',
        onClick: () => loadSession(session.id, mediaUrl.session(session.id), session.title),
      }, ['🔊 Cargar audio'])
      : null,
    el('a', { class: 'btn btn-sm', href: mediaUrl.export(session.id) }, ['⬇ Exportar']),
    ['done', 'error', 'empty'].includes(session.status)
      ? el('button', {
        class: 'btn btn-sm', type: 'button',
        title: 'Vuelve a generar notas, timeline y roleplays con el transcript actual',
        onClick: async (event) => withBusy(event.currentTarget, async () => {
          try {
            await sessionsApi.repolish(session.id);
            toast('Regenerando el cuaderno…', 'success');
          } catch (error) {
            reportError(error);
          }
        }),
      }, ['♻ Regenerar cuaderno'])
      : null,
  ]);

  const head = el('header', { class: 'card' }, [
    el('div', { class: 'card-head' }, [title, statusPill(session.status, session.status_detail)]),
    meta,
    session.status_detail && session.status !== 'done'
      ? el('p', { class: 'mono', text: session.status_detail })
      : null,
    actions,
  ]);
  if (session.status === 'processing') {
    head.append(el('progress', {
      value: String(session.progress || 0), max: '1', style: 'width:100%;margin-top:.6rem',
    }));
  }
  if (session.status === 'error') {
    head.append(retryRow(session, container, navigate));
  }
  return head;
}

function retryRow(session, container, navigate) {
  return el('div', { class: 'btn-row', style: 'margin-top:.6rem' }, [
    el('button', {
      class: 'btn btn-sm', type: 'button',
      onClick: async (event) => withBusy(event.currentTarget, async () => {
        try {
          const result = await sessionsApi.retryTranscription(session.id);
          toast(`${result.reactivated} fragmentos reencolados`, 'success');
        } catch (error) {
          reportError(error);
        }
      }),
    }, ['↻ Reintentar transcripción']),
  ]);
}

/* ------------------------------------------------------------------ pestañas */

async function renderTab(panel, session, tab, navigate) {
  mount(panel, loading());
  try {
    switch (tab) {
      case 'notes': return await renderNotes(panel, session);
      case 'timeline': return await renderTimeline(panel, session);
      case 'transcript': return await renderTranscript(panel, session);
      case 'speakers': return await renderSpeakers(panel, session);
      default: return await renderStudyTab(panel, session, tab, { navigate });
    }
  } catch (error) {
    reportError(error);
    mount(panel, notice('No se pudo cargar la pestaña', error.message, 'notice-error'));
    return undefined;
  }
}

/* ------------------------------------------------------------------ notas */

async function renderNotes(panel, session) {
  const [topics, usage] = await Promise.all([
    content.topics(session.id),
    content.usage(session.id).catch(() => null),
  ]);
  if (!topics.length) {
    mount(panel, emptyState(
      'Todavía no hay notas',
      session.status === 'recording'
        ? 'Las notas en borrador aparecen cada pocos minutos mientras la clase avanza.'
        : 'Cuando finalices la clase se generarán las notas, la línea de tiempo y los roleplays.',
    ));
    return;
  }

  const isDraft = topics[0].status === 'draft';
  const parts = [];
  if (isDraft) {
    parts.push(notice(
      'Borrador en vivo',
      'Estas notas se están construyendo durante la clase. Al detenerla se sustituyen por '
      + 'la versión final, con frases textuales, vocabulario y roleplays.',
      'notice-info',
    ));
  }
  for (const topic of topics) parts.push(topicCard(session, topic));
  if (usage && usage.total_usd) {
    parts.push(el('div', { class: 'usage' }, [
      el('span', {}, ['Transcripción: ', el('b', { text: `${usage.stt_minutes} min` })]),
      el('span', {}, ['Coste estimado: ', el('b', { text: `$${usage.total_usd.toFixed(2)}` })]),
      el('span', {}, ['Llamadas al modelo: ', el('b', { text: String(usage.llm_calls) })]),
    ]));
  }
  mount(panel, ...parts);
}

function topicCard(session, topic) {
  const card = el('article', {
    class: `card topic${topic.mastered ? ' mastered' : ''}`,
  });
  const head = el('div', { class: 'card-head' }, [
    el('h3', {}, [
      topic.wall_clock ? el('span', { class: 'mono', text: `${topic.wall_clock} · ` }) : null,
      topic.title,
    ]),
    topic.start_t !== null && topic.start_t !== undefined && isLoaded()
      ? el('button', {
        class: 'btn btn-sm btn-ghost', type: 'button', title: 'Escuchar este tema',
        onClick: () => playRange(topic.start_t, topic.end_t),
      }, ['▶'])
      : null,
    el('label', { class: 'check', title: 'Marcar como dominado' }, [
      el('input', {
        type: 'checkbox', checked: topic.mastered,
        onChange: async (event) => {
          try {
            await content.patchTopic(session.id, topic.id, {
              mastered: event.target.checked,
            });
            card.classList.toggle('mastered', event.target.checked);
          } catch (error) {
            reportError(error);
          }
        },
      }),
      'Dominado',
    ]),
  ]);
  card.append(head);

  if (topic.points.length) {
    card.append(el('ul', {}, topic.points.map((point) => el('li', { text: point }))));
  }
  for (const note of topic.spanish_notes) {
    card.append(el('div', { class: 'es-note', text: note }));
  }
  if (topic.phrases.length) {
    card.append(el('p', { class: 'section-label', text: 'Frases de la clase' }));
    card.append(el('ul', { class: 'phrases' }, topic.phrases.map((phrase) => el('li', {}, [
      el('span', { class: 'en', text: phrase.en }),
      phrase.es ? el('span', { class: 'es', text: phrase.es }) : null,
    ]))));
  }
  if (topic.vocab.length) {
    card.append(el('p', { class: 'section-label', text: 'Vocabulario nuevo' }));
    card.append(el('ul', { class: 'vocab' }, topic.vocab.map((item) => el('li', {}, [
      el('b', { text: item.word }),
      item.en_def ? el('span', { text: item.en_def }) : null,
      item.es ? el('span', { class: 'es', text: item.es }) : null,
      item.example_en ? el('span', { class: 'example', text: `“${item.example_en}”` }) : null,
    ]))));
  }
  card.append(editRow(session, topic, card));
  return card;
}

function editRow(session, topic, card) {
  return el('details', { style: 'margin-top:.7rem' }, [
    el('summary', { text: 'Editar a mano' }),
    el('p', {
      class: 'mono',
      text: 'Tus cambios tienen prioridad: al regenerar el cuaderno este tema no se sobrescribe.',
    }),
    (() => {
      const textarea = el('textarea', {
        'aria-label': 'Puntos del tema, uno por línea',
        value: topic.points.join('\n'),
      });
      textarea.value = topic.points.join('\n');
      const button = el('button', { class: 'btn btn-sm btn-primary', type: 'button' }, ['Guardar']);
      button.addEventListener('click', () => withBusy(button, async () => {
        const points = textarea.value.split('\n').map((line) => line.trim()).filter(Boolean);
        try {
          const updated = await content.patchTopic(session.id, topic.id, { points });
          card.replaceWith(topicCard(session, updated));
          toast('Tema actualizado', 'success');
        } catch (error) {
          reportError(error);
        }
      }));
      return el('div', { class: 'stack' }, [textarea, button]);
    })(),
  ]);
}

/* ------------------------------------------------------------------ timeline */

const KIND_LABELS = {
  topic: 'Tema', break: 'Receso', activity: 'Actividad', roleplay: 'Roleplay',
  closing: 'Cierre',
};

async function renderTimeline(panel, session) {
  const events = await content.timeline(session.id);
  if (!events.length) {
    mount(panel, emptyState('Sin línea de tiempo',
      'Se genera al finalizar la clase, con las horas reales y los recesos detectados.'));
    return;
  }
  const list = el('ul', { class: 'timeline' });
  for (const event of events) {
    list.append(el('li', {}, [
      el('span', {
        class: 'clock',
        text: event.wall_clock || formatClock(event.start_t),
        title: `${formatClock(event.start_t)} → ${formatClock(event.end_t)}`,
      }),
      el('div', { class: 'label' }, [
        el('div', {}, [event.label || KIND_LABELS[event.kind] || event.kind]),
        el('span', {
          class: `kind mono kind-${event.kind}`,
          text: `${KIND_LABELS[event.kind] || event.kind} · ${formatDuration(event.end_t - event.start_t)}`,
        }),
        event.note_md ? el('p', { class: 'mono', text: event.note_md }) : null,
      ]),
      isLoaded()
        ? el('button', {
          class: 'btn btn-sm btn-ghost', type: 'button', title: 'Escuchar este tramo',
          onClick: () => playRange(event.start_t, event.end_t),
        }, ['▶'])
        : null,
    ]));
  }
  const parts = [el('article', { class: 'card' }, [
    el('h3', { text: 'Cómo fue la clase' }), list,
  ])];
  if (!isLoaded()) {
    parts.unshift(notice('', 'Carga el audio de la clase para poder escuchar cada tramo.',
      'notice-info'));
  }
  mount(panel, ...parts);
}

/* ------------------------------------------------------------------ transcript */

async function renderTranscript(panel, session) {
  const [segments, speakerList] = await Promise.all([
    content.transcript(session.id),
    speakersApi.list(session.id).catch(() => []),
  ]);
  const names = new Map(speakerList.map((s) => [s.speaker_index, s]));

  const search = el('input', {
    type: 'search', placeholder: 'Buscar en la transcripción…',
    'aria-label': 'Buscar en la transcripción',
  });
  const log = el('div', { class: 'transcript', id: 'transcript-log' });
  log.dataset.sessionId = String(session.id);

  const draw = (rows, highlight = '') => {
    mount(log, ...rows.map((row) => turnRow(row, names, highlight)));
    if (!rows.length) log.append(el('p', { class: 'mono', text: 'Sin resultados.' }));
  };

  search.addEventListener('input', debounce(async () => {
    const query = search.value.trim();
    if (!query) {
      draw(segments);
      return;
    }
    try {
      draw(await content.search(session.id, query), query);
    } catch (error) {
      reportError(error);
    }
  }, 280));

  const parts = [
    el('div', { class: 'search-row' }, [
      search,
      el('button', {
        class: 'btn', type: 'button',
        onClick: () => { search.value = ''; draw(segments); },
      }, ['Limpiar']),
    ]),
  ];
  if (!segments.length) {
    parts.push(emptyState(
      'Sin transcripción todavía',
      session.status === 'recording'
        ? 'Los turnos aparecen aquí conforme se transcriben (unos segundos de retraso).'
        : 'No se transcribió audio en esta sesión.',
    ));
  } else {
    draw(segments);
    parts.push(log);
  }
  mount(panel, ...parts);
}

function turnRow(row, names, highlight = '') {
  const speaker = names.get(row.speaker_index);
  const label = speaker
    ? (speaker.name || speaker.suggested_name || (speaker.is_me ? 'Yo' : `Speaker ${row.speaker_index + 1}`))
    : (row.is_me ? 'Yo' : `Speaker ${row.speaker_index + 1}`);
  const textNode = el('div');
  if (highlight) {
    const parts = String(row.text).split(new RegExp(`(${escapeRegExp(highlight)})`, 'ig'));
    for (const part of parts) {
      textNode.append(
        part.toLowerCase() === highlight.toLowerCase()
          ? el('mark', { text: part })
          : document.createTextNode(part),
      );
    }
  } else {
    textNode.textContent = row.text;
  }
  return el('div', { class: `turn${row.is_me ? ' is-me' : ''}` }, [
    el('button', {
      class: 'clock', type: 'button',
      title: 'Escuchar este momento',
      onClick: () => {
        if (!playRange(row.start_t, row.end_t)) {
          toast('Carga primero el audio de la clase', 'warn');
        }
      },
    }, [row.wall_clock || formatClock(row.start_t)]),
    el('span', {
      class: 'who',
      text: label,
      style: speaker?.color ? `color:${speaker.color}` : '',
    }),
    textNode,
  ]);
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function appendLiveSegments(panel, session, segments) {
  const log = panel.querySelector('#transcript-log');
  if (!log || log.dataset.sessionId !== String(session.id)) return;
  for (const segment of segments) {
    log.append(turnRow({
      start_t: segment.start_t,
      end_t: segment.end_t,
      speaker_index: segment.speaker_index,
      is_me: segment.is_me,
      text: segment.text,
      wall_clock: '',
    }, new Map()));
  }
  log.lastElementChild?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

/* ------------------------------------------------------------------ speakers */

async function renderSpeakers(panel, session) {
  const [list, people] = await Promise.all([
    speakersApi.list(session.id),
    speakersApi.people().catch(() => []),
  ]);
  if (!list.length) {
    mount(panel, emptyState('Sin hablantes detectados',
      'Aparecerán cuando haya transcripción con diarización.'));
    return;
  }

  const card = el('article', { class: 'card' }, [
    el('h3', { text: '¿Quién es quién?' }),
    el('p', {
      class: 'mono',
      text: 'La IA propone los nombres a partir de lo que se dice en clase; nada se guarda '
        + 'hasta que confirmas. Al confirmar, la app recuerda la voz para las próximas clases.',
    }),
  ]);

  const rows = [];
  for (const speaker of list) {
    const nameInput = el('input', {
      type: 'text', list: 'known-people', placeholder: 'Nombre real',
      'aria-label': `Nombre del hablante ${speaker.speaker_index + 1}`,
    });
    nameInput.value = speaker.name || speaker.suggested_name || '';
    const roleSelect = el('select', { 'aria-label': 'Rol' });
    for (const [value, label] of [['teacher', 'Teacher'], ['me', 'Yo'],
      ['student', 'Compañero/a'], ['other', 'Otro']]) {
      roleSelect.append(el('option', { value, text: label }));
    }
    roleSelect.value = speaker.role || speaker.suggested_role || 'other';

    rows.push({ speaker, nameInput, roleSelect });
    card.append(el('div', { class: 'speaker-row' }, [
      el('span', {
        class: 'speaker-swatch',
        style: `background:${speaker.color || 'var(--border)'}`,
        'aria-hidden': 'true',
      }),
      nameInput,
      roleSelect,
      el('span', { class: 'mono', text: `${Math.round(speaker.talk_seconds / 60)} min` }),
    ]));
    const hints = [];
    if (speaker.auto_matched) hints.push('reconocido por su voz de otra clase');
    if (speaker.is_me) hints.push('detectado por tu micrófono');
    if (speaker.confirmed) hints.push('confirmado');
    card.append(el('p', { class: 'speaker-sample' }, [
      hints.length ? el('span', { class: 'tag', text: hints.join(' · ') }) : null,
      speaker.sample_text ? ` “${speaker.sample_text}”` : '',
    ]));
  }

  const datalist = el('datalist', { id: 'known-people' });
  for (const person of people) datalist.append(el('option', { value: person.name }));

  const saveButton = el('button', { class: 'btn btn-primary', type: 'button' },
    ['Guardar y recordar']);
  saveButton.addEventListener('click', () => withBusy(saveButton, async () => {
    const payload = {
      speakers: rows.map(({ speaker, nameInput, roleSelect }) => ({
        speaker_index: speaker.speaker_index,
        name: nameInput.value.trim(),
        role: roleSelect.value,
        remember: Boolean(nameInput.value.trim()),
      })),
    };
    try {
      await speakersApi.confirm(session.id, payload);
      toast('Personas confirmadas', 'success');
      renderSpeakers(panel, session);
    } catch (error) {
      reportError(error);
    }
  }));

  card.append(datalist, el('div', { class: 'btn-row', style: 'margin-top:.9rem' },
    [saveButton]));
  mount(panel, card);
}
