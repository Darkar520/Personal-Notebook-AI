"""Chatbot de la sesión: recuperación BM25 + respuesta del LLM (Fase 8).

Mejoras sobre el plan original (Task 8.1/8.2):

1. **Stopwords bilingües.** El plan solo filtraba stopwords inglesas, pero las preguntas
   llegan en español ("¿qué significa handle time?"). Sin filtrar "qué/significa/para/de",
   esas palabras dominan el ranking y devuelven segmentos irrelevantes.
2. **Expansión de vecinos.** Recuperar turnos sueltos deja al modelo sin contexto: se
   incluyen los turnos contiguos al mejor resultado, que es donde suele estar la
   explicación completa.
3. **Nombres reales y hora de pared en el contexto.** El plan pasaba `sp0`, `sp1`; así el
   modelo no puede responder "lo dijo Sara a las 9:12", que es justo el requisito del
   spec (§9: "referencia siempre qué parte de la clase sustenta la respuesta").
4. **Citas devueltas a la interfaz** (`citations`), para que la SPA pueda ofrecer "ir a
   ese momento del audio" en vez de solo texto.
5. **Historial desde la base de datos**, no desde el navegador: el plan confiaba el
   historial al cliente, así que abrir el cuaderno en otra pestaña perdía la conversación.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from app.ai import opencode_client, prompts

log = logging.getLogger(__name__)

STOP_EN = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are", "was",
    "were", "be", "been", "am", "how", "what", "when", "who", "why", "which", "where",
    "this", "that", "it", "its", "as", "at", "by", "with", "from", "do", "does", "did",
    "can", "could", "should", "would", "will", "we", "you", "i", "they", "he", "she",
    "my", "your", "our", "their", "me", "us", "them", "so", "if", "then", "than", "but",
    "not", "no", "yes", "there", "here", "about", "into", "over", "just", "very",
}
STOP_ES = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al", "a", "en",
    "y", "o", "que", "qué", "como", "cómo", "cuando", "cuándo", "donde", "dónde", "es",
    "son", "era", "eran", "ser", "estar", "esta", "este", "esto", "esa", "ese", "eso",
    "por", "para", "con", "sin", "sobre", "entre", "me", "te", "se", "nos", "le", "les",
    "lo", "mi", "tu", "su", "sus", "mis", "tus", "yo", "tú", "él", "ella", "nosotros",
    "significa", "significan", "dijo", "dice", "dijeron", "explica", "explicame",
    "explícame", "traduce", "cual", "cuál", "cuales", "cuáles", "hay", "muy", "más",
    "mas", "pero", "porque", "si", "no", "sí", "también", "todo", "toda", "algo",
}
STOPWORDS = STOP_EN | STOP_ES

_TOKEN_RE = re.compile(r"[a-záéíóúñü0-9]+", re.IGNORECASE)
NEIGHBOUR_WINDOW = 1
DEFAULT_K = 8
MAX_CONTEXT_SEGMENTS = 26


def tokenize(text: str) -> list[str]:
    """Tokens útiles: minúsculas, sin puntuación y sin palabras vacías EN/ES."""
    return [
        token
        for token in (m.group(0).lower() for m in _TOKEN_RE.finditer(str(text)))
        if token not in STOPWORDS and len(token) > 1
    ]


class BM25:
    """BM25 Okapi en Python puro. Sin dependencias y sobra para una sesión."""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.docs = docs
        self.k1, self.b = k1, b
        self.tokens = [tokenize(d) for d in docs]
        self.doclen = [len(t) for t in self.tokens]
        self.N = len(docs)
        self.avgdl = (sum(self.doclen) / self.N) if self.N else 0.0
        self.freqs: list[dict[str, int]] = []
        self.dfs: dict[str, int] = {}
        for tokens in self.tokens:
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            self.freqs.append(counts)
            for token in counts:
                self.dfs[token] = self.dfs.get(token, 0) + 1

    @classmethod
    def build(cls, docs: list[str]) -> "BM25":
        return cls(docs)

    def _idf(self, token: str) -> float:
        df = self.dfs.get(token, 0)
        return math.log(1 + (self.N - df + 0.5) / (df + 0.5))

    def score(self, query: str, index: int) -> float:
        if not self.N:
            return 0.0
        dl = self.doclen[index] or 1
        norm = self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
        total = 0.0
        for token in tokenize(query):
            freq = self.freqs[index].get(token, 0)
            if freq:
                total += self._idf(token) * (freq * (self.k1 + 1)) / (freq + norm)
        return total

    def ranked(self, query: str) -> list[tuple[int, float]]:
        scores = [(i, self.score(query, i)) for i in range(self.N)]
        scores.sort(key=lambda pair: (-pair[1], pair[0]))
        return scores

    def top(self, query: str, k: int = DEFAULT_K) -> list[int]:
        return [i for i, score in self.ranked(query)[:k] if score > 0] or [
            i for i, _ in self.ranked(query)[: min(k, self.N)]
        ]


@dataclass(slots=True)
class Segment:
    start_t: float
    end_t: float
    speaker_index: int
    text: str
    speaker_name: str = ""
    wall_clock: str = ""

    @property
    def label(self) -> str:
        who = self.speaker_name or f"Speaker {self.speaker_index + 1}"
        stamp = self.wall_clock or f"{int(self.start_t // 60)}:{int(self.start_t % 60):02d}"
        return f"[{stamp}] {who}"


def select_segments(segments: list[Segment], question: str, k: int = DEFAULT_K) -> list[int]:
    """Índices relevantes: BM25 + vecinos inmediatos, en orden cronológico."""
    if not segments:
        return []
    bm25 = BM25.build([s.text for s in segments])
    best = bm25.top(question, k=k)
    expanded: set[int] = set()
    for index in best:
        for offset in range(-NEIGHBOUR_WINDOW, NEIGHBOUR_WINDOW + 1):
            neighbour = index + offset
            if 0 <= neighbour < len(segments):
                expanded.add(neighbour)
    return sorted(expanded)[:MAX_CONTEXT_SEGMENTS]


def build_context(segments: list[Segment], question: str, k: int = DEFAULT_K
                  ) -> tuple[str, list[dict[str, Any]]]:
    indexes = select_segments(segments, question, k=k)
    lines: list[str] = []
    citations: list[dict[str, Any]] = []
    for index in indexes:
        segment = segments[index]
        lines.append(f"{segment.label}: {segment.text.strip()}")
        citations.append(
            {
                "start_t": round(segment.start_t, 2),
                "end_t": round(segment.end_t, 2),
                "wall_clock": segment.wall_clock,
                "speaker": segment.speaker_name or f"Speaker {segment.speaker_index + 1}",
                "text": segment.text.strip()[:180],
            }
        )
    return "\n".join(lines), citations


def answer(
    *,
    segments: list[Segment],
    topics: list[dict[str, Any]],
    question: str,
    history: list[dict[str, str]] | None = None,
    session_meta: dict[str, Any] | None = None,
    model: str,
    base_url: str,
    api_key: str,
    session_id: int | None = None,
    pricing: dict[str, Any] | None = None,
    k: int = DEFAULT_K,
) -> tuple[str, list[dict[str, Any]]]:
    """Responde una pregunta sobre la sesión. Devuelve `(respuesta, citas)`."""
    context, citations = build_context(segments, question, k=k)
    user = opencode_client.fit_text(
        json.dumps(
            {
                "session": session_meta or {},
                "notes": topics,
                "relevant_transcript": context,
                "question": question,
            },
            ensure_ascii=False,
        ),
        max_tokens=24_000,
    )
    reply = opencode_client.chat_text(
        prompts.CHAT_TUTOR,
        user,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=0.4,
        history=history or [],
        session_id=session_id,
        purpose="chat",
        pricing=pricing,
    )
    return reply.strip(), citations
