# 上游同步流程

Parser X 不直接复制 rconsole-plugin 的 Yunzai 代码。同步的单位是“用户可观察功能”和“平台协议变化”，
而不是文件级覆盖。

1. 运行 `python tools/check_upstream.py`，记录本地基线与远端 commit。
2. 在临时目录检出新上游，查看基线到新 commit 的提交和差异：

   ```bash
   git -C ../rconsole-plugin fetch origin master
   git -C ../rconsole-plugin log --oneline <旧commit>..origin/master
   git -C ../rconsole-plugin diff <旧commit>..origin/master -- apps utils constants config
   ```

3. 按平台更新 `docs/UPSTREAM_COMPATIBILITY.md`：新增功能、接口变更、配置项和已删除功能都要记录。
4. 选择适配路径：
   - 核心平台或图文/评论等复杂能力：更新原生 Parser。
   - yt-dlp 已稳定支持的纯音视频平台：更新 `core/parsers/ytdlp.py` 路由或下载策略。
   - Yunzai 专属运维能力：标记“不适用”，不要引入 Redis、全局 Bot 或自更新逻辑。
   - 国外平台：当前项目范围明确排除，不增加解析器、配置项或路由。
   - 评论区：先对照 `utils/bili-comment.js`、`utils/douyin-comment.js`、
     `utils/weibo.js` 的接口与可见字段，再分别更新平台 feed；共享布局只更新
     `core/comment_canvas.py` 与 B站专属 Canvas，平台色统一从 `core/card_theme.py` 读取，
     B站等级/粉丝牌等字段仍留在 `core/parsers/bilibili/`。
   - 正文/动态卡：统一骨架更新 `core/text_renderer.py`，平台只在 `core/card_theme.py`
     声明名称、字标和品牌色；媒体投递顺序继续由各 Parser 的 `DeliveryPlan` 控制。
5. 为每个行为变化增加测试夹具；禁止只修改 manifest 来“消除更新提示”。
6. 运行 compileall、pytest、ruff，以及 AstrBot 当前版本下的插件导入测试。
7. 人工在 aiocqhttp/NapCat 或 Lagrange 上至少验证：文本、单图、多图、视频、合并转发、失败降级。
8. 完成后再更新 `upstream/manifest.json` 的 `commit`、`synced_at` 和说明。

此流程让适配层保持稳定，也保留对上游演进的可追踪性。
