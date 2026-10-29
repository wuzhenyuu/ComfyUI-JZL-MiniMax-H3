"""Optional local ChatGPT-login backend; importing this module does not run Codex."""

from __future__ import annotations

import importlib
import os
import json
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import types
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4


CODEX_PROVIDER = "Codex (本机登录)"
_RUNNER_PACKAGE = "_jzl_local_codex_cli"
_RUNNER_LOCK = threading.Lock()
_CATALOG_LOCK = threading.Lock()
_CATALOG = None
_CATALOG_TIME = 0.0
_IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}


class CodexBackendError(RuntimeError):
    pass


def model_catalog(refresh: bool = False) -> dict:
    """Ask the installed CLI for its model menu; this does not create a model turn."""
    global _CATALOG, _CATALOG_TIME
    with _CATALOG_LOCK:
        if not refresh and _CATALOG is not None and time.monotonic() - _CATALOG_TIME < 60:
            return _CATALOG
        runner = _load_runner()
        executable, env = _runtime(runner)
        messages = queue.Queue()
        deadline = time.monotonic() + 20
        with _work_directory() as work_dir:
            process = subprocess.Popen(
                [str(executable), "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=work_dir, env=env, text=True, encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            def read_messages():
                try:
                    for line in process.stdout:
                        try:
                            value = json.loads(line)
                            if isinstance(value, dict):
                                messages.put(value)
                        except ValueError:
                            pass
                finally:
                    messages.put(None)

            reader = threading.Thread(target=read_messages, daemon=True)
            reader.start()

            def request(request_id, method, params):
                process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
                process.stdin.flush()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CodexBackendError("识别 Codex 模型超时，请稍后重新识别。")
                    message = messages.get(timeout=remaining)
                    if message is None:
                        raise CodexBackendError("Codex 模型目录连接提前关闭，请检查本机 CLI。")
                    if message.get("id") == request_id:
                        if "error" in message or not isinstance(message.get("result"), dict):
                            raise CodexBackendError("本机 Codex 未返回模型目录，请检查 CLI 版本和登录。")
                        return message["result"]

            try:
                request(1, "initialize", {"clientInfo": {"name": "jzl_codex_catalog", "version": "1.0"}})
                process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
                process.stdin.flush()
                models, cursor, seen_cursors = [], None, set()
                while True:
                    result = request(2 + len(seen_cursors), "model/list", {"limit": 100, "includeHidden": False, "cursor": cursor})
                    for item in result.get("data", []):
                        if not isinstance(item, dict) or item.get("hidden"):
                            continue
                        model = item.get("model")
                        if not isinstance(model, str) or not model.strip() or any(x["id"] == model for x in models):
                            continue
                        models.append({"id": model, "name": item.get("displayName") or model,
                                       "is_default": bool(item.get("isDefault"))})
                    cursor = result.get("nextCursor")
                    if not cursor:
                        break
                    if not isinstance(cursor, str) or cursor in seen_cursors:
                        raise CodexBackendError("Codex 模型目录分页无效，未使用不完整结果。")
                    seen_cursors.add(cursor)
                if not models:
                    raise CodexBackendError("本机 Codex 模型目录为空，请检查登录和账户权限。")
                _CATALOG = {"models": models, "source": "codex-app-server/model/list",
                            "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat()}
                _CATALOG_TIME = time.monotonic()
                return _CATALOG
            except (OSError, ValueError, queue.Empty):
                raise CodexBackendError("无法识别本机 Codex 模型，请检查 CLI 登录与连接后重新识别。") from None
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                reader.join(timeout=1)
                process.stdout.close()


def _load_runner():
    runtime_dir = Path(__file__).resolve().parent.parent / "comfyui-codex-cli-nodes"
    if not (runtime_dir / "codex_runner.py").is_file() or not (runtime_dir / "codex_events.py").is_file():
        raise CodexBackendError("本机 Codex 接口需要相邻的 comfyui-codex-cli-nodes 执行层，请先恢复该本机依赖。")
    with _RUNNER_LOCK:
        if _RUNNER_PACKAGE not in sys.modules:
            package = types.ModuleType(_RUNNER_PACKAGE)
            package.__path__ = [str(runtime_dir)]
            sys.modules[_RUNNER_PACKAGE] = package
        try:
            return importlib.import_module(f"{_RUNNER_PACKAGE}.codex_runner")
        except (ImportError, OSError):
            raise CodexBackendError("本机 Codex 执行层无法加载，请检查 ComfyUI 和 comfyui-codex-cli-nodes 的依赖。") from None


def _settings(config: dict | None) -> tuple[str | None, str | None, int]:
    config = config or {}
    if not isinstance(config, dict):
        raise CodexBackendError("Codex 配置需要是对象。")
    model = config.get("codex_model", "")
    reasoning = config.get("codex_reasoning", "high")
    if not isinstance(model, str) or not isinstance(reasoning, str):
        raise CodexBackendError("Codex 模型和推理强度需要是文本。")
    model = model.strip() or None
    reasoning = reasoning.strip() or None
    if model is not None and (len(model) > 200 or any(ord(char) < 32 for char in model)):
        raise CodexBackendError("Codex 模型名称无效。")
    if reasoning not in (None, "low", "medium", "high", "xhigh"):
        raise CodexBackendError("Codex 推理强度支持 low、medium、high、xhigh，留空继承本机配置。")
    raw_timeout = config.get("codex_timeout", 1800)
    try:
        timeout = int(raw_timeout)
        if isinstance(raw_timeout, bool) or float(raw_timeout) != timeout or not 1 <= timeout <= 86400:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise CodexBackendError("Codex 超时需要是 1 至 86400 秒的整数。") from None
    return model, reasoning, timeout


def _runtime(runner):
    try:
        executable, env = runner.resolve_chatgpt_runtime(dict(os.environ))
    except runner.CodexConfigurationError:
        raise CodexBackendError("找不到可用的本机 Codex CLI，请检查 PATH 或 CODEX_CLI_EXE。") from None
    except (runner.CodexExecutionError, runner.CodexTimeoutError, OSError):
        raise CodexBackendError("本机 Codex 尚未确认 ChatGPT 登录，请在运行 ComfyUI 的同一 Windows 用户下检查 codex login status。") from None
    home_value = next((value for key, value in env.items() if key.upper() == "CODEX_HOME"), None)
    codex_home = Path(home_value) if home_value else Path.home() / ".codex"
    if not codex_home.is_absolute():
        raise CodexBackendError("本机 CODEX_HOME 需要使用绝对路径。")
    codex_home = codex_home.resolve()
    env = {key: value for key, value in env.items() if key.upper() not in {
        "CODEX_HOME", "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_BASE_URL"
    }}
    env["CODEX_HOME"] = str(codex_home)
    _check_provider(codex_home)
    return executable, env


def _check_provider(codex_home: Path) -> None:
    # Inspect provider settings only; never open auth.json or copy login credentials.
    config_path = codex_home / "config.toml"
    if not config_path.is_file():
        return
    try:
        import tomllib

        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
        profile_name = config.get("profile")
        profile = config.get("profiles", {}).get(profile_name, {}) if profile_name else {}
        provider = profile.get("model_provider", config.get("model_provider", "openai"))
        custom_openai = "openai" in config.get("model_providers", {}) or "openai" in profile.get("model_providers", {})
        custom_url = profile.get("chatgpt_base_url", config.get("chatgpt_base_url"))
    except (ImportError, OSError, ValueError, TypeError, AttributeError):
        raise CodexBackendError("无法确认本机 Codex 的提供方设置，请检查 config.toml；需要 Python 3.11 或更新版本。") from None
    if (provider != "openai" or custom_openai
            or (custom_url is not None and (not isinstance(custom_url, str)
                                           or custom_url.rstrip("/") != "https://chatgpt.com/backend-api"))):
        raise CodexBackendError("本机 Codex 配置选择了其他或自定义提供方；此接口只支持官方 ChatGPT 登录，请先检查本机 Codex 配置。")


def _instruction(system_prompt: str, user_msg: str) -> str:
    if not isinstance(system_prompt, str) or not isinstance(user_msg, str) or not user_msg.strip():
        raise CodexBackendError("请提供非空的 Codex 用户请求。")
    marker = uuid4().hex
    while marker in system_prompt or marker in user_msg:
        marker = uuid4().hex
    return (
        "The SYSTEM section below contains the application's instructions. "
        "The USER section contains the user's original request. Apply SYSTEM instructions to the USER request.\n"
        f"[SYSTEM_{marker}]\n{system_prompt}\n[/SYSTEM_{marker}]\n"
        f"[USER_{marker}]\n{user_msg}\n[/USER_{marker}]"
    )


@contextmanager
def _work_directory():
    parent = Path(tempfile.gettempdir()).resolve(strict=True)
    work_dir = Path(tempfile.mkdtemp(prefix="jzl-codex-", dir=parent)).resolve(strict=True)
    try:
        yield work_dir
    finally:
        try:
            info = work_dir.lstat()
            is_reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if work_dir.parent == parent and work_dir.resolve() == work_dir and not work_dir.is_symlink() and not is_reparse:
                shutil.rmtree(work_dir)
        except (OSError, RuntimeError):
            pass


def _run(runner, instruction: str, image_paths: list[Path], work_dir: Path,
         config: dict | None, *, image_generation: bool):
    model, reasoning, timeout = _settings(config)
    executable, env = _runtime(runner)
    generated_root = Path(env["CODEX_HOME"]) / "generated_images"
    try:
        previous_sessions = {path.name for path in generated_root.iterdir()} if image_generation and generated_root.is_dir() else set()
        result = runner.run_codex(
            executable=executable, instruction=instruction, image_paths=image_paths,
            work_dir=work_dir, connect_timeout_seconds=min(60, timeout), connection_retries=0,
            task_timeout_seconds=timeout, env=env,
            sandbox_mode="workspace-write" if image_generation else "read-only",
            model=model, reasoning_effort=reasoning, provider_mode="chatgpt",
            enable_image_generation=image_generation, invocation_id=uuid4().hex,
        )
    except runner.CodexExecutionError as exc:
        messages = {
            "auth_error": "本机 Codex 登录已失效，请重新检查 ChatGPT 登录。",
            "rate_limited": "本机 Codex 额度或速率暂受限制，请稍后再试。",
            "task_timeout": "本机 Codex 任务超时；没有自动重试，可调整 Codex 超时后重新提交。",
            "connect_timeout": "连接本机 Codex 服务超时；没有自动重试，请检查网络后重新提交。",
            "network_error": "本机 Codex 网络连接失败；没有自动重试，请检查网络。",
            "capability_unsupported": "当前 Codex 模型不支持所需能力，请检查 Codex 模型及原生生图权限。",
            "model_incompatible": "当前 Codex 模型不兼容，请检查本机模型设置。",
            "protocol_error": "本机 Codex 返回格式无法识别，请检查 CLI 与执行层版本。",
        }
        raise CodexBackendError(messages.get(exc.code, "本机 Codex 执行失败；没有自动重试，请检查 CLI 登录和模型设置。")) from None
    except runner.CodexConfigurationError:
        raise CodexBackendError("本机 Codex 配置无效，请检查 CLI 路径、模型及推理强度。") from None
    except (runner.CodexTimeoutError, OSError, ValueError):
        raise CodexBackendError("本机 Codex 无法完成执行，请检查本机 CLI 和文件权限；没有自动重试。") from None
    return result, generated_root, previous_sessions


def chat(system_prompt: str, user_msg: str, config: dict) -> str:
    instruction = _instruction(system_prompt, user_msg)
    runner = _load_runner()
    with _work_directory() as work_dir:
        result, _, _ = _run(runner, instruction, [], work_dir, config, image_generation=False)
    if not isinstance(result.final_text, str) or not result.final_text.strip():
        raise CodexBackendError("本机 Codex 没有返回可用文字，请检查 CLI 任务结果。")
    return result.final_text


def _verify_image(path: Path) -> None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            if image.format != _IMAGE_FORMATS.get(path.suffix.lower()) or min(image.size) <= 0:
                raise ValueError
            image.verify()
        with Image.open(path) as image:
            image.load()
    except ImportError:
        raise CodexBackendError("本机 Codex 图片接口需要 ComfyUI 的 Pillow 图片依赖。") from None
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise CodexBackendError("Codex 图片文件损坏或格式与后缀不一致，未将其作为生成结果返回。") from None


def _output_directory(output_dir: Path) -> Path:
    try:
        import folder_paths

        output_root = Path(folder_paths.get_output_directory()).resolve(strict=True)
        original = Path(output_dir)
        output_dir = original.resolve(strict=True)
        if (not original.is_absolute() or original != output_dir or not output_dir.is_dir()
                or output_dir == output_root or not output_dir.is_relative_to(output_root)):
            raise ValueError
        return output_dir
    except (ImportError, OSError, ValueError, RuntimeError):
        raise CodexBackendError("Codex 图片输出目录必须是已创建的 ComfyUI output 内独立子目录。") from None


def _image_hash(path: Path) -> bytes:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def generate_images(prompt: str, reference_paths: list[Path], output_dir: Path,
                    config: dict | None = None) -> list[Path]:
    output_dir = _output_directory(output_dir)
    references = []
    try:
        for path in reference_paths:
            path = Path(path).resolve(strict=True)
            if not path.is_file() or path.suffix.lower() not in _IMAGE_FORMATS:
                raise ValueError
            _verify_image(path)
            references.append(path)
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, CodexBackendError):
            raise
        raise CodexBackendError("Codex 参考图不存在或不是可读取的 PNG、JPEG、WebP 图片。") from None
    instruction = _instruction(
        "Use Codex's native image_generation tool to fulfill the user's request. "
        "Attached images are reference images in the original supplied order. "
        "Preserve requested identity, composition, dimensions and details. "
        "Generate native image assets; do not substitute scripts, HTML, SVG, shell rendering, "
        "upscaling, or a written description for actual generated images. "
        "Return the native generated image assets without copying or rewriting the input references.",
        prompt,
    )
    runner = _load_runner()
    copied = []
    try:
        reference_hashes = set()
        for path in references:
            reference_hashes.add(_image_hash(path))
        with _work_directory() as work_dir:
            result, generated_root, previous_sessions = _run(runner, instruction, references, work_dir, config, image_generation=True)
            thread_id = result.thread_id
            if (not isinstance(thread_id, str) or not thread_id or thread_id in {".", ".."}
                    or "/" in thread_id or "\\" in thread_id or ":" in thread_id):
                raise CodexBackendError("Codex 未返回本次生成线程，无法安全确认图片来源。")
            if thread_id in previous_sessions:
                raise CodexBackendError("Codex 返回了已存在的线程缓存，未将旧图片作为本次结果。")
            generated_root = generated_root.resolve(strict=True)
            session_dir = (generated_root / thread_id).resolve(strict=True)
            if session_dir.parent != generated_root or not session_dir.is_dir() or not result.generated_images:
                raise CodexBackendError("Codex 未返回本次线程的原生生成图片；文字回复不代表生图成功。")
            seen = set()
            for artifact in result.generated_images:
                source = artifact.path.resolve(strict=True)
                if (not source.is_file() or not source.is_relative_to(session_dir)
                        or source in references or source.suffix.lower() not in _IMAGE_FORMATS):
                    raise CodexBackendError("Codex 返回的图片不属于本次线程，未接收该结果。")
                if source in seen:
                    continue
                seen.add(source)
                _verify_image(source)
                if _image_hash(source) in reference_hashes:
                    raise CodexBackendError("Codex 返回的是原始参考图，未将其作为生成结果。")
                destination = output_dir / f"codex_{uuid4().hex}{source.suffix.lower()}"
                copied.append(destination)
                with source.open("rb") as source_file, destination.open("xb") as destination_file:
                    shutil.copyfileobj(source_file, destination_file)
                _verify_image(destination)
        return copied
    except (OSError, ValueError, RuntimeError) as exc:
        for path in copied:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, CodexBackendError):
            raise
        raise CodexBackendError("未找到可确认来源的本次 Codex 原生图片，或无法保存图片；原始 Codex 缓存保留。") from None
