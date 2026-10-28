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

from shared.config import settings
from engine.infer import comfy_http
from engine.infer.base import EngineResult
from engine.infer.schema_guard import ensure_inputs_reach

# ComfyUI 子进程句柄放在**模块级**，不挂在实例上。
# manager.unload() 会把引擎实例丢掉，但子进程还在跑；句柄一丢就既杀不掉也拉不起来
# （8188 端口被占），「卡死后自动重启引擎」这条路会直接失效。
_COMFY_PROC: Optional[subprocess.Popen] = None


class ComfyEngine:
    """通过 ComfyUI 服务端推理。"""

    name = "comfy"

    def __init__(self) -> None:
        self._ready = False
        self.last_error: Optional[str] = None
        self.last_load_seconds: Optional[float] = None
        self.client_id = f"qwenimages-{uuid.uuid4().hex[:8]}"
        self._current_prompt_id: Optional[str] = None

    @property
    def _proc(self) -> Optional[subprocess.Popen]:
        return _COMFY_PROC

    @_proc.setter
    def _proc(self, value: Optional[subprocess.Popen]) -> None:
        global _COMFY_PROC
        _COMFY_PROC = value

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

    # ================= 卡死恢复 =================
    def interrupt_all(self) -> bool:
        """软恢复：清空 ComfyUI 队列 + 中断当前执行 + 释放显存。不重启进程。"""
        ok = False
        calls = (
            ("/queue", json.dumps({"clear": True}).encode()),
            ("/interrupt", b""),
            ("/free", json.dumps({"unload_models": False, "free_memory": True}).encode()),
        )
        for path, payload in calls:
            try:
                req = urllib.request.Request(
                    f"{settings.comfyui_url}{path}", data=payload,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with comfy_http.open(req, timeout=10):
                    ok = True
            except Exception:
                continue
        self._current_prompt_id = None
        return ok

    def hard_restart(self, graceful_seconds: float = 15.0) -> tuple[bool, str]:
        """硬恢复：结束本服务拉起的 ComfyUI 子进程并重新拉起。

        卡死的典型形态是 ComfyUI 进程本身失去响应（依赖冲突导致启动即退、
        CUDA 上下文崩溃、显存被别的进程抢光），这时 /interrupt 也一起失效，
        只有重启进程才能解开——挂起的 socket 一断，卡在等待里的调用方线程
        必然抛错退出。

        只处理自己拉起的子进程；若 ComfyUI 是用户手动启动的，退回软中断并
        明确告知，不替用户杀进程。
        """
        self.interrupt_all()  # 先尽力让正在跑的 prompt 收尾
        proc, self._proc = self._proc, None
        self._ready = False

        if proc is None or proc.poll() is not None:
            self.last_error = (
                "ComfyUI 不是由本服务拉起的（未持有进程句柄），无法自动重启；"
                "已请求中断，若仍无响应请手动重启 ComfyUI。"
            )
            return False, self.last_error

        try:
            proc.terminate()
            try:
                proc.wait(timeout=graceful_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        except Exception as exc:  # pragma: no cover - 平台相关
            self.last_error = f"结束 ComfyUI 进程失败：{exc}"
            return False, self.last_error

        try:
            self.load()  # 进程已退出，load() 会重新 _spawn 并等待就绪
        except Exception as exc:
            self.last_error = f"ComfyUI 重启失败：{exc}"
            return False, self.last_error
        return True, f"ComfyUI 已重启并就绪（{self.last_load_seconds}s）"

    # ================= 控制图能力 =================
    def control_capability(self, refresh: bool = False) -> tuple:
        """探测本机的控制通路（结果缓存 60 秒，避免每次出图都问一遍 ComfyUI）。"""
        now = time.time()
        cached = getattr(self, "_control_cache", None)
        if cached and not refresh and now - cached[0] < 60:
            return cached[1], cached[2]
        from engine.infer import controlnet

        route, why = controlnet.detect()
        self._control_cache = (now, route, why)
        return route, why

    def control_status(self) -> dict:
        """给接口 / 前端看的能力表（不抛错）。"""
        route, why = self.control_capability()
        return {
            "available": route is not None,
            "route": getattr(route, "route", None),
            "label": getattr(route, "label", None),
            "model": getattr(route, "model_name", None),
            "detail": why or getattr(route, "detail", ""),
        }

    # ================= 工作流 =================
    def _build_workflow(self, *, prompt: str, negative: str, width: int, height: int,
                        steps: int, guidance_scale: float, seed: int,
                        ref_name: Optional[str], strength: float,
                        ref_names: Optional[list[str]] = None,
                        control: Optional[dict] = None) -> dict:
        """control = {route, name, strength, start, end}；None 表示不加控制。"""
        g = self._graph
        nodes: dict = {}
        route = (control or {}).get("route")

        if control:
            nodes["40"] = g("LoadImage", image=control["name"])
            control_link = ["40", 0]

        # ComfyUI 的文件列表均为「文件名」（子目录由 extra_model_paths 展开映射），
        # 因此这里统一取 basename，兼容 .env 里带 text_encoders/ 前缀的配置
        nodes["1"] = g("UnetLoaderGGUF",
                       unet_name=Path(settings.transformer_file).name)
        nodes["2"] = g("CLIPLoader",
                       clip_name=Path(settings.text_encoder_file).name,
                       type="qwen_image", device="default")
        nodes["3"] = g("VAELoader", vae_name=Path(settings.vae_file).name)

        # ---- 采样偏移（shift）----
        # Qwen-Image 2.1 在 supported_models.py 里自带 shift=0.69 / multiplier=1.0，
        # 这是官方按模型调好的 sigma 调度。旧版 Qwen-Image(1.0/2509) 模板里的
        # 3.1 是给 1.15 基线的，套到 2.1 上等于把噪声曲线整体抬高 4 倍多：
        # 采样绝大部分步数耗在高噪段，参考图条件被稀释——表现就是「长得不像」。
        # 因此默认 0 = 不覆盖，直接用模型自带调度；只有在明确要调时才设值。
        shift = float(getattr(settings, "sampling_shift", 0.0) or 0.0)
        if shift > 0:
            nodes["4"] = g("ModelSamplingAuraFlow", model=["1", 0], shift=shift)
            model_link = ["4", 0]
        else:
            model_link = ["1", 0]

        if ref_names:
            # ---- Qwen-Image 2.1 原生编辑（多参考图融合）----
            # 这是官方用法：TextEncodeQwenImage21 把参考图**同时**喂给 Qwen3-VL 视觉塔
            # （模型真正"看见"图）并按序列拼进 VAE latent，模型据此理解
            # "把 image_2 里的人物放进 image_1 的场景"。
            # 官方采样约定：latent 必须用该节点输出的空 latent（尺寸跟随 image_1），
            # CFG=1、denoise=1；prompt 里用 image_1/image_2… 按序号引用参考图。
            #
            # ⚠ 输入名必须是「点号拼接的扁平键」images.image_N
            # ---------------------------------------------------
            # `images` 在这个节点上是 Autogrow（动态输入），ComfyUI 解析 API
            # 格式时按 comfy_api/latest/_io.py 的 finalize_prefix() 用 "." 拼出
            # 每个动态槽位的真名（images.image_1 …）。若写成嵌套字典
            # images={"image_1": [...]}，展开后的 schema 里根本没有 "images"
            # 这个键 → execution.get_input_data 直接把整个输入丢掉 → 节点收到
            # images=None → 参考图一张都没进模型，编辑静默退化成文生图：
            # 不报错、不告警，每格各画一个人。这正是「角色一致性全乱」的根因。
            enc: dict[str, Any] = {
                "clip": ["2", 0],
                "prompt": prompt,
                "negative_prompt": negative or "",
                "vae": ["3", 0],
                # resolution=0：参考图保持自身尺寸（调用方已按需缩放）。
                # 不能改成别的采样尺寸，否则编辑会整体偏移（官方文档明确警告）。
                "resolution": 0,
            }
            for i, name in enumerate(ref_names, start=1):
                nid = str(20 + i)  # 21, 22, ...
                nodes[nid] = g("LoadImage", image=name)
                enc[f"images.image_{i}"] = [nid, 0]
            if route == "union":
                # Union 分支的参考图编码：image_1 决定画布，image_2/_3 只是外观参考，
                # 语义与 TextEncodeQwenImage21 的 images.image_N 一一对应
                uenc: dict[str, Any] = {
                    "clip": ["2", 0], "vae": ["3", 0],
                    "reference_image": enc.get("images.image_1"),
                    "prompt": prompt, "negative_prompt": negative or "",
                    "resolution": 0,
                }
                if enc.get("images.image_2"):
                    uenc["reference_image_2"] = enc["images.image_2"]
                if enc.get("images.image_3"):
                    uenc["reference_image_3"] = enc["images.image_3"]
                nodes["12"] = {"class_type": "QwenImage21UnionReferenceEncode",
                               "inputs": uenc}
            else:
                nodes["12"] = {"class_type": "TextEncodeQwenImage21", "inputs": enc}
            model_link = self._patch_control(nodes, model_link, control)
            nodes["9"] = g("KSampler",
                           model=model_link,
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

        if ref_name:
            # 传统图生图：参考图编码回 latent，按 strength 降噪（仅 1.0 基线；
            # 2.1 一律走上面 ref_names 融合路径）
            nodes["5"] = g("CLIPTextEncode", clip=["2", 0], text=prompt)
            nodes["6"] = g("CLIPTextEncode", clip=["2", 0],
                           text=negative or "")
            nodes["7"] = g("LoadImage", image=ref_name)
            nodes["8"] = g("VAEEncode", pixels=["7", 0], vae=["3", 0])
            latent = ["8", 0]
            denoise = strength
            cfg = guidance_scale
        else:
            # 2.1 官方 txt2img：TextEncodeQwenImage21 无参考编码 + CFG 固定 1。
            # 2.1 是 CFG 蒸馏模型，喂 cfg=guidance_scale(4.0) 会把整张图烧成
            # 纯黑（实测 2026-10-25）；负面提示词在 CFG=1 下不生效属官方约定。
            nodes["12"] = g("TextEncodeQwenImage21",
                            clip=["2", 0], prompt=prompt,
                            negative_prompt=negative or "",
                            vae=["3", 0], resolution=0)
            nodes["7"] = g("EmptySD3LatentImage",
                           width=width, height=height, batch_size=1)
            latent = ["7", 0]
            denoise = 1.0
            cfg = 1.0

        model_link = self._patch_control(nodes, model_link, control)

        nodes["9"] = g("KSampler",
                       model=model_link,
                       positive=["12" if ref_name is None else "5", 0],
                       negative=["12" if ref_name is None else "6", 0],
                       latent_image=latent,
                       seed=seed, steps=steps,
                       cfg=cfg,
                       sampler_name="euler",
                       scheduler="simple",
                       denoise=denoise)
        nodes["10"] = g("VAEDecode", samples=["9", 0], vae=["3", 0])
        nodes["11"] = g("SaveImage", images=["10", 0],
                        filename_prefix="qwenimages/api")
        return {"prompt": nodes, "client_id": self.client_id}

    def _patch_control(self, nodes: dict, model_link: list,
                       control: Optional[dict]) -> list:
        """把「控制图 + Union/Control-LoRA」接进模型链，返回新模型链接。

        两条编码分支（融合编辑 / 文本编码）都要经过这里——融合分支是提前
        return 的，只在尾部接会漏掉它。
        """
        if not control:
            return model_link
        from engine.infer import controlnet

        extra, link = controlnet.build_nodes(
            route=controlnet.ControlRoute(str(control["route"]), control["name"], ""),
            model_link=model_link, vae_link=["3", 0],
            control_link=["40", 0],
            strength=float(control.get("strength", 0.8)),
            start=float(control.get("start", 0.0)),
            end=float(control.get("end", 0.9)))
        nodes.update(extra)
        return link

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

    def _post_prompt(self, workflow: dict, expect_refs: int = 0) -> str:
        # 提交前先向真实服务核对节点契约：动态输入写错会被 ComfyUI 静默丢弃，
        # 那种失败不报错、只表现为"角色每格都不一样"，必须在这里拦住。
        ensure_inputs_reach(workflow, expect_refs=expect_refs)

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
        control: Optional[Image.Image] = None,
        control_mode: str = "pose",
        control_strength: float = 0.8,
        control_start: float = 0.0,
        control_end: float = 0.9,
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

        ctrl: Optional[dict] = None
        if control is not None:
            route, why = self.control_capability()
            if route is None:
                # 不能静默丢控制图：骨架没进模型，出来的还是那一堆「站着不动」，
                # 而用户以为已经开了姿态控制。报出来，让人知道该装什么。
                from engine.infer.controlnet import ControlUnavailable

                raise ControlUnavailable(f"姿态控制不可用：{why}")
            ctrl = {"route": route.route,
                    "name": self._upload_reference(control),
                    "strength": control_strength,
                    "start": control_start, "end": control_end}
            if progress_cb:
                progress_cb(4, f"控制图已上传（{route.label} · {control_mode}）")
            print(f"[engine] 控制图：mode={control_mode} strength={control_strength} "
                  f"range={control_start}-{control_end} route={route.route} "
                  f"({route.model_name})", flush=True)

        workflow = self._build_workflow(
            prompt=prompt, negative=negative, width=width, height=height,
            steps=steps, guidance_scale=guidance_scale, seed=seed,
            ref_name=ref_name, strength=strength, ref_names=ref_names,
            control=ctrl,
        )
        if ref_names:
            # 一行可核对的证据：日志里看到这行，才说明参考图真的进了工作流
            names = ", ".join(f"images.image_{i}" for i in range(1, len(ref_names) + 1))
            print(f"[engine] 融合编辑：{len(ref_names)} 张参考图 → {names}", flush=True)
        started = time.time()
        prompt_id = self._post_prompt(workflow, expect_refs=len(ref_names or []))
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
