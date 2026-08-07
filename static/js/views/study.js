/** Pestañas generadas: chat, podcast, quiz, flashcards, mapa conceptual y roleplays. */

import { chat as chatApi, content, mediaUrl, study as studyApi } from '../api.js';
import { isLoaded, loadExternal, playRange } from '../player.js';
import {
  el, emptyState, formatClock, formatDuration, loading, markdown, mount, notice,
  reportError, toast, withBusy,
} from '../ui.js';

export async function renderStudyTab(panel, session, tab, { navigate }) {
  switch (tab) {
    case 'chat': return renderChat(panel, session);
    case 'podcast': return renderPodcast(panel, session);
    case 'quiz': return renderQuiz(panel, session);
    case 'cards': return renderFlashcards(panel, session);
    case 'map': return renderConceptMap(panel, session);
    case 'roleplays': return renderRoleplays(panel, session);
    default: return navigate(`#/session/${session.id}/notes`);
  }
}

/** Botón "generar/regenerar" reutilizado por todas las pestañas de materiales. */
function generateButton(label, task, { primary = true } = {}) {
  const button = el('button', {
    class: `btn ${primary ? 'btn-primary' : ''}`, type: 'button',
  }, [label]);
  button.addEventListener('click', () => withBusy(button, async () => {
    try {
      await task();
    } catch (error) {
      reportError(error);
    }
  }));
  return button;
}

/* ------------------------------------------------------------------ chat */

async function renderChat(panel, session) {
  const history = await chatApi.messages(session.id).catch(() => []);
  const log = el('div', { class: 'chat-log' });
  const input = el('input', {
    type: 'text', placeholder: 'Pregunta lo que quieras sobre esta clase…',
    'aria-label': 'Mensaje para el tutor', autocomplete: 'off',
  });
  const send = el('button', { class: 'btn btn-primary', type: 'submit' }, ['Enviar']);
  const form = el('form', { class: 'chat-form' }, [input, send]);

  const drawMessage = (message) => {
    const bubble = el('div', { class: `msg msg-${message.role}` });
    bubble.innerHTML = markdown(message.content);
    const citations = message.meta?.citations || message.citations || [];
    if (citations.length) {
      const row = el('div', { class: 'citations' });
      for (const citation of citations.slice(0, 6)) {
        row.append(el('button', {
          class: 'btn btn-sm btn-ghost', type: 'button',
          title: citation.text || 'Ir a este momento',
          onClick: () => {
            if (!playRange(citation.start_t, citation.end_t)) {
              toast('Carga primero el audio de la clase', 'warn');
            }
          },
        }, [`▶ ${citation.wall_clock || formatClock(citation.start_t)} · ${citation.speaker}`]));
      }
      bubble.append(row);
    }
    log.append(bubble);
    bubble.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  };

  for (const message of history) drawMessage(message);
  if (!history.length) {
    log.append(notice(
      'Tu tutor de esta clase',
      'Responde en el idioma en que le escribas, cita el momento de la clase en que se dijo '
      + 'y puede evaluarte. Prueba con «explícame handle time» o «evalúame».',
      'notice-info',
    ));
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message) return;
    input.value = '';
    drawMessage({ role: 'user', content: message });
    const pending = el('div', { class: 'msg msg-assistant', text: 'Pensando…' });
    log.append(pending);
    send.disabled = true;
    try {
      const answer = await chatApi.send(session.id, message);
      pending.remove();
      drawMessage({ role: 'assistant', content: answer.reply, citations: answer.citations });
    } catch (error) {
      pending.remove();
      reportError(error);
    } finally {
      send.disabled = false;
      input.focus();
    }
  });

  const hints = el('div', { class: 'chat-hints' });
  for (const hint of ['Resume la clase en 5 puntos', '¿Qué vocabulario nuevo hubo?',
    'Evalúame con 3 preguntas', 'Tradúceme las frases clave']) {
    hints.append(el('button', {
      class: 'btn btn-sm', type: 'button',
      onClick: () => { input.value = hint; form.requestSubmit(); },
    }, [hint]));
  }

  mount(panel, el('section', { class: 'card chat' }, [
    el('div', { class: 'card-head' }, [
      el('h3', { text: 'Chat de la clase' }),
      el('button', {
        class: 'btn btn-sm btn-ghost', type: 'button',
        onClick: async () => {
          try {
            await chatApi.clear(session.id);
            renderChat(panel, session);
          } catch (error) {
            reportError(error);
          }
        },
      }, ['Vaciar conversación']),
    ]),
    log, form, hints,
  ]));
  input.focus();
}

/* ------------------------------------------------------------------ podcast */

async function renderPodcast(panel, session) {
  const podcast = await studyApi.podcast(session.id).catch(() => null);
  const regenerate = generateButton(
    podcast ? '♻ Regenerar podcast' : '🎙️ Generar podcast',
    async () => {
      toast('Escribiendo el guion y sintetizando las voces… puede tardar un minuto.');
      await studyApi.makePodcast(session.id);
      toast('Podcast listo', 'success');
      renderPodcast(panel, session);
    },
  );

  if (!podcast) {
    mount(panel, el('article', { class: 'card' }, [
      el('h3', { text: 'Resumen en audio' }),
      el('p', {
        text: 'Una conversación de unos minutos entre dos voces repasando la clase. '
          + 'Se genera con edge-tts, sin coste.',
      }),
      regenerate,
    ]));
    return;
  }

  mount(panel, el('article', { class: 'card stack' }, [
    el('div', { class: 'card-head' }, [
      el('h3', { text: 'Resumen en audio' }),
      el('span', { class: 'tag', text: formatDuration(podcast.duration_sec) }),
    ]),
    el('audio', { controls: true, src: mediaUrl.podcast(session.id), style: 'width:100%' }),
    el('div', { class: 'btn-row' }, [
      regenerate,
      el('button', {
        class: 'btn', type: 'button',
        onClick: () => loadExternal(mediaUrl.podcast(session.id), 'Podcast de la clase'),
      }, ['Abrir en el reproductor']),
      el('a', {
        class: 'btn', href: mediaUrl.podcast(session.id), download: `podcast-${session.id}.mp3`,
      }, ['⬇ Descargar']),
    ]),
    el('details', {}, [
      el('summary', { text: 'Ver el guion' }),
      el('pre', {
        style: 'white-space:pre-wrap;font-size:.88rem', text: podcast.script,
      }),
    ]),
  ]));
}

/* ------------------------------------------------------------------ quiz */

async function renderQuiz(panel, session) {
  const questions = await studyApi.quiz(session.id).catch(() => []);
  const count = el('select', { 'aria-label': 'Número de preguntas' });
  for (const value of [5, 8, 10, 15, 20]) {
    count.append(el('option', { value: String(value), text: `${value} preguntas` }));
  }
  count.value = '10';

  const generate = generateButton(
    questions.length ? '♻ Nuevas preguntas' : '❓ Generar quiz',
    async () => {
      await studyApi.makeQuiz(session.id, Number(count.value));
      toast('Quiz generado', 'success');
      renderQuiz(panel, session);
    },
  );

  const header = el('div', { class: 'toolbar' }, [
    el('h3', { text: 'Quiz de práctica' }),
    el('span', { class: 'spacer' }),
    count, generate,
  ]);

  if (!questions.length) {
    mount(panel, header, emptyState('Sin preguntas todavía',
      'Genera un quiz con el contenido de esta clase para comprobar qué recuerdas.'));
    return;
  }

  const answers = new Map();
  const list = el('div', { class: 'stack' });
  questions.forEach((question, index) => {
    const options = el('div', { class: 'quiz-options', role: 'radiogroup' });
    question.options.forEach((option, optionIndex) => {
      const label = el('label', { class: 'quiz-option' }, [
        el('input', {
          type: 'radio', name: `q${question.id}`, value: String(optionIndex),
          onChange: () => answers.set(question.id, optionIndex),
        }),
        el('span', { text: option }),
      ]);
      options.append(label);
    });
    list.append(el('article', { class: 'card quiz-question' }, [
      el('h4', { text: `${index + 1}. ${question.question}` }),
      options,
      el('div', { class: 'quiz-explanation', hidden: true, dataset: { role: 'explanation' } },
        [question.explanation || '']),
    ]));
  });

  const score = el('p', { class: 'score', role: 'status' });
  const check = el('button', { class: 'btn btn-primary', type: 'button' }, ['Corregir']);
  check.addEventListener('click', () => {
    let correct = 0;
    questions.forEach((question, index) => {
      const card = list.children[index];
      const labels = card.querySelectorAll('.quiz-option');
      labels.forEach((label, optionIndex) => {
        label.classList.remove('correct', 'wrong');
        if (optionIndex === question.correct_index) label.classList.add('correct');
        else if (answers.get(question.id) === optionIndex) label.classList.add('wrong');
      });
      const explanation = card.querySelector('[data-role="explanation"]');
      if (explanation.textContent.trim()) explanation.hidden = false;
      if (answers.get(question.id) === question.correct_index) correct += 1;
    });
    score.textContent = `${correct} de ${questions.length} correctas`;
  });

  mount(panel, header, list, el('div', { class: 'btn-row' }, [check, score]));
}

/* ------------------------------------------------------------------ flashcards */

async function renderFlashcards(panel, session) {
  const cards = await studyApi.flashcards(session.id).catch(() => []);
  const generate = generateButton(
    cards.length ? '♻ Nuevas tarjetas' : '🃏 Generar flashcards',
    async () => {
      await studyApi.makeFlashcards(session.id, 20);
      toast('Flashcards generadas', 'success');
      renderFlashcards(panel, session);
    },
  );
  const header = el('div', { class: 'toolbar' }, [
    el('h3', { text: 'Flashcards' }),
    el('span', { class: 'spacer' }),
    cards.length
      ? el('button', {
        class: 'btn', type: 'button',
        onClick: () => renderFlashcards(panel, session, true),
      }, ['🔀 Barajar'])
      : null,
    generate,
  ]);

  if (!cards.length) {
    mount(panel, header, emptyState('Sin tarjetas',
      'Genera tarjetas con el vocabulario y las frases de la clase. '
      + 'Acertar sube la tarjeta de caja y retrasa el repaso; fallar la devuelve a la primera.'));
    return;
  }

  const shuffled = [...cards].sort(() => Math.random() - 0.5);
  const grid = el('div', { class: 'cards' });
  for (const card of shuffled) grid.append(flashcard(session, card));
  mount(panel, header, el('p', {
    class: 'mono',
    text: `${cards.length} tarjetas · caja 1 = por aprender, caja 5 = dominada`,
  }), grid);
}

function flashcard(session, card) {
  const front = el('div', { class: 'front', text: card.front });
  const back = el('div', { class: 'back', hidden: true });
  back.innerHTML = markdown(card.back_md);
  const node = el('button', { class: 'flashcard', type: 'button' }, [
    el('span', { class: 'tag box', text: `Caja ${card.box}` }),
    front, back,
  ]);
  const actions = el('div', { class: 'flashcard-actions', hidden: true });
  for (const [label, correct, cls] of [['✓ La sabía', true, ''], ['✕ Fallé', false, 'btn-danger']]) {
    actions.append(el('button', {
      class: `btn btn-sm ${cls}`, type: 'button',
      onClick: async (event) => {
        event.stopPropagation();
        try {
          const updated = await studyApi.review(session.id, card.id, correct);
          node.replaceWith(flashcard(session, updated));
        } catch (error) {
          reportError(error);
        }
      },
    }, [label]));
  }
  node.append(actions);
  node.addEventListener('click', () => {
    const revealed = !back.hidden;
    back.hidden = revealed;
    front.hidden = false;
    actions.hidden = revealed;
  });
  return node;
}

/* ------------------------------------------------------------------ mapa */

async function renderConceptMap(panel, session) {
  const layout = await studyApi.conceptMap(session.id).catch(() => ({ nodes: [], edges: [] }));
  const generate = generateButton(
    layout.nodes.length ? '♻ Regenerar mapa' : '🗺️ Generar mapa',
    async () => {
      await studyApi.makeConceptMap(session.id);
      toast('Mapa generado', 'success');
      renderConceptMap(panel, session);
    },
  );
  const header = el('div', { class: 'toolbar' }, [
    el('h3', { text: 'Mapa conceptual' }),
    el('span', { class: 'spacer' }),
    generate,
  ]);
  if (!layout.nodes.length) {
    mount(panel, header, emptyState('Sin mapa',
      'Un esquema visual con el tema central, los subtemas y sus términos clave.'));
    return;
  }
  mount(panel, header, el('article', { class: 'card' }, [drawMap(layout)]));
}

/**
 * Render SVG por capas (BFS desde la raíz). No usamos una librería de grafos: con ≤16
 * nodos jerárquicos, un layout por niveles es más legible que un `force-directed` y no
 * añade 100 KB de dependencias a una app que debe abrirse sin internet.
 */
function drawMap(layout) {
  const NS = 'http://www.w3.org/2000/svg';
  const width = 960;
  const nodes = new Map(layout.nodes.map((node) => [node.id, { ...node, children: [] }]));
  const hasParent = new Set();
  for (const edge of layout.edges) {
    if (nodes.has(edge.from) && nodes.has(edge.to) && edge.from !== edge.to) {
      nodes.get(edge.from).children.push(edge.to);
      hasParent.add(edge.to);
    }
  }
  const roots = layout.nodes.filter((node) => !hasParent.has(node.id)).map((node) => node.id);
  const levels = [];
  const seen = new Set();
  let frontier = roots.length ? roots : [layout.nodes[0].id];
  while (frontier.length) {
    const level = frontier.filter((id) => !seen.has(id));
    level.forEach((id) => seen.add(id));
    if (!level.length) break;
    levels.push(level);
    frontier = level.flatMap((id) => nodes.get(id).children);
  }
  for (const node of layout.nodes) {
    if (!seen.has(node.id)) {
      levels[levels.length - 1] = (levels[levels.length - 1] || []).concat(node.id);
      seen.add(node.id);
    }
  }

  const rowHeight = 110;
  const height = Math.max(260, levels.length * rowHeight + 40);
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'concept-map');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'Mapa conceptual de la sesión');

  const positions = new Map();
  levels.forEach((level, levelIndex) => {
    const step = width / (level.length + 1);
    level.forEach((id, index) => {
      positions.set(id, { x: step * (index + 1), y: 46 + levelIndex * rowHeight });
    });
  });

  for (const edge of layout.edges) {
    const from = positions.get(edge.from);
    const to = positions.get(edge.to);
    if (!from || !to) continue;
    const line = document.createElementNS(NS, 'path');
    line.setAttribute('class', 'edge');
    line.setAttribute('fill', 'none');
    const midY = (from.y + to.y) / 2;
    line.setAttribute('d', `M ${from.x} ${from.y + 16} C ${from.x} ${midY}, ${to.x} ${midY}, ${to.x} ${to.y - 18}`);
    svg.append(line);
  }

  for (const node of layout.nodes) {
    const position = positions.get(node.id);
    if (!position) continue;
    const group = document.createElementNS(NS, 'g');
    group.setAttribute('class', `node-${node.group || 'term'}`);
    const label = String(node.label || '');
    const boxWidth = Math.min(220, Math.max(72, label.length * 8 + 22));
    const rect = document.createElementNS(NS, 'rect');
    rect.setAttribute('x', String(position.x - boxWidth / 2));
    rect.setAttribute('y', String(position.y - 17));
    rect.setAttribute('width', String(boxWidth));
    rect.setAttribute('height', '34');
    rect.setAttribute('rx', '9');
    rect.setAttribute('stroke-width', '1.2');
    const text = document.createElementNS(NS, 'text');
    text.setAttribute('x', String(position.x));
    text.setAttribute('y', String(position.y + 5));
    text.setAttribute('text-anchor', 'middle');
    text.textContent = label.length > 26 ? `${label.slice(0, 25)}…` : label;
    const title = document.createElementNS(NS, 'title');
    title.textContent = label;
    group.append(rect, text, title);
    svg.append(group);
  }
  return svg;
}

/* ------------------------------------------------------------------ roleplays */

async function renderRoleplays(panel, session) {
  const roleplays = await content.roleplays(session.id);
  if (!roleplays.length) {
    mount(panel, emptyState('Sin roleplays',
      'Si en la clase hubo prácticas de conversación, aparecerán aquí con su contexto, '
      + 'las frases clave y una devolución.'));
    return;
  }
  const parts = roleplays.map((roleplay) => el('article', { class: 'card stack' }, [
    el('div', { class: 'card-head' }, [
      el('h3', { text: roleplay.title }),
      roleplay.start_t !== null && isLoaded()
        ? el('button', {
          class: 'btn btn-sm btn-ghost', type: 'button',
          onClick: () => playRange(roleplay.start_t, roleplay.end_t),
        }, ['▶ Escuchar'])
        : null,
    ]),
    roleplay.your_role ? el('p', {}, [el('b', { text: 'Tu papel: ' }), roleplay.your_role]) : null,
    roleplay.context_md ? el('p', { text: roleplay.context_md }) : null,
    roleplay.participants.length
      ? el('p', { class: 'mono', text: `Con: ${roleplay.participants.join(', ')}` })
      : null,
    roleplay.key_phrases.length
      ? el('div', {}, [
        el('p', { class: 'section-label', text: 'Frases que tocaba usar' }),
        el('ul', {}, roleplay.key_phrases.map((phrase) => el('li', {
          text: typeof phrase === 'string' ? phrase : (phrase.en || JSON.stringify(phrase)),
        }))),
      ])
      : null,
    roleplay.feedback_md
      ? el('div', { class: 'es-note', text: roleplay.feedback_md })
      : null,
  ]));
  mount(panel, ...parts);
}
