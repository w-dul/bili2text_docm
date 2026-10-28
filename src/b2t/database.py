from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime

from b2t.config import Settings
from b2t.models import ProgressSnapshot, TaskRecord, TranscriptVersionRecord


def utc_now() -> str:
    return datetime.now().isoformat()


class AppDatabase:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_directories()
        self.path = settings.app_db_path
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_input TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    workspace_root TEXT NOT NULL,
                    progress_percent REAL NOT NULL DEFAULT 0,
                    current_stage TEXT NOT NULL DEFAULT 'queued',
                    current_message TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    video_id INTEGER,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS task_progress_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    percent REAL NOT NULL DEFAULT 0,
                    indeterminate INTEGER NOT NULL DEFAULT 0,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    slug TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    slug TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_kind TEXT NOT NULL,
                    source_input TEXT NOT NULL,
                    source_url TEXT,
                    source_bv TEXT,
                    title TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    language TEXT,
                    engine TEXT NOT NULL,
                    model TEXT NOT NULL,
                    video_path TEXT,
                    audio_path TEXT NOT NULL,
                    metadata_path TEXT NOT NULL UNIQUE,
                    current_transcript_version_id INTEGER,
                    category_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transcript_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS video_tags (
                    video_id INTEGER NOT NULL,
                    tag_id INTEGER NOT NULL,
                    PRIMARY KEY (video_id, tag_id)
                );

                CREATE TABLE IF NOT EXISTS transcript_segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER NOT NULL,
                    version_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (version_id, seq)
                );

                CREATE TABLE IF NOT EXISTS typing_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER NOT NULL,
                    version_id INTEGER NOT NULL,
                    segment_seq INTEGER NOT NULL,
                    segment_text TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    correct_chars INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    cpm INTEGER NOT NULL,
                    accuracy INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS correction_previews (
                    task_id TEXT PRIMARY KEY,
                    video_id INTEGER NOT NULL,
                    source_version_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def create_task(
        self,
        *,
        kind: str,
        source_input: str,
        provider: str,
        model: str,
        video_id: int | None = None,
    ) -> TaskRecord:
        now = utc_now()
        task = TaskRecord(
            id=uuid.uuid4().hex,
            kind=kind,
            status="queued",
            source_input=source_input,
            provider=provider,
            model=model,
            workspace_root=str(self.settings.workspace_root),
            video_id=video_id,
            created_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, kind, status, source_input, provider, model, workspace_root,
                    progress_percent, current_stage, current_message, error_message, video_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.kind,
                    task.status,
                    task.source_input,
                    task.provider,
                    task.model,
                    task.workspace_root,
                    task.progress_percent,
                    task.current_stage,
                    task.current_message,
                    task.error_message,
                    task.video_id,
                    task.created_at,
                ),
            )
        return task

    def record_progress(self, snapshot: ProgressSnapshot) -> None:
        with self._connect() as conn:
            if snapshot.status == "running":
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, progress_percent = ?, current_stage = ?, current_message = ?,
                        started_at = COALESCE(started_at, ?)
                    WHERE id = ?
                    """,
                    (
                        snapshot.status,
                        snapshot.percent,
                        snapshot.stage,
                        snapshot.message,
                        snapshot.updated_at,
                        snapshot.task_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, progress_percent = ?, current_stage = ?, current_message = ?
                    WHERE id = ?
                    """,
                    (
                        snapshot.status,
                        snapshot.percent,
                        snapshot.stage,
                        snapshot.message,
                        snapshot.task_id,
                    ),
                )
            conn.execute(
                """
                INSERT INTO task_progress_events (
                    task_id, status, stage, message, percent, indeterminate, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.task_id,
                    snapshot.status,
                    snapshot.stage,
                    snapshot.message,
                    snapshot.percent,
                    1 if snapshot.indeterminate else 0,
                    json.dumps(snapshot.detail, ensure_ascii=False),
                    snapshot.updated_at,
                ),
            )

    def complete_task(self, task_id: str, *, video_id: int | None = None, message: str = "") -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'completed', progress_percent = 1.0, current_stage = 'completed',
                    current_message = ?, video_id = ?, finished_at = ?
                WHERE id = ?
                """,
                (message, video_id, now, task_id),
            )

    def fail_task(self, task_id: str, *, error_message: str) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'failed', current_stage = 'failed', error_message = ?, finished_at = ?
                WHERE id = ?
                """,
                (error_message, now, task_id),
            )

    def cancel_task(self, task_id: str) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', current_stage = 'cancelled', finished_at = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (now, task_id),
            )

    def get_active_correction_task(self, video_id: int) -> TaskRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM tasks
                WHERE kind = 'correction' AND video_id = ? AND status IN ('queued', 'running')
                ORDER BY created_at DESC LIMIT 1
                """,
                (video_id,),
            ).fetchone()
        return None if row is None else self._task_from_row(row)

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return None if row is None else self._task_from_row(row)

    def list_tasks(self, *, status: str | None = None, provider: str | None = None) -> list[TaskRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._task_from_row(row) for row in rows]

    def list_task_events(self, task_id: str) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, task_id, status, stage, message, percent, indeterminate, detail_json, created_at
                FROM task_progress_events
                WHERE task_id = ?
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "task_id": row["task_id"],
                "status": row["status"],
                "stage": row["stage"],
                "message": row["message"],
                "percent": float(row["percent"]),
                "indeterminate": bool(row["indeterminate"]),
                "detail": json.loads(row["detail_json"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def create_video(
        self,
        *,
        source_kind: str,
        source_input: str,
        source_url: str | None,
        source_bv: str | None,
        title: str,
        display_name: str,
        language: str | None,
        engine: str,
        model: str,
        video_path: str | None,
        audio_path: str,
        metadata_path: str,
    ) -> int:
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM videos WHERE metadata_path = ?", (metadata_path,)).fetchone()
            if row is not None:
                return int(row["id"])
            cursor = conn.execute(
                """
                INSERT INTO videos (
                    source_kind, source_input, source_url, source_bv, title, display_name,
                    language, engine, model, video_path, audio_path, metadata_path,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_kind,
                    source_input,
                    source_url,
                    source_bv,
                    title,
                    display_name,
                    language,
                    engine,
                    model,
                    video_path,
                    audio_path,
                    metadata_path,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def create_transcript_version(
        self,
        *,
        video_id: int,
        kind: str,
        file_path: str,
        text_sha256: str,
        char_count: int,
        is_active: bool,
    ) -> int:
        now = utc_now()
        with self._connect() as conn:
            if is_active:
                conn.execute("UPDATE transcript_versions SET is_active = 0 WHERE video_id = ?", (video_id,))
            cursor = conn.execute(
                """
                INSERT INTO transcript_versions (
                    video_id, kind, file_path, text_sha256, char_count, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (video_id, kind, file_path, text_sha256, char_count, 1 if is_active else 0, now, now),
            )
            version_id = int(cursor.lastrowid)
            if is_active:
                conn.execute(
                    "UPDATE videos SET current_transcript_version_id = ?, updated_at = ? WHERE id = ?",
                    (version_id, now, video_id),
                )
            return version_id

    def activate_transcript_version(self, video_id: int, version_id: int) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute("UPDATE transcript_versions SET is_active = 0 WHERE video_id = ?", (video_id,))
            conn.execute("UPDATE transcript_versions SET is_active = 1, updated_at = ? WHERE id = ?", (now, version_id))
            conn.execute(
                "UPDATE videos SET current_transcript_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, now, video_id),
            )

    def list_videos(
        self,
        *,
        query: str | None = None,
        category_id: int | None = None,
        tag_id: int | None = None,
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[object] = []
        join_video_tags = tag_id is not None
        if query:
            clauses.append("(LOWER(v.title) LIKE ? OR LOWER(v.display_name) LIKE ? OR LOWER(v.source_input) LIKE ?)")
            needle = f"%{query.strip().lower()}%"
            params.extend([needle, needle, needle])
        if category_id is not None:
            clauses.append("v.category_id = ?")
            params.append(category_id)
        if tag_id is not None:
            clauses.append("vt.tag_id = ?")
            params.append(tag_id)

        sql = """
            SELECT DISTINCT v.*, c.name AS category_name
            FROM videos v
            LEFT JOIN categories c ON c.id = v.category_id
        """
        if join_video_tags:
            sql += " JOIN video_tags vt ON vt.video_id = v.id "
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY v.created_at DESC "
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            tags = self._load_tags_by_video(conn)
        return [self._video_payload(row, tags.get(int(row["id"]), [])) for row in rows]

    def get_video(self, video_id: int) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT v.*, c.name AS category_name
                FROM videos v
                LEFT JOIN categories c ON c.id = v.category_id
                WHERE v.id = ?
                """,
                (video_id,),
            ).fetchone()
            if row is None:
                return None
            tags = self._load_tags_by_video(conn)
        return self._video_payload(row, tags.get(video_id, []))

    def list_transcript_versions(self, video_id: int) -> list[TranscriptVersionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transcript_versions WHERE video_id = ? ORDER BY created_at DESC",
                (video_id,),
            ).fetchall()
        return [self._version_from_row(row) for row in rows]

    def get_transcript_version(self, video_id: int, version_id: int) -> TranscriptVersionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM transcript_versions WHERE video_id = ? AND id = ?",
                (video_id, version_id),
            ).fetchone()
        return None if row is None else self._version_from_row(row)

    def get_active_transcript_version(self, video_id: int) -> TranscriptVersionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM transcript_versions WHERE video_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
                (video_id,),
            ).fetchone()
        return None if row is None else self._version_from_row(row)

    def replace_segments(self, *, video_id: int, version_id: int, texts: list[str]) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute("DELETE FROM transcript_segments WHERE version_id = ?", (version_id,))
            conn.executemany(
                """
                INSERT INTO transcript_segments (video_id, version_id, seq, text, char_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        video_id,
                        version_id,
                        index,
                        text,
                        sum(1 for char in text if not char.isspace()),
                        now,
                    )
                    for index, text in enumerate(texts, start=1)
                ],
            )

    def list_segments(self, version_id: int) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transcript_segments WHERE version_id = ? ORDER BY seq ASC",
                (version_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_segments(self, version_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM transcript_segments WHERE version_id = ?",
                (version_id,),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def get_segment(self, video_id: int, version_id: int, seq: int) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM transcript_segments
                WHERE video_id = ? AND version_id = ? AND seq = ?
                """,
                (video_id, version_id, seq),
            ).fetchone()
        return None if row is None else dict(row)

    def add_typing_record(
        self,
        *,
        video_id: int,
        version_id: int,
        segment_seq: int,
        segment_text: str,
        mode: str,
        duration_seconds: float,
        correct_chars: int,
        error_count: int,
        cpm: int,
        accuracy: int,
    ) -> dict[str, object]:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO typing_records (
                    video_id, version_id, segment_seq, segment_text, mode, duration_seconds,
                    correct_chars, error_count, cpm, accuracy, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id,
                    version_id,
                    segment_seq,
                    segment_text,
                    mode,
                    duration_seconds,
                    correct_chars,
                    error_count,
                    cpm,
                    accuracy,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM typing_records WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_typing_records(self, video_id: int, *, limit: int = 5) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM typing_records
                WHERE video_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (video_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_completed_segment_seqs(self, video_id: int, version_id: int) -> set[int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT segment_seq FROM typing_records
                WHERE video_id = ? AND version_id = ?
                """,
                (video_id, version_id),
            ).fetchall()
        return {int(row["segment_seq"]) for row in rows}

    def save_correction_preview(
        self,
        *,
        task_id: str,
        video_id: int,
        source_version_id: int,
        text: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO correction_previews (
                    task_id, video_id, source_version_id, text, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, video_id, source_version_id, text, utc_now()),
            )

    def get_correction_preview(self, task_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM correction_previews WHERE task_id = ?", (task_id,)
            ).fetchone()
        return None if row is None else dict(row)

    def delete_correction_preview(self, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM correction_previews WHERE task_id = ?", (task_id,))

    def create_category(self, name: str) -> dict[str, object]:
        slug = slugify(name)
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO categories (name, slug, created_at) VALUES (?, ?, ?)",
                (name, slug, now),
            )
            row = conn.execute("SELECT * FROM categories WHERE slug = ?", (slug,)).fetchone()
        assert row is not None
        return dict(row)

    def update_category(self, category_id: int, name: str) -> dict[str, object] | None:
        slug = slugify(name)
        with self._connect() as conn:
            conn.execute("UPDATE categories SET name = ?, slug = ? WHERE id = ?", (name, slug, category_id))
            row = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        return None if row is None else dict(row)

    def delete_category(self, category_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE videos SET category_id = NULL WHERE category_id = ?", (category_id,))
            conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))

    def list_categories(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM categories ORDER BY name ASC").fetchall()
        return [dict(row) for row in rows]

    def assign_category(self, video_id: int, category_id: int | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE videos SET category_id = ?, updated_at = ? WHERE id = ?",
                (category_id, utc_now(), video_id),
            )

    def create_tag(self, name: str) -> dict[str, object]:
        slug = slugify(name)
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tags (name, slug, created_at) VALUES (?, ?, ?)",
                (name, slug, now),
            )
            row = conn.execute("SELECT * FROM tags WHERE slug = ?", (slug,)).fetchone()
        assert row is not None
        return dict(row)

    def update_tag(self, tag_id: int, name: str) -> dict[str, object] | None:
        slug = slugify(name)
        with self._connect() as conn:
            conn.execute("UPDATE tags SET name = ?, slug = ? WHERE id = ?", (name, slug, tag_id))
            row = conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
        return None if row is None else dict(row)

    def delete_tag(self, tag_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM video_tags WHERE tag_id = ?", (tag_id,))
            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))

    def list_tags(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tags ORDER BY name ASC").fetchall()
        return [dict(row) for row in rows]

    def add_video_tag(self, video_id: int, tag_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO video_tags (video_id, tag_id) VALUES (?, ?)",
                (video_id, tag_id),
            )

    def remove_video_tag(self, video_id: int, tag_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM video_tags WHERE video_id = ? AND tag_id = ?", (video_id, tag_id))

    def _load_tags_by_video(self, conn: sqlite3.Connection) -> dict[int, list[dict[str, object]]]:
        rows = conn.execute(
            """
            SELECT vt.video_id, t.id, t.name, t.slug
            FROM video_tags vt
            JOIN tags t ON t.id = vt.tag_id
            ORDER BY t.name ASC
            """
        ).fetchall()
        tags: dict[int, list[dict[str, object]]] = {}
        for row in rows:
            tags.setdefault(int(row["video_id"]), []).append(
                {"id": int(row["id"]), "name": row["name"], "slug": row["slug"]}
            )
        return tags

    def _video_payload(self, row: sqlite3.Row, tags: list[dict[str, object]]) -> dict[str, object]:
        return {
            **dict(row),
            "tags": tags,
        }

    def _task_from_row(self, row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            source_input=row["source_input"],
            provider=row["provider"],
            model=row["model"],
            workspace_root=row["workspace_root"],
            progress_percent=float(row["progress_percent"]),
            current_stage=row["current_stage"],
            current_message=row["current_message"],
            error_message=row["error_message"],
            video_id=row["video_id"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def _version_from_row(self, row: sqlite3.Row) -> TranscriptVersionRecord:
        return TranscriptVersionRecord(
            id=int(row["id"]),
            video_id=int(row["video_id"]),
            kind=row["kind"],
            file_path=row["file_path"],
            text_sha256=row["text_sha256"],
            char_count=int(row["char_count"]),
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def slugify(value: str) -> str:
    return "-".join(value.strip().lower().split())
