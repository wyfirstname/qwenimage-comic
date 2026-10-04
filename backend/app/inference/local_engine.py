# -*- coding: utf-8 -*-
"""本地真实推理引擎。

加载 Qwen-Image 2.1 的 GGUF 权重 + Qwen3-VL 文本编码器 + VAE，并用 diffusers
的 QwenImagePipeline 完成文生图 / 图生图。

────────────────────────────────────────────────────────────────
接入说明（重要）
────────────────────────────────────────────────────────────────
Qwen-Image 2.1（架构 qwen_image21）属于较新的架构，diffusers 与各 GGUF 加载器
的支持仍在演进。因此本文件把「GGUF 权重如何塞进 diffusers」这一步隔离在
`_load_transformer()` 里，并提供两种策略：

  1. 自动：调用 diffusers 官方的 GGUF 量化加载能力（若当前版本支持）；
  2. 手工：逐个读取 GGUF 张量，替换 transformer 中的 Linear 层权重。

若两者都失败，会抛出带有明确指引的 RuntimeError，提示改用 ComfyUI 通道
（见 docs/本地图片生成器-项目文档.md 附录 A），而不是静默降级成假图，
以免产生「以为在用真模型」的误解。
"""

from __future__ import annotations

import gc
import time
from typing import Callable, Optional

from PIL import Image

from app.config import settings
from app.inference import device as dev
from app.inference.base import EngineResult


class LocalEngine:
    """基于 diffusers + GGUF 的本地推理引擎。"""

    name = "local"

    def __init__(self) -> None:
        self.pipe = None
        self._loaded = False
        self.last_error: Optional[str] = None
        self.last_load_seconds: Optional[float] = None

    # ================= 生命周期 =================
    @property
    def loaded(self) -> bool:
        return self._loaded and self.pipe is not None

    def load(self) -> None:
        if self.loaded:
            return
        started = time.time()
        self._check_files()

        if not dev.TORCH_AVAILABLE:
            raise RuntimeError(
                "未检测到 PyTorch。请先安装推理依赖：\n"
                "    pip install -r backend/requirements-inference.txt\n"
                "（并根据 https://pytorch.org 选择匹配本机 CUDA 的版本）"
            )

        try:
            from diffusers import QwenImagePipeline  # noqa: WPS433 (延迟导入)
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "当前 diffusers 版本不包含 QwenImagePipeline，请升级：\n"
                "    pip install -U 'diffusers>=0.32.0'\n"
                f"原始错误：{exc}"
            ) from exc

        dtype = dev.resolve_dtype(settings.dtype)
        self.pipe = self._build_pipeline(QwenImagePipeline, dtype)

        if settings.vae_tiling:
            try:
                self.pipe.vae.enable_tiling()
            except Exception:
                pass

        # 低显存模式：启用顺序卸载 + 注意力切块，牺牲速度换显存
        if settings.low_vram:
            try:
                self.pipe.enable_sequential_cpu_offload()
            except Exception:
                try:
                    self.pipe.enable_model_cpu_offload()
                except Exception:
                    pass
            try:
                self.pipe.enable_attention_slicing()
            except Exception:
                pass
            try:
                self.pipe.enable_vae_slicing()
            except Exception:
                pass
            logger_notes = "已启用低显存模式（顺序卸载 + 注意力切块）"
            print(f"[推理] {logger_notes}")

        self._loaded = True
        self.last_load_seconds = round(time.time() - started, 2)
        self.last_error = None

    def unload(self) -> None:
        self.pipe = None
        self._loaded = False
        gc.collect()
        dev.empty_cache()

    # ================= 加载实现 =================
    def _check_files(self) -> None:
        status = settings.model_files_status()
        missing = [k for k, v in status.items() if isinstance(v, dict) and not v["exists"]]
        if missing:
            names = {
                "transformer": settings.transformer_file,
                "text_encoder": settings.text_encoder_file,
                "vae": settings.vae_file,
            }
            lines = "\n".join(f"  - {names[m]}  （期望位置：{status[m]['path']}）" for m in missing)
            raise RuntimeError(
                "模型文件缺失，无法加载本地推理引擎：\n"
                f"{lines}\n\n"
                "下载方式（国内建议走镜像）：\n"
                "    set HF_ENDPOINT=https://hf-mirror.com\n"
                "    huggingface-cli download abenzerps/Qwen-Image-2.1-Uncensored-GGUF "
                "--include \"qwen-image-2.1-UC-Q4_K_M.gguf\" "
                "--include \"text_encoders/qwen3vl_8b_int8_convrot.safetensors\" "
                "--include \"vae/qwen_image_2.1_vae_bf16.safetensors\" "
                "--local-dir models/qwen-image-2.1-unc"
            )

    def _build_pipeline(self, pipeline_cls, dtype):
        """构造 diffusers 管道。"""
        transformer = self._load_transformer(dtype)

        # 文本编码器：按配置放到 CPU 以节省显存
        from transformers import Qwen2VLForConditionalGeneration  # 实际类型随权重而定

        text_encoder = Qwen2VLForConditionalGeneration.from_pretrained(
            str(settings.model_dir_path),
            torch_dtype=dtype,
            low_cpu_mem_usage=True,
        )

        from diffusers import AutoencoderKL

        vae = AutoencoderKL.from_single_file(str(settings.vae_path), torch_dtype=dtype)

        pipe = pipeline_cls(
            transformer=transformer,
            text_encoder=text_encoder,
            vae=vae,
            torch_dtype=dtype,
        )

        pipe.to(settings.device)
        pipe.set_progress_bar_config(disable=True)
        return pipe

    def _load_transformer(self, dtype):
        """把 GGUF 权重加载为 diffusers 可用的 transformer。

        策略 1：diffusers 原生 GGUF 支持（新版本提供了 from_single_file 的
                GGUF 分支或专门的量化后端）。
        策略 2：手工张量替换。
        """
        # 前置架构检查：qwen_image21（Qwen-Image 2.1）与 diffusers 的
        # QwenImageTransformer2DModel 结构不兼容（hidden=4096、单流 img_mlp 块、
        # modulation.1 等新模块，已通过张量对比验证），不要盲目尝试后报出误导性错误。
        arch = self._gguf_architecture()
        if arch == "qwen_image21":
            raise RuntimeError(
                "检测到 GGUF 架构为 qwen_image21（Qwen-Image 2.1）。\n"
                "当前 diffusers（含最新 0.40）的 QwenImageTransformer2DModel 只支持 "
                "1.0 架构，无法加载 2.1 权重（张量对比已确认：隐藏维度 4096 vs 3072、"
                "单流 img_mlp 块、新增 modulation.1）。\n\n"
                "可行方案：\n"
                "  A. 使用 ComfyUI 作为推理引擎（ComfyUI-GGUF 已原生支持 qwen_image21）：\n"
                "     安装 ComfyUI + ComfyUI-GGUF 后，在 .env 中配置 "
                "COMFYUI_URL=http://127.0.0.1:8188 即可切换；\n"
                "  B. 等待 diffusers 官方支持 qwen_image21 后升级；\n"
                "  C. 改用 1.0 架构的模型权重（非本仓库）。"
            )

        try:  # 策略 1
            from diffusers import QwenImageTransformer2DModel

            return QwenImageTransformer2DModel.from_single_file(
                str(settings.transformer_path),
                torch_dtype=dtype,
                local_files_only=True,  # 禁止联网拉 config，失败要当场暴露
            )
        except Exception as exc:  # 策略 1 失败 -> 策略 2
            first_error = exc

        try:  # 策略 2
            return self._load_transformer_via_gguf(dtype)
        except Exception as exc:  # 两种策略都失败
            self.last_error = str(exc)
            raise RuntimeError(
                "无法把 GGUF 权重加载进 diffusers。\n"
                f"策略1错误：{first_error}\n"
                f"策略2错误：{exc}\n\n"
                "可行的替代方案（任选其一）：\n"
                "  A. 升级 diffusers 到最新版后重试；\n"
                "  B. 改用 ComfyUI 作为推理引擎（见文档附录 A），后端通过 "
                "http://127.0.0.1:8188/prompt 提交工作流；\n"
                "  C. 若已有非量化的 safetensors 权重，可把 .env 中的 "
                "TRANSFORMER_FILE 指向它，走标准 from_pretrained 路径。"
            ) from exc

    def _gguf_architecture(self) -> Optional[str]:
        """读取 GGUF 的 general.architecture（读不到返回 None）。"""
        try:
            import gguf

            reader = gguf.GGUFReader(str(settings.transformer_path))
            field = reader.fields.get("general.architecture")
            if field is None:
                return None
            return bytes(field.parts[-1]).decode("utf-8", "replace")
        except Exception:
            return None

    def _load_transformer_via_gguf(self, dtype):
        """手工读取 GGUF 张量并写入 transformer 权重。"""
        import gguf  # type: ignore
        import torch
        from diffusers import QwenImageTransformer2DModel

        # 先用配置构造空骨架（不加载权重），再逐层灌入
        model = QwenImageTransformer2DModel.from_config(
            QwenImageTransformer2DModel.load_config(str(settings.model_dir_path))
        ).to(dtype)

        reader = gguf.GGUFReader(str(settings.transformer_path))
        state = {}
        for tensor in reader.tensors:
            name = tensor.name
            arr = torch.from_numpy(tensor.data.copy())
            # GGUF 常见为反量化后的 fp16/bf16，此处按目标精度转换
            state[name] = arr.to(dtype)

        missing, unexpected = model.load_state_dict(state, strict=False)
        if len(missing) > len(state) * 0.3:
            raise RuntimeError(
                f"GGUF 张量与模型结构匹配度过低（缺失 {len(missing)} 项），"
                "可能是架构版本不一致。"
            )
        return model

    # ================= 推理 =================
    def generate(
        self,
        *,
        prompt: str,
        negative: str = "",
        width: int = 1024,
        height: int = 1024,
        steps: int = 20,
        guidance_scale: float = 4.0,
        seed: int = -1,
        reference: Optional[Image.Image] = None,
        references: Optional[list[Image.Image]] = None,
        strength: float = 0.6,
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> EngineResult:
        import torch

        if not self.loaded:
            self.load()

        started = time.time()
        if seed is None or seed < 0:
            seed = int(torch.randint(0, 2 ** 31 - 1, (1,)).item())

        generator = torch.Generator(device="cpu").manual_seed(seed)

        def _cb(pipe_self, step, timestep, kwargs):  # noqa: ANN001
            if progress_cb:
                pct = int((step + 1) / max(1, steps) * 92)
                progress_cb(pct, f"采样 {step + 1}/{steps}")

        kwargs = dict(
            prompt=prompt,
            negative_prompt=negative or None,
            width=width,
            height=height,
            num_inference_steps=steps,
            guidance_scale=guidance_scale,
            generator=generator,
            callback_on_step_end=_cb,
        )

        # diffusers 后端（当前架构不支持，仅保留接口）：多参考图退化为第一张
        if reference is None and references:
            reference = references[0]
        if reference is not None:
            kwargs["image"] = reference
            kwargs["strength"] = strength

        result = self.pipe(**kwargs)
        image = result.images[0]

        if progress_cb:
            progress_cb(95, "解码")

        return EngineResult(
            image=image,
            seed=seed,
            elapsed_ms=int((time.time() - started) * 1000),
            engine=self.name,
        )
