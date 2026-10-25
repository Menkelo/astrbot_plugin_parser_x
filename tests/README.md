# Test organization

Run from the plugin checkout with its parent on `PYTHONPATH`:

```sh
PYTHONPATH=.. python -m pytest tests
```

The suite deliberately exercises both historical `core.*` imports and package-qualified `astrbot_plugin_parser_x.*` imports. Keep the checkout as the working directory; no test modifies `sys.path` or aliases runtime modules. Test directories are Python packages and shared test helpers use explicit relative imports.

## Functional groups and move map

All original `tests/test_<name>.py` files moved unchanged in basename to the directory below. No tests were deleted, merged, or renamed; class/function names and parameter IDs remain intact.

| Directory | Original filename stems (`test_` prefix and `.py` suffix omitted) |
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

`routing/test_routing.py` retains the historical broad regression suite, including its helper-delegating entry points. Splitting that module would be a separate behavioral refactor, not a file-organization change. Source/schema assertions in relocated modules now traverse two parents to the plugin root instead of one.

## Shared helper relationships

- `cache/test_comment_card_cache.py` provides `plugin` and `result` to `cache/test_comment_cache_review.py`, `cache/test_simple_card_disk_cache.py`, and `cache/test_cache_policy_repair.py`.
- `delivery/test_unified_delivery.py` provides plugin/event/document/forward inspection helpers to `cache/test_cache_simple_delivery_review.py`, `cache/test_disk_results_integration.py`, `cache/test_simple_card_disk_cache.py`, `cache/test_cache_policy_repair.py`, and `delivery/test_disabled_summary_delivery.py`.
- `routing/test_routing.py` calls the existing parametrized video-completion regression and comment/forward failure helpers in `delivery/test_unified_delivery.py` inside historical test functions, preserving collection without importing additional tests into module globals.
- `delivery/test_media_completion.py` provides `Downloads`, `Event`, `_FakeCardRenderer`, and `config` to `delivery/test_large_album_forward.py`.
- `comments/test_comment_filter.py` provides `_entry`, `_filter`, and `_text` to `comments/test_mention_preservation.py`.
- `routing/test_emoji_ack.py` provides `_plugin` to `routing/test_reaction_toggle.py`.
- `parsers/test_kuaishou_comment.py` provides `make_feed` and `raw` to `parsers/test_kuaishou_media_repair.py`.

For collection comparisons, map each moved node ID back through the table (or compare basename plus the complete `::class::test[param-id]` suffix). Directory prefixes necessarily change; all other identity components must match as a multiset.
