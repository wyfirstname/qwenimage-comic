# -*- coding: utf-8 -*-
"""SQLite 建表与连接管理。

使用标准库 sqlite3，避免引入 ORM 依赖；所有写操作走同一个连接工厂，
读操作使用独立连接，兼容 FastAPI 的多线程/异步执行环境。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from shared.config import settings

CORE_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    status       TEXT NOT NULL,           -- queued|running|succeeded|failed|canceled
    mode         TEXT NOT NULL DEFAULT 'txt2img',
    prompt       TEXT NOT NULL,
    negative     TEXT,
    params_json  TEXT NOT NULL,           -- 完整参数快照（JSON）
    seed         INTEGER,
    count        INTEGER DEFAULT 1,
    progress     INTEGER DEFAULT 0,       -- 0-100
    message      TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);

CREATE TABLE IF NOT EXISTS images (
    id          TEXT PRIMARY KEY,
    job_id      TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    path        TEXT NOT NULL,
    url         TEXT NOT NULL,
    width       INTEGER,
    height      INTEGER,
    steps       INTEGER,
    guidance    REAL,
    seed        INTEGER,
    prompt      TEXT,
    negative    TEXT,
    mode        TEXT DEFAULT 'txt2img',
    favorite    INTEGER DEFAULT 0,
    flagged     INTEGER DEFAULT 0,        -- 内容策略命中标记
    elapsed_ms  INTEGER,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_images_created  ON images(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_images_job      ON images(job_id);
CREATE INDEX IF NOT EXISTS idx_images_favorite ON images(favorite);

CREATE TABLE IF NOT EXISTS settings_kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

"""


# 可作为瞬时故障重试的 SQLite 错误关键词
# （多线程写库 + WAL 检查点回收时可能出现 readonly / locked）
_TRANSIENT_KEYWORDS = ("readonly", "read-only", "locked", "busy", "disk i/o error")


def _is_transient(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(k in text for k in _TRANSIENT_KEYWORDS)


# 与 app.blobs.MIN_BLOB_LEN 保持一致：超过它就算「塞了图片的大字段」
_BIG_STRING_LIMIT = 4096
_STRIP_NOTE = "<{kb}KB 图片数据已省略：任务已结束，出图结果见项目目录>"


def _strip_big_strings(value, limit: int = _BIG_STRING_LIMIT):
    """递归把超长字符串换成占位说明，返回 (新对象, 替换个数)。"""
    if isinstance(value, str):
        if len(value) > limit:
            return _STRIP_NOTE.format(kb=len(value) // 1024), 1
        return value, 0
    if isinstance(value, list):
        out, n = [], 0
        for item in value:
            new, c = _strip_big_strings(item, limit)
            out.append(new)
            n += c
        return out, n
    if isinstance(value, dict):
        out, n = {}, 0
        for k, item in value.items():
            new, c = _strip_big_strings(item, limit)
            out[k] = new
            n += c
        return out, n
    return value, 0


class _ResilientConnection:
    """sqlite3 连接包装：瞬时错误时自动重连并重试单条语句。

    背景：出图过程中进度回调（工作线程）与结果回写（事件循环线程）会并发写库，
    反复开关连接会让 WAL 检查点回收与写入相撞，偶发 "attempt to write a readonly
    database"。这里用常驻连接 + 重连重试把这问题消化在数据层。
    """

    def __init__(self, db: "Database", autocommit: bool = False) -> None:
        self._db = db
        self._autocommit = autocommit
        self._conn = db._new_connection(autocommit=autocommit)
        self._lock = threading.RLock()

    # ---- 内部 ----
    def _reconnect(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = self._db._new_connection(autocommit=self._autocommit)

    @staticmethod
    def _sleep(attempt: int) -> None:
        time.sleep(0.15 * (attempt + 1))

    # ---- 语句执行（带重试）----
    def execute(self, sql: str, *args):
        for attempt in range(3):
            try:
                with self._lock:
                    return self._conn.execute(sql, *args)
            except sqlite3.Error as exc:
                if attempt >= 2 or not _is_transient(exc):
                    raise
                self._sleep(attempt)
                self._reconnect()
        raise RuntimeError("unreachable")

    def executemany(self, sql: str, seq):
        for attempt in range(3):
            try:
                with self._lock:
                    return self._conn.executemany(sql, seq)
            except sqlite3.Error as exc:
                if attempt >= 2 or not _is_transient(exc):
                    raise
                self._sleep(attempt)
                self._reconnect()
        raise RuntimeError("unreachable")

    def executescript(self, script: str):
        with self._lock:
            return self._conn.executescript(script)

    def commit(self) -> None:
        for attempt in range(3):
            try:
                with self._lock:
                    return self._conn.commit()
            except sqlite3.Error as exc:
                if attempt >= 2 or not _is_transient(exc):
                    raise
                self._sleep(attempt)

    def rollback(self) -> None:
        try:
            with self._lock:
                self._conn.rollback()
        except Exception:
            pass

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass

    def __getattr__(self, item):
        return getattr(self._conn, item)


class Database:
    """轻量 SQLite 封装（常驻连接 + 写串行化 + 瞬时错误重试）。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or settings.db_path_abs
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._read_lock = threading.Lock()
        self._writer: _ResilientConnection | None = None
        self._reader: _ResilientConnection | None = None
        self._initialized = False
        self._schemas: dict[str, tuple[str, tuple]] = {}

    # ---------------- 基础 ----------------
    def _new_connection(self, autocommit: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        if autocommit:
            # 读连接保持自动提交，确保每次都读到最新数据
            conn.isolation_level = None
        return conn

    def _write_conn(self) -> _ResilientConnection:
        if self._writer is None:
            self._writer = _ResilientConnection(self)
        return self._writer

    def _read_conn(self) -> _ResilientConnection:
        if self._reader is None:
            self._reader = _ResilientConnection(self, autocommit=True)
        return self._reader

    def init_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            conn = self._write_conn()
            conn.executescript(CORE_SCHEMA)
            for name, (script, migrations) in self._schemas.items():
                self._apply_business_schema(conn, name, script, migrations)
            self._migrate(conn)
            self._initialized = True

    def _apply_business_schema(self, conn, name, script, migrations) -> None:
        if script:
            conn.executescript(script)
        self.apply_migrations(conn, migrations)
        conn.commit()

    def register_schema(self, name: str, script: str = "", migrations=()) -> None:
        """供各应用注册自己的表结构与迁移（幂等）。

        注册发生在应用模块 import 时；若数据库已初始化（例如先被门户
        启动了核心表），会立即补建该应用的表。
        """
        self._schemas[name] = (script, migrations)
        if self._initialized:
            with self._init_lock:
                self._apply_business_schema(self._write_conn(), name, script, migrations)

    # 旧库增量迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表补字段，
    # 这里按「缺什么补什么」的方式演进，保证老项目数据不丢。
    _MIGRATIONS: tuple = (
    )

    def _migrate(self, conn: _ResilientConnection) -> None:
        self.apply_migrations(conn, self._MIGRATIONS)
        shrank = self._shrink_oversized_job_params(conn)
        conn.commit()
        if shrank:
            self._vacuum_quietly(conn)

    def apply_migrations(self, conn: _ResilientConnection, migrations) -> None:
        """执行迁移列表：元素可以是 (表, 列, DDL) 或 可调用对象(conn)。"""
        for item in migrations:
            if callable(item):
                try:
                    item(conn)
                except Exception as exc:  # pragma: no cover - 防御性
                    print(f"[DB] 业务迁移失败：{exc}")
                continue
            table, column, ddl = item
            try:
                rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            except Exception:
                continue
            if not rows:
                continue
            if column in {r["name"] for r in rows}:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                print(f"[DB] 迁移：{table}.{column} 已补齐")
            except Exception as exc:  # pragma: no cover - 防御性
                print(f"[DB] 迁移 {table}.{column} 失败：{exc}")

    # jobs.params_json 超过这个字符数就认为是「塞了图片的老数据」
    # （正常参数快照只有几百字节，最长也不过几 KB）
    _OVERSIZE_PARAMS_CHARS = 200_000

    def _shrink_oversized_job_params(self, conn: _ResilientConnection) -> bool:
        """清理历史任务参数里塞进去的 base64 图片数据（一次性回收空间）。

        2026-10 之前的版本把分镜参考图编码成 data URL 直接写进 `jobs.params_json`，
        单条任务 4~5MB，实测本机 jobs 表涨到 151MB，最终写坏 -wal 让整个漫画模块
        报 `database disk image is malformed`。

        出图结果早已落盘、任务也早已结束，参数快照里再留一份图片没有意义：
        这里把超长字符串换成一行占位说明，只保留真正的参数（提示词 / 尺寸 / 种子）。
        新任务不再走这条路（见 app/blobs.py，大字段外置到文件）。
        """
        try:
            rows = conn.execute(
                "SELECT id, params_json FROM jobs WHERE LENGTH(params_json) > ?",
                (self._OVERSIZE_PARAMS_CHARS,),
            ).fetchall()
        except Exception:  # pragma: no cover - 防御性
            return False
        if not rows:
            return False

        freed = 0
        for row in rows:
            text = row["params_json"] or ""
            try:
                data = json.loads(text)
            except Exception:
                continue
            compact, dropped = _strip_big_strings(data)
            if not dropped:
                continue
            new_text = json.dumps(compact, ensure_ascii=False)
            conn.execute("UPDATE jobs SET params_json = ? WHERE id = ?",
                         (new_text, row["id"]))
            freed += max(0, len(text) - len(new_text))
        if freed:
            print(f"[DB] 清理历史任务参数里的图片数据：{len(rows)} 条任务，"
                  f"回收约 {freed / 1048576:.1f}MB")
            return True
        return False

    def _vacuum_quietly(self, conn: _ResilientConnection) -> None:
        """回收磁盘空间（VACUUM 需要临时空间，失败绝不影响启动）。"""
        import shutil as _shutil

        try:
            before = self.db_path.stat().st_size
        except OSError:
            return
        if before > 2 * 1024 ** 3:  # 超大库不动它，避免长时间占用磁盘
            print("[DB] 数据库超过 2GB，跳过自动 VACUUM（可手动执行）")
            return
        free = _shutil.disk_usage(str(self.db_path.parent)).free
        if free < before * 2:  # VACUUM 需要约等于库大小的临时空间
            print("[DB] 磁盘剩余空间不足，跳过自动 VACUUM")
            return
        try:
            conn.execute("VACUUM")
            # WAL 模式下 VACUUM 的结果先写进 -wal，必须再检查点回写并截断，
            # 主库文件才会真正变小（否则 156MB 的壳子一直留在磁盘上）
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:  # pragma: no cover - 防御性
            print(f"[DB] VACUUM 跳过：{exc}")
            return
        try:
            after = self.db_path.stat().st_size
        except OSError:
            return
        print(f"[DB] 数据库已瘦身：{before / 1048576:.0f}MB → {after / 1048576:.0f}MB")

    @contextmanager
    def write(self) -> Iterator[_ResilientConnection]:
        """写事务：进程内串行 + 自动提交 / 回滚。"""
        self.init_schema()
        with self._write_lock:
            conn = self._write_conn()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def read(self) -> Iterator[_ResilientConnection]:
        """读连接（常驻，避免频繁开关触发 WAL 检查点）。"""
        self.init_schema()
        conn = self._read_conn()
        try:
            yield conn
        finally:
            pass

    def close_all(self) -> None:
        for c in (self._writer, self._reader):
            if c is not None:
                c.close()
        self._writer = None
        self._reader = None
        self._initialized = False


db = Database()
