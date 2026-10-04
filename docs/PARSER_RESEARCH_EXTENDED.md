# Parser X 扩展调研：下载器、官网文档与 Codeberg

调研日期：2026-10-04 至 2026-10-05。范围排除上一轮四个 AstrBot / NoneBot 项目。
除了 GitHub，也查看了独立产品官网、项目文档站与 Codeberg 页面；推荐依据是能核实的实现和
适用性，不是 Star 数。没有逐一部署这些项目，也没有测试它们对当前各平台的在线解析成功率。

## 五个值得关注的项目

### 1. gallery-dl：参考下载完成判定

- [项目](https://github.com/mikf/gallery-dl) · [文档站](https://gdl-org.github.io/docs/)
- 定位：图片、图集及媒体集合下载工具。读取源码版本 `6f7e9c62a56fcc48f76e3c3cc8fa238e42cf25b5`。
- 可核实实现：默认启用 `.part` 临时文件，下载完成后再将文件移动到最终路径；提供下载归档，
  记录资源身份，避免重复下载。
- 对 Parser X 的价值：下一步可让通用下载器统一使用临时文件、长度检查和完成后替换，避免
  另一个请求把正在写入的文件误认为已经完成。新结果缓存已会淘汰失败任务和失效文件，但下载器
  原有的文件命中规则仍值得独立加强。
- 不能直接照搬：下载归档里的“已经下载就跳过”不等于聊天里“不再回复”。不同群或不同消息仍要
  收到结果，本次缓存已经保留分别回复的行为。
- 证据：[临时文件开关](https://github.com/mikf/gallery-dl/blob/6f7e9c62a56fcc48f76e3c3cc8fa238e42cf25b5/gallery_dl/downloader/common.py#L30)、
  [临时文件与完成处理](https://github.com/mikf/gallery-dl/blob/6f7e9c62a56fcc48f76e3c3cc8fa238e42cf25b5/gallery_dl/path.py#L350)。源码相关文件采用 GPL-2.0。

### 2. cobalt：参考清晰的结果和错误分类

- [产品官网](https://cobalt.tools/about/general) · [源码与文档](https://github.com/imputnet/cobalt)
- 定位：面向用户的媒体保存工具与可自托管的处理 API。读取版本 `a636575b09de1fc55d9b8cd98cac88f5f2f16b42`。
- 可核实实现：接口区分 `redirect`、`tunnel`、`local-processing`、`picker`、`error`；错误提供
  机器可识别的 code 和上下文，输入选项由 schema 校验。
- 对 Parser X 的价值：统一“内容不存在、需要登录、限流、下载失败、发送失败”等错误类型，让
  调试页和聊天提示使用同一份错误信息。多媒体结果可明确区分图集、音频与待合并的音视频。
- 边界：官网 API 文档明确公共实例不面向未经许可的第三方调用；若将来实际接入，应自托管或
  获得实例所有者授权。本次只参考设计，没有调用其解析接口或引入服务依赖。
- 证据：[响应与错误结构](https://github.com/imputnet/cobalt/blob/a636575b09de1fc55d9b8cd98cac88f5f2f16b42/docs/api.md)、
  [输入校验](https://github.com/imputnet/cobalt/blob/a636575b09de1fc55d9b8cd98cac88f5f2f16b42/api/src/processing/schema.js)。仓库许可证 AGPL-3.0。

### 3. Streamlink：参考接口变化后的早期校验

- [官方文档](https://streamlink.github.io/) · [响应校验指南](https://streamlink.github.io/api_guide/validate.html)
- 定位：直播流提取工具及 Python API。此次阅读文档版本 8.6.1；仓库未归档。
- 可核实设计：用声明式规则验证并提取 HTTP 响应中的字段，结构不符合预期时给出明确的提取错误；
  缓存 API 支持过期时间和过期项清理。
- 对 Parser X 的价值：每个平台在提取视频、图片前，先明确验证核心字段。平台改接口后，可以提示
  “作品数据结构已变化”，而不是一路产生难以定位的 KeyError 或空列表。现有 B站错误码映射可保留，
  再逐步完善其他平台。
- 边界：它的重点是直播。这里借鉴数据校验方法，不代表建议把 Parser X 扩展为全平台直播录制器。
- 补充证据：[缓存 API](https://streamlink.github.io/api/cache.html)。仓库许可证 BSD-2-Clause。

### 4. RSSHub：参考缓存分层和过期策略

- [项目](https://github.com/DIYgod/RSSHub) · [官方文档入口](https://docs.rsshub.app/)
- 定位：将不同网站内容转为统一订阅源。读取源码版本 `ad611a1cc1d205c9a67f68c0bff1f4b5c8314da5`。
- 可核实实现：缓存访问与存储后端分离；内存实现使用 LRU 和 TTL，`tryGet` 在取值函数成功返回后
  写入结果；另有针对并发占用的原子 claim 能力。
- 对 Parser X 的价值：将缓存集中在一个模块，平台实现不必各写一套。0.8.0 已采用单独缓存模块、
  成功结果复用、固定 TTL、容量限制和进行中请求合并。这里是设计层面的参考，没有复制其 TypeScript。
- 有意保留的区别：RSSHub 部分读操作可刷新 TTL；Parser X 的命中不延长寿命，以减少长期重复分享
  导致作品元数据一直不更新的情况。当前单进程缓存足够，不需要引入 Redis。
- 证据：[缓存抽象与 tryGet](https://github.com/DIYgod/RSSHub/blob/ad611a1cc1d205c9a67f68c0bff1f4b5c8314da5/lib/utils/cache/index.ts)、
  [内存实现](https://github.com/DIYgod/RSSHub/blob/ad611a1cc1d205c9a67f68c0bff1f4b5c8314da5/lib/utils/cache/memory.ts)。
  官方文档站此次请求返回 403，所以这些判断依据源码。仓库许可证 AGPL-3.0。

### 5. Lux：参考媒体候选的统一结构与断点续传

- [项目](https://github.com/iawia002/lux)
- 定位：Go 编写的多站点视频下载库与命令行工具。读取版本 `dd00f6d258d80b6684a0b9402d7124e5c18ef42f`。
- 可核实实现：作品、清晰度、分段、体积分别由 Data / Stream / Part 表达；下载器使用临时文件，
  并支持 HTTP Range 续传及分块重试。
- 对 Parser X 的价值：把平台返回的多个清晰度和备用资源表达成统一候选，逐步扩展到 B站之外的
  平台，再按 QQ 可发送体积与编码兼容性选择。大文件中途断开时可考虑续传。
- 边界：按发送体积预算选择清晰度是针对 Parser X 的建议，并非声称 Lux 已实现同样策略。
  跨 CDN 续传前也必须验证资源身份、Range 响应和总长度，不能仅凭下载了一部分就继续追加。
- 证据：[媒体数据结构](https://github.com/iawia002/lux/blob/dd00f6d258d80b6684a0b9402d7124e5c18ef42f/extractors/types.go)、
  [下载实现](https://github.com/iawia002/lux/blob/dd00f6d258d80b6684a0b9402d7124e5c18ef42f/downloader/downloader.go)。许可证 MIT。

## GitHub 之外的查阅结果与筛选

- 成功访问 cobalt 产品官网、gallery-dl 文档站及 Streamlink 官方文档，不只阅读仓库介绍。
- 成功读取 [Codeberg 上的 yt-dlp 镜像](https://codeberg.org/yt-dlp/yt-dlp) 及其原始 README。
  页面显示的最后提交在 2026-02-22，明显落后于当前时间，因此适合作为镜像资料入口，不建议作为
  插件依赖的更新来源。Parser X 已使用 yt-dlp，这不是本次新增解析后端。
- [BBDown](https://github.com/nilaoda/BBDown) 当前仓库已归档，README 明确不再维护，默认分支仅保留
  说明和许可证。本次不将它列为应新增的持续维护依赖，也没有把历史功能描述当成当前源码验证结果。
- Gitee 检索入口返回验证码，GitLab 的候选页返回登录/反爬页面。没有绕过限制，也没有将这些未能
  核实的候选包装成推荐项目。限制平台范围之外，还应区分“能搜到”与“能验证”。

## 建议实施顺序

1. **本次已完成：成功解析结果缓存。** 默认 300 秒、128 条，同一输入并发只解析一次，仍向各请求
   分别回复；配置与登录态隔离，文件失效后重解析，普通取消和共享下载取消分别处理。
2. **下一步：下载完成判定与最终失败回复。** 借鉴 gallery-dl 的临时文件机制，并结合上一轮的
   发送失败保底建议；避免半个文件被命中，也避免用户最后什么都收不到。
3. **之后：统一错误分类和平台响应样本测试。** 借鉴 cobalt、Streamlink，帮助定位平台接口变化。
4. **按实际需要：体积预算选流、断点续传和分机媒体中转。** 这些比简单加开关更复杂，需要对应
   的真实部署和资源样本验证。

本次新增缓存为独立 Python 实现，使用标准库；没有复制上述项目代码，也未安装它们为运行依赖。
以后若复用具体实现，需要分别核对 MIT、BSD、GPL、AGPL 等许可，不能混作同一套授权。
