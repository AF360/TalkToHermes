from __future__ import annotations

from typing import Literal

ResponseStyle = Literal["short", "normal", "detailed"]
RESPONSE_STYLES = frozenset({"short", "normal", "detailed"})

DEFAULT_VOICE_INSTRUCTIONS = (
    "Du antwortest in einer gesprochenen Unterhaltung. Antworte direkt, "
    "natürlich und ohne Begrüßungs-, Bestätigungs- oder Schlussfloskeln. Verwende keine "
    "Überschriften, Tabellen, langen Aufzählungen, URLs, Dateipfade oder vorgelesenen "
    "Codeblöcke. Nenne zuerst die eigentliche Antwort. Erkläre Details nur auf ausdrückliche "
    "Nachfrage oder wenn sie für Sicherheit und Korrektheit notwendig sind. "
    "Bei komplexen Ergebnissen fasse das Wesentliche zusammen und biete knapp weitere "
    "Details an. Stelle bei echter Unklarheit genau eine kurze Rückfrage. Kürze niemals "
    "notwendige Sicherheits-, Freigabe- oder Unsicherheitshinweise."
)

_STYLE_INSTRUCTIONS: dict[str, str] = {
    "short": (
        "Antwortlänge KURZ: Antworte gewöhnlich in ein bis drei Sätzen und höchstens "
        "etwa 20 bis 60 gesprochenen Sekunden."
    ),
    "normal": (
        "Antwortlänge NORMAL: Gib eine kompakte Erklärung mit den wichtigsten Details; "
        "vermeide unnötige Vollständigkeit."
    ),
    "detailed": (
        "Antwortlänge AUSFÜHRLICH: Du darfst ausführlich erklären, bleibst aber für "
        "gesprochene Wiedergabe klar gegliedert und vermeidest vorgelesene Rohdaten."
    ),
}


def validate_response_style(value: str) -> str:
    if value not in RESPONSE_STYLES:
        raise ValueError("unsupported response style")
    return value


def build_voice_instructions(base: str, response_style: str) -> str:
    style = validate_response_style(response_style)
    normalized = base.strip()
    if not normalized:
        raise ValueError("voice instructions are required")
    return f"{normalized}\n\n{_STYLE_INSTRUCTIONS[style]}"
