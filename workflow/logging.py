"""Stash's encoded stderr logging; stdout remains the raw JSON result."""

import re
import sys

VERSION = "0.2.1"


def overall_message(progress):
    if progress["id"] is None:
        return "Gesamt: aktuell kein Lauf vorhanden"
    remaining = progress["remaining_seconds"]
    if progress["status"] == "complete":
        eta = "abgeschlossen"
    elif progress["status"] == "blocked":
        eta = "verbleibende Fehler muessen geklaert werden"
    elif progress["status"] in {"collecting", "incomplete"}:
        eta = "Einreihen noch nicht vollstaendig abgeschlossen"
    elif remaining is None:
        eta = "Restzeit noch nicht schaetzbar"
    else:
        minutes, seconds = divmod(remaining, 60)
        hours, minutes = divmod(minutes, 60)
        eta = f"Restzeit ca. {hours}h {minutes}min {seconds}s"
    outcomes = ", ".join(
        f"{label}={progress.get(key, 0)}"
        for key, label in [
            ("deferred", "zurueckgestellt"),
            ("skipped", "uebersprungen"),
            ("planned", "Testlauf"),
        ]
        if progress.get(key)
    )
    details = f"; {outcomes}" if outcomes else ""
    return (
        f"Gesamt: {progress['done'] + progress['inherited']}/{progress['total']} Aufgaben abgearbeitet, "
        f"verworfen={progress['cancelled']}, offen={progress['pending']}, "
        f"fehlgeschlagen={progress['failed']}{details}; {progress['percent']:.1f}%; {eta}"
    )


class Log:
    def __init__(self, config=None):
        self.config = config

    def write(self, level, message):
        text = str(message)
        if self.config is not None:
            text = self.config.scrub(text)
        text = re.sub(r"[\x00-\x1f\x7f]", " ", text)[:2000]
        try:
            print(f"\x01{level}\x02{text}", file=sys.stderr, flush=True)
        except (OSError, UnicodeError):
            # A closed log stream must not fail or repeat a media operation.
            pass

    def info(self, message):
        self.write("i", message)

    def warning(self, message):
        self.write("w", message)

    def error(self, message):
        self.write("e", message)

    def debug(self, message):
        self.write("d", message)

    def progress(self, fraction):
        self.write("p", f"{max(0.0, min(1.0, fraction)):.4f}")


def entity(kind, entity_id, display_name=None):
    name = {
        "scene": "Szene",
        "performer": "Darsteller",
        "scene_delete": "Szenenloeschung",
        "favorites": "Favoriten",
        "rebuild": "Zuordnungsaufbau",
        "cleanup": "Ordnerbereinigung",
    }.get(kind, kind)
    if display_name:
        return f'{name} "{display_name}" (ID={entity_id})'
    return f"{name} ID={entity_id}"
