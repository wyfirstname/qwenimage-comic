# -*- coding: utf-8 -*-
"""姿态骨架（OpenPose 18 点）——给 ControlNet 用的「姿势地图」。

为什么自己画骨架，而不是在提示词里写姿势：
    Qwen-Image 的融合编辑（多参考图 + denoise=1）会牢牢抓住参考图画布的构图，
    "walking, left foot forward" 只是软约束。实测 8 帧走完，64 格里几乎还是
    同一个站姿——提示词根本没在管姿势。ControlNet 的 Pose 模式读的是像素：
    有骨架才真的改姿势。

为什么用程序画、不用 dwpose 预处理器：
    · 我们的姿势本来就是已知的（第几帧、哪个朝向），不需要从照片里猜；
    · 不用多装一个姿态估计模型，也就不多占一份显存（16G 卡上这是硬指标）；
    · 同一个朝向里 4 个关键姿态的关节角不同 → 走/跑循环被「钉」出来；
      换朝向只改 yaw → 侧面帧是真侧面，而不是正面的近亲。

坐标系：局部空间 x=角色右手方向、y=向上、z=角色正面朝向；先按朝向 yaw 旋转，
再按相机俯角做一次正交投影，最后按「脚底贴地 + 高度占画幅 86%」缩放。
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

# ------------------------------------------------------------
#  OpenPose 18 关键点（顺序 = 官方 body_25 之外的 COCO 18 点）
# ------------------------------------------------------------
JOINTS = [
    "nose", "neck",
    "r_shoulder", "r_elbow", "r_wrist",
    "l_shoulder", "l_elbow", "l_wrist",
    "r_hip", "r_knee", "r_ankle",
    "l_hip", "l_knee", "l_ankle",
    "r_eye", "l_eye", "r_ear", "l_ear",
]
# 官方 limbSeq（1 基）→ 0 基
LIMBS = [
    (1, 2), (1, 5), (2, 3), (3, 4), (5, 6), (6, 7),
    (1, 8), (8, 9), (9, 10), (1, 11), (11, 12), (12, 13),
    (1, 0), (0, 14), (14, 16), (0, 15), (15, 17),
]
LIMB_COLORS = [
    (255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0),
    (85, 255, 0), (0, 255, 0), (0, 255, 85), (0, 255, 170), (0, 255, 255),
    (0, 170, 255), (0, 85, 255), (0, 0, 255), (85, 0, 255), (170, 0, 255),
    (255, 0, 255), (255, 0, 170),
]
JOINT_COLORS = [
    (255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0),
    (85, 255, 0), (0, 255, 0), (0, 255, 85), (0, 255, 170), (0, 255, 255),
    (0, 170, 255), (0, 85, 255), (0, 0, 255), (85, 0, 255), (170, 0, 255),
    (255, 0, 255), (255, 0, 170), (255, 0, 85),
]

# 朝向 → 绕竖轴的偏转角（0=正对镜头，90=面朝画面右侧）
DIR_YAW: dict[str, float] = {
    "s": 0.0, "se": 45.0, "e": 90.0, "ne": 135.0,
    "n": 180.0, "nw": 225.0, "w": 270.0, "sw": 315.0,
}

# 相机俯角。这个数字直接决定「正面/背面那几向的步态读不读得出来」：
# 相机是 2.5D 俯视时，腿往前迈是**朝镜头方向**动，在画面上表现为纵向位移，
# 幅度 ≈ sin(俯角) × 步幅。10° 时摆动腿只位移 16px（和站着几乎一样，正是
# 用户那张表里「64 格都站着」的观感来源之一），28° 才会变成 50px。
# 但太大又会让身体压扁、脑袋盖住胸口，20° 是「台阶看得清、身体不变形」的折中。
CAM_PITCH = 20.0

# 卡通比例（单位 = 身高 1.0）：约 3.5 头身，腿短、头大，和 VIEW_LOCK 的口径一致
HEAD_TOP = 1.0
NECK_Y = 0.665
HIP_Y = 0.44
SHOULDER_X = 0.085
HIP_X = 0.048
TORSO = NECK_Y - HIP_Y
UPPER_ARM = 0.105
FOREARM = 0.098
THIGH = 0.198
SHIN = 0.192
HEAD_RISE = 0.115
HEAD_R = 0.125              # 头半径（卡通比例：约 3.5 头身）
FOOT_MIN_Y = 0.045          # 脚踝离地（脚掌厚度）
FILL = 0.86                 # 身高占画幅比例
FILL_W = 0.92               # 横向占画幅比例（竖长画布上按宽度兜底，别把迈开的腿切了）


# ------------------------------------------------------------
#  关节角：正 = 向前（角色面朝方向），负 = 向后
#  clip/帧号 → {髋/膝/肩/肘/前倾}，左右两侧分开写，镜像帧用 _mirror()
# ------------------------------------------------------------
_NEUTRAL = {
    "hip_l": 0.0, "knee_l": 4.0, "hip_r": 0.0, "knee_r": 4.0,
    "sho_l": 2.0, "elb_l": 10.0, "sho_r": 2.0, "elb_r": 10.0,
    "lean": 0.0, "bob": 0.0, "out_l": -13.0, "out_r": 13.0,
}


def _mirror(a: dict) -> dict:
    """左右互换（步态的第 5~8 帧就是第 1~4 帧的镜像）。"""
    out = dict(a)
    for k in ("hip", "knee", "sho", "elb", "out"):
        out[f"{k}_l"], out[f"{k}_r"] = a.get(f"{k}_r", 0.0), a.get(f"{k}_l", 0.0)
    return out


def _walk_key(idx: int) -> dict:
    """行走八帧的关节角。KEY_POSES 只出 0/2/4/6，其余帧靠整帧位移补。"""
    contact = {**_NEUTRAL, "hip_l": 26.0, "knee_l": 6.0, "hip_r": -22.0, "knee_r": 16.0,
               "sho_l": -16.0, "elb_l": 14.0, "sho_r": 18.0, "elb_r": 22.0,
               "lean": 3.0, "bob": 0.0}
    passing = {**_NEUTRAL, "hip_l": -8.0, "knee_l": 10.0, "hip_r": 6.0, "knee_r": 52.0,
               "sho_l": -4.0, "elb_l": 12.0, "sho_r": 6.0, "elb_r": 14.0,
               "lean": 2.0, "bob": 0.02}
    settle = {**_NEUTRAL, "hip_l": 18.0, "knee_l": 20.0, "hip_r": -14.0, "knee_r": 22.0,
              "sho_l": -12.0, "elb_l": 14.0, "sho_r": 12.0, "elb_r": 20.0,
              "lean": 2.5, "bob": -0.012}
    swing = {**_NEUTRAL, "hip_l": 2.0, "knee_l": 6.0, "hip_r": 14.0, "knee_r": 34.0,
             "sho_l": -2.0, "elb_l": 12.0, "sho_r": 2.0, "elb_r": 14.0,
             "lean": 2.0, "bob": 0.014}
    table = [contact, settle, passing, swing]
    return table[idx % 8] if idx < 4 else _mirror(table[idx % 4])


def _run_key(idx: int) -> dict:
    """奔跑八帧：触地 / 吸收 / 蹬伸 / 滞空。"""
    strike = {**_NEUTRAL, "hip_l": 38.0, "knee_l": 18.0, "hip_r": -28.0, "knee_r": 58.0,
              "sho_l": -36.0, "elb_l": 58.0, "sho_r": 40.0, "elb_r": 68.0,
              "lean": 13.0, "bob": -0.01}
    absorb = {**_NEUTRAL, "hip_l": 24.0, "knee_l": 46.0, "hip_r": -10.0, "knee_r": 72.0,
              "sho_l": -28.0, "elb_l": 56.0, "sho_r": 32.0, "elb_r": 66.0,
              "lean": 15.0, "bob": -0.03}
    push = {**_NEUTRAL, "hip_l": -24.0, "knee_l": 12.0, "hip_r": 32.0, "knee_r": 78.0,
            "sho_l": 18.0, "elb_l": 56.0, "sho_r": -18.0, "elb_r": 48.0,
            "lean": 16.0, "bob": 0.01}
    air = {**_NEUTRAL, "hip_l": -30.0, "knee_l": 64.0, "hip_r": 44.0, "knee_r": 40.0,
           "sho_l": 26.0, "elb_l": 62.0, "sho_r": -26.0, "elb_r": 54.0,
           "lean": 10.0, "bob": 0.05}
    table = [strike, absorb, push, air]
    return table[idx % 8] if idx < 4 else _mirror(table[idx % 4])


def _idle_key(idx: int) -> dict:
    """站立：只有呼吸与重心微动，别把「站立」画成体操。"""
    base = dict(_NEUTRAL)
    table = [
        base,
        {**base, "sho_l": 3.5, "sho_r": 3.5, "lean": -0.8, "bob": 0.006},
        {**base, "sho_l": 1.0, "sho_r": 1.0, "lean": 0.6, "bob": -0.004},
        {**base, "hip_l": 2.0, "hip_r": -2.0, "lean": 0.4, "bob": 0.0},
    ]
    return table[idx % 8 if idx < 4 else idx % 4]


def angles(clip: str, idx: int) -> dict:
    """该 clip 第 idx 帧的关节角。未知 clip 退化成站立。"""
    if clip == "walk":
        return _walk_key(idx)
    if clip == "run":
        return _run_key(idx)
    return _idle_key(idx)


# ------------------------------------------------------------
#  正向运动学：局部三维骨架
# ------------------------------------------------------------
def _rot(v: tuple[float, float, float], axis: str, deg: float):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    x, y, z = v
    if axis == "x":
        return (x, y * c - z * s, y * s + z * c)
    if axis == "y":
        return (x * c + z * s, y, -x * s + z * c)
    return (x * c - y * s, x * s + y * c, z)


def _add(p, v, k: float = 1.0):
    return (p[0] + v[0] * k, p[1] + v[1] * k, p[2] + v[2] * k)


def _chain(root, direction, length: float, swing: float, out: float = 0.0):
    """从 root 沿「向下」方向摆 swing（正=向前）/ out（正=向外）度，走 length。"""
    d = _rot((0.0, -1.0, 0.0), "x", -swing)
    if out:
        d = _rot(d, "z", out)
    return _add(root, d, length), d


def skeleton(clip: str, idx: int) -> dict[str, tuple[float, float, float]]:
    """局部空间骨架（未旋转、未投影）。脚底已贴到 FOOT_MIN_Y 这条地平线。"""
    a = angles(clip, idx)
    pts: dict[str, tuple[float, float, float]] = {}

    hip_c = (0.0, HIP_Y + a.get("bob", 0.0), 0.0)
    up = _rot((0.0, 1.0, 0.0), "x", a.get("lean", 0.0))
    neck = _add(hip_c, up, TORSO)
    head_c = _add(neck, up, HEAD_RISE)
    pts["neck"] = neck
    # 头部：五点撑开「头有多大」——18 点本身没有头，光靠鼻/眼/耳会画成一个帽子。
    # 这里额外给一个很淡的头球（见 render()），让 ControlNet 知道脑袋占多高。
    pts["_head"] = head_c
    pts["_head_top"] = _add(head_c, (0.0, HEAD_R, 0.0))   # 参与取景，不画成关节点
    pts["nose"] = _add(head_c, (0.0, -0.015, 0.098))
    pts["r_eye"] = _add(head_c, (0.040, 0.042, 0.086))
    pts["l_eye"] = _add(head_c, (-0.040, 0.042, 0.086))
    pts["r_ear"] = _add(head_c, (0.100, 0.004, -0.006))
    pts["l_ear"] = _add(head_c, (-0.100, 0.004, -0.006))

    for side, sx in (("r", 1.0), ("l", -1.0)):
        sho = _add(neck, (SHOULDER_X * sx, -0.022, 0.0))
        hip = _add(hip_c, (HIP_X * sx, 0.0, 0.0))
        pts[f"{side}_shoulder"] = sho
        pts[f"{side}_hip"] = hip

        elbow, adir = _chain(sho, None, UPPER_ARM, a.get(f"sho_{side}", 0.0),
                             a.get(f"out_{side}", 0.0))
        # 肘：前臂在前臂基础上再弯（正=更向前）
        fdir = _rot(adir, "x", -a.get(f"elb_{side}", 0.0))
        wrist = _add(elbow, fdir, FOREARM)
        pts[f"{side}_elbow"] = elbow
        pts[f"{side}_wrist"] = wrist

        knee, tdir = _chain(hip, None, THIGH, a.get(f"hip_{side}", 0.0), 3.0 * sx)
        # 膝：小腿向后弯（正=脚跟后收）
        sdir = _rot(tdir, "x", a.get(f"knee_{side}", 0.0))
        ankle = _add(knee, sdir, SHIN)
        pts[f"{side}_knee"] = knee
        pts[f"{side}_ankle"] = ankle
        # 脚尖：脚掌与前臂一样，按髋-膝的净角度近似朝向
        foot = _rot((0.0, 0.0, 1.0), "x", a.get(f"hip_{side}", 0.0) - a.get(f"knee_{side}", 0.0))
        pts[f"{side}_toe"] = _add(ankle, (foot[0], foot[1] * 0.25, foot[2]), 0.082)

    # 贴地：最低的脚踝落到地平线，另一只脚该抬多高就多高
    lowest = min(pts["r_ankle"][1], pts["l_ankle"][1])
    dy = FOOT_MIN_Y - lowest
    return {k: (v[0], v[1] + dy, v[2]) for k, v in pts.items()}


def _flatten(pts: dict[str, tuple[float, float, float]], dir_id: str):
    """局部三维 → 相机平面（x 横向、down 向下）。朝向 yaw + 相机俯角都在这里。"""
    yaw = math.radians(DIR_YAW.get(dir_id, 0.0))
    ca, sa = math.cos(yaw), math.sin(yaw)
    pitch = math.radians(CAM_PITCH)
    cp, sp = math.cos(pitch), math.sin(pitch)
    out = {}
    for k, (x, y, z) in pts.items():
        cam_x = -x * ca + z * sa          # 角色右手在 yaw=0 时落在画面左侧
        cam_z = x * sa + z * ca           # 越靠镜头越「低」→ 俯视时有纵向层次
        out[k] = (cam_x, -y * cp + cam_z * sp)
    return out


@lru_cache(maxsize=256)
def _fit(dir_id: str, clip: str, size: tuple[int, int]) -> tuple[float, float]:
    """整段 clip 的统一取景：(缩放, 横向中心)。

    为什么按「整段 8 帧的并集」而不是逐帧算：逐帧各自铺满画幅，角色会一胀一缩
    （走路像在呼吸膨胀）；而只按高度铺满，竖长画布上侧向奔跑会把迈开的腿切掉
    （320×640 实测直接顶到左右边）。所以两个方向都兜底，且整段共用一套比例。
    """
    xs: list[float] = []
    downs: list[float] = []
    for i in range(8):
        for x, d in _flatten(skeleton(clip, i), dir_id).values():
            xs.append(x)
            downs.append(d)
    hspan = max(1e-6, max(xs) - min(xs))
    vspan = max(1e-6, max(downs) - min(downs))
    w, h = size
    scale = min(FILL * h / vspan, FILL_W * w / hspan)
    return scale, (max(xs) + min(xs)) / 2.0


def project(pts: dict[str, tuple[float, float, float]], dir_id: str,
            clip: str, size: tuple[int, int]) -> tuple[dict[str, tuple[float, float]], int]:
    """局部骨架 → 画布像素坐标（含朝向 yaw、相机俯角、脚底锚点）。"""
    w, h = size
    scale, cx = _fit(dir_id, clip, size)
    flat = _flatten(pts, dir_id)
    bottom = max(v[1] for v in flat.values())
    base_y = 0.94 * h                     # 脚底锚点：留 6% 给脚掌与阴影
    out = {k: (w / 2 + (v[0] - cx) * scale, base_y - (bottom - v[1]) * scale)
           for k, v in flat.items()}
    return out, int(round(scale))


def render(dir_id: str, clip: str, idx: int,
           size: tuple[int, int] = (384, 384)) -> Image.Image:
    """画一张 OpenPose 控制图（黑底、彩色骨架，尺寸 = size）。"""
    pts, scale = project(skeleton(clip, idx), dir_id, clip, size)
    img = Image.new("RGB", size, (0, 0, 0))
    draw = ImageDraw.Draw(img)

    stick = max(4, round(size[1] * 0.021))
    dot = max(3, round(size[1] * 0.010))

    # 先画肢体，再画关节点：关节点盖在肢体上，颜色边界干净（和 OpenPose 输出一致）
    for (i, j), color in zip(LIMBS, LIMB_COLORS):
        a, b = JOINTS[i], JOINTS[j]
        draw.line([pts[a], pts[b]], fill=color, width=stick)
    # 脚尖：官方 18 点没有脚，但「脚朝哪」是行走帧最关键的读图信息
    for side, color in (("r", (255, 0, 85)), ("l", (85, 0, 255))):
        draw.line([pts[f"{side}_ankle"], pts[f"{side}_toe"]], fill=color, width=stick)
    for name, color in zip(JOINTS, JOINT_COLORS):
        x, y = pts[name]
        draw.ellipse([x - dot, y - dot, x + dot, y + dot], fill=color)
    # 头球：淡灰细圈。只是给比例参照，不参与骨架连线，强度远低于肢体色块
    head = pts.get("_head")
    if head:
        r = HEAD_R * scale
        draw.ellipse([head[0] - r, head[1] - r, head[0] + r, head[1] + r],
                     outline=(110, 110, 110), width=max(2, stick // 3))
    return img


def pose_map(out_path: str | Path, dir_id: str, clip: str, idx: int,
             size: tuple[int, int] = (384, 384)) -> Path:
    """落盘一张控制图并返回路径（同参数重复调用是幂等的）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    render(dir_id, clip, idx, size).save(p)
    return p


def contact_sheet(out_path: str | Path, size: tuple[int, int] = (192, 192),
                  dirs: list[str] | None = None,
                  cells: list[tuple[str, int]] | None = None) -> Path:
    """调试 / 文档用：朝向 × 关键姿态的对照表（一眼看出侧背向是否真的转了）。"""
    dirs = dirs or ["s", "se", "e", "ne", "n", "nw", "w", "sw"]
    cells = cells or [("idle", 0), ("walk", 0), ("walk", 2), ("run", 0), ("run", 2)]
    w, h = size
    sheet = Image.new("RGB", (w * len(cells), h * len(dirs)), (0, 0, 0))
    for r, d in enumerate(dirs):
        for c, (clip, idx) in enumerate(cells):
            sheet.paste(render(d, clip, idx, size), (c * w, r * h))
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(p)
    return p


if __name__ == "__main__":                      # pragma: no cover - 手工检查用
    import sys

    dst = Path(sys.argv[1] if len(sys.argv) > 1 else "data/_pose_sheet.png")
    print(contact_sheet(dst))
