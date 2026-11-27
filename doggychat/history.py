import json
import os
import tempfile
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


MAX_HISTORY_ENTRIES = 500


def default_history_path():
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / "DoggyChat" / "history.json"
    return Path.home() / ".local" / "share" / "doggychat" / "history.json"


def history_path_for_port(port, default_port):
    path = default_history_path()
    if port == default_port:
        return path
    return path.with_name(f"history-{port}.json")


class ChatHistory:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_history_path()
        self.identity_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._entries = deque(maxlen=MAX_HISTORY_ENTRIES)
        self._entry_ids = set()
        self._load()

    def entries(self):
        with self._lock:
            return [dict(entry) for entry in self._entries]

    def add(self, entry):
        return bool(self.add_many([entry]))

    def add_many(self, entries):
        records = [_validate_entry(entry) for entry in entries]
        with self._lock:
            previous_entries = list(self._entries)
            added = []
            for record in records:
                if record["id"] in self._entry_ids:
                    continue
                if len(self._entries) == MAX_HISTORY_ENTRIES:
                    expired = self._entries[0]["id"]
                    self._entry_ids.remove(expired)
                self._entries.append(record)
                self._entry_ids.add(record["id"])
                added.append(record)
            if not added:
                return []
            try:
                self._save()
            except OSError:
                self._entries = deque(previous_entries, maxlen=MAX_HISTORY_ENTRIES)
                self._entry_ids = {entry["id"] for entry in previous_entries}
                raise
            return added

    def replace(self, entries):
        records = [_validate_entry(entry) for entry in entries][-MAX_HISTORY_ENTRIES:]
        unique = []
        seen = set()
        for record in records:
            if record["id"] not in seen:
                unique.append(record)
                seen.add(record["id"])
        with self._lock:
            previous_entries = list(self._entries)
            self._entries = deque(unique, maxlen=MAX_HISTORY_ENTRIES)
            self._entry_ids = {entry["id"] for entry in unique}
            try:
                self._save()
            except OSError:
                self._entries = deque(previous_entries, maxlen=MAX_HISTORY_ENTRIES)
                self._entry_ids = {entry["id"] for entry in previous_entries}
                raise

    def clear(self):
        self.replace([])

    def new_entry(self, message_id, sender_id, nickname, text, timestamp=None):
        return _validate_entry(
            {
                "id": message_id,
                "sender_id": sender_id,
                "nickname": nickname,
                "text": text,
                "timestamp": timestamp
                or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )

    def _load(self):
        try:
            with self.path.open(encoding="utf-8") as history_file:
                data = json.load(history_file)
        except FileNotFoundError:
            self._save()
            return
        if not isinstance(data, dict):
            raise ValueError(f"Invalid chat history file: {self.path}")
        identity_id = data.get("identity_id")
        try:
            if uuid.UUID(hex=identity_id).hex != identity_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError(f"Invalid DoggyChat identity in {self.path}") from error
        entries = data.get("messages")
        if not isinstance(entries, list):
            raise ValueError(f"Invalid chat messages in {self.path}")
        self.identity_id = identity_id
        for entry in entries[-MAX_HISTORY_ENTRIES:]:
            record = _validate_entry(entry)
            if record["id"] not in self._entry_ids:
                self._entries.append(record)
                self._entry_ids.add(record["id"])

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "identity_id": self.identity_id,
            "messages": list(self._entries),
        }
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".history-",
                suffix=".tmp",
                delete=False,
            ) as history_file:
                temporary_path = Path(history_file.name)
                json.dump(data, history_file, ensure_ascii=False, separators=(",", ":"))
                history_file.flush()
                os.fsync(history_file.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def _validate_entry(entry):
    if not isinstance(entry, dict):
        raise ValueError("Invalid history record")
    record = {
        "id": entry.get("id"),
        "sender_id": entry.get("sender_id"),
        "nickname": entry.get("nickname"),
        "text": entry.get("text"),
        "timestamp": entry.get("timestamp"),
    }
    for key in ("id", "sender_id"):
        value = record[key]
        if not isinstance(value, str) or len(value) != 32:
            raise ValueError(f"Invalid history record {key}")
        try:
            if uuid.UUID(hex=value).hex != value:
                raise ValueError
        except ValueError as error:
            raise ValueError(f"Invalid history record {key}") from error
    if (
        not isinstance(record["nickname"], str)
        or not record["nickname"].strip()
        or len(record["nickname"]) > 32
        or not isinstance(record["text"], str)
        or not record["text"].strip()
        or len(record["text"]) > 2000
        or not isinstance(record["timestamp"], str)
        or len(record["timestamp"]) > 40
    ):
        raise ValueError("Invalid history record content")
    return record
