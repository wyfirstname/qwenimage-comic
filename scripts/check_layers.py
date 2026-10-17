# -*- coding: utf-8 -*-
"""层级铁律检查（分层重构的机械保障）。

规则：
  1. shared/     不许 import apps / engine / portal
  2. engine/     不许 import apps / portal
  3. apps/       只许 import engine.sdk，不许 import engine.infer / engine.generation
                 也不许 import 其它应用（apps.<other>）
  4. portal/     不许 import engine.infer（要走 engine.sdk）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path("E:/AIGC/qwenimages")
IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w\.]*)", re.M)

RULES = {
    "shared": [("apps.", None), ("engine.", None), ("portal.", None)],
    "engine": [("apps.", None), ("portal.", None)],
    "apps": [
        ("engine.infer", "应用只许走 engine.sdk"),
        ("engine.generation", "应用只许走 engine.sdk"),
        ("engine.bootstrap", "应用只许走 engine.sdk"),
        ("engine.serve", "应用只许走 engine.sdk"),
    ],
    "portal": [("engine.infer", "门户要走 engine.sdk")],
}

violations: list[str] = []
for layer, rules in RULES.items():
    for py in (ROOT / layer).rglob("*.py"):
        rel = py.relative_to(ROOT).as_posix()
        if "__pycache__" in rel:
            continue
        src = py.read_text(encoding="utf-8")
        for m in IMPORT_RE.finditer(src):
            mod = m.group(1)
            for bad, why in rules:
                if mod == bad.rstrip(".") or mod.startswith(bad):
                    violations.append(f"{rel}: import {mod}  <- {why or '越层依赖'}")

# apps 之间零引用
app_ids = [d.name for d in (ROOT / "apps").iterdir() if d.is_dir()]
for app_id in app_ids:
    for py in (ROOT / "apps" / app_id).rglob("*.py"):
        rel = py.relative_to(ROOT).as_posix()
        src = py.read_text(encoding="utf-8")
        for other in app_ids:
            if other == app_id:
                continue
            for m in IMPORT_RE.finditer(src):
                mod = m.group(1)
                if mod == f"apps.{other}" or mod.startswith(f"apps.{other}."):
                    violations.append(f"{rel}: import {mod}  <- 应用之间不许互相引用")

if violations:
    print(f"发现 {len(violations)} 处越层依赖：")
    for v in sorted(set(violations)):
        print("  ", v)
    sys.exit(1)
print("层级检查通过：shared/engine/apps/portal 依赖方向全部合规")
