"""SQLite persistence. Customer intake commits before optional API processing."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
import sqlite3
import time
from dotenv import dotenv_values

from .data import ARTICLES, DEMO_MESSAGES, FIELDS, PRIORITIES, STATUSES, TOPICS
from .engine import (ROOT, AIError, Settings, analyze, assistant_reply, classify_rules, extract_known,
                     detect_language, probe_connection, suggested_status, validate_sources)

LOCAL_TZ = timezone(timedelta(hours=5))


def now():
    return datetime.now(timezone.utc).isoformat()


def db_path():
    path = Path(os.getenv("SUPPORTLENS_DB") or dotenv_values(ROOT / ".env").get("SUPPORTLENS_DB") or "data/supportlens.sqlite3")
    return path if path.is_absolute() else ROOT / path


class ConflictError(ValueError):
    """The persisted record is newer than the operator/model's snapshot."""


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
            additions = {
                "tickets": {"revision": "INTEGER NOT NULL DEFAULT 0", "draft_revision": "INTEGER NOT NULL DEFAULT 0",
                            "language": "TEXT NOT NULL DEFAULT 'ru'", "known_fields": "TEXT NOT NULL DEFAULT '{}'",
                            "operator_notes": "TEXT NOT NULL DEFAULT ''", "error_category": "TEXT NOT NULL DEFAULT ''",
                            "source_snapshots": "TEXT NOT NULL DEFAULT '[]'", "intake_token": "TEXT"},
                "articles": {"version": "INTEGER NOT NULL DEFAULT 1"},
            }
            for table, columns in additions.items():
                present = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
                for name, definition in columns.items():
                    if name not in present:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            con.executescript("""
                CREATE UNIQUE INDEX IF NOT EXISTS intake_once ON tickets(intake_token) WHERE intake_token IS NOT NULL;
                CREATE TABLE IF NOT EXISTS answer_versions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL REFERENCES tickets(id), created_at TEXT NOT NULL,
                    revision INTEGER NOT NULL, reason TEXT NOT NULL, answer TEXT NOT NULL,
                    sources TEXT NOT NULL, snapshots TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS article_versions (article_id TEXT NOT NULL, version INTEGER NOT NULL,
                    created_at TEXT NOT NULL, snapshot TEXT NOT NULL, PRIMARY KEY(article_id,version));
                CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL REFERENCES tickets(id), created_at TEXT NOT NULL,
                    role TEXT NOT NULL, text TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                    base_revision INTEGER NOT NULL);
            """)
            for a in ARTICLES:
                con.execute("INSERT OR IGNORE INTO articles(id,title,topic,summary,body,keywords,fields,action) VALUES (?,?,?,?,?,?,?,?)", (
                    a["id"], a["title"], a["topic"], a["summary"], a["body"],
                    json.dumps(a["keywords"], ensure_ascii=False), json.dumps(a["fields"]), a["action"]))
            if not con.execute("SELECT 1 FROM metadata WHERE key='migration_v2'").fetchone():
                catalog = self._articles(con)
                for a in catalog:
                    con.execute("INSERT OR IGNORE INTO article_versions VALUES (?,?,?,?)", (a["id"], a["version"], now(), json.dumps(a, ensure_ascii=False)))
                for row in con.execute("SELECT * FROM tickets").fetchall():
                    snapshots = self._snapshots(json.loads(row["sources"]), catalog)
                    con.execute("UPDATE tickets SET source_snapshots=?,known_fields=?,language=? WHERE id=?",
                                (json.dumps(snapshots, ensure_ascii=False), json.dumps(extract_known(row["message"]), ensure_ascii=False), detect_language(row["message"]), row["id"]))
                    if row["draft"]:
                        con.execute("INSERT INTO answer_versions(ticket_id,created_at,revision,reason,answer,sources,snapshots,status) VALUES (?,?,?,?,?,?,?,?)",
                                    (row["id"], now(), 0, "migration", row["draft"], row["sources"], json.dumps(snapshots, ensure_ascii=False), row["status"]))
                con.execute("INSERT INTO metadata VALUES ('migration_v2','complete')")
        if seed:
            self.seed_once()

    def articles(self):
        with self.connection() as con:
            return self._articles(con)

    @staticmethod
    def _articles(con):
        rows = con.execute("SELECT * FROM articles ORDER BY id").fetchall()
        return [{**dict(row), "keywords": json.loads(row["keywords"]), "fields": json.loads(row["fields"])} for row in rows]

    @staticmethod
    def decode(row):
        if row is None:
            return None
        return {**dict(row), **{key: json.loads(row[key]) for key in ("sources", "missing_fields", "known_fields", "source_snapshots")}}

    def tickets(self, topic=None, priority=None, status=None, demo=None, start=None, end=None, sort="risk"):
        clauses, args = [], []
        for name, value in [("topic", topic), ("priority", priority), ("status", status), ("is_demo", demo)]:
            if value is not None:
                clauses.append(name + " = ?")
                args.append(value)
        query = "SELECT * FROM tickets" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        with self.connection() as con:
            tickets = [self.decode(row) for row in con.execute(query, args).fetchall()]
        tickets = [t for t in tickets if (start is None or self._date(t["created_at"]) >= start) and (end is None or self._date(t["created_at"]) <= end)]
        if sort == "old":
            return sorted(tickets, key=lambda t: t["created_at"])
        if sort == "new":
            return sorted(tickets, key=lambda t: t["created_at"], reverse=True)
        return sorted(tickets, key=lambda t: (t["status"] == "утверждено", PRIORITIES.index(t["priority"]), t["created_at"]))

    @staticmethod
    def _date(value):
        return datetime.fromisoformat(value).astimezone(LOCAL_TZ).date()

    def ticket(self, id):
        with self.connection() as con:
            return self.decode(con.execute("SELECT * FROM tickets WHERE id = ?", (id,)).fetchone())

    def create_ticket(self, message, intake_token=None):
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 6000:
            raise ValueError("Введите обращение от 1 до 6000 символов.")
        timestamp = now()
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            if intake_token:
                existing = con.execute("SELECT id FROM tickets WHERE intake_token=?", (intake_token,)).fetchone()
                if existing:
                    return existing["id"]
            return con.execute("INSERT INTO tickets(message,created_at,updated_at,intake_token) VALUES (?,?,?,?)",
                               (message.strip(), timestamp, timestamp, intake_token)).lastrowid

    def _event(self, con, id, event, payload):
        con.execute("INSERT INTO audit(ticket_id,created_at,event,payload) VALUES (?,?,?,?)",
                    (id, now(), event, json.dumps(payload, ensure_ascii=False)))

    @staticmethod
    def _snapshots(sources, articles):
        ids = {s["id"] for s in sources}
        return [{k: a.get(k, 1 if k == "version" else "") for k in ("id", "title", "body", "version")} for a in articles if a["id"] in ids]

    @staticmethod
    def _check_revision(row, expected):
        if expected is not None and row["revision"] != expected:
            raise ConflictError("Обращение изменено в другом сеансе. Ваш текст сохранён в редакторе. Обновите карточку и сравните версии перед сохранением.")

    def _version(self, con, id, reason):
        row = self._require(con, id)
        if row["draft"]:
            con.execute("INSERT INTO answer_versions(ticket_id,created_at,revision,reason,answer,sources,snapshots,status) VALUES (?,?,?,?,?,?,?,?)",
                        (id, now(), row["revision"], reason, row["draft"], row["sources"], row["source_snapshots"], row["status"]))

    @staticmethod
    def _require(con, id):
        row = con.execute("SELECT * FROM tickets WHERE id=?", (id,)).fetchone()
        if row is None:
            raise ValueError("Обращение не найдено.")
        return row

    def process(self, id, settings=None, transport=None, expected_revision=None, language=None, progress=None):
        ticket = self.ticket(id)
        if ticket is None:
            raise ValueError("Обращение не найдено.")
        self._check_revision(ticket, expected_revision)
        settings, articles = settings or Settings.from_env(), self.articles()
        kwargs = {"transport": transport} if transport else {}
        overrides = {k: ticket[k] for k in ("topic", "priority", "topic_reason", "priority_reason")} if ticket["manual_override"] else None
        result = analyze(ticket["message"], articles, settings, language_override=language, progress=progress, overrides=overrides, **kwargs)
        validate_sources(result["sources"], articles)
        conflict = False
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO processing_runs(ticket_id,created_at,mode,elapsed_ms,ai_ms,error) VALUES (?,?,?,?,?,?)",
                        (id, now(), result["mode"], result["elapsed_ms"], result["ai_ms"], result["error"]))
            if settings.enabled:
                self._connection_state(con, settings, {"state": "ready" if result["mode"] == "ai" else "error", "category": result["error_category"], "message": "Модель успешно ответила." if result["mode"] == "ai" else result["error"]})
            current = self._require(con, id)
            if current["revision"] != ticket["revision"]:
                conflict = True
                self._event(con, id, "analysis_conflict", {"expected_revision": ticket["revision"], "current_revision": current["revision"]})
            else:
                self._version(con, id, "before_generation")
                keys = ("topic", "priority", "topic_reason", "priority_reason", "action", "draft", "mode", "error", "elapsed_ms", "ai_ms", "language", "operator_notes", "error_category")
                con.execute("UPDATE tickets SET " + ",".join(k + "=?" for k in keys) +
                            ",sources=?,source_snapshots=?,missing_fields=?,known_fields=?,status=?,updated_at=?,approved_answer=NULL,approved_at=NULL,revision=revision+1,draft_revision=draft_revision+1 WHERE id=? AND revision=?",
                            (*[result[k] for k in keys], json.dumps(result["sources"], ensure_ascii=False), json.dumps(self._snapshots(result["sources"], articles), ensure_ascii=False),
                             json.dumps(result["missing_fields"]), json.dumps(result["known_fields"], ensure_ascii=False), suggested_status(result), now(), id, ticket["revision"]))
                self._version(con, id, "generated")
                self._event(con, id, "analysis", {"topic": result["topic"], "priority": result["priority"], "mode": result["mode"]})
        if conflict:
            raise ConflictError("Пока шёл анализ, обращение изменилось. Устаревший результат не применён; утверждение и ручные правки сохранены.")
        return result

    def correct(self, id, topic, priority, expected_revision=None):
        if topic not in TOPICS or priority not in PRIORITIES:
            raise ValueError("Неизвестная тема или приоритет.")
        ticket = self.ticket(id)
        if ticket is None:
            raise ValueError("Обращение не найдено.")
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._require(con, id)
            self._check_revision(row, expected_revision)
            self._version(con, id, "classification_changed")
            con.execute("UPDATE tickets SET topic=?,priority=?,topic_reason='Тема исправлена оператором.',priority_reason='Приоритет исправлен оператором.',manual_override=1,revision=revision+1,updated_at=? WHERE id=?", (topic, priority, now(), id))
            self._event(con, id, "manual_classification", {"topic": topic, "priority": priority, "answer_preserved": True})

    def save_draft(self, id, answer, approve=False, expected_revision=None, sources=None, snapshots=None, language=None):
        if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 12000:
            raise ValueError("Ответ должен содержать от 1 до 12000 символов.")
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._require(con, id)
            self._check_revision(row, expected_revision)
            if row["mode"] == "pending":
                raise ValueError("Сначала обработайте обращение.")
            chosen_sources = sources if sources is not None else json.loads(row["sources"])
            chosen_snapshots = snapshots if snapshots is not None else json.loads(row["source_snapshots"])
            validate_sources(chosen_sources, chosen_snapshots)
            self._version(con, id, "before_edit")
            timestamp = now()
            status = "утверждено" if approve else "черновик"
            con.execute("UPDATE tickets SET draft=?,status=?,approved_answer=?,approved_at=?,updated_at=?,sources=?,source_snapshots=?,language=?,revision=revision+1,draft_revision=draft_revision+1 WHERE id=?",
                        (answer.strip(), status, answer.strip() if approve else None, timestamp if approve else None, timestamp,
                         json.dumps(chosen_sources, ensure_ascii=False), json.dumps(chosen_snapshots, ensure_ascii=False), language or row["language"], id))
            self._version(con, id, "approved" if approve else "edited")
            self._event(con, id, "approved" if approve else "draft_saved", {"answer": answer.strip()})

    def change_status(self, id, status, expected_revision=None):
        if status not in STATUSES or status == "утверждено":
            raise ValueError("Утверждение доступно только через сохранение ответа.")
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._require(con, id)
            self._check_revision(row, expected_revision)
            self._version(con, id, "status_changed")
            con.execute("UPDATE tickets SET status=?,updated_at=?,approved_answer=NULL,approved_at=NULL,revision=revision+1 WHERE id=?", (status, now(), id))
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
                con.execute("UPDATE tickets SET " + ",".join(k + "=?" for k in keys) + ",sources=?,source_snapshots=?,missing_fields=?,known_fields=?,language=?,operator_notes=?,status=? WHERE id=?",
                            (*[result[k] for k in keys], json.dumps(result["sources"], ensure_ascii=False), json.dumps(self._snapshots(result["sources"], articles), ensure_ascii=False),
                             json.dumps(result["missing_fields"]), json.dumps(result["known_fields"], ensure_ascii=False), result["language"], result["operator_notes"], suggested_status(result), id))
                con.execute("INSERT INTO processing_runs(ticket_id,created_at,mode,elapsed_ms,ai_ms,error) VALUES (?,?,?,?,?,?)",
                            (id, now(), result["mode"], result["elapsed_ms"], None, ""))
                if index in {0, 12, 14, 17, 22, 29, 35, 47}:
                    # These are actual saved demo approvals, not invented dashboard totals.
                    con.execute("UPDATE tickets SET status='утверждено',approved_answer=draft,approved_at=? WHERE id=?", (timestamp, id))
                    self._event(con, id, "demo_approved", {"answer": result["draft"]})
                self._version(con, id, "initial_demo")
            con.execute("INSERT INTO metadata VALUES ('demo_seed_v1','complete')")

    def metrics(self, demo=None, start=None, end=None):
        tickets = self.tickets(demo=demo, start=start, end=end)
        processed = [t for t in tickets if t["mode"] != "pending"]
        approved = sum(t["status"] == "утверждено" and bool(t["approved_answer"]) for t in tickets)
        ids = {t["id"] for t in tickets}
        with self.connection() as con:
            runs = [dict(row) for row in con.execute("SELECT * FROM processing_runs").fetchall() if row["ticket_id"] in ids and
                    (start is None or self._date(row["created_at"]) >= start) and (end is None or self._date(row["created_at"]) <= end)]
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
                    local_runs=sum(r["mode"] == "demo" for r in runs), tickets=tickets,
                    processing_runs=len(runs), mean_processing_ms=sum(r["elapsed_ms"] for r in runs) / len(runs) if runs else None)

    def versions(self, id):
        with self.connection() as con:
            return [dict(r) for r in con.execute("SELECT * FROM answer_versions WHERE ticket_id=? ORDER BY id DESC", (id,))]

    def ai_state(self, settings):
        if not settings.enabled:
            return dict(state="not_configured", message="AI не настроен", category="config")
        with self.connection() as con:
            row = con.execute("SELECT value FROM metadata WHERE key=?", ("ai:" + settings.fingerprint,)).fetchone()
        return json.loads(row["value"]) if row else dict(state="unverified", category="", message="Настройки заполнены · соединение не проверено")

    @staticmethod
    def _connection_state(con, settings, result):
        con.execute("INSERT INTO metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("ai:" + settings.fingerprint, json.dumps({**result, "checked_at": now()}, ensure_ascii=False)))

    def check_ai(self, settings, transport=None):
        result = probe_connection(settings, **({"transport": transport} if transport else {}))
        if settings.enabled:
            with self.connection() as con:
                self._connection_state(con, settings, result)
        return result

    def conversation(self, id):
        with self.connection() as con:
            return [{**dict(r), "result": json.loads(r["result"])} for r in con.execute("SELECT * FROM conversations WHERE ticket_id=? ORDER BY id", (id,))]

    def ask_assistant(self, id, question, settings, expected_revision=None, transport=None):
        ticket = self.ticket(id)
        if ticket is None:
            raise ValueError("Обращение не найдено.")
        self._check_revision(ticket, expected_revision)
        articles, history = self.articles(), self.conversation(id)
        try:
            result = assistant_reply(ticket, articles, history, question, settings, **({"transport": transport} if transport else {}))
        except AIError as exc:
            with self.connection() as con:
                self._connection_state(con, settings, dict(state="error", category=exc.category, message=str(exc)))
            raise
        result["snapshots"] = self._snapshots(result["suggestion_sources"], articles)
        result["citation_snapshots"] = self._snapshots(result["sources"], articles)
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            self._check_revision(self._require(con, id), ticket["revision"])
            for role, text, payload in [("user", question, {}), ("assistant", result["text"], result)]:
                con.execute("INSERT INTO conversations(ticket_id,created_at,role,text,result,base_revision) VALUES (?,?,?,?,?,?)",
                            (id, now(), role, text, json.dumps(payload, ensure_ascii=False), ticket["revision"]))
            self._connection_state(con, settings, dict(state="ready", category="", message="Модель успешно ответила."))
        return result

    def apply_suggestion(self, id, conversation_id, expected_revision):
        with self.connection() as con:
            row = con.execute("SELECT * FROM conversations WHERE id=? AND ticket_id=? AND role='assistant'", (conversation_id, id)).fetchone()
        if not row or row["base_revision"] != expected_revision:
            raise ConflictError("Предложение относится к другой версии обращения. Задайте вопрос помощнику заново.")
        result = json.loads(row["result"])
        if not result.get("suggestion"):
            raise ValueError("В этом ответе нет предложения для черновика.")
        self.save_draft(id, result["suggestion"], expected_revision=expected_revision, sources=result["suggestion_sources"], snapshots=result["snapshots"], language=result["language"])

    def save_article(self, id, title, topic, body, keywords, fields, action, expected_version=None):
        if not re.fullmatch(r"KB-[A-Z0-9-]{3,30}", id) or topic not in TOPICS or action not in ("Проверить данные", "Нужно уточнение", "Передать специалисту"):
            raise ValueError("Проверьте идентификатор KB-…, тему и действие.")
        if not title.strip() or not 20 <= len(body.strip()) <= 20000 or not keywords or any(f not in FIELDS for f in fields):
            raise ValueError("Нужны название, текст от 20 до 20000 символов и ключевые слова.")
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute("SELECT * FROM articles WHERE id=?", (id,)).fetchone()
            if previous and previous["version"] != expected_version:
                raise ConflictError("Статья изменена в другом сеансе. Обновите редактор.")
            if not previous and expected_version is not None:
                raise ConflictError("Статья не найдена.")
            version = previous["version"] + 1 if previous else 1
            con.execute("INSERT INTO articles(id,title,topic,summary,body,keywords,fields,action,version) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,topic=excluded.topic,summary=excluded.summary,body=excluded.body,keywords=excluded.keywords,fields=excluded.fields,action=excluded.action,version=excluded.version",
                        (id, title.strip(), topic, body.strip()[:200], body.strip(), json.dumps(keywords, ensure_ascii=False), json.dumps(fields), action, version))
            saved = next(a for a in self._articles(con) if a["id"] == id)
            con.execute("INSERT INTO article_versions VALUES (?,?,?,?)", (id, version, now(), json.dumps(saved, ensure_ascii=False)))
        return version
