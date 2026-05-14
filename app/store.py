from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.formatting import truncate
from app.models import TaskRecord
from app.task_lifecycle import should_stamp_completed_at, TaskStatus


def make_terminal_fingerprint(argv: tuple[str, ...], cwd: str) -> str:
    body = json.dumps({"argv": list(argv), "cwd": cwd}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def make_git_push_fingerprint(*, remote: str, branch: str, repo_path: str, agent: str) -> str:
    body = json.dumps(
        {"agent": agent, "branch": branch, "remote": remote, "repo": repo_path},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self.session() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent TEXT NOT NULL,
                    request TEXT NOT NULL,
                    status TEXT NOT NULL,
                    branch TEXT,
                    commit_hash TEXT,
                    summary TEXT,
                    verification TEXT,
                    created_by TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    pushed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS task_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );

                CREATE TABLE IF NOT EXISTS handoffs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_task_id INTEGER NOT NULL,
                    from_agent TEXT NOT NULL,
                    to_agent TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_task_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(source_task_id) REFERENCES tasks(id),
                    FOREIGN KEY(created_task_id) REFERENCES tasks(id)
                );

                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER,
                    agent TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );

                CREATE TABLE IF NOT EXISTS terminal_command_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    argv TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    exit_code INTEGER,
                    approved INTEGER NOT NULL DEFAULT 0,
                    timed_out INTEGER NOT NULL DEFAULT 0,
                    stdout_preview TEXT,
                    stderr_preview TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS terminal_pending_approval (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent TEXT NOT NULL,
                    argv TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS git_push_pending_approval (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent TEXT NOT NULL,
                    remote TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            self._ensure_task_columns(db)
            self._migrate_task_status_values(db)

    def _table_columns(self, db: sqlite3.Connection, table: str) -> set[str]:
        rows = db.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r[1]) for r in rows}

    def _ensure_task_columns(self, db: sqlite3.Connection) -> None:
        cols = self._table_columns(db, "tasks")
        alters = []
        if "files_changed" not in cols:
            alters.append("ALTER TABLE tasks ADD COLUMN files_changed TEXT")
        if "completed_at" not in cols:
            alters.append("ALTER TABLE tasks ADD COLUMN completed_at TEXT")
        if "project_key" not in cols:
            alters.append("ALTER TABLE tasks ADD COLUMN project_key TEXT")
        if "rollback_ref" not in cols:
            alters.append("ALTER TABLE tasks ADD COLUMN rollback_ref TEXT")
        for stmt in alters:
            db.execute(stmt)

    def _migrate_task_status_values(self, db: sqlite3.Connection) -> None:
        mapping = [
            ("queued", TaskStatus.TODO),
            ("running", TaskStatus.IN_PROGRESS),
            ("committed", TaskStatus.REVIEW),
            ("done", TaskStatus.DONE),
            ("pushed", TaskStatus.DONE),
            ("blocked", TaskStatus.FAILED),
            ("failed", TaskStatus.FAILED),
        ]
        for old, new in mapping:
            db.execute("UPDATE tasks SET status = ? WHERE status = ?", (new, old))

    def current_timestamp(self) -> str:
        with self.session() as db:
            return str(db.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0])

    def create_task(
        self,
        agent: str,
        request: str,
        created_by: str | None,
        *,
        project_key: str | None = None,
        status: str | None = None,
    ) -> TaskRecord:
        proj = project_key or agent
        st = status or TaskStatus.TODO
        with self.session() as db:
            cursor = db.execute(
                """
                INSERT INTO tasks (agent, request, status, created_by, project_key)
                VALUES (?, ?, ?, ?, ?)
                """,
                (agent, request, st, created_by, proj),
            )
            task_id = int(cursor.lastrowid)
            self._add_log(db, task_id, "info", f"Task created ({st}) for {agent}.")
        return self.get_task(task_id)

    def get_task(self, task_id: int) -> TaskRecord:
        with self.session() as db:
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Task {task_id} was not found.")
            return self._task_from_row(row)

    def list_tasks(self, limit: int = 10) -> list[TaskRecord]:
        with self.session() as db:
            rows = db.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._task_from_row(row) for row in rows]

    def list_tasks_for_agent(self, agent: str, limit: int = 10) -> list[TaskRecord]:
        with self.session() as db:
            rows = db.execute(
                "SELECT * FROM tasks WHERE agent = ? ORDER BY id DESC LIMIT ?",
                (agent, limit),
            ).fetchall()
            return [self._task_from_row(row) for row in rows]

    def recent_handoff_lines_for_agent(self, agent: str, limit: int = 5) -> list[str]:
        with self.session() as db:
            rows = db.execute(
                """
                SELECT source_task_id, from_agent, to_agent, message, created_at
                FROM handoffs
                WHERE from_agent = ? OR to_agent = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (agent, agent, limit),
            ).fetchall()
        lines: list[str] = []
        for row in rows:
            msg = truncate(str(row["message"]), 240)
            lines.append(
                f"  - task #{row['source_task_id']}: {row['from_agent']} → {row['to_agent']} ({row['created_at']}): {msg}",
            )
        return list(reversed(lines))

    def update_task(self, task_id: int, **fields: object) -> TaskRecord:
        if not fields:
            return self.get_task(task_id)

        allowed = {
            "status",
            "branch",
            "commit_hash",
            "summary",
            "verification",
            "pushed_at",
            "files_changed",
            "completed_at",
            "project_key",
            "rollback_ref",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown task fields: {', '.join(sorted(unknown))}")

        data = dict(fields)
        with self.session() as db:
            if (
                "status" in data
                and should_stamp_completed_at(str(data["status"]))
                and "completed_at" not in data
            ):
                data["completed_at"] = str(db.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0])
            assignments = ", ".join(f"{field} = ?" for field in data)
            values = list(data.values())
            values.append(task_id)
            db.execute(
                f"""
                UPDATE tasks
                SET {assignments}, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                values,
            )
            self._add_log(db, task_id, "info", f"Task updated: {', '.join(data)}.")
        return self.get_task(task_id)

    def add_log(self, task_id: int, level: str, message: str) -> None:
        with self.session() as db:
            self._add_log(db, task_id, level, message)

    def add_decision(self, task_id: int | None, agent: str, message: str) -> None:
        with self.session() as db:
            db.execute(
                "INSERT INTO decisions (task_id, agent, message) VALUES (?, ?, ?)",
                (task_id, agent, message),
            )

    def add_handoff(
        self,
        source_task_id: int,
        from_agent: str,
        to_agent: str,
        message: str,
        created_task_id: int | None,
    ) -> None:
        with self.session() as db:
            db.execute(
                """
                INSERT INTO handoffs
                    (source_task_id, from_agent, to_agent, message, created_task_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_task_id, from_agent, to_agent, message, created_task_id),
            )

    def recent_logs(self, task_id: int, limit: int = 5) -> list[str]:
        with self.session() as db:
            rows = db.execute(
                """
                SELECT level, message, created_at
                FROM task_logs
                WHERE task_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
            return [
                f"{row['created_at']} {row['level'].upper()}: {row['message']}"
                for row in reversed(rows)
            ]

    def _add_log(
        self,
        db: sqlite3.Connection,
        task_id: int,
        level: str,
        message: str,
    ) -> None:
        db.execute(
            "INSERT INTO task_logs (task_id, level, message) VALUES (?, ?, ?)",
            (task_id, level, message),
        )

    def log_terminal_execution(
        self,
        *,
        agent: str,
        cwd: str,
        argv: tuple[str, ...],
        tool: str,
        exit_code: int | None,
        approved: bool,
        timed_out: bool,
        stdout: str,
        stderr: str,
    ) -> None:
        preview = 4000
        with self.session() as db:
            db.execute(
                """
                INSERT INTO terminal_command_log
                    (agent, cwd, argv, tool, exit_code, approved, timed_out, stdout_preview, stderr_preview)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent,
                    cwd,
                    json.dumps(list(argv)),
                    tool,
                    exit_code,
                    1 if approved else 0,
                    1 if timed_out else 0,
                    stdout[:preview],
                    stderr[:preview],
                ),
            )

    def insert_terminal_pending(self, agent: str, argv: tuple[str, ...], cwd: str) -> int:
        fp = make_terminal_fingerprint(argv, cwd)
        with self.session() as db:
            cur = db.execute(
                """
                INSERT INTO terminal_pending_approval (agent, argv, cwd, fingerprint, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (agent, json.dumps(list(argv)), cwd, fp),
            )
            return int(cur.lastrowid)

    def approve_terminal_pending(self, pending_id: int) -> bool:
        with self.session() as db:
            cur = db.execute(
                """
                UPDATE terminal_pending_approval
                SET status = 'approved'
                WHERE id = ? AND status = 'pending'
                """,
                (pending_id,),
            )
            return cur.rowcount == 1

    def terminal_pending_status(self, pending_id: int) -> str | None:
        with self.session() as db:
            row = db.execute(
                "SELECT status FROM terminal_pending_approval WHERE id = ?",
                (pending_id,),
            ).fetchone()
            return str(row["status"]) if row else None

    def verify_and_delete_terminal_approval(
        self,
        pending_id: int,
        *,
        agent: str,
        fingerprint: str,
    ) -> bool:
        """Ensure row is approved, matches agent and fingerprint, then delete (one-shot)."""
        with self.session() as db:
            row = db.execute(
                "SELECT * FROM terminal_pending_approval WHERE id = ?",
                (pending_id,),
            ).fetchone()
            if row is None:
                return False
            if str(row["status"]) != "approved":
                return False
            if str(row["agent"]) != agent:
                return False
            if str(row["fingerprint"]) != fingerprint:
                return False
            db.execute("DELETE FROM terminal_pending_approval WHERE id = ?", (pending_id,))
            return True

    def insert_git_push_pending(
        self,
        *,
        agent: str,
        remote: str,
        branch: str,
        repo_path: str,
    ) -> int:
        fp = make_git_push_fingerprint(remote=remote, branch=branch, repo_path=repo_path, agent=agent)
        with self.session() as db:
            cur = db.execute(
                """
                INSERT INTO git_push_pending_approval
                    (agent, remote, branch, repo_path, fingerprint, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (agent, remote, branch, repo_path, fp),
            )
            return int(cur.lastrowid)

    def approve_git_push_pending(self, pending_id: int) -> bool:
        with self.session() as db:
            cur = db.execute(
                """
                UPDATE git_push_pending_approval
                SET status = 'approved'
                WHERE id = ? AND status = 'pending'
                """,
                (pending_id,),
            )
            return cur.rowcount == 1

    def verify_and_delete_git_push_approval(
        self,
        pending_id: int,
        *,
        agent: str,
        fingerprint: str,
    ) -> bool:
        with self.session() as db:
            row = db.execute(
                "SELECT * FROM git_push_pending_approval WHERE id = ?",
                (pending_id,),
            ).fetchone()
            if row is None:
                return False
            if str(row["status"]) != "approved":
                return False
            if str(row["agent"]) != agent:
                return False
            if str(row["fingerprint"]) != fingerprint:
                return False
            db.execute("DELETE FROM git_push_pending_approval WHERE id = ?", (pending_id,))
            return True

    def _task_from_row(self, row: sqlite3.Row) -> TaskRecord:
        def col(key: str) -> str | None:
            try:
                v = row[key]
            except (KeyError, IndexError):
                return None
            return str(v) if v is not None else None

        return TaskRecord(
            id=int(row["id"]),
            agent=str(row["agent"]),
            request=str(row["request"]),
            status=str(row["status"]),
            branch=row["branch"],
            commit_hash=row["commit_hash"],
            summary=row["summary"],
            verification=row["verification"],
            created_by=row["created_by"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            pushed_at=row["pushed_at"],
            files_changed=col("files_changed"),
            completed_at=col("completed_at"),
            project_key=col("project_key"),
            rollback_ref=col("rollback_ref"),
        )
