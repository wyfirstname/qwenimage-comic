# -*- coding: utf-8 -*-
"""Qwen-Image 2.1 人像编辑（图像编辑）业务层。

设计原则
--------
1. **纯新增，不动漫画**：本模块只负责「玩法预设 + 变量 → 完整提示词」的组装，
   出图仍然复用既有出图队列（`mode=img2img` + `fusion=True`），
   由引擎里已经存在的 `TextEncodeQwenImage21` 多参考图编辑通道完成推理。
   因此漫画工作室的代码一行都不用改。
2. **按官方文档实现**：官方图像编辑文档给出的提示词结构是
   「锁定身份 → 描述要改的内容 → 光线 → 构图 → 禁止项」，
   官方还明确要求局部编辑声明「三不变」（取景/构图/缩放级别 + 人物大小位置），
   修复类要求「不虚构、不美化」。这些约束全部固化进模板，保证出图稳定。
3. **官方采样约定**：2.1 编辑通道固定 CFG=1、denoise=1，latent 取参考图的空 latent，
   输出尺寸由 image_1 决定（见 inference/comfy_engine.py）。所以这里不做宽高缩放，
   改为对参考图本身做「像素预算」归一化。
"""

from __future__ import annotations

import base64
import io
import queue
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

from PIL import Image

from app.config import settings
from app.inference.comfy_text import LocalTextError, text_engine
from app.models_util import decode_base64_image


class PortraitError(RuntimeError):
    """人像编辑业务错误，message 直接面向用户。"""


# ============================================================
#  编辑层级（官方 Qwen-Image-Edit-Plus 文档的两种编辑方式）
# ============================================================

LEVEL_APPEARANCE = "appearance"   # 外观级：局部修补，除目标区域外其余不动
LEVEL_SEMANTIC = "semantic"       # 语义级：允许大幅改像素，只保证语义与身份一致

LEVEL_LABELS = {
    LEVEL_APPEARANCE: "外观级编辑（只改目标区域，其余保持不变）",
    LEVEL_SEMANTIC: "语义级编辑（重建画面，保留身份与语义）",
}

# 官方对「局部编辑」的固定约束语句（三不变 + 不重绘）
KEEP_FRAME = (
    "此为在原图上的局部编辑：保持原图的取景、构图、缩放级别与人物在画面中的大小位置不变，"
    "不作重新构图或整体重绘"
)
NO_TEXT = "画面中不出现任何可读文字、水印或 logo。"


# ============================================================
#  各玩法的提示词构建器
#  统一返回 [(分区标题, 正文), ...]，便于前端展示「官方结构」的每一段
# ============================================================

def _sec_scene(v: dict, n: int) -> list[tuple[str, str]]:
    """场景重绘人像（对应官方 Traditional / Mangrove / Indoor 三例）。"""
    out: list[tuple[str, str]] = []
    out.append(("① 锁定身份", (
        "以 image_1 中的人物为身份基准：面部五官比例、脸型与辨识特征严格沿用原图不变，"
        "画面中的人物必须与 image_1 是同一个人。"
    )))
    change = [f"围绕该人物重建整个画面，场景设定为：{v['scene']}。"]
    if v.get("outfit"):
        change.append(f"人物换装为：{v['outfit']}。")
    if v.get("action"):
        change.append(f"姿态与动作：{v['action']}。")
    out.append(("② 变更内容", "".join(change)))
    out.append(("③ 光线", (
        f"{v['light']}。新增场景、服装与皮肤上的受光与阴影遵循统一的真实光照逻辑，"
        "不出现粘贴感或与画面原有光影冲突的亮暗面。"
    )))
    out.append(("④ 构图", (
        f"构图为{v['shot']}，人物位于画面视觉中心，身体比例与透视自然正确"
        "（头部保持正常成人比例），背景与人物层次分明、留有合理的呼吸空间。"
    )))
    quality = (
        "照片级真实质感，皮肤保留自然纹理与毛孔，发丝细节清晰，"
        "避免塑料感、过度磨皮与涂抹感。"
    )
    if v.get("extra"):
        quality += v["extra"] if v["extra"].endswith("。") else v["extra"] + "。"
    out.append(("⑤ 画质与禁止项", quality + NO_TEXT))
    return out


def _sec_hair(v: dict, n: int) -> list[tuple[str, str]]:
    """发型编辑（官方局部编辑，只改发型）。"""
    return [
        ("① 变更内容", (
            f"对 image_1 进行局部编辑：只把人物的发型由原图中的发型改为：{v['hairstyle']}。"
            "发丝走向自然、层次分明、边缘过渡真实，卷度与厚度符合真实毛发规律，"
            "原本散落在脸颊与颈侧的碎发顺势融入新发型的走向。"
        )),
        ("② 光线", (
            "新发型的受光、阴影与高光顺应画面既有的光照方向与色温，"
            "在发丝凹陷处形成柔和暗部、在发脊处形成细腻高光，不出现贴图感。"
        )),
        ("③ 三不变（官方固定句式）", (
            f"{KEEP_FRAME}；人物的面部五官、表情、肤色、妆容、服装、背景"
            "以及画面其余部分均与输入图完全保持一致。"
        )),
        ("④ 禁止项", NO_TEXT),
    ]


def _sec_expression(v: dict, n: int) -> list[tuple[str, str]]:
    """表情 / 动作局部编辑（官方双人合照示例的写法）。"""
    change = [f"对 image_1 进行局部编辑：将{v['people']}的表情由原图改为{v['expression']}"]
    if v.get("action"):
        change.append(f"，同时把动作调整为：{v['action']}")
    change.append(
        "。面部肌肉与笑肌的自然变化、牙齿与眼睛的结构真实完整、眼神有神采，"
        "新出现的表情程度一致、情绪相互呼应。"
    )
    if v.get("action"):
        change.append(
            "新增的手部与肢体结构完整清晰（手指数量、关节与朝向正确），"
            "抓握物件符合真实受力关系，不出现多指、断指或肢体粘连。"
        )
    return [
        ("① 变更内容", "".join(change)),
        ("② 光线", (
            "新增内容（表情与肢体）的受光与阴影遵循原图统一的自然光照逻辑，"
            "亮暗面与原有画面方向一致，不出现粘贴感。"
        )),
        ("③ 三不变（官方固定句式）", (
            f"{KEEP_FRAME}；面部五官与身份特征不变，画面其余部分与输入图保持一致。"
        )),
        ("④ 禁止项", NO_TEXT),
    ]


def _sec_restore(v: dict, n: int) -> list[tuple[str, str]]:
    """老照片修复（官方英文长提示词，核心是「不虚构、不美化」）。"""
    base = (
        "Significantly improve the resolution and overall clarity of this vintage "
        "photograph and restore it as a realistic, naturally colorized high-fidelity "
        "photograph. Remove age-related dust, scratches, spots, fading, and excessive "
        "grain; recover fine detail and balanced highlights and shadows without "
        "oversharpening, waxy skin, or painted textures."
    )
    preserve = (
        "Preserve the exact facial anatomy, wrinkles, gaze, head angle, poses, "
        "expressions, clothing designs, object placement, and occlusions of the people "
        "in the image; clarify fine hair, eyebrows, natural skin texture and fabric "
        "details without altering their identity. Strictly preserve the original aspect "
        "ratio, framing, camera viewpoint, exact number and arrangement of people, ages, "
        "and the original direction and character of the lighting."
    )
    colorize = (
        "Colorize with believable, restrained colors appropriate to the period and "
        "materials, with lifelike skin tones and no color bleeding; produce a full-color "
        "photograph, not monochrome or sepia."
    )
    forbid = (
        "Where the original is unclear, retain natural softness rather than inventing "
        "anatomy, objects, symbols, or readable text. Do not crop, expand, beautify "
        "faces, modernize the scene, or alter existing printed marks. Do not add any "
        "readable text, watermark, or logo."
    )
    if v.get("extra"):
        forbid += " " + v["extra"].strip()
    return [
        ("① 修复目标", base),
        ("② 严格保留（不虚构）", preserve),
        ("③ 上色", colorize),
        ("④ 禁止项", forbid),
    ]


def _sec_style(v: dict, n: int) -> list[tuple[str, str]]:
    """照片风格化（官方 amusement-park 示例的中文改写）。"""
    keep_text = v.get("allow_text") == "yes"
    forbid = (
        "不新增原图没有的装饰边框或签名；原图已有的可识别文字保留其位置与内容，不得改写或新增。"
        if keep_text else
        "不新增原图没有的文字、标语、说明文字、装饰边框或签名。" + NO_TEXT
    )
    return [
        ("① 风格目标", (
            f"将 image_1 整张画面转换为指定的视觉风格：{v['style']}。"
            "整幅画面必须统一呈现该风格，不留任何照片原样的斑块。"
        )),
        ("② 结构保留", (
            "保留原图的构图、画幅比例、视角与景深关系，主体与物体的位置、结构、数量"
            "及姿态不变；笔触与质感不得让结构变形、融化或错位（如圆形与直线的几何关系必须保持）。"
            "笔画走向应顺应物体的形体与结构。"
        )),
        ("③ 光色", (
            "光照方向、色温与色彩关系保持统一协调，符合原图的明暗逻辑。"
        )),
        ("④ 禁止项", forbid),
    ]


def _sec_compose(v: dict, n: int) -> list[tuple[str, str]]:
    """多图合成（官方 2509 「人+人 / 人+商品 / 人+场景」多图输入玩法）。"""
    refs = "、".join(f"image_{i}" for i in range(2, n + 1))
    return [
        ("① 参考图分工", (
            f"以 image_1 为画面基础，把 {refs} 中的主体自然融入同一个场景中：{v['compose']}。"
            f"提示词中 image_1…image_{n} 按上传顺序对应各张参考图。"
        )),
        ("② 空间与融合", (
            "各主体之间的相对位置、比例、朝向、遮挡关系与接触阴影符合真实空间逻辑；"
            "不同来源的元素必须统一到同一套光照方向、色温、白平衡与景深之中，"
            "看起来像同一次拍摄完成——禁止拼贴、并排摆放、分割画布或出现明显接缝。"
        )),
        ("③ 身份保留", (
            "保留各主体各自的身份特征与关键细节（人脸辨识特征、服装款式、物体结构）不丢失。"
        )),
        ("④ 画质与禁止项", (
            "画面整体为照片级真实质感，光影自然、细节清晰。" + NO_TEXT
        )),
    ]


def _sec_outfit(v: dict, n: int) -> list[tuple[str, str]]:
    """换装（image_1 人物 + image_2 服装）。"""
    return [
        ("① 锁定身份", (
            "保持 image_1 中人物的面部五官、发型、肤色、姿态、构图与背景完全不变，"
            "画面中的人物与 image_1 是同一个人。"
        )),
        ("② 变更内容", (
            "仅将人物的服装替换为 image_2 中的服装：款式、版型、颜色、面料质感与图案细节"
            "必须与 image_2 完全一致，并按人物的身体与姿态自然贴合，"
            "布料的褶皱、垂坠与受力关系真实。"
            + (f"补充要求：{v['extra']}" if v.get("extra") else "")
        )),
        ("③ 光线", (
            "新服装的受光与阴影严格遵循原图的光照方向与色温，与人物及背景自然融合，无拼接痕迹。"
        )),
        ("④ 三不变（官方固定句式）", (
            f"{KEEP_FRAME}；画面其余部分（面部、发型、背景、其他人物与物体）与输入图保持一致。"
        )),
        ("⑤ 禁止项", NO_TEXT),
    ]


def _sec_editorial(v: dict, n: int) -> list[tuple[str, str]]:
    """编辑风人像（文生图，官方 Editorial portrait 示例）。"""
    text = (
        f"Create an editorial portrait of {v['subject']}. "
        "Natural skin texture, soft directional lighting, refined modern editorial styling, "
        "medium-format photography, shallow depth of field, subtle film grain, calm expression, "
        "high detail, no text or watermark."
    )
    if v.get("light"):
        text += f" Lighting: {v['light']}."
    return [
        ("① 主体", text),
        ("② 禁止项", "no text, no watermark, no logo, no extra limbs or malformed hands."),
    ]


# ============================================================
#  玩法预设
# ============================================================

PRESETS: list[dict[str, Any]] = [
    {
        "id": "portrait_scene",
        "name": "场景重绘人像",
        "group": "图像编辑",
        "level": LEVEL_SEMANTIC,
        "kind": "edit",
        "images": {"min": 1, "max": 1},
        "desc": "保留人物身份，重建整个场景（含换装、换动作）。对应官方 Traditional clothing / Mangrove / Indoor candid portrait 三例的写法。",
        "official": "官方示例：以输入图片为身份基准，面部五官沿用原图不变，围绕人物重建整个画面",
        "fields": [
            {"key": "scene", "label": "目标场景", "required": True, "multiline": True,
             "placeholder": "中国南方海岸的红树林湿地，木质浮桥伸向红树林深处，两侧是支柱根与呼吸根，浅滩倒映天空",
             "hint": "场景越具体越好：地点 + 环境物件 + 远景层次"},
            {"key": "outfit", "label": "服装（留空=沿用原图）",
             "placeholder": "纯色白圆领短袖 T 恤 + 卡其色多口袋工装裤 + 米白色帆布鞋"},
            {"key": "action", "label": "姿态 / 动作",
             "placeholder": "行走中的半步动态，右手捏着一顶草编帽的帽檐，目光落在镜头上"},
            {"key": "light", "label": "光线", "default": "温暖的午后阳光", "multiline": True,
             "placeholder": "温暖的午后阳光透过树叶缝隙洒落，人物面部处于柔和明亮的散射光中"},
            {"key": "shot", "label": "构图", "default": "全身环境人像（中长焦平视）",
             "placeholder": "全身环境人像（中长焦平视）"},
            {"key": "extra", "label": "补充要求", "multiline": True,
             "placeholder": "例如：叠加 CCD 直出噪点与轻微暗角，呈现抓拍快照质感"},
        ],
        "builder": _sec_scene,
    },
    {
        "id": "portrait_hair",
        "name": "发型编辑",
        "group": "图像编辑",
        "level": LEVEL_APPEARANCE,
        "kind": "edit",
        "images": {"min": 1, "max": 1},
        "desc": "只改发型，其余全部不动。对应官方 Hairstyle editing 示例。",
        "official": "官方示例：此在原图上的局部编辑，保持取景、构图、缩放级别与人物大小位置不变",
        "fields": [
            {"key": "hairstyle", "label": "目标发型", "required": True, "multiline": True,
             "placeholder": "蓬松的大波浪长卷发，自头顶自然披落至双肩与胸前，卷度呈大而清晰的波浪弧形"},
        ],
        "builder": _sec_hair,
    },
    {
        "id": "portrait_expression",
        "name": "表情 / 动作编辑",
        "group": "图像编辑",
        "level": LEVEL_APPEARANCE,
        "kind": "edit",
        "images": {"min": 1, "max": 1},
        "desc": "只改表情与局部动作，脸和构图都不变。对应官方 Expression editing 示例。",
        "official": "官方示例：保持取景、构图、缩放级别与人物大小位置不变，新增肢体的光线遵循原图逻辑",
        "fields": [
            {"key": "people", "label": "对象", "default": "画面中的人物",
             "placeholder": "画面中并肩站立的两位年轻女性"},
            {"key": "expression", "label": "目标表情", "required": True, "multiline": True,
             "placeholder": "一同开口大笑：张嘴露齿、笑肌提起、嘴角大幅上扬、双眼弯成月牙形"},
            {"key": "action", "label": "目标动作（可留空）", "multiline": True,
             "placeholder": "一起比耶：左手举到脸颊外侧比出 V 字手势"},
        ],
        "builder": _sec_expression,
    },
    {
        "id": "photo_restore",
        "name": "老照片修复",
        "group": "图像编辑",
        "level": LEVEL_APPEARANCE,
        "kind": "edit",
        "images": {"min": 1, "max": 1},
        "desc": "去噪、去划痕、上色、提升清晰度，强调不虚构不美化。对应官方 Vintage photo restoration 示例。",
        "official": "官方示例：Strictly preserve … do not fabricate / do not beautify（宁可保留模糊也不编造细节）",
        "fields": [
            {"key": "extra", "label": "补充要求", "multiline": True,
             "placeholder": "例如：请恢复成 1920 年代风格的自然彩色，不要过度锐化"},
        ],
        "builder": _sec_restore,
    },
    {
        "id": "photo_style",
        "name": "照片风格化",
        "group": "图像编辑",
        "level": LEVEL_SEMANTIC,
        "kind": "edit",
        "images": {"min": 1, "max": 1},
        "desc": "整张转成指定画风，保留构图与可识别结构。对应官方 Photo stylization 示例。",
        "official": "官方示例：整幅画面统一呈现目标介质，禁止出现照片原样的斑块与结构变形",
        "fields": [
            {"key": "style", "label": "目标风格", "required": True, "multiline": True,
             "placeholder": "明亮的印象派油画，细纹画布上层层叠加的自信笔触、破碎色彩与含蓄的厚涂高光"},
            {"key": "allow_text", "label": "保留原图文字", "default": "no",
             "placeholder": "yes / no", "hint": "no=不允许出现任何文字（默认）；yes=允许保留原图已有文字"},
        ],
        "builder": _sec_style,
    },
    {
        "id": "multi_compose",
        "name": "多图合成",
        "group": "图像编辑",
        "level": LEVEL_SEMANTIC,
        "kind": "edit",
        "images": {"min": 2, "max": 3},
        "desc": "人+人 / 人+商品 / 人+场景 的多图输入融合。官方 Qwen-Image-Edit-2509 的多图能力，1~3 张效果最佳。",
        "official": "官方文档：多图编辑支持「人物+人物」「人物+商品」「人物+场景」，1~3 张输入表现最佳",
        "fields": [
            {"key": "compose", "label": "组合方式", "required": True, "multiline": True,
             "placeholder": "让 image_1 中的人物穿上 image_2 的服装，站在 image_1 的场景里自然对视"},
        ],
        "builder": _sec_compose,
    },
    {
        "id": "outfit_swap",
        "name": "换装（人+服装）",
        "group": "图像编辑",
        "level": LEVEL_APPEARANCE,
        "kind": "edit",
        "images": {"min": 2, "max": 2},
        "desc": "image_1 的人物 + image_2 的服装，脸与构图不变。官方「人+商品」多图玩法的典型用法。",
        "official": "官方文档：多图输入可实现「人物+商品」的商品植入与换装，保持人物一致性",
        "fields": [
            {"key": "extra", "label": "补充要求", "multiline": True,
             "placeholder": "例如：保留毛衣的费尔岛花纹与围巾的针织质感"},
        ],
        "builder": _sec_outfit,
    },
    {
        "id": "editorial_portrait",
        "name": "编辑风人像（文生图）",
        "group": "文生图",
        "level": LEVEL_SEMANTIC,
        "kind": "txt2img",
        "images": {"min": 0, "max": 0},
        "desc": "不需要参考图，一句话生成杂志编辑风人像。对应官方 Editorial portrait 示例。",
        "official": "官方示例（智能改写：开）：Create an editorial portrait of …",
        "fields": [
            {"key": "subject", "label": "人物与场景", "required": True, "multiline": True,
             "placeholder": "a botanist in a sunlit greenhouse, surrounded by ferns and delicate orchids"},
            {"key": "light", "label": "光线（英文，可留空）",
             "placeholder": "soft morning backlight"},
        ],
        "builder": _sec_editorial,
    },
]

PRESET_MAP: dict[str, dict[str, Any]] = {p["id"]: p for p in PRESETS}


def presets_payload() -> dict:
    """给前端的预设清单（剔除不可序列化的 builder）。"""
    items = []
    for p in PRESETS:
        items.append({
            "id": p["id"],
            "name": p["name"],
            "group": p["group"],
            "level": p["level"],
            "level_label": LEVEL_LABELS.get(p["level"], p["level"]),
            "kind": p["kind"],
            "images": p["images"],
            "desc": p["desc"],
            "official": p.get("official", ""),
            "fields": p["fields"],
        })
    groups: list[str] = []
    for p in items:
        if p["group"] not in groups:
            groups.append(p["group"])
    return {"groups": groups, "items": items, "levels": LEVEL_LABELS}


# ============================================================
#  提示词组装
# ============================================================

def get_preset(preset_id: str) -> dict:
    preset = PRESET_MAP.get(str(preset_id or "").strip())
    if not preset:
        raise PortraitError(f"未知玩法：{preset_id}")
    return preset


def _fill_fields(preset: dict, values: dict | None) -> dict:
    """按字段定义取值：缺省用 default，必填缺失报错。"""
    values = values or {}
    filled: dict[str, str] = {}
    missing: list[str] = []
    for f in preset["fields"]:
        raw = values.get(f["key"])
        text = (str(raw).strip() if raw is not None else "") or str(f.get("default", "")).strip()
        filled[f["key"]] = text
        if f.get("required") and not text:
            missing.append(f["label"])
    if missing:
        raise PortraitError("请填写：" + "、".join(missing))
    return filled


def check_images(preset: dict, image_count: int, strict: bool = True) -> list[str]:
    """校验参考图数量。

    `strict=True`（提交出图）：数量不够直接报错；
    `strict=False`（提示词预览）：数量不够只提示——用户往往是先选玩法、
    写描述，最后才上传图片，预览不该因此罢工。
    """
    lo, hi = preset["images"]["min"], preset["images"]["max"]
    warnings: list[str] = []
    if preset["kind"] == "txt2img":
        if image_count:
            warnings.append("该玩法为文生图，已忽略上传的参考图。")
        return warnings
    if image_count < lo:
        msg = (f"「{preset['name']}」需要 {lo} 张参考图（当前 {image_count} 张）"
               if hi == lo else
               f"「{preset['name']}」需要 {lo}~{hi} 张参考图（当前 {image_count} 张）")
        if strict:
            raise PortraitError(msg)
        warnings.append(msg)
        return warnings
    if image_count > hi:
        warnings.append(f"「{preset['name']}」最多使用 {hi} 张参考图，多余的上传图已被忽略。")
    return warnings


def build_prompt(preset_id: str, values: dict | None = None,
                 image_count: int = 0, prompt_override: str = "",
                 strict: bool = True) -> dict:
    """组装完整提示词。

    * `prompt_override` 非空时直接采用（用户在预览框里改过，或用了智能改写结果）；
    * `strict=False` 时参考图数量不足只给提示，不抛错（供预览使用）；
    * 返回 `sections` 便于前端展示官方结构的每一段。
    """
    preset = get_preset(preset_id)
    warnings = check_images(preset, image_count, strict=strict)
    filled = _fill_fields(preset, values)
    sections = preset["builder"](filled, max(1, image_count))

    auto_prompt = "\n".join(text for _, text in sections if text).strip()
    prompt = (prompt_override or "").strip() or auto_prompt

    lo, hi = preset["images"]["min"], preset["images"]["max"]
    if preset["kind"] == "edit":
        warnings.append(
            f"{LEVEL_LABELS.get(preset['level'], '')}："
            + ("除目标区域外，画面其余部分应保持与输入图一致。"
               if preset["level"] == LEVEL_APPEARANCE else
               "整张画面会重绘像素，只保证身份与语义一致。")
        )
        warnings.append(
            "官方 2.1 编辑通道固定 CFG=1（负面提示词不生效），"
            "因此「不要什么」必须写进正向提示词里——模板已内置常见禁止项。"
        )
        warnings.append("出图尺寸由 image_1（上传的第 1 张参考图）决定。")
        if hi > 1:
            warnings.append(f"建议使用 {lo}~{hi} 张参考图，官方文档说明 1~3 张效果最佳。")

    return {
        "preset": preset["id"],
        "name": preset["name"],
        "level": preset["level"],
        "level_label": LEVEL_LABELS.get(preset["level"], preset["level"]),
        "kind": preset["kind"],
        "sections": [{"label": label, "text": text} for label, text in sections],
        "prompt": prompt,
        "auto_prompt": auto_prompt,
        "rewritten": bool(prompt_override and prompt_override.strip() != auto_prompt),
        "warnings": warnings,
    }


# ============================================================
#  参考图归一化
# ============================================================

def normalize_reference(img: Image.Image, max_mp: float = 0.8,
                        multiple: int = 64, max_side: int = 1280) -> Image.Image:
    """把参考图压到「像素预算」内。

    2.1 编辑通道的输出尺寸 = image_1 尺寸，而本机（RTX 2060 6GB）实测约
    260 秒/步/百万像素——放任用户传 2K/4K 原图会让一张图跑到几小时。
    这里按最长边与总像素双约束缩放，并把边长对齐到 64 的整数倍。
    """
    w, h = img.size
    if w <= 0 or h <= 0:
        raise PortraitError("参考图尺寸无效")

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
    """PIL 图 → data URL（前端与队列都按 data URL 处理）。"""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def prepare_references(images_data: list[str], max_mp: float = 0.8) -> tuple[list[str], list[str]]:
    """解码 → 归一化 → 重新编码，返回 (data URL 列表, 说明文案)。"""
    refs: list[str] = []
    notes: list[str] = []
    for idx, data in enumerate(images_data, start=1):
        if not data:
            continue
        try:
            img = decode_base64_image(data)
        except Exception as exc:
            raise PortraitError(f"第 {idx} 张参考图解析失败：{exc}") from exc
        try:
            before = img.size
            out = normalize_reference(img, max_mp=max_mp)
            if out is not img:
                img.close()
            refs.append(encode_reference(out))
            after = out.size
            if after != before:
                notes.append(f"image_{idx}：{before[0]}×{before[1]} → {after[0]}×{after[1]}（像素预算内）")
            out.close()
        except PortraitError:
            raise
        except Exception as exc:
            raise PortraitError(f"第 {idx} 张参考图处理失败：{exc}") from exc
    return refs, notes


# ============================================================
#  出图 payload（复用既有出图队列，不触碰漫画代码）
# ============================================================

def build_generation_payload(*, preset_id: str, values: dict | None = None,
                             images_data: list[str] | None = None,
                             prompt_override: str = "",
                             steps: int = 8, seed: int = -1, count: int = 1,
                             width: int = 768, height: int = 1024,
                             max_mp: float = 0.8) -> dict:
    """构造出图任务参数。

    编辑类玩法走 `mode=img2img` + `fusion=True`：
    引擎会用 `TextEncodeQwenImage21` 的官方多参考图通道（CFG=1、denoise=1、空 latent），
    与传统图生图完全分开，互不影响。
    """
    preset = get_preset(preset_id)
    images_data = [d for d in (images_data or []) if d]
    # 先按「用户实际传了几张」做校验与提示，再截断到玩法上限——
    # 否则超量上传会被静默丢弃，用户不知道少了图。
    over_warnings: list[str] = []
    if preset["kind"] == "edit":
        over_warnings = check_images(preset, len(images_data))
        images_data = images_data[: preset["images"]["max"]]

    info = build_prompt(preset_id, values, len(images_data), prompt_override)
    has_override = bool(prompt_override and prompt_override.strip())
    prompt = info["prompt"]

    payload: dict[str, Any] = {
        "prompt": prompt,
        # 编辑通道固定 CFG=1，负面提示词不生效；这里留空避免误导
        "negative_prompt": "",
        "width": int(width),
        "height": int(height),
        "steps": int(steps),
        "guidance_scale": 1.0 if preset["kind"] == "edit" else float(settings.default_guidance),
        "seed": int(seed),
        "count": max(1, int(count)),
        "strength": 1.0,
        "channel": "auto",
        # 按玩法归档（与漫画按项目归档同一套机制）
        "output_subdir": f"portrait/{datetime.now().strftime('%Y-%m-%d')}",
        "mode": "txt2img",
    }

    warnings = over_warnings + list(info["warnings"])
    if preset["kind"] == "edit":
        refs, notes = prepare_references(images_data, max_mp=max_mp)
        if not refs:
            raise PortraitError(f"「{preset['name']}」需要上传参考图")
        payload.update({
            "mode": "img2img",
            "fusion": True,           # → 引擎走多参考图编辑通道
            "ref_images": refs,
            "image": None,
        })
        # 输出尺寸跟随 image_1，估算按参考图实际尺寸走
        try:
            first = decode_base64_image(refs[0])
            payload["width"], payload["height"] = first.size
            first.close()
        except Exception:
            pass
        warnings.extend(notes)

    payload["portrait"] = {
        "preset": preset["id"],
        "name": preset["name"],
        "level": preset["level"],
        "image_count": len(images_data),
        "rewritten": has_override,
    }
    return {"payload": payload, "info": info, "warnings": warnings}


# ============================================================
#  智能改写（官方文档明确建议：图像编辑必须做提示词改写才稳定）
# ============================================================

REWRITE_SYSTEM = (
    "你是 Qwen-Image 2.1 图像编辑的提示词工程师。"
    "你只输出最终提示词本身，不解释、不分点、不加引号、不写前缀。"
)

_REWRITE_RULES = """请把用户给出的简短描述，改写为一条可直接用于 Qwen-Image 2.1 图像编辑的完整提示词。

必须遵守官方文档给出的结构，按顺序写成连贯的段落：
1. 先锁定身份：以 image_1 中人物为身份基准，面部五官比例、脸型与辨识特征严格沿用原图不变；
2. 再描述需要变更的内容，具体到主体、材质、颜色、数量与关系；
3. 说明新增元素的光线与阴影遵循原图统一的真实光照逻辑；
4. 局部编辑必须明确写出「保持原图的取景、构图、缩放级别与人物在画面中的大小位置不变，不作重新构图或整体重绘」；场景重绘则写明重建画面的构图与镜头；
5. 结尾写禁止项：画面中不出现任何可读文字、水印或 logo；修复类还要写明不虚构、不美化面部；
6. 只输出提示词本身，总长度不超过 400 字，不要换行分点。"""


def build_rewrite_request(*, preset_id: str, values: dict | None, image_count: int,
                          base_prompt: str = "") -> str:
    preset = get_preset(preset_id)
    filled = _fill_fields(preset, values)
    user_bits = "；".join(f"{f['label']}={filled[f['key']]}" for f in preset["fields"] if filled.get(f["key"]))
    parts = [
        f"玩法：{preset['name']}（{preset['desc']}）",
        f"编辑层级：{LEVEL_LABELS.get(preset['level'], preset['level'])}",
        f"参考图数量：{image_count} 张" + (f"（image_1…image_{image_count}）" if image_count else ""),
        f"用户填写的变量：{user_bits or '（无）'}",
    ]
    if base_prompt.strip():
        parts.append(f"当前模板提示词（可优化，但不得改变用户意图）：\n{base_prompt.strip()}")
    return "\n".join(parts)


def _clean_rewrite(text: str) -> str:
    out = (text or "").strip()
    for prefix in ("```", "提示词：", "Prompt:", "prompt:", "最终提示词："):
        out = out.replace(prefix, "")
    out = out.replace("```", "").strip()
    # 模型偶尔会把解释写在后面，取最后一个空行前的正文
    return out.strip().strip('"“”').strip()


class RewriteTaskManager:
    """智能改写任务：单并发后台执行，前端轮询（与漫画的 AI 任务同一套体验）。"""

    MAX_KEEP = 20

    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._boot()

    def _boot(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._loop, name="portrait-rewrite", daemon=True)
        self._worker.start()

    def submit(self, *, preset_id: str, values: dict | None, image_count: int,
               base_prompt: str = "") -> dict:
        self._boot()
        task_id = "prw_" + uuid.uuid4().hex[:12]
        task = {
            "id": task_id,
            "kind": "portrait_rewrite",
            "label": "AI 改写提示词",
            "preset": preset_id,
            "status": "queued",
            "elapsed": 0.0,
            "message": "已排队",
            "result": None,
            "error": None,
            "created_at": time.time(),
        }
        with self._lock:
            self._tasks[task_id] = task
            self._order.append(task_id)
            while len(self._order) > self.MAX_KEEP:
                self._tasks.pop(self._order.pop(0), None)
        self._queue.put((task_id, preset_id, values, image_count, base_prompt))
        return dict(task)

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def list_recent(self, limit: int = 10) -> list[dict]:
        with self._lock:
            items = [self._tasks[i] for i in reversed(self._order) if i in self._tasks]
        return [dict(t) for t in items[:limit]]

    def running(self) -> Optional[dict]:
        with self._lock:
            for i in reversed(self._order):
                t = self._tasks.get(i)
                if t and t["status"] in ("queued", "running"):
                    return dict(t)
        return None

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task["status"] not in ("queued", "running"):
                return False
            task["status"] = "canceled"
            task["message"] = "已取消"
        try:
            text_engine.cancel()
        except Exception:
            pass
        return True

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            task_id, preset_id, values, image_count, base_prompt = item
            with self._lock:
                task = self._tasks.get(task_id)
            if not task or task["status"] == "canceled":
                continue
            self._run(task, preset_id, values, image_count, base_prompt)

    def _run(self, task: dict, preset_id: str, values: dict | None,
             image_count: int, base_prompt: str) -> None:
        with self._lock:
            task["status"] = "running"
            task["message"] = "本地文本模型推理中（CPU 推理较慢，请耐心等待）"
            task["started_at"] = time.time()

        def on_progress(seconds: float) -> None:
            with self._lock:
                task["elapsed"] = seconds
                task["message"] = f"本地模型推理中，已用 {int(seconds)} 秒"

        try:
            request = build_rewrite_request(
                preset_id=preset_id, values=values,
                image_count=image_count, base_prompt=base_prompt,
            )
            text = text_engine.generate(
                request,
                system=REWRITE_SYSTEM + _REWRITE_RULES,
                max_length=settings.llm_max_length,
                temperature=0.6,
                on_progress=on_progress,
            )
            cleaned = _clean_rewrite(text)
            if not cleaned:
                raise LocalTextError("模型没有返回内容，请重试或改用手写提示词")
            self._finish(task, result={"prompt": cleaned})
        except LocalTextError as exc:
            self._finish(task, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，错误要透出给界面
            import traceback

            print(f"[人像改写失败]\n{traceback.format_exc()}", flush=True)
            self._finish(task, error=f"{exc.__class__.__name__}: {exc}")

    def _finish(self, task: dict, result: Any = None, error: str = "") -> None:
        with self._lock:
            if task["status"] == "canceled":
                return
            task["status"] = "failed" if error else "done"
            task["error"] = error or None
            task["result"] = result
            task["elapsed"] = round(task.get("elapsed") or 0, 1)
            task["message"] = error or f"完成，用时 {task['elapsed']} 秒"


rewrite_tasks = RewriteTaskManager()


def llm_status() -> dict:
    st = text_engine.status()
    st["speed_note"] = (
        "智能改写复用 Qwen-Image 自带的 Qwen3-VL-8B 文本编码器（无需额外下载模型）。"
        "本机为 CPU 文本推理，改写一次约 1~4 分钟。"
    )
    st["official_note"] = (
        "官方文档指出：图像编辑不做提示词改写时结果容易不稳定，建议开启改写后再出图。"
    )
    return st


def output_subdir() -> str:
    """人像编辑的出图目录（与漫画按项目归档同一套机制）。"""
    return f"portrait/{datetime.now().strftime('%Y-%m-%d')}"
