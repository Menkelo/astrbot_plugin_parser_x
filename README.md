# astrbot_plugin_parser_x

Parser X 是面向 AstrBot `aiocqhttp`（OneBot v11）的多平台分享链接解析插件。项目以
[rconsole-plugin](https://github.com/zhiyu1998/rconsole-plugin) 的平台覆盖和交互习惯为功能基线，
使用原生 Python 解析器、AstrBot 消息链与 `yt-dlp` 兼容层实现，不依赖 Yunzai 运行时。

## 支持范围

### 原生解析器

- Bilibili：视频、分 P、动态/opus、直播卡片、可选评论区卡片。
- 抖音：视频、图文和图集；短链跳转；Cookie 与 yt-dlp 兜底。
- 快手：视频、图片和图文作品。
- 微博：视频、图片与纯文本卡片。
- 小红书：视频、图片和图文笔记。

### yt-dlp 兼容层

- TikTok
- Twitter / X
- Instagram
- YouTube
- AcFun
- 西瓜视频
- 皮皮虾
- 微视
- 网易云音乐
- 汽水音乐

各平台均可在 AstrBot 插件配置页单独启停。完整的上游功能映射和未移植项见
[docs/UPSTREAM_COMPATIBILITY.md](docs/UPSTREAM_COMPATIBILITY.md)。

## 安装

在 AstrBot WebUI 的插件管理中使用本仓库地址安装：

```text
https://github.com/Menkelo/astrbot_plugin_parser_x
```

运行环境还应提供：

- `ffmpeg`：音视频合并、格式转换和 H.264 发送兜底。
- Playwright Chromium：动态、评论、直播和纯文本卡片渲染。

如 AstrBot 没有自动安装浏览器，可在 AstrBot 的 Python 环境中执行：

```bash
playwright install chromium
```

## 使用

直接在 QQ 群聊或私聊发送支持平台的分享链接。插件不会处理 AstrBot 指令消息，也会忽略
非 B站平台的直播分享。

会话管理命令：

- `/开启解析`
- `/关闭解析`
- `/解析状态`

## 配置重点

- `platforms.*`：逐个平台启停。
- `performance.max_concurrent_downloads`：下载并发上限。
- `performance.source_max_size`：单个媒体的最大体积（MB）。
- `performance.video_codec`：B站编码偏好。
- `cookies.douyin_ck`、`cookies.bili_ck`：原生解析器 Cookie。
- `cookies.ytdlp_cookie_file`：Netscape 格式 Cookie 文件，用于需要登录的平台。
- `bili_comment`：是否发送 B站评论区卡片。

缓存只写入 AstrBot 官方约定的 `data/plugin_data/astrbot_plugin_parser_x/`，默认每天清理一次。

## 上游兼容维护

仓库记录了上游 commit 和功能映射。检查是否有上游更新：

```bash
python tools/check_upstream.py
```

定时 GitHub Actions 也会执行同一检查。发现漂移后，按
[docs/UPSTREAM_SYNC.md](docs/UPSTREAM_SYNC.md) 的流程更新兼容矩阵、实现和测试，最后再修改
`upstream/manifest.json` 中的 commit。这样可以区分“上游有更新”和“本插件已完成兼容”两个状态，
避免盲目覆盖本地适配层。

## 开发与检查

```bash
python -m compileall -q .
python -m pytest
ruff check .
ruff format --check .
```

项目只声明支持 `aiocqhttp`。OneBot 合并转发、文件与媒体发送均使用 AstrBot 当前公开组件，
必要时通过 `AiocqhttpMessageEvent` 调用协议端能力。

## 许可与声明

本项目以 MIT License 发布。上游和参考实现的许可、来源与用途说明见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。请遵守目标平台协议、版权要求及所在地法律法规。
