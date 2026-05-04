# 抖音视频转文字 / Douyin to Text

> 粘贴抖音分享链接 → 自动下载视频 → 用 Whisper 转录为文字稿。
> 一步完成，无需手动跑两个网站。

[English](#english) | [中文](#中文)

---

## 中文

### 这是什么？

像 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 或市面上各种"抖音视频解析下载"在线工具一样可以拿到视频，
**但额外多了一步**：自动用本地 Whisper 模型转录出语音文字。

适合这种场景：朋友扔过来一条抖音链接 → 你只想要文字内容 → 复制粘贴给 Claude / GPT / Grok / Gemini 做摘要、翻译、分析。

### 三种使用方式

| 方式 | 适合谁 | 怎么跑 |
|------|--------|--------|
| **网页 UI** | 不写代码的普通用户 | `python app.py`，浏览器打开 `127.0.0.1:7860` |
| **MCP server** | Claude Desktop / Claude Code 用户 | 配进 MCP，对 AI 直接说"分析这条抖音" |
| **Python 库** | 想集成到自己脚本 | `from server import _get_video_object, _transcribe_sync` |

### 快速开始

```bash
# 1. 克隆
git clone https://github.com/ZY-ZhichaoYu/douyin-transcribe.git
cd douyin-transcribe

# 2. 装依赖（建议用虚拟环境）
pip install -r requirements.txt
playwright install chromium

# 3a. 跑网页 UI
python app.py
# 然后浏览器打开 http://127.0.0.1:7860

# 3b. 或作为 MCP server 使用（见下文 MCP 配置章节）
python server.py
```

第一次运行会自动下载 Whisper `tiny` 模型（~39MB）。

### 常规开始（推荐）

如果你是第一次在本机跑 Python 项目，建议按下面的完整流程来，避免依赖污染到系统 Python。

#### Windows PowerShell

```powershell
git clone https://github.com/ZY-ZhichaoYu/douyin-transcribe.git
cd douyin-transcribe

python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

python app.py
```

看到类似下面的输出就说明服务启动成功：

```text
Running on local URL: http://127.0.0.1:7860
```

然后打开浏览器访问 [http://127.0.0.1:7860](http://127.0.0.1:7860)，粘贴抖音分享链接或整段分享文本即可。运行 `python app.py` 的终端窗口不要关闭；关闭后本地网页服务也会停止。

#### macOS / Linux

```bash
git clone https://github.com/ZY-ZhichaoYu/douyin-transcribe.git
cd douyin-transcribe

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

python app.py
```

#### 验证安装

可以用下面的命令确认核心依赖可导入：

```bash
python -c "import gradio, playwright, faster_whisper; print('ok')"
```

如果网页打不开，先看终端里 `python app.py` 是否还在运行。浏览器显示 `127.0.0.1 refused to connect` 通常表示本地服务没有启动成功，而不是浏览器问题。

### MCP 配置

#### Claude Desktop

编辑 `%APPDATA%\Claude\claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "douyin": {
      "command": "python",
      "args": ["C:\\path\\to\\douyin-transcribe\\server.py"]
    }
  }
}
```

#### Claude Code

```bash
claude mcp add douyin -s user python /path/to/douyin-transcribe/server.py
```

并建议在 `~/.claude/settings.json` 里加：

```json
{
  "env": {
    "MCP_TOOL_TIMEOUT": "120000"
  }
}
```

### 工具清单（MCP）

| 工具 | 用途 | 推荐场景 |
|------|------|----------|
| `analyze_douyin(url)` | 同步：下载+转录，一次返回文字稿 (~25s) | Claude Code |
| `douyin_to_text(url)` | 异步：立即返回 `job_id`，后台干活 | **Claude Desktop**（避超时） |
| `get_transcript_result(job_id)` | 取异步任务结果（最多等 25 秒） | 配合 `douyin_to_text` 使用 |
| `download_douyin(url)` | 仅下载视频（最高画质） | 想保留原视频 |
| `transcribe_video(file_path)` | 转录本地视频/音频文件 | 本地已有视频 |

### 性能与精度

实测一段 4 分钟的财经口播视频（i5 笔记本，CPU）：

| 阶段 | 耗时 |
|------|------|
| Playwright 拦截 CDN URL | ~10s |
| 下载（自动选低码率，~20MB） | ~6s |
| Whisper `tiny` 转录 | ~7s |
| **合计** | **~25s** |

精度参考：

- `tiny` (39MB)：核心意思 OK，专有名词同音字偶有错误（"白酒"→"通车"），适合摘要、要点提取
- `small` (244MB)：~80s 左右，准度明显提升，适合需要精确文字的场景
- `medium` (1.5GB)：~3 分钟，几乎逐字准确，适合长视频字幕

切换模型：`analyze_douyin(url, model_size="small")`

### 技术原理

抖音视频下载的难点不是带宽，是 **cookie**：直接 `requests.get(video_url)` 会被反爬挡掉，
yt-dlp 需要从浏览器导出 `s_v_web_id` cookie，但 Chrome/Edge 的 SQLite 文件常被 WebView2 锁住，导不出。

本项目的解法：**用 headless Chromium 自己充当浏览器**，让抖音的 JS 自然跑起来生成 cookie，
拦截 `aweme/v1/web/aweme/detail` 这条 API 响应，从中取出无水印 CDN 直链（`zjcdn.com`），
然后 urllib 下载。整个过程不依赖系统 cookie。

转录用 [faster-whisper](https://github.com/SYSTRAN/faster-whisper)，
比官方 openai-whisper 快 4 倍且占内存少。

### 已知限制

- 只测试了**单视频**链接，**图集/合集/直播回放**未测试
- 中文语音识别效果取决于模型大小和音频质量；背景音乐大、多人重叠时准度下降
- 不支持需要登录才能看的私密视频
- 抖音改 API 路径会失效（截至 2026-05 工作正常）

### 常见问题

**Q: Playwright 报 "Executable doesn't exist"？**
A: 跑一次 `playwright install chromium`。

**Q: faster-whisper 报 CUDA 错误？**
A: 默认配置是 CPU (`device="cpu", compute_type="int8"`)，不需要 GPU。如果你装了 CUDA 想加速，把 `_transcribe_sync` 里的 `device` 改 `"cuda"`。

**Q: faster-whisper / PyAV 报 `tuple index out of range`？**
A: 通常是拿到了只有视频、没有音频的 DASH 分片。本项目现在转录时会优先选择普通 MP4 候选，以避免这个问题。如果仍遇到，请先更新到最新代码。

**Q: Claude Desktop 调用工具一直超时？**
A: 用 `douyin_to_text` 而不是 `analyze_douyin`，前者立即返回 job_id，规避超时。

### 致谢

- [Playwright](https://playwright.dev/) — headless browser
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 语音识别
- [Gradio](https://www.gradio.app/) — Web UI
- [Model Context Protocol](https://modelcontextprotocol.io/) — Claude 的工具协议

### 许可证

MIT License — 详见 [LICENSE](./LICENSE)

---

## English

### What is this?

Douyin to Text downloads a Douyin (Chinese TikTok) video from a share link and
transcribes the speech locally with Whisper.

Paste a Douyin share URL, or the full share text copied from the Douyin app, and
get a transcript that you can send to Claude, ChatGPT, Gemini, or another LLM
for summary, translation, note taking, or analysis.

### Features

- Web UI for non-technical users, powered by Gradio
- MCP server for Claude Desktop / Claude Code
- Local transcription with faster-whisper
- No browser cookie export required
- Supports plain Douyin URLs and full app share text
- Automatically avoids DASH video-only streams when selecting a file for transcription

### Why not yt-dlp?

yt-dlp needs a fresh `s_v_web_id` cookie from douyin.com, which on Windows
gets locked by WebView2 even when the browser is closed.
This tool uses headless Chromium instead, so the cookie is generated on-the-fly
and never needs to be exported from the host browser.

### Quick Start

```bash
git clone https://github.com/ZY-ZhichaoYu/douyin-transcribe.git
cd douyin-transcribe
pip install -r requirements.txt
playwright install chromium

# Web UI:
python app.py        # then open http://127.0.0.1:7860

# Or as MCP server (for Claude Desktop / Code):
python server.py
```

The first run downloads the default Whisper `tiny` model.

### Standard Setup

Use a virtual environment if you want a cleaner local install.

#### Windows PowerShell

```powershell
git clone https://github.com/ZY-ZhichaoYu/douyin-transcribe.git
cd douyin-transcribe

python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

python app.py
```

Open [http://127.0.0.1:7860](http://127.0.0.1:7860), paste a Douyin link, and start transcription.

#### macOS / Linux

```bash
git clone https://github.com/ZY-ZhichaoYu/douyin-transcribe.git
cd douyin-transcribe

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

python app.py
```

### MCP Setup

#### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` on Windows, or the equivalent Claude Desktop config file on macOS:

```json
{
  "mcpServers": {
    "douyin": {
      "command": "python",
      "args": ["C:\\path\\to\\douyin-transcribe\\server.py"]
    }
  }
}
```

#### Claude Code

```bash
claude mcp add douyin -s user python /path/to/douyin-transcribe/server.py
```

If your client has a short MCP timeout, prefer the asynchronous workflow:

1. Call `douyin_to_text(url)` and keep the returned `job_id`.
2. Call `get_transcript_result(job_id)` until the transcript is ready.

### MCP Tools

| Tool | Purpose |
|------|---------|
| `analyze_douyin(url)` | Synchronous download and transcription |
| `douyin_to_text(url)` | Start an asynchronous background transcription job |
| `get_transcript_result(job_id)` | Poll an asynchronous transcription job |
| `download_douyin(url)` | Download the source video only |
| `transcribe_video(file_path)` | Transcribe a local video or audio file |

### Model Choice

The default model is `tiny`, which is fast and good enough for summaries. Use
`small` or `medium` when you need cleaner text:

```python
analyze_douyin(url, model_size="small")
```

Available model names include `tiny`, `base`, `small`, `medium`, and `large-v3`.

### Troubleshooting

**`127.0.0.1 refused to connect`**

The local Gradio service is not running. Start it with `python app.py` and keep
that terminal open.

**Playwright says the browser executable does not exist**

Run:

```bash
playwright install chromium
```

**Transcription fails with `tuple index out of range`**

This usually means the selected video file has no audio stream. Update to the
latest code; transcription now prefers progressive MP4 streams and avoids DASH
video-only fragments.

### Limitations

- Tested mainly with single-video Douyin links
- Private or login-only videos are not supported
- Speech recognition accuracy depends on audio quality and model size
- Douyin may change its web APIs, which can break metadata extraction

### License

MIT License. See [LICENSE](./LICENSE).
