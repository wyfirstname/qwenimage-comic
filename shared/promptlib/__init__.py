# -*- coding: utf-8 -*-
"""导演层模板库（promptlib）。

把 `stickman-video-director` 那套提示词工程**编译成数据**，而不是把上百 KB
的规则直接喂给模型。

为什么必须编译
--------------
本地跑的是 Qwen3-VL-8B（复用 Qwen-Image 的文本编码器）。它遵守长规则的能力
很弱：规则一多，模型会挑几条执行、剩下的静默忽略，而且输出格式会在第 3、4
条之后开始漂。所以这里的分工是：

    LLM 只产出「节拍 / 动作 / 景别 / 镜头 / 旁白」这五项最小语义
    其余全部（画风锁、构图、光线、禁令、字段顺序）由本包拼装

这样即使模型只出半句话，成片规格也不会崩——因为规格根本不经它手。

三个模块
--------
styles.py      画风锁：一部片内必须逐字复现的风格字段
storyboard.py  叙事模板 + JSON 解析 + 校验修复 + 无 LLM 时的规则降级
shots.py       七段式首帧提示词拼装 + 镜头运动定义（Ken Burns 与视频模型共用）

分层约束
--------
本包属于 shared，**不得** import engine / apps。/ 它只提供纯函数与数据，
不碰模型、不碰磁盘、不碰配置。
"""

from shared.promptlib import shots, storyboard, styles

__all__ = ["styles", "storyboard", "shots"]
