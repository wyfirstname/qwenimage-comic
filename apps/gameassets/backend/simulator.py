# -*- coding: utf-8 -*-
"""仿真测试（8.0）：把一整套素材拼进模拟游戏画面，并逐张体检。

「仿真」回答两个问题：
1. 这堆图放在一起，像不像一个能玩的游戏？——按场景分组合成模拟画面：
   场景打底，建筑压远角，怪物按等级定大小站在右侧，主角带宠物站前台，
   武器进底部假动作栏，再加 HUD。等级决定体积感（与生成侧同一套预设），
   一眼就能看出「终局boss比杂兵大、主角和场景比例是否合理」。
2. 每张图交付质量是否达标？——逐张体检四项：
   文件存在 / 画幅与设计一致 / 该透明的有 alpha / 色调在阈值内。

纯 PIL 本地运算：不占显存、不依赖 ComfyUI，秒级完成，可反复跑。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from PIL.ImageFont import FreeTypeFont

from shared.config import settings

from . import store, transmat
from .presets import (ASPECTS, MONSTER_TIER_ORDER, MONSTER_TIERS,
                      SEASONS, SCENE_USAGES, transparent_mode)

CANVAS = (1280, 720)          # 模拟画面统一 16:9 桌面视口
GROUND_Y = int(CANVAS[1] * 0.93)

# 等级 → 画面高度（px @720p）。与生成侧 MONSTER_TIERS 同源：
# 杂兵只到膝盖、终局占半个屏幕——体积感就是等级的主要视觉信号。
_TIER_HEIGHT = {"minion": 92, "elite": 135, "boss": 200, "worldboss": 290}
_CHAR_H = 250                 # 主角站高（约画面 1/3，2:3 立绘缩放后）
_PET_H = 110                  # 宠物到主角腰
_BUILD_H = 300                # 建筑压远景（不作主体）
_SLOT = 58                    # 动作栏图标位

_FONT: FreeTypeFont | None = None
_FONT_TRIED = False


def _font(size: int) -> FreeTypeFont:
    global _FONT, _FONT_TRIED
    if not _FONT_TRIED:
        _FONT_TRIED = True
        for cand in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            if Path(cand).exists():
                try:
                    _FONT = ImageFont.truetype(cand, size)
                except Exception:
                    _FONT = None
                break
    return _FONT or ImageFont.load_default(size)


# ============================================================
#  逐张体检
# ============================================================

def _designed_size(s: dict, a: dict) -> tuple[int, int] | None:
    """该素材的设计画幅：记录里的 size 优先；老记录按方案条目现算。"""
    sz = a.get("size")
    if isinstance(sz, (list, tuple)) and len(sz) == 2:
        return int(sz[0]), int(sz[1])
    from .presets import asset_size
    items = s["pipeline"]["stages"].get(a["kind"], {}).get("items", [])
    item = next((it for it in items if it.get("id") == a.get("item_id")), None)
    if item is None:                       # 老数据按类默认兜底
        return ASPECTS.get(a["kind"])
    return asset_size(s, a["kind"], item)


def _has_alpha(path: Path) -> bool | None:
    """图里是否真有透明内容：按透明像素占比判断（见 _transparency_ratio）。"""
    try:
        img = Image.open(path)
    except Exception:
        return None
    if img.mode not in ("RGBA", "LA"):
        return False
    return _transparency_ratio(img.convert("RGBA")) > 0.01


def check_asset(s: dict, a: dict) -> dict:
    """单张素材体检：exists / size / alpha / tone，全过 ok=True。"""
    path = Path(a.get("path") or "")
    exists = path.is_file()
    checks: dict = {"exists": bool(exists)}

    if exists:
        try:
            with Image.open(path) as img:
                actual = img.size
        except Exception:
            actual = None
        expect = _designed_size(s, a)
        checks["size"] = {"expect": list(expect) if expect else None,
                          "actual": list(actual) if actual else None,
                          "pass": bool(expect and actual
                                       and abs(actual[0] - expect[0]) <= 64
                                       and abs(actual[1] - expect[1]) <= 64)}
        tmode = transparent_mode(s, a["kind"])
        if tmode != "none":
            has = _has_alpha(path)
            checks["alpha"] = {"mode": tmode, "has": has, "pass": bool(has)}
    else:
        checks["size"] = {"expect": None, "actual": None, "pass": False}

    tone = (a.get("tone") or {})
    if tone.get("pass") is not None:
        checks["tone"] = bool(tone["pass"])

    ok = all(v if isinstance(v, bool) else v.get("pass")
             for v in checks.values())
    return {"id": a["id"], "name": a["name"], "kind": a["kind"],
            "kind_label": a.get("kind_label") or store.STAGE_LABELS[a["kind"]],
            "gender": a.get("gender_name"), "tier": a.get("tier"),
            "season": a.get("season_name"),
            "checks": checks, "ok": bool(ok)}


# ============================================================
#  合成模拟画面
# ============================================================

def _transparency_ratio(img: Image.Image) -> float:
    """alpha<128 的像素占比。有些引擎出图带一个几乎不透明的 alpha 通道
    （min 208 这类），只看 getextrema() 会误判成「已有透明底」。"""
    if img.mode not in ("RGBA", "LA"):
        return 0.0
    hist = img.getchannel("A").histogram()
    return sum(hist[:128]) / (img.width * img.height)


def _load(path: Path) -> Image.Image | None:
    try:
        img = Image.open(path).convert("RGBA")
    except Exception:
        return None
    # 真有透明内容的图裁到内容包围盒——去掉四周留白，贴图比例才真实
    if _transparency_ratio(img) > 0.01:
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
    return img


def _cutout(path: Path, cache_dir: Path) -> Image.Image | None:
    """白底素材现场色键抠图（结果缓存）；抠不动返回 None，调用方退回软边罩。

    只对「本来就该透明底」的类启用（角色/宠物/武器/野怪）；
    alpha_ratio 异常（接近 0 = 抠没了，接近 1 = 根本不是白底）视为失败。
    """
    try:
        st = path.stat()
    except OSError:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    dst = cache_dir / f"{path.stem}_{st.st_mtime_ns % 10**12}.png"
    if not dst.is_file():
        try:
            rep = transmat.make_transparent(path, dst, "chroma")
            if not 0.02 < rep.get("alpha_ratio", 0) < 0.985:
                dst.unlink(missing_ok=True)
                return None
        except Exception:
            return None
    return _load(dst)


def _sprite(path: Path, *, cut: bool, cache_dir: Path) -> Image.Image | None:
    """精灵图：优先现场抠图（该透明的类），失败退回原图。"""
    if cut:
        img = _cutout(path, cache_dir)
        if img:
            return img
    return _load(path)


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """cover 模式铺满（等比放大裁切，不拉伸）。"""
    sw, sh = img.size
    scale = max(w / sw, h / sh)
    img = img.resize((max(1, round(sw * scale)), max(1, round(sh * scale))),
                     Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def _soft_card(img: Image.Image) -> Image.Image:
    """抠图失败的兜底（如白袍白底）：中心裁掉大片留白，套椭圆软边罩——
    让它读作「设定图卡片」而不是一块生硬的矩形。"""
    w, h = img.size
    img = img.crop((int(w * 0.16), int(h * 0.10), int(w * 0.84), int(h * 0.90)))
    w, h = img.size
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, w - 1, h - 1), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(min(w, h) * 0.07))
    return Image.merge("RGBA", (*img.convert("RGB").split(), mask))


def _paste_sprite(canvas: Image.Image, img: Image.Image, *, cx: int,
                  ground: int, height: int, flip: bool = False) -> None:
    """把一张精灵按目标高度贴到地面上（底部中心对齐 cx）；
    没有透明通道的图套椭圆软边罩，再补一个地面阴影。"""
    w, h = img.size
    scale = height / h
    img = img.resize((max(1, round(w * scale)), height), Image.LANCZOS)
    if flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if img.mode != "RGBA" or _transparency_ratio(img) < 0.01:
        img = _soft_card(img)
    # 地面阴影
    sh = ImageDraw.Draw(canvas)
    rw = img.width * 0.42
    sh.ellipse((cx - rw, ground - 9, cx + rw, ground + 7), fill=(0, 0, 0, 70))
    canvas.alpha_composite(img, (int(cx - img.width / 2), int(ground - height)))


def _hud(canvas: Image.Image, title: str, sub: str) -> None:
    """模拟游戏 HUD：顶部信息条 + 底部渐变（遮住 sprite 底边的生硬感）。"""
    d = ImageDraw.Draw(canvas, "RGBA")
    w, h = canvas.size
    for i in range(64):                              # 顶部渐变条
        d.line([(0, i), (w, i)], fill=(10, 12, 20, int(150 * (1 - i / 64))))
    for i in range(90):                              # 底部渐变
        d.line([(0, h - 90 + i), (w, h - 90 + i)],
               fill=(8, 10, 16, int(120 * i / 90)))
    d.text((18, 14), title, font=_font(24), fill=(255, 255, 255, 235))
    d.text((18, 46), sub, font=_font(14), fill=(255, 255, 255, 170))
    d.text((w - 18, 16), "SIMULATION", anchor="ra",
           font=_font(13), fill=(255, 255, 255, 110))


def _action_bar(canvas: Image.Image, weapons: list[Image.Image]) -> None:
    """底部假动作栏：武器图标进槽位（检验图标在 UI 里的可读性）。"""
    if not weapons:
        return
    d = ImageDraw.Draw(canvas, "RGBA")
    n = len(weapons)
    bar_w = n * (_SLOT + 10) + 10
    x0 = (canvas.width - bar_w) // 2
    y0 = GROUND_Y - _SLOT - 14
    d.rounded_rectangle((x0, y0, x0 + bar_w, y0 + _SLOT + 20), 12,
                        fill=(15, 18, 28, 170))
    for i, wp in enumerate(weapons[:8]):
        wx = x0 + 10 + i * (_SLOT + 10)
        d.rounded_rectangle((wx, y0 + 8, wx + _SLOT, y0 + 8 + _SLOT), 8,
                            fill=(255, 255, 255, 26))
        im = wp.copy()
        im.thumbnail((_SLOT - 8, _SLOT - 8), Image.LANCZOS)
        if im.mode != "RGBA" or _transparency_ratio(im) < 0.01:
            mask = Image.new("L", im.size, 0)
            ImageDraw.Draw(mask).ellipse((-2, -2, im.width + 2, im.height + 2),
                                         fill=255)
            im = Image.merge("RGBA", (*im.convert("RGB").split(), mask))
        canvas.alpha_composite(im, (wx + (_SLOT - im.width) // 2,
                                    y0 + 8 + (_SLOT - im.height) // 2))


def compose_group(s: dict, scene: dict, monsters: list[dict],
                  characters: list[dict], pets: list[dict],
                  buildings: list[dict], weapons: list[dict],
                  out_path: Path) -> dict:
    """一组（场景×季节）→ 一张模拟游戏画面。"""
    canvas = Image.new("RGBA", CANVAS, (14, 16, 24, 255))
    notes: list[str] = []

    bg = _load(Path(scene["path"]))
    if bg:
        canvas.alpha_composite(_cover(bg, *CANVAS))
    else:
        notes.append("场景图缺失，用底色占位")
    canvas.alpha_composite(
        Image.merge("RGBA", (*(Image.new("RGB", CANVAS, (10, 12, 20)).split()),
                             Image.new("L", CANVAS, 36))))

    cache = out_path.parent / "_cut"
    t_cut = transparent_mode(s, "character") != "none"    # 角/宠/武器/野怪同参
    for i, b in enumerate(buildings):                # 建筑：远角压层次（不抠图）
        img = _load(Path(b["path"]))
        if img:
            _paste_sprite(canvas, img, cx=190 + i * 230, ground=GROUND_Y - 8,
                          height=_BUILD_H, flip=bool(i % 2))
    for i, c in enumerate(characters):               # 主角：中左前台
        img = _sprite(Path(c["path"]), cut=t_cut, cache_dir=cache)
        if img:
            _paste_sprite(canvas, img, cx=430 + i * 260, ground=GROUND_Y,
                          height=_CHAR_H)
    for i, p in enumerate(pets):                     # 宠物：主角脚边
        img = _sprite(Path(p["path"]), cut=t_cut, cache_dir=cache)
        if img:
            _paste_sprite(canvas, img, cx=560 + i * 90, ground=GROUND_Y,
                          height=_PET_H, flip=True)
    mx = CANVAS[0] - 40                              # 怪物：右侧，按等级定大小，
    for i, m in enumerate(monsters):                 # 从右往左按实际宽度排布
        img = _sprite(Path(m["path"]), cut=t_cut, cache_dir=cache)
        if not img:
            continue
        tid = m.get("tier_id") if m.get("tier_id") in MONSTER_TIERS else "minion"
        h = _TIER_HEIGHT[tid]
        w = img.width * h / img.height
        cx = int(min(max(mx - w / 2, 70), CANVAS[0] - w / 2 - 10))
        _paste_sprite(canvas, img, cx=cx, ground=GROUND_Y, height=h,
                      flip=bool(i % 2))
        mx = cx - w / 2 - 26                         # 下一只要留出间隙

    _action_bar(canvas, [im for w_ in weapons
                         if (im := _sprite(Path(w_["path"]), cut=t_cut,
                                           cache_dir=cache))])
    season = scene.get("season_name") or (SEASONS.get(scene.get("season"), {})
                                          .get("name", ""))
    usage = scene.get("usage_name") or "场景"
    _hud(canvas, f"{scene['name']} · {season}",
         f"{usage}｜素材集仿真测试 {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, quality=90)
    try:
        rel = out_path.resolve().relative_to(settings.data_dir_path.resolve())
        url = f"/data/{rel.as_posix()}"
    except ValueError:
        url = f"/data/{out_path.name}"
    return {"scene": scene["name"], "season": scene.get("season"),
            "season_name": season, "url": url, "path": str(out_path),
            "notes": notes,
            "roster": {
                "characters": [c["name"] + (f"({c.get('gender_name')})"
                                            if c.get("gender_name") else "")
                               for c in characters],
                "pets": [p["name"] for p in pets],
                "monsters": [f"{m['name']}·{m.get('tier') or '杂兵'}"
                             for m in monsters],
                "buildings": [b["name"] for b in buildings],
                "weapons": [w["name"] for w in weapons],
            }}


# ============================================================
#  汇总入口
# ============================================================

def run(s: dict) -> dict:
    """整集仿真：逐张体检 + 按场景分组合成模拟画面。同步执行，秒级。"""
    assets = s.get("assets") or []
    by = {k: [a for a in assets if a["kind"] == k] for k in
          ("scene", "character", "pet", "weapon", "building", "monster")}

    asset_checks = [check_asset(s, a) for a in assets]

    out_dir = settings.data_dir_path / "outputs" / "gameassets" / "sim" / s["id"]
    scenes = sorted(by["scene"], key=lambda a: (a.get("season") or ""))
    # 没配属场景的怪归入第一组（并在 notes 里说明），别让它们凭空消失
    anchored = {m.get("scene") for m in by["monster"] if m.get("scene")}
    unanchored = [m for m in by["monster"] if not m.get("scene")]

    groups: list[dict] = []
    for i, sc in enumerate(scenes):
        monsters = ([m for m in by["monster"] if m.get("scene") == sc["name"]]
                    + (unanchored if i == 0 else []))
        notes = (["以下野怪未在方案里配属场景，暂入本组："
                  + "、".join(m["name"] for m in unanchored)]
                 if i == 0 and unanchored else [])
        if scenes and i == 0 and not by["character"]:
            notes.append("整套素材没有主角——组不成可玩画面，建议方案里加角色")
        g = compose_group(s, sc, monsters, by["character"], by["pet"],
                          by["building"], by["weapon"],
                          out_path=out_dir /
                          f"sim_{i}_{sc['name']}_{sc.get('season') or 'na'}.png")
        g["notes"] = (notes + g["notes"])[:6]
        groups.append(g)

    n_fail = sum(1 for c in asset_checks if not c["ok"])
    n_warn = sum(1 for g in groups if g["notes"])
    report = {
        "created_at": store.now_iso(),
        "canvas": f"{CANVAS[0]}×{CANVAS[1]}",
        "groups": groups,
        "asset_checks": asset_checks,
        "summary": {"total": len(asset_checks), "fail": n_fail,
                    "pass": len(asset_checks) - n_fail, "warn": n_warn,
                    "groups": len(groups)},
    }

    def _apply(t: dict) -> None:
        t["simulation"] = report
    store.mutate(s["id"], _apply)
    return report
