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

from app.config import settings

SCHEMA = """
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

-- ============================================================
--  漫画工作室
-- ============================================================
CREATE TABLE IF NOT EXISTS comic_projects (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    style       TEXT NOT NULL DEFAULT 'jp_bw',      -- 风格预设 key
    layout      TEXT NOT NULL DEFAULT 'grid_2x2',   -- 排版预设 key
    width       INTEGER NOT NULL DEFAULT 768,       -- 单格出图宽
    height      INTEGER NOT NULL DEFAULT 768,       -- 单格出图高
    steps       INTEGER NOT NULL DEFAULT 8,
    guidance    REAL NOT NULL DEFAULT 4.0,
    negative    TEXT DEFAULT '',                    -- 画面通用反向提示词（默认已填好，可改）
    char_negative TEXT DEFAULT '',                  -- 人物专用反向提示词（压制畸形/怪表情）
    ref_strength REAL DEFAULT 0.55,                 -- 分镜图生图去噪强度：越低越像形象图
    synopsis    TEXT DEFAULT '',                    -- 故事梗概 / 原始剧本
    keep_style  INTEGER DEFAULT 1,                  -- 是否强制统一画风
    use_ref     INTEGER DEFAULT 1,                  -- 角色已有形象图时是否走图生图保持形象
    output_dir  TEXT DEFAULT '',                    -- 项目专属出图目录（相对 output_dir）
    profile     TEXT DEFAULT '',                    -- 生成档位 dev|target|''（配置分辨率/步数/CFG）
    theme_color TEXT DEFAULT '',                    -- 主题色 id（handraw 36 色，如 C-01）
    theme_color2 TEXT DEFAULT '',                   -- 点缀色 id（可选）
    style_ref_enabled INTEGER DEFAULT 0,            -- 是否挂画风参考图兜底
    rhythm_template TEXT DEFAULT '',                -- 分镜节奏模板（handraw SB-xxx）
    batch       INTEGER DEFAULT 1,                  -- 一次出几张候选
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comic_characters (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES comic_projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    role        TEXT DEFAULT '',                    -- 主角/配角/反派
    gender      TEXT DEFAULT '',                    -- 男/女/其他（角色提取产出）
    age         TEXT DEFAULT '',                    -- 年龄或年龄段
    appearance  TEXT DEFAULT '',                    -- 外貌特征
    outfit      TEXT DEFAULT '',                    -- 服装
    personality TEXT DEFAULT '',
    background  TEXT DEFAULT '',                    -- 人物小传
    detail_prompt TEXT DEFAULT '',                  -- 英文角色细节描述（喂绘图的稳定部分）
    negative    TEXT DEFAULT '',                    -- 该角色额外反向提示词（叠加在人物反提示词之后）
    prompt      TEXT DEFAULT '',                    -- 最近一次形象图的实际提示词
    ref_path    TEXT,                               -- 形象图本地路径（本地生成，也可手动上传覆盖）
    ref_url     TEXT,
    ref_source  TEXT DEFAULT '',                    -- local=本地模型生成 / upload=手动上传
    status      TEXT DEFAULT 'draft',               -- 形象图状态 draft|queued|running|done|failed
    job_id      TEXT,
    image_id    TEXT,
    image_url   TEXT,
    error       TEXT,
    seed        INTEGER DEFAULT -1,                 -- 固定种子，形象图与分镜共用，保证前后一致
    created_at  TEXT NOT NULL,
    updated_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_comic_char_project ON comic_characters(project_id);

-- 场景设定（由剧本提取，可分别生成场景概念图）
CREATE TABLE IF NOT EXISTS comic_scenes (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES comic_projects(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL DEFAULT 0,
    name        TEXT NOT NULL,                      -- 场景名（分镜按名字引用）
    location    TEXT DEFAULT '',                    -- 具体地点
    time_of_day TEXT DEFAULT '',                    -- 日/夜/黄昏/清晨
    atmosphere  TEXT DEFAULT '',
    desc        TEXT DEFAULT '',                    -- 画面描述
    prompt      TEXT DEFAULT '',                    -- 英文概念图提示词（AI 提取产物，拼装时作为输入）
    last_prompt TEXT DEFAULT '',                    -- 最近一次实际使用的完整提示词（只用于展示，绝不参与拼装）
    status      TEXT DEFAULT 'draft',               -- draft|queued|running|done|failed
    job_id      TEXT,
    image_id    TEXT,
    image_url   TEXT,
    seed        INTEGER DEFAULT -1,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comic_scene_project ON comic_scenes(project_id, seq);
CREATE INDEX IF NOT EXISTS idx_comic_scene_job     ON comic_scenes(job_id);

CREATE TABLE IF NOT EXISTS comic_panels (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES comic_projects(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL DEFAULT 0,       -- 全局顺序
    page          INTEGER NOT NULL DEFAULT 1,       -- 页码
    cell          INTEGER NOT NULL DEFAULT 0,       -- 页内格位
    shot          TEXT DEFAULT 'medium',            -- 景别 key
    scene         TEXT DEFAULT '',                  -- 画面描述
    dialogue      TEXT DEFAULT '',                  -- 对话 / 旁白
    sfx           TEXT DEFAULT '',                  -- 拟声词
    scene_id      TEXT,                             -- 所属场景（comic_scenes.id）
    character_ids TEXT DEFAULT '[]',                -- JSON 数组
    extra_prompt  TEXT DEFAULT '',                  -- 追加提示词
    prompt        TEXT DEFAULT '',                  -- 最近一次实际使用的完整提示词
    status        TEXT DEFAULT 'draft',             -- draft|queued|running|done|failed
    job_id        TEXT,
    image_id      TEXT,
    image_url     TEXT,
    seed          INTEGER DEFAULT -1,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comic_panel_project ON comic_panels(project_id, seq);
CREATE INDEX IF NOT EXISTS idx_comic_panel_job     ON comic_panels(job_id);

CREATE TABLE IF NOT EXISTS comic_renders (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES comic_projects(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    url         TEXT NOT NULL,
    layout      TEXT NOT NULL,
    page        INTEGER DEFAULT 1,
    width       INTEGER,
    height      INTEGER,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comic_render_project ON comic_renders(project_id, created_at DESC);
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
            conn.executescript(SCHEMA)
            self._migrate(conn)
            self._initialized = True

    # 旧库增量迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表补字段，
    # 这里按「缺什么补什么」的方式演进，保证老项目数据不丢。
    _MIGRATIONS: tuple[tuple[str, str, str], ...] = (
        ("comic_characters", "gender", "TEXT DEFAULT ''"),
        ("comic_characters", "age", "TEXT DEFAULT ''"),
        ("comic_characters", "background", "TEXT DEFAULT ''"),
        ("comic_characters", "detail_prompt", "TEXT DEFAULT ''"),
        ("comic_characters", "prompt", "TEXT DEFAULT ''"),
        ("comic_characters", "ref_source", "TEXT DEFAULT ''"),
        ("comic_characters", "status", "TEXT DEFAULT 'draft'"),
        ("comic_characters", "job_id", "TEXT"),
        ("comic_characters", "image_id", "TEXT"),
        ("comic_characters", "image_url", "TEXT"),
        ("comic_characters", "error", "TEXT"),
        ("comic_characters", "updated_at", "TEXT"),
        ("comic_characters", "negative", "TEXT DEFAULT ''"),
        ("comic_panels", "scene_id", "TEXT"),
        ("comic_panels", "extra_prompt", "TEXT DEFAULT ''"),
        ("comic_scenes", "last_prompt", "TEXT DEFAULT ''"),
        ("comic_projects", "ref_strength", "REAL DEFAULT 0.55"),
        ("comic_projects", "char_negative", "TEXT DEFAULT ''"),
        # 项目专属出图目录（相对 output_dir，形如 comics/雨夜天台_1a2b3c）；
        # 持久化后项目改名不会导致已有图片搬家，新图继续落在同一目录
        ("comic_projects", "output_dir", "TEXT DEFAULT ''"),
        # ---- handraw-style 接入（画风库 / 主题色 / 分镜节奏）----
        # 生成档位：dev（6GB 开发档）/ target（16GB 目标档）/ 空（沿用项目自带值）
        ("comic_projects", "profile", "TEXT DEFAULT ''"),
        # 主题色（handraw 36 色，存颜色 id 如 C-01；空=不注入）
        ("comic_projects", "theme_color", "TEXT DEFAULT ''"),
        ("comic_projects", "theme_color2", "TEXT DEFAULT ''"),
        # 是否挂画风参考图兜底（P2，默认关；16GB 实测后再决定）
        ("comic_projects", "style_ref_enabled", "INTEGER DEFAULT 0"),
        # 分镜节奏模板（handraw SB-xxx，空=用项目排版预设的默认节奏）
        ("comic_projects", "rhythm_template", "TEXT DEFAULT ''"),
        # 一次出几张候选（16GB 下 batch 2~4 很实用；默认 1）
        ("comic_projects", "batch", "INTEGER DEFAULT 1"),
    )

    def _migrate(self, conn: _ResilientConnection) -> None:
        for table, column, ddl in self._MIGRATIONS:
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
        self._backfill_comic_defaults(conn)
        self._clean_scene_prompt_pollution(conn)
        shrank = self._shrink_oversized_job_params(conn)
        conn.commit()
        if shrank:
            self._vacuum_quietly(conn)

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

    @staticmethod
    def _clean_scene_prompt_pollution(conn: _ResilientConnection) -> None:
        """修复历史上被自身输出「套娃」的场景提示词。

        旧实现在出图时把「拼装后的完整提示词」回写到 prompt 字段，而该字段
        又是拼装的输入 —— 于是每生成一次就多套一层包装
        （画风前缀 / background concept art / location setting 各多一份），
        提示词越来越长、越来越乱，最终出图失败。这里剥回最初的核心英文提示词。
        """
        import re

        try:
            rows = conn.execute(
                "SELECT id, prompt FROM comic_scenes "
                "WHERE prompt LIKE '%background concept art%'"
            ).fetchall()
        except Exception:
            return

        patterns = (
            r"background concept art, environment only, no people,\s*(.*?),?\s*location setting:",
            r"background concept art, environment only, no people,\s*(.*?),?\s*establishing wide view",
            r"background concept art, environment only, no people,\s*(.+)",
        )
        fixed = 0
        for row in rows:
            text = (row["prompt"] or "").strip()
            original = text
            # 污染是嵌套的（3 层包装 = 套了 3 次），要反复剥直到干净
            for _ in range(8):
                if "background concept art" not in text:
                    break
                core = ""
                for pat in patterns:
                    m = re.search(pat, text, re.S)
                    if m:
                        cand = m.group(1).strip().strip(",").strip()
                        if cand:
                            core = cand
                            break
                if not core or core == text:
                    break
                text = core
            if text and text != original:
                conn.execute("UPDATE comic_scenes SET prompt = ? WHERE id = ?",
                             (text, row["id"]))
                fixed += 1
        if fixed:
            print(f"[DB] 修复场景提示词自污染：{fixed} 个场景已剥回原始英文提示词")

    @staticmethod
    def _backfill_comic_defaults(conn: _ResilientConnection) -> None:
        """给存量项目补上默认反向提示词与形象锁定强度。

        只在字段为空时填充，用户手填过的值不会被动。
        模板与「漫画工作室」共用 comic_defaults 里的同一份文本。
        """
        from app.comic_defaults import DEFAULT_CHAR_NEGATIVE, DEFAULT_PROJECT_NEGATIVE

        try:
            rows = conn.execute("PRAGMA table_info(comic_projects)").fetchall()
        except Exception:
            return
        cols = {r["name"] for r in rows}
        if not rows:
            return
        if "negative" in cols:
            cur = conn.execute(
                "UPDATE comic_projects SET negative = ? WHERE negative IS NULL OR TRIM(negative) = ''",
                (DEFAULT_PROJECT_NEGATIVE,),
            )
            if cur.rowcount:
                print(f"[DB] 回填默认反向提示词：{cur.rowcount} 个项目")
        if "char_negative" in cols:
            conn.execute(
                "UPDATE comic_projects SET char_negative = ? "
                "WHERE char_negative IS NULL OR TRIM(char_negative) = ''",
                (DEFAULT_CHAR_NEGATIVE,),
            )
        if "ref_strength" in cols:
            conn.execute(
                "UPDATE comic_projects SET ref_strength = 0.55 "
                "WHERE ref_strength IS NULL OR ref_strength <= 0"
            )

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
