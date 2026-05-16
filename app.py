"""
Douyin / Bilibili → Text  (Gradio Web UI)

粘贴抖音或 Bilibili 分享链接，自动下载媒体并用 Whisper 转录为文字。
适合不用 MCP 的普通用户在浏览器里直接使用。

运行：
    pip install -r requirements.txt
    playwright install chromium
    python app.py

然后浏览器打开 http://127.0.0.1:7860
"""
import asyncio
import os
import sys

import gradio as gr

# 复用 server.py 里的核心逻辑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import (
    _download_video_async,
    _platform_label,
    _transcribe_url_async,
)

WEB_DEFAULT_MODEL = "base"


def transcribe(url: str, model_size: str, progress=gr.Progress()) -> tuple[str, str]:
    """
    返回 (transcript_text, status_message)。
    用 gr.Progress 实时显示阶段。
    """
    if not url or not url.strip():
        return "", "请输入抖音或 Bilibili 链接"

    try:
        progress(0.1, desc="下载媒体并转录...")
        text, platform, size_mb = asyncio.run(_transcribe_url_async(url, model_size))
    except Exception as e:
        return "", f"处理失败: {e}"

    if not text.strip():
        return "", "转录完成，但视频中未检测到语音"

    progress(1.0, desc="完成")
    status = f"✅ 完成 | {_platform_label(platform)} | 媒体 {size_mb:.1f}MB | 模型 {model_size}"
    return text, status


def download_only(url: str, progress=gr.Progress()) -> tuple[str | None, str, str]:
    """只下载视频（最高画质），返回本地路径。"""
    if not url or not url.strip():
        return None, "", "请输入抖音或 Bilibili 链接"
    try:
        progress(0.15, desc="下载视频...")
        out_path, platform, size_mb = asyncio.run(_download_video_async(url))
        progress(1.0)
        status = f"✅ 下载完成 | {_platform_label(platform)} | {size_mb:.1f}MB | 路径: {out_path}"
        return out_path, out_path, status
    except Exception as e:
        return None, "", f"失败: {e}"


with gr.Blocks(title="视频转文字 / Douyin & Bilibili to Text") as demo:
    gr.Markdown(
        """
        # 抖音 / Bilibili 视频 → 文字稿
        粘贴抖音分享链接、整段抖音分享文本，或 Bilibili `BV` 链接，一键提取视频中的语音文字。

        - 默认 **base** 模型：速度和识别质量比较均衡，适合拿去给 AI 做后续分析
        - 很急可切到 **tiny**；复杂内容/嘉宾对谈建议切到 **small** 或 **medium**
        - 可直接转录，也可以只下载原视频到本机
        """
    )

    with gr.Tab("转文字"):
        with gr.Row():
            with gr.Column(scale=3):
                url_in = gr.Textbox(
                    label="视频链接 / 分享文本",
                    placeholder="粘贴 https://v.douyin.com/xxx/、整段抖音分享文本，或 https://www.bilibili.com/video/BV...",
                    lines=2,
                )
                model_in = gr.Radio(
                    choices=["tiny", "base", "small", "medium"],
                    value=WEB_DEFAULT_MODEL,
                    label="Whisper 模型",
                    info="tiny: 最快但易错 / base: 默认均衡 / small: 更准更慢 / medium: 最准最慢",
                )
                with gr.Row():
                    go_btn = gr.Button("开始转录", variant="primary")
                    dl_btn_main = gr.Button("下载视频")
                dl_status_main = gr.Markdown()
                dl_file_main = gr.File(label="下载文件")
                dl_path_main = gr.Textbox(label="本地文件路径", interactive=False)
            with gr.Column(scale=4):
                status_out = gr.Markdown()
                text_out = gr.Textbox(
                    label="文字稿（可复制）",
                    lines=20,
                )

        go_btn.click(
            fn=transcribe,
            inputs=[url_in, model_in],
            outputs=[text_out, status_out],
        )
        dl_btn_main.click(
            fn=download_only,
            inputs=url_in,
            outputs=[dl_file_main, dl_path_main, dl_status_main],
        )

    with gr.Tab("仅下载视频"):
        url2 = gr.Textbox(label="抖音 / Bilibili 链接", lines=2)
        dl_btn = gr.Button("下载（最高画质）", variant="primary")
        dl_status = gr.Markdown()
        dl_file = gr.File(label="下载文件")
        dl_path = gr.Textbox(label="本地文件路径", interactive=False)
        dl_btn.click(fn=download_only, inputs=url2, outputs=[dl_file, dl_path, dl_status])

    gr.Markdown(
        """
        ---
        **隐私说明**：所有处理在本地完成，视频和音频不上传任何第三方服务（Whisper 模型本地运行）。

        **致谢**：技术栈 — Playwright（抖音元数据）+ yt-dlp（Bilibili 下载）+ faster-whisper（语音识别）+ Gradio（界面）。
        """
    )


if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7860, inbrowser=False)
