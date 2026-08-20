# Third-party notices

## rconsole-plugin

- Repository: https://github.com/zhiyu1998/rconsole-plugin
- License: Mulan Permissive Software License, Version 2
- Use in this project: feature inventory, platform coverage and compatibility baseline. Parser X does not bundle the
  Yunzai runtime or copy its JavaScript modules verbatim. `core/parsers/douyin/a_bogus.py` is a modified,
  clean Python adaptation of the behavior in `utils/a-bogus.cjs`; the applicable Mulan PSL v2 text is bundled at
  `licenses/MulanPSL-2.0.txt`.

## astrbot_plugin_r_parser

- Repository: https://github.com/Menkelo/astrbot_plugin_r_parser
- License: MIT License (copyright notice retained in `LICENSE`)
- Use in this project: historical OneBot delivery and lifecycle reference only. The B站评论区
  implementation is independently adapted from the upstream `rconsole-plugin` behavior and does
  not reuse that repository's B站评论模块。

## AstrBot documentation and source

- Repository: https://github.com/AstrBotDevs/AstrBot
- Documentation: https://docs.astrbot.app/
- Use in this project: public plugin API, metadata, storage and aiocqhttp integration contract.

## xhshow

- Repository: https://github.com/Cloxl/xhshow
- License: MIT License
- Use in this project: generate the current Xiaohongshu web request signature headers for the optional,
  Cookie-authenticated video comment feed. Parser X does not start or bundle a browser for this feature.

## zxing-cpp

- Repository: https://github.com/zxing-cpp/zxing-cpp
- License: Apache License 2.0
- Use in this project: detect QR codes in user-supplied comment images before rendering video comment cards.
  Detection is local, bounded by image size and timeouts, and fails open when an image cannot be decoded.
