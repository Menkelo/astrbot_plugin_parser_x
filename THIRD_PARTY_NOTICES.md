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
