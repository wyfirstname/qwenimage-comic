# -*- coding: utf-8 -*-
"""全局配置（shared 层，纯公共，不依赖 apps / engine）。

配置来源优先级：环境变量 > 项目根目录的 .env > 默认值。
所有路径均以项目根目录（本文件的上上级目录）为基准解析，
保证数据全部落在 E:/AIGC/qwenimages 下。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# shared/config.py -> shared -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 服务 ----------------
    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 1
    reload: bool = False

    # ---------------- 运行模式 ----------------
    # mock | local | comfy
    engine_mode: str = "mock"
    # 引擎接入方式：inproc=与调用方同进程（单机最快，默认）；http=连独立引擎进程
    engine_transport: str = "inproc"
    # 引擎独立进程地址（engine_transport=http 时生效）
    engine_url: str = "http://127.0.0.1:8189"
    # 单应用独立运行时的监听端口（门户模式不用）
    app_port: int = 8000

    # ---------------- ComfyUI 引擎（engine_mode=comfy 时生效）----------------
    comfyui_url: str = "http://127.0.0.1:8188"
    # 为 true 时，若 ComfyUI 未运行则由本服务自动拉起子进程
    comfyui_autostart: bool = True
    comfyui_dir: str = "comfyui"
    comfyui_port: int = 8188
    # 低显存时把模型权重常驻内存/显存的策略交给 ComfyUI（--lowvram 由这里控制）
    comfyui_extra_args: str = "--lowvram"

    # ---------------- 模型 ----------------
    model_dir: str = "models/qwen-image-2.1-unc"
    transformer_file: str = "qwen-image-2.1-UC-Q4_K_M.gguf"
    text_encoder_file: str = "text_encoders/qwen3vl_8b_int8_convrot.safetensors"
    vae_file: str = "vae/qwen_image_2.1_vae_bf16.safetensors"

    # ---------------- 设备 ----------------
    device: str = "cuda:0"
    # 注意：文本编码器（Qwen3-VL-8B）的落点**没有**配置项。
    # ComfyUI 的 CLIPLoader 只接受 default / cpu 两个取值，且 TextGenerate 路径
    # 要求 cuda——显式传 cpu 会直接抛 `Expected a cuda device, but got: cpu`。
    # 因此统一用 default，由 ComfyUI 的 model_management / DynamicVRAM 按当前
    # 显存状态自行调度（详见 inference/comfy_text.py 的说明）。
    dtype: str = "float16"
    keep_loaded: bool = True
    idle_unload_seconds: int = 1800
    vae_tiling: bool = True
    # 低显存模式：启用顺序卸载、切块等策略（6GB 及以下显存建议开启）
    low_vram: bool = False

    # ---------------- 生成默认值 ----------------
    default_width: int = 1024
    default_height: int = 1024
    default_steps: int = 20
    default_guidance: float = 4.0
    max_batch: int = 4
    max_pixels: int = 2073600
    infer_timeout_seconds: int = 600
    max_queue_size: int = 20

    # ---------------- 外部工具 ----------------
    # ffmpeg 位置：可填可执行文件，也可填它所在目录；留空则按
    # runtime/ffmpeg → runtime/bin → tools/ffmpeg → PATH 的顺序查找
    ffmpeg_path: str = ""

    # ---------------- 显存槽位（单卡跑多个模型的核心开关）----------------
    # 出图与 TTS 共用一个 GPU，互相会顶掉对方的权重。开启后：
    #   用哪个就把哪个装进来，另一个当场卸载；两个都闲着超过
    #   slot_idle_unload_seconds 秒就全部卸掉，把显存还给系统。
    model_slots_enabled: bool = True
    slot_idle_unload_seconds: int = 600

    # ---------------- TTS（IndexTTS-2，独立进程）----------------
    # 必须独立进程 + 独立 venv：IndexTTS-2 要求 python>=3.10,<3.12 且
    # transformers 锁在 4.52.1，而主环境是 3.13 + transformers 5.x，
    # 装在一起会把 ComfyUI 的 GGUF 通道一起拖死。
    tts_enabled: bool = True
    tts_url: str = "http://127.0.0.1:8190"
    # 后端：auto = 有 IndexTTS 就用它，否则 edge-tts，都没有则占位音
    #       edge     = 微软在线接口，零模型、零显存（默认推荐）
    #       indextts = 本地 5.5GB 模型，音质最好，吃显存
    #       stub     = 占位音，只用于验证链路
    tts_backend: str = "auto"
    # edge-tts 音色（也是 TTS_SPEAKER 留空时的默认值）
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    # index-tts 仓库目录（其下的 .venv 会被自动使用）
    tts_home: str = ""
    # 直接指定 TTS 解释器，优先于 tts_home
    tts_python: str = ""
    tts_autostart: bool = True
    # 权重目录（相对 tts_home）；2.0 用 checkpoints_2，2.5 用 checkpoints
    tts_models_dir: str = "checkpoints_2"
    # 2 = IndexTTS-2，2.5 = IndexTTS-2.5（多语言 / 语速可控）
    tts_model_version: str = "2"
    tts_use_fp16: bool = True
    # 默认音色参考音频（相对 tts_home 或绝对路径）；留空则用内置示例
    tts_speaker: str = ""
    tts_timeout_seconds: int = 300

    # ---------------- 本地文本模型（剧本 / 角色提取 / 场景提取）----------------
    # 复用 Qwen-Image 自带的 Qwen3-VL-8B 文本编码器，经 ComfyUI 的 TextGenerate 节点
    # 做本地文本生成，不引入任何云端 API、不额外下载模型。
    llm_enabled: bool = True
    llm_max_length: int = 768
    llm_timeout_seconds: int = 1800

    # ---------------- 存储 ----------------
    data_dir: str = "data"
    output_dir: str = "data/outputs"
    db_path: str = "data/app.db"
    max_output_dir_gb: float = 50.0

    # ---------------- 双通道 ----------------
    enable_remote_fallback: bool = False
    remote_provider: str = "siliconflow"
    remote_api_key: str = ""
    remote_base_url: str = "https://api.siliconflow.cn/v1"
    remote_model: str = "Qwen/Qwen-Image-Edit-2509"

    # ---------------- 内容策略 ----------------
    # off | flag_only | block
    nsfw_filter: str = "flag_only"

    # ================= 派生路径（绝对路径） =================
    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    def _abs(self, relative: str) -> Path:
        p = Path(relative)
        if p.is_absolute():
            return p
        return (PROJECT_ROOT / p).resolve()

    @property
    def model_dir_path(self) -> Path:
        return self._abs(self.model_dir)

    @property
    def transformer_path(self) -> Path:
        return self.model_dir_path / self.transformer_file

    @property
    def text_encoder_path(self) -> Path:
        return self.model_dir_path / self.text_encoder_file

    @property
    def vae_path(self) -> Path:
        return self.model_dir_path / self.vae_file

    @property
    def data_dir_path(self) -> Path:
        return self._abs(self.data_dir)

    @property
    def output_dir_path(self) -> Path:
        return self._abs(self.output_dir)

    @property
    def db_path_abs(self) -> Path:
        return self._abs(self.db_path)

    @property
    def tts_home_path(self) -> Path:
        """IndexTTS 仓库目录（未配置时为项目下的 tools/index-tts）。"""
        return self._abs(self.tts_home or "tools/index-tts")

    # ---- 平台 / 应用目录 ----
    @property
    def apps_dir_path(self) -> Path:
        """apps/ 根目录（每个应用一个自包含子目录）。"""
        return PROJECT_ROOT / "apps"

    @property
    def portal_frontend_path(self) -> Path:
        """门户（平台入口）前端目录。"""
        return PROJECT_ROOT / "portal" / "frontend"

    @property
    def frontend_dir_path(self) -> Path:
        """兼容旧调用：指向门户前端目录。"""
        return self.portal_frontend_path

    def engine_mode_is_remote(self) -> bool:
        """引擎是否以独立进程/HTTP 方式接入（独立运行模式）。"""
        return (self.engine_transport or "inproc").lower() == "http"

    def ensure_dirs(self) -> None:
        """确保运行所需的目录都存在。"""
        for p in (self.data_dir_path, self.output_dir_path, self.db_path_abs.parent):
            p.mkdir(parents=True, exist_ok=True)

    def model_files_status(self) -> dict:
        """返回模型文件的存在情况，供前端提示用户下载。"""
        items = {
            "transformer": self.transformer_path,
            "text_encoder": self.text_encoder_path,
            "vae": self.vae_path,
        }
        detail = {}
        for key, path in items.items():
            exists = path.exists()
            detail[key] = {
                "path": str(path),
                "exists": exists,
                "size_gb": round(path.stat().st_size / 1024 ** 3, 2) if exists else 0.0,
            }
        detail["ready"] = all(v["exists"] for v in detail.values() if isinstance(v, dict))
        return detail


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
