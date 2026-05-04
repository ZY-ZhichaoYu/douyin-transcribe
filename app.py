"""
Douyin → Text  (Gradio Web UI)

粘贴抖音分享链接，自动下载视频并用 Whisper 转录为文字。
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
import tempfile

import gradio as gr

# 复用 server.py 里的核心逻辑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import (
    _extract_url,
    _get_video_object,
    _pick_url_for_transcription,
    _pick_url_for_download,
    _download_sync,
    _transcribe_sync,
    WHISPER_MODEL,
)


def transcribe(url: str, model_size: str, progress=gr.Progress()) -> tuple[str, str]:
    """
    返回 (transcript_text, status_message)。
    用 gr.Progress 实时显示阶段。
    """
    if not url or not url.strip():
        return "", "请输入抖音链接"

    try:
        progress(0.05, desc="提取 URL...")
        real_url = _extract_url(url)
    except Exception as e:
        return "", f"URL 解析失败: {e}"

    try:
        progress(0.15, desc="Playwright 抓取视频元数据...")
        video = asyncio.run(_get_video_object(real_url))
    except Exception as e:
        return "", f"获取视频信息失败: {e}"

    try:
        dl_url = _pick_url_for_transcription(video)
    except Exception as e:
        return "", f"选取下载链接失败: {e}"

    with tempfile.TemporaryDirectory(prefix="douyin_web_") as tmp:
        out_path = os.path.join(tmp, "video.mp4")
        try:
            progress(0.4, desc="下载视频（约 20MB）...")
            _download_sync(dl_url, out_path)
            size_mb = os.path.getsize(out_path) / 1024 / 1024
        except Exception as e:
            return "", f"下载失败: {e}"

        try:
            progress(0.7, desc=f"Whisper {model_size} 转录中...")
            text = _transcribe_sync(out_path, model_size)
        except Exception as e:
            return "", f"转录失败: {e}"

    if not text.strip():
        return "", "转录完成，但视频中未检测到语音"

    progress(1.0, desc="完成")
    status = f"✅ 完成 | 视频 {size_mb:.1f}MB | 模型 {model_size}"
    return text, status


def download_only(url: str, progress=gr.Progress()) -> tuple[str, str]:
    """只下载视频（最高画质），返回本地路径。"""
    if not url or not url.strip():
        return "", "请输入抖音链接"
    try:
        progress(0.1, desc="提取 URL...")
        real_url = _extract_url(url)
        progress(0.3, desc="抓取元数据...")
        video = asyncio.run(_get_video_object(real_url))
        dl_url = _pick_url_for_download(video)

        out_dir = tempfile.mkdtemp(prefix="douyin_dl_")
        out_path = os.path.join(out_dir, "video.mp4")
        progress(0.6, desc="下载中...")
        _download_sync(dl_url, out_path)
        size_mb = os.path.getsize(out_path) / 1024 / 1024
        progress(1.0)
        return out_path, f"✅ 下载完成 | {size_mb:.1f}MB | 路径: {out_path}"
    except Exception as e:
        return "", f"失败: {e}"


with gr.Blocks(title="抖音转文字 / Douyin to Text") as demo:
    gr.Markdown(
        """
        # 抖音视频 → 文字稿
        粘贴抖音分享链接（短链 `v.douyin.com` / 长链 `douyin.com/video/...` /
        App 整段分享文本均可），一键提取视频中的语音文字。

        - 默认 **tiny** 模型：~25 秒出结果，识别质量适合内容理解（专有名词偶有同音字错误）
        - 复杂内容/嘉宾对谈建议切到 **small**：慢约 4 倍，但更准
        - 转录结果可复制后发给任意 AI 模型做摘要、翻译、要点提取等下游处理
        """
    )

    with gr.Tab("转文字"):
        with gr.Row():
            with gr.Column(scale=3):
                url_in = gr.Textbox(
                    label="抖音链接 / 分享文本",
                    placeholder="粘贴 https://v.douyin.com/xxx/ 或整段抖音分享文本",
                    lines=2,
                )
                model_in = gr.Radio(
                    choices=["tiny", "small", "medium"],
                    value=WHISPER_MODEL,
                    label="Whisper 模型",
                    info="tiny: 快(~25s) / small: 准(~80s) / medium: 最准(~3min)",
                )
                go_btn = gr.Button("开始转录", variant="primary")
            with gr.Column(scale=4):
                status_out = gr.Markdown()
                text_out = gr.Textbox(
                    label="文字稿（可复制）",
                    lines=20,
                    show_copy_button=True,
                )

        go_btn.click(
            fn=transcribe,
            inputs=[url_in, model_in],
            outputs=[text_out, status_out],
        )

    with gr.Tab("仅下载视频"):
        url2 = gr.Textbox(label="抖音链接", lines=2)
        dl_btn = gr.Button("下载（最高画质）", variant="primary")
        dl_status = gr.Markdown()
        dl_path = gr.Textbox(label="本地文件路径", show_copy_button=True)
        dl_btn.click(fn=download_only, inputs=url2, outputs=[dl_path, dl_status])

    gr.Markdown(
        """
        ---
        **隐私说明**：所有处理在本地完成，视频和音频不上传任何第三方服务（Whisper 模型本地运行）。

        **致谢**：技术栈 — Playwright（绕过抖音 cookie 限制）+ faster-whisper（语音识别）+ Gradio（界面）。
        """
    )


if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7860, inbrowser=True)
