# -*- coding: utf-8 -*-
"""素材导出：ZIP 资源树 / 图集拼版 / 引擎导入清单。

资源树布局（可直接拖进引擎）：
    <项目名>/
    ├── manifest.json            全量元数据（名称/描述/提示词/seed/色板/质量）
    ├── palette.csv              逐张素材清单（文件/类别/名称/季节/等级/seed/色距）
    ├── scene/  character/  pet/  weapon/  building/  monster/
    │   └── scene_起点村庄_spring_101.png     命名：kind_名称[_season][_tier]_seed
    └── engine/unity.json | godot.json | phaser.json   （可选导入清单）

命名安全：文件名组件剥掉路径非法字符，中文保留（现代引擎都支持）。
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path

from shared.config import settings

_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')


def _safe(text: str, maxlen: int = 24) -> str:
    return _UNSAFE.sub("", str(text or ""))[:maxlen] or "unnamed"


def asset_filename(a: dict) -> str:
    parts = [a["kind"], _safe(a["name"])]
    if a.get("season"):
        parts.append(a["season"])
    if a.get("tier_id"):
        parts.append(a["tier_id"])
    parts.append(str(a.get("seed") or 0))
    return "_".join(parts) + ".png"


def _manifest(s: dict) -> dict:
    return {
        "id": s["id"], "name": s["name"], "idea": s.get("idea"),
        "style": s.get("style_name") or s.get("style"),
        "time_preset": s.get("time_name") or s.get("time_preset"),
        "scale": s.get("scale"),
        "master": {"path": s["master"]["path"], "seed": s["master"].get("seed"),
                   "palette": s["master"].get("palette")} if s.get("master") else None,
        "assets": [{
            "file": f"{a['kind']}/{asset_filename(a)}",
            "kind": a["kind"], "name": a["name"], "desc": a.get("desc"),
            "season": a.get("season"), "tier": a.get("tier"),
            "usage": a.get("usage"), "scene": a.get("scene"),
            "seed": a.get("seed"), "size": a.get("size"),
            "transparent": (a.get("transparent") or {}).get("method"),
            "tone_score": (a.get("tone") or {}).get("score"),
            "quality": {k: (a.get("quality") or {}).get(k)
                        for k in ("pass", "reasons", "attempts")},
            "prompt": a.get("prompt"),
            "anim": ({"level": (a["anim"] or {}).get("level"),
                      "cell": (a["anim"] or {}).get("cell"),
                      "anchor": (a["anim"] or {}).get("anchor"),
                      "dirs": (a["anim"] or {}).get("dirs"),
                      "clips": (a["anim"] or {}).get("clips"),
                      "frames_total": (a["anim"] or {}).get("frames_total")}
                     if a.get("anim") else None),
        } for a in s["assets"]],
    }


def _engine_manifest(s: dict, engine: str) -> dict:
    """引擎导入清单：统一字段 + 引擎特有包装。"""
    m = _manifest(s)
    base = [{"file": a["file"], "kind": a["kind"], "name": a["name"],
             "season": a.get("season"), "tier": a.get("tier"),
             "usage": a.get("usage"), "scene": a.get("scene"),
             "width": (a.get("size") or [None])[0],
             "height": (a.get("size") or [None, None])[1]}
            for a in m["assets"]]
    if engine == "unity":
        return {"format": "unity", "resourcesFolder": "Assets/Art/GameAssets",
                "spriteImport": {"textureType": "Sprite (2D and UI)",
                                 "pivot": "0.5,0.5"},
                "assets": base}
    if engine == "godot":
        return {"format": "godot", "importBase": "res://assets/gameassets",
                "assets": [{**a, "path": f"res://assets/gameassets/{a['file']}"}
                           for a in base]}
    if engine == "phaser":
        return {"format": "phaser",
                "load": [{"key": f"{a['kind']}_{a['name']}"
                                   + (f"_{a['season']}" if a.get("season") else "")
                                   + (f"_{a['tier']}" if a.get("tier") else ""),
                          "url": a["file"]} for a in base]}
    raise ValueError(f"未知引擎模板：{engine}（可选 unity | godot | phaser）")


def build_zip(s: dict, engine_tpl: str | None = None) -> Path:
    """把素材集打包成 ZIP，返回 zip 路径。"""
    out_dir = settings.data_dir_path / "exports" / "gameassets"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{s['id']}_{_safe(s['name'])}.zip"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        root = _safe(s["name"], 32)
        m = _manifest(s)
        zf.writestr(f"{root}/manifest.json",
                    json.dumps(m, ensure_ascii=False, indent=1))
        # 逐张清单 CSV
        csv_buf = io.StringIO()
        w = csv.writer(csv_buf)
        w.writerow(["file", "kind", "name", "season", "tier", "usage", "scene",
                    "seed", "width", "height", "transparent",
                    "tone_score", "quality_pass", "attempts"])
        for a in m["assets"]:
            size = a.get("size") or [None, None]
            w.writerow([a["file"], a["kind"], a["name"], a["season"] or "",
                        a["tier"] or "", a["usage"] or "", a["scene"] or "",
                        a["seed"], size[0], size[1],
                        a["transparent"] or "", a["tone_score"],
                        a["quality"]["pass"], a["quality"]["attempts"]])
        zf.writestr(f"{root}/palette.csv", csv_buf.getvalue().encode("utf-8-sig"))
        # 引擎导入清单
        if engine_tpl:
            zf.writestr(f"{root}/engine/{engine_tpl}.json",
                        json.dumps(_engine_manifest(s, engine_tpl),
                                   ensure_ascii=False, indent=1))
        # 图片本体
        for a in s["assets"]:
            p = Path(a["path"])
            if not p.is_absolute():
                p = Path(".") / p
            if p.exists():
                zf.write(p, f"{root}/{a['kind']}/{asset_filename(a)}")
        # 动画图集（角色/宠物）：每方向一张 PNG + anim.json 帧表
        anim_index = []
        for a in s["assets"]:
            am = a.get("anim")
            if not am:
                continue
            banks = {}
            for d, b in (am.get("banks") or {}).items():
                bp = Path(b.get("path") or "")
                if not bp.is_absolute():
                    bp = Path(".") / bp
                name = f"anim/{_safe(a['name'])}_{_safe(a.get('gender') or '')}_{d}.png"
                if bp.exists():
                    zf.write(bp, f"{root}/{name}")
                banks[d] = {"file": name, "cols": b.get("cols"),
                            "rows": {k: v.get("row") for k, v in
                                     (b.get("clips") or {}).items()}}
            anim_index.append({
                "kind": a["kind"], "name": a["name"], "gender": a.get("gender"),
                "level": am.get("level"), "level_name": am.get("level_name"),
                "cell": am.get("cell"), "anchor": am.get("anchor"),
                "dirs": am.get("dirs"), "mirror": am.get("mirror"),
                "clips": am.get("clips"), "frames_total": am.get("frames_total"),
                "banks": banks})
        if anim_index:
            zf.writestr(f"{root}/anim/anim.json",
                        json.dumps({"view": "2.5D 45°", "items": anim_index},
                                   ensure_ascii=False, indent=1))
        # 基准图
        if s.get("master"):
            mp = Path(s["master"]["path"])
            if not mp.is_absolute():
                mp = Path(".") / mp
            if mp.exists():
                zf.write(mp, f"{root}/master/master.png")
    zip_path.write_bytes(buf.getvalue())
    return zip_path


def build_atlas(s: dict, kind: str, cols: int = 4, pad: int = 8) -> Path:
    """把某一类素材拼成图集（sprite sheet），返回 PNG 路径。

    取该类最新素材，按 cell 最大宽高对齐网格；返回图同时带坐标索引 JSON 旁边。
    """
    from PIL import Image

    assets = [a for a in s["assets"] if a["kind"] == kind]
    if not assets:
        raise ValueError(f"「{kind}」类暂无素材")
    cols = max(1, min(8, cols))
    rows = (len(assets) + cols - 1) // cols
    imgs = []
    for a in assets:
        p = Path(a["path"])
        if not p.is_absolute():
            p = Path(".") / p
        imgs.append(Image.open(p).convert("RGBA"))
    cell_w = max(im.width for im in imgs) + pad * 2
    cell_h = max(im.height for im in imgs) + pad * 2
    sheet = Image.new("RGBA", (cell_w * cols, cell_h * rows), (0, 0, 0, 0))
    index = []
    for i, (a, im) in enumerate(zip(assets, imgs)):
        cx, cy = (i % cols) * cell_w + pad, (i // cols) * cell_h + pad
        sheet.alpha_composite(im, (cx, cy))
        index.append({"col": i % cols, "row": i // cols,
                      "x": cx, "y": cy, "w": im.width, "h": im.height,
                      "name": a["name"], "file": asset_filename(a)})
    out_dir = settings.data_dir_path / "exports" / "gameassets"
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"{s['id']}_{kind}_atlas.png"
    sheet.save(png, "PNG")
    (png.with_suffix(".json")).write_text(
        json.dumps({"kind": kind, "cols": cols, "cell": [cell_w, cell_h],
                    "items": index}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return png


def export_url(zip_path: Path) -> str:
    try:
        rel = zip_path.resolve().relative_to(settings.data_dir_path.resolve())
        return f"/data/{rel.as_posix()}"
    except ValueError:
        return f"/data/{zip_path.name}"
