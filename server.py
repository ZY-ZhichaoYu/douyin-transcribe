"""
Douyin / Bilibili Video Analysis MCP Server

工具一览：
  - analyze_video(url)         : 一键转录（同步）。支持抖音和 Bilibili。
  - video_to_text(url)         : 异步任务，立即返回 job_id。适用于 Claude Desktop（硬超时）。
                                 后续用 get_transcript_result(job_id) 取结果。
  - get_transcript_result(...) : 轮询/等待异步任务结果。
  - download_video(url)        : 只下载视频，返回本地文件路径。
  - transcribe_video(file)     : 转录本地视频/音频文件。
  - analyze_douyin / douyin_to_text / download_douyin: 旧工具名，保留兼容。

Architecture:
  - Playwright (headless Chromium) intercepts aweme/detail API to get signed CDN URLs
  - yt-dlp extracts and downloads Bilibili media
  - urllib downloads direct Douyin/Bilibili media URLs where possible
  - faster-whisper transcribes (it internally uses ffmpeg on local files, which is fine)
"""

import asyncio
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Video Analysis", log_level="ERROR")

_URL_RE = re.compile(r'https?://\S+')
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# 下载和转录分开排队：长视频转录很慢，不能阻塞普通下载任务。
_DOWNLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=3)
_TRANSCRIBE_EXECUTOR = ThreadPoolExecutor(max_workers=1)

# Whisper 模型：tiny=39MB/快，small=244MB/更准。
# 默认 tiny：抖音口播一般清晰，端到端 ~25s 可在 MCP 超时内完成。
# 如某段音频效果差，可在工具调用时传入 model_size="small"。
WHISPER_MODEL = "tiny"
_ALLOWED_MODELS = {"tiny", "base", "small", "medium", "large-v3"}
_DETAIL_RESPONSE_TIMEOUT = 30.0
_DETAIL_RESPONSE_RETRIES = 2
_BILIBILI_VIDEO_FORMAT = (
    "bv*[vcodec^=avc1][ext=mp4]+ba[ext=m4a]/"
    "bv*[vcodec^=avc1]+ba/"
    "bv*[ext=mp4]+ba[ext=m4a]/"
    "bv*+ba/"
    "b[vcodec^=avc1]/b"
)


# ── URL 提取 ──────────────────────────────────────────────

def _extract_url(text: str) -> str:
    """从纯URL或 App 分享文本中提取第一个URL。"""
    text = text.strip()
    m = _URL_RE.search(text)
    if not m:
        raise ValueError(f"输入中未找到有效URL: {text!r}")
    url = m.group(0)
    url = re.sub(r'[^\w./:?=&%-]+$', '', url)
    return url


def _detect_platform(url: str) -> str:
    """Return the supported site name for a URL."""
    host = urlparse(url).netloc.lower()
    if "douyin.com" in host or "iesdouyin.com" in host:
        return "douyin"
    if "bilibili.com" in host or host.endswith("b23.tv"):
        return "bilibili"
    raise ValueError(f"暂不支持这个网站: {host or url}")


def _platform_label(platform: str) -> str:
    return {"douyin": "抖音", "bilibili": "Bilibili"}.get(platform, platform)


# ── Playwright：拦截 aweme/detail API ─────────────────────

async def _get_video_object(page_url: str) -> dict:
    """
    用 async Playwright 打开抖音页面，拦截 aweme/detail API，
    返回 aweme_detail.video 字典（包含 bit_rate 数组和 play_addr）。
    """
    last_error: Exception | None = None

    for attempt in range(_DETAIL_RESPONSE_RETRIES):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(
                    user_agent=_UA,
                    locale="zh-CN",
                    viewport={"width": 1280, "height": 720},
                    extra_http_headers={
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    },
                )
                page = await ctx.new_page()
                loop = asyncio.get_running_loop()
                detail_future = loop.create_future()
                parser_tasks = set()

                async def parse_detail_response(resp):
                    nonlocal last_error
                    try:
                        payload = await resp.json()
                    except Exception as e:
                        last_error = e
                        return
                    if payload.get("aweme_detail") and not detail_future.done():
                        detail_future.set_result(payload)

                def on_response(resp):
                    if detail_future.done():
                        return
                    if "aweme/v1/web/aweme/detail" not in resp.url:
                        return
                    task = asyncio.create_task(parse_detail_response(resp))
                    parser_tasks.add(task)
                    task.add_done_callback(parser_tasks.discard)

                page.on("response", on_response)
                try:
                    await page.goto(page_url, wait_until="commit", timeout=45000)
                except Exception as e:
                    last_error = e

                try:
                    payload = await asyncio.wait_for(
                        detail_future, timeout=_DETAIL_RESPONSE_TIMEOUT
                    )
                    return payload["aweme_detail"].get("video", {})
                except asyncio.TimeoutError as e:
                    last_error = e
                finally:
                    for task in parser_tasks:
                        task.cancel()
            finally:
                await browser.close()

        if attempt < _DETAIL_RESPONSE_RETRIES - 1:
            await asyncio.sleep(1)

    suffix = f" ({type(last_error).__name__})" if last_error else ""
    raise RuntimeError(f"Playwright 未能拦截到视频信息，请稍后重试{suffix}")


# ── 抖音分享页兜底：移动端 HTML 中的 window._ROUTER_DATA ─────

def _read_url_text_sync(url: str, headers: dict | None = None, timeout: int = 30) -> tuple[str, str]:
    request_headers = {
        "User-Agent": _MOBILE_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.douyin.com/",
    }
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace"), resp.geturl()


def _extract_json_object_after_marker(text: str, marker: str) -> dict:
    marker_pos = text.find(marker)
    if marker_pos < 0:
        raise ValueError(f"未找到 {marker}")
    start = text.find("{", marker_pos)
    if start < 0:
        raise ValueError(f"{marker} 后面没有 JSON 对象")

    depth = 0
    in_string = False
    escaped = False
    for idx, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:idx + 1])

    raise ValueError(f"{marker} JSON 对象不完整")


def _looks_like_douyin_video(value) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("play_addr"), dict)
        and any(key in value for key in ("bit_rate", "duration", "cover"))
    )


def _find_douyin_video_dict(value) -> dict | None:
    if isinstance(value, dict):
        if _looks_like_douyin_video(value):
            return value

        aweme = value.get("aweme_detail")
        if isinstance(aweme, dict) and _looks_like_douyin_video(aweme.get("video")):
            return aweme["video"]

        video = value.get("video")
        if _looks_like_douyin_video(video):
            return video

        for child in value.values():
            found = _find_douyin_video_dict(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_douyin_video_dict(child)
            if found:
                return found
    return None


def _douyin_share_page_candidates(original_url: str, final_url: str) -> list[str]:
    result = []

    def add(url: str) -> None:
        if url and url not in result:
            result.append(url)

    add(final_url)
    add(original_url)

    for url in (final_url, original_url):
        match = re.search(r"/(?:share/)?video/(\d+)", url)
        if not match:
            continue
        video_id = match.group(1)
        add(f"https://www.iesdouyin.com/share/video/{video_id}/")
        add(f"https://www.douyin.com/video/{video_id}")

    return result


def _get_video_object_from_share_page_sync(page_url: str) -> dict:
    errors: list[str] = []
    first_html = ""
    final_url = page_url
    try:
        first_html, final_url = _read_url_text_sync(page_url)
    except Exception as e:
        errors.append(f"{page_url}: {type(e).__name__}")

    fetched = {final_url: first_html} if first_html else {}
    for candidate in _douyin_share_page_candidates(page_url, final_url):
        try:
            html = fetched.get(candidate)
            if html is None:
                html, _ = _read_url_text_sync(candidate)
            data = _extract_json_object_after_marker(html, "window._ROUTER_DATA")
            video = _find_douyin_video_dict(data)
            if video:
                return video
            errors.append(f"{candidate}: 未找到 video 字段")
        except Exception as e:
            errors.append(f"{candidate}: {type(e).__name__}")

    detail = "; ".join(errors[-3:]) if errors else "没有可解析的分享页"
    raise RuntimeError(f"抖音分享页解析失败: {detail}")


async def _get_douyin_video_object(page_url: str) -> dict:
    loop = asyncio.get_running_loop()
    try:
        return await _get_video_object(page_url)
    except Exception as e:
        playwright_error = e

    try:
        return await loop.run_in_executor(
            _DOWNLOAD_EXECUTOR, _get_video_object_from_share_page_sync, page_url
        )
    except Exception as e:
        raise RuntimeError(
            f"抖音视频信息获取失败: Playwright 拦截 {playwright_error}; 分享页解析 {e}"
        ) from e


# ── URL 选取（纯内存操作，无网络调用）────────────────────

def _external_cdn_urls(urls) -> list[str]:
    """Return public CDN URLs and drop internal douyin.com play endpoints."""
    raw = []
    if isinstance(urls, dict):
        for key in ("main_url", "backup_url", "url_list", "fallback_url"):
            value = urls.get(key)
            if isinstance(value, list):
                raw.extend(value)
            elif isinstance(value, str):
                raw.append(value)
    elif isinstance(urls, list):
        raw.extend(urls)
    elif isinstance(urls, str):
        raw.append(urls)

    result = []
    for url in raw:
        if not isinstance(url, str) or not url:
            continue
        if "douyin.com" in url:
            continue
        candidates = [url]
        if "/playwm/" in url:
            candidates.insert(0, url.replace("/playwm/", "/play/"))
        for candidate in candidates:
            if candidate not in result:
                result.append(candidate)
    return result


def _cdn_urls(item: dict) -> list[str]:
    """从 bit_rate 条目中提取外部 CDN URL（排除需要 cookie 的 douyin.com 内部地址）。"""
    return _external_cdn_urls(item.get("play_addr", {}).get("url_list", []))


def _sorted_audio_candidates(video: dict) -> list[tuple[int, int, str]]:
    """Return audio-only CDN candidates sorted by bitrate/size."""
    result = []
    for item in video.get("bit_rate_audio") or []:
        meta = item.get("audio_meta") or {}
        urls = _external_cdn_urls(meta.get("url_list") or {})
        if urls:
            result.append((
                meta.get("bitrate") or item.get("audio_quality") or 0,
                meta.get("size") or 0,
                urls[0],
            ))

    audio = video.get("audio") or {}
    for key in ("play_url", "play_addr"):
        value = audio.get(key) or {}
        urls = _external_cdn_urls(value.get("url_list", []))
        if urls:
            result.append((0, 0, urls[0]))

    result.sort(key=lambda item: (item[0], item[1]))
    return result


def _sorted_candidates(video: dict) -> list[tuple[int, str]]:
    """返回按码率升序排列的 (bit_rate, cdn_url) 列表。"""
    result = []
    for item in video.get("bit_rate") or []:
        urls = _cdn_urls(item)
        if urls:
            result.append((item.get("bit_rate", 0), urls[0]))
    result.sort()
    return result


def _sorted_progressive_candidates(video: dict) -> list[tuple[int, str]]:
    """返回普通 MP4 候选；DASH 候选通常是纯视频分片，不适合直接给 Whisper。"""
    result = []
    for item in video.get("bit_rate") or []:
        if item.get("format") != "mp4":
            continue
        urls = _cdn_urls(item)
        if urls:
            result.append((item.get("bit_rate", 0), urls[0]))
    result.sort()
    return result


def _pick_url_for_transcription(video: dict) -> str:
    """
    选取用于转录的最小音频/视频 URL。
    策略：优先使用 bit_rate_audio 中的音频流；没有音频流时再选择普通 MP4。
    DASH 视频候选常是纯视频分片，直接交给 Whisper/PyAV 会因为没有音频流而解码失败。
    """
    audio_cands = _sorted_audio_candidates(video)
    if audio_cands:
        return audio_cands[0][2]
    cands = _sorted_progressive_candidates(video)
    if cands:
        return cands[0][1]
    cands = _sorted_candidates(video)
    if cands:
        return cands[0][1]
    # 回退
    fallback = _external_cdn_urls((video.get("play_addr") or {}).get("url_list", []))
    if fallback:
        return fallback[0]
    raise RuntimeError("无法找到可用的视频链接")


def _pick_url_for_download(video: dict) -> str:
    """选取最高码率的外部 CDN URL（用于全质量下载）。"""
    cands = _sorted_progressive_candidates(video)
    if cands:
        return cands[-1][1]  # 最高码率的自包含 MP4
    cands = _sorted_candidates(video)
    if cands:
        return cands[-1][1]  # 最高码率
    fallback = _external_cdn_urls((video.get("play_addr") or {}).get("url_list", []))
    if fallback:
        return fallback[0]
    raise RuntimeError("无法找到可用的视频链接")


# ── Bilibili：yt-dlp 提取和下载 ──────────────────────────

def _load_ytdlp():
    try:
        from yt_dlp import YoutubeDL
    except ImportError as e:
        raise RuntimeError("Bilibili 支持需要安装 yt-dlp：pip install -r requirements.txt") from e
    return YoutubeDL


def _extract_bilibili_info(url: str) -> dict:
    """Extract Bilibili metadata and direct media URLs with yt-dlp."""
    YoutubeDL = _load_ytdlp()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,
        "http_headers": {"User-Agent": _UA},
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = info.get("entries")
    if entries:
        first = next((entry for entry in entries if entry), None)
        if first:
            info = first
    return info


def _bilibili_headers(info: dict, fmt: dict | None = None) -> dict[str, str]:
    headers = {"User-Agent": _UA, "Referer": "https://www.bilibili.com/"}
    for source in (info.get("http_headers") or {}, (fmt or {}).get("http_headers") or {}):
        for key, value in source.items():
            if value:
                headers[key] = value
    return headers


def _is_direct_http_format(fmt: dict) -> bool:
    protocol = str(fmt.get("protocol") or "")
    return bool(fmt.get("url")) and protocol.startswith("http") and not fmt.get("fragments")


def _format_size(fmt: dict) -> int:
    return int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)


def _pick_bilibili_transcription_format(info: dict) -> dict:
    formats = [fmt for fmt in info.get("formats", []) if _is_direct_http_format(fmt)]
    audio = [
        fmt for fmt in formats
        if fmt.get("vcodec") == "none" and fmt.get("acodec") not in (None, "none")
    ]
    combined = [
        fmt for fmt in formats
        if fmt.get("vcodec") not in (None, "none") and fmt.get("acodec") not in (None, "none")
    ]
    candidates = audio or combined
    if not candidates:
        raise RuntimeError("Bilibili 未返回可直接下载的音频/视频格式")
    return max(
        candidates,
        key=lambda fmt: (
            float(fmt.get("abr") or fmt.get("tbr") or 0),
            _format_size(fmt),
            int(fmt.get("quality") or 0),
        ),
    )


def _safe_extension(ext: str | None, default: str = "mp4") -> str:
    ext = (ext or default).lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]{1,8}", ext):
        return default
    return ext


def _download_bilibili_transcription_media_sync(url: str, out_dir: str) -> str:
    info = _extract_bilibili_info(url)
    fmt = _pick_bilibili_transcription_format(info)
    ext = _safe_extension(fmt.get("ext"), "m4a")
    out_path = os.path.join(out_dir, f"bilibili_media.{ext}")
    _download_sync(fmt["url"], out_path, headers=_bilibili_headers(info, fmt))
    return out_path


def _probe_media_streams(path: str) -> list[dict] | None:
    if not shutil.which("ffprobe"):
        return None
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,duration",
            "-of",
            "json",
            path,
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    streams = data.get("streams")
    return streams if isinstance(streams, list) else None


def _has_video_stream(path: str) -> bool | None:
    streams = _probe_media_streams(path)
    if streams is None:
        return None
    return any(stream.get("codec_type") == "video" for stream in streams)


def _ensure_video_stream(path: str) -> None:
    has_video = _has_video_stream(path)
    if has_video is False:
        raise RuntimeError(
            "下载完成，但输出文件没有视频画面。请更新 yt-dlp/ffmpeg 后重试，或换 VLC 播放器验证。"
        )


def _find_downloaded_file(out_dir: str, before: set[str]) -> str:
    after = {
        os.path.join(out_dir, name)
        for name in os.listdir(out_dir)
        if os.path.isfile(os.path.join(out_dir, name))
    }
    created = sorted(after - before, key=lambda path: os.path.getmtime(path), reverse=True)
    video_created = [path for path in created if _has_video_stream(path) is True]
    if video_created:
        return video_created[0]
    mp4_created = [path for path in created if Path(path).suffix.lower() == ".mp4"]
    if mp4_created:
        return mp4_created[0]
    if created:
        return created[0]
    existing = sorted(after, key=lambda path: os.path.getmtime(path), reverse=True)
    if existing:
        return existing[0]
    raise RuntimeError("下载完成但未找到输出文件")


def _download_bilibili_video_sync(url: str, out_dir: str) -> str:
    YoutubeDL = _load_ytdlp()
    before = {
        os.path.join(out_dir, name)
        for name in os.listdir(out_dir)
        if os.path.isfile(os.path.join(out_dir, name))
    }
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "format": _BILIBILI_VIDEO_FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(out_dir, "%(title).100B [%(id)s].%(ext)s"),
        "windowsfilenames": True,
        "http_headers": {"User-Agent": _UA},
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    downloads = info.get("requested_downloads") or []
    for item in downloads:
        for key in ("filepath", "filename", "_filename"):
            path = item.get(key)
            if path and os.path.exists(path):
                _ensure_video_stream(path)
                return path
    path = _find_downloaded_file(out_dir, before)
    _ensure_video_stream(path)
    return path


# ── 下载（urllib，无 ffmpeg 网络调用）───────────────────

def _download_sync(video_url: str, out_path: str, headers: dict | None = None) -> None:
    """用 urllib 同步下载媒体文件。在线程池中调用。"""
    request_headers = {"User-Agent": _UA, "Referer": "https://www.douyin.com/"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(
        video_url,
        headers=request_headers,
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
    beam_size=1（贪心解码）比 beam_size=5 快。语言交给 Whisper 自动识别，
    避免英文口播被强制按中文解码。
    """
    from faster_whisper import WhisperModel

    if model_size not in _ALLOWED_MODELS:
        model_size = WHISPER_MODEL
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(file_path, beam_size=1)
    return "\n".join(seg.text.strip() for seg in segments)


# ── 通用平台流程 ─────────────────────────────────────────

async def _download_transcription_media(real_url: str, out_dir: str) -> tuple[str, str]:
    """Download the best media file for transcription and return (path, platform)."""
    platform = _detect_platform(real_url)
    loop = asyncio.get_running_loop()
    if platform == "douyin":
        video = await _get_douyin_video_object(real_url)
        dl_url = _pick_url_for_transcription(video)
        out_path = os.path.join(out_dir, "douyin_media.mp4")
        await loop.run_in_executor(_DOWNLOAD_EXECUTOR, _download_sync, dl_url, out_path)
        return out_path, platform
    if platform == "bilibili":
        out_path = await loop.run_in_executor(
            _DOWNLOAD_EXECUTOR, _download_bilibili_transcription_media_sync, real_url, out_dir
        )
        return out_path, platform
    raise ValueError(f"暂不支持这个平台: {platform}")


async def _download_video_file(real_url: str, out_dir: str) -> tuple[str, str]:
    """Download the source video and return (path, platform)."""
    platform = _detect_platform(real_url)
    loop = asyncio.get_running_loop()
    if platform == "douyin":
        video = await _get_douyin_video_object(real_url)
        dl_url = _pick_url_for_download(video)
        out_path = os.path.join(out_dir, "douyin_video.mp4")
        await loop.run_in_executor(_DOWNLOAD_EXECUTOR, _download_sync, dl_url, out_path)
        await loop.run_in_executor(_DOWNLOAD_EXECUTOR, _ensure_video_stream, out_path)
        return out_path, platform
    if platform == "bilibili":
        out_path = await loop.run_in_executor(
            _DOWNLOAD_EXECUTOR, _download_bilibili_video_sync, real_url, out_dir
        )
        return out_path, platform
    raise ValueError(f"暂不支持这个平台: {platform}")


async def _transcribe_url_async(url: str, model_size: str = WHISPER_MODEL) -> tuple[str, str, float]:
    """Download media for a supported URL and transcribe it."""
    real_url = _extract_url(url)
    loop = asyncio.get_running_loop()
    with tempfile.TemporaryDirectory(prefix="video_transcribe_") as tmp:
        media_path, platform = await _download_transcription_media(real_url, tmp)
        size_mb = os.path.getsize(media_path) / 1024 / 1024
        transcript = await loop.run_in_executor(
            _TRANSCRIBE_EXECUTOR, _transcribe_sync, media_path, model_size
        )
    return transcript, platform, size_mb


async def _download_video_async(url: str) -> tuple[str, str, float]:
    """Download source video for a supported URL into a persistent temp directory."""
    real_url = _extract_url(url)
    out_dir = tempfile.mkdtemp(prefix="video_dl_")
    video_path, platform = await _download_video_file(real_url, out_dir)
    size_mb = os.path.getsize(video_path) / 1024 / 1024
    return video_path, platform, size_mb


# ── MCP 工具 ─────────────────────────────────────────────

@mcp.tool()
async def analyze_douyin(url: str, model_size: str = WHISPER_MODEL) -> str:
    """
    抖音/Bilibili 视频一键转录：下载适合转录的媒体 → Whisper 转录 → 返回文字稿。

    url: 抖音或 Bilibili 分享链接/分享文本（自动提取URL）。
         支持格式：
           - 纯URL:  https://v.douyin.com/43Hxli09K70/
           - 长URL:  https://www.douyin.com/video/7628423061288682112
           - Bilibili: https://www.bilibili.com/video/BV...
           - 分享文本: "5.33 复制打开抖音... https://v.douyin.com/xxx/"，自动提取URL
    model_size: Whisper 模型大小，默认 "tiny"（快）。
                若文字识别明显有误，可改用 "small"（更准但慢约 4x）。
                可选: tiny / base / small / medium / large-v3
    """
    try:
        transcript, platform, _ = await _transcribe_url_async(url, model_size)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"转录失败: {e}"

    if not transcript.strip():
        return f"{_platform_label(platform)} 转录完成，但未检测到语音内容（视频可能没有人声）。"

    return transcript


@mcp.tool()
async def analyze_video(url: str, model_size: str = WHISPER_MODEL) -> str:
    """
    通用视频转文字：支持抖音和 Bilibili 链接，返回 Whisper 文字稿。
    参数同 analyze_douyin。
    """
    return await analyze_douyin(url, model_size)


@mcp.tool()
async def download_douyin(url: str) -> str:
    """
    下载抖音/Bilibili 视频，返回本地文件路径。
    文件保存在系统临时目录，不会自动清理，请手动删除。

    url: 抖音或 Bilibili 分享链接/分享文本（自动提取URL）。
    """
    try:
        out_path, _, _ = await _download_video_async(url)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"下载失败: {e}"

    return out_path


@mcp.tool()
async def download_video(url: str) -> str:
    """
    通用视频下载：支持抖音和 Bilibili 链接，返回本地文件路径。
    参数同 download_douyin。
    """
    return await download_douyin(url)


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
    """后台跑完整流程：URL 提取 → 下载 → 转录。"""
    job = _JOBS[job_id]
    loop = asyncio.get_event_loop()
    tmp_dir = None
    try:
        job["stage"] = "extracting_url"
        real_url = _extract_url(url)

        tmp_dir = tempfile.mkdtemp(prefix="douyin_job_")

        job["stage"] = "downloading"
        media_path, platform = await _download_transcription_media(real_url, tmp_dir)

        job["stage"] = "transcribing"
        text = await loop.run_in_executor(
            _TRANSCRIBE_EXECUTOR, _transcribe_sync, media_path, model_size
        )

        job["status"] = "done"
        job["stage"] = "done"
        job["result"] = text or f"（{_platform_label(platform)} 未检测到语音内容）"
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
    【推荐 Claude Desktop 使用】抖音/Bilibili 视频转文字（异步）。
    立即返回 job_id（<1秒），后台执行下载+转录。
    随后请调用 get_transcript_result(job_id) 取结果（该工具会等待最多 25 秒，未完成请再次调用）。

    适用场景：Claude Desktop chat 等客户端 MCP 工具调用有硬超时（约 30-60 秒），
    无法承受完整流程（25-90 秒）的同步调用。

    url: 抖音或 Bilibili 分享链接/分享文本（自动提取URL）。
         支持: https://v.douyin.com/xxx/、https://www.douyin.com/video/xxx、
               https://www.bilibili.com/video/BV... 或整段分享文本
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
async def video_to_text(url: str, model_size: str = WHISPER_MODEL) -> str:
    """
    通用异步视频转文字：支持抖音和 Bilibili，返回 job_id。
    参数同 douyin_to_text。
    """
    return await douyin_to_text(url, model_size)


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
    使用 faster-whisper（默认 tiny 模型，自动识别语言）。

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
            _TRANSCRIBE_EXECUTOR, _transcribe_sync, str(path), model_size
        )
    except Exception as e:
        return f"转录失败: {e}"

    if not transcript.strip():
        return "转录完成，但未检测到语音内容。"

    return transcript


if __name__ == "__main__":
    mcp.run(transport="stdio")
