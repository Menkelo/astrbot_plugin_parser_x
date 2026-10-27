# 第三方来源与许可声明

## 范围、授权边界与发布前待决事项

本文件区分随包复制/移植的代码、字体和图形、仅安装的运行依赖，以及行为/接口参考。项目根 `LICENSE` 中的 MIT 声明不替代第三方许可，不将依赖、平台内容或字体重新授权为 MIT。第三方版权、商标及其他权利仍归各自权利人；公开可访问、可下载、能够渲染或注明来源都不等于取得再分发许可。

2026-10-27 对当前源码副本的审计记录以下**许可边界与未解决审查项**。维护者在知悉下述文件级额外声明及许可疑点后，选择保留现有抖音签名实现并完整披露，不将候选签名替换为未经验证的其他实现。该维护决定不是权利人授权，也不代表法律审查通过；补充本声明本身不会取得授权，不能据此声称 0.0.2 已通过许可验收：

1. **A-Bogus 文件级限制与仓库许可证冲突。** `core/parsers/douyin/a_bogus.py` 明确是下述 rconsole-plugin 模块的修改后 Python 实现。上游仓库根许可证为 Mulan PSL v2，但 `utils/a-bogus.cjs` 首行另有禁止商用等限制（下文逐字保留）。未取得权利人澄清前，不应把该移植模块无条件表述为仅受 Mulan PSL v2 或插件 MIT 许可；需取得适用于该文件的明确授权，或移除/合法替换后重新审查。
2. **平台表情已改为运行时取回，不再随包分发图像。** 旧的 `core/xiaohongshu_emote_data.py` 与 `core/coolapk_emotes.json` 已从候选发布副本移除；现在仅带官方 URL/文件名来源表。首次使用时下载静态图并缓存，失败保留表情名；PAG-only 表情当前保留文本，不带 libpag 二进制或生成帧。此变更移除了上述内嵌图像的再分发问题，但不表示平台已授予图像、目录或运行时使用的开放许可。
3. **GPL 运行依赖的组合分发边界需审查。** `bilibili-api-python` 是实际直接导入的依赖，其当前上游许可为 GPL-3.0-or-later。原创文件保留 MIT 许可并不自动满足结合分发时的 GPL 条件；发布者应核实实际解析版本、插件与依赖的组合方式及对应源码/许可义务，不能将组合物一概宣传为“全部 MIT”。本文件不是法律意见，亦不自行改变根许可证。
4. **小黑盒来源的额外文件声明需保留并核清。** 下文列明 MIT 许可链及 `encrypt.py` 的禁止渗透测试声明；仓库级 MIT 与文件附加限制的适用范围仍应向上游确认，不把该声明删减为单纯致谢。

审计依据为本副本内的源码、资源、构建配置及下列公开上游文件（核验日 2026-10-27）；本副本没有 Git 历史，无法据此证明未注明出处的代码全部独立创作，也未完成所有传递依赖和各平台服务条款的法律审查。发布资源 `logo.png`、调试台 HTML/JS/CSS 未见额外第三方来源声明；如维护者另有素材/代码来源，应补充其证明，不能仅凭未写声明推定原创。本副本 `data/t2i_templates/` 另有 AstrBot 风格模板并引用远程 Outfit、MiSans 和 KaTeX；`data/` 属 `.gitignore` 标明的运行数据且不在 wheel 资源白名单，不应进入发布包，也不应把这些模板误当作插件随包原创资源。

## 随包字体：Noto Sans SC

- 文件：`assets/fonts/NotoSansSC-400.woff2`、`assets/fonts/NotoSansSC-700.woff2`，由 `core/browser_backend.py` 内联用于卡片。
- 本地 WOFF2 `name` 表实际包含：`(c) 2014-2021 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source'.`；版本 `Version 2.004-H2;hotconv 1.0.118;makeotfexe 2.5.65603`；许可地址 `http://scripts.sil.org/OFL`。family 字段为 `Noto Sans SC Thin`，subfamily 分别为 `Regular` / `Bold`；文件名不是上游下载溯源记录。
- 上游项目：[Noto CJK](https://github.com/notofonts/noto-cjk)；核对的 [Google Fonts Noto Sans SC OFL 原文](https://github.com/google/fonts/blob/main/ofl/notosanssc/OFL.txt) 与本地版权、保留字体名一致。精确下载 URL/子集化命令未记录，不虚构其来源或宣称字节等同上游原包。
- 许可：**SIL Open Font License 1.1**，完整原文附后；字体本身不适用插件 MIT。OFL 允许随软件嵌入、分发，须保留版权和许可、遵守保留字体名等条件。系统上另行安装的 Emoji/回退字体不随本插件打包，其许可由对应提供者负责。
- SHA-256：400 为 `95e3633b6a98f764ba3adfb54504a0cd4799328c009adf9081d6c1850f9c4c78`；700 为 `e1df51edc00bce27b58044e829fb8ec6accc8a5daece475413de90d52818845c`。

## 小黑盒签名与 SM 指纹移植

- 对应文件：`core/parsers/xiaoheihe/sign.py`、`core/parsers/xiaoheihe/sm.py`。本地文件已明确标出社区逆向来源，不能仅按插件原创代码处理。
- [nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite) 的 [MIT LICENSE](https://github.com/sokoko-org/nonebot-plugin-parser-lite/blob/main/LICENSE)：`Copyright (c) 2025 molanp`、`Copyright (c) 2024 Les Freire`。
- 签名对应 [heybox/encrypt.py](https://github.com/sokoko-org/nonebot-plugin-parser-lite/blob/main/src/nonebot_plugin_parser_lite/parsers/heybox/encrypt.py)，上游保留声明如下：

```text
本脚本由 焦化葱葱(negichan) 提供
感谢 焦化葱葱(negichan) 的逆向工程
感谢群友们的积极测试和为逆向付出的努力
严禁非法滥用或用于渗透测试
```

- SM 对应 [heybox/sm.py](https://github.com/sokoko-org/nonebot-plugin-parser-lite/blob/main/src/nonebot_plugin_parser_lite/parsers/heybox/sm.py)，该文件明确修改自 [skyland-auto-sign/SecuritySm.py，提交 72b627a72c83250551115f7453ff226f972dec2d](https://github.com/YueHen14/skyland-auto-sign/blob/72b627a72c83250551115f7453ff226f972dec2d/SecuritySm.py)。[skyland-auto-sign LICENSE](https://github.com/YueHen14/skyland-auto-sign/blob/main/LICENSE) 为 MIT，`Copyright (c) 2023 xxyz30`。
- 下述 MIT 条款适用于以上明确采用 MIT 的上游贡献；不用于抹去文件级限制或授予对小黑盒、数美 SDK、平台商标及服务的其他权利。原前端逆向背景不等于原厂授权。

```text
MIT License

Copyright (c) 2025 molanp
Copyright (c) 2024 Les Freire
Copyright (c) 2023 xxyz30

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 平台表情、目录 JSON 与远程内容

| 文件/素材 | 来源与随包形式 | 授权边界 |
| --- | --- | --- |
| `core/emote_sources.py` | 小红书官方静态图 URL 及 PAG 来源 URL 元数据；无图像字节 | 静态图运行时下载，PAG-only 当前回落文本；来源表不表示图像开放授权 |
| `core/coolapk_emote_files.json` | 141 个表情 token → 酷安官方 CDN 文件名，无 PNG/data URI | 运行时按正文实际 token 下载；权利归平台/原作者，不宣称为插件原创素材 |
| `core/parsers/kuaishou_emotes.json` | 文件自带来源 `https://www.kuaishou.com/graphql`、操作 `query visionBaseEmoticons { visionBaseEmoticons { iconUrls } }`、采集日期 2026-10-27；表情 token → 第一方 CDN URL | 随包是目录快照，不含图像字节；未发现平台授予目录/图像的开放许可。公开接口不等于再许可；目录与运行时取回图像均不宣称 MIT |
| 运行时取回的作品、封面、头像、评论及平台表情 | 各平台官方接口、公开页面或内容 CDN，按解析结果下载/缓存 | 权利归平台/原作者；不属于插件代码许可范围。平台商标不构成对插件的支持/背书 |

旧内嵌小红书图像曾由 `libpag` 离线生成，现已移除。候选副本保留默认禁用的 PAG 实验入口，但不带 libpag JS/WASM、图像或生成帧；常规运行不下载 libpag。工具许可证不会自动许可输入/输出的第三方图形。

## Python 运行依赖与外部程序（非插件重新授权）

以下是 `requirements.txt` 的直接依赖，不是已 vendored 到本项目的库源码。许可证名称根据 2026-10-27 [PyPI JSON 项目元数据](https://docs.pypi.org/api/json/)及项目来源核对；范围未全量锁版，**安装时以实际版本自带 LICENSE/NOTICE 为准**，下表不是完整传递依赖 SBOM，也不能替代重新打包依赖时的版权、许可证副本及源码义务。

| 依赖 | 核对的上游/项目元数据 | 许可 |
| --- | --- | --- |
| aiohttp | https://pypi.org/pypi/aiohttp/json | Apache-2.0 AND MIT（当前版本元数据） |
| aiofiles | https://github.com/Tinche/aiofiles | Apache-2.0 |
| curl_cffi | https://github.com/lexiforest/curl_cffi | MIT；其 libcurl/原生组件另有各自声明 |
| msgspec | https://github.com/msgspec/msgspec | BSD-3-Clause |
| yt-dlp | https://github.com/yt-dlp/yt-dlp | Unlicense（Python 项目）；组合可执行文件/附带组件不得一概视为 Unlicense |
| bilibili-api-python | https://github.com/Nemo2011/bilibili-api | GPL-3.0-or-later，见上文组合分发审查项 |
| tqdm | https://github.com/tqdm/tqdm | MPL-2.0 AND MIT（分别覆盖不同部分，不是随意二选一） |
| APScheduler | https://github.com/agronholm/apscheduler | MIT（当前 3.x 元数据） |
| Pillow | https://github.com/python-pillow/Pillow | MIT-CMU；附带原生库另行遵守其许可 |
| Playwright Python | https://github.com/microsoft/playwright-python | Apache-2.0；Chromium 及驱动/浏览器附属组件另有许可 |
| zxing-cpp | https://github.com/zxing-cpp/zxing-cpp | Apache-2.0 |
| xhshow | https://github.com/cloxl/xhshow | MIT（PyPI 0.2.0 classifier） |

`cryptography` 被 `core/parsers/xiaoheihe/sm.py` 直接导入，已显式列入 `requirements.txt`，不依赖其他包偶然传递安装；其当前项目元数据为 `Apache-2.0 OR BSD-3-Clause`，其原生/传递组件另有声明。参见 https://github.com/pyca/cryptography 。

外部 AstrBot、ffmpeg、Chromium 及其系统库不随插件源码/wheel 打包，不因被调用而改成 MIT。AstrBot 为 AGPL-3.0-or-later 项目（https://github.com/AstrBotDevs/AstrBot）；ffmpeg 的 LGPL/GPL 等具体义务取决于实际构建选项和附加组件（https://ffmpeg.org/legal.html）；Chromium 的 BSD 风格主许可不涵盖所有第三方组件（https://www.chromium.org/developers/how-tos/get-the-code/）。若另制作含上述软件/依赖的镜像或二进制合集，分发者须针对实际版本另行整理许可、NOTICE 和适用的源码提供义务。

## ParseHub（新增平台适配参考）

- 来源仓库：https://github.com/z-mio/ParseHub
- 许可证：MIT；Copyright (c) 2024 梓澪。
- 用途：贴吧 `provider_api/tieba.py` / `parser/tieba.py` 的公开帖子字段及请求契约，酷安 `provider_api/coolapk.py` 与公众号 `provider_api/weixin.py` 及相关 parser 的正文选择器、图片顺序和公开页面字段参考；在 Parser X 的现有数据、下载、评论、缓存及投递接口内适配，不引入 ParseHub 运行时。
- 知乎签名模块另注明衍生自 `cv-cat/ZhihuApis`，未核实该上游授权，因此本版本不复制该签名算法或常量。网易云音乐及 NGA 为独立适配，并非 ParseHub 移植。未复制 AGPL 的 `astrbot_plugin_media_parser` 实现。
- 贴吧后续 HTTPS Android 接口验证参考了 `drdon1234/astrbot_plugin_media_parser` 提交 `056085e430ff2b1d8c03c64a2030fff81efdce04` 所展示的接口位置，并用两个公开帖子独立观察请求/响应协议；该仓库为 AGPL-3.0，不将其源码、函数或解析实现移植到本项目。接口字段观察不等于对上游代码的兼容许可声明。

```text
MIT License

Copyright (c) 2024 梓澪

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## rconsole-plugin

- 来源仓库：https://github.com/zhiyu1998/rconsole-plugin
- 仓库根许可证：[木兰宽松许可证，第 2 版（Mulan PSL v2）](https://github.com/zhiyu1998/rconsole-plugin/blob/master/LICENSE)，完整中英文原文保留于本文下方。
- 本项目用途：功能范围、平台覆盖与兼容行为的参考，不包含 Yunzai 运行时。`core/parsers/douyin/a_bogus.py` 是对 [`utils/a-bogus.cjs`](https://github.com/zhiyu1998/rconsole-plugin/blob/master/utils/a-bogus.cjs) 进行修改后的 Python 实现，不能将整个用途描述成“未复制、仅参考”。
- **文件级额外声明（上游首行原文）**：

```text
All the content in this article is only for learning and communication use, not for any other purpose, strictly prohibited for commercial use and illegal use, otherwise all the consequences are irrelevant to the author!
```

- 该声明与仓库宽松许可证之间的冲突未获澄清；本文不擅自删减限制或裁定其无效，也不将整份插件改为“禁止商用”。2026-10-27 维护者选择保留本实现及上述完整声明；未声称已取得权利人额外授权。使用者和再分发者不应仅凭项目根 MIT 或本文件的来源署名推定该模块可无条件商用或用于其他目的；需要此类使用时，应先取得权利人明确澄清，或使用授权清晰且完成兼容验证的替代实现。

## astrbot_plugin_r_parser

- 来源仓库：https://github.com/Menkelo/astrbot_plugin_r_parser
- 许可证：MIT 许可证（版权声明保留在 `LICENSE` 中）
- 本项目用途：历史 OneBot 投递和生命周期实现的参考。B站评论区实现根据上游 `rconsole-plugin` 的行为独立适配，未复用该仓库的 B站评论模块。

## AstrBot 文档与源码

- 来源仓库：https://github.com/AstrBotDevs/AstrBot
- 文档：https://docs.astrbot.app/
- 本项目用途：公共插件 API、元数据、存储及 aiocqhttp 集成约定的参考。

## zxing-cpp

- 来源仓库：https://github.com/zxing-cpp/zxing-cpp
- 许可证：Apache 许可证 2.0
- 本项目用途：渲染评论卡片前，检测评论配图中的二维码。检测在本地完成，并限制图片大小和执行时间；图片无法解码时保留，不因检测失败而过滤。

## Twemoji

- 来源仓库：https://github.com/jdecked/twemoji
- 代码许可证：MIT 许可证
- 图形许可证：知识共享署名 4.0 国际许可协议（CC BY 4.0）
- 历史用途：曾通过 jsDelivr 提供的 Twemoji 15.1 SVG 资源渲染正文和视频评论卡片中的 Unicode 表情，未将图形资源打包分发；加载失败时回退为原始 Unicode 字符。图形由 Twitter 和项目贡献者提供。当前 Unicode 表情使用本地字体，不再请求 Twemoji CDN；此处保留历史来源说明。

## SIL Open Font License 1.1——完整原文

以下版权与许可副本随前述两个字体一起提供；不得以插件根 MIT 文件替代。

```text
Copyright 2014-2021 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source'

This Font Software is licensed under the SIL Open Font License, Version 1.1.
This license is copied below, and is also available with a FAQ at:
https://scripts.sil.org/OFL

-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
```

## 木兰宽松许可证第 2 版——完整原文

<details>
<summary>木兰宽松许可证，第2版 / Mulan Permissive Software License, Version 2</summary>

```text
木兰宽松许可证, 第2版

木兰宽松许可证，第2版
2020年1月 http://license.coscl.org.cn/MulanPSL2

您对“软件”的复制、使用、修改及分发受木兰宽松许可证，第2版（“本许可证”）的如下条款的约束：

0. 定义

“软件”是指由“贡献”构成的许可在“本许可证”下的程序和相关文档的集合。

“贡献”是指由任一“贡献者”许可在“本许可证”下的受版权法保护的作品。

“贡献者”是指将受版权法保护的作品许可在“本许可证”下的自然人或“法人实体”。

“法人实体”是指提交贡献的机构及其“关联实体”。

“关联实体”是指，对“本许可证”下的行为方而言，控制、受控制或与其共同受控制的机构，此处的控制是指有受控方或共同受控方至少50%直接或间接的投票权、资金或其他有价证券。

1. 授予版权许可

每个“贡献者”根据“本许可证”授予您永久性的、全球性的、免费的、非独占的、不可撤销的版权许可，您可以复制、使用、修改、分发其“贡献”，不论修改与否。

2. 授予专利许可

每个“贡献者”根据“本许可证”授予您永久性的、全球性的、免费的、非独占的、不可撤销的（根据本条规定撤销除外）专利许可，供您制造、委托制造、使用、许诺销售、销售、进口其“贡献”或以其他方式转移其“贡献”。前述专利许可仅限于“贡献者”现在或将来拥有或控制的其“贡献”本身或其“贡献”与许可“贡献”时的“软件”结合而将必然会侵犯的专利权利要求，不包括对“贡献”的修改或包含“贡献”的其他结合。如果您或您的“关联实体”直接或间接地，就“软件”或其中的“贡献”对任何人发起专利侵权诉讼（包括反诉或交叉诉讼）或其他专利维权行动，指控其侵犯专利权，则“本许可证”授予您对“软件”的专利许可自您提起诉讼或发起维权行动之日终止。

3. 无商标许可

“本许可证”不提供对“贡献者”的商品名称、商标、服务标志或产品名称的商标许可，但您为满足第4条规定的声明义务而必须使用除外。

4. 分发限制

您可以在任何媒介中将“软件”以源程序形式或可执行形式重新分发，不论修改与否，但您必须向接收者提供“本许可证”的副本，并保留“软件”中的版权、商标、专利及免责声明。

5. 免责声明与责任限制

“软件”及其中的“贡献”在提供时不带任何明示或默示的担保。在任何情况下，“贡献者”或版权所有者不对任何人因使用“软件”或其中的“贡献”而引发的任何直接或间接损失承担责任，不论因何种原因导致或者基于何种法律理论，即使其曾被建议有此种损失的可能性。

6. 语言

“本许可证”以中英文双语表述，中英文版本具有同等法律效力。如果中英文版本存在任何冲突不一致，以中文版为准。

条款结束

如何将木兰宽松许可证，第2版，应用到您的软件

如果您希望将木兰宽松许可证，第2版，应用到您的新软件，为了方便接收者查阅，建议您完成如下三步：

1，请您补充如下声明中的空白，包括软件名、软件的首次发表年份以及您作为版权人的名字；

2，请您在软件包的一级目录下创建以“LICENSE”为名的文件，将整个许可证文本放入该文件中；

3，请将如下声明文本放入每个源文件的头部注释中。

Copyright (c) [Year] [name of copyright holder]
[Software Name] is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details.


Mulan Permissive Software License，Version 2

Mulan Permissive Software License，Version 2 (Mulan PSL v2)
January 2020 http://license.coscl.org.cn/MulanPSL2

Your reproduction, use, modification and distribution of the Software shall be subject to Mulan PSL v2 (this License) with the following terms and conditions:

0. Definition

Software means the program and related documents which are licensed under this License and comprise all Contribution(s).

Contribution means the copyrightable work licensed by a particular Contributor under this License.

Contributor means the Individual or Legal Entity who licenses its copyrightable work under this License.

Legal Entity means the entity making a Contribution and all its Affiliates.

Affiliates means entities that control, are controlled by, or are under common control with the acting entity under this License, ‘control’ means direct or indirect ownership of at least fifty percent (50%) of the voting power, capital or other securities of controlled or commonly controlled entity.

1. Grant of Copyright License

Subject to the terms and conditions of this License, each Contributor hereby grants to you a perpetual, worldwide, royalty-free, non-exclusive, irrevocable copyright license to reproduce, use, modify, or distribute its Contribution, with modification or not.

2. Grant of Patent License

Subject to the terms and conditions of this License, each Contributor hereby grants to you a perpetual, worldwide, royalty-free, non-exclusive, irrevocable (except for revocation under this Section) patent license to make, have made, use, offer for sale, sell, import or otherwise transfer its Contribution, where such patent license is only limited to the patent claims owned or controlled by such Contributor now or in future which will be necessarily infringed by its Contribution alone, or by combination of the Contribution with the Software to which the Contribution was contributed. The patent license shall not apply to any modification of the Contribution, and any other combination which includes the Contribution. If you or your Affiliates directly or indirectly institute patent litigation (including a cross claim or counterclaim in a litigation) or other patent enforcement activities against any individual or entity by alleging that the Software or any Contribution in it infringes patents, then any patent license granted to you under this License for the Software shall terminate as of the date such litigation or activity is filed or taken.

3. No Trademark License

No trademark license is granted to use the trade names, trademarks, service marks, or product names of Contributor, except as required to fulfill notice requirements in Section 4.

4. Distribution Restriction

You may distribute the Software in any medium with or without modification, whether in source or executable forms, provided that you provide recipients with a copy of this License and retain copyright, patent, trademark and disclaimer statements in the Software.

5. Disclaimer of Warranty and Limitation of Liability

THE SOFTWARE AND CONTRIBUTION IN IT ARE PROVIDED WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED. IN NO EVENT SHALL ANY CONTRIBUTOR OR COPYRIGHT HOLDER BE LIABLE TO YOU FOR ANY DAMAGES, INCLUDING, BUT NOT LIMITED TO, ANY DIRECT, OR INDIRECT, SPECIAL OR CONSEQUENTIAL DAMAGES ARISING FROM YOUR USE OR INABILITY TO USE THE SOFTWARE OR THE CONTRIBUTION IN IT, NO MATTER HOW IT’S CAUSED OR BASED ON WHICH LEGAL THEORY, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGES.

6. Language

THIS LICENSE IS WRITTEN IN BOTH CHINESE AND ENGLISH, AND THE CHINESE VERSION AND ENGLISH VERSION SHALL HAVE THE SAME LEGAL EFFECT. IN THE CASE OF DIVERGENCE BETWEEN THE CHINESE AND ENGLISH VERSIONS, THE CHINESE VERSION SHALL PREVAIL.

END OF THE TERMS AND CONDITIONS

How to Apply the Mulan Permissive Software License，Version 2 (Mulan PSL v2) to Your Software

To apply the Mulan PSL v2 to your work, for easy identification by recipients, you are suggested to complete following three steps:

i Fill in the blanks in following statement, including insert your software name, the year of the first publication as well as your name identified as the copyright owner;

ii Create a file named “LICENSE” which contains the whole context of this License in the first directory of the software package;

iii Attach the statement to the appropriate annotated syntax at the beginning of each source file.

Copyright (c) [Year] [name of copyright holder]
[Software Name] is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details.

```

</details>
