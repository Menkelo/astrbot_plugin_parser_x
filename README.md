# astrbot_plugin_parser_x

Parser X 是面向 AstrBot `aiocqhttp`（OneBot v11）的国内平台分享链接解析插件。项目以
[rconsole-plugin](https://github.com/zhiyu1998/rconsole-plugin) 的平台覆盖和交互习惯为功能基线，
使用原生 Python 解析器、AstrBot 消息链与 `yt-dlp` 兼容层实现，不依赖 Yunzai 运行时。

## 支持范围

### 原生解析器

- Bilibili：视频、分 P 及所有动态使用统一纵向长卡；单图动态会把原图直接放进引用原消息的
  长卡且不再重复发送，转发动态会补回原动态正文与图片；直播保持原生消息链。所有
  HTML 卡片统一使用 AstrBot 官方 `html_render`（Canvas/T2I）渲染。
  视频长卡与视频文件分开发送；视频/动态正文支持公共、已购及富文本自带表情。评论区按
  rconsole-plugin 的用户可见行为独立适配热门/置顶评论、富文本表情、粉丝牌、UP 标识、
  等级、IP 属地、互动数、评论配图和首条楼中楼；启用时使用独立评论图合并转发。
- 抖音：视频、图文和图集；短链跳转；Cookie 与 yt-dlp 兜底；视频和图文均使用统一长卡，
  视频文件独立发送。配置 `douyin_ck` 后支持作品/评论表情、作者标识、评论图片、贴纸和首条楼中楼，
  评论使用独立评论图合并转发。
- 快手：视频、图片和图文作品均使用统一长卡，视频文件独立发送。
- 微博：完整正文使用统一长卡；单图只在长卡内显示，多图保留独立批次，正文卡和视频按完成
  顺序发送。公开热门评论支持认证/VIP、原博标识、作者点赞和首条楼中楼，并作为独立评论图
  合并转发；正文及评论中的微博表情均按图片渲染。
- 小红书：视频、图片和图文笔记均使用统一长卡，单图只在卡片内显示，视频文件独立发送，
  发布时间兼容秒和毫秒时间戳；空正文不会回填分享链接，平台内部的 `[话题]` 标记会清理为标准 `#话题#`。
- 米游社：文章使用统一长卡；单图只在卡片内显示，多图保留原生批次，正文卡和视频按完成顺序
  发送；公开热门评论使用独立评论图合并转发，官方表情和用户自定义评论表情会按图片渲染。
- 小黑盒：帖子与游戏使用统一长卡；长卡单独引用原消息，富文本仍按原图文顺序转发，正文卡
  和视频按完成顺序发送，帖子评论使用独立评论图合并转发；官方表情会在长卡、富文本节点和
  评论图中显示，客户端本地缓存路径会被过滤；
  公开内容会先尝试签名接口，失败后回退公开游戏接口或官方分享页，Cookie 仅用于受限内容。

### yt-dlp 兼容层

- AcFun
- 西瓜视频
- 皮皮虾
- 微视
- 网易云音乐

视频兼容层会并发准备统一长卡和独立视频文件，谁先完成就先发送；网易云音乐会发送统一长卡
和独立音频文件。

插件不注册 TikTok、Twitter/X、Instagram、YouTube、Apple Music、Spotify 等国外平台路由，
也不注册微信视频号、QQ 音乐、酷狗音乐、汽水音乐和通用网页 AI 总结功能。
贴吧同样不再注册：官方帖子页目前只稳定返回小程序壳，完整正文依赖非官方服务，无法作为
可靠的平台支持保留。

各平台均可在 AstrBot 插件配置页单独启停。完整的上游功能映射和未移植项见
[docs/UPSTREAM_COMPATIBILITY.md](docs/UPSTREAM_COMPATIBILITY.md)。

## 安装

在 AstrBot WebUI 的插件管理中使用本仓库地址安装：

```text
https://github.com/Menkelo/astrbot_plugin_parser_x
```

运行环境还应提供 `ffmpeg`，用于音视频合并、格式转换和 H.264 发送兜底。统一长卡依次展示
固定平台代表色纯色顶栏、主视觉、真实互动数据、作者、正文、作品信息和
`Menkelo/astrbot_plugin_parser_x` 页脚；顶栏不再受作品图片颜色影响，也不再使用磨砂、颗粒、
模糊或 B/米/微等字母和单字平台标记。直播继续使用原生消息链；视频作品的正文卡与原生视频
文件并发准备，完成者先发送。

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
- `cookies.weibo_cookie`：可选微博登录态；公开热门评论通常无需配置。
- `cookies.ytdlp_cookie_file`：Netscape 格式 Cookie 文件，用于需要登录的平台。
- `cookies.xiaoheihe_cookie`：可选的小黑盒登录态；公开内容未配置时也会先尝试签名接口。
- `comments.bilibili`、`comments.douyin`、`comments.weibo`、`comments.xiaoheihe`、
  `comments.miyoushe`：各平台评论区开关。
- `comments.display_count`：最多展示的热门评论总数。
- `comments.timeout`：评论抓取、独立评论图渲染和缓存生成的总超时。
- `rendering.timeout`：单次 `html_render` 截图超时。
- `behavior.show_download_fail_tip`：是否在聊天中提示下载失败或超限。
- `behavior.disabled_sessions`：已关闭解析的会话列表，通常通过命令自动维护。

启用评论区时，插件会与主内容同时准备热门评论，但不会再把评论塞进正文卡。主内容发送完成后，
评论区以独立图片放入一条合并转发：第一节点为平台评论标题，后续每张评论图各占一个节点；
合并转发失败时降级为标题和评论图逐条发送。评论抓取或渲染失败不会阻塞正文卡和媒体。
统一长卡始终作为引用原消息的独立消息；单图作品由卡片主视觉完整承载且不再重复发送，多图和
富文本继续使用独立批次。视频作品的正文卡和视频文件按实际完成顺序发送。
所有渲染均使用 AstrBot 官方 `html_render`。快手和小红书评论接口依赖频繁变化的私有签名与
完整登录态，因此没有用易碎网页抓取方式勉强加入。

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
