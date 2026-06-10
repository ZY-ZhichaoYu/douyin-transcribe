# 抖音 / Bilibili 视频转文字

> 粘贴抖音或 Bilibili 链接，自动下载媒体，用本地 Whisper 转成文字稿。
> 也可以只下载原视频到本机。

[English](#english) | [中文](#中文)

---

## 中文

### 这是什么？

这个项目适合这样的日常流程：

1. 别人发来一条抖音或 Bilibili 视频链接。
2. 你不想完整看视频，只想要里面说了什么。
3. 打开本地网页，把链接粘进去。
4. 点击“开始转录”，拿到文字稿。
5. 把文字稿发给 Claude / GPT / Gemini 做摘要、翻译、分析，或者自己快速阅读。

支持：

- 抖音短链：`https://v.douyin.com/.../`
- 抖音长链：`https://www.douyin.com/video/...`
- 抖音 App 整段分享文本
- Bilibili 视频：`https://www.bilibili.com/video/BV...`
- Bilibili 短链：`https://b23.tv/...`（由 `yt-dlp` 解析）

也可以粘贴不带 `https://` 的裸域名链接，例如 `bilibili.com/video/BV...`，程序会自动补全。

### 界面里能做什么？

打开网页后主要有两个操作：

- **开始转录**：下载适合转录的音频/视频流，然后用 `faster-whisper` 生成文字稿。
- **下载视频**：只下载原视频，不转文字。抖音会下载可用 MP4；Bilibili 会用 `yt-dlp` 下载视频流和音频流并合并成 MP4。

默认模型是 `base`。如果只想快速看大意，可以选 `tiny`；如果要更干净的文字稿，选 `small` 或 `medium`。
注意：十几二十分钟的长视频在 CPU 上转录会明显变慢，尤其是 `medium`。日常使用建议先用 `base` 或 `small`，只有对准确率要求很高时再用 `medium`。

### 如果你本地已经有这个项目

Windows PowerShell：

```powershell
Set-Location C:\path\to\douyin-transcribe
# 例如本机：Set-Location E:\ZY_Work_from_20260402\GitHub\douyin-transcribe

git pull
python -m pip install --upgrade pip
python -m pip install --upgrade -r requirements.txt
python -m playwright install chromium

python app.py
```

然后打开：

```text
http://127.0.0.1:7860
```

注意：运行 `python app.py` 的终端窗口要保持打开。关掉终端，本地网页服务也会停止。
如果 `7860` 端口已经被占用，程序会自动尝试 `7861`、`7862` 等后续端口，并在终端里打印实际地址。

### 快捷启动

本仓库带了两个快捷启动脚本：

```text
run_web.bat
run_web.ps1
```

最简单的方式是在资源管理器里双击 `run_web.bat`。它会进入项目目录，自动创建 `.venv`，补齐 Python 依赖，安装 Playwright Chromium，然后运行 `python app.py`。
第一次运行需要联网下载依赖和浏览器内核，时间会比较久；后续再启动会快很多。

PowerShell 方式：

```powershell
Set-Location C:\path\to\douyin-transcribe
.\run_web.ps1
```

如果 PowerShell 提示脚本执行策略限制，可以先用普通方式：

```powershell
python app.py
```

### 第一次全新安装

如果这台电脑上还没有仓库：

```powershell
git clone https://github.com/ZY-ZhichaoYu/douyin-transcribe.git
cd douyin-transcribe

python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install --upgrade -r requirements.txt
python -m playwright install chromium

python app.py
```

第一次转录会自动下载 Whisper 模型：

- Web UI 默认 `base`，约 74MB。
- MCP 工具默认 `tiny`，约 39MB，便于更快返回。

### 常用操作

#### 抖音转文字

1. 复制抖音 App 分享文本，整段都可以。
2. 粘贴到“视频链接 / 分享文本”。
3. 模型默认 `base`。
4. 点击“开始转录”。
5. 右侧“文字稿（可复制）”出现结果后，复制给 AI 或自己阅读。

#### Bilibili 转文字

1. 复制 Bilibili 视频链接，例如 `https://www.bilibili.com/video/BV...`。
2. 粘贴到同一个输入框。
3. 点击“开始转录”。

Bilibili 转录会优先下载音频流，速度通常比下载完整视频更快。

#### 下载视频

在“转文字”页或“仅下载视频”页都可以点“下载视频 / 下载（最高画质）”。

下载完成后会看到：

- 浏览器里的下载文件控件。
- 本地文件路径，例如 `C:\Users\...\AppData\Local\Temp\video_dl_xxx\xxx.mp4`。

文件保存在系统临时目录，不会立刻自动删除；需要长期保存时，可以把它移动到自己的视频目录。

### 依赖说明

核心依赖：

- `playwright`：抖音页面抓取和接口拦截。
- `yt-dlp`：Bilibili 下载和格式解析。
- `ffmpeg`：Bilibili 视频流 + 音频流合并成 MP4 时需要。
- `faster-whisper`：本地语音识别。
- `gradio`：本地网页界面。

检查依赖：

```powershell
python -c "import gradio, playwright, faster_whisper, yt_dlp, mcp; print('ok')"
ffmpeg -version
```

如果 `ffmpeg -version` 不存在，Bilibili 转文字通常仍可工作，因为它只下载音频；但 Bilibili “下载视频”可能无法把视频流和音频流合并成一个 MP4。Windows 可以安装 Gyan.dev 或 `winget install Gyan.FFmpeg`。

### MCP 用法

`server.py` 仍然可以作为 MCP server 使用。

Claude Desktop 配置示例：

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

可用工具：

| 工具 | 用途 |
|------|------|
| `analyze_video(url)` | 通用同步转录，支持抖音和 Bilibili |
| `video_to_text(url)` | 通用异步转录，返回 `job_id` |
| `get_transcript_result(job_id)` | 获取异步任务结果 |
| `download_video(url)` | 通用下载视频，支持抖音和 Bilibili |
| `transcribe_video(file_path)` | 转录本地视频或音频文件 |
| `analyze_douyin(url)` | 兼容旧工具名，现在也可处理 Bilibili |
| `douyin_to_text(url)` | 兼容旧工具名，现在也可处理 Bilibili |
| `download_douyin(url)` | 兼容旧工具名，现在也可处理 Bilibili |

如果 MCP 客户端容易超时，优先用异步流程：

1. 调 `video_to_text(url)`，拿到 `job_id`。
2. 调 `get_transcript_result(job_id)`，没完成就隔一会儿再调同一个 `job_id`。

### 技术原理

抖音：

- 用 headless Chromium 打开页面。
- 拦截 `aweme/v1/web/aweme/detail` 接口。
- 如果浏览器没有拦截到接口，会回退到移动端分享页里的 `window._ROUTER_DATA` 解析，减少短链偶发失败。
- 转录时优先使用 `bit_rate_audio` 音频流，没有音频流时回退到带音频的 MP4。

Bilibili：

- 用 `yt-dlp` 提取视频信息和直链。
- 转录时优先下载音频流。
- 下载视频时优先选择 H.264/AVC 视频流和 M4A 音频流，并让 ffmpeg 合并成 MP4。这样比 HEVC/H.265 更容易在 Windows 默认播放器和浏览器里正常出画面。

Whisper：

- 语言自动识别，适合中文、英文或中英混杂视频。
- 默认 CPU + int8，速度优先，不需要显卡。

### 已知限制

- 主要测试单视频链接。合集、图集、直播回放不保证。
- Bilibili 未登录时通常只能拿到游客可看的清晰度；1080P、4K、会员视频需要 cookies，本项目暂未做登录/cookies 导入界面。
- 抖音或 Bilibili 修改网页接口时，抓取可能失效，需要更新代码或 `yt-dlp`。
- 语音识别质量取决于音频质量、背景音乐、多人重叠、模型大小。

### 常见问题

**Q: 打开 `127.0.0.1:7860` 显示 refused to connect？**
A: 本地服务没跑。先在项目目录执行 `python app.py`，并保持终端打开。

**Q: 运行时报 `Cannot find empty port in range: 7860-7860`？**
A: 旧版本会固定占用 `7860`。更新后请先 `git pull`，再重新运行 `python app.py`；如果 `7860` 被占用，程序会自动换到下一个可用端口。也可以手动指定：
```powershell
$env:GRADIO_SERVER_PORT = "7861"
python app.py
```

**Q: 进度条停在转录中，是不是卡死了？**
A: 不一定。下载完成后进入 Whisper 转录，CPU 上处理长视频会很慢，`medium` 最慢。20 分钟视频建议先用 `base` 或 `small`，确认内容够用后再考虑 `medium`。

**Q: Playwright 报 browser executable 不存在？**
A: 执行：

```powershell
python -m playwright install chromium
```

**Q: Bilibili 下载视频失败，但转文字可以？**
A: 多数是没有 ffmpeg，导致视频流和音频流不能合并。执行 `ffmpeg -version` 检查。

**Q: 下载的 MP4 只有声音，没有画面？**
A: 多数是播放器不支持 HEVC/H.265。当前版本已优先下载 H.264/AVC 格式；先 `git pull` 后重新下载。如果仍有问题，可以换 VLC 播放器验证。

**Q: 转录结果错字很多？**
A: 先把模型从 `tiny` 或 `base` 调到 `small`。财经、技术、英文夹杂视频建议至少用 `base` 或 `small`。

**Q: 为什么下载的视频在 Temp 目录？**
A: 这是为了不污染项目目录。下载完成后界面会显示本地路径，需要长期保存时手动移动即可。

### 致谢

- [Playwright](https://playwright.dev/)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [Gradio](https://www.gradio.app/)
- [Model Context Protocol](https://modelcontextprotocol.io/)

### 许可证

MIT License，见 [LICENSE](./LICENSE)。

---

## English

### What Is This?

This is a local web app and MCP server that turns Douyin or Bilibili videos into text.
Paste a video URL, download the media locally, transcribe it with faster-whisper, then copy the transcript into an AI model for summary or analysis.

Supported inputs:

- Douyin short links: `https://v.douyin.com/.../`
- Douyin video URLs: `https://www.douyin.com/video/...`
- Full Douyin app share text
- Bilibili BV URLs: `https://www.bilibili.com/video/BV...`
- Bilibili short links: `https://b23.tv/...`

Bare URLs without `https://`, such as `bilibili.com/video/BV...`, are accepted and normalized automatically.

### Quick Start

If you already have the repo locally:

```powershell
Set-Location C:\path\to\douyin-transcribe
git pull
python -m pip install --upgrade -r requirements.txt
python -m playwright install chromium
python app.py
```

Then open:

```text
http://127.0.0.1:7860
```

Keep the `python app.py` terminal open while using the web UI.
If port `7860` is already busy, the app will automatically try the next available port and print the actual local URL in the terminal.

You can also double-click `run_web.bat` on Windows. The script creates `.venv` if needed, installs Python dependencies, installs Playwright Chromium, then starts `python app.py`.

### Dependencies

- Playwright for Douyin browser capture
- yt-dlp for Bilibili extraction and download
- ffmpeg for merging Bilibili video and audio into MP4
- faster-whisper for local transcription
- Gradio for the web UI

### MCP Tools

| Tool | Purpose |
|------|---------|
| `analyze_video(url)` | Synchronous transcript for Douyin or Bilibili |
| `video_to_text(url)` | Start async transcription and return a job id |
| `get_transcript_result(job_id)` | Poll async transcription result |
| `download_video(url)` | Download source video |
| `transcribe_video(file_path)` | Transcribe a local media file |

Legacy tool names (`analyze_douyin`, `douyin_to_text`, `download_douyin`) are kept for compatibility.

### Notes

For long videos on CPU, `medium` can take a long time. Start with `base` or `small` for everyday use, then rerun with `medium` only when you need the extra accuracy.

Douyin extraction first tries the browser-captured detail API. If that fails, it falls back to parsing `window._ROUTER_DATA` from the mobile share page.

Bilibili downloads use yt-dlp. Guest access may only expose lower resolutions; premium or login-only formats need cookies, which this UI does not manage yet.

MIT License. See [LICENSE](./LICENSE).
