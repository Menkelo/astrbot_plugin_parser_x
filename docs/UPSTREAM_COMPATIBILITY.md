# rconsole-plugin 兼容矩阵

基线：`zhiyu1998/rconsole-plugin@2f74647778d166b746ac3a72294888f2b7e38917`

状态含义：

- `原生`：AstrBot/Python 专用实现，具备平台定制逻辑。
- `兼容层`：由 yt-dlp 提取和下载，平台变更主要跟随 yt-dlp。
- `不适用`：属于 Yunzai 运维或宿主能力，不应照搬到 AstrBot。
- `待适配`：上游有功能，但 Parser X 尚未提供等价实现。

| 上游模块 | Parser X 状态 | 实现位置 | 说明 |
| --- | --- | --- | --- |
| Bilibili | 原生 | `core/parsers/bilibili/` | 视频、分P、动态、直播；`comment_feed.py`/`comment_canvas.py` 对照上游 `utils/bili-comment.js` 与评论模板做独立语义适配，支持分页候选补足并将选中评论渲染为单张自适应长图，优先使用 AstrBot Canvas |
| 抖音 | 原生 | `core/parsers/douyin/` | 视频、图文、图集、Cookie 与 yt-dlp 兜底；`comment_feed.py` 对照上游 `utils/douyin-comment.js` 适配 Cookie 评论接口、A-Bogus、表情、图片、贴纸、作者标识与楼中楼 |
| 快手 | 原生 | `core/parsers/kuaishou.py` | 视频、图片、图文 |
| 微博 | 原生 | `core/parsers/weibo.py`、`core/parsers/weibo_comment.py` | 正文、图片、视频按原生消息链分开发送，单图引用原消息；使用公开 `m.weibo.cn/comments/hotflow` 生成热门评论 Canvas 卡片，支持认证/VIP、微博表情、原博与楼中楼 |
| 小红书 | 原生 | `core/parsers/xiaohongshu.py` | 视频、图片、图文 |
| AcFun | 兼容层 | `core/parsers/ytdlp.py` | 单视频 |
| 西瓜视频 | 兼容层 | `core/parsers/ytdlp.py` | 单视频 |
| 皮皮虾 | 兼容层 | `core/parsers/ytdlp.py` | 取决于 yt-dlp extractor 可用性 |
| 微视 | 兼容层 | `core/parsers/ytdlp.py` | 取决于 yt-dlp extractor 可用性 |
| 网易云音乐 | 兼容层 | `core/parsers/ytdlp.py` | 发送音频文件 |
| 米游社 | 原生 | `core/parsers/miyoushe.py`、`core/parsers/miyoushe_comment.py` | 正文、图片和视频按原生消息链分开发送；公开 `getPostReplies` 热门评论区支持认证、等级、配图与楼中楼 |
| 小黑盒 | 原生 | `core/parsers/xiaoheihe.py`、`core/parsers/xiaoheihe_comment.py` | 帖子富文本按图文顺序转发，游戏详情与截图分层发送，视频独立发送；无 Cookie 也先尝试签名接口并读取帖子评论 |
| 贴吧、微信视频号、QQ音乐、酷狗音乐、汽水音乐、通用网页 AI 总结 | 已移除 | - | 不注册路由、配置项或命令；贴吧官方页面不稳定且正文依赖非官方服务，因此不保留虚假可用入口 |
| 点歌、云盘上传、扫码登录 | 不适用 | - | 强依赖 Yunzai 群文件、Redis 与管理员模型 |
| 插件自更新 | 不适用 | - | 由 AstrBot 插件管理器负责 |
| Redis 信任用户/海外开关 | 不适用 | - | 改用 AstrBot 配置与会话开关 |

## 评论区范围

- 已适配：B站视频、抖音作品（需要 `douyin_ck`）、微博、小黑盒帖子、米游社文章。
- 暂不加入：快手、小红书等平台的评论接口依赖频繁变化的私有签名和完整登录态；当前没有
  足够稳定、可真实验证的公开接口，因此不使用易碎网页抓取充数。
- 五个平台共享评论数量与超时配置；选中的全部评论始终合并为一张自适应高度 Canvas 长图，
  不按条数或预估内容高度拆图。

## 地区范围

按项目当前范围，TikTok、Twitter/X、Instagram、YouTube、Apple Music、Spotify 等国外平台已完全移除：没有解析器、配置开关或 URL 路由。上游后续新增国外平台时也不会自动纳入同步范围。

## OneBot v11 差异

- 合并转发使用 AstrBot `Nodes`/`Node` 组件，由 aiocqhttp 适配器转换为
  `send_group_forward_msg` 或 `send_private_forward_msg`。
- 本地文件与媒体通过 AstrBot `File`、`Image`、`Video` 组件发送。
- JSON 分享卡从 `Json` 消息段递归提取 URL。
- 不支持的平台不会注册处理器，插件元数据也只声明 `aiocqhttp`。
