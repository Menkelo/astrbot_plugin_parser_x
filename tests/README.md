# 测试目录说明

在插件 Git 检出目录中执行，并将其父目录加入 `PYTHONPATH`：

```sh
PYTHONPATH=.. python -m pytest tests
```

测试同时覆盖历史 `core.*` 导入和完整包名 `astrbot_plugin_parser_x.*` 导入。请以插件目录为工作目录；测试不会修改 `sys.path` 或为运行模块设置别名。各测试目录是 Python 包，共享辅助代码使用显式相对导入。

## 功能分组与迁移对照

原有 `tests/test_<name>.py` 按下表分组，文件名保持不变。没有删除、合并或重命名测试，测试类、函数和参数标识均保留。

| 目录 | 原文件名主体（省略 `test_` 前缀及 `.py` 后缀） |
| --- | --- |
| `acfun/` | acfun_comment, acfun_emote_catalog, acfun_emote_resilience, acfun_integration, acfun_metadata_correct, acfun_pure_emotes, acfun_ubb |
| `cache/` | cache_policy_repair, cache_simple_delivery_review, comment_cache_review, comment_card_cache, disk_result_cache, disk_results_integration, result_cache, simple_card_disk_cache |
| `comments/` | caption_dedup, comment_contract_repair, comment_filter, mention_preservation, reply_prefix |
| `config/` | config_policy, cookie_settings, group_ids |
| `contracts/` | factory_browser, factory_packaging, slim_contract |
| `debug/` | debug_api_layer, debug_mode_api, debug_mode_page, debug_page, debug_resource_limits |
| `delivery/` | delivery_layer, disabled_summary_delivery, large_album_forward, media_completion, media_send_fallback, unified_delivery, video_forward_heading |
| `media/` | codec_downloader, codec_other, codec_social, download_atomic, ffmpeg_cancel, media_file_guard, video_codec |
| `parsers/` | douyin_commerce_images, douyin_image_decode, kuaishou_comment, kuaishou_cookie_schema, kuaishou_media_repair, visual_parser_cleanup, weibo_card_repair, weibo_fid_browser, xiaoheihe_error_hint, xiaoheihe_html |
| `rendering/` | body_image_scaling, card_flow, card_guard, card_polish, card_service_layer, failed_comment_gallery, optical_spacing, reference_layout, timestamp_highlight, visual_refresh |
| `routing/` | emoji_ack, factory_routing, live_guard, nonwork_guard, reaction_toggle, routing |

`routing/test_routing.py` 保留历史综合回归用例及其辅助调用。拆分该文件应作为独立重构处理，不混入目录整理。移动后的源码和配置断言使用两级父目录定位插件根目录。

## 共享辅助代码关系

- `cache/test_comment_card_cache.py` 为同组的缓存复用、简单磁盘缓存和策略修复测试提供 `plugin`、`result`。
- `delivery/test_unified_delivery.py` 为缓存集成和投递降级测试提供插件、事件、评论文档及转发节点检查辅助代码。
- `routing/test_routing.py` 在测试函数内部调用统一投递测试的辅助代码，不将其他测试导入模块全局，避免重复收集。
- `delivery/test_media_completion.py` 为大图集投递测试提供 `Downloads`、`Event`、`_FakeCardRenderer` 和 `config`。
- `comments/test_comment_filter.py` 为提及保留测试提供 `_entry`、`_filter`、`_text`。
- `routing/test_emoji_ack.py` 为表情开关测试提供 `_plugin`。
- `parsers/test_kuaishou_comment.py` 为快手媒体修复测试提供 `make_feed` 和 `raw`。

对比迁移前后的收集结果时，可按文件名与完整 `::class::test[param-id]` 后缀比较。目录前缀会变化，其余标识及各自出现次数应保持一致。
