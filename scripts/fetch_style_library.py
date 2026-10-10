# -*- coding: utf-8 -*-
"""抓取 handraw-style 的画风缩略图（一次性；缩略图不进 git）。

为什么要单独抓：
  327 张 webp 约 4~6MB，塞进主仓库不划算，所以 .gitignore 掉 thumbs/，
  用本脚本一次性抓取 + 校验，绿色包构建时(python build_portable.py)一并打进包。

用法：
    python scripts/fetch_style_library.py            # 抓画风缩略图（默认）
    python scripts/fetch_style_library.py --colors   # 顺便抓主题色卡
    python scripts/fetch_style_library.py --check    # 只校验已有文件完整性
    python scripts/fetch_style_library.py --force    # 覆盖已存在的图

图片源：优先 jsDelivr CDN（国内可访问、直镜像仓库），失败回退 raw.githubusercontent。
注意：图片**不参与**运行，缺失时界面自动降级为纯文字画风网格。
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "backend" / "app" / "style_library" / "data"
THUMBS_DIR = ROOT / "backend" / "app" / "style_library" / "thumbs"

REPO = "yang0/handraw-style"
BRANCH = "master"   # 该仓库默认分支是 master（不是 main）
# jsDelivr 优先（国内可访问），raw 兜底
SOURCES = (
    f"https://cdn.jsdelivr.net/gh/{REPO}@{BRANCH}/",
    f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/",
)


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except Exception:
        pass
    return ctx


def _download(path_in_repo: str, timeout: int = 40) -> bytes | None:
    ctx = _ssl_ctx()
    for base in SOURCES:
        url = base + path_in_repo
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "qwenimages-fetch/1.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                data = r.read()
            if data and len(data) > 64:
                return data
        except Exception as exc:  # noqa: BLE001
            print(f"    [{base.split('/')[2]}] {exc}")
    return None


def _style_image_paths() -> list[tuple[str, str]]:
    """返回 [(key, repo 内相对路径), ...]。

    画风图在 `images/individual/<组代码>/<KEY>.webp`，
    组代码 = styles.json 里 group 的第一个空格前 token（FA / FB / ...）。
    """
    styles = json.loads((DATA_DIR / "styles.json").read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for s in styles:
        num = str(s.get("number") or "").strip()
        grp = str(s.get("group") or "").strip()
        if not num:
            continue
        code = grp.split(" ")[0] if grp else num.split("-")[0]
        out.append((num, f"images/individual/{code}/{num}.webp"))
    return out


def _color_image_paths() -> list[tuple[str, str]]:
    colors = json.loads((DATA_DIR / "colors.json").read_text(encoding="utf-8"))
    return [(c["id"], f"images/colors/{c['id']}.webp") for c in colors if c.get("id")]


def fetch(items: list[tuple[str, str]], force: bool, label: str) -> dict:
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    ok = skip = fail = 0
    failures: list[str] = []
    for i, (key, rel) in enumerate(items, 1):
        dest = THUMBS_DIR / f"{key}.webp"
        if dest.exists() and not force:
            skip += 1
            continue
        print(f"[{i}/{len(items)}] {label} {key}")
        data = _download(rel)
        if not data:
            fail += 1
            failures.append(key)
            continue
        dest.write_bytes(data)
        ok += 1
    print(f"\n{label} 完成：新增 {ok}，跳过 {skip}，失败 {fail}")
    if failures:
        print("失败清单：", ", ".join(failures))
    return {"ok": ok, "skip": skip, "fail": fail, "failures": failures}


def check(items: list[tuple[str, str]]) -> int:
    missing = [k for k, _ in items if not (THUMBS_DIR / f"{k}.webp").exists()]
    print(f"应有 {len(items)} 张，缺失 {len(missing)} 张")
    if missing:
        print("缺失（前 40）：", ", ".join(missing[:40]))
    return len(missing)


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取 handraw-style 画风缩略图")
    ap.add_argument("--colors", action="store_true", help="顺便抓主题色卡")
    ap.add_argument("--check", action="store_true", help="只校验，不下载")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的图")
    args = ap.parse_args()

    styles = _style_image_paths()
    if args.check:
        n = check(styles)
        if args.colors:
            n += check(_color_image_paths())
        return 1 if n else 0

    res = fetch(styles, force=args.force, label="画风")
    if args.colors:
        fetch(_color_image_paths(), force=args.force, label="主题色")
    return 0 if res["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
