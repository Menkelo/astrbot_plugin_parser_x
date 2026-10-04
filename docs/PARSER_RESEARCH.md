# Parser X 同类项目调研与改进建议

调研日期：2026-10-04。通过 GitHub 搜索筛选项目，并阅读下列版本的 README 和相关源码。
这里确认的是设计与代码实现，没有使用真实平台账号逐一验证各项目的在线解析成功率。

本次 0.7.0 实现小黑盒、米游社移除及旧配置清理。下列建议是后续路线，不代表已经加入这些功能。

## 值得关注的四个项目

| 项目 | 为什么值得看 | 适合 Parser X 借鉴什么 |
| --- | --- | --- |
| [Zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser) | 同为 AstrBot 插件，可直接比较平台适配和消息发送；MIT | 平台解析器示例、集中配置访问、模块边界。Parser X 已有类似 BaseParser 和下载器，无需整套替换 |
| [fllesser/nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser) | NoneBot 社交分享解析项目；MIT | 解析结果缓存：同一链接再次分享时复用已完成结果，减少重复请求 |
| [sokoko-org/nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite) | 富文本、评论、主题和媒体失败回退较完整；MIT | 只回退上传失败的消息分包，并保留文字和来源；主题文件与解析逻辑分离 |
| [drdon1234/astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser) | 同为 AstrBot 插件，部署与发送方案更丰富；AGPL-3.0 | 借助宿主文件 Token 服务生成有时效的媒体 URL，支持机器人与协议端分机部署 |

## 建议优先做的改进

### 1. 重复分享更快：短期结果缓存和并发请求合并

**使用场景：** 两个人接连分享同一个视频，或多个群同时转发同一作品。目前 Parser X 会再次进入
平台解析流程；虽然下载器已有文件缓存和 yt-dlp 元信息缓存，原生平台接口仍可能重复请求。

参考项目在成功发送后缓存最多 50 个解析结果。适合吸收的是“缓存成功结果”的思路；Parser X
可进一步把相同作品正在进行的解析合为一次，再分别向各个请求会话发送。

建议先实现有容量上限、短 TTL 的内存缓存，不引入 Redis。缓存应区分平台、作品 ID、B站分 P、
有效登录态和影响结果的设置；小红书不能丢失访问所需的 `xsec_token`。评论缓存应有更短寿命。
不要直接长期复用含旧异步任务、失效媒体 URL 或已清理文件的 ParseResult。

**验收：** 同时请求同一作品只解析一次，各会话都收到内容；失败后可立即重试；更换 Cookie、
缓存过期或文件被清理后会重新解析；不同分 P 不会互相串结果。

来源：[成功发送后的有界结果缓存](https://github.com/fllesser/nonebot-plugin-parser/blob/4aa5364432193db701d71fad9c71ae768c951490/src/nonebot_plugin_parser/matchers/__init__.py#L55)。
TTL、并发合并与上述缓存隔离是对 Parser X 的建议扩展，参考项目未必已实现。

### 2. 发不出视频时，至少留下可用的标题和原链接

**使用场景：** 视频已解析成功，但 QQ 协议端拒绝上传，整条内容最后没有送达。

Parser X 已有视频兼容处理、文件发送及合并转发拆开发送等回退。可以补齐最终失败时的统一
“标题 + 作者 + 原始作品链接 + 简短原因”，并且只补发失败的部分。

参考项目在发送前保留消息快照，明确识别媒体上传错误，依次尝试省略视频和纯文字回退。
普通超时不一定代表未发送，应避免盲目重发导致刷屏。建议先统一失败分类和发送结果记录，
再接到现有发送函数中。

**验收：** 已成功发送的图片不重复；上传失败仍能看到来源；评论失败不拖累视频；普通超时
不会无限重试；关闭失败提示时仍遵守用户设置。

来源：[错误识别与分包回退实现](https://github.com/sokoko-org/nonebot-plugin-parser-lite/blob/aee45675f7ecc3cb823160dcede04a4c2cef28d8/src/nonebot_plugin_parser_lite/delivery.py)。

### 3. 分机部署时，增加可选的媒体中转

**使用场景：** AstrBot 和 NapCat 等协议端位于不同容器或服务器，协议端无法读取下载文件路径。

参考项目将本地媒体注册到 AstrBot 的文件 Token 服务，得到临时 URL 再发送。Parser X 当前
没有显式的媒体中转开关；现有调试页媒体接口面向已登录 Dashboard 用户，不能直接拿来当 QQ
协议端的下载地址。

建议优先复用宿主提供的服务，默认关闭，配置可达的回调地址和有效期。只暴露本次发送所需
的文件；失效或不可达时给出明确提示。是否需要此功能取决于实际部署：同机共享目录用户收益较小。

**验收：** 在不共享磁盘的两端完成图片、视频、音频发送；链接过期失效；中转失败时提示清晰。

来源：[文件 Token 服务集成](https://github.com/drdon1234/astrbot_plugin_media_parser/blob/5265ff57bf83caa7f5d6a94e7197d0dbd5f6dbf7/core/storage/file_token.py)。

## 后续可做，优先级较低

- **主题模板独立维护：** Parser X 已有统一配色、正文卡和评论 Canvas，可在其上增加少量模板
  选择和版本化缓存。当前多数作品直接发送媒体，因此主题主要服务评论图和 B站动态。
  参考 [主题指南](https://github.com/sokoko-org/nonebot-plugin-parser-lite/blob/aee45675f7ecc3cb823160dcede04a4c2cef28d8/THEME.md)
  与 [模板选择实现](https://github.com/sokoko-org/nonebot-plugin-parser-lite/blob/aee45675f7ecc3cb823160dcede04a4c2cef28d8/src/nonebot_plugin_parser_lite/render/theme.py)。
- **维护成本更低：** 保留现有 BaseParser，参考 [解析器示例](https://github.com/Zhalslar/astrbot_plugin_parser/blob/4d8bf1cb03e06dd183c8e3f65af1acceff2c1eb1/core/parsers/example.py)
  与 [集中配置访问](https://github.com/Zhalslar/astrbot_plugin_parser/blob/4d8bf1cb03e06dd183c8e3f65af1acceff2c1eb1/core/config.py)，
  逐步统一各平台输入、输出和异常约定。Parser X 已有大量回归测试，下一步更适合补充脱敏的真实
  响应样本和异常样本，让接口变更更容易定位。
- **调试台显示平台健康状况：** 利用已有 SSE 调试台，汇总最近成功时间、失败阶段和耗时，
  帮助区分接口失效、下载失败与 QQ 上传失败。这是结合现有代码提出的改进，不是已核实的参考项目功能。

## 已有能力与范围选择

Parser X 已有原生解析器和受限 yt-dlp 兼容层、下载并发与体积限制、多种下载回退、评论超时隔离、
评论过滤、分享卡提取以及调试页。这些应继续保留，不必重复引入其他项目的整套基础设施。

建议先完善缓存与发送失败体验，再按部署需求考虑中转，最后做主题。当前以七个保留平台为范围，
不因参考项目支持更多平台就重新加入已移除的平台或评论接口。

本次只做设计调研，没有复制四个项目的代码。若后续直接使用 MIT 项目的实现，需要保留其许可与
版权声明；媒体解析项目采用 AGPL-3.0，不能把其代码直接拷入本项目后仍按纯 MIT 发布，应单独
评估许可兼容性。本次使用的是功能思路，不新增第三方代码依赖。
