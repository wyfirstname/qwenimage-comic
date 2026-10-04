# -*- coding: utf-8 -*-
"""ComfyUI 引擎适配层。

把出图请求翻译成 ComfyUI 工作流（API 格式），提交到 /prompt，
通过 WebSocket 订阅实时进度，完成后经 /view 取回图片。

模型加载本身由 ComfyUI 负责（ComfyUI-GGUF 原生支持 qwen_image21），
本引擎的 load() 只负责：确认服务可达，不可达且允许时自动拉起子进程。

Qwen-Image 2.1 GGUF 与 diffusers 不兼容（见 local_engine.py 的说明），
这是当前唯一可用的真实推理通道。
"""

from __future__ import annotations

import io as _io
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import urllib.error
import urllib.parse
import urllib.request

from PIL import Image

from app.config import settings
from app.inference import comfy_http
from app.inference.base import EngineResult


class ComfyEngine:
    """通过 ComfyUI 服务端推理。"""

    name = "comfy"

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._ready = False
        self.last_error: Optional[str] = None
        self.last_load_seconds: Optional[float] = None
        self.client_id = f"qwenimages-{uuid.uuid4().hex[:8]}"
        self._current_prompt_id: Optional[str] = None

    # ================= 生命周期 =================
    @property
    def loaded(self) -> bool:
        return self._ready

    def _ping(self, timeout: float = 3.0) -> bool:
        try:
            with comfy_http.open(
                f"{settings.comfyui_url}/system_stats", timeout=timeout
            ):
                return True
        except Exception:
            return False

    def load(self) -> None:
        if self._ready and self._ping():
            return
        started = time.time()

        if not self._ping():
            if not settings.comfyui_autostart:
                raise RuntimeError(
                    f"ComfyUI 未运行（{settings.comfyui_url}）。\n"
                    "请先启动 ComfyUI，或在 .env 中设置 COMFYUI_AUTOSTART=true 让本服务自动拉起。"
                )
            self._spawn()
            # ComfyUI 冷启动（含 torch 导入）可能要 30-120 秒
            deadline = time.time() + 240
            while time.time() < deadline:
                if self._ping():
                    break
                if self._proc and self._proc.poll() is not None:
                    raise RuntimeError(
                        f"ComfyUI 进程启动后立即退出（exit={self._proc.returncode}），"
                        "请查看 data/comfyui.log"
                    )
                time.sleep(1.5)
            else:
                raise RuntimeError("等待 ComfyUI 启动超时（240s），请查看 data/comfyui.log")

        self._ready = True
        self.last_error = None
        self.last_load_seconds = round(time.time() - started, 2)

    def _spawn(self) -> None:
        comfy_dir = settings._abs(settings.comfyui_dir)
        main_py = comfy_dir / "main.py"
        if not main_py.exists():
            raise RuntimeError(
                f"未找到 ComfyUI（期望位于 {comfy_dir}）。\n"
                "安装方式：git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "
                f"{comfy_dir}"
            )
        extra = settings.comfyui_extra_args.split() if settings.comfyui_extra_args else []
        cmd = [
            sys.executable,
            str(main_py),
            "--listen", "127.0.0.1",
            "--port", str(settings.comfyui_port),
            "--disable-auto-launch",
            *extra,
        ]
        log_path = settings.data_dir_path / "comfyui.log"
        log_f = open(log_path, "ab")  # noqa: SIM115 (随进程生命周期)
        self._proc = subprocess.Popen(
            cmd, cwd=str(comfy_dir), stdout=log_f, stderr=subprocess.STDOUT
        )
        print(f"[comfy] 已拉起 ComfyUI 子进程 PID={self._proc.pid}，日志 {log_path}")

    def unload(self) -> None:
        # 请求 ComfyUI 卸载权重释放显存；子进程保留（复用比重启快得多）
        try:
            req = urllib.request.Request(
                f"{settings.comfyui_url}/free",
                data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            comfy_http.open(req, timeout=10)
        except Exception:
            pass
        self._ready = self._ping()

    def cancel(self) -> None:
        """取消当前在 ComfyUI 端执行的任务（超时/异常时必须调用，否则会积压拖垮后续任务）。"""
        pid, self._current_prompt_id = self._current_prompt_id, None
        try:
            if pid:
                req = urllib.request.Request(
                    f"{settings.comfyui_url}/queue",
                    data=json.dumps({"delete": [pid]}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                comfy_http.open(req, timeout=10)
            # 中断正在执行的那个（无论是不是本任务——超时场景下它就该被停掉）
            req = urllib.request.Request(
                f"{settings.comfyui_url}/interrupt", data=b"", method="POST"
            )
            comfy_http.open(req, timeout=10)
        except Exception:
            pass

    # ================= 工作流 =================
    def _build_workflow(self, *, prompt: str, negative: str, width: int, height: int,
                        steps: int, guidance_scale: float, seed: int,
                        ref_name: Optional[str], strength: float,
                        ref_names: Optional[list[str]] = None) -> dict:
        g = self._graph
        nodes: dict = {}

        # ComfyUI 的文件列表均为「文件名」（子目录由 extra_model_paths 展开映射），
        # 因此这里统一取 basename，兼容 .env 里带 text_encoders/ 前缀的配置
        nodes["1"] = g("UnetLoaderGGUF",
                       unet_name=Path(settings.transformer_file).name)
        nodes["2"] = g("CLIPLoader",
                       clip_name=Path(settings.text_encoder_file).name,
                       type="qwen_image", device="default")
        nodes["3"] = g("VAELoader", vae_name=Path(settings.vae_file).name)
        nodes["4"] = g("ModelSamplingAuraFlow",
                       model=["1", 0], shift=3.1)

        if ref_names:
            # ---- Qwen-Image 2.1 原生编辑（多参考图融合）----
            # 这是官方用法：TextEncodeQwenImage21 把参考图**同时**喂给 Qwen3-VL 视觉塔
            # （模型真正"看见"图）并按序列拼进 VAE latent，模型据此理解
            # "把 image_2 里的人物放进 image_1 的场景"。
            # 官方采样约定：latent 必须用该节点输出的空 latent（尺寸跟随 image_1），
            # CFG=1、denoise=1；prompt 里用 image_1/image_2… 按序号引用参考图。
            slots: dict[str, Any] = {}
            for i, name in enumerate(ref_names, start=1):
                nid = str(20 + i)  # 21, 22, ...
                nodes[nid] = g("LoadImage", image=name)
                slots[f"image_{i}"] = [nid, 0]
            # resolution=0：参考图保持自身尺寸（调用方已按需缩放）。
            # 不能改成别的采样尺寸，否则编辑会整体偏移（官方文档明确警告）。
            nodes["12"] = g("TextEncodeQwenImage21",
                            clip=["2", 0], prompt=prompt,
                            negative_prompt=negative or "",
                            vae=["3", 0], resolution=0, images=slots)
            nodes["9"] = g("KSampler",
                           model=["4", 0],
                           positive=["12", 0],
                           negative=["12", 1],
                           latent_image=["12", 2],
                           seed=seed, steps=steps,
                           cfg=1.0,          # 官方 2.1 路径固定 CFG=1（此时负面提示词不生效）
                           sampler_name="euler",
                           scheduler="simple",
                           denoise=1.0)
            nodes["10"] = g("VAEDecode", samples=["9", 0], vae=["3", 0])
            nodes["11"] = g("SaveImage", images=["10", 0],
                            filename_prefix="qwenimages/api")
            return {"prompt": nodes, "client_id": self.client_id}

        nodes["5"] = g("CLIPTextEncode", clip=["2", 0], text=prompt)
        nodes["6"] = g("CLIPTextEncode", clip=["2", 0],
                       text=negative or "")

        if ref_name:
            # 传统图生图：参考图编码回 latent，按 strength 降噪
            nodes["7"] = g("LoadImage", image=ref_name)
            nodes["8"] = g("VAEEncode", pixels=["7", 0], vae=["3", 0])
            latent = ["8", 0]
            denoise = strength
        else:
            nodes["7"] = g("EmptySD3LatentImage",
                           width=width, height=height, batch_size=1)
            latent = ["7", 0]
            denoise = 1.0

        nodes["9"] = g("KSampler",
                       model=["4", 0],
                       positive=["5", 0],
                       negative=["6", 0],
                       latent_image=latent,
                       seed=seed, steps=steps,
                       cfg=guidance_scale,
                       sampler_name="euler",
                       scheduler="simple",
                       denoise=denoise)
        nodes["10"] = g("VAEDecode", samples=["9", 0], vae=["3", 0])
        nodes["11"] = g("SaveImage", images=["10", 0],
                        filename_prefix="qwenimages/api")
        return {"prompt": nodes, "client_id": self.client_id}

    @staticmethod
    def _graph(class_type: str, **inputs: object) -> dict:
        return {"class_type": class_type, "inputs": inputs}

    @staticmethod
    def _speed_hint() -> str:
        """基于实测的速度提示（RTX 2060 6GB + --lowvram：约 260 秒/步/百万像素）。"""
        return "每步约 2-2.5 分钟（768 分辨率），8 步约 18 分钟"

    # ================= 请求与进度 =================
    def _upload_reference(self, reference: Image.Image) -> str:
        """把参考图上传给 ComfyUI，返回 LoadImage 用的文件名。"""
        buf = _io.BytesIO()
        reference.convert("RGB").save(buf, "PNG")
        boundary = uuid.uuid4().hex
        name = f"ref_{uuid.uuid4().hex[:8]}.png"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode() + buf.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{settings.comfyui_url}/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with comfy_http.open(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data.get("name", name)

    def _post_prompt(self, workflow: dict) -> str:
        req = urllib.request.Request(
            f"{settings.comfyui_url}/prompt",
            data=json.dumps(workflow).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with comfy_http.open(req, timeout=30) as resp:
                return json.loads(resp.read())["prompt_id"]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:600]
            raise RuntimeError(f"ComfyUI 拒绝了工作流（HTTP {exc.code}）：{detail}") from exc

    def _listen_progress(self, prompt_id: str, steps: int,
                         progress_cb: Optional[Callable], timeout: float) -> dict:
        """订阅 WebSocket 直到该 prompt 完成，返回 history 记录。"""
        import websocket  # websocket-client

        ws_url = settings.comfyui_url.replace("http", "ws", 1) + f"/ws?clientId={self.client_id}"
        ws = websocket.create_connection(ws_url, timeout=10)
        deadline = time.time() + timeout
        last_pct = 0
        try:
            while time.time() < deadline:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if isinstance(raw, (bytes, bytearray)):
                    continue  # 预览二进制帧，忽略
                msg = json.loads(raw)
                mtype = msg.get("type")
                data = msg.get("data", {})
                if data.get("prompt_id") != prompt_id and mtype in ("progress", "executing"):
                    continue
                if mtype == "progress":
                    value, max_v = data.get("value", 0), data.get("max", steps)
                    pct = int(value / max(1, max_v) * 92)
                    if pct != last_pct and progress_cb:
                        progress_cb(pct, f"采样 {value}/{max_v}")
                    last_pct = pct
                elif mtype == "executing":
                    if data.get("node") is None:  # 全图执行完毕
                        break
                elif mtype == "execution_error":
                    raise RuntimeError(
                        f"ComfyUI 执行出错（节点 {data.get('node_type')}）："
                        f"{data.get('exception_message', '')}"
                    )
            else:
                raise TimeoutError(f"等待 ComfyUI 执行超时（{int(timeout)}s）")
        finally:
            ws.close()

        with comfy_http.open(
            f"{settings.comfyui_url}/history/{prompt_id}", timeout=15
        ) as resp:
            history = json.loads(resp.read())
        if prompt_id not in history:
            raise RuntimeError("ComfyUI 历史中找不到该任务（可能被清除）")
        return history[prompt_id]

    def _fetch_output(self, history: dict) -> Image.Image:
        outputs = history.get("outputs", {})
        for node_out in outputs.values():
            for img in node_out.get("images", []):
                if img.get("type") not in (None, "output"):
                    continue  # 跳过临时预览
                q = urllib.parse.urlencode({
                    "filename": img["filename"],
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                })
                with comfy_http.open(
                    f"{settings.comfyui_url}/view?{q}", timeout=60
                ) as resp:
                    return Image.open(_io.BytesIO(resp.read())).copy()
        raise RuntimeError(f"ComfyUI 输出中未找到图片：{list(outputs.keys())}")

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
        import random

        if not self.loaded:
            self.load()
        if progress_cb:
            progress_cb(2, "提交工作流")

        if seed is None or seed < 0:
            seed = random.randint(0, 2 ** 31 - 1)

        # references（多图）→ Qwen-Image 2.1 原生编辑融合；reference（单图）→ 传统图生图
        ref_names: Optional[list[str]] = None
        if references:
            ref_names = [self._upload_reference(img) for img in references]
            if progress_cb:
                progress_cb(4, f"已上传 {len(ref_names)} 张参考图")
        ref_name = self._upload_reference(reference) if reference is not None else None

        workflow = self._build_workflow(
            prompt=prompt, negative=negative, width=width, height=height,
            steps=steps, guidance_scale=guidance_scale, seed=seed,
            ref_name=ref_name, strength=strength, ref_names=ref_names,
        )
        started = time.time()
        prompt_id = self._post_prompt(workflow)
        self._current_prompt_id = prompt_id
        if progress_cb:
            progress_cb(5, "排队中")

        # 引擎侧超时要略早于路由层的 wait_for，保证取消逻辑能执行
        engine_timeout = max(120, settings.infer_timeout_seconds - 30)
        try:
            history = self._listen_progress(
                prompt_id, steps, progress_cb,
                timeout=engine_timeout,
            )
            if progress_cb:
                progress_cb(95, "取回图片")
            image = self._fetch_output(history)
        except TimeoutError:
            self.cancel()
            minutes = engine_timeout // 60
            raise RuntimeError(
                f"推理超时（>{minutes} 分钟），已取消 ComfyUI 端的任务。\n"
                f"本机实测速度约 {self._speed_hint()}；请降低分辨率或步数后重试。"
            )
        except Exception:
            self.cancel()  # 任何失败都要清理，防止 ComfyUI 积压拖垮后续任务
            raise
        finally:
            self._current_prompt_id = None

        if progress_cb:
            progress_cb(99, "完成")
        return EngineResult(
            image=image,
            seed=seed,
            elapsed_ms=int((time.time() - started) * 1000),
            engine=self.name,
        )
