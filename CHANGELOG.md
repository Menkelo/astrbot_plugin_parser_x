# Changelog

本项目遵循语义化版本，版本更新同时记录用户可见变化、兼容迁移和验证范围。

## [0.5.0] - 2026-08-14

### Changed

- 新增共享 `HtmlRenderService`，正文卡、B站动态、B站直播以及 B站/抖音/微博评论卡统一复用
  AstrBot 官方 `Star.html_render()`（Canvas/T2I）。
- 移除插件内所有 Playwright / 本地 Chromium 启动路径和运行时依赖；官方渲染失败时由调用方
  按原有容错策略跳过卡片或回退文本，不再维护第二套浏览器环境。
- 保留官方截图结果的轻量裁边，避免 AstrBot 默认画布在卡片右侧和底部留下空白。
- 按 AstrBot 最新配置 Schema 重组配置面板：新增“卡片渲染”和“发送行为”分组，使用
  `obvious_hint`、下拉 `labels`、滑块、折叠项和 `file` 上传组件。
- 自动迁移旧版顶层 `show_download_fail_tip`、`disabled_sessions` 和 `bili_comment` 配置，
  保留升级前行为。

### Fixed

- 微博主评论及楼中楼头像改为带微博 Referer 下载后内嵌，避免新浪 CDN 防盗链导致字母占位。
- 小黑盒支持 `api.xiaoheihe.cn` 等分享子域名，并读取分享跳转中的帖子标题与简介。
- 评论区页脚统一为 `Menkelo/astrbot_plugin_parser_x`。

### Validation

- 路由、配置迁移、评论适配、官方渲染调用与发送降级均有自动化回归测试。
- 提交前执行 `pytest`、Ruff、格式检查和 Python 编译检查。

## [0.4.0] - 2026-08-12

### Added

- 建立面向 AstrBot OneBot v11 的国内平台解析基线。
- 原生支持 Bilibili、抖音、快手、微博、小红书、米游社、贴吧、小黑盒，并加入有限的
  yt-dlp 国内平台兼容层。
- 建立上游兼容清单、同步流程和定时漂移检查。
