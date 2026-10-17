# -*- coding: utf-8 -*-
"""漫画工作室的默认提示词常量。

单独成模块是为了让「数据层迁移」和「业务服务」共用同一份文本，
避免默认反向提示词在两处各写一遍后走样。

注意：这里是**反向提示词**（negative prompt），用来压制肢体缺陷、
五官变形、奇怪表情等问题，保证人物画得「正常」。
"""

from __future__ import annotations

# ------------------------------------------------------------
#  一、项目级默认反向提示词（画面通用）
# ------------------------------------------------------------
# 本地 Qwen-Image GGUF 只有 8 步采样，靠提示词很难救回结构错误，
# 因此把「畸形 / 多指 / 表情崩坏」这类高频问题写进默认反提示词。
DEFAULT_PROJECT_NEGATIVE = (
    "worst quality, low quality, blurry, jpeg artifacts, out of focus, "
    "watermark, signature, logo, ui, text, letters, words, "
    "speech bubble, caption, subtitles, frame, border, collage, split screen, "
    "bad anatomy, bad proportions, deformed, disfigured, mutation, amputee, "
    "extra limbs, extra arms, extra legs, extra heads, missing limbs, fused limbs, "
    "bad hands, extra fingers, missing fingers, fused fingers, malformed hands, "
    "extra hands, twisted fingers, "
    "deformed face, distorted face, asymmetrical eyes, cross-eyed, lazy eye, "
    "weird facial expression, grimace, ugly face, exaggerated expression, "
    "bad teeth, deformed mouth, long neck, broken neck, "
    "duplicate person, cloned face, multiple views, character sheet"
)

# ------------------------------------------------------------
#  二、人物专用默认反向提示词（角色形象图 + 有角色的分镜）
# ------------------------------------------------------------
# 用户诉求：角色设置默认就要带反提示词，保证人物正确，
# 不要出现奇怪的表情或肢体缺陷。
DEFAULT_CHAR_NEGATIVE = (
    "weird facial expression, grimace, exaggerated expression, ugly face, "
    "distorted face, deformed face, asymmetrical eyes, cross-eyed, lazy eye, "
    "deformed mouth, bad teeth, weird smile, creepy smile, "
    "extra fingers, missing fingers, fused fingers, malformed hands, bad hands, "
    "extra hands, extra arms, extra limbs, missing limbs, fused limbs, "
    "twisted body, broken joints, bad proportions, deformed body, disfigured, "
    "multiple heads, two faces, duplicate person, "
    "long neck, thin neck, floating limbs"
)

# ------------------------------------------------------------
#  三、各出图场景追加的反向提示词
# ------------------------------------------------------------
# 分镜：人物 + 环境，禁字（漫画文字由 PIL 排版绘制，不能让模型画）
PANEL_NEGATIVE_EXTRA = (
    "text, letters, words, speech bubble, speech balloon, caption, subtitles, "
    "watermark, signature, logo, ui, frame, border, comic page layout, "
    "low quality, blurry, jpeg artifacts, "
    "extra limbs, bad hands, deformed face, extra fingers, fused fingers, "
    "weird facial expression, grimace, asymmetrical eyes, bad proportions"
)

# 场景概念图：纯环境，不能有人
SCENE_NEGATIVE_EXTRA = (
    "people, person, character, human figure, crowd, portrait, face, hands, "
    "text, letters, words, watermark, signature, logo, ui, frame, border, "
    "low quality, blurry, jpeg artifacts"
)

# 角色形象图：单人立绘，不能有多人 / 分栏 / 文字
CHARACTER_NEGATIVE_EXTRA = (
    "multiple people, two people, crowd, group shot, couple, "
    "text, letters, words, watermark, signature, logo, ui, frame, border, "
    "collage, split screen, multiple views, character sheet grid, "
    "low quality, blurry, jpeg artifacts, deformed face, extra limbs, extra fingers"
)

# 正向提示词里用于「把人画对」的通用短语（8 步采样下也需要显式强调）
ANATOMY_POSITIVE = (
    "correct human anatomy, natural pose, well-proportioned body, "
    "anatomically correct hands with five fingers, natural facial expression"
)
