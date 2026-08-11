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
| AcFun | 兼容层 | `core/parsers/ytdlp.py` | 单视频 |
| 西瓜视频 | 兼容层 | `core/parsers/ytdlp.py` | 单视频 |
| 皮皮虾 | 兼容层 | `core/parsers/ytdlp.py` | 取决于 yt-dlp extractor 可用性 |
| 微视 | 兼容层 | `core/parsers/ytdlp.py` | 取决于 yt-dlp extractor 可用性 |
| 网易云音乐 | 兼容层 | `core/parsers/ytdlp.py` | 发送音频文件 |
| 汽水音乐 | 兼容层 | `core/parsers/ytdlp.py` | 发送音频文件 |
| QQ音乐 | 原生 | `core/parsers/music.py` | 官方接口解析歌曲、歌手、专辑和封面；不绕过版权取流 |
| 酷狗音乐 | 原生 | `core/parsers/music.py` | 官方元数据；可选对接自建 KuGouMusicApi 音源 |
| 米游社 | 原生 | `core/parsers/miyoushe.py` | 正文、图片、视频和作者信息 |
| 贴吧 | 原生 | `core/parsers/tieba.py` | 默认官方页面元数据；可选详情 API 获取楼主正文与媒体 |
| 小黑盒 | 原生 | `core/parsers/xiaoheihe.py` | 帖子/游戏卡片；Cookie 可用时调用签名接口 |
| 微信视频号 | 原生 | `core/parsers/weixin_channel.py` | 复用上游两阶段流程；需元宝 Cookie，默认关闭 |
| 通用网页 AI 总结 | 原生命令 | `core/web_summary.py`、`main.py` | `/链接总结` 调用当前 AstrBot Provider；含 SSRF、大小和重定向防护 |
| 点歌、云盘上传、扫码登录 | 不适用 | - | 强依赖 Yunzai 群文件、Redis 与管理员模型 |
| 插件自更新 | 不适用 | - | 由 AstrBot 插件管理器负责 |
| Redis 信任用户/海外开关 | 不适用 | - | 改用 AstrBot 配置与会话开关 |

## 地区范围

按项目当前范围，TikTok、Twitter/X、Instagram、YouTube、Apple Music、Spotify 等国外平台已完全移除：没有解析器、配置开关或 URL 路由。上游后续新增国外平台时也不会自动纳入同步范围。

## OneBot v11 差异

- 合并转发使用 AstrBot `Nodes`/`Node` 组件，由 aiocqhttp 适配器转换为
  `send_group_forward_msg` 或 `send_private_forward_msg`。
- 本地文件与媒体通过 AstrBot `File`、`Image`、`Video` 组件发送。
- JSON 分享卡从 `Json` 消息段递归提取 URL。
- 不支持的平台不会注册处理器，插件元数据也只声明 `aiocqhttp`。
