# -*- coding: utf-8 -*-
"""时间背景预设：每个预设固定「色调 + 光照 + 负面词」，全素材集共用。

一致性第一层就在这里：同一素材集内，每张图（基准图、角色、道具、场景）
的提示词都强制拼上同一个 preset 的 tone 片段，负面词同样统一。
prompt 片段用英文（Qwen-Image 对英文色调词响应更稳），UI 展示用中文名。
"""

from __future__ import annotations

TIME_PRESETS: dict[str, dict] = {
    "dawn": {
        "id": "dawn",
        "name": "清晨",
        "hour": "06:00",
        "prompt": ("soft cool morning light, pale blue and misty pink tones, "
                    "gentle dawn glow on horizon, delicate long shadows, fresh airy atmosphere"),
        "negative": "harsh midday sun, dark night, saturated orange sunset",
        "swatch": ["#A8C8E8", "#D8C4E0", "#F2D5C4", "#88A8CC"],
    },
    "noon": {
        "id": "noon",
        "name": "正午",
        "hour": "12:00",
        "prompt": ("bright neutral daylight, clear blue sky, strong overhead sunlight, "
                    "crisp high-contrast shadows, vivid saturated colors"),
        "negative": "night, sunset colors, dim moody lighting, fog",
        "swatch": ["#5AA7E8", "#8FD0F5", "#F5F0DC", "#3B7BD8"],
    },
    "dusk": {
        "id": "dusk",
        "name": "黄昏",
        "hour": "18:00",
        "prompt": ("warm orange and magenta sunset glow, golden rim light, "
                    "long soft shadows, amber highlights fading into violet dusk"),
        "negative": "harsh midday sun, cold blue moonlight, overcast grey sky",
        "swatch": ["#F08300", "#E85A7B", "#8E5AA8", "#F5B041"],
    },
    "night": {
        "id": "night",
        "name": "夜晚",
        "hour": "22:00",
        # 注意：旧措辞 "deep blue moonlight, cool desaturated shadows, strong chiaroscuro,
        # vast cold darkness" 会把 2.1 蒸馏模型（CFG=1、8步）推进采样崩溃，整图烧成
        # 近纯黑（实测 2026-10-25，多组对照实验定位）。改用温和夜色词。
        "prompt": ("night, bright moonlight, deep blue night sky, "
                    "warm lantern glow, gentle contrast, details visible in shadows"),
        "negative": "bright daylight, clear blue sky, warm sunset tones",
        "swatch": ["#1F2E5C", "#2851A3", "#565B63", "#F5B041"],
    },
    "overcast": {
        "id": "overcast",
        "name": "阴天",
        "hour": "全天",
        "prompt": ("soft diffused overcast light, muted grey-blue palette, "
                    "low contrast, gentle fog, no visible sun, quiet melancholic mood"),
        "negative": "strong direct sunlight, vivid saturated colors, blue sky",
        "swatch": ["#8C9199", "#A8B8C4", "#C3B091", "#6B7A8A"],
    },
    "snow": {
        "id": "snow",
        "name": "雪天",
        "hour": "全天",
        "prompt": ("cold snowy daylight, white and pale blue palette, "
                    "soft bounced light from snow, breath-fog atmosphere, serene winter mood"),
        "negative": "warm summer colors, green foliage, desert, sunset glow",
        "swatch": ["#E8EEF5", "#C9D8E8", "#9FB8D0", "#DDE5EE"],
    },
}


def get_preset(preset_id: str) -> dict:
    if preset_id not in TIME_PRESETS:
        raise KeyError(f"未知时间背景：{preset_id}（可选：{', '.join(TIME_PRESETS)}）")
    return TIME_PRESETS[preset_id]


# ------------------------------------------------------------
#  四季变体：场景在同一时间背景（光照色温）下换季。
#  季节改变的是内容（植被/雪/花）与部分环境色，不改变色温与光源方向，
#  所以季节卡的色调校验阈值在流水线里放宽 1.6 倍（合法偏色，不是跑色）。
# ------------------------------------------------------------
SEASONS: dict[str, dict] = {
    "spring": {"name": "春", "prompt": ("fresh spring atmosphere, tender green foliage, "
                                        "pink blossoms drifting, soft rain-washed light, new growth")},
    "summer": {"name": "夏", "prompt": ("lush summer atmosphere, deep green foliage, "
                                        "vivid flowers, bright warm sunlight, clear air")},
    "autumn": {"name": "秋", "prompt": ("autumn atmosphere, golden and crimson foliage, "
                                        "harvest light, falling leaves, mellow warm haze")},
    "winter": {"name": "冬", "prompt": ("winter atmosphere, thick snow cover, bare branches, "
                                        "icicles, cold pale light, breath fog")},
}
SEASON_ORDER = ["spring", "summer", "autumn", "winter"]


# ------------------------------------------------------------
#  性别变体：主角默认男女各一版（同一名字展开成两条）。
#  性别词进提示词前半段——2.1 对前置主体词最敏感，写在描述后面容易被忽略。
# ------------------------------------------------------------
GENDERS: dict[str, dict] = {
    "male": {"name": "男", "prompt": ("male character, masculine build, "
                                      "male facial features, male body proportions")},
    "female": {"name": "女", "prompt": ("female character, feminine build, "
                                        "female facial features, female body proportions")},
}
GENDER_ORDER = ["male", "female"]


def entry_genders(entry: dict) -> list[str]:
    """方案条目 → 要展开的性别列表。

    显式 genders 优先；否则按名字猜：含「女主角」→ 只女；含「主角」→ 男女各一；
    其余（NPC/导师/商人）默认男，用户可在方案编辑里补勾。
    """
    raw = entry.get("genders")
    if raw:
        picked = [g for g in GENDER_ORDER if g in raw]
        return picked or ["male"]
    name = str(entry.get("name") or "")
    if "女主" in name:
        return ["female"]
    if entry.get("protagonist") or "主角" in name:
        return ["male", "female"]
    return ["male"]



# ------------------------------------------------------------
#  武器等级：固定四级，等级词直接描述材质与光效（模型可画）。
# ------------------------------------------------------------
WEAPON_TIERS: dict[str, dict] = {
    "common":    {"name": "普通", "prompt": ("simple humble equipment, rough wood and iron, "
                                             "visible wear, no glow, practical design")},
    "fine":      {"name": "精良", "prompt": ("well-forged steel equipment, fine craftsmanship, "
                                             "clean polish, subtle engraved pattern")},
    "epic":      {"name": "史诗", "prompt": ("enchanted equipment, glowing runes carved along the blade, "
                                             "ornate metalwork, faint magical aura")},
    "legendary": {"name": "传说", "prompt": ("legendary artifact, radiant divine glow, elaborate "
                                             "gold filigree, gemstones, awe-inspiring presence")},
}
TIER_ORDER = ["common", "fine", "epic", "legendary"]


# ------------------------------------------------------------
#  怪物等级：不再是一锅「野怪」，按战斗定位分四级。
#  等级同时决定画幅——体积感是等级的主要视觉信号，杂兵用 640 省显存，
#  终局 1024 顶到 6GB 卡能稳出的上限。size 后面的 note 就是「为什么这么大」。
# ------------------------------------------------------------
MONSTER_TIERS: dict[str, dict] = {
    "minion": {
        "name": "杂兵", "size": (640, 640),
        "prompt": ("small minion creature, low threat level, simple readable silhouette, "
                   "hunched scavenger proportions, single accent color, plain hide"),
        "note": "成群刷的杂兵，640 见方足够又省显存；轮廓要一眼能认，细节多了反而糊",
    },
    "elite": {
        "name": "精英", "size": (768, 768),
        "prompt": ("elite variant creature, heavier armored hide, old battle scars, "
                   "glowing eyes, clearly larger and meaner than a common one"),
        "note": "带甲带光效的精英，768 才留得住材质与伤疤细节",
    },
    "boss": {
        "name": "首领", "size": (896, 896),
        "prompt": ("boss-tier behemoth, towering imposing scale, heavy horned silhouette, "
                   "dramatic menacing presence, powerful planted stance"),
        "note": "单个 BOSS 要撑满屏幕，896 保证体积感不被压糊",
    },
    "worldboss": {
        "name": "终局", "size": (1024, 1024),
        "prompt": ("world-ending colossus, colossal otherworldly anatomy, cataclysmic aura, "
                   "silhouette dwarfs the frame, epic apocalyptic scale"),
        "note": "终局演出用，1024 是 2060 6GB 能稳定出图的上限，再大就开始崩结构",
    },
}
MONSTER_TIER_ORDER = ["minion", "elite", "boss", "worldboss"]


# ------------------------------------------------------------
#  视图锁：全项目统一 2.5D 斜 45°（等距斜视）。
#
#  以前每类各画各的视角——角色正面立绘、宠物侧前、场景横版 16:9，
#  单看都好看，拼进一张画面立刻露馅：人物是平的，地面是斜的，影子对不上。
#  2.5D 的关键不是「斜」，而是**所有素材共用同一台相机**：
#  俯角 45°、正交等比、脚底贴地、Q 版三头身（斜视角下头身比太小会糊成一团）。
#  这一段会拼进所有类的提示词，负面词里禁掉正面/平视/俯视/畸变。
# ------------------------------------------------------------
VIEW_LOCK: dict = {
    "id": "iso45",
    "name": "2.5D 斜 45°",
    "prompt": ("2.5D isometric game art, 45-degree top-down camera, slight downward tilt, "
               "consistent orthographic scale, chibi proportions about 3 heads tall, "
               "full body visible, feet planted at the bottom of the frame"),
    "negative": ("front orthographic view, straight-on eye-level view, flat top-down view, "
                 "fisheye, extreme foreshortening, tilted horizon, cast shadow on background, "
                 "multiple characters, text, watermark"),
    # 动画帧统一画布：192 见方，脚底锚点落在 (96,178)——留 14px 给脚部阴影/裙摆
    "cell": (192, 192),
    "anchor": (96, 178),
}

# 八方向：生成 4~8 个基向，左半侧由镜像得到（大话西游/梦幻那套老做法）
DIRECTIONS: dict[str, dict] = {
    "s":  {"name": "正面", "short": "S",  "prompt": "facing the camera (south, toward viewer)"},
    "se": {"name": "右下", "short": "SE", "prompt": ("facing lower-right (southeast), "
                                                     "three-quarter front view")},
    "e":  {"name": "右侧", "short": "E",  "prompt": "facing right (east), three-quarter side view"},
    "ne": {"name": "右上", "short": "NE", "prompt": ("facing upper-right (northeast), "
                                                     "three-quarter back view")},
    "n":  {"name": "背面", "short": "N",  "prompt": "facing away from camera (north), full back view"},
    "nw": {"name": "左上", "short": "NW", "prompt": ("facing upper-left (northwest), "
                                                     "three-quarter back view")},
    "w":  {"name": "左侧", "short": "W",  "prompt": "facing left (west), three-quarter side view"},
    "sw": {"name": "左下", "short": "SW", "prompt": ("facing lower-left (southwest), "
                                                     "three-quarter front view")},
}

# 动作：站立（呼吸循环）+ 行走（八帧）+ 奔跑（八帧）。
# 主角与宠物共用这一套三段 clip——同一台相机、同一 cell、同一锚点，
# 引擎侧只要按 clip 切行就能播。八帧是大话西游那代表准帧数，
# 六帧步态会明显跳步，八帧才圆得上；奔跑多两拍滞空。
CLIPS: dict[str, dict] = {
    "idle": {
        "name": "站立", "fps": 6,
        "poses": ["standing neutral, arms relaxed at sides, weight even",
                  "slight breath in, chest lifts, shoulders rise a little",
                  "breath out, shoulders settle back down",
                  "tiny weight shift, returning to the neutral stance"],
    },
    "walk": {
        "name": "行走", "fps": 10,
        "poses": ["left foot forward contact, right arm swung forward, body lowest",
                  "double support, weight settling onto the left foot, slight recoil",
                  "passing pose, right leg lifting off, body rising",
                  "right leg swinging through under the body, highest point of the step",
                  "right foot forward contact, left arm swung forward, body lowest",
                  "double support, weight settling onto the right foot, slight recoil",
                  "passing pose, left leg lifting off, body rising",
                  "left leg swinging through under the body, highest point of the step"],
    },
    "run": {
        "name": "奔跑", "fps": 14,
        "poses": ["left foot strike contact, torso leaning forward, arms pumping hard",
                  "left leg absorbing impact, knee deeply bent, body at its lowest",
                  "push off the left foot, right knee driving forward, body rising",
                  "airborne, both feet off the ground, legs scissored wide",
                  "right foot strike contact, torso leaning forward, opposite arm forward",
                  "right leg absorbing impact, knee deeply bent, body at its lowest",
                  "push off the right foot, left knee driving forward, body rising",
                  "airborne, both feet off the ground, legs scissored the other way"],
    },
}
CLIP_ORDER = ["idle", "walk", "run"]

# ------------------------------------------------------------
#  动画档位：决定生成几个朝向、每段几帧——直接决定要烧多少张图，UI 上明码标价。
#  三段（站立 4 / 行走 8 / 奔跑 8 = 每向 20 帧）是主角与宠物的标配；
#  off 档仍可回到「只出一张立绘」的老行为。
# ------------------------------------------------------------
ANIM_LEVELS: dict[str, dict] = {
    "off": {
        "name": "静态单帧", "dirs": [], "mirror": {}, "idle": 0, "walk": 0, "run": 0,
        "note": "只出一张立绘，不生成动画帧（老项目兼容）",
    },
    "lite": {
        "name": "轻量 · 3 向", "dirs": ["s", "e"],
        "mirror": {"w": "e"}, "idle": 4, "walk": 8, "run": 8,
        "note": "正面 + 右侧（左侧镜像），40 张/角色；2.5D 起步价，站立/走/跑齐",
    },
    "standard": {
        "name": "标准 · 5 向", "dirs": ["s", "se", "e", "ne", "n"],
        "mirror": {"w": "e", "nw": "ne", "sw": "se"}, "idle": 4, "walk": 8, "run": 8,
        "note": "含斜向与背面，100 张/角色；八向走位不穿帮，实战够用",
    },
    "full": {
        "name": "全向 · 8 向", "dirs": ["s", "se", "e", "ne", "n", "nw", "w", "sw"],
        "mirror": {}, "idle": 4, "walk": 8, "run": 8,
        "note": "八向全实拍不镜像，160 张/角色；左右不对称的角色（单边披风/武器）必须选这个",
    },
}
ANIM_LEVEL_ORDER = ["off", "lite", "standard", "full"]
ANIM_KINDS = ("character", "pet")          # 需要动画帧的类
ANIM_DEFAULT_LEVEL = "lite"

# ------------------------------------------------------------
#  三层分布（13.0）：身份卡 → 关键姿态 → 插值补帧
#
#  12.x 是「逐帧文生图」：每帧都从零采样，模型的概率性让 40 帧像 40 个人，
#  而守门量的是黑图/纯色/跑色这类全局统计量，量不出「不像」——所以一直
#  「都过关但看着不对」。13.0 改成分层，把「身份」这件事从概率问题变成
#  一次性的确定问题：
#
#    ① 身份卡（本段）：一张图里并排画同一角色的 3~8 个朝向。作用不是拿到
#       能用的帧（实测模型会躲纯侧身，给的是「正面 + 微转」），而是给后面
#       所有帧一个共享的服装 / 发型 / 配色 / 头身比锚；
#    ② 关键姿态（每向 10 张）：站立 2 + 行走 4 + 奔跑 4，用身份卡对应那一栏
#       + 基准图做参考图跑图生图，身份被参考图焊住，不再逐帧重新掷骰子；
#    ③ 插值补帧（不烧显存）：关键帧之间的「下沉 / 抬起」按传统二维步态做法
#       用整帧垂直位移补出来（contact 压低、passing 抬高）。这不是光学流插值，
#       只是把「身体起伏」这一维还原——四拍步态的骨架仍是四拍。
#
#  出图次数：lite 40 帧 → 身份卡 1 + 关键姿态 20 = 21 次；
#            full 160 帧 → 身份卡 2 + 关键姿态 80 = 82 次。
# ------------------------------------------------------------
TURNAROUND_CELL = 320          # 身份卡单栏宽（高 640，2:1 竖条，正好装一个全身 Q 版）
TURNAROUND_HEIGHT = 640
# 每个档位要出几张身份卡、每张几栏、每栏是哪个朝向。
# 超过 4 栏就分张（一张图里塞 8 个人，每栏只剩 200px 宽，脸就糊了）。
TURNAROUND_SETS: dict[str, list[list[str]]] = {
    "off": [],
    "lite": [["s", "se", "e"]],
    "standard": [["s", "se", "e", "n"]],
    "full": [["s", "se", "e", "ne"], ["n", "nw", "w", "sw"]],
}
# 档位要、身份卡却没实拍的朝向：拿哪一栏当参考 + 提示词里补的朝向词。
# standard 的 ne（右上/斜后）最能体现「转过去」的中间态，单靠后背栏只能近似，
# 所以这里显式写清「躯干转 45° 背对」，让图生图带着朝向词去改那 45°。
TURNAROUND_DERIVED: dict[str, tuple[str, str]] = {
    "ne": ("n", "three-quarter back view, torso turned 45 degrees away from the camera"),
    "nw": ("n", "three-quarter back view, torso turned 45 degrees away from the camera"),
}

# 关键姿态：只出这几帧，其余靠插值补。
# 步态四拍 = 左脚接触 / 右腿过渡 / 右脚接触 / 左腿过渡，本来就是「同一套
# 两个极限姿势 + 身体起伏」，中间那四帧正是接触帧压低、过渡帧抬高。
KEY_POSES: dict[str, list[int]] = {
    "idle": [0, 2],                 # 中立 → 呼气回落；吸气/微调靠起伏补
    "walk": [0, 2, 4, 6],           # 接触 / 过渡 / 接触 / 过渡
    "run":  [0, 2, 4, 6],           # 触地 / 蹬伸 / 触地 / 蹬伸
}
# 插值帧：帧号 → (源关键帧号, 垂直位移 px@192cell)。正 = 整体下沉，负 = 抬高。
TWEENS: dict[str, dict[int, tuple[int, int]]] = {
    "idle": {1: (0, -2), 3: (2, 1)},
    "walk": {1: (0, 3), 3: (2, -3), 5: (4, 3), 7: (6, -3)},
    "run":  {1: (0, 4), 3: (2, -6), 5: (4, 4), 7: (6, -6)},
}
# 动画帧生成画幅：独立于立绘画幅。帧最终落进 192×192 cell，384² = 2 倍采样
# 足够精细；用立绘的 512×768 逐帧生成一张要 2.7 倍像素，40 帧×3 次重试能把
# 单个角色拖到半小时以上（用户实测主角久不出图），降到这里后单帧显著提速。
ANIM_FRAME_SIZE = (384, 384)

# ------------------------------------------------------------
#  人工确认（13.2）
#
#  全自动的问题不是「慢」，是「错得没机会刹车」：一个朝向 10 张关键姿态，
#  首帧人物就歪了，后面 9 张只会沿着歪的那张继续歪（它们都以它为参考）。
#  所以每个朝向只先出 1 张确认帧，人工点通过后才继续——错的成本从 10 张降到 1 张。
#
#  off    全自动（批量跑老项目、mock 测试用）
#  dir    每个朝向首帧确认（默认）
#  design 身份卡也确认：身份卡是多栏共用锚，它错了后面全错
# ------------------------------------------------------------
ANIM_CONFIRM_MODES: dict[str, dict] = {
    "off": {"name": "全自动", "note": "中途不停，一路跑完（错了也会把整组跑完）"},
    "dir": {"name": "每朝向确认", "note": "每个朝向先出 1 张确认帧，通过后才继续该朝向"},
    "design": {"name": "身份卡 + 每朝向确认", "note": "身份卡先确认，再逐朝向确认（错得最省）"},
}
ANIM_CONFIRM_ORDER = ["off", "dir", "design"]
ANIM_CONFIRM_DEFAULT = "dir"


# ------------------------------------------------------------
#  姿态控制（13.3）
#
#  为什么必须有：融合编辑（参考图 + denoise=1）的输出几乎完全跟着参考图画布，
#  提示词里的 "left foot forward" 是软约束。用户 16G 机器实拍：一整张 8×8 走
#  路表里 64 格几乎都是同一个站姿，朝向也只是「正面的近亲」。骨架进了模型
#  （ControlNet Pose）才是硬约束。
#
#  auto   有 Union 权重就开，没有就诚实降级（卡片上标「姿态无控制」）
#  pose   强制开：没有权重就报错，不悄悄降级（批量跑图前的自检档）
#  off    关闭（只想快速看构图 / 显存很紧）
# ------------------------------------------------------------
ANIM_CONTROL_MODES: dict[str, dict] = {
    "auto": {"name": "自动（有 Union 权重就用）",
             "note": "有 ControlNet Union 就上骨架硬控；没有就照旧并标出「姿态无控制」"},
    "pose": {"name": "强制姿态控制",
             "note": "缺权重直接报错，不悄悄降级——批量跑之前先用它自检"},
    "off":  {"name": "关闭", "note": "不加骨架，姿势全交给提示词（当前效果就是这么来的）"},
}
ANIM_CONTROL_ORDER = ["auto", "pose", "off"]
ANIM_CONTROL_DEFAULT = "auto"
# 姿态强度：0.8 是 Union Pose 的常用平衡点（再高肢体发僵，再低会回到参考图的站姿）
ANIM_CONTROL_STRENGTH = 0.8
# 采样区间：前 90% 步数由骨架主导，最后 10% 放开让模型补细节（发型/衣褶）
ANIM_CONTROL_START = 0.0
ANIM_CONTROL_END = 0.9


def anim_control(s: dict, item: dict | None = None) -> str:
    """姿态控制档位：条目覆盖 > 项目设置 > 默认（自动）。"""
    for src in ((item or {}), (s or {}).get("anim") or {}, (s or {})):
        mid = str((src or {}).get("anim_control") or "").strip()
        if mid in ANIM_CONTROL_MODES:
            return mid
    return ANIM_CONTROL_DEFAULT


def anim_control_strength(s: dict, item: dict | None = None) -> float:
    for src in ((item or {}), (s or {}).get("anim") or {}, (s or {})):
        raw = (src or {}).get("anim_control_strength")
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if 0.0 < v <= 2.0:
            return v
    return ANIM_CONTROL_STRENGTH


def turnaround_spec(level_id: str | None) -> dict:
    """档位 → 身份卡规格：几张、每张几栏、每栏朝向、派生朝向、单张画幅。"""
    lid = str(level_id or "").strip()
    if lid not in TURNAROUND_SETS:
        lid = ANIM_DEFAULT_LEVEL
    sets = TURNAROUND_SETS[lid]
    where: dict[str, dict] = {}
    sheets = []
    for si, cols in enumerate(sets):
        for ci, d in enumerate(cols):
            where.setdefault(d, {"sheet": si, "col": ci})
        w = _align((TURNAROUND_CELL * len(cols), TURNAROUND_HEIGHT))[0]
        sheets.append({"dirs": list(cols), "cols": len(cols),
                       "size": [w, TURNAROUND_HEIGHT]})
    dirs = list(ANIM_LEVELS[lid]["dirs"])
    derived: dict[str, dict] = {}
    for d in dirs:
        if d in where:
            continue
        src = TURNAROUND_DERIVED.get(d)
        if not src or src[0] not in where:
            src = (list(where)[0] if where else "s", "")
        derived[d] = {"from": src[0], "view": src[1],
                      "sheet": where.get(src[0], {}).get("sheet", 0),
                      "col": where.get(src[0], {}).get("col", 0)}
    return {"level": lid, "enabled": bool(sets), "cell": TURNAROUND_CELL,
            "height": TURNAROUND_HEIGHT, "sheets": sheets,
            "where": where, "derived": derived, "count": len(sheets)}


def anim_spec(level_id: str | None) -> dict:
    """档位 id → 展开的规格（dirs/mirror/各 clip 帧数/每向帧数/总张数）。

    13.0 起同时给出三层分布的账：每向关键姿态数（keys）、插值帧表（tweens）、
    身份卡规格（turnaround），以及真正要烧的出图次数（calls_total）。
    """
    lid = str(level_id or "").strip()
    if lid not in ANIM_LEVELS:
        lid = ANIM_DEFAULT_LEVEL
    lv = ANIM_LEVELS[lid]
    clips = {c: int(lv.get(c) or 0) for c in CLIP_ORDER}
    per_dir = sum(clips.values())
    total = len(lv["dirs"]) * per_dir
    keys = {c: [i for i in KEY_POSES.get(c, []) if i < clips[c]]
            for c in CLIP_ORDER if clips[c]}
    tweens = {c: {i: t for i, t in TWEENS.get(c, {}).items() if i < clips[c]}
              for c in CLIP_ORDER if clips[c]}
    keys_per_dir = sum(len(v) for v in keys.values())
    ta = turnaround_spec(lid)
    return {"id": lid, "name": lv["name"], "dirs": list(lv["dirs"]),
            "mirror": dict(lv["mirror"]), "clips": clips, "per_dir": per_dir,
            "frames_total": total, "note": lv["note"],
            "keys": keys, "tweens": tweens, "keys_per_dir": keys_per_dir,
            "keys_total": keys_per_dir * len(lv["dirs"]),
            "interp_total": total - keys_per_dir * len(lv["dirs"]),
            "turnaround": ta,
            # 真·出图次数：身份卡（按张）+ 关键姿态（按向）。镜像向不出图。
            "calls_total": ta["count"] + keys_per_dir * len(lv["dirs"])}


def anim_confirm(s: dict, item: dict | None = None) -> str:
    """人工确认模式：条目覆盖 > 项目设置 > 默认（每朝向确认）。"""
    for src in ((item or {}), (s or {}).get("anim") or {}, (s or {})):
        mid = str((src or {}).get("anim_confirm") or "").strip()
        if mid in ANIM_CONFIRM_MODES:
            return mid
    return ANIM_CONFIRM_DEFAULT


def anim_level(s: dict, item: dict | None = None) -> str:
    """动画档位：条目覆盖 > 项目设置 > 默认档。"""
    for src in ((item or {}), (s or {}).get("anim") or {}, (s or {})):
        lid = str((src or {}).get("anim_level") or "").strip()
        if lid in ANIM_LEVELS:
            return lid
    return ANIM_DEFAULT_LEVEL


def anim_frames_of(s: dict, kind: str, item: dict | None = None) -> int:
    """该条目要生成多少张帧图（UI 估算与日志用）。"""
    if kind not in ANIM_KINDS:
        return 0
    return anim_spec(anim_level(s, item))["frames_total"]



def monster_tier(entry: dict) -> str:
    """方案条目的怪物等级 id（老数据没有就按杂兵）。"""
    tid = str(entry.get("tier_id") or "").strip()
    return tid if tid in MONSTER_TIERS else "minion"


# ------------------------------------------------------------
#  场景用途：每条场景必须显式选一个用途，用途决定画幅。
#  以前「场景统一 1152×640」是拍脑袋——平铺地表和竖屏副本根本不该同尺寸。
#  reason 会直接显示在 UI 上，让「为什么这么大」有据可查。
# ------------------------------------------------------------
SCENE_USAGES: dict[str, dict] = {
    "battle": {
        "name": "横版战斗背景", "size": (1152, 640), "aspect": "16:9",
        "reason": ("对齐全高清 16:9，等比缩到 1920×1080 不裁切；主体压中央 70% 安全区，"
                   "左右各留 15% 作视差滚动带，站中轴的角色不会被切到"),
    },
    "tile": {
        "name": "可平铺地表", "size": (1024, 1024), "aspect": "1:1",
        "reason": ("四边无缝的方形贴图，按 512 网格切片进 TileMap 无限拼接；"
                   "横竖等长保证任意方向平铺都不拉伸"),
    },
    "portrait": {
        "name": "竖屏副本", "size": (768, 1152), "aspect": "2:3",
        "reason": ("手机竖屏 9:16 的中间安全区，满屏显示不裁切；上下各留一段给状态栏与对话框"),
    },
    "banner": {
        "name": "章节横幅", "size": (1152, 512), "aspect": "9:4",
        "reason": ("加载页 / 章节标题的超宽横幅，故意压扁高度让天际线成主导，左右留白写标题"),
    },
    "iso": {
        "name": "等距斜视小场景", "size": (896, 896), "aspect": "1:1",
        "reason": ("战棋 / 等距副本的一整块战场：7×7 格 × 128px 正好铺满，"
                   "方图便于相机 45° 环绕不露边"),
    },
}
SCENE_USAGE_ORDER = ["battle", "tile", "portrait", "banner", "iso"]


def scene_usage(entry: dict) -> str:
    """方案条目的场景用途 id。老数据没有 usage 就按 tileable 猜。"""
    uid = str(entry.get("usage") or "").strip()
    if uid in SCENE_USAGES:
        return uid
    return "tile" if entry.get("tileable") else "battle"


def scene_size(entry: dict) -> tuple[int, int]:
    return SCENE_USAGES[scene_usage(entry)]["size"]


# ------------------------------------------------------------
#  方案 → 阶段条目表（store 归一化与 pipeline 执行共用；场景全部 ×4 季）
#  条目带稳定 id（itm_*）：方案编辑后靠 id 对账，没改动的条目不重跑。
# ------------------------------------------------------------
PLAN_KEYS = {"scene": "scenes", "character": "characters", "pet": "pets",
             "weapon": "weapons", "building": "buildings", "monster": "monsters"}
ASSET_KINDS = ("scene", "character", "pet", "weapon", "building", "monster")


def build_stage_items(plan: dict, existing: dict[str, list[dict]] | None = None
                      ) -> dict[str, list[dict]]:
    """方案展开为条目表。existing 提供旧条目（按 key 对账沿用 id/状态）。

    条目 key：非场景 = 方案条目在 PLAN_KEYS 里的下标；场景 = 下标+季节。
    """
    from shared.models_util import new_id
    existing = existing or {}
    out: dict[str, list[dict]] = {}
    for kind in ASSET_KINDS:
        old_items = existing.get(kind, [])
        old_by_key = {}
        for it in old_items:
            old_by_key.setdefault(it.get("key") or it.get("id"), it)
        items: list[dict] = []
        for idx, entry in enumerate(plan[PLAN_KEYS[kind]]):
            def _inherit(old, name, desc, **meta):
                """旧条目内容没变才沿用状态；任一字段变了回到 pending（触发重跑）。"""
                if not old or old.get("name") != name or old.get("desc") != desc:
                    return "pending"
                for k, v in meta.items():
                    if (old.get(k) or "") != (v or ""):
                        return "pending"
                return old.get("status", "pending")
            if kind == "scene":
                usage = scene_usage(entry)
                seasons = entry.get("seasons") or SEASON_ORDER
                for season in seasons:
                    key = f"{idx}:{season}"
                    old = old_by_key.get(key) or old_by_key.get(f"{entry['name']}:{season}")
                    items.append({
                        "id": (old or {}).get("id") or new_id("itm"),
                        "key": key,
                        "name": entry["name"], "desc": entry["desc"],
                        "season": season,
                        "usage": usage, "usage_name": SCENE_USAGES[usage]["name"],
                        "tileable": usage == "tile",
                        "status": _inherit(old, entry["name"], entry["desc"],
                                           usage=usage),
                    })
            elif kind == "character":
                gids = entry_genders(entry)
                lv = str(entry.get("anim_level") or "")
                for gid in gids:
                    key = f"{idx}:{gid}"
                    old = (old_by_key.get(key)
                           or old_by_key.get(f"{entry['name']}:{gid}")
                           or (old_by_key.get(entry["name"]) if len(gids) == 1 else None))
                    items.append({
                        "id": (old or {}).get("id") or new_id("itm"),
                        "key": key,
                        "name": entry["name"], "desc": entry["desc"],
                        "gender": gid, "anim_level": lv,
                        "anim_frames": anim_frames_of({}, kind, {"anim_level": lv}),
                        "status": _inherit(old, entry["name"], entry["desc"],
                                           gender=gid, anim_level=lv),
                    })
            else:
                key = str(idx)
                old = old_by_key.get(key) or old_by_key.get(entry["name"])
                item = {"id": (old or {}).get("id") or new_id("itm"),
                        "key": key,
                        "name": entry["name"], "desc": entry["desc"]}
                if kind == "monster":
                    tid = monster_tier(entry)
                    item.update({"tier_id": tid,
                                 "tier": MONSTER_TIERS[tid]["name"],
                                 "scene": entry.get("scene")})
                else:
                    item.update({"tier": entry.get("tier"),
                                 "tier_id": entry.get("tier_id")})
                if kind == "pet":
                    lv = str(entry.get("anim_level") or "")
                    item.update({"anim_level": lv,
                                 "anim_frames": anim_frames_of({}, kind, {"anim_level": lv})})
                item["status"] = _inherit(old, entry["name"], entry["desc"],
                                          tier_id=item.get("tier_id"),
                                          scene=item.get("scene"),
                                          anim_level=item.get("anim_level"))
                items.append(item)
        out[kind] = items
    return out


# ------------------------------------------------------------
#  按类画幅：640~1152 长边、64 对齐（ComfyUI latent 对齐要求）。
#  场景/怪物不用这里的默认值——它们的画幅由「场景用途 / 怪物等级」逐条决定，
#  见 SCENE_USAGES / MONSTER_TIERS 里的 size + reason。
#  set["sizes"][kind] 只对没有逐条设计的类（character/pet/weapon/building）生效。
# ------------------------------------------------------------
ASPECTS: dict[str, tuple[int, int]] = {
    "scene":     (1152, 640),   # 16:9 横幅概念图（= battle 用途，仅兜底）
    "character": (512, 768),    # 2:3 立绘
    "pet":       (768, 768),    # 1:1
    "weapon":    (768, 768),    # 1:1 图标
    "building":  (896, 672),    # 4:3
    "monster":   (640, 640),    # 1:1（= 杂兵，仅兜底）
}
_MASTER_SIZE = (1152, 640)


def _align(size) -> tuple[int, int]:
    w, h = int(size[0]), int(size[1])
    return max(256, w - w % 64), max(256, h - h % 64)


def asset_size(s: dict, kind: str, item: dict | None = None) -> tuple[int, int]:
    """画幅：本条设计（场景用途 / 怪物等级）> 项目按类覆盖 > 类默认。"""
    if item:
        if kind == "scene":
            return _align(scene_size(item))
        if kind == "monster":
            return _align(MONSTER_TIERS[monster_tier(item)]["size"])
    size = ((s.get("sizes") or {}).get(kind)) or ASPECTS.get(kind, (768, 768))
    return _align(size)


# ------------------------------------------------------------
#  透明底：按类指定抠图方式。none=不抠（场景/建筑默认）；
#  rembg=AI 抠图（U2Net，白袍白底这类色键必败的图也能抠，默认）；
#  chroma=白底色键（快，但主体本身接近白色时会抠穿/抠不动）。
#  set["transparent"] 覆盖。
# ------------------------------------------------------------
TRANSPARENT_DEFAULT: dict[str, str] = {
    "scene": "none", "building": "none",
    "character": "rembg", "pet": "rembg",
    "weapon": "rembg", "monster": "rembg",
}


def transparent_mode(s: dict, kind: str) -> str:
    return ((s.get("transparent") or {}).get(kind)) or TRANSPARENT_DEFAULT.get(kind, "none")


# ------------------------------------------------------------
#  按类提示词：固定模板（代码里，不可改）+ 项目级补充（可改）。
#  补充词只追加在这类素材的提示词尾部、负面前部，不影响其它类。
#  set["prompts"][kind] = {"extra": "...", "negative": "..."}
# ------------------------------------------------------------
CLASS_LABELS: dict[str, str] = {
    "scene": "场景", "character": "角色", "pet": "宠物",
    "weapon": "武器", "building": "建筑", "monster": "野怪",
}

# 给 UI 显示的固定模板摘要（真正的拼装在 pipeline.asset_prompt 里）
CLASS_TEMPLATE_HINT: dict[str, str] = {
    "scene": ("2.5D 斜 45° 环境图 · 大全景 · 无角色无文字 · 画幅由「用途」定："
              "16:9 战斗 / 1:1 平铺 / 2:3 竖屏 / 9:4 横幅 / 1:1 等距战场"),
    "character": ("2.5D 斜 45° · Q 版三头身 · 全身 · 脚底贴地 · 站立/行走/奔跑三段"
                  "（轻量 3 向 40 张 / 标准 5 向 100 张 / 全向 8 向 160 张；"
                  "每向 站立4 + 行走8 + 奔跑8，可导出走/跑经典整表）"),
    "pet": ("2.5D 斜 45° · Q 版 · 全身 · 脚底贴地 · 与主角同一台相机 · "
            "站立/行走/奔跑三段（帧数同角色）"),
    "weapon": "武器设计 · 单件 · 与角色同一 45° 相机 · 居中 · 可叠加等级词",
    "building": "建筑外观 · 2.5D 斜 45° · 底面落在等距地面上 · 完整结构可见",
    "monster": ("怪物设计 · 2.5D 斜 45° · 全身 · 脚底贴地 · 画幅由「等级」定："
                "杂兵 640 / 精英 768 / 首领 896 / 终局 1024，并写明出没场景"),
}


def prompt_extra(s: dict, kind: str) -> str:
    """该类追加的正向提示词（项目级，可空）。"""
    return str(((s.get("prompts") or {}).get(kind) or {}).get("extra") or "").strip()


def prompt_negative(s: dict, kind: str) -> str:
    """该类追加的负面词（项目级，可空）。"""
    return str(((s.get("prompts") or {}).get(kind) or {}).get("negative") or "").strip()
