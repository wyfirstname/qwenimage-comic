# -*- coding: utf-8 -*-
"""画风锁。

为什么叫「锁」而不是「风格提示词」
--------------------------------
分镜最怕的不是单格难看，而是**相邻两格不像同一部片**：第一格是马卡龙色，
第三格突然变成莫兰迪灰；第一格圆脸，第四格变尖脸。所以每个画风在这里给出的
不是一句可以随手改写的形容，而是一组**必须在每条提示词里逐字复现**的字段
（render / palette / line / negative）。同一部片的所有格子共用同一份拷贝，
画风才锁得住。

画风的信息量分级（选型时最重要的一条）
------------------------------------
    stick_bold（火柴人）  <  dango_ink（单色线稿）  <  dango_soft（上色）

人物细节越少，跨帧漂移越不容易被看出来。团子的刘海走向、瞳色深浅、衣服花纹，
每一帧都在飘，几秒就会让人觉得「这不是我的崽」；而一根线条变粗没人会认为是
身份漂移。所以**低显存 / 弱模型下，火柴人反而是最稳的载体**，这不是妥协。

每个条目字段
------------
label       展示名
ratio       头身比（写进提示词，比「chibi」这种词有效得多）
render      画风正文
palette     色调锁
line        线条语言
negative    该画风的负面（仅文生图通道生效，见 shots.py 说明）
subject     默认主体（用户不填时兜底）
load        相对「显存/模型友好度」的档位，供界面排序提示
"""

from __future__ import annotations

from typing import Optional


# 各画风共用的基础负面：只放「绝对不要」的硬伤，不放风格倾向，
# 避免和下面每个画风自己的 negative 打架。
BASE_NEGATIVE = (
    "写实照片，真人，真实人体比例，成年人体型，解剖肌肉细节，写实皮肤纹理，"
    "多余的肢体，多余的手指，畸形的手，错位的手指，"
    "文字，字幕，水印，logo，签名，边框，拼图，分屏，"
    "低清晰度，模糊，噪点，严重压缩伪影"
)


STYLES: dict[str, dict] = {
    # ---------------- 团子系（本平台主角，Q 版一致性要求最高）----------------
    "dango_soft": {
        "label": "团子·软糖",
        "ratio": "2.5 头身",
        "render": "平涂上色搭配柔和的二分阴影，色彩低饱和、偏奶油质感，边缘干净无杂线",
        "palette": "马卡龙色系（奶白 / 樱粉 / 薄荷绿 / 奶油黄），整体明亮、不用高对比",
        "line": "深棕色细描线，线条圆润、转角处不出现锐角",
        "negative": "暗黑，血腥，恐怖，塑料感，3D 渲染，硬边高光",
        "subject": "一个圆滚滚的团子角色，头大身小，四肢短圆，脸颊有淡粉色腮红",
        "load": 3,
    },
    "dango_ink": {
        "label": "团子·墨线",
        "ratio": "2.5 头身",
        "render": "黑白线稿，纯线描无上色，留白干净，像漫画分镜草稿",
        "palette": "只有黑与白，允许少量网点灰",
        "line": "黑色粗细变化的墨线，笔锋略带抖动的手绘感",
        "negative": "彩色，上色，渐变，水彩，油画，厚涂",
        "subject": "一个圆滚滚的团子角色，头大身小，四肢短圆",
        "load": 2,
    },
    "dango_pop": {
        "label": "团子·潮玩",
        "ratio": "3 头身",
        "render": "高饱和潮玩海报风，大色块撞色，硬边阴影，边缘带一点印刷网点",
        "palette": "高饱和撞色（亮橙 / 电光紫 / 柠檬黄 / 湖蓝），允许粗描边",
        "line": "黑色粗描边，线宽统一，像贴纸",
        "negative": "低饱和，灰调，柔和，水彩，写实材质",
        "subject": "一个圆滚滚的团子角色，头大身小，造型利落",
        "load": 3,
    },
    # ---------------- 火柴人系（低信息量、最稳，适合先跑通链路）----------------
    "stick_bold": {
        "label": "火柴人·粗线",
        "ratio": "3 头身",
        "render": "极简火柴人，纯色块背景，无透视无阴影，像信息图插画",
        "palette": "最多三色：黑线条 + 一个主色 + 一个点缀色，背景为单一浅色块",
        "line": "等宽粗黑线，圆头线帽，关节处为实心圆点",
        "negative": "五官细节，服装褶皱，头发细节，纹理，渐变，复杂背景",
        "subject": "一个火柴人，圆形头部，粗线条四肢，无五官细节",
        "load": 1,
    },
    "stick_neon": {
        "label": "火柴人·霓虹",
        "ratio": "3 头身",
        "render": "深色底 + 发光线条，赛博霓虹感，像夜里的广告牌草图",
        "palette": "深蓝黑背景 + 青与品红霓虹光，允许辉光溢出",
        "line": "等宽发光线条，带柔和辉光，无内部细节",
        "negative": "白天，明亮背景，纯白底，写实材质，复杂纹理",
        "subject": "一个火柴人，圆形头部，发光线条四肢",
        "load": 2,
    },
    # ---------------- 通用 ----------------
    "chalk": {
        "label": "粉笔黑板",
        "ratio": "3 头身",
        "render": "黑板上的粉笔画，颗粒感明显，笔触有粉尘飞白",
        "palette": "深墨绿黑板底 + 白 / 淡黄 / 淡粉粉笔色",
        "line": "粉笔质感的粗线，边缘有颗粒毛刺",
        "negative": "光滑线条，矢量感，塑料质感，鲜艳渐变",
        "subject": "一个简笔角色，粉笔线条勾勒，造型概括",
        "load": 1,
    },
}

DEFAULT_STYLE = "dango_soft"


def style_of(style_id: Optional[str]) -> dict:
    """取画风定义，未知 id 回落到默认，永不抛错。"""
    return STYLES.get(str(style_id or ""), STYLES[DEFAULT_STYLE])


def style_id_of(style_id: Optional[str]) -> str:
    key = str(style_id or "")
    return key if key in STYLES else DEFAULT_STYLE


def style_options() -> list[dict]:
    """给前端的画风列表，按「模型友好度」从低到高排。

    低信息量排前面是有意的：显存紧张或第一次跑通链路时，应当先选火柴人。
    """
    items = [
        {"id": k, "label": v["label"], "ratio": v["ratio"],
         "render": v["render"], "palette": v["palette"],
         "subject": v["subject"], "load": v["load"]}
        for k, v in STYLES.items()
    ]
    return sorted(items, key=lambda x: (x["load"], x["id"]))


def negative_of(style_id: Optional[str]) -> str:
    """画风专属负面（文生图通道用；编辑通道负面不生效，见 shots.py）。"""
    s = style_of(style_id)
    return f"{BASE_NEGATIVE}，{s['negative']}"
