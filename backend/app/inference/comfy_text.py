# -*- coding: utf-8 -*-
"""本地文本生成引擎（剧本创作 / 角色提取 / 场景提取 / 分镜拆解）。

设计要点
--------
* **不引入任何新的运行时、不额外下载模型**：直接复用 Qwen-Image 自带的
  Qwen3-VL-8B 文本编码器（`text_encoders/qwen3vl_8b_int8_convrot.safetensors`）。
  在 ComfyUI 中它被注册为 `CLIPLoader(type="qwen_image")`，其 `Qwen3VLClipModel`
  本身带 `generate()`，因此可以直接用官方 `TextGenerate` 节点做文本生成。
* 工作流：CLIPLoader → TextGenerate → SaveText（TextGenerate 本身不是输出节点，
  必须挂一个输出节点，否则 ComfyUI 会以 `prompt_no_outputs` 拒绝工作流）。
* 生成结果直接取自 `/history` 的 `outputs[node]["text"][0]`，不必读盘。
* 硬件实测量级（RTX 2060 6GB，文本编码器在 CPU）：约 20 秒完成一次短输出，
  225 字输出约 7 分钟。因此调用方必须按「长耗时任务」处理，带进度与超时。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from app.config import settings
from app.inference import comfy_http


class LocalTextError(RuntimeError):
    """本地文本模型不可用或生成失败。"""


class ComfyTextEngine:
    """通过 ComfyUI 的 TextGenerate 节点做本地文本生成（串行）。"""

    # 单次生成的兜底超时（秒），由 settings.llm_timeout_seconds 覆盖
    DEFAULT_TIMEOUT = 1800

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._client_id = f"qwenimages-llm-{uuid.uuid4().hex[:8]}"
        self._current_prompt_id: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_seconds: Optional[float] = None
        self.last_chars: Optional[int] = None

    # ================= 状态 =================
    @property
    def text_encoder_path(self) -> Path:
        return settings.text_encoder_path

    def _reachable(self, timeout: float = 3.0) -> bool:
        try:
            with comfy_http.open(f"{settings.comfyui_url}/system_stats", timeout=timeout):
                return True
        except Exception:
            return False

    def status(self) -> dict:
        """供界面展示的可用性信息。"""
        mode = (settings.engine_mode or "mock").lower()
        encoder_exists = self.text_encoder_path.exists()
        # mock 模式下没有真实推理通道，但也不应谎报可用
        if not settings.llm_enabled:
            available, reason = False, "本地文本模型已在配置中关闭（LLM_ENABLED=false）"
        elif mode != "comfy":
            available, reason = False, (
                f"本地文本模型需要 ENGINE_MODE=comfy（当前 {mode}）。"
                "它复用 Qwen-Image 的文本编码器，不需要额外下载模型。"
            )
        elif not encoder_exists:
            available, reason = False, f"未找到文本编码器：{self.text_encoder_path}"
        elif not self._reachable():
            available, reason = False, "ComfyUI 未运行，首次调用时会自动拉起（约 1-2 分钟）"
        else:
            available, reason = True, ""
        return {
            "available": available,
            "reason": reason,
            "engine": "comfyui-textgenerate",
            "model": Path(settings.text_encoder_file).name,
            "model_gb": round(self.text_encoder_path.stat().st_size / 1024 ** 3, 2)
            if encoder_exists else 0.0,
            "device": "auto（ComfyUI 管理，低显存自动在显存/内存间搬运）",
            "last_error": self.last_error,
            "last_seconds": self.last_seconds,
            "last_chars": self.last_chars,
            "note": "复用 Qwen-Image 自带文本编码器，全部本地推理，不联网",
        }

    # ================= 工作流 =================
    @staticmethod
    def _graph(class_type: str, **inputs: Any) -> dict:
        return {"class_type": class_type, "inputs": inputs}

    def _build_workflow(self, *, prompt: str, system: str, max_length: int,
                        temperature: Optional[float], seed: int) -> dict:
        g = self._graph
        nodes: dict[str, Any] = {}
        # 注意：文本生成必须用 ComfyUI 的默认设备（cuda）。
        # .env 里的 TEXT_ENCODER_DEVICE=cpu 是为「出图」工作流的显存布局调的，
        # 在 TextGenerate 的 generate 路径上会直接报 "Expected a cuda device, but got: cpu"。
        # 这里交给 ComfyUI 自己调度（--lowvram 下会自动在显存/内存间搬运）。
        nodes["1"] = g(
            "CLIPLoader",
            clip_name=Path(settings.text_encoder_file).name,
            type="qwen_image",
            device="default",
        )

        inputs: dict[str, Any] = {
            "clip": ["1", 0],
            "prompt": prompt,
            "max_length": max(16, min(int(max_length), 16384)),
            "thinking": False,
            "use_default_template": True,
            "mtp": "off",
        }
        if temperature is None:
            # 贪心解码：结构化提取场景下更稳定、可复现
            inputs["sampling_mode"] = "off"
        else:
            inputs["sampling_mode"] = "on"
            inputs.update({
                "sampling_mode.temperature": float(temperature),
                "sampling_mode.top_k": 40,
                "sampling_mode.top_p": 0.9,
                "sampling_mode.min_p": 0.05,
                "sampling_mode.repetition_penalty": 1.05,
                "sampling_mode.seed": int(seed),
                "sampling_mode.presence_penalty": 0.0,
            })
        if system:
            # system_prompt 是 forceInput，需要由字符串节点提供
            nodes["9"] = g("PrimitiveStringMultiline", value=system)
            inputs["system_prompt"] = ["9", 0]

        nodes["2"] = g("TextGenerate", **inputs)
        nodes["3"] = g(
            "SaveText",
            text=["2", 0],
            filename_prefix="comic_llm/out",
            format="txt",
        )
        return {"prompt": nodes, "client_id": self._client_id}

    # ================= 请求 =================
    def _ensure_ready(self) -> None:
        """确保 ComfyUI 在跑（必要时复用出图引擎的拉起逻辑）。"""
        if self._reachable():
            return
        mode = (settings.engine_mode or "mock").lower()
        if mode != "comfy":
            raise LocalTextError(
                "本地文本模型需要 ENGINE_MODE=comfy（它复用 Qwen-Image 的文本编码器）。"
            )
        from app.inference.manager import manager

        try:
            manager.ensure_loaded()  # 内部会按需拉起 ComfyUI 子进程
        except Exception as exc:  # pragma: no cover - 环境相关
            raise LocalTextError(f"ComfyUI 启动失败：{exc}") from exc
        if not self._reachable():
            raise LocalTextError(f"ComfyUI 不可达（{settings.comfyui_url}）")

    def _post(self, workflow: dict) -> str:
        req = urllib.request.Request(
            f"{settings.comfyui_url}/prompt",
            data=json.dumps(workflow).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with comfy_http.open(req, timeout=60) as resp:
                return json.loads(resp.read())["prompt_id"]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:600]
            raise LocalTextError(f"ComfyUI 拒绝了文本工作流（HTTP {exc.code}）：{detail}") from exc

    def _history(self, prompt_id: str) -> Optional[dict]:
        try:
            with comfy_http.open(
                f"{settings.comfyui_url}/history/{prompt_id}", timeout=30
            ) as resp:
                data = json.loads(resp.read())
        except Exception:
            return None
        return data.get(prompt_id)

    @staticmethod
    def _pick_text(record: dict) -> str:
        for out in (record.get("outputs") or {}).values():
            if not isinstance(out, dict):
                continue
            for key in ("text", "generated_text", "string"):
                val = out.get(key)
                if val:
                    return val[0] if isinstance(val, list) else str(val)
        return ""

    def cancel(self) -> None:
        """中断当前文本生成（超时或用户取消时必须调用，避免任务积压）。"""
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
            req = urllib.request.Request(
                f"{settings.comfyui_url}/interrupt", data=b"", method="POST"
            )
            comfy_http.open(req, timeout=10)
        except Exception:
            pass

    # ================= 对外接口 =================
    def generate(self, prompt: str, *, system: str = "", max_length: Optional[int] = None,
                 temperature: Optional[float] = None, seed: int = 0,
                 timeout: Optional[float] = None,
                 on_progress=None) -> str:
        """生成文本。同一时刻只允许一个任务（ComfyUI 侧也是串行执行的）。"""
        if not settings.llm_enabled:
            raise LocalTextError("本地文本模型已关闭（LLM_ENABLED=false）")
        limit = int(max_length or settings.llm_max_length)
        budget = float(timeout or settings.llm_timeout_seconds)

        with self._lock:
            self._ensure_ready()
            workflow = self._build_workflow(
                prompt=prompt, system=system, max_length=limit,
                temperature=temperature, seed=seed,
            )
            started = time.time()
            prompt_id = self._post(workflow)
            self._current_prompt_id = prompt_id
            try:
                while True:
                    if time.time() - started > budget:
                        self.cancel()
                        raise LocalTextError(
                            f"文本生成超时（>{int(budget)} 秒）。"
                            "本机文本推理在 CPU 上较慢，可减小 max_length 或精简剧本后重试。"
                        )
                    record = self._history(prompt_id)
                    if record is not None:
                        state = (record.get("status") or {}).get("status_str")
                        if state == "success":
                            text = self._pick_text(record).strip()
                            self.last_seconds = round(time.time() - started, 1)
                            self.last_chars = len(text)
                            self.last_error = None
                            return text
                        if state == "error":
                            self.cancel()
                            raise LocalTextError(self._error_message(record))
                    if on_progress:
                        try:
                            on_progress(round(time.time() - started, 1))
                        except Exception:
                            pass
                    time.sleep(2.0)
            finally:
                self._current_prompt_id = None

    @staticmethod
    def _error_message(record: dict) -> str:
        for msg in (record.get("status") or {}).get("messages", []):
            if msg and msg[0] == "execution_error":
                info = msg[1] or {}
                return (f"文本模型执行失败（{info.get('node_type', '?')}）："
                        f"{info.get('exception_message', '未知错误')}")
        return "文本模型执行失败"


text_engine = ComfyTextEngine()
