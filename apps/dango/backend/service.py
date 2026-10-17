# -*- coding: utf-8 -*-
"""团子衣橱业务层：Q 版角色的「捏 → 做衣服 → 换装」全部提示词与出图参数。

这一层只做三件事，不碰任何模型权重：

1. **把用户点选的东西变成提示词**（本文件 80% 的代码就是这些模板）；
2. **把提示词变成引擎认识的 payload**；
3. **决定走哪条推理通道**。

三条通道（都在 engine 层已存在，这里只是正确使用）
------------------------------------------------------
* 文生图（捏团子 / 做衣服）：`mode=txt2img`，走 CLIPTextEncode + EmptySD3LatentImage，
  **CFG 生效、负面提示词真的有效**——所以「不要写实人体」这类约束写负面里最稳。
* 多参考图换装：`mode=img2img` + `fusion=True`，走官方的 `TextEncodeQwenImage21`：
  参考图同时进 Qwen3-VL 视觉塔与 VAE latent，模型是真的「看着 image_1 和 image_2 重画」。
  该通道**固定 CFG=1**，负面提示词一律不生效——所以所有「不要……」必须写进正向提示词。
* 局部微调（换发色 / 换表情 / 衣服改色）：同样是 fusion 通道，只传一张参考图。

Q 版为什么比写实人像难
----------------------
公开底模对「正常人体比例」的偏好极强，编辑时非常容易把 Q 版角色"修正"成七头身。
所以本文件的每个提示词里都有两处硬约束，缺一不可：

* 正向写死「头身比锁为 N 头身，头部高度占全身 X」；
* 正向补一句「不得把角色画成正常人体比例、不得拉长四肢」。

另外素体图的姿态也是刻意设计的（`POSE_LOCK`）：双臂外张 15 度、双脚分开。
这不是为了好看，是为了让袖子和裙摆在换装时**不需要模型去猜被遮住的躯干**——
遮得越少，重绘越不容易崩。
"""

from __future__ import annotations

import base64
import io
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PIL import Image

from shared.config import settings
from shared.models_util import decode_base64_image, resolve_stored


class DangoError(RuntimeError):
    """业务错误，message 直接面向用户。"""


# ============================================================
#  画风与比例
# ============================================================

STYLE_RENDER = {
    "mochi": "平涂上色搭配柔和的二分阴影，色彩低饱和、偏奶油质感，边缘干净无杂线",
    "cel": "干净的黑色描线 + 赛璐璐硬边阴影，色块分明，像动画关键帧",
    "gouache": "水彩纸纹理与轻微的颜料晕染，颜色通透、边缘略微化开，像绘本插图",
    "pixel": "32 位像素风，色板受限、像素块清晰、不出现抗锯齿模糊",
}

STYLE_LABEL = {
    "mochi": "软糯麻薯",
    "cel": "描线赛璐璐",
    "gouache": "水彩绘本",
    "pixel": "像素小人",
}

HEAD_FRACTION = {
    2.0: "二分之一",
    2.5: "五分之二",
    3.0: "三分之一",
    3.5: "七分之二",
}

# 素体姿态：为换装预留轮廓缝隙，不是审美选择（见模块头注释）
POSE_LOCK = (
    "正面站立，平视视角，从头到脚完整入镜（头顶与脚底都留出余白），"
    "双脚分开约一肩宽、脚尖略微向外，双臂自然下垂并向外张开约 15 度、"
    "与躯干之间留出清晰的缝隙，手掌位于身体两侧、不遮挡躯干的轮廓，"
    "十指自然并拢不张开"
)

# fusion 通道（CFG=1）里，「不要什么」只能写在正向里
EDIT_FORBID = (
    "画面中不出现任何可读文字、水印、签名、logo 或边框；"
    "不出现真人写实质感、不出现照片、不出现 3D 渲染；"
    "不改变角色的画风，不改变头身比"
)

# 文生图通道的负面提示词（CFG>1，真的生效）
BASE_NEGATIVE = (
    "写实照片，真人，真实人体比例，成年人体型，长腿，细长手臂，解剖肌肉细节，"
    "写实皮肤纹理，毛孔，多余的肢体，多余的手指，畸形的手，错位的手指，"
    "文字，水印，logo，签名，边框，复杂背景，场景道具，家具，地面倒影，"
    "低清晰度，模糊，噪点，多重人物，仅头部特写，半身像，被裁切的脚，"
    "写实褶皱，电影打光，强对比阴影"
)

OUTFIT_NEGATIVE = (
    "人体，模特，真人，穿着效果图，衣架，模特台，手，脸，"
    "写实照片，写实布料微距，3D 渲染，"
    "文字，水印，logo，标签，吊牌上的可读文字，边框，复杂背景，场景，"
    "低清晰度，模糊，噪点，多重单品，被裁切的衣服"
)


def _style_of(style_id: str) -> str:
    return STYLE_RENDER.get(style_id or "", STYLE_RENDER["mochi"])


def _proportion_of(value: Any) -> float:
    try:
        p = float(value)
    except (TypeError, ValueError):
        p = 2.5
    # 只接受 2.0 / 2.5 / 3.0 / 3.5，其余就近吸附
    return min((2.0, 2.5, 3.0, 3.5), key=lambda x: abs(x - p))


def _proportion_text(p: float) -> str:
    return f"{p:g} 头身"


def _style_block(style_id: str, proportion: float) -> str:
    p = _proportion_of(proportion)
    frac = HEAD_FRACTION.get(p, "五分之二")
    return (
        f"日系 Q 版（chibi）插画风格，{_style_of(style_id)}。"
        f"人物比例锁定为 {_proportion_text(p)}：头部高度约占全身的 {frac}，"
        "躯干短、肩膀窄、四肢短而圆、手掌与脚掌简化为圆润的块面；"
        "五官集中在脸部偏下的位置，眼睛大而明亮、眼距略宽，鼻子与嘴巴极小；"
        "整体轮廓圆润、四肢末端收成小的圆弧，不出现尖锐的解剖转折"
    )


# ============================================================
#  可选项目录（前端渲染表单用，同时保证「点选的东西」和提示词一一对应）
# ============================================================

HAIRS = {
    "short": "干净的齐刘海短发，发梢在耳垂上方、向内扣",
    "bob": "蓬松的短波波头，发尾在下巴附近微微外翻",
    "twin": "双马尾，两侧各扎一束到肩下、发梢微卷，配简单的小发圈",
    "long": "直长发，自头顶自然垂到腰际，发梢略微内收",
    "bun": "丸子头，头顶偏后扎一个圆发包，脸颊两侧留几缕碎发",
    "ahoge": "短碎发，头顶翘起一撮明显的呆毛（一撮向上弯的呆毛）",
    "curl": "中长卷发，头发呈大而柔和的波浪弧线，蓬松有体积感",
    "spiky": "短刺头，发丝呈一小簇一小簇的尖角，方向向外",
}

HAIR_LABEL = {
    "short": "齐刘海短发", "bob": "短波波头", "twin": "双马尾", "long": "长直发",
    "bun": "丸子头", "ahoge": "呆毛短发", "curl": "蓬松卷发", "spiky": "小刺头",
}

EYE_STYLES = {
    "dot": "圆润的豆豆眼，黑色小圆点高光，简洁可爱",
    "big": "大而明亮的圆眼，瞳孔占据眼眶大部分，内含两点高光",
    "star": "大眼睛配星星形状的高光，眼神亮得像在发光",
    "half": "微微下垂的半睁眼，眼型偏狭长，透出一点慵懒",
    "closed": "弯成月牙形的闭眼笑眼，只剩两道向上弯的弧线",
}

EYE_LABEL = {
    "dot": "豆豆眼", "big": "大圆眼", "star": "星星眼", "half": "半睁眼", "closed": "月牙眼",
}

EXPRESSIONS = {
    "smile": "嘴角小幅上扬的温和微笑，脸颊带一点点红晕",
    "flat": "没有明显表情，嘴角平直，神情平静",
    "open": "张开嘴开心大笑，露出一小截舌头与上排牙齿",
    "shy": "害羞地把嘴角抿起，脸颊两侧泛起明显的红晕",
    "sleepy": "一副困倦的样子，眼睛半闭、头顶飘一个小气泡",
    "proud": "得意地微微仰头、下巴略抬，嘴角一侧上扬",
}

EXPRESSION_LABEL = {
    "smile": "微笑", "flat": "无表情", "open": "张嘴笑",
    "shy": "害羞", "sleepy": "困困", "proud": "得意",
}

SLOTS = {
    "full": "整套服装（连衣裙 / 套装这类一次覆盖全身的搭配）",
    "top": "上装",
    "bottom": "下装",
    "outer": "外套",
    "shoes": "鞋袜",
    "acc": "配饰（头饰 / 围巾 / 包 / 眼镜）",
}

SLOT_LABEL = {
    "full": "整套", "top": "上装", "bottom": "下装",
    "outer": "外套", "shoes": "鞋袜", "acc": "配饰",
}

# 换装时要「保持不变」的槽位文案，用于告诉模型别动其他部位
_KEEP_NAME = {
    "full": "", "top": "上装", "bottom": "下装",
    "outer": "外套", "shoes": "鞋袜", "acc": "配饰",
}


# ============================================================
#  ① 捏团子：文生图素体
# ============================================================

def build_character_prompt(params: dict) -> dict:
    """素体提示词。输出 = 一张可以反复换装的「人台底图」。

    刻意让角色穿白色紧身连体打底衣，而不是裸模：
    一是避免内容策略问题，二是白色无花纹的底衣在换装时最容易被整件覆盖，
    不会像有花纹的衣服那样从袖口、领口"漏"出来。
    """
    style = str(params.get("style") or "mochi")
    proportion = _proportion_of(params.get("proportion", 2.5))
    hair = str(params.get("hair") or "short")
    eyes = str(params.get("eye") or "big")
    expr = str(params.get("expression") or "smile")
    hair_color = str(params.get("hair_color") or "栗棕色")
    eye_color = str(params.get("eye_color") or "琥珀色")
    skin = str(params.get("skin") or "白皙偏暖")

    sections = [
        ("① 画风与比例", _style_block(style, proportion) + "。"),
        ("② 角色特征", (
            f"{skin}的肤色，{hair_color}的头发：{HAIRS.get(hair, HAIRS['short'])}；"
            f"{eye_color}的眼睛：{EYE_STYLES.get(eyes, EYE_STYLES['big'])}；"
            f"表情为{EXPRESSIONS.get(expr, EXPRESSIONS['smile'])}。"
        )),
        ("③ 素体底衣", (
            "角色穿着一件纯白色、没有任何花纹图案、没有 logo 与装饰的短袖短裤紧身连体打底衣，"
            "简单的圆领，下摆到上大腿，贴合身体但不紧绷、没有明显褶皱。"
            "这件底衣是供后续换装使用的素体，必须干净、极简、无装饰。"
        )),
        ("④ 姿态与取景", POSE_LOCK + "。"),
        ("⑤ 画面", (
            "纯白背景，不画场景、不画地面与投影（除脚下极淡的一点接触阴影），"
            "均匀柔和的正面光，角色整体受光一致、没有强烈的高光与暗部。"
            "不要文字、水印、logo、边框与签名。"
        )),
    ]
    prompt = "\n".join(text for _, text in sections)
    return {
        "prompt": prompt,
        "negative": BASE_NEGATIVE,
        "sections": [{"label": k, "text": v} for k, v in sections],
        "meta": {
            "style": style, "style_label": STYLE_LABEL.get(style, style),
            "proportion": proportion,
            "hair": hair, "eye": eyes, "expression": expr,
        },
    }


# ============================================================
#  ② 做衣服：文生图单品图
# ============================================================

def build_outfit_prompt(params: dict) -> dict:
    """服装单品图提示词。输出 = 一张白底平铺的单品图，用作换装的 image_2。

    为什么做成「平铺白底图」而不是「穿在模特身上」：
    官方多图编辑的推荐用法就是「人物 + 商品」，商品图越干净、轮廓越完整，
    模型越容易把它准确地贴到人台上。穿在别人身上的衣服会带进另一个人的体型信息。
    """
    style = str(params.get("style") or "mochi")
    slot = str(params.get("slot") or "full")
    desc = str(params.get("description") or "").strip()
    if not desc:
        raise DangoError("请先描述这件衣服长什么样")

    slot_hint = {
        "full": "一件完整的连身或成套服装",
        "top": "一件上装（T 恤 / 衬衫 / 卫衣 / 毛衣这类）",
        "bottom": "一件下装（裙子 / 短裤 / 长裤这类）",
        "outer": "一件外套（开襟的夹克 / 大衣 / 斗篷这类）",
        "shoes": "一双鞋或一组鞋袜",
        "acc": "一件配饰（发饰 / 围巾 / 帽子 / 小包 / 眼镜这类）",
    }.get(slot, "一件服装单品")

    sections = [
        ("① 画风", (
            f"日系 Q 版插画风格的服装设计稿，{_style_of(style)}。"
            "造型语言与 Q 版角色一致：轮廓圆润、结构简化、细节做减法，"
            "适合穿在短圆四肢的 Q 版角色身上"
        )),
        ("② 单品", f"{slot_hint}：{desc}。"),
        ("③ 呈现方式", (
            "这一件单品单独呈现，正面视角，平铺展开在纯白背景上，像电商白底商品图："
            "衣服完整摊平、轮廓边界清楚、左右对称自然，领口与袖口敞开可见，"
            "不穿在任何人身上、不出现人体或模特、不出现衣架与支架"
        )),
        ("④ 细节", (
            "面料质感、颜色、图案、纽扣与缝线清晰可辨；"
            "褶皱只保留平铺状态下自然形成的少量浅褶，不做立体穿着褶皱"
        )),
        ("⑤ 画面", (
            "纯白背景，均匀正面光，单品整体受光一致，"
            "不出现文字、水印、logo、吊牌上的可读文字、边框与签名"
        )),
    ]
    prompt = "\n".join(text for _, text in sections)
    return {
        "prompt": prompt,
        "negative": OUTFIT_NEGATIVE,
        "sections": [{"label": k, "text": v} for k, v in sections],
        "meta": {"style": style, "slot": slot, "description": desc},
    }


# ============================================================
#  ③ 换装：多参考图编辑（fusion 通道）
# ============================================================

_KEEP_FRAME = (
    "保持原图的取景、构图、缩放级别与角色在画面中的大小位置不变，"
    "不作重新构图或整体重绘"
)


def build_wear_prompt(*, slot: str, outfit_names: list[str],
                      extra: str = "", combo: bool = False) -> dict:
    """换装提示词。image_1 = 团子当前形象，image_2..n = 服装单品图。

    `combo=False`：单件替换，其他部位明确要求保持原样（靠 `_KEEP_NAME` 列出来）。
    `combo=True` ：多件一次性穿好，逐件说明 image_n 对应哪个部位。
    """
    slot = slot if slot in SLOTS else "full"

    if combo:
        pairs = []
        for i, name in enumerate(outfit_names, start=2):
            pairs.append(f"image_{i}（{name}）")
        change = (
            "把以下服装同时穿到角色身上：" + "、".join(pairs) + "。"
            "各件服装的款式、版型、颜色、面料质感与图案细节必须与对应的参考图完全一致，"
            "并按先后层次正确叠穿（贴身层在内、外套与配饰在外），"
            "各件之间的衔接处（领口、袖口、腰线、下摆）自然过渡，不出现互相穿模或错位"
        )
        keep = ""
    else:
        label = SLOT_LABEL.get(slot, "服装")
        name = outfit_names[0] if outfit_names else "image_2 中的服装"
        change = (
            f"仅把角色的{label}替换为 image_2 中的服装（{name}）："
            "款式、版型、颜色、面料质感与图案细节必须与 image_2 完全一致"
        )
        if slot == "full":
            change += (
                "，整套服装完整覆盖角色原来的上装与下装，"
                "角色的四肢从新的袖口与裙摆/裤口自然伸出"
            )
        elif slot == "top":
            change += "，新上装的下摆压在原来的下装之外，下装与鞋袜保持原样"
        elif slot == "bottom":
            change += "，新下装从原来的上装下摆处接续，上装与鞋袜保持原样"
        elif slot == "outer":
            change += "，外套穿在原来服装的最外层，原来的内搭在领口与袖口处露出"
        elif slot == "shoes":
            change += "，只替换脚部的鞋袜，小腿与其余服装保持原样"
        elif slot == "acc":
            change += "，配饰戴在画面中合适的位置（头部 / 颈部 / 手部），其余服装保持原样"
        keep = ""
        if slot != "full":
            others = [v for k, v in _KEEP_NAME.items() if k not in ("full", slot) and v]
            if others:
                keep = "同时" + "、".join(others) + "严格沿用 image_1 不变；"

    sections = [
        ("① 锁定身份", (
            "以 image_1 中的 Q 版角色为身份基准：保持同一张脸——"
            "脸型、五官位置、眼睛的形状与颜色、鼻子与嘴巴的画法、"
            "发型轮廓与发色、肤色、表情全部沿用 image_1 不变，"
            "画面中的角色与 image_1 是同一个角色"
        )),
        ("② 变更内容", change + "。"),
        ("③ 锁定比例", (
            "头身比保持 image_1 的原样不得改变："
            "头部大小不变、躯干与四肢长度不变、肩膀宽度不变；"
            "绝对不得把角色画成正常人体比例，不得拉长四肢、不得加长腿部、"
            "不得把 Q 版画成少年或成人比例；新服装必须按 Q 版短圆的身体重新贴合，"
            "袖长、裙长、裤长按角色实际的四肢长度等比例调整，不做写实级褶皱"
        )),
        ("④ 融合与光线", (
            "新服装的受光方向、明暗关系与色温与 image_1 完全一致，"
            "衣服与身体之间产生合理的接触阴影，袖口、领口、腰线处贴合无缝隙，"
            "不出现贴图感、不出现悬浮的衣片、不出现与身体错位的布料"
        )),
        ("⑤ 三不变", (
            f"{_KEEP_FRAME}；角色其余部分（脸、发型、表情、背景、画面留白）"
            "与输入图保持一致。" + keep
        )),
        ("⑥ 禁止项", EDIT_FORBID + "。"),
    ]
    if extra.strip():
        sections.insert(5, ("补充要求", extra.strip() + "。"))

    prompt = "\n".join(text for _, text in sections if text)
    return {
        "prompt": prompt,
        "sections": [{"label": k, "text": v} for k, v in sections if v],
        "meta": {"slot": slot, "combo": combo, "outfits": outfit_names},
    }


# ============================================================
#  ④ 微调：单张图的局部编辑
# ============================================================

TWEAKS = {
    "hair_color": {
        "label": "换发色", "field": "目标发色", "placeholder": "冷调银灰",
        "level": "appearance",
        "body": ("只把角色的头发颜色改为{value}，发丝的走向、发型轮廓与长度完全不变，"
                 "新发色的明暗关系与原图一致、不出现染色不均的斑块"),
    },
    "hair_style": {
        "label": "换发型", "field": "目标发型", "placeholder": "双马尾，两侧各扎一束到肩下、发梢微卷",
        "level": "appearance",
        "body": ("只把角色的发型改为{value}，头发长度与体积随之调整但必须仍然符合 Q 版的夸张比例，"
                 "发丝走向自然、层次分明，原本露出的耳朵与脸型轮廓不受影响"),
    },
    "eye_color": {
        "label": "换瞳色", "field": "目标瞳色", "placeholder": "湖蓝色",
        "level": "appearance",
        "body": ("只把角色眼睛的瞳色改为{value}，眼型、眼睛大小、高光位置与眼神方向完全不变，"
                 "两侧瞳孔颜色一致"),
    },
    "expression": {
        "label": "换表情", "field": "目标表情", "placeholder": "张嘴开心大笑，露出一小截舌头与上排牙齿",
        "level": "appearance",
        "body": ("只把角色的表情改为{value}，脸型、五官位置与画风保持不变，"
                 "新表情的肌肉变化符合 Q 版的简化画法、不做写实面部细节"),
    },
    "outfit_color": {
        "label": "衣服改色", "field": "目标颜色", "placeholder": "雾霾蓝",
        "level": "appearance",
        "body": ("只把角色身上服装的主色改为{value}，服装的款式、版型、图案、缝线与装饰完全不变，"
                 "颜色替换只改色相不改图案结构，面料质感的明暗关系与原图一致"),
    },
    "scene": {
        "label": "换背景", "field": "目标背景", "placeholder": "铺满樱花瓣的草地，远处有一棵大树",
        "level": "semantic",
        "body": ("把角色身后的背景替换为{value}，背景的光线与角色身上的受光方向保持统一，"
                 "角色本身（发型、表情、服装、姿态、比例）完全不变，"
                 "角色边缘与原背景之间的接触关系自然、不出现抠图般的硬边"),
    },
    "style": {
        "label": "换画风", "field": "目标画风", "placeholder": "水彩绘本，颜色通透、边缘略微化开",
        "level": "semantic",
        "body": ("把整幅画面的画风转换为{value}，"
                 "角色的头身比、五官结构、发型与服装款式严格保持原样，"
                 "只改变绘制媒介与笔触，不改变角色的任何设计"),
    },
}


def build_tweak_prompt(tweak: str, value: str) -> dict:
    spec = TWEAKS.get(tweak)
    if not spec:
        raise DangoError(f"未知的微调类型：{tweak}")
    value = (value or "").strip()
    if not value:
        raise DangoError(f"请填写「{spec['field']}」")

    body = spec["body"].format(value=value)
    sections = [
        ("① 变更内容", f"对 image_1 进行局部编辑：{body}。"),
        ("② 锁定身份", (
            "保持原图的取景、构图、缩放级别与角色在画面中的大小位置不变；"
            "角色是 Q 版比例，头身比不得改变，不得画成正常人体比例"
        )),
        ("③ 光线", "新增或改变的部位，其受光方向、明暗与色温与原图保持一致，不出现粘贴感。"),
        ("④ 禁止项", EDIT_FORBID + "。"),
    ]
    prompt = "\n".join(text for _, text in sections)
    return {
        "prompt": prompt,
        "sections": [{"label": k, "text": v} for k, v in sections],
        "meta": {"tweak": tweak, "level": spec["level"], "value": value},
    }


# ============================================================
#  参考图处理
# ============================================================

def normalize_reference(img: Image.Image, max_mp: float = 0.9,
                        multiple: int = 64, max_side: int = 1280) -> Image.Image:
    """把参考图压进「像素预算」。

    2.1 编辑通道的输出尺寸 = image_1 的尺寸，且耗时随像素线性增长。
    用户随手传的 2K 图会让一次换装跑到不可接受的长度，所以统一压到
    ~0.9MP 并让边长对齐 64（VAE 的 8 倍下采样 × 潜在空间分块）。
    """
    w, h = img.size
    if w <= 0 or h <= 0:
        raise DangoError("参考图尺寸无效")

    def _round(v: float) -> int:
        return max(256, int(round(v / multiple)) * multiple)

    scale = 1.0
    max_mp = max(0.1, float(max_mp))
    if w * h > max_mp * 1_000_000:
        scale = min(scale, (max_mp * 1_000_000 / (w * h)) ** 0.5)
    if max(w, h) > max_side:
        scale = min(scale, max_side / max(w, h))

    tw, th = _round(w * scale), _round(h * scale)
    if (tw, th) == (w, h):
        return img
    return img.resize((tw, th), Image.LANCZOS)


def encode_reference(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def load_look_reference(look: dict, max_mp: float = 0.9) -> str:
    """把资料库里的一张形象图读成 data URL（换装时作为 image_1）。"""
    if not look:
        raise DangoError("这个团子还没有形象图，请先生成素体")
    raw = look.get("path") or look.get("url") or ""
    path: Optional[Path] = resolve_stored(raw)
    if not path or not path.exists():
        raise DangoError("形象图文件已丢失（可能被清理），请重新生成")
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            out = normalize_reference(img, max_mp=max_mp)
            data = encode_reference(out)
            if out is not img:
                out.close()
            return data
    except DangoError:
        raise
    except Exception as exc:
        raise DangoError(f"形象图读取失败：{exc}") from exc


def load_outfit_reference(outfit: dict, max_mp: float = 0.9) -> str:
    image = (outfit or {}).get("image") or {}
    if not image:
        raise DangoError(f"服装「{outfit.get('name', '?')}」缺少图片")
    return load_look_reference(image, max_mp=max_mp)


def decode_upload(data: str) -> str:
    """用户直接上传的服装图：解码 → 归一化 → 重新编码。"""
    try:
        img = decode_base64_image(data)
    except Exception as exc:
        raise DangoError(f"图片解析失败：{exc}") from exc
    try:
        out = normalize_reference(img)
        encoded = encode_reference(out)
        if out is not img:
            out.close()
        return encoded
    finally:
        img.close()


# ============================================================
#  出图 payload
# ============================================================

def output_subdir() -> str:
    return f"dango/{datetime.now().strftime('%Y-%m-%d')}"


def _base_payload(*, prompt: str, negative: str, width: int, height: int,
                  steps: int, seed: int, count: int, tag: str) -> dict:
    return {
        "prompt": prompt,
        # 编辑通道 CFG=1，负面不生效；文生图通道负面生效
        "negative_prompt": negative or "",
        "width": int(width),
        "height": int(height),
        "steps": int(steps),
        "guidance_scale": float(settings.default_guidance),
        "seed": int(seed),
        "count": max(1, int(count)),
        "strength": 1.0,
        "channel": "auto",
        "output_subdir": output_subdir(),
        "mode": "txt2img",
        "dango": {"kind": tag},
    }


def _attach_refs(payload: dict, refs: list[str]) -> dict:
    """把参考图挂成 fusion 通道（编辑固定 CFG=1、denoise=1、尺寸随 image_1）。"""
    payload.update({
        "mode": "img2img",
        "fusion": True,
        "ref_images": refs,
        "image": None,
        "negative_prompt": "",
        "guidance_scale": 1.0,
        "strength": 1.0,
    })
    try:
        first = decode_base64_image(refs[0])
        payload["width"], payload["height"] = first.size
        first.close()
    except Exception:
        pass
    return payload


def payload_character(params: dict, *, steps: int = 8, seed: int = -1,
                      count: int = 1, width: int = 768, height: int = 1152) -> dict:
    built = build_character_prompt(params)
    payload = _base_payload(prompt=built["prompt"], negative=built["negative"],
                            width=width, height=height, steps=steps, seed=seed,
                            count=count, tag="character")
    payload["dango"]["params"] = params
    return {"payload": payload, "info": built}


def payload_outfit(params: dict, *, steps: int = 8, seed: int = -1,
                   count: int = 1, width: int = 768, height: int = 768) -> dict:
    built = build_outfit_prompt(params)
    payload = _base_payload(prompt=built["prompt"], negative=built["negative"],
                            width=width, height=height, steps=steps, seed=seed,
                            count=count, tag="outfit")
    payload["dango"]["params"] = params
    return {"payload": payload, "info": built}


def payload_wear(*, base_look: dict, outfits: list[dict], slot: str,
                 extra: str = "", steps: int = 8, seed: int = -1,
                 count: int = 1, max_mp: float = 0.9) -> dict:
    if not outfits:
        raise DangoError("请至少选一件衣服")
    if len(outfits) > 3:
        raise DangoError("一次最多同时穿 3 件，多了模型会顾不上")

    names = [str(o.get("name") or "服装单品") for o in outfits]
    combo = len(outfits) > 1
    built = build_wear_prompt(slot=slot, outfit_names=names,
                              extra=extra, combo=combo)

    refs = [load_look_reference(base_look, max_mp=max_mp)]
    for o in outfits:
        refs.append(load_outfit_reference(o, max_mp=max_mp))

    payload = _base_payload(prompt=built["prompt"], negative="",
                            width=768, height=1152, steps=steps, seed=seed,
                            count=count, tag="wear")
    payload = _attach_refs(payload, refs)
    payload["dango"]["outfits"] = [o.get("id") for o in outfits]
    payload["dango"]["slot"] = slot
    return {"payload": payload, "info": built}


def payload_tweak(*, base_look: dict, tweak: str, value: str,
                  steps: int = 8, seed: int = -1, count: int = 1,
                  max_mp: float = 0.9) -> dict:
    built = build_tweak_prompt(tweak, value)
    refs = [load_look_reference(base_look, max_mp=max_mp)]
    payload = _base_payload(prompt=built["prompt"], negative="",
                            width=768, height=1152, steps=steps, seed=seed,
                            count=count, tag="tweak")
    payload = _attach_refs(payload, refs)
    payload["dango"]["tweak"] = tweak
    return {"payload": payload, "info": built}


# ============================================================
#  选项目录
# ============================================================

HAIR_COLORS = ["乌黑", "栗棕色", "亚麻金", "樱花粉", "薄荷绿", "湖蓝", "冷调银灰",
               "蜜橘色", "葡萄紫", "纯白"]
EYE_COLORS = ["琥珀色", "湖蓝色", "翠绿色", "紫罗兰色", "石榴红", "金棕色", "灰蓝色"]
SKINS = ["白皙偏暖", "白皙偏冷", "健康的浅麦色", "奶白色"]


def options_payload() -> dict:
    def items(mapping: dict, labels: dict) -> list[dict]:
        return [{"id": k, "label": labels.get(k, k), "text": v} for k, v in mapping.items()]

    return {
        "styles": [{"id": k, "label": STYLE_LABEL[k], "text": STYLE_RENDER[k]}
                   for k in STYLE_RENDER],
        "proportions": [
            {"value": 2.0, "label": "2 头身", "desc": "最圆最糯，适合做挂件 / 表情包"},
            {"value": 2.5, "label": "2.5 头身", "desc": "标准 Q 版，换装细节刚好够画"},
            {"value": 3.0, "label": "3 头身", "desc": "稍微长一点，能看清服装版型"},
            {"value": 3.5, "label": "3.5 头身", "desc": "接近小少年，适合有设计感的套装"},
        ],
        "hairs": items(HAIRS, HAIR_LABEL),
        "eyes": items(EYE_STYLES, EYE_LABEL),
        "expressions": items(EXPRESSIONS, EXPRESSION_LABEL),
        "hair_colors": HAIR_COLORS,
        "eye_colors": EYE_COLORS,
        "skins": SKINS,
        "slots": [{"id": k, "label": SLOT_LABEL[k], "desc": v} for k, v in SLOTS.items()],
        "tweaks": [{"id": k, "label": v["label"], "field": v["field"],
                    "placeholder": v["placeholder"], "level": v["level"]}
                   for k, v in TWEAKS.items()],
        "defaults": {
            "style": "mochi", "proportion": 2.5, "hair": "short", "eye": "big",
            "expression": "smile", "hair_color": "栗棕色", "eye_color": "琥珀色",
            "skin": "白皙偏暖", "slot": "full",
        },
        "sizes": {
            "character": [{"w": 768, "h": 1152, "label": "3:4 竖版（推荐）"},
                          {"w": 640, "h": 960, "label": "小一点，快一点"},
                          {"w": 896, "h": 1344, "label": "大一点，细节更多"}],
            "outfit": [{"w": 768, "h": 768, "label": "1:1 方图（推荐）"},
                       {"w": 896, "h": 896, "label": "大一点"}],
        },
    }
