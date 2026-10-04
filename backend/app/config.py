# -*- coding: utf-8 -*-
"""全局配置。

配置来源优先级：环境变量 > 项目根目录的 .env > 默认值。
所有路径均以项目根目录（本文件的上上级目录）为基准解析，
保证数据全部落在 E:/AIGC/qwenimages 下。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> backend/app -> backend -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    text_encoder_device: str = "cpu"
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
    def frontend_dir_path(self) -> Path:
        return PROJECT_ROOT / "frontend"

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
