from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.formatting import truncate
from app.models import TaskRecord


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
                """
            )

    def create_task(self, agent: str, request: str, created_by: str | None) -> TaskRecord:
        with self.session() as db:
            cursor = db.execute(
                """
                INSERT INTO tasks (agent, request, status, created_by)
                VALUES (?, ?, 'queued', ?)
                """,
                (agent, request, created_by),
            )
            task_id = int(cursor.lastrowid)
            self._add_log(db, task_id, "info", f"Task queued for {agent}.")
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
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown task fields: {', '.join(sorted(unknown))}")

        assignments = ", ".join(f"{field} = ?" for field in fields)
        values = list(fields.values())
        values.append(task_id)

        with self.session() as db:
            db.execute(
                f"""
                UPDATE tasks
                SET {assignments}, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                values,
            )
            self._add_log(db, task_id, "info", f"Task updated: {', '.join(fields)}.")
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

    def _task_from_row(self, row: sqlite3.Row) -> TaskRecord:
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
        )
