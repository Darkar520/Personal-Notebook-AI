"""Extracción tolerante de JSON de una respuesta de LLM.

**Hueco del plan original.** Todos los generadores hacían `json.loads(content)` sobre la
respuesta cruda del modelo. En la práctica, incluso pidiendo "return JSON only", los
modelos devuelven cosas como:

    Here is the structure:
    ```json
    {"topics": [ ... ], }
    ```

Un `json.loads` directo lanza `JSONDecodeError` y, en el pase final de una clase de
3,5 h, eso significa perder el libro entero (y el dinero de la llamada). Este módulo
recupera el JSON en cuatro pasadas: directo → sin vallas de código → recorte balanceado
→ reparaciones habituales (comas sobrantes, comillas tipográficas, comentarios,
`NaN`/`None`). Es puro texto: no ejecuta nada de lo que devuelva el modelo.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_LINE_COMMENT_RE = re.compile(r"^\s*//.*$", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_SMART_QUOTES = {
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2018": "'", "\u2019": "'", "\u00ab": '"', "\u00bb": '"',
}


class JsonExtractionError(ValueError):
    """No se pudo obtener JSON de la respuesta del modelo."""


def _strip_fences(text: str) -> str:
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned)
    return cleaned.strip()


def _balanced_slice(text: str) -> str | None:
    """Recorta el primer objeto/array JSON completo, respetando cadenas y escapes."""
    start = -1
    opener = ""
    for i, ch in enumerate(text):
        if ch in "{[":
            start, opener = i, ch
            break
    if start < 0:
        return None
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    # JSON truncado (el modelo se quedó sin tokens): cerramos lo que falte.
    if depth > 0:
        return text[start:] + closer * depth
    return None


def _repair(text: str) -> str:
    out = text
    for bad, good in _SMART_QUOTES.items():
        out = out.replace(bad, good)
    out = _BLOCK_COMMENT_RE.sub("", out)
    out = _LINE_COMMENT_RE.sub("", out)
    out = _TRAILING_COMMA_RE.sub(r"\1", out)
    out = re.sub(r"\bNaN\b|\bInfinity\b|\b-Infinity\b", "null", out)
    out = re.sub(r"\bNone\b", "null", out)
    out = re.sub(r"\bTrue\b", "true", out)
    out = re.sub(r"\bFalse\b", "false", out)
    return out


def loads(text: str) -> Any:
    """Devuelve el objeto Python del primer JSON válido encontrado en `text`."""
    if text is None:
        raise JsonExtractionError("Respuesta vacía del modelo")
    raw = str(text).strip()
    if not raw:
        raise JsonExtractionError("Respuesta vacía del modelo")

    candidates: list[str] = [raw]
    stripped = _strip_fences(raw)
    if stripped != raw:
        candidates.append(stripped)
    for base in list(candidates):
        sliced = _balanced_slice(base)
        if sliced and sliced not in candidates:
            candidates.append(sliced)
    candidates.extend(_repair(c) for c in list(candidates))

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    preview = raw[:280].replace("\n", " ")
    raise JsonExtractionError(f"Sin JSON válido en la respuesta: {preview!r}")


def as_dict(text: str) -> dict[str, Any]:
    """Como `loads` pero garantizando un dict (una lista suelta se envuelve)."""
    value = loads(text)
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value}
    raise JsonExtractionError(f"Se esperaba un objeto JSON, llegó {type(value).__name__}")


def pick_list(payload: Any, *keys: str) -> list[Any]:
    """Extrae una lista de un payload tolerando la clave que use el modelo.

    Los modelos alternan entre `{"topics": [...]}`, `{"items": [...]}` y `[...]` a secas.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in (*keys, "items", "data", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    # Único valor de tipo lista en el dict: lo damos por bueno.
    lists = [v for v in payload.values() if isinstance(v, list)]
    return lists[0] if len(lists) == 1 else []


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " ".join(as_str(v) for v in value if v is not None).strip()
    return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
            match = re.search(r"-?\d+(?:\.\d+)?", value)
            if not match:
                return default
            value = match.group(0)
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    return int(as_float(value, float(default)))


def as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        return [as_str(v) for v in value.values() if as_str(v)]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = as_str(item.get("text") or item.get("point") or item.get("value"))
            else:
                text = as_str(item)
            if text:
                out.append(text)
        return out
    return []
