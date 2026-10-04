# -*- coding: utf-8 -*-
"""API 请求 / 响应数据模型。"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.config import settings


class GenerateRequest(BaseModel):
    """文生图 / 图生图请求。"""

    prompt: str = Field(..., min_length=1, description="提示词")
    negative_prompt: str = Field("", description="负面提示词")
    width: int = Field(default=settings.default_width, ge=64, le=2048)
    height: int = Field(default=settings.default_height, ge=64, le=2048)
    steps: int = Field(default=settings.default_steps, ge=1, le=100)
    guidance_scale: float = Field(default=settings.default_guidance, ge=0.0, le=20.0)
    seed: int = Field(-1, description="-1 表示随机")
    batch_size: int = Field(1, ge=1, le=settings.max_batch)
    mode: Literal["txt2img", "img2img"] = "txt2img"
    image: Optional[str] = Field(None, description="图生图参考图，base64 或 data URL")
    ref_images: List[str] = Field(default_factory=list, description="多参考图")
    strength: float = Field(0.6, ge=0.0, le=1.0)
    channel: Literal["auto", "local", "remote"] = "auto"

    @field_validator("width", "height")
    @classmethod
    def _multiple_of_64(cls, v: int) -> int:
        if v % 64 != 0:
            raise ValueError("width/height 必须是 64 的整数倍（如 768 / 1024 / 1152 / 1536）")
        return v

    @field_validator("prompt")
    @classmethod
    def _strip_prompt(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("提示词不能为空")
        return v


class BatchRequest(GenerateRequest):
    """批量任务：在基础请求上增加张数。"""

    count: int = Field(4, ge=1, le=32, description="生成张数")


class ImageOut(BaseModel):
    id: str
    url: str
    path: str
    width: Optional[int] = None
    height: Optional[int] = None
    seed: Optional[int] = None
    steps: Optional[int] = None
    guidance: Optional[float] = None
    prompt: Optional[str] = None
    negative: Optional[str] = None
    mode: Optional[str] = None
    favorite: int = 0
    flagged: int = 0
    elapsed_ms: Optional[int] = None
    created_at: str


class JobOut(BaseModel):
    id: str
    status: str
    mode: str = "txt2img"
    prompt: str
    negative: Optional[str] = None
    seed: Optional[int] = None
    count: int = 1
    progress: int = 0
    message: Optional[str] = None
    error: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    images: List[ImageOut] = Field(default_factory=list)


class JobCreated(BaseModel):
    job_id: str
    status: str
    position: int = 0
    queue_size: int = 0


class GalleryQuery(BaseModel):
    page: int = 1
    page_size: int = 24
    keyword: str = ""
    favorite_only: bool = False
    job_id: Optional[str] = None


class GalleryPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[ImageOut]


class ModelStatus(BaseModel):
    engine_mode: str
    loaded: bool
    loading: bool
    device: str
    text_encoder_device: str
    keep_loaded: bool
    queue_size: int
    running_job: Optional[str] = None
    vram_total_gb: Optional[float] = None
    vram_free_gb: Optional[float] = None
    torch_available: bool
    model_files: dict[str, Any]
    last_error: Optional[str] = None
    last_load_seconds: Optional[float] = None
    note: Optional[str] = None


class PreviewRequest(BaseModel):
    """参数预览 / 估算。"""

    width: int = settings.default_width
    height: int = settings.default_height
    steps: int = settings.default_steps
    batch_size: int = 1


class PreviewOut(BaseModel):
    estimated_seconds: float
    estimated_vram_gb: float
    megapixels: float
    warnings: List[str] = Field(default_factory=list)


class ConfigOut(BaseModel):
    engine_mode: str
    nsfw_filter: str
    default_width: int
    default_height: int
    default_steps: int
    default_guidance: float
    max_batch: int
    max_queue_size: int
    enable_remote_fallback: bool
    remote_model: str
    model_ready: bool
    model_dir: str
    output_dir: str


class OkOut(BaseModel):
    ok: bool = True
    message: str = ""


# ============================================================
#  漫画工作室
# ============================================================


class ComicProjectIn(BaseModel):
    title: str = ""
    style: str = "jp_bw"
    layout: str = "grid_2x2"
    width: int = Field(768, ge=64, le=1536)
    height: int = Field(768, ge=64, le=1536)
    steps: int = Field(8, ge=1, le=60)
    guidance: float = Field(4.0, ge=0.0, le=20.0)
    negative: str = ""
    char_negative: str = ""
    ref_strength: float = Field(0.55, ge=0.2, le=0.95)
    synopsis: str = ""
    keep_style: bool = True
    use_ref: bool = True

    @field_validator("width", "height")
    @classmethod
    def _mul64(cls, v: int) -> int:
        if v % 64 != 0:
            raise ValueError("尺寸必须是 64 的整数倍")
        return v


class ComicProjectPatch(BaseModel):
    title: Optional[str] = None
    style: Optional[str] = None
    layout: Optional[str] = None
    width: Optional[int] = Field(None, ge=64, le=1536)
    height: Optional[int] = Field(None, ge=64, le=1536)
    steps: Optional[int] = Field(None, ge=1, le=60)
    guidance: Optional[float] = Field(None, ge=0.0, le=20.0)
    negative: Optional[str] = None
    char_negative: Optional[str] = None
    ref_strength: Optional[float] = Field(None, ge=0.2, le=0.95)
    keep_style: Optional[bool] = None
    use_ref: Optional[bool] = None


class ComicCharacterIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    role: str = ""
    appearance: str = ""
    outfit: str = ""
    personality: str = ""
    gender: str = ""
    age: str = ""
    background: str = ""
    detail_prompt: str = ""
    negative: str = ""
    seed: int = -1


class ComicCharacterPatch(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    appearance: Optional[str] = None
    outfit: Optional[str] = None
    personality: Optional[str] = None
    gender: Optional[str] = None
    age: Optional[str] = None
    background: Optional[str] = None
    detail_prompt: Optional[str] = None
    negative: Optional[str] = None
    seed: Optional[int] = None


class ComicSceneIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    location: str = ""
    time_of_day: str = ""
    atmosphere: str = ""
    desc: str = ""
    prompt: str = ""
    seed: int = -1


class ComicScenePatch(BaseModel):
    name: Optional[str] = None
    location: Optional[str] = None
    time_of_day: Optional[str] = None
    atmosphere: Optional[str] = None
    desc: Optional[str] = None
    prompt: Optional[str] = None
    seed: Optional[int] = None


class ComicScriptGenIn(BaseModel):
    """剧本生成：按梗概写正文。"""

    idea: str = ""
    pages: int = Field(2, ge=1, le=20)
    save: bool = True


class ComicScriptContinueIn(BaseModel):
    """剧本续写：在现有剧本后接着写 N 页。"""

    idea: str = ""
    pages: int = Field(2, ge=1, le=20)


class ComicScriptPolishIn(BaseModel):
    text: str = ""


class ComicExtractIn(BaseModel):
    """角色 / 场景提取。"""

    script: str = ""
    count: int = Field(6, ge=1, le=12)
    replace: bool = False


class ComicRefIn(BaseModel):
    image: str = Field(..., description="base64 或 data URL")


class ComicScriptIn(BaseModel):
    text: str = ""
    replace: bool = True


class ComicPanelIn(BaseModel):
    shot: str = "medium"
    scene: str = ""
    dialogue: str = ""
    sfx: str = ""
    scene_id: Optional[str] = None
    character_ids: List[str] = Field(default_factory=list)
    extra_prompt: str = ""
    seed: int = -1
    page: int = 1


class ComicPanelPatch(BaseModel):
    shot: Optional[str] = None
    scene: Optional[str] = None
    dialogue: Optional[str] = None
    sfx: Optional[str] = None
    scene_id: Optional[str] = None
    character_ids: Optional[List[str]] = None
    extra_prompt: Optional[str] = None
    seed: Optional[int] = None
    page: Optional[int] = None


class ComicMoveIn(BaseModel):
    direction: int = 1


class ComicGenerateIn(BaseModel):
    only_missing: bool = True
    strength: Optional[float] = Field(None, ge=0.2, le=0.95)
    # True=每次换随机种子（重绘会出不同的图）；False=锁定该格已有种子便于复现
    reseed: bool = True


class ComicRenderIn(BaseModel):
    layout: Optional[str] = None
    page_width: int = Field(1240, ge=600, le=2400)
    bubble_position: str = "auto"
    grayscale: Optional[bool] = None
    show_page_number: bool = True


class ComicExportIn(ComicRenderIn):
    """导出整部漫画为 PDF。rerender=True 时先按当前设置重新排版再导出。

    color_mode：`color`=彩色版（保留色彩），`bw`=黑白版（合成时转灰度）。
    """

    rerender: bool = False
    color_mode: str = Field("color", pattern="^(color|bw)$")


# ============================================================
#  人像编辑（Qwen-Image 2.1 图像编辑，官方玩法）
# ============================================================


class PortraitComposeIn(BaseModel):
    """提示词组装 / 智能改写请求。"""

    preset: str = Field(..., min_length=1, description="玩法 id")
    values: Dict[str, str] = Field(default_factory=dict, description="玩法变量")
    image_count: int = Field(0, ge=0, le=8, description="已上传参考图张数")
    prompt_override: str = Field("", description="手动 / 智能改写后的提示词，非空则直接采用")


class PortraitSubmitIn(PortraitComposeIn):
    """人像编辑出图请求（复用既有出图队列）。"""

    images: List[str] = Field(default_factory=list,
                              description="有序参考图（base64 或 data URL），第 1 张决定输出尺寸")
    steps: int = Field(8, ge=1, le=100)
    seed: int = -1
    count: int = Field(1, ge=1, le=4)
    width: int = Field(768, ge=64, le=2048)
    height: int = Field(1024, ge=64, le=2048)
    max_mp: float = Field(0.8, ge=0.1, le=4.0, description="参考图像素预算（百万像素）")

    @field_validator("width", "height")
    @classmethod
    def _mul64_portrait(cls, v: int) -> int:
        if v % 64 != 0:
            raise ValueError("width/height 必须是 64 的整数倍")
        return v
