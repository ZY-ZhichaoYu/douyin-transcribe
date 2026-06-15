"""
Douyin / Bilibili → Text  (Gradio Web UI)

粘贴抖音或 Bilibili 分享链接，自动下载媒体并用 Whisper 转录为文字。
适合不用 MCP 的普通用户在浏览器里直接使用。

运行：
    python -m pip install --upgrade -r requirements.txt
    python -m playwright install chromium
    python app.py

然后浏览器打开 http://127.0.0.1:7860
"""
import asyncio
import os
import socket
import sys
import tempfile
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from collections.abc import Generator

import gradio as gr

# 复用 server.py 里的核心逻辑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import (
    _detect_platform,
    _download_transcription_media,
    _download_video_file,
    _extract_url,
    _platform_label,
    _transcribe_sync,
    _TRANSCRIBE_EXECUTOR,
)

WEB_DEFAULT_MODEL = "base"


def _format_error(exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    lower = message.lower()
    hints = []
    if "executable doesn't exist" in lower and "playwright" in lower:
        hints.append("先运行 python -m playwright install chromium")
    if "geo-restricted" in lower or "may be deleted" in lower:
        hints.append("这个视频可能已删除、地区限制、需要登录，或当前网络不可访问")
    if "ffmpeg" in lower:
        hints.append("Bilibili 下载完整视频需要 ffmpeg，可用 winget install Gyan.FFmpeg 安装")
    if "no audio" in lower or "没有音频" in message:
        hints.append("下载到的媒体没有音频流；可换一个链接，或更新后重试")
    if hints:
        message = f"{message}\n\n建议：" + "；".join(hints)
    return message


def _choose_server_port(default_port: int = 7860) -> int:
    env_port = os.environ.get("GRADIO_SERVER_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError as e:
            raise SystemExit("GRADIO_SERVER_PORT 必须是数字，例如 7861") from e

    for port in range(default_port, default_port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return default_port


async def _transcribe_for_ui(url: str, model_size: str, progress) -> tuple[str, str, float]:
    progress(0.05, desc="提取链接...")
    real_url = _extract_url(url)
    platform = _detect_platform(real_url)
    label = _platform_label(platform)

    with tempfile.TemporaryDirectory(prefix="video_transcribe_") as tmp:
        progress(0.2, desc=f"{label}: 下载转录媒体（优先音频流）...")
        media_path, platform = await _download_transcription_media(real_url, tmp)
        size_mb = os.path.getsize(media_path) / 1024 / 1024

        if model_size == "medium":
            desc = "Whisper medium 转录中；长视频在 CPU 上可能需要很久..."
        else:
            desc = f"Whisper {model_size} 转录中..."
        progress(0.55, desc=desc)

        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(
            _TRANSCRIBE_EXECUTOR, _transcribe_sync, media_path, model_size
        )

    return text, platform, size_mb


async def _download_for_ui(url: str, progress) -> tuple[str, str, float]:
    progress(0.05, desc="提取链接...")
    real_url = _extract_url(url)
    platform = _detect_platform(real_url)
    label = _platform_label(platform)

    out_dir = tempfile.mkdtemp(prefix="video_dl_")
    if platform == "bilibili":
        desc = "Bilibili: 下载最高画质并合并音视频，较大文件可能需要几分钟..."
    else:
        desc = f"{label}: 下载视频..."
    progress(0.25, desc=desc)
    out_path, platform = await _download_video_file(real_url, out_dir)
    size_mb = os.path.getsize(out_path) / 1024 / 1024
    return out_path, platform, size_mb


def transcribe(
    url: str, model_size: str, progress=gr.Progress()
) -> Generator[tuple[str, str], None, None]:
    """
    返回 (transcript_text, status_message)。
    用 gr.Progress 实时显示阶段。
    """
    if not url or not url.strip():
        yield "", "请输入抖音或 Bilibili 链接"
        return

    try:
        progress(0.02, desc="提取链接...")
        yield "", "进行中：正在提取链接..."
        real_url = _extract_url(url)
        platform = _detect_platform(real_url)
        label = _platform_label(platform)

        with tempfile.TemporaryDirectory(prefix="video_transcribe_") as tmp:
            progress(0.20, desc=f"{label}: 下载转录媒体...")
            yield "", f"进行中：{label} 正在下载适合转录的音频/视频流..."
            media_path, platform = asyncio.run(_download_transcription_media(real_url, tmp))
            size_mb = os.path.getsize(media_path) / 1024 / 1024

            if model_size == "medium":
                desc = "Whisper medium 转录中；长视频在 CPU 上可能需要很久..."
            else:
                desc = f"Whisper {model_size} 转录中..."
            progress((0, None), desc=desc)
            yield "", f"进行中：已下载 {size_mb:.1f}MB，{desc}"

            future = _TRANSCRIBE_EXECUTOR.submit(_transcribe_sync, media_path, model_size)
            started = time.monotonic()
            last_reported = -1
            while True:
                try:
                    text = future.result(timeout=1.0)
                    break
                except FuturesTimeoutError:
                    elapsed = int(time.monotonic() - started)
                    bucket = elapsed // 10
                    progress((elapsed, None), desc=f"{desc} 已运行 {elapsed}s")
                    if bucket != last_reported:
                        last_reported = bucket
                        yield "", f"进行中：{desc} 已运行 {elapsed}s。长视频在 CPU 上会停留较久。"
    except Exception as e:
        progress(None)
        yield "", f"处理失败：{_format_error(e)}"
        return

    if not text.strip():
        progress(1.0, desc="完成")
        yield "", "转录完成，但视频中未检测到语音"
        return

    progress(1.0, desc="完成")
    status = f"完成 | {_platform_label(platform)} | 媒体 {size_mb:.1f}MB | 模型 {model_size}"
    yield text, status


def download_only(
    url: str, progress=gr.Progress()
) -> Generator[tuple[object, str, str], None, None]:
    """只下载视频（最高画质），返回本地路径。"""
    if not url or not url.strip():
        yield gr.update(value=None, visible=False), "", "请输入抖音或 Bilibili 链接"
        return
    try:
        progress(0.02, desc="提取链接...")
        yield gr.update(value=None, visible=False), "", "进行中：正在提取链接..."
        real_url = _extract_url(url)
        platform = _detect_platform(real_url)
        label = _platform_label(platform)
        if platform == "bilibili":
            desc = "Bilibili: 下载最高画质并合并音视频，较大文件可能需要几分钟..."
        else:
            desc = f"{label}: 下载视频..."
        progress((0, None), desc=desc)
        yield gr.update(value=None, visible=False), "", f"进行中：{desc}"

        out_dir = tempfile.mkdtemp(prefix="video_dl_")
        out_path, platform = asyncio.run(_download_video_file(real_url, out_dir))
        size_mb = os.path.getsize(out_path) / 1024 / 1024
        progress(1.0)
        status = f"下载完成 | {_platform_label(platform)} | {size_mb:.1f}MB | 路径: {out_path}"
        yield gr.update(value=out_path, visible=True), out_path, status
    except Exception as e:
        progress(None)
        yield gr.update(value=None, visible=False), "", f"失败：{_format_error(e)}"


THEME = gr.themes.Soft(
    primary_hue=gr.themes.colors.rose,
    secondary_hue=gr.themes.colors.sky,
    neutral_hue=gr.themes.colors.slate,
    radius_size=gr.themes.sizes.radius_lg,
    # 仅用系统字体，不联网拉取 Google Fonts，符合"全程本地"的定位。
    font=[
        "Inter",
        "system-ui",
        "Segoe UI",
        "Microsoft YaHei",
        "PingFang SC",
        "sans-serif",
    ],
)

CSS = """
.gradio-container {
    max-width: 1080px !important;
    margin: 0 auto !important;
}

/* ---- Hero header ---- */
#hero {
    background: linear-gradient(135deg, #fb7185 0%, #f43f5e 45%, #6366f1 100%);
    border-radius: 20px;
    padding: 28px 32px;
    margin-bottom: 8px;
    color: #fff;
    box-shadow: 0 10px 30px -12px rgba(244, 63, 94, 0.55);
}
#hero h1 {
    margin: 0 0 6px 0;
    font-size: 1.8rem;
    font-weight: 800;
    letter-spacing: -0.01em;
    color: #fff;
}
#hero p { margin: 0; opacity: 0.95; font-size: 0.97rem; line-height: 1.55; }
#hero .badges { margin-top: 14px; display: flex; flex-wrap: wrap; gap: 8px; }
#hero .badges span {
    background: rgba(255, 255, 255, 0.18);
    border: 1px solid rgba(255, 255, 255, 0.28);
    padding: 4px 12px;
    border-radius: 999px;
    font-size: 0.8rem;
    backdrop-filter: blur(4px);
}

/* ---- Card panels ---- */
.soft-card {
    border: 1px solid var(--block-border-color);
    border-radius: 16px !important;
    padding: 18px !important;
    background: var(--block-background-fill);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}

/* ---- Primary action button ---- */
#go-btn { font-weight: 700; }
#go-btn, #dl-btn { min-height: 46px; }

/* ---- Transcript box ---- */
#transcript textarea {
    font-size: 0.95rem;
    line-height: 1.6;
}

/* ---- Footer ---- */
#footer {
    margin-top: 18px;
    padding: 16px 20px;
    border-radius: 14px;
    background: var(--block-background-fill);
    border: 1px solid var(--block-border-color);
    font-size: 0.86rem;
    color: var(--body-text-color-subdued);
}
#footer strong { color: var(--body-text-color); }
"""

with gr.Blocks(title="视频转文字 / Douyin & Bilibili to Text") as demo:
    gr.HTML(
        """
        <div id="hero">
            <h1>🎬 抖音 / Bilibili 视频 → 文字稿</h1>
            <p>粘贴抖音分享链接、整段抖音分享文本，或 Bilibili <code>BV</code> 链接，一键提取视频里说的话。
            所有处理都在本地完成。</p>
            <div class="badges">
                <span>🔒 本地运行</span>
                <span>⚡ faster-whisper</span>
                <span>🎯 中英自动识别</span>
                <span>📋 一键复制</span>
            </div>
        </div>
        """
    )

    with gr.Tab("📝 转文字"):
        with gr.Row(equal_height=False):
            with gr.Column(scale=3):
                with gr.Group(elem_classes="soft-card"):
                    url_in = gr.Textbox(
                        label="视频链接 / 分享文本",
                        placeholder="粘贴 https://v.douyin.com/xxx/、整段抖音分享文本，或 https://www.bilibili.com/video/BV...",
                        lines=3,
                    )
                    model_in = gr.Radio(
                        choices=["tiny", "base", "small", "medium"],
                        value=WEB_DEFAULT_MODEL,
                        label="Whisper 模型",
                        info="tiny 最快易错 · base 默认均衡 · small 更准更慢 · medium 最准但长视频很慢",
                    )
                    with gr.Row():
                        go_btn = gr.Button(
                            "开始转录", variant="primary", scale=2, elem_id="go-btn"
                        )
                        dl_btn_main = gr.Button("下载视频", scale=1)

                with gr.Group(elem_classes="soft-card"):
                    dl_status_main = gr.Markdown("下载结果会显示在这里。")
                    dl_file_main = gr.File(label="下载文件", height=88, visible=False)
                    dl_path_main = gr.Textbox(
                        label="本地文件路径",
                        interactive=False,
                        buttons=["copy"],
                    )

            with gr.Column(scale=4):
                with gr.Group(elem_classes="soft-card"):
                    status_out = gr.Markdown("准备就绪，粘贴链接后点击 **开始转录**。")
                    text_out = gr.Textbox(
                        label="文字稿",
                        lines=22,
                        buttons=["copy"],
                        elem_id="transcript",
                        placeholder="转录完成后，文字稿会出现在这里，可一键复制给 AI 做摘要 / 翻译 / 分析。",
                    )

        go_btn.click(
            fn=transcribe,
            inputs=[url_in, model_in],
            outputs=[text_out, status_out],
            show_progress_on=status_out,
        )
        dl_btn_main.click(
            fn=download_only,
            inputs=url_in,
            outputs=[dl_file_main, dl_path_main, dl_status_main],
            show_progress_on=dl_status_main,
        )

    with gr.Tab("⬇️ 仅下载视频"):
        with gr.Group(elem_classes="soft-card"):
            url2 = gr.Textbox(
                label="抖音 / Bilibili 链接",
                placeholder="粘贴抖音分享链接 / 文本，或 Bilibili BV 链接",
                lines=3,
            )
            dl_btn = gr.Button(
                "下载（最高画质）", variant="primary", elem_id="dl-btn"
            )
            dl_status = gr.Markdown("下载结果会显示在这里。")
            dl_file = gr.File(label="下载文件", height=88, visible=False)
            dl_path = gr.Textbox(
                label="本地文件路径",
                interactive=False,
                buttons=["copy"],
            )
        dl_btn.click(
            fn=download_only,
            inputs=url2,
            outputs=[dl_file, dl_path, dl_status],
            show_progress_on=dl_status,
        )

    gr.HTML(
        """
        <div id="footer">
            <strong>🔒 隐私说明</strong> · 所有处理在本地完成，视频和音频不上传任何第三方服务，Whisper 模型本地运行。<br>
            <strong>🛠 技术栈</strong> · Playwright（抖音元数据）· yt-dlp（Bilibili 下载）· faster-whisper（语音识别）· Gradio（界面）。
        </div>
        """
    )


if __name__ == "__main__":
    port = _choose_server_port()
    if port != 7860 and not os.environ.get("GRADIO_SERVER_PORT"):
        print(f"127.0.0.1:7860 已被占用，改用 http://127.0.0.1:{port}")
    demo.launch(
        server_name="127.0.0.1",
        server_port=port,
        inbrowser=False,
        theme=THEME,
        css=CSS,
    )
