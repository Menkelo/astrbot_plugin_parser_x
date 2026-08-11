# rconsole-plugin 兼容矩阵

基线：`zhiyu1998/rconsole-plugin@2f74647778d166b746ac3a72294888f2b7e38917`

状态含义：

- `原生`：AstrBot/Python 专用实现，具备平台定制逻辑。
- `兼容层`：由 yt-dlp 提取和下载，平台变更主要跟随 yt-dlp。
- `不适用`：属于 Yunzai 运维或宿主能力，不应照搬到 AstrBot。
- `待适配`：上游有功能，但 Parser X 尚未提供等价实现。

| 上游模块 | Parser X 状态 | 实现位置 | 说明 |
| --- | --- | --- | --- |
| Bilibili | 原生 | `core/parsers/bilibili/` | 视频、分P、动态、直播、评论卡片 |
| 抖音 | 原生 | `core/parsers/douyin/` | 视频、图文、图集、Cookie 与 yt-dlp 兜底 |
| 快手 | 原生 | `core/parsers/kuaishou.py` | 视频、图片、图文 |
| 微博 | 原生 | `core/parsers/weibo.py` | 视频、图片、纯文本卡片 |
| 小红书 | 原生 | `core/parsers/xiaohongshu.py` | 视频、图片、图文 |
| TikTok | 兼容层 | `core/parsers/ytdlp.py` | 可配置 Cookie 文件 |
| Twitter / X | 兼容层 | `core/parsers/ytdlp.py` | 登录限制由 Cookie/yt-dlp 决定 |
| Instagram | 兼容层 | `core/parsers/ytdlp.py` | 登录限制由 Cookie/yt-dlp 决定 |
| YouTube | 兼容层 | `core/parsers/ytdlp.py` | 默认不处理播放列表，只取单项 |
| AcFun | 兼容层 | `core/parsers/ytdlp.py` | 单视频 |
| 西瓜视频 | 兼容层 | `core/parsers/ytdlp.py` | 单视频 |
| 皮皮虾 | 兼容层 | `core/parsers/ytdlp.py` | 取决于 yt-dlp extractor 可用性 |
| 微视 | 兼容层 | `core/parsers/ytdlp.py` | 取决于 yt-dlp extractor 可用性 |
| 网易云音乐 | 兼容层 | `core/parsers/ytdlp.py` | 发送音频文件 |
| 汽水音乐 | 兼容层 | `core/parsers/ytdlp.py` | 发送音频文件 |
| QQ音乐 / 酷狗音乐 | 待适配 | - | 上游使用定制 API、Cookie 与解密流程 |
| 米游社 / 贴吧 / 小黑盒 | 待适配 | - | 需要文章/卡片类原生解析器 |
| 微信视频号 | 待适配 | - | 上游依赖元宝 Cookie 与专用会话接口 |
| Apple Music / Spotify | 待适配 | - | 上游依赖 freyr；DRM 内容不直接下载 |
| 通用网页 AI 总结 | 待适配 | - | 后续应接 AstrBot Provider API，而不是复制上游 OpenAI 客户端 |
| 点歌、云盘上传、扫码登录 | 不适用 | - | 强依赖 Yunzai 群文件、Redis 与管理员模型 |
| 插件自更新 | 不适用 | - | 由 AstrBot 插件管理器负责 |
| Redis 信任用户/海外开关 | 不适用 | - | 改用 AstrBot 配置与会话开关 |

## OneBot v11 差异

- 合并转发使用 AstrBot `Nodes`/`Node` 组件，由 aiocqhttp 适配器转换为
  `send_group_forward_msg` 或 `send_private_forward_msg`。
- 本地文件与媒体通过 AstrBot `File`、`Image`、`Video` 组件发送。
- JSON 分享卡从 `Json` 消息段递归提取 URL。
- 不支持的平台不会注册处理器，插件元数据也只声明 `aiocqhttp`。
