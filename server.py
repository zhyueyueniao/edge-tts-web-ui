"""
edge-tts HTTP 服务：把 edge-tts 包装成一个常驻 HTTP 服务，供局域网内其他应用调用。

接口：
    GET  /health                     健康检查
    GET  /voices?locale=zh-CN        列出可用音色
    GET  /tts?text=你好&voice=...     GET 方式合成（返回 audio/mpeg）
    POST /tts                        JSON 方式合成（推荐，body 见 TTSRequest）
    POST /v1/audio/speech            OpenAI 兼容 TTS 接口（可被 OpenAI SDK / Open WebUI 等直接调用）
    GET  /v1/models                  OpenAI 兼容模型列表

环境变量：
    DEFAULT_VOICE   默认音色，默认 zh-CN-XiaoxiaoNeural
    MAX_TEXT_LEN    单条文本最大长度，默认 1000
    OPENAI_API_KEY  设置后 /v1/* 接口要求 Authorization: Bearer <key>，不设则不鉴权
    LOG_LEVEL       日志级别，默认 info
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat as stat_module
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import edge_tts
from edge_tts.version import __version__ as EDGE_TTS_VERSION
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "zh-CN-XiaoxiaoNeural")
MAX_TEXT_LEN = int(os.getenv("MAX_TEXT_LEN", "1000"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "info").upper())
log = logging.getLogger("edge-tts-server")

app = FastAPI(title="edge-tts server", version="1.0.0")


def _normalize_param(value: str | None, suffix: str, default: str) -> str:
    """把 '0' / '0%' / '10%' 归一化为 edge-tts 要求的 '+0%' / '-10%' 形式。

    edge-tts 的校验正则为 ^[+-]\\d+%$，不带符号会直接抛 ValueError。
    """
    if value is None:
        return default
    m = re.fullmatch(rf"([+-]?)(\d+)\s*{re.escape(suffix)}?", str(value).strip())
    if not m:
        return default
    sign, num = m.group(1) or "+", m.group(2)
    return f"{sign}{num}{suffix}"


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, description="待合成的文本")
    voice: str = Field(DEFAULT_VOICE, description="音色，如 zh-CN-XiaoxiaoNeural")
    rate: str = Field("+0%", description="语速，如 -20% / +10%")
    volume: str = Field("+0%", description="音量，如 -10% / +10%")
    pitch: str = Field("+0Hz", description="音调，如 -10Hz / +10Hz")


async def synthesize(text: str, voice: str, rate: str, volume: str, pitch: str) -> bytes:
    """调用 edge-tts 合成 MP3 音频，返回 bytes。"""
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=_normalize_param(rate, "%", "+0%"),
        volume=_normalize_param(volume, "%", "+0%"),
        pitch=_normalize_param(pitch, "Hz", "+0Hz"),
    )
    chunks: list[bytes] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])
    if not chunks:
        raise HTTPException(status_code=502, detail="未从 TTS 服务收到音频数据")
    return b"".join(chunks)


@app.get("/health", summary="健康检查", description="返回服务状态、edge-tts 版本与默认音色")
async def health() -> dict[str, Any]:
    return {"status": "ok", "edge_tts": EDGE_TTS_VERSION, "default_voice": DEFAULT_VOICE}


# ---------- Web 界面 ----------
STATIC_DIR = Path(__file__).resolve().parent / "static"

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """浏览器操作界面（单页，无外部依赖）。"""
        return FileResponse(str(STATIC_DIR / "index.html"))


# ==================== 自动更新（可选功能） ====================
# 两种更新方式：
#   1. 一键自动更新 —— 需挂载源码目录(SRC_DIR)与 docker.sock，且服务器能访问外网
#   2. 离线上传更新 —— 只需挂载 docker.sock；安装包在能上网的电脑上获取后从界面上传，
#      服务端仅执行 docker load + 重建容器，全程不需要 git 与外网
# 安全提示：挂载 docker.sock 等于把宿主机 Docker 控制权交给本容器，请仅在可信网络使用。
SRC_DIR = os.getenv("SRC_DIR", "").strip()
SELF_CONTAINER = os.getenv("SELF_CONTAINER", "edge-tts")
IMAGE_TAG = os.getenv("IMAGE_TAG", "edge-tts-web-ui")
DOCKER_SOCK = os.getenv("DOCKER_SOCK", "/var/run/docker.sock")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/tmp")


def _sock_ok() -> bool:
    """docker.sock 是否为真实套接字（wslc 等无 Docker 环境下挂载的会是普通目录）。"""
    try:
        return stat_module.S_ISSOCK(os.stat(DOCKER_SOCK).st_mode)
    except OSError:
        return False


_sock_ready = _sock_ok()
AUTO_UPDATE_ENABLED = _sock_ready and os.getenv("ENABLE_UPDATE", "1") not in ("0", "false")
UPLOAD_UPDATE_ENABLED = _sock_ready and os.getenv("ENABLE_UPDATE", "1") not in ("0", "false")
UPDATE_ENABLED = AUTO_UPDATE_ENABLED or UPLOAD_UPDATE_ENABLED
BUILD_ARGS = [
    f"--build-arg={key}={val}"
    for key, val in (("PIP_INDEX_URL", os.getenv("PIP_INDEX_URL", "")),
                     ("BASE_REGISTRY", os.getenv("BASE_REGISTRY", "")))
    if val
]

_update_lock = threading.Lock()
_update_state: dict[str, Any] = {
    "enabled": UPDATE_ENABLED,
    "state": "idle",              # idle | running | done | error
    "stage": "",                  # check / pull / build / deploy / package
    "percent": 0,
    "log": [],
    "error": None,
    "current_version": EDGE_TTS_VERSION,
    "latest_version": None,
    "new_image": None,
    "wheel_url": None,
}


def _ulog(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    _update_state["log"].append(f"[{ts}] {msg}")
    del _update_state["log"][:-200]
    log.info("[update] %s", msg)


def _set_stage(stage: str, percent: int) -> None:
    _update_state["stage"] = stage
    _update_state["percent"] = percent


def _run(cmd: list[str], cwd: str | None = None) -> str:
    """执行命令并把输出写入更新日志，失败时抛出异常。"""
    _ulog("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=900)
    for line in (proc.stdout or "").splitlines():
        if line.strip():
            _ulog(line.rstrip())
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-8:])
        raise RuntimeError(f"命令失败({proc.returncode}): {' '.join(cmd[:3])}\n{tail}")
    return proc.stdout or ""


GITHUB_RELEASES_URL = "https://github.com/rany2/edge-tts/releases/latest"


def _check_latest() -> tuple[str | None, str, str | None, str | None]:
    """查询 edge-tts 最新版本与安装包直链：先 PyPI，失败再 GitHub Releases。

    返回 (版本号, 来源, whl 直链, git tag 名)。
    注意：上游 tag 不带 v 前缀（如 7.3.1），生成 archive 链接必须用原始 tag。
    """
    try:
        out = _run([
            "python", "-c",
            "import json,urllib.request;"
            "d=json.load(urllib.request.urlopen('https://pypi.org/pypi/edge-tts/json',timeout=10));"
            "print(d['info']['version']);"
            "print(next((u['url'] for u in d['urls'] if u['packagetype']=='bdist_wheel'), ''))"
        ])
        lines = out.strip().splitlines()
        wheel = (lines[1].strip() if len(lines) > 1 else "") or None
        ver = lines[0].strip()
        return ver, "PyPI", wheel, ver   # 该仓库 tag 与版本号一致（无 v 前缀）
    except Exception as exc:
        _ulog(f"PyPI 查询失败：{exc}")
    try:
        out = _run([
            "python", "-c",
            "import json,urllib.request;"
            "d=json.load(urllib.request.urlopen("
            "'https://api.github.com/repos/rany2/edge-tts/releases/latest',timeout=10));"
            "print(d['tag_name'])"
        ])
        tag = out.strip()
        return tag.lstrip("v"), "GitHub", None, tag
    except Exception as exc:
        _ulog(f"GitHub 查询失败：{exc}")
    return None, "", None, None


def _detect_pkg(pkg_path: str, name: str) -> str:
    """识别上传的安装包类型：'python'（上游 whl/sdist/源码包）或 'docker'（docker save 镜像包）。"""
    lower = name.lower()
    if lower.endswith(".whl"):
        return "python"
    try:
        if lower.endswith(".zip"):
            import zipfile
            with zipfile.ZipFile(pkg_path) as zf:
                names = zf.namelist()[:80]
        else:
            import tarfile
            with tarfile.open(pkg_path, "r:*") as tf:
                names = tf.getnames()[:80]
    except Exception:
        return "python" if ("edge_tts" in lower or "edge-tts" in lower) else "docker"
    if any(n == "manifest.json" or n.endswith("/manifest.json") for n in names):
        return "docker"
    if any("PKG-INFO" in n or n.endswith("setup.cfg") or "/edge_tts/" in n for n in names):
        return "python"
    return "python" if ("edge_tts" in lower or "edge-tts" in lower) else "docker"


def _redeploy_container(new_image: str) -> None:
    """用指定镜像重建本容器（自动更新与上传更新共用）。"""
    _set_stage("deploy", 85)
    _ulog(f"准备用新镜像 {new_image} 重建容器 {SELF_CONTAINER} ...")
    time.sleep(2)  # 留时间让 HTTP 响应先返回
    _run(["docker", "rm", "-f", SELF_CONTAINER])
    env_args: list[str] = []
    for key in ("PORT", "DEFAULT_VOICE", "MAX_TEXT_LEN", "LOG_LEVEL", "TZ",
                "SRC_DIR", "SELF_CONTAINER", "IMAGE_TAG", "ENABLE_UPDATE",
                "PIP_INDEX_URL", "BASE_REGISTRY"):
        val = os.getenv(key)
        if val:
            env_args += ["-e", f"{key}={val}"]
    port = os.getenv("PORT", "5050")
    _run([
        "docker", "run", "-d", "--name", SELF_CONTAINER,
        "--restart", "unless-stopped",
        "-p", f"{port}:{port}",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{SRC_DIR}:{SRC_DIR}",
        *env_args,
        new_image,
    ])
    _ulog("容器已重建，服务即将恢复")


def _install_whl(whl_path: str) -> str:
    """把官方 whl 原地安装到 /app（优先于系统 site-packages），返回生效版本号。"""
    _run(["pip", "install", "--no-deps", "--force-reinstall",
          "--upgrade", "--target", "/app", whl_path])
    return _run(["python", "-c",
                 "import edge_tts; print(edge_tts.__version__)"]).strip()


def _restart_self() -> None:
    _set_stage("restart", 90)
    _ulog(f"重启容器 {SELF_CONTAINER} ...")
    time.sleep(2)  # 留时间让 HTTP 响应先返回
    _run(["docker", "restart", SELF_CONTAINER])


def _update_worker(mode: str) -> None:
    """mode: auto（联网自动更新） | upload（离线：安装用户上传的 whl/sdist 或镜像包）"""
    try:
        with _update_lock:
            _update_state.update(state="running", error=None, log=[])
        if mode == "auto":
            _set_stage("check", 5)
            latest, source, wheel_url, _tag = _check_latest()
            if not latest:
                raise RuntimeError("无法获取上游最新版本（PyPI 与 GitHub 均不可达）")
            _update_state["latest_version"] = latest
            _ulog(f"最新版本：{latest}（来源 {source}），当前：{EDGE_TTS_VERSION}")
            if latest == EDGE_TTS_VERSION:
                _ulog("已是最新版本，无需更新")
                _set_stage("done", 100)
                _update_state["state"] = "done"
                return

            # ---- 首选轻量路径：服务端直接下载官方 whl，原地安装 + 重启（无需 git/源码目录/docker build）----
            if wheel_url:
                whl_path = os.path.join(UPLOAD_DIR, f"edge_tts-{latest}-auto.whl")
                try:
                    _set_stage("download", 25)
                    _ulog(f"下载官方 whl：{wheel_url}")
                    _run(["curl", "-fsSL", "--max-time", "300", "-o", whl_path, wheel_url])
                    if os.path.getsize(whl_path) < 1024:
                        raise RuntimeError("下载的 whl 过小，疑似无效")
                    _set_stage("install", 55)
                    ver = _install_whl(whl_path)
                    _ulog(f"安装完成，重启后生效版本：{ver}（当前运行 {EDGE_TTS_VERSION}）")
                    try:
                        os.remove(whl_path)
                    except OSError:
                        pass
                    _restart_self()
                    _set_stage("done", 100)
                    _update_state["state"] = "done"
                    return
                except Exception as exc:
                    _ulog(f"轻量更新失败：{exc}")
                    if not SRC_DIR:
                        raise RuntimeError(
                            "轻量更新失败且未挂载源码目录(SRC_DIR)，无法回退到完整重建；"
                            "请使用离线上传更新"
                        )
                    _ulog("回退到完整重建流程（git pull + docker build）…")
            else:
                if not SRC_DIR:
                    raise RuntimeError("未能获取 whl 直链，且未挂载源码目录(SRC_DIR)，无法自动更新")

            # ---- 回退路径：git pull + 整镜像重建 ----
            new_image = f"{IMAGE_TAG}:latest"   # tag 固定 latest，避免每次部署改版本号
            _update_state["new_image"] = new_image

            _set_stage("pull", 20)
            if not Path(SRC_DIR, ".git").exists():
                raise RuntimeError(f"{SRC_DIR} 不是 git 仓库，无法拉取上游代码")
            _run(["git", "-C", SRC_DIR, "pull", "--ff-only"])

            _set_stage("build", 50)
            _run(["docker", "build", *BUILD_ARGS, "-t", new_image, SRC_DIR])
            _redeploy_container(new_image)
        else:
            pkg_path = _update_state.get("upload_path") or ""
            name = os.path.basename(pkg_path)
            pkg_type = _detect_pkg(pkg_path, name)
            _ulog(f"安装包 {name} 识别为：{'上游 Python 包' if pkg_type == 'python' else 'Docker 镜像包'}")

            if pkg_type == "python":
                # ---- 上游 whl/sdist/源码包：原地安装到 /app（优先于系统 site-packages），重启即生效 ----
                _set_stage("install", 50)
                pip_cmd = ["pip", "install", "--no-deps", "--force-reinstall",
                           "--upgrade", "--target", "/app"]
                if not name.lower().endswith(".whl"):
                    # 源码包本地构建 wheel，避免构建隔离环境联网拉取 setuptools
                    pip_cmd.append("--no-build-isolation")
                pip_cmd.append(pkg_path)
                _run(pip_cmd)
                ver = _run(["python", "-c",
                            "import edge_tts; print(edge_tts.__version__)"]).strip()
                _ulog(f"安装完成，重启后生效版本：{ver}（当前运行 {EDGE_TTS_VERSION}）")
                _set_stage("restart", 90)
                _ulog(f"重启容器 {SELF_CONTAINER} ...")
                time.sleep(2)  # 留时间让 HTTP 响应先返回
                _run(["docker", "restart", SELF_CONTAINER])
            else:
                # ---- Docker 镜像包：load 后重建容器 ----
                _set_stage("load", 40)
                out = _run(["docker", "load", "-i", pkg_path])
                m = re.search(r"Loaded image:\s*(\S+)", out)
                image = m.group(1) if m else f"{IMAGE_TAG}:latest"
                if not image.startswith(f"{IMAGE_TAG}:"):
                    raise RuntimeError(
                        f"安装包镜像名为 {image}，不符合要求（需以 {IMAGE_TAG}: 开头），已拒绝部署"
                    )
                _update_state["new_image"] = image
                _ulog(f"已加载镜像 {image}")
                _redeploy_container(image)

            try:
                os.remove(pkg_path)  # 处理完清理上传的临时文件
            except OSError:
                pass

        _set_stage("done", 100)
        _update_state["state"] = "done"
    except Exception as exc:
        _update_state["state"] = "error"
        _update_state["error"] = str(exc)
        _ulog(f"更新失败：{exc}")
        log.exception("更新失败")


@app.get("/api/version", summary="版本信息", description="当前服务版本、edge-tts 版本、各更新方式是否可用")
async def api_version() -> dict[str, Any]:
    return {
        "app": "1.9",
        "edge_tts": EDGE_TTS_VERSION,
        "update_enabled": UPDATE_ENABLED,
        "auto_update_enabled": AUTO_UPDATE_ENABLED,
        "upload_update_enabled": UPLOAD_UPDATE_ENABLED,
        "latest_version": _update_state["latest_version"],
    }


@app.get("/api/update/status", summary="更新任务状态", description="轮询更新进度：阶段、百分比、日志、错误信息")
async def api_update_status() -> dict[str, Any]:
    state = dict(_update_state)
    state["log"] = _update_state["log"][-60:]
    return state


@app.post("/api/update/check", summary="检查上游新版本", description="查询 PyPI/GitHub 获取最新版本并对比；同时返回 whl 安装包直链与 GitHub Releases 页面链接，供用户在本机下载后离线上传")
async def api_update_check() -> dict[str, Any]:
    latest, source, wheel_url, tag = _check_latest()
    _update_state["latest_version"] = latest
    _update_state["wheel_url"] = wheel_url
    return {
        "current": EDGE_TTS_VERSION,
        "latest": latest,
        "source": source,
        "has_update": bool(latest and latest != EDGE_TTS_VERSION),
        "wheel_url": wheel_url,
        "github_url": GITHUB_RELEASES_URL,
        "source_zip_url": f"https://github.com/rany2/edge-tts/archive/refs/tags/{tag}.zip" if tag else None,
    }


@app.post("/api/update/run", summary="一键自动更新（联网）", description="拉取上游代码→构建新镜像→重建容器。需挂载 SRC_DIR 与 docker.sock，且服务器可访问外网")
async def api_update_run() -> dict[str, Any]:
    if not AUTO_UPDATE_ENABLED:
        if not _sock_ready:
            raise HTTPException(status_code=400, detail="自动更新未启用：当前环境无可用 Docker 套接字")
        raise HTTPException(
            status_code=400,
            detail="自动更新未启用：需挂载源码目录(SRC_DIR)与 docker.sock；无外网环境请改用离线上传更新",
        )
    if _update_state["state"] == "running":
        raise HTTPException(status_code=409, detail="任务已在进行中")
    threading.Thread(target=_update_worker, args=("auto",), daemon=True).start()
    return {"ok": True}


@app.post("/api/update/upload", summary="上传安装包更新（离线）", description="上传 tar/tar.gz 镜像包，服务端 docker load 后重建容器。全程无需外网与 git，仅需 docker.sock")
async def api_update_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    if not UPLOAD_UPDATE_ENABLED:
        raise HTTPException(
            status_code=400,
            detail="离线上传更新未启用：需挂载 docker.sock（当前环境无可用 Docker 套接字）",
        )
    if _update_state["state"] == "running":
        raise HTTPException(status_code=409, detail="任务已在进行中")
    name = os.path.basename(file.filename or "update.tar.gz")
    if not re.search(r"\.(whl|zip|tar|tar\.gz|tgz)$", name, re.I):
        raise HTTPException(
            status_code=400,
            detail="仅支持 .whl / .zip / .tar.gz（上游 Release 源码包，推荐）或 .tar / .tar.gz / .tgz（Docker 镜像包）",
        )
    pkg_path = os.path.join(UPLOAD_DIR, "upload-" + name)
    try:
        with open(pkg_path, "wb") as f:
            while chunk := await file.read(4 * 1024 * 1024):
                f.write(chunk)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"保存上传文件失败: {exc}") from exc
    finally:
        await file.close()
    size_mb = os.path.getsize(pkg_path) // 1024 // 1024
    if size_mb < 50:
        os.remove(pkg_path)
        raise HTTPException(status_code=400, detail=f"文件过小（{size_mb} MB），不像有效的镜像安装包")
    _update_state["upload_path"] = pkg_path
    threading.Thread(target=_update_worker, args=("upload",), daemon=True).start()
    return {"ok": True, "package": name, "size_mb": size_mb}


@app.get("/voices", summary="音色列表", description="列出全部可用音色，可用 locale 参数过滤，如 locale=zh-CN")
async def voices(locale: str | None = None) -> list[dict[str, Any]]:
    try:
        all_voices = await edge_tts.list_voices()
    except Exception as exc:  # 上游不可达时给出明确错误
        raise HTTPException(status_code=502, detail=f"获取音色列表失败: {exc}") from exc
    result = [
        {
            "Name": v["ShortName"],
            "Gender": v["Gender"],
            "Locale": v["Locale"],
            "FriendlyName": v["FriendlyName"],
        }
        for v in all_voices
        if locale is None or v["Locale"].lower().startswith(locale.lower())
    ]
    return result


@app.get("/tts", summary="语音合成（GET）", description="返回 audio/mpeg。参数：text、voice、rate(+0%)、volume(+0%)、pitch(+0Hz)")
async def tts_get(
    text: str,
    voice: str = DEFAULT_VOICE,
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
) -> Response:
    return await tts_post(TTSRequest(text=text, voice=voice, rate=rate, volume=volume, pitch=pitch))


@app.post("/tts", summary="语音合成（POST，推荐）", description="JSON body 提交文本与参数，返回 audio/mpeg 音频流")
async def tts_post(req: TTSRequest) -> Response:
    if len(req.text) > MAX_TEXT_LEN:
        raise HTTPException(
            status_code=400, detail=f"文本过长（{len(req.text)} > {MAX_TEXT_LEN}）"
        )
    try:
        audio = await synthesize(req.text, req.voice, req.rate, req.volume, req.pitch)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("合成失败")
        raise HTTPException(status_code=502, detail=f"合成失败: {exc}") from exc
    return Response(content=audio, media_type="audio/mpeg")


# ==================== OpenAI 兼容接口（/v1/*） ====================
# 兼容 OpenAI TTS API：POST /v1/audio/speech + GET /v1/models
# 可被 OpenAI SDK、Open WebUI、Dify、LobeChat 等直接作为 TTS 后端使用。

# OpenAI 音色名 → 本服务音色映射（可在 voice 里直接传 edge 音色名覆盖）
OPENAI_VOICE_MAP = {
    "alloy": "zh-CN-XiaoxiaoNeural",    # 女，温柔
    "echo": "zh-CN-YunxiNeural",        # 男，活泼
    "fable": "zh-CN-XiaoyiNeural",      # 女，活泼
    "onyx": "zh-CN-YunjianNeural",      # 男，浑厚
    "nova": "zh-CN-XiaoxiaoNeural",     # 女
    "shimmer": "zh-CN-XiaoyiNeural",    # 女
}
_EDGE_VOICE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}-\w+Neural$")


def _map_openai_voice(voice: str) -> str:
    v = (voice or "").strip()
    if v in OPENAI_VOICE_MAP:
        return OPENAI_VOICE_MAP[v]
    if _EDGE_VOICE_RE.fullmatch(v):
        return v          # 允许直接传 edge-tts 原生音色名，如 zh-CN-YunxiNeural
    raise HTTPException(
        status_code=400,
        detail=(
            f"未知音色 '{v}'。可传 OpenAI 音色名（{'、'.join(OPENAI_VOICE_MAP)}）"
            f"或 edge-tts 原生音色名（如 zh-CN-XiaoxiaoNeural，完整列表见 /voices）"
        ),
    )


class OpenAISpeechRequest(BaseModel):
    model: str = Field("tts-1", description="模型名，兼容性参数，任意值均可（实际引擎为 edge-tts）")
    input: str = Field(..., min_length=1, description="待合成的文本")
    voice: str = Field("alloy", description="OpenAI 音色名（alloy/echo/fable/onyx/nova/shimmer）或 edge-tts 原生音色名")
    response_format: str = Field("mp3", description="音频格式，当前仅支持 mp3（edge-tts 原生输出格式）")
    speed: float = Field(1.0, description="语速倍率，0.5~2.0，1.0 为正常语速")


async def _openai_auth(authorization: str | None = Header(None)) -> None:
    """可选鉴权：设置环境变量 OPENAI_API_KEY 后，/v1/* 要求 Bearer Token。"""
    if OPENAI_API_KEY and authorization != f"Bearer {OPENAI_API_KEY}":
        raise HTTPException(status_code=401, detail="无效的 API Key（Authorization: Bearer <OPENAI_API_KEY>）")


@app.get("/v1/models", summary="OpenAI 兼容：模型列表", description="供 OpenAI 兼容客户端探测模型，返回固定 tts-1 条目")
async def openai_models(_: None = Depends(_openai_auth)) -> dict[str, Any]:
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": "tts-1", "object": "model", "created": now, "owned_by": "edge-tts-web-ui"},
            {"id": "tts-1-hd", "object": "model", "created": now, "owned_by": "edge-tts-web-ui"},
        ],
    }


@app.post(
    "/v1/audio/speech",
    summary="OpenAI 兼容：语音合成",
    description=(
        "与 OpenAI POST /v1/audio/speech 请求/响应格式一致：body 含 model/input/voice/"
        "response_format/speed，返回音频二进制流。voice 可用 OpenAI 音色名或 edge-tts 原生音色名；"
        "speed 为语速倍率（0.5~2.0）；当前仅支持 mp3 输出"
    ),
)
async def openai_speech(req: OpenAISpeechRequest, _: None = Depends(_openai_auth)) -> Response:
    if len(req.input) > MAX_TEXT_LEN:
        raise HTTPException(status_code=400, detail=f"文本过长（{len(req.input)} > {MAX_TEXT_LEN}）")
    fmt = (req.response_format or "mp3").lower()
    if fmt not in ("mp3", "mpeg"):
        raise HTTPException(status_code=400, detail=f"暂不支持格式 '{req.response_format}'，当前仅支持 mp3")
    if not 0.25 <= req.speed <= 4.0:
        raise HTTPException(status_code=400, detail="speed 需在 0.25 ~ 4.0 之间")
    voice = _map_openai_voice(req.voice)
    # OpenAI speed 倍率 → edge-tts 语速百分比：1.0 → +0%，1.5 → +50%，0.5 → -50%
    rate = int(round((req.speed - 1) * 100))
    rate_str = f"+{rate}%" if rate >= 0 else f"{rate}%"
    try:
        audio = await synthesize(req.input, voice, rate_str, "+0%", "+0Hz")
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("OpenAI 兼容合成失败")
        raise HTTPException(status_code=502, detail=f"合成失败: {exc}") from exc
    return Response(content=audio, media_type="audio/mpeg", headers={"Content-Disposition": 'inline; filename="speech.mp3"'})


@app.exception_handler(Exception)
async def unhandled(request, exc):  # 兜底，避免把栈抛给调用方
    log.exception("未处理异常")
    return JSONResponse(status_code=500, content={"detail": str(exc)})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "5050")))
