# Parser X · astrbot_plugin_parser_x

面向 AstrBot **`aiocqhttp`（OneBot v11）** 的国内平台分享链接解析插件。只声明支持此适配器；调试页不是其他消息适配器的支持承诺。不依赖 Yunzai，也不需要外部截图服务。

## 支持范围

固定注册以下 **7 个平台**，不提供逐平台启停开关：

| 平台 | 范围与限制 |
| --- | --- |
| Bilibili | 视频、分 P、动态；视频动态可转入原视频解析；支持热门评论与首条可用楼中楼。 |
| 抖音 | 视频、图文、图集及实况片段；作品/评论的可用性取决于登录态和平台风控。 |
| 快手 | 视频、图片、混合媒体；视频评论可匿名尝试或填写 Cookie，但匿名曾返回 `result=2`，不承诺匿名或登录后必定成功。 |
| 微博 | 文字、图片、视频和实况混合媒体；当前热门评论需有效 Cookie，公开作品可匿名尝试。 |
| 小红书 | 视频、图文及实况笔记；评论由统一总开关与有效 Cookie 共同控制，详见下方账号风险。 |
| 小黑盒（xiaoheihe） | 帖子、正文、图片和可识别视频，接入统一卡片与评论流程；接口可能要求 Cookie 或返回 `show_captcha`，不能绕过验证码。 |
| AcFun | 视频通过 yt-dlp 兼容层解析，评论走平台接口；Cookie 可选，匿名不保证成功。 |

只处理支持的作品链接；跳过直播及已识别的非作品页面，不注册国外平台、音乐平台或通用网页 AI 总结路由。

媒体按作品顺序发送，多媒体使用 OneBot 合并转发，必要时逐项降级；实况动态部分以短视频发送，不会恢复手机相册的原生 Live Photo。单项失败不应阻断后续媒体。卡片和评论使用统一 HTML/CSS + 本地 Playwright Chromium 渲染；评论失败不影响原始媒体。具体卡片组合随作品类型决定，不保证每个作品都附带正文卡或评论。

## 安装

推荐在 **AstrBot WebUI → 插件管理** 使用仓库地址安装：

```text
https://github.com/Menkelo/astrbot_plugin_parser_x
```

运行环境必须另行准备 **ffmpeg、Python Playwright 包、与该包版本匹配的 Chromium 和浏览器系统库**。Python 依赖包含 `playwright>=1.58,<2`（当前验证运行版本为 1.58）；安装 Python 包并不等于安装浏览器。**Chromium 二进制及系统库不随插件或 wheel 捆绑**，插件运行时也不会自动下载浏览器、提权或安装系统依赖。

在运行 AstrBot 的同一 Python 环境、同一容器/用户下手动执行：

```bash
python -m playwright install chromium
```

Linux 系统库的特权安装、持久缓存和验证步骤见 [安装指南](docs/INSTALL.md)。字体资源随插件提供于 `assets/fonts/`，内联到渲染页面；它们不能替代 Chromium 所需系统库。

wheel 保留完整的 `astrbot_plugin_parser_x` 包命名空间和运行资源，依赖元数据直接来自 `requirements.txt`。但 **`pip install` wheel 不会向 AstrBot 注册插件，也不是 WebUI 安装流程的替代品**；仍须由 AstrBot 插件管理器发现并加载插件。ffmpeg/Chromium 也不会被 wheel 安装。

## 使用与精简配置

在 QQ 群聊或私聊发送作品分享链接。会话管理命令：`/开启解析`、`/关闭解析`；会话关闭记录使用完整 UMO，不是只有群号。

当前用户配置仅包括：

- `comments.enabled`：统一卡片/热门评论开关，默认 `true`。关闭时不生成、复用或发送生成卡片；原始媒体和原生正文仍按发送流程处理。
- `comments.display_count`：最多展示的主评论数，默认 **10**，范围 **1–20**；首条楼中楼不计入主评论数量。
- `performance.video_codec`：B站编码偏好，`auto` / `hevc` / `avc`，默认 `hevc`。
- `cookies.*`：7 个平台的原始 Cookie，见下节。
- `behavior.disabled_sessions`：关闭解析的会话列表，通常由命令维护。

`debug.enabled` 由插件 Page 调试台管理，默认关闭且不在普通配置页展示。数据目录、缓存目录和迁移标记为内部字段，不应手改。

### 内嵌运行策略（不是可恢复的配置开关）

- 全部 7 个平台注册；下载并发 **9**，单资源上限 **100 MB**；下载失败提示始终开启。
- 单次截图超时 **45 秒**，评论获取/渲染总超时 **90 秒**。
- 成功解析结果内存缓存开启，TTL **300 秒**，最多 **128** 条；命中不续期，重载/重启清空。
- 评论过滤固定开启：平衡处理 `@`、二维码配图过滤、广告组合评分（阈值 **6**）及重复去重；低信息过滤关闭。二维码检测失败保留评论，楼中楼单独过滤。
- 磁盘缓存位于 AstrBot 数据目录下的 `plugin_data/astrbot_plugin_parser_x/`；每天按 AstrBot 时区（未配置时 Asia/Shanghai）00:00 清理超过 **24 小时**未更新的缓存文件。

旧 `platforms`、`rendering`、`result_cache`、逐平台评论/过滤/超时等设置已退出配置面；迁移会移除这些旧字段而非保留隐藏开关，保留平台 Cookie，并合并旧的会话禁用列表。旧全局 yt-dlp Cookie 文件上传字段只从配置中移除，不读取或删除旧上传文件。不要把旧文档中的键重新加回配置。

### 登录态与账号风险

使用浏览器请求中的原始 Cookie 字符串，例如 `key=value; key2=value2`，不要包含 `Cookie:` 前缀、JSON 或文件路径。字段为：

- `cookies.bili_ck`
- `cookies.douyin_ck`
- `cookies.weibo_cookie`
- `cookies.xiaohongshu_cookie`
- `cookies.xiaoheihe_cookie`
- `cookies.kuaishou_cookie`
- `cookies.acfun_cookie`

**小红书没有独立评论开关，也不是“评论功能已移除”或“默认独立关闭”。** 当前 `comments.enabled=true` 且填写有效 `cookies.xiaohongshu_cookie` 后，对满足条件的作品会尝试评论请求；缺少登录态或必要作品参数时跳过。**登录账号可能触发风控、验证、限制甚至封禁，没有“不封号”保证**。建议使用专用小号；不接受风险就不要填写小红书 Cookie，或关闭统一总开关（会影响所有平台卡片/评论）。

微博评论同样需有效 Cookie；抖音等平台也会受权限和风控约束。快手 Cookie 仅用于其评论接口，不发往通用图片/CDN。AcFun Cookie 用于解析、下载、页面元数据及评论接口；下载时在插件缓存内生成 `.acfun.cn` 域限定、权限 0600 的 Netscape Cookie 文件。Cookie 不能绕过付费、地区、权限、验证码或风控，失效后需手动更新。

**配置、缓存及任何导出的诊断资料都可能含敏感登录态或作品内容，勿公开分享。**

## Plugin Page 调试台

从 AstrBot WebUI 插件详情打开“解析调试台”。它复用当前插件实例、Cookie、解析/下载/渲染及发送流程，并通过 SSE 展示真实任务顺序和消息组件。

普通模式下 QQ 与调试台均可解析；开启“仅调试”后，只接受已登录 Dashboard 用户的 Page 请求，QQ 分享不再触发解析。关闭并完成插件重载后恢复。

## 缓存与失败处理

相同链接、匹配信息及分享文本可复用已完成结果与媒体；同时到达的相同请求合并解析，但分别回复。首次请求仍边准备边发送。缓存键保留查询参数，避免混用 B站分 P、小红书 token 和附文；不同长短链接不保证自动归并。

解析失败和主动跳过不缓存；下载失败、取消、媒体丢失或为空会淘汰结果。配置及 Cookie 变化刷新结果缓存。生成卡片只复用磁盘非空 PNG，卡片路径包括输入、配置与渲染版本；命中后不再抓评论。没有评论的临时正文兜底不会写入稳定评论缓存。每次发送使用结果副本，评论任务和已构造的消息不放入解析结果缓存；卸载会关闭任务和浏览器。

普通 Unicode Emoji 使用本地字体，不请求 Twemoji CDN；平台自定义表情仍可能需要平台目录与媒体下载，不等于整个解析流程离线。

## 开发与许可

依赖与构建验证见 [安装指南](docs/INSTALL.md)。测试应在隔离环境中运行，不应以生产插件目录为测试包。

```bash
python -m pytest
ruff check .
```

项目以 MIT License 发布；第三方来源和许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。请遵守目标平台协议、版权要求及所在地法律法规。
