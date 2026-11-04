"""Append-only SQLite research ledger. Immutable artifacts remain the source evidence."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from study.auth import ROOT

DEFAULT_DB = ROOT / ".local/expansion/ledger.sqlite3"


def now():
    return datetime.now(UTC).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def immutable_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical(value)
    if path.exists():
        if path.read_text("utf-8") != data:
            raise ValueError(f"Immutable artifact conflict: {path}")
        return
    with path.open("x", encoding="utf-8") as f:
        f.write(data)


class Ledger:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS configs(id TEXT PRIMARY KEY, hash TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, created TEXT NOT NULL, config TEXT NOT NULL,
                    trial TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_config ON events(config);
                CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events BEGIN
                    SELECT RAISE(ABORT, 'Events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events BEGIN
                    SELECT RAISE(ABORT, 'Events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_configs_update BEFORE UPDATE ON configs BEGIN
                    SELECT RAISE(ABORT, 'Configurations are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_configs_delete BEFORE DELETE ON configs BEGIN
                    SELECT RAISE(ABORT, 'Configurations are immutable'); END;
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def config(self, value):
        body = canonical(value)
        with self.connect() as db:
            old = db.execute(
                "SELECT body FROM configs WHERE id=?", (value["id"],)
            ).fetchone()
            if old and old["body"] != body:
                raise ValueError("Configuration changed: create a new configuration ID")
            db.execute(
                "INSERT OR IGNORE INTO configs VALUES(?,?,?)",
                (value["id"], digest(value), body),
            )

    def event(self, event_id, config, trial, kind, body, created=None):
        value = canonical(body)
        with self.connect() as db:
            old = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if old:
                if (old["config"], old["trial"], old["kind"], old["body"]) != (
                    config,
                    trial,
                    kind,
                    value,
                ):
                    raise ValueError("Conflicting event replay")
                return False
            db.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?)",
                (event_id, created or now(), config, trial, kind, value),
            )
        return True

    def snapshot(self):
        with self.connect() as db:
            db.execute("BEGIN")
            configs = [
                json.loads(r["body"])
                for r in db.execute("SELECT body FROM configs ORDER BY id")
            ]
            events = [
                dict(r) for r in db.execute("SELECT * FROM events ORDER BY created,id")
            ]
        for event in events:
            event["body"] = json.loads(event["body"])
        return configs, events

    def reserve(self, config, trial, role, request):
        # Unique primary key makes concurrent claims atomic. Never repurchase uncertain calls.
        claimed = self.event(
            f"{config}/{trial}/{role}/reserved",
            config,
            trial,
            "reserved",
            {"role": role, "request_sha256": digest(request)},
        )
        if not claimed:
            raise RuntimeError(
                "Attempt already reserved; reconcile instead of retrying"
            )


def usage(response):
    u = response.get("usage") or {}
    details = u.get("input_tokens_details") or u.get("prompt_tokens_details") or {}
    output_details = (
        u.get("output_tokens_details") or u.get("completion_tokens_details") or {}
    )
    return {
        "input": u.get("input_tokens", u.get("prompt_tokens")),
        "output": u.get("output_tokens", u.get("completion_tokens")),
        "cache_read": details.get("cached_tokens", 0),
        "cache_write": details.get("cache_write_tokens"),
        "reasoning_subset": output_details.get("reasoning_tokens", 0),
    }
