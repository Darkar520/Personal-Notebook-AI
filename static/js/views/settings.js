/** Ajustes: llaves, modelos, audio, preferencias y diagnóstico de conexión. */

import { settings as settingsApi } from '../api.js';
import {
  el, loading, mount, notice, reportError, toast, withBusy,
} from '../ui.js';

const MODEL_ROLES = [
  ['live', 'En vivo — notas en borrador durante la clase (prioriza modelos rápidos/baratos)'],
  ['polish', 'Libro final — pase de calidad al terminar (prioriza modelos más capaces)'],
  ['chat', 'Chat — chatbot del cuaderno'],
  ['podcast', 'Guion del podcast — síntesis del contenido de la clase'],
  ['study', 'Quiz, flashcards y mapa — materiales de estudio'],
];

// Valores recomendados para clases de 3,5 h (CAMBIO 3).
const RECOMMENDED_3_5H = {
  'settings.integration_interval_sec': 5,   // minutos en la UI
  'settings.break_min_seconds': 5,          // minutos en la UI
  'settings.podcast_minutes': 15,
  'settings.auto_generate_all': true,
  'settings.keep_raw_audio': false,
  'audio.chunk_seconds': 90,
  'audio.overlap_seconds': 8,
  'settings.stt_backend': 'deepgram',
};

export async function renderSettings(container) {
  mount(container, loading('Cargando ajustes…'));
  let config;
  let system;
  let devices;
  let catalog = [];
  try {
    [config, system, devices] = await Promise.all([
      settingsApi.get(),
      settingsApi.system().catch(() => null),
      settingsApi.devices().catch(() => ({})),
    ]);
    const modelsData = await settingsApi.models(false).catch(() => ({ catalog: [] }));
    catalog = modelsData.catalog || [];
  } catch (error) {
    reportError(error);
    mount(container, notice('No se pudo cargar', error.message, 'notice-error'));
    return;
  }

  const fields = new Map();
  const field = (path, node) => {
    fields.set(path, node);
    return node;
  };

  const keysSection = el('fieldset', {}, [
    el('legend', { text: 'Llaves de API' }),
    el('p', {
      class: 'mono',
      text: 'Se guardan solo en config.local.json, en tu equipo, con permisos restringidos. '
        + 'Deja el campo vacío para conservar la llave que ya tienes.',
    }),
    keyField('OpenCode Go (modelos de texto)', 'opencode', config, field),
    keyField('Deepgram (transcripción con diarización)', 'deepgram', config, field),
    keyField('Gemini (opcional, transcripción de respaldo)', 'gemini', config, field),
    el('label', {}, [
      'URL base de OpenCode',
      field('opencode.base_url', el('input', {
        type: 'url', value: config.opencode.base_url,
      })),
    ]),
  ]);

  const modelsSection = createModelsSection(catalog, config, field);

  const audioSection = el('fieldset', {}, [
    el('legend', { text: 'Audio y captura' }),
    el('label', {}, [
      'Qué se graba',
      el('span', {
        class: 'hint',
        text: '«Sistema + micrófono» captura además tu voz: no hace falta activar '
          + '«Hear my own voice» en Zoom y la app sabe con certeza cuándo hablas tú.',
      }),
      field('settings.capture_mode', selectField(config.settings.capture_mode, [
        ['loopback+mic', 'Sistema + micrófono (recomendado)'],
        ['loopback', 'Solo el audio del sistema'],
        ['mic', 'Solo el micrófono'],
      ])),
    ]),
    deviceSelect('Dispositivo de salida a capturar', 'audio.output_device_index',
      devices.loopback || [], config.audio.output_device_index, field),
    deviceSelect('Micrófono', 'audio.mic_device_index',
      devices.input || [], config.audio.mic_device_index, field),
    el('label', {}, [
      'Duración de cada fragmento (segundos)',
      el('span', {
        class: 'hint',
        text: '90 s es el punto óptimo para esta app: la transcripción llega ~2 s después '
          + 'de cada fragmento. Bajar a 30 s da notas más inmediatas pero triplica las '
          + 'peticiones a Deepgram (mismo costo, más llamadas). Subir a 180 s reduce '
          + 'llamadas pero las notas en borrador tardan más en aparecer.',
      }),
      el('div', { class: 'inline-field' }, [
        field('audio.chunk_seconds', el('input', {
          type: 'number', min: '30', max: '300', value: String(config.audio.chunk_seconds),
        })),
        minutesHint('audio.chunk_seconds', fields),
      ]),
    ]),
    el('label', {}, [
      'Solape entre fragmentos (segundos)',
      el('span', {
        class: 'hint',
        text: 'Evita perder palabras en el corte entre fragmentos. Con 8 s rara vez se '
          + 'pierde una frase. Bajar a 0 puede causar palabras cortadas; subir a 15 s no '
          + 'aporta más que 8 s.',
      }),
      field('audio.overlap_seconds', el('input', {
        type: 'number', min: '0', max: '20', value: String(config.audio.overlap_seconds),
      })),
    ]),
    el('label', { class: 'check' }, [
      field('settings.keep_raw_audio', el('input', {
        type: 'checkbox', checked: config.settings.keep_raw_audio,
      })),
      'Conservar WAV crudo además del MP3 (añade ~400 MB por clase de 3.5 h — no recomendado)',
    ]),
  ]);

  const behaviourSection = el('fieldset', {}, [
    el('legend', { text: 'Comportamiento' }),
    el('button', {
      class: 'btn btn-sm', type: 'button', id: 'apply-recommended',
    }, ['Aplicar configuración recomendada para clases de 3.5 h']),
    el('p', {
      class: 'hint',
      text: 'Aplica los valores óptimos para tu caso (clases de 3.5 h) sin guardar: '
        + 'revísalos y pulsa «Guardar ajustes».',
    }),
    el('label', {}, [
      'Cada cuántos minutos se actualizan las notas y resúmenes en vivo',
      el('span', {
        class: 'hint',
        text: 'Se configura en minutos. Cada 5 min es el balance recomendado: las notas y '
          + 'resúmenes se actualizan con frecuencia sin gastar llamadas innecesarias al modelo.',
      }),
      field('settings.integration_interval_sec', el('input', {
        type: 'number', min: '1', max: '30', step: '1',
        value: String(Math.round((config.settings.integration_interval_sec || 300) / 60)),
      })),
    ]),
    el('label', {}, [
      'Pausa mínima para marcar un receso (minutos)',
      el('span', {
        class: 'hint',
        text: 'Se configura en minutos. 5 min significa que deben pasar al menos cinco '
          + 'minutos sin actividad para marcar un receso, evitando confundir silencios '
          + 'cortos entre preguntas con una pausa real.',
      }),
      field('settings.break_min_seconds', el('input', {
        type: 'number', min: '1', max: '10', step: '1',
        value: String(Math.round((config.settings.break_min_seconds || 60) / 60)),
      })),
    ]),
    el('label', {}, [
      'Duración objetivo del podcast (minutos)',
      el('span', {
        class: 'hint',
        text: 'Para una clase de 3.5 h se recomienda 15–20 min. El podcast resume los '
          + 'puntos más importantes, no es una transcripción completa. Más de 30 min puede '
          + 'sonar repetitivo.',
      }),
      field('settings.podcast_minutes', el('input', {
        type: 'number', min: '2', max: '240', value: String(config.settings.podcast_minutes),
      })),
    ]),
    el('label', {}, [
      'Motor de transcripción',
      field('settings.stt_backend', selectField(config.settings.stt_backend, [
        ['deepgram', 'Deepgram Nova-3 (recomendado) — identifica quién habla, muy preciso en inglés, requiere internet y crédito ($1.20–1.50 por clase de 3.5 h, cubierto por los $200 de crédito gratuito ≈ 130–160 clases)'],
        ['whisper', 'Whisper local — gratis, funciona sin internet, pero NO identifica quién habla (todo sale como un solo hablante). Requiere instalar: pip install faster-whisper'],
        ['gemini', 'Gemini (respaldo de emergencia) — usa si Deepgram falla y no tienes Whisper. Calidad de diarización variable. Requiere llave de Gemini y pip install google-genai'],
      ])),
    ]),
    el('p', {
      class: 'hint',
      text: 'Para tu caso de uso (clase de inglés con 2–5 personas, 3.5 h) la mejor opción '
        + 'es Deepgram. Whisper solo tiene sentido si te quedas sin crédito o sin internet.',
    }),
    el('label', { class: 'check' }, [
      field('settings.auto_generate_all', el('input', {
        type: 'checkbox', checked: config.settings.auto_generate_all,
      })),
      'Al terminar la clase, generar automáticamente podcast, quiz, flashcards y mapa '
        + '(tarda ~3–5 min adicionales, requiere conexión)',
    ]),
    el('p', {
      class: 'hint',
      text: 'Recomendado activarlo. Así abres el cuaderno ya con todo listo sin tener que '
        + 'pulsar «Generar» en cada pestaña.',
    }),
    el('label', {}, [
      'Aviso cuando queden menos de (MB) libres',
      field('settings.min_free_space_mb', el('input', {
        type: 'number', min: '256', step: '256',
        value: String(config.settings.min_free_space_mb),
      })),
    ]),
    el('label', {}, [
      'Nivel de registro',
      field('settings.log_level', selectField(config.settings.log_level, [
        ['INFO', 'Normal'], ['DEBUG', 'Detallado (para depurar)'], ['WARNING', 'Solo avisos'],
      ])),
    ]),
  ]);

  const saveButton = el('button', { class: 'btn btn-primary', type: 'button' },
    ['Guardar ajustes']);
  const testButton = el('button', { class: 'btn', type: 'button' }, ['Probar conexión']);
  const report = el('div', { id: 'connection-report', class: 'stack' });

  saveButton.addEventListener('click', () => withBusy(saveButton, async () => {
    try {
      await settingsApi.save(collect(fields));
      toast('Ajustes guardados', 'success');
      renderSettings(container);
    } catch (error) {
      reportError(error);
    }
  }));
  testButton.addEventListener('click', () => withBusy(testButton, async () => {
    mount(report, loading('Comprobando proveedores…'));
    try {
      mount(report, ...connectionReport(await settingsApi.test()));
    } catch (error) {
      mount(report, notice('No se pudo comprobar', error.message, 'notice-error'));
    }
  }));

  mount(
    container,
    el('div', { class: 'toolbar' }, [el('h2', { text: 'Ajustes' })]),
    system ? systemCard(system) : null,
    el('form', { class: 'stack', onSubmit: (event) => event.preventDefault() }, [
      keysSection, modelsSection, audioSection, behaviourSection,
      el('div', { class: 'btn-row' }, [saveButton, testButton]),
      report,
    ]),
  );

  container.querySelector('#apply-recommended').addEventListener('click', () => {
    for (const [path, value] of Object.entries(RECOMMENDED_3_5H)) {
      const node = fields.get(path);
      if (!node) continue;
      if (node.type === 'checkbox') node.checked = Boolean(value);
      else node.value = String(value);
    }
    toast('Configuración recomendada aplicada. Revisa y pulsa «Guardar ajustes».', 'info');
  });

  bindModelsEvents(container, config, field);
}

/* ------------------------------------------------------------------ helpers */

function createModelsSection(catalog, config, field) {
  return el('fieldset', { id: 'models-section' }, [
    el('legend', { text: 'Modelos' }),
    el('p', {
      class: 'mono',
      text: 'Elige el modelo de cada tarea desde el catálogo real del proveedor. '
        + 'Si el identificador no existe, la app busca el más parecido en lugar de fallar.',
    }),
    ...MODEL_ROLES.map(([role, label]) => modelField(role, label, config, field, catalog)),
    el('div', { class: 'btn-row' }, [
      el('button', {
        class: 'btn btn-sm', type: 'button', id: 'refresh-models',
      }, ['↻ Actualizar catálogo']),
      el('button', {
        class: 'btn btn-sm', type: 'button', id: 'show-models',
      }, ['Ver catálogo de modelos']),
    ]),
    el('div', { id: 'models-output' }),
  ]);
}

/**
 * Enlaza los botones del catálogo. Debe llamarse SIEMPRE después de crear o
 * reemplazar `#models-section`, porque `replaceWith` elimina los listeners
 * de los nodos anteriores.
 */
function bindModelsEvents(container, config, field) {
  const refreshBtn = container.querySelector('#refresh-models');
  refreshBtn?.addEventListener('click', async (event) => {
    await withBusy(event.currentTarget, async () => {
      try {
        const data = await settingsApi.models(true);
        const fresh = data.catalog || [];
        const section = container.querySelector('#models-section');
        section.replaceWith(createModelsSection(fresh, config, field));
        // Re-enlazar: los botones nuevos no tienen listeners tras replaceWith.
        bindModelsEvents(container, config, field);
        toast(`Catálogo actualizado (${fresh.length} modelos)`, 'success');
      } catch (error) {
        reportError(error);
      }
    });
  });

  const showBtn = container.querySelector('#show-models');
  showBtn?.addEventListener('click', async (event) => {
    const output = container.querySelector('#models-output');
    await withBusy(event.currentTarget, async () => {
      try {
        const data = await settingsApi.models(true);
        mount(output, el('div', { class: 'card stack' }, [
          el('p', { class: 'section-label', text: 'Se usará' }),
          el('ul', {}, Object.entries(data.resolved).map(([role, model]) =>
            el('li', {}, [el('b', { text: `${role}: ` }), model || '—']))),
          el('p', { class: 'section-label', text: `Catálogo (${data.catalog.length})` }),
          el('p', { class: 'mono', text: data.catalog.join(', ') || 'sin datos' }),
        ]));
      } catch (error) {
        mount(output, notice('No se pudo leer el catálogo', error.message, 'notice-error'));
      }
    });
  });
}

function modelField(role, label, config, field, catalog) {
  const current = config.opencode.models[role] || '';
  const wrapper = el('label', {}, [label]);

  if (!catalog || !catalog.length) {
    // Sin llave configurada: el catálogo viene vacío. Mostramos un select deshabilitado
    // con aviso y dejamos un input de texto editable como fallback.
    const select = el('select', { disabled: true }, [
      el('option', { value: '', text: '— Configura la llave para ver el catálogo —' }),
    ]);
    const input = field(`opencode.models.${role}`, el('input', {
      type: 'text', value: current,
    }));
    wrapper.append(select, input);
    return wrapper;
  }

  const select = el('select', {});
  const options = [...catalog];
  if (current && !catalog.includes(current)) {
    options.push(current);
  }
  for (const model of options) {
    const isCurrent = model === current;
    select.append(el('option', {
      value: model,
      text: isCurrent && !catalog.includes(model) ? `${model} (no encontrado)` : model,
    }));
  }
  select.value = current || catalog[0] || '';
  wrapper.append(field(`opencode.models.${role}`, select));
  return wrapper;
}

function minutesHint(path, fields) {
  const span = el('span', { class: 'hint', text: '' });
  const update = () => {
    const node = fields.get(path);
    const value = Number(node?.value) || 0;
    span.textContent = value ? `≈ ${value} min` : '';
  };
  // Se actualiza al cambiar el input (se enlaza después de montar).
  setTimeout(() => {
    const node = fields.get(path);
    if (node) node.addEventListener('input', update);
    update();
  }, 0);
  return span;
}

function keyField(label, provider, config, field) {
  const block = config[provider] || {};
  const input = field(`${provider}.api_key`, el('input', {
    type: 'password', autocomplete: 'off', placeholder: block.api_key_set
      ? `Guardada (${block.api_key_masked}) — escribe una nueva para cambiarla`
      : 'Pega aquí tu llave',
  }));
  const clear = el('button', {
    class: 'btn btn-sm btn-danger', type: 'button', hidden: !block.api_key_set,
    onClick: async (event) => withBusy(event.currentTarget, async () => {
      try {
        await settingsApi.clearKey(provider);
        toast('Llave borrada', 'success');
        input.placeholder = 'Pega aquí tu llave';
        event.currentTarget.hidden = true;
      } catch (error) {
        reportError(error);
      }
    }),
  }, ['Borrar']);
  return el('label', {}, [
    label,
    el('div', { class: 'btn-row' }, [input, clear]),
  ]);
}

function selectField(current, options) {
  const select = el('select', {});
  for (const [value, label] of options) {
    select.append(el('option', { value, text: label }));
  }
  select.value = String(current ?? options[0][0]);
  return select;
}

function deviceSelect(label, path, list, current, field) {
  const select = el('select', {});
  select.append(el('option', { value: '', text: 'Automático (predeterminado del sistema)' }));
  for (const device of list) {
    select.append(el('option', {
      value: String(device.index),
      text: `${device.name}${device.is_default ? ' — predeterminado' : ''}`,
    }));
  }
  select.value = current === null || current === undefined ? '' : String(current);
  return el('label', {}, [
    label,
    list.length ? null : el('span', {
      class: 'hint', text: 'No se detectaron dispositivos (¿falta PyAudioWPatch?).',
    }),
    field(path, select),
  ]);
}

function collect(fields) {
  const payload = {};
  for (const [path, node] of fields) {
    let value;
    if (node.type === 'checkbox') value = node.checked;
    else if (node.type === 'number') value = node.value === '' ? null : Number(node.value);
    else value = node.value;
    if (path.endsWith('device_index')) value = value === '' ? null : Number(value);
    if (path.endsWith('api_key') && !String(value || '').trim()) continue;
    // Los campos de minutos se guardan en segundos en el backend.
    if (path === 'settings.integration_interval_sec' && value !== null) value = Math.round(value * 60);
    if (path === 'settings.break_min_seconds' && value !== null) value = Math.round(value * 60);
    const keys = path.split('.');
    let target = payload;
    for (const key of keys.slice(0, -1)) {
      target[key] = target[key] || {};
      target = target[key];
    }
    target[keys.at(-1)] = value;
  }
  return payload;
}

function systemCard(system) {
  const warnings = [];
  if (!system.keys.opencode) warnings.push('Falta la llave de OpenCode Go: sin ella no hay notas, chat ni materiales.');
  if (!system.keys.deepgram) warnings.push('Falta la llave de Deepgram: sin ella no hay transcripción.');
  if (!system.audio_capture) warnings.push('No se puede capturar audio: instala PyAudioWPatch.');
  if (!system.ffmpeg) warnings.push('Falta ffmpeg: no se podrá generar el MP3 ni el podcast.');

  return el('article', { class: 'card stack' }, [
    el('h3', { text: 'Estado del sistema' }),
    el('div', { class: 'usage' }, [
      el('span', {}, ['Versión: ', el('b', { text: system.version })]),
      el('span', {}, ['Espacio libre: ', el('b', { text: `${system.free_mb} MB` })]),
      el('span', {}, ['Grabación posible: ',
        el('b', { text: `${system.recording_minutes_left} min` })]),
      el('span', {}, ['En cola: ', el('b', { text: String(system.queue.total || 0) })]),
    ]),
    el('p', { class: 'mono', text: `Datos en ${system.data_dir}` }),
    ...warnings.map((message) => notice('', message)),
  ]);
}

function connectionReport(report) {
  const labels = {
    opencode: 'OpenCode Go (texto)',
    deepgram: 'Transcripción',
    tts: 'Voces del podcast (edge-tts)',
    audio: 'Captura de audio',
  };
  return Object.entries(labels).map(([key, label]) => {
    const check = report[key] || {};
    const extra = check.extra || {};
    const details = [];
    if (extra.balance_usd !== undefined) details.push(`saldo $${extra.balance_usd}`);
    if (extra.model) details.push(`modelo ${extra.model}`);
    if (extra.recording_minutes_left) details.push(`${extra.recording_minutes_left} min de disco`);
    return el('div', { class: `notice ${check.ok ? 'notice-info' : 'notice-error'}` }, [
      el('h3', { text: `${check.ok ? '✓' : '✕'} ${label}` }),
      el('p', { text: [check.detail, ...details].filter(Boolean).join(' · ') || '—' }),
    ]);
  });
}