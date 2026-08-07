# Checklist de beta: validación con clases reales (Fase 11)

Lo que las pruebas automáticas **no** pueden verificar: la calidad editorial de las notas,
el acierto de los nombres propuestos, la naturalidad del podcast y el comportamiento con
3,5 horas de audio real. Este guion cubre eso en tres clases.

Anota los problemas en `docs/beta-issues.md` con prioridad: **P0** (impide usar la app),
**P1** (molesta cada día), **P2** (mejora deseable).

---

## Antes de la primera clase (15 minutos)

- [ ] `python -m venv .venv` → `pip install -r requirements.txt`.
- [ ] `copy config.local.example.json config.local.json`.
- [ ] `python run.py` → aparece la burbuja y <http://127.0.0.1:8787> responde.
- [ ] Acepta el aviso de uso responsable (primera ejecución).
- [ ] **Ajustes**: pega las llaves de OpenCode Go y Deepgram → `Guardar` → `Probar conexión`.
      Las cuatro filas deben salir en verde: texto, transcripción, voces y audio.
- [ ] **Ajustes → Ver catálogo de modelos**: comprueba qué modelo se usará en cada rol. Si
      alguno sale raro, corrige el identificador aquí y no en el código.
- [ ] **Ajustes → Audio**: modo `Sistema + micrófono`. Comprueba que el dispositivo de
      salida detectado es el que usa Zoom (auriculares, altavoces, monitor HDMI…).
- [ ] **Espacio libre**: la tarjeta de estado del sistema dice cuántos minutos de grabación
      caben. Una clase de 3,5 h necesita ~475 MB con audio crudo y ~75 MB sin él.
- [ ] Ensayo en seco de 3 minutos: pon un vídeo en inglés, `▶ Iniciar clase`, habla un par
      de frases por el micrófono, espera 2 minutos y comprueba en la pestaña
      **Transcripción** que:
      - [ ] aparecen turnos en vivo (con unos segundos de retraso);
      - [ ] tus frases salen marcadas como tuyas (fondo resaltado);
      - [ ] el gadget muestra el cronómetro y el estado en verde.
      Después `⏹ Detener y finalizar` y borra el cuaderno de prueba.

---

## Clase 1 — Media clase (30–45 min de grabación)

**Objetivo:** validar el flujo completo con material real, sin arriesgar una clase entera.

Durante la clase:

- [ ] La burbuja se mantiene encima de Zoom y se puede arrastrar sin molestar.
- [ ] El cronómetro avanza; el estado sigue verde después de 20 minutos.
- [ ] Abre la web a mitad: la transcripción va al día y las **Notas (borrador)** ya tienen
      temas. Comprueba que no repiten la misma idea con otras palabras.
- [ ] Cierra el navegador y vuelve a abrirlo: el estado se recupera solo.

Al detener:

- [ ] El estado pasa a naranja con detalle ("Transcribiendo…", "Estructurando…").
- [ ] El cuaderno queda listo en menos de ~5 minutos.

Revisión del cuaderno:

- [ ] **Título**: reconocible una semana después. Si no, renómbralo (tu título queda fijado).
- [ ] **Notas**: puntos en inglés, concisos, sin relleno del tipo "the teacher explained
      that…". Las notas en español aparecen **solo** en lo genuinamente difícil.
- [ ] **Frases de la clase**: son citas reales, no inventadas. Verifica dos contra el audio.
- [ ] **Vocabulario**: términos que de verdad eran nuevos.
- [ ] **Línea de tiempo**: las horas coinciden con la clase real (±1 min). Los recesos
      marcados existieron.
- [ ] Pulsa ▶ en un tramo: suena **exactamente** ese intervalo y se detiene al final.
- [ ] **¿Quién es quién?**: ¿acertó los nombres? Confirma y anota cuántos de cuántos.
- [ ] Tras confirmar, los nombres aparecen en la transcripción y en el chat.

Anota en `docs/beta-issues.md`: nombres acertados / totales, temas detectados, y cualquier
nota redundante o traducción innecesaria (eso se ajusta en `app/ai/prompts.py`).

---

## Clase 2 — Clase completa (3,5 h) y materiales

**Objetivo:** el caso real de uso diario.

- [ ] Grabación completa sin intervención. El estado sigue verde al final.
- [ ] El consumo mostrado en **Notas** cuadra con lo esperado (~$1,2–1,6 de transcripción).
- [ ] El cuaderno final tiene: ≥1 tema bien delimitado por bloque, los recesos reales, los
      roleplays si los hubo, y el transcript completo sin huecos.
- [ ] Los hablantes **no se intercambian** a mitad de clase (revisa un turno del principio y
      otro del final del mismo hablante).
- [ ] La transcripción no repite frases en los cortes de fragmento (busca una frase larga
      cerca del minuto 1:30, 3:00, 4:30… y confirma que aparece una sola vez).
- [ ] **Chat**: pregunta algo que se dijo a mitad de clase. ¿Responde bien y cita el minuto?
      Pulsa la cita: debe sonar ese momento.
- [ ] **Chat en español**: "explícame X" responde en español con un ejemplo en inglés.
- [ ] **Chat**: "evalúame" hace 3 preguntas y corrige.
- [ ] **Podcast**: dura 3–5 minutos, se entiende, no lee asteriscos ni acotaciones, y el
      contenido es de *esta* clase.
- [ ] **Quiz**: 10 preguntas razonables, con una sola respuesta correcta y explicación útil.
- [ ] **Flashcards**: términos que merecen memorizarse; acertar sube de caja.
- [ ] **Mapa**: se lee, conecta los temas y no tiene nodos sueltos.
- [ ] **Roleplays**: contexto y devolución tienen sentido.
- [ ] Edita a mano un tema y pulsa `♻ Regenerar cuaderno`: **tu versión sobrevive**.
- [ ] Marca un tema como dominado: se atenúa y sigue regenerándose con el resto.

---

## Clase 3 — Robustez

**Objetivo:** que un imprevisto no cueste una clase.

- [ ] **Sin internet**: desconecta el wifi 5 minutos a mitad de clase. La grabación no se
      detiene, la burbuja avisa, y al reconectar la cola se vacía sola (mira el contador de
      fragmentos pendientes). El transcript final no tiene huecos.
- [ ] **Cambio de dispositivo**: cambia la salida de audio de Windows a mitad. La app avisa
      y se recupera; la línea de tiempo no se desplaza (el hueco se rellena con silencio).
- [ ] **Cierre inesperado**: mata el proceso (Administrador de tareas) con la clase
      grabando. Vuelve a lanzar `python run.py`: la lista de cuadernos ofrece
      **Finalizar / Descartar**. Finaliza y comprueba que el cuaderno sale bien con lo
      grabado hasta ese punto.
- [ ] **Llave inválida**: cambia la llave de Deepgram por una falsa y graba 2 minutos. Debe
      salir un aviso claro (no un error técnico) y `↻ Reintentar transcripción` debe
      funcionar tras corregir la llave.
- [ ] **Sin llave de texto**: borra la llave de OpenCode y abre el chat. Mensaje accionable
      ("Falta la llave…"), no un 500.
- [ ] **Exportar / restaurar**: exporta un cuaderno, bórralo, restaura el ZIP. Notas,
      transcript, timeline y audio vuelven. Abre el `notes.md` del ZIP: se lee sin la app.
- [ ] **Borrado total**: borra un cuaderno y comprueba que `data/sessions/<id>/` desaparece.
- [ ] **Disco**: baja `min_free_space_mb` a un valor por encima del espacio libre. Debe
      aparecer el aviso una sola vez, no repetido.

---

## Criterios de cierre (spec §16)

- [ ] Una clase real de 3,5 h produce línea de tiempo con horas, recesos, ≥1 tema por
      bloque, transcript completo, frases textuales, vocabulario y roleplays cuando existen.
- [ ] Los hablantes aparecen con **nombre real**, confirmados una vez por persona.
- [ ] Notas concisas, sin duplicados, en inglés con explicaciones puntuales en español.
- [ ] Podcast, quiz, flashcards y mapa son regenerables; consultarlos funciona sin internet.
- [ ] Borrar un cuaderno elimina todos sus datos, incluido el audio.
- [ ] Ajustes con prueba de conexión funcional.

## Ajuste fino después de la beta

Casi todo lo editorial se corrige en un solo fichero, `app/ai/prompts.py`:

| Síntoma | Dónde ajustar |
|---|---|
| Títulos largos o genéricos | `BOOK_TITLE` |
| Notas en español redundantes | `STYLE_RULES` |
| Temas demasiado troceados o demasiado gruesos | `BOOK_MAIN` (instrucciones de `timeline`) |
| Podcast largo, corto o acartonado | `Ajustes → duración`, y `PODCAST_SCRIPT` |
| Preguntas del quiz triviales | `QUIZ` |
| El chat no cita el minuto | `CHAT_TUTOR` |
| Nombres mal propuestos | `BOOK_SPEAKERS` y el umbral `SAME_PERSON_MIN` en `voiceprint.py` |
