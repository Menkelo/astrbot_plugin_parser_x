# astrbot_plugin_parser_x

Parser X 是面向 AstrBot `aiocqhttp`（OneBot v11）的国内平台分享链接解析插件。项目以
[rconsole-plugin](https://github.com/zhiyu1998/rconsole-plugin) 的平台覆盖和交互习惯为功能基线，
使用原生 Python 解析器、AstrBot 消息链与 `yt-dlp` 兼容层实现，不依赖 Yunzai 运行时。

## 支持范围

### 原生解析器

- Bilibili：视频、分 P、动态和直播。视频直接发送源视频；图文动态单图引用原消息，多图将
  原图逐张放入合并转发；直播保持原生消息链。视频/动态正文解析兼容公共、已购及富文本
  自带表情。评论区按
  rconsole-plugin 的用户可见行为独立适配热门/置顶评论、富文本表情、粉丝牌、UP 标识、
  等级、IP 属地、互动数、评论配图和首条楼中楼；仅视频作品启用时使用独立评论图合并转发。
- 抖音：视频、图文和图集；短链跳转；Cookie 与 yt-dlp 兜底。视频直接发送；图文单图
  引用原图，多图逐张放入合并转发。配置 `douyin_ck` 后支持作品/评论
  表情、作者标识、评论图片、贴纸和首条楼中楼；评论区仅随视频作品发送。
- 快手：视频、图片和图文作品；视频直接发送，单图引用原消息，多图使用合并转发。
- 微博：视频微博发送源视频并可附带热门评论；无视频图文单图直接引用原图，多图逐张放入
  合并转发，纯文本微博发送正文。公开热门评论支持认证/VIP、原博标识、作者点赞和首条
  楼中楼；正文及评论中的微博表情均可正确解析。
- 小红书：视频、图片和图文笔记；视频直接发送，图文单图引用原消息，多图使用合并转发；
  发布时间兼容秒和毫秒时间戳，空正文不会回填分享链接，平台内部的 `[话题]` 标记会清理
  为标准 `#话题#`。配置完整网页 Cookie 后，视频笔记可附带热门评论、作者/置顶标识、
  IP 属地、点赞数、评论图片和首条楼中楼；图文笔记不会抓取或发送评论区。
- 米游社：文章正文、封面、正文配图和官方表情按富文本原始顺序合成为一张图片；含视频时可
  并发发送源视频与独立评论图，用户自定义评论表情会按图片渲染。
- 小黑盒：帖子概要、富文本正文、配图和平台表情合成为一张图片，不再拆分或重复发送节点；
  含视频时可并发发送源视频与独立评论图。客户端本地缓存路径会被过滤；
  公开内容会先尝试签名接口，失败后回退公开游戏接口或官方分享页，Cookie 仅用于受限内容。

### yt-dlp 兼容层

- AcFun
- 网易云音乐

视频兼容层直接发送源视频；网易云音乐发送独立音频文件。

插件不注册 TikTok、Twitter/X、Instagram、YouTube、Apple Music、Spotify 等国外平台路由，
也不注册微信视频号、QQ 音乐、酷狗音乐、汽水音乐和通用网页 AI 总结功能。
西瓜视频、皮皮虾、微视与贴吧同样不再注册，也不会保留配置开关或兼容层入口。

各平台均可在 AstrBot 插件配置页单独启停。完整的上游功能映射和未移植项见
[docs/UPSTREAM_COMPATIBILITY.md](docs/UPSTREAM_COMPATIBILITY.md)。

## 安装

在 AstrBot WebUI 的插件管理中使用本仓库地址安装：

```text
https://github.com/Menkelo/astrbot_plugin_parser_x
```

运行环境还应提供 `ffmpeg`，用于音视频合并、格式转换和 H.264 发送兜底。图文分离平台只发送
作品原图或视频；小黑盒与米游社将正文和配图合成为一张图片。独立评论区图片与这两个平台的
图文一体图片均使用 AstrBot 官方 `html_render`（Canvas/T2I）渲染。

## 使用

直接在 QQ 群聊或私聊发送支持平台的分享链接。插件不会处理 AstrBot 指令消息，也会忽略
非 B站平台的直播分享。

### 官方 Plugin Page 调试台

从 AstrBot WebUI 的插件详情页打开“解析调试台”即可直接测试，无需先开启独占模式。
调试台直接复用当前插件实例、平台配置、Cookie、下载器、解析器、评论 Canvas 和发送逻辑，
可粘贴原本要发到 QQ 的完整分享文本，并通过 SSE 查看真实完成顺序及 QQ 消息组件结构。

`debug.enabled` 默认关闭；关闭时调试台和 QQ 普通消息均可触发解析。开启后 Parser X 进入独占
调试模式，不接受 QQ 或其他消息适配器中的解析请求，只响应已登录 Dashboard 用户从插件 Page
发起的调试；关闭并完成插件热重载后，普通消息解析恢复。

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
- `cookies.xiaohongshu_cookie`：小红书视频评论区登录态，必须包含 `a1`，通常还需要
  `web_session`；建议使用专用小号。
- `cookies.ytdlp_cookie_file`：Netscape 格式 Cookie 文件，用于需要登录的平台。
- `cookies.xiaoheihe_cookie`：可选的小黑盒登录态；公开内容未配置时也会先尝试签名接口。
- `comments.bilibili`、`comments.douyin`、`comments.weibo`、`comments.xiaohongshu`、
  `comments.xiaoheihe`、`comments.miyoushe`：各平台视频评论区开关；所有平台的纯图文、图集和
  纯文字作品都不会抓取或发送评论，小红书评论默认关闭。
- `comments.display_count`：最多展示的热门评论总数。
- `comments.filter.*`：视频评论共享过滤。默认使用平衡 `@` 处理、二维码图片过滤、明显广告组合
  评分和重复评论去重；二维码检测失败时保留评论，低信息评论过滤默认关闭。
- `comments.timeout`：评论抓取、独立评论图渲染和缓存生成的总超时。
- `rendering.timeout`：单次 `html_render` 截图超时。
- `debug.enabled`：只响应调试页面；默认关闭，开启后普通消息不再触发解析。
- `behavior.show_download_fail_tip`：是否在聊天中提示下载失败或超限。
- `behavior.disabled_sessions`：已关闭解析的会话列表，通常通过命令自动维护。

启用评论区时，插件只为含视频的作品与主内容并发准备热门评论；纯图文、图集和纯文字作品不会
挂载评论任务，也不会请求或发送评论区。评论使用独立合并转发，第一节点为平台评论标题，后续
每张评论图各占一个节点；转发失败时降级逐条发送，评论抓取或渲染失败不会阻塞视频。B站、
抖音、小红书与微博纯图文单图直接引用原图，多图只发送原图合并转发；小黑盒与米游社使用
图文一体图片。评论渲染使用 AstrBot 官方 `html_render`。小红书视频评论使用完整登录态与
网页签名接口；Cookie、`xsec_token` 或签名不可用时自动跳过评论。

评论适配完成后会先经过统一过滤层，再截取 `comments.display_count`：平衡模式会清除单个
`@用户名` 并保留实际正文，纯召唤、多人群发、联系方式引流、明显广告、二维码配图和重复评论
会被移除。楼中楼单独判断，过滤楼中楼不会连带删除正常主评论；过滤后会继续使用后续候选评论
补足展示数量。二维码仅在本地扫描评论配图，不会扫描头像、封面或平台表情。

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
