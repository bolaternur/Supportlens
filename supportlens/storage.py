"""SQLite persistence. Customer intake commits before optional API processing."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import time

from .data import ARTICLES, DEMO_MESSAGES, PRIORITIES, STATUSES, TOPICS
from .engine import ROOT, Settings, analyze, demo_analysis, suggested_status, validate_sources

LOCAL_TZ = timezone(timedelta(hours=5))


def now():
    return datetime.now(timezone.utc).isoformat()


def db_path():
    path = Path(os.getenv("SUPPORTLENS_DB", "data/supportlens.sqlite3"))
    return path if path.is_absolute() else ROOT / path


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            with con:
                yield con
        finally:
            con.close()

    def initialize(self, seed=True):
        with self.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS articles (id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    topic TEXT NOT NULL, summary TEXT NOT NULL, body TEXT NOT NULL,
                    keywords TEXT NOT NULL, fields TEXT NOT NULL, action TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, seed_key TEXT UNIQUE,
                    message TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    is_demo INTEGER NOT NULL DEFAULT 0, topic TEXT NOT NULL DEFAULT 'другое',
                    priority TEXT NOT NULL DEFAULT 'обычный', status TEXT NOT NULL DEFAULT 'новое',
                    topic_reason TEXT NOT NULL DEFAULT '', priority_reason TEXT NOT NULL DEFAULT '',
                    sources TEXT NOT NULL DEFAULT '[]', missing_fields TEXT NOT NULL DEFAULT '[]',
                    action TEXT NOT NULL DEFAULT 'Нужно уточнение', draft TEXT NOT NULL DEFAULT '',
                    approved_answer TEXT, approved_at TEXT, mode TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '', elapsed_ms REAL, ai_ms REAL,
                    manual_override INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS processing_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                    created_at TEXT NOT NULL, mode TEXT NOT NULL, elapsed_ms REAL NOT NULL,
                    ai_ms REAL, error TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                    created_at TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS tickets_filters ON tickets(topic, priority, status);
            """)
            for a in ARTICLES:
                con.execute("INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?,?)", (
                    a["id"], a["title"], a["topic"], a["summary"], a["body"],
                    json.dumps(a["keywords"], ensure_ascii=False), json.dumps(a["fields"]), a["action"]))
        if seed:
            self.seed_once()

    def articles(self):
        with self.connection() as con:
            rows = con.execute("SELECT * FROM articles ORDER BY id").fetchall()
        return [{**dict(row), "keywords": json.loads(row["keywords"]), "fields": json.loads(row["fields"])} for row in rows]

    @staticmethod
    def decode(row):
        if row is None:
            return None
        return {**dict(row), "sources": json.loads(row["sources"]), "missing_fields": json.loads(row["missing_fields"])}

    def tickets(self, topic=None, priority=None, status=None, demo=None):
        clauses, args = [], []
        for name, value in [("topic", topic), ("priority", priority), ("status", status), ("is_demo", demo)]:
            if value is not None:
                clauses.append(name + " = ?")
                args.append(value)
        query = "SELECT * FROM tickets" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at DESC, id DESC"
        with self.connection() as con:
            return [self.decode(row) for row in con.execute(query, args).fetchall()]

    def ticket(self, id):
        with self.connection() as con:
            return self.decode(con.execute("SELECT * FROM tickets WHERE id = ?", (id,)).fetchone())

    def create_ticket(self, message):
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 6000:
            raise ValueError("Введите обращение от 1 до 6000 символов.")
        timestamp = now()
        with self.connection() as con:
            return con.execute("INSERT INTO tickets(message,created_at,updated_at) VALUES (?,?,?)",
                               (message.strip(), timestamp, timestamp)).lastrowid

    def _event(self, con, id, event, payload):
        con.execute("INSERT INTO audit(ticket_id,created_at,event,payload) VALUES (?,?,?,?)",
                    (id, now(), event, json.dumps(payload, ensure_ascii=False)))

    def _apply_result(self, con, id, result, manual=False):
        self._require(con, id)
        validate_sources(result["sources"], self.articles())
        keys = ("topic", "priority", "topic_reason", "priority_reason", "action", "draft", "mode", "error", "elapsed_ms", "ai_ms")
        values = [result.get(k) for k in keys]
        con.execute("UPDATE tickets SET " + ",".join(k + "=?" for k in keys) +
                    ", sources=?, missing_fields=?, status=?, updated_at=?, approved_answer=NULL, approved_at=NULL, manual_override=? WHERE id=?",
                    (*values, json.dumps(result["sources"], ensure_ascii=False), json.dumps(result["missing_fields"]),
                     suggested_status(result), now(), int(manual), id))
        if not manual:
            con.execute("INSERT INTO processing_runs(ticket_id,created_at,mode,elapsed_ms,ai_ms,error) VALUES (?,?,?,?,?,?)",
                        (id, now(), result["mode"], result["elapsed_ms"], result["ai_ms"], result["error"]))
        self._event(con, id, "manual_classification" if manual else "analysis", {"topic": result["topic"], "priority": result["priority"], "mode": result["mode"]})

    @staticmethod
    def _require(con, id):
        row = con.execute("SELECT * FROM tickets WHERE id=?", (id,)).fetchone()
        if row is None:
            raise ValueError("Обращение не найдено.")
        return row

    def process(self, id, settings=None, transport=None):
        ticket = self.ticket(id)
        if ticket is None:
            raise ValueError("Обращение не найдено.")
        kwargs = {"transport": transport} if transport else {}
        result = analyze(ticket["message"], self.articles(), settings, **kwargs)
        with self.connection() as con:
            self._apply_result(con, id, result)
        return result

    def correct(self, id, topic, priority):
        if topic not in TOPICS or priority not in PRIORITIES:
            raise ValueError("Неизвестная тема или приоритет.")
        ticket = self.ticket(id)
        if ticket is None:
            raise ValueError("Обращение не найдено.")
        start = time.perf_counter()
        result = demo_analysis(ticket["message"], self.articles(), topic, priority)
        result.update(mode="manual", elapsed_ms=(time.perf_counter() - start) * 1000)
        with self.connection() as con:
            self._apply_result(con, id, result, manual=True)

    def save_draft(self, id, answer, approve=False):
        if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 12000:
            raise ValueError("Ответ должен содержать от 1 до 12000 символов.")
        with self.connection() as con:
            row = self._require(con, id)
            if row["mode"] == "pending":
                raise ValueError("Сначала обработайте обращение.")
            validate_sources(json.loads(row["sources"]), self.articles())
            timestamp = now()
            status = "утверждено" if approve else "черновик"
            con.execute("UPDATE tickets SET draft=?, status=?, approved_answer=?, approved_at=?, updated_at=? WHERE id=?",
                        (answer.strip(), status, answer.strip() if approve else None, timestamp if approve else None, timestamp, id))
            self._event(con, id, "approved" if approve else "draft_saved", {"answer": answer.strip()})

    def change_status(self, id, status):
        if status not in STATUSES or status == "утверждено":
            raise ValueError("Утверждение доступно только через сохранение ответа.")
        with self.connection() as con:
            self._require(con, id)
            con.execute("UPDATE tickets SET status=?, updated_at=?, approved_answer=NULL, approved_at=NULL WHERE id=?", (status, now(), id))
            self._event(con, id, "status", {"status": status})

    def audit(self, id):
        with self.connection() as con:
            return [dict(row) for row in con.execute("SELECT * FROM audit WHERE ticket_id=? ORDER BY id DESC", (id,)).fetchall()]

    def seed_once(self):
        articles = self.articles()
        # Lock prevents concurrent Streamlit sessions from seeding the same dataset.
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT value FROM metadata WHERE key='demo_seed_v1'").fetchone():
                return
            origin = datetime.now(timezone.utc)
            for index, message in enumerate(DEMO_MESSAGES):
                timestamp = (origin - timedelta(days=index % 10, hours=index % 7, minutes=index * 3)).isoformat()
                id = con.execute("INSERT OR IGNORE INTO tickets(seed_key,message,created_at,updated_at,is_demo) VALUES (?,?,?,?,1)",
                                 (f"demo-v1-{index:02d}", message, timestamp, timestamp)).lastrowid
                result = analyze(message, articles, Settings())  # Seed never spends API credits.
                # Avoid another database connection inside the locked seeding transaction.
                keys = ("topic", "priority", "topic_reason", "priority_reason", "action", "draft", "mode", "error", "elapsed_ms", "ai_ms")
                con.execute("UPDATE tickets SET " + ",".join(k + "=?" for k in keys) + ",sources=?,missing_fields=?,status=? WHERE id=?",
                            (*[result[k] for k in keys], json.dumps(result["sources"], ensure_ascii=False), json.dumps(result["missing_fields"]), suggested_status(result), id))
                con.execute("INSERT INTO processing_runs(ticket_id,created_at,mode,elapsed_ms,ai_ms,error) VALUES (?,?,?,?,?,?)",
                            (id, now(), result["mode"], result["elapsed_ms"], None, ""))
                if index in {0, 12, 14, 17, 22, 29, 35, 47}:
                    # These are actual saved demo approvals, not invented dashboard totals.
                    con.execute("UPDATE tickets SET status='утверждено',approved_answer=draft,approved_at=? WHERE id=?", (timestamp, id))
                    self._event(con, id, "demo_approved", {"answer": result["draft"]})
            con.execute("INSERT INTO metadata VALUES ('demo_seed_v1','complete')")

    def metrics(self, demo=None):
        tickets = self.tickets(demo=demo)
        processed = [t for t in tickets if t["mode"] != "pending"]
        approved = sum(t["status"] == "утверждено" and bool(t["approved_answer"]) for t in tickets)
        ids = {t["id"] for t in tickets}
        with self.connection() as con:
            runs = [dict(row) for row in con.execute("SELECT * FROM processing_runs").fetchall() if row["ticket_id"] in ids]
        successful_ai = [r["ai_ms"] for r in runs if r["mode"] == "ai" and r["ai_ms"] is not None]
        failed_ai = [r["ai_ms"] for r in runs if r["mode"] == "fallback" and r["ai_ms"] is not None]
        by_day = {}
        for t in tickets:
            date = datetime.fromisoformat(t["created_at"]).astimezone(LOCAL_TZ).date().isoformat()
            by_day[date] = by_day.get(date, 0) + 1
        if by_day:
            day = datetime.fromisoformat(min(by_day)).date()
            end = datetime.fromisoformat(max(by_day)).date()
            while day <= end:
                by_day.setdefault(day.isoformat(), 0)
                day += timedelta(days=1)
        return dict(total=len(tickets), processed=len(processed), approved=approved,
                    approval_share=approved / len(processed) if processed else 0,
                    no_instruction=[t for t in processed if not t["sources"]],
                    topics={k: sum(t["topic"] == k for t in tickets) for k in TOPICS},
                    priorities={k: sum(t["priority"] == k for t in tickets) for k in PRIORITIES},
                    by_day=dict(sorted(by_day.items())), successful_ai_runs=len(successful_ai),
                    mean_ai_ms=sum(successful_ai) / len(successful_ai) if successful_ai else None,
                    failed_ai_runs=len(failed_ai), mean_failed_ai_ms=sum(failed_ai) / len(failed_ai) if failed_ai else None,
                    processing_runs=len(runs), mean_processing_ms=sum(r["elapsed_ms"] for r in runs) / len(runs) if runs else None)
