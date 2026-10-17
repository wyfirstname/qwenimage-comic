# -*- coding: utf-8 -*-
"""镜头与首帧提示词拼装。

一条镜头运动，两种表达
----------------------
`CAMERA_MOVES` 里每条同时带 `kenburns`（ffmpeg zoompan 参数）和 `motion`
（给视频模型的自然语言运动描述）。这样：

    现在（16G 卡，无视频模型）  → 用 kenburns 做推拉/平移
    将来（24G 卡，接 LongCat）→ 用 motion 做真运动，把 kenburns 那一步换掉

前端、任务流、分镜数据一行都不用改。这是把「渲染方式」与「镜头意图」解耦的
全部目的——分镜表里存的是意图（push_in），不是实现（zoompan 表达式）。

两条通道的负面提示词待遇不同（踩过的坑，别再踩）
------------------------------------------------
* 文生图：guidance_scale > 1，负面**真的生效** → 禁止项写 negative
* fusion 编辑（TextEncodeQwenImage21）：CFG 固定 1.0，负面**完全不生效**
  → 所有「不要什么」必须强制写进正向提示词，写成「保持……不变」这种正向句式，
    而不是「不要改变……」

同一个 `shot_prompt` 通过 fusion 参数切换，调用方不用记这两套规则。
"""

from __future__ import annotations

from typing import Optional

from shared.promptlib.styles import negative_of, style_of


# 景别：从紧到松。顺序本身有用——分镜排布时相邻两格不宜连续同景别。
SHOT_SIZES: list[str] = ["特写", "近景", "中景", "全景", "远景"]
SHOT_HINT = {
    "特写": "只拍到头部与肩部，面部占画面主要面积",
    "近景": "拍到胸部以上，能看清表情与手势",
    "中景": "拍到腰部或膝盖，兼顾动作与环境",
    "全景": "完整拍到主体全身，人物占画面高度约三分之二",
    "远景": "主体在画面中较小，环境占主导",
}

CAMERA_MOVES: dict[str, dict] = {
    "push_in": {
        "label": "缓推",
        "motion": "镜头缓慢向前推进，画面轻微放大，注意力收向主体",
        # z 从 1.0 线性到 1+delta；x/y 锁中心
        "z": "1+{delta}*on/{d}",
        "x": "iw/2-(iw/zoom/2)",
        "y": "ih/2-(ih/zoom/2)",
    },
    "pull_out": {
        "label": "缓拉",
        "motion": "镜头缓慢向后拉开，画面逐渐露出更多环境",
        "z": "1+{delta}*(1-on/{d})",
        "x": "iw/2-(iw/zoom/2)",
        "y": "ih/2-(ih/zoom/2)",
    },
    "pan_left": {
        "label": "左摇",
        "motion": "镜头向左平移，画面内容向右滑过",
        "z": "1.12",
        "x": "(iw-iw/zoom)*(1-on/{d})",
        "y": "ih/2-(ih/zoom/2)",
    },
    "pan_right": {
        "label": "右摇",
        "motion": "镜头向右平移，画面内容向左滑过",
        "z": "1.12",
        "x": "(iw-iw/zoom)*on/{d}",
        "y": "ih/2-(ih/zoom/2)",
    },
    "tilt_up": {
        "label": "上摇",
        "motion": "镜头缓缓上摇，从下往上带出主体",
        "z": "1.12",
        "x": "iw/2-(iw/zoom/2)",
        "y": "(ih-ih/zoom)*(1-on/{d})",
    },
    "tilt_down": {
        "label": "下摇",
        "motion": "镜头缓缓下摇，从上往下落到主体",
        "z": "1.12",
        "x": "iw/2-(iw/zoom/2)",
        "y": "(ih-ih/zoom)*on/{d}",
    },
    # still 不做完全静止：纯静止帧在成片里看起来像卡住了。
    # 给 3% 的极缓推，肉眼几乎看不出位移，但画面是「活」的。
    "still": {
        "label": "定格",
        "motion": "镜头基本固定，只有极轻微的呼吸感浮动",
        "z": "1+0.03*on/{d}",
        "x": "iw/2-(iw/zoom/2)",
        "y": "ih/2-(ih/zoom/2)",
    },
}

DEFAULT_MOVE = "push_in"


def move_of(key: Optional[str]) -> dict:
    return CAMERA_MOVES.get(str(key or ""), CAMERA_MOVES[DEFAULT_MOVE])


def move_key(key: Optional[str]) -> str:
    k = str(key or "")
    return k if k in CAMERA_MOVES else DEFAULT_MOVE


def shot_size_of(value: Optional[str]) -> str:
    v = str(value or "")
    return v if v in SHOT_SIZES else "中景"


# ============================================================
#  提示词拼装
# ============================================================

# 画风锁段落：同一部片里逐字复现，是跨格一致性的第一道锁
def _style_block(style_id: Optional[str]) -> str:
    s = style_of(style_id)
    return (
        f"画风：{s['render']}；"
        f"线条：{s['line']}；"
        f"色调：{s['palette']}；"
        f"人物比例：{s['ratio']}，"
        f"比例与头部大小必须与整部片其他镜头完全一致。"
    )


def cast_prompt(style_id: Optional[str], subject: str = "",
                outfit: str = "") -> dict:
    """定妆照：整部片的「角色身份证」。

    为什么要有这一步
    ----------------
    纯靠文字描述锁角色，每一格都会画成不同的人。所以先单独出一张满身、
    纯背景、姿态中性的定妆照，后面所有格都用 fusion 通道以它为 image_1。
    这和团子衣橱的做法是同一个原理：**角色是资产，不是提示词**。

    姿态为什么要外张
    ----------------
    双臂贴紧躯干会让后续编辑无从判断手臂与身体的边界，模型只能猜，猜错就是
    「手臂融进衣服里」。双臂外张 15°、双脚分开，躯干轮廓始终可见。
    """
    s = style_of(style_id)
    body = (subject or "").strip() or s["subject"]
    extra = f"服装：{outfit.strip()}。" if (outfit or "").strip() else ""
    prompt = (
        f"一张角色设定图。主体：{body}。{extra}"
        f"{_style_block(style_id)}"
        "构图：全身正面站姿，双臂自然下垂并向外张开约 15 度，双脚分开与肩同宽，"
        "视线看向镜头，表情平静中性。"
        "背景：单一纯色背景（浅奶油色），不出现任何道具、地面阴影与环境元素。"
        "规格：竖屏 9:16 单幅图像，角色完整不被画面边缘裁切，边缘干净。"
    )
    return {"prompt": prompt, "negative": negative_of(style_id), "kind": "cast"}


def shot_prompt(*, style_id: Optional[str], action: str, shot: str = "中景",
                camera: str = DEFAULT_MOVE, scene: str = "",
                subject: str = "", lock: str = "", fusion: bool = False,
                cast_ref: bool = True) -> dict:
    """单格首帧提示词。

    fusion=True  走编辑通道（有定妆照）：这是默认路径，角色锁交给 image_1
    fusion=False 走文生图通道（无定妆照）：角色锁退回文本，稳定性明显更差
    """
    s = style_of(style_id)
    size = shot_size_of(shot)
    move = move_of(camera)
    act = (action or "").strip() or "安静地站着，看着镜头"
    scn = (scene or "").strip()
    who = (lock or "").strip() or (subject or "").strip() or s["subject"]

    parts: list[str] = []

    # ① 画面规格
    if fusion and cast_ref:
        # 融合通道的输出尺寸由 image_1 决定（latent 来自 TextEncodeQwenImage21），
        # 所以这里不能另说一个比例，否则模型会在固定画幅里硬塞一个"新"构图。
        parts.append("单幅图像，画幅与 image_1 完全一致（同尺寸、同比例），动画成片中的一个镜头画面。")
    else:
        parts.append("竖屏 9:16 单幅图像，动画成片中的一个镜头画面。")

    # ② 场景
    if scn:
        parts.append(f"场景：{scn}。")

    # ③ 角色锁
    if fusion and cast_ref:
        # 写成「编辑指令」而不是「参考描述」：Qwen-Image-Edit 是按指令改图的模型，
        # 说「参考这张图画个新的」它会重画一个人；说「只改这几项、别动角色」
        # 它才会真的保留。这是融合能不能锁住角色的分水岭。
        parts.append(
            "编辑要求：image_1 是这位角色的标准设定图，请把它改画成下面这个镜头，"
            "角色本体原样保留——脸型、五官比例、眼睛颜色、发型、发色、肤色、"
            "头身比、服装与配饰与 image_1 完全一致，看上去就是同一个人在同一天、"
            "换了个位置和姿势被拍到。"
            "允许改变的只有三样：背景环境、身体姿势与朝向、取景范围（景别）。"
            "景别变化是对同一个角色重新取景，不是重新设计一个角色。"
        )
    else:
        parts.append(f"角色：{who}，保持整部片统一的造型与比例。")
        parts.append(f"角色比例：{s['ratio']}。")

    # ④ 动作
    parts.append(f"动作：{act}。")

    # ⑤ 构图与镜头
    parts.append(f"构图：{size}，{SHOT_HINT[size]}。{move['motion']}。")

    # ⑥ 光线与色调
    parts.append(_style_block(style_id))
    if scn:
        parts.append("光线遵循场景本身的自然光源，明暗过渡柔和。")

    # ⑦ 禁令（按通道分开写）
    if fusion and cast_ref:
        # CFG=1，负面不生效 → 必须写成正向的「保持不变」
        parts.append(
            "同一个人：不把角色画成另一个人、不换脸、不换发型发色、不换服装、"
            "不把角色画成正常人体比例、不拉长四肢、不增加第二个角色；"
            "画面中不出现任何文字、字幕、水印、边框、拼图或分屏；"
            "不使用写实照片质感与真实皮肤纹理。"
        )
        negative = ""
    else:
        negative = negative_of(style_id)

    return {
        "prompt": "".join(parts),
        "negative": negative,
        "kind": "shot",
        "meta": {"shot": size, "camera": move_key(camera),
                 "fusion": bool(fusion and cast_ref)},
    }


def shot_options() -> dict:
    """给前端的镜头选项。"""
    return {
        "shot_sizes": [{"id": k, "label": k, "hint": SHOT_HINT[k]} for k in SHOT_SIZES],
        "cameras": [{"id": k, "label": v["label"], "motion": v["motion"]}
                    for k, v in CAMERA_MOVES.items()],
    }
