"""
Douyin Video Analysis MCP Server

工具一览：
  - analyze_douyin(url)        : 一键转录（同步，~25s）。适用于 Claude Code（无超时压力）。
  - douyin_to_text(url)        : 异步任务，立即返回 job_id。适用于 Claude Desktop（硬超时）。
                                 后续用 get_transcript_result(job_id) 取结果。
  - get_transcript_result(...) : 轮询/等待异步任务结果。
  - download_douyin(url)       : 只下载（最高画质），返回本地文件路径。
  - transcribe_video(file)     : 转录本地视频/音频文件。

Architecture:
  - Playwright (headless Chromium) intercepts aweme/detail API to get signed CDN URLs
  - urllib downloads the video (no ffmpeg/ffprobe network calls — they hang in MCP context)
  - faster-whisper transcribes (it internally uses ffmpeg on local files, which is fine)
"""

import asyncio
import os
import re
import ssl
import tempfile
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from playwright.async_api import async_playwright
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Douyin Analysis")

_URL_RE = re.compile(r'https?://\S+')
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# 阻塞操作（urllib 下载、Whisper 转录）在此线程池运行
_EXECUTOR = ThreadPoolExecutor(max_workers=1)

# Whisper 模型：tiny=39MB/快，small=244MB/更准。
# 默认 tiny：抖音口播一般清晰，端到端 ~25s 可在 MCP 超时内完成。
# 如某段音频效果差，可在工具调用时传入 model_size="small"。
WHISPER_MODEL = "tiny"
_ALLOWED_MODELS = {"tiny", "base", "small", "medium", "large-v3"}


# ── URL 提取 ──────────────────────────────────────────────

def _extract_url(text: str) -> str:
    """从纯URL或抖音App分享文本中提取第一个URL。"""
    text = text.strip()
    m = _URL_RE.search(text)
    if not m:
        raise ValueError(f"输入中未找到有效URL: {text!r}")
    url = m.group(0)
    url = re.sub(r'[^\w./:?=&%-]+$', '', url)
    return url


# ── Playwright：拦截 aweme/detail API ─────────────────────

async def _get_video_object(page_url: str) -> dict:
    """
    用 async Playwright 打开抖音页面，拦截 aweme/detail API，
    返回 aweme_detail.video 字典（包含 bit_rate 数组和 play_addr）。
    """
    detail: list = [None]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=_UA)
        page = await ctx.new_page()

        async def on_response(resp):
            if detail[0]:
                return
            if "aweme/v1/web/aweme/detail" in resp.url:
                try:
                    j = await resp.json()
                    if j.get("aweme_detail"):
                        detail[0] = j
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            # "commit" = 拿到第一个 HTTP 响应就返回，不等 DOM 解析完
            await page.goto(page_url, wait_until="commit", timeout=35000)
        except Exception:
            pass

        # 轮询等待 detail API 响应，最多 12 秒
        for _ in range(24):
            if detail[0]:
                break
            await asyncio.sleep(0.5)

        await browser.close()

    if not detail[0]:
        raise RuntimeError("Playwright 未能拦截到视频信息，请稍后重试")

    return detail[0]["aweme_detail"].get("video", {})


# ── URL 选取（纯内存操作，无网络调用）────────────────────

def _cdn_urls(item: dict) -> list[str]:
    """从 bit_rate 条目中提取外部 CDN URL（排除需要 cookie 的 douyin.com 内部地址）。"""
    return [u for u in item.get("play_addr", {}).get("url_list", [])
            if "douyin.com" not in u]


def _sorted_candidates(video: dict) -> list[tuple[int, str]]:
    """返回按码率升序排列的 (bit_rate, cdn_url) 列表。"""
    result = []
    for item in video.get("bit_rate", []):
        urls = _cdn_urls(item)
        if urls:
            result.append((item.get("bit_rate", 0), urls[0]))
    result.sort()
    return result


def _pick_url_for_transcription(video: dict) -> str:
    """
    选取用于转录的最小视频 URL。
    策略：跳过绝对最低码率条目（通常是视频无音轨的 DASH 分片），
    取第二低码率（约 730k，20MB，含音轨）。
    若只有一个条目则直接使用。回退到 play_addr。
    """
    cands = _sorted_candidates(video)
    if len(cands) >= 2:
        return cands[1][1]   # 第二低 = 最小含音轨版本
    if cands:
        return cands[0][1]
    # 回退
    fallback = [u for u in video.get("play_addr", {}).get("url_list", [])
                if "douyin.com" not in u]
    if fallback:
        return fallback[0]
    raise RuntimeError("无法找到可用的视频链接")


def _pick_url_for_download(video: dict) -> str:
    """选取最高码率的外部 CDN URL（用于全质量下载）。"""
    cands = _sorted_candidates(video)
    if cands:
        return cands[-1][1]  # 最高码率
    fallback = [u for u in video.get("play_addr", {}).get("url_list", [])
                if "douyin.com" not in u]
    if fallback:
        return fallback[0]
    raise RuntimeError("无法找到可用的视频链接")


# ── 下载（urllib，无 ffmpeg 网络调用）───────────────────

def _download_sync(video_url: str, out_path: str) -> None:
    """用 urllib 同步下载视频文件。在线程池中调用。"""
    req = urllib.request.Request(
        video_url,
        headers={"User-Agent": _UA, "Referer": "https://www.douyin.com/"},
    )
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=90) as r:
        with open(out_path, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)


# ── 转录（faster-whisper，beam_size=1 加速）──────────────

def _transcribe_sync(file_path: str, model_size: str = WHISPER_MODEL) -> str:
    """
    用 faster-whisper 转录视频/音频文件。
    beam_size=1（贪心解码）比 beam_size=5 快约 2-3 倍，中文效果仍够用。
    """
    from faster_whisper import WhisperModel

    if model_size not in _ALLOWED_MODELS:
        model_size = WHISPER_MODEL
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(file_path, language="zh", beam_size=1)
    return "\n".join(seg.text.strip() for seg in segments)


# ── MCP 工具 ─────────────────────────────────────────────

@mcp.tool()
async def analyze_douyin(url: str, model_size: str = WHISPER_MODEL) -> str:
    """
    抖音视频一键转录：下载最小含音轨版本 → Whisper 转录 → 返回文字稿。
    自动选取约 20MB 的低码率流（vs 原版 170MB），大幅缩短等待时间。

    url: 抖音分享链接或App分享文本（自动提取URL）。
         支持格式：
           - 纯URL:  https://v.douyin.com/43Hxli09K70/
           - 长URL:  https://www.douyin.com/video/7628423061288682112
           - 分享文本: "5.33 复制打开抖音... https://v.douyin.com/xxx/"，自动提取URL
    model_size: Whisper 模型大小，默认 "tiny"（快）。
                若文字识别明显有误，可改用 "small"（更准但慢约 4x）。
                可选: tiny / base / small / medium / large-v3
    """
    try:
        real_url = _extract_url(url)
    except ValueError as e:
        return str(e)

    loop = asyncio.get_event_loop()

    # Step 1: Playwright 拦截视频信息（~10s）
    try:
        video = await _get_video_object(real_url)
    except RuntimeError as e:
        return f"获取视频信息失败: {e}"

    # Step 2: 选 URL（纯内存，瞬间完成）
    try:
        dl_url = _pick_url_for_transcription(video)
    except RuntimeError as e:
        return f"下载失败: {e}"

    # Step 3: urllib 下载 + Whisper 转录
    with tempfile.TemporaryDirectory(prefix="douyin_") as tmp:
        out_path = os.path.join(tmp, "video.mp4")
        try:
            await loop.run_in_executor(_EXECUTOR, _download_sync, dl_url, out_path)
        except Exception as e:
            return f"下载失败: {e}"

        try:
            transcript = await loop.run_in_executor(
                _EXECUTOR, _transcribe_sync, out_path, model_size
            )
        except Exception as e:
            return f"转录失败: {e}"

    if not transcript.strip():
        return "转录完成，但未检测到语音内容（视频可能没有人声）。"

    return transcript


@mcp.tool()
async def download_douyin(url: str) -> str:
    """
    只下载抖音视频（无水印，最高画质），返回本地文件路径。
    文件保存在系统临时目录，不会自动清理，请手动删除。

    url: 抖音分享链接或App分享文本（自动提取URL）。
    """
    try:
        real_url = _extract_url(url)
    except ValueError as e:
        return str(e)

    loop = asyncio.get_event_loop()

    try:
        video = await _get_video_object(real_url)
    except RuntimeError as e:
        return f"获取视频信息失败: {e}"

    try:
        dl_url = _pick_url_for_download(video)
    except RuntimeError as e:
        return f"下载失败: {e}"

    out_dir = tempfile.mkdtemp(prefix="douyin_dl_")
    out_path = os.path.join(out_dir, "video.mp4")
    try:
        await loop.run_in_executor(_EXECUTOR, _download_sync, dl_url, out_path)
    except Exception as e:
        return f"下载失败: {e}"

    return out_path


# ── 异步任务模式（用于 Claude Desktop 等硬超时客户端）─────

# job_id -> {"status": "running"|"done"|"error", "stage": str, "result": str|None, "started": float}
_JOBS: dict[str, dict] = {}
# 已完成任务保留时长（秒），之后 GC
_JOB_TTL = 600


def _gc_jobs():
    """清理过期任务。"""
    now = time.time()
    expired = [jid for jid, j in _JOBS.items()
               if j["status"] != "running" and now - j.get("done_at", j["started"]) > _JOB_TTL]
    for jid in expired:
        _JOBS.pop(jid, None)


async def _full_pipeline_bg(job_id: str, url: str, model_size: str) -> None:
    """后台跑完整流程：URL 提取 → Playwright → 下载 → 转录。"""
    job = _JOBS[job_id]
    loop = asyncio.get_event_loop()
    tmp_dir = None
    try:
        job["stage"] = "extracting_url"
        real_url = _extract_url(url)

        job["stage"] = "fetching_metadata"
        video = await _get_video_object(real_url)

        job["stage"] = "picking_url"
        dl_url = _pick_url_for_transcription(video)

        tmp_dir = tempfile.mkdtemp(prefix="douyin_job_")
        out_path = os.path.join(tmp_dir, "video.mp4")

        job["stage"] = "downloading"
        await loop.run_in_executor(_EXECUTOR, _download_sync, dl_url, out_path)

        job["stage"] = "transcribing"
        text = await loop.run_in_executor(
            _EXECUTOR, _transcribe_sync, out_path, model_size
        )

        job["status"] = "done"
        job["stage"] = "done"
        job["result"] = text or "（未检测到语音内容）"
    except Exception as e:
        job["status"] = "error"
        job["result"] = f"{type(e).__name__}: {e}"
    finally:
        job["done_at"] = time.time()
        # 清理临时文件
        if tmp_dir:
            try:
                for f in os.listdir(tmp_dir):
                    try:
                        os.remove(os.path.join(tmp_dir, f))
                    except OSError:
                        pass
                os.rmdir(tmp_dir)
            except OSError:
                pass


@mcp.tool()
async def douyin_to_text(url: str, model_size: str = WHISPER_MODEL) -> str:
    """
    【推荐 Claude Desktop 使用】抖音视频转文字（异步）。
    立即返回 job_id（<1秒），后台执行下载+转录。
    随后请调用 get_transcript_result(job_id) 取结果（该工具会等待最多 25 秒，未完成请再次调用）。

    适用场景：Claude Desktop chat 等客户端 MCP 工具调用有硬超时（约 30-60 秒），
    无法承受完整流程（25-90 秒）的同步调用。

    url: 抖音分享链接或App分享文本（自动提取URL）。
         支持: https://v.douyin.com/xxx/ 或 https://www.douyin.com/video/xxx
               或 "5.33 复制打开抖音... https://v.douyin.com/xxx/" 整段分享文本
    model_size: Whisper 模型，默认 "tiny"（快）。准度不够时改 "small"。
    """
    _gc_jobs()
    job_id = uuid.uuid4().hex[:8]
    _JOBS[job_id] = {
        "status": "running",
        "stage": "queued",
        "result": None,
        "started": time.time(),
    }
    asyncio.create_task(_full_pipeline_bg(job_id, url, model_size))
    return (
        f"任务已启动 (job_id={job_id})。\n"
        f"请调用 get_transcript_result(\"{job_id}\") 获取文字稿。"
        f"该工具会等待最多 25 秒，若未完成请再次调用同一个 job_id。"
    )


@mcp.tool()
async def get_transcript_result(job_id: str, wait_seconds: float = 25.0) -> str:
    """
    获取异步转录任务的结果。会等待最多 wait_seconds 秒（默认 25，建议保持）。
    若任务在等待窗口内完成，直接返回文字稿；否则返回当前状态，调用方应再次调用。

    job_id: douyin_to_text 返回的任务ID。
    wait_seconds: 最长等待秒数（默认 25，必须小于客户端超时）。
    """
    if job_id not in _JOBS:
        return f"未知或已过期的 job_id: {job_id}"

    deadline = time.time() + max(0.5, min(wait_seconds, 28.0))
    while time.time() < deadline:
        job = _JOBS.get(job_id)
        if not job:
            return f"未知或已过期的 job_id: {job_id}"
        if job["status"] != "running":
            break
        await asyncio.sleep(0.5)

    job = _JOBS.get(job_id)
    if not job:
        return f"任务结果已过期: {job_id}"

    elapsed = time.time() - job["started"]
    if job["status"] == "running":
        return (
            f"[进行中] 已运行 {elapsed:.0f}s，当前阶段: {job['stage']}。\n"
            f"请再次调用 get_transcript_result(\"{job_id}\")。"
        )
    if job["status"] == "error":
        result = job["result"]
        # 错误也保留一段时间供 debug，由 GC 清理
        return f"转录失败: {result}"

    # done — 返回结果后立即清理
    result = job["result"]
    _JOBS.pop(job_id, None)
    return result


@mcp.tool()
async def transcribe_video(file_path: str, model_size: str = WHISPER_MODEL) -> str:
    """
    转录本地视频或音频文件，返回文字稿。
    使用 faster-whisper（默认 tiny 模型，中文）。

    file_path: 本地视频/音频文件的绝对路径。
    model_size: Whisper 模型大小，默认 "tiny"。准度不够时可改为 "small"。
    """
    path = Path(file_path)
    if not path.exists():
        return f"文件不存在: {file_path}"
    if not path.is_file():
        return f"路径不是文件: {file_path}"

    loop = asyncio.get_event_loop()
    try:
        transcript = await loop.run_in_executor(
            _EXECUTOR, _transcribe_sync, str(path), model_size
        )
    except Exception as e:
        return f"转录失败: {e}"

    if not transcript.strip():
        return "转录完成，但未检测到语音内容。"

    return transcript


if __name__ == "__main__":
    mcp.run(transport="stdio")
