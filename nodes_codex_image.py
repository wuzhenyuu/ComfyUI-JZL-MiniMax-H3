"""Generate or edit native-resolution images with the locally signed-in Codex."""

import json
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageOps, UnidentifiedImageError

import comfy.model_management
import folder_paths
from comfy_api.latest import io

from .codex_backend import CODEX_PROVIDER, CodexBackendError, generate_images


class JZL_CodexImageGenerate(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="JZL_CodexImageGenerate",
            display_name="JZL Codex 生图/改图（本机）",
            category="JZL/Codex",
            description="使用本机已登录 Codex。无参考图为文生图；连接参考图为改图或多参考。数量、尺寸和画面要求写入提示词，每次执行都会发起新请求。",
            not_idempotent=True,
            inputs=[
                io.String.Input("prompt", display_name="提示词", multiline=True, default="", dynamic_prompts=False),
                io.String.Input("codex_model", display_name="Codex 模型", default="",
                                tooltip="留空继承本机 Codex 模型设置。"),
                io.Combo.Input("codex_reasoning", display_name="推理强度",
                               options=["", "low", "medium", "high", "xhigh"], default="high",
                               tooltip="留空继承本机 Codex 设置。"),
                io.Int.Input("codex_timeout", display_name="超时（秒）", default=1800, min=1, max=86400),
                io.Autogrow.Input("reference_images", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input("reference_image", display_name="参考图"),
                        prefix="reference_image_", min=0, max=100)),
            ],
            outputs=[
                io.Image.Output("images", display_name="图像", is_output_list=True),
                io.Mask.Output("masks", display_name="透明遮罩", is_output_list=True),
                io.String.Output("info", display_name="生成信息"),
            ],
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    def execute(cls, prompt, codex_model="", codex_reasoning="high", codex_timeout=1800,
                reference_images=None) -> io.NodeOutput:
        comfy.model_management.throw_exception_if_processing_interrupted()
        parent = Path(folder_paths.get_output_directory()).resolve() / "jzl" / "codex"
        parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d_%H%M%S")
        output_dir = parent / f"{stamp}_{uuid.uuid4().hex}"
        output_dir.mkdir()
        config = {"codex_model": codex_model, "codex_reasoning": codex_reasoning,
                  "codex_timeout": codex_timeout}

        with tempfile.TemporaryDirectory(prefix="jzl_codex_refs_") as temporary:
            reference_paths = []
            ordered = sorted((reference_images or {}).items(), key=lambda item: int(item[0].rsplit("_", 1)[-1]))
            for name, batch in ordered:
                if batch is None:
                    continue
                if batch.ndim != 4 or batch.shape[-1] not in (3, 4):
                    raise CodexBackendError(f"参考图 {name} 需要 [B,H,W,3/4] IMAGE 输入。")
                for image in batch:
                    comfy.model_management.throw_exception_if_processing_interrupted()
                    pixels = image.detach().cpu().float().clamp(0, 1).mul(255).round().to(torch.uint8).numpy()
                    path = Path(temporary) / f"reference_{len(reference_paths):04d}.png"
                    Image.fromarray(pixels).save(path, format="PNG")
                    reference_paths.append(path)
            paths = generate_images(prompt, reference_paths, output_dir, config=config)

        comfy.model_management.throw_exception_if_processing_interrupted()
        if not paths:
            raise CodexBackendError("Codex 未返回可用图片。")
        images, masks, records = [], [], []
        for returned_path in paths:
            comfy.model_management.throw_exception_if_processing_interrupted()
            path = Path(returned_path).resolve()
            if not path.is_relative_to(output_dir):
                raise CodexBackendError("Codex 返回图片不在本次输出目录中。")
            try:
                with Image.open(path) as source:
                    image = ImageOps.exif_transpose(source)
                    has_alpha = "A" in image.getbands() or "transparency" in image.info
                    rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / np.float32(255)
                    if has_alpha:
                        alpha = np.asarray(image.convert("RGBA").getchannel("A"), dtype=np.float32) / np.float32(255)
                        mask = torch.from_numpy(1.0 - alpha)[None]
                    else:
                        mask = torch.zeros((1, image.height, image.width), dtype=torch.float32)
                    images.append(torch.from_numpy(rgb)[None])
                    masks.append(mask)
                    records.append({"path": str(path), "width": image.width, "height": image.height,
                                    "has_alpha": has_alpha})
            except (OSError, ValueError, UnidentifiedImageError) as exc:
                raise CodexBackendError(f"Codex 返回图片无法解码：{path.name}（{exc}）") from exc
        info = {"provider": CODEX_PROVIDER, "mode": "edit" if reference_paths else "generate",
                "reference_count": len(reference_paths), "requested_config": config,
                "output_dir": str(output_dir), "images": records}
        return io.NodeOutput(images, masks, json.dumps(info, ensure_ascii=False))
