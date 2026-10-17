# -*- coding: utf-8 -*-
"""漫画工作室自己的表结构与增量迁移（应用启动时注册进 shared.db）。

拆出来是为了让 shared 层完全不认识任何业务表——漫画的表只属于漫画应用。
"""

from __future__ import annotations

from shared.db import db

COMIC_SCHEMA = """
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

# 增量迁移：元素为 (表, 列, DDL)。
COMIC_MIGRATIONS: tuple = (
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


def _clean_scene_prompt_pollution(conn) -> None:
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

def _backfill_comic_defaults(conn) -> None:
    """给存量项目补上默认反向提示词与形象锁定强度。

    只在字段为空时填充，用户手填过的值不会被动。
    模板与「漫画工作室」共用 comic_defaults 里的同一份文本。
    """
    from apps.comic.backend.defaults import DEFAULT_CHAR_NEGATIVE, DEFAULT_PROJECT_NEGATIVE

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


db.register_schema(
    "comic",
    COMIC_SCHEMA,
    COMIC_MIGRATIONS + (_backfill_comic_defaults, _clean_scene_prompt_pollution),
)
