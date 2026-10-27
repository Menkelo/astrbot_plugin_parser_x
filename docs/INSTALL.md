# 安装与构建验证

## 支持的运行方式

仅支持 AstrBot 的 **aiocqhttp / OneBot v11**。推荐通过 AstrBot WebUI 的插件管理器安装仓库，或由管理员按 AstrBot 插件目录规范安装源码。插件不会自动安装系统软件、浏览器或浏览器系统库。

## 测试目录与安装归档

测试源码保留在 Git 仓库，按功能分组，仅供开发验证，插件运行不会自动执行测试。GitHub 源码 ZIP 通过 `.gitattributes` 的 `export-ignore` 排除 `tests/`；正常使用 HTTPS GitHub 仓库地址并走源码 ZIP 安装时不包含测试。wheel 同样不包含测试。

直接 `git clone`、使用 Git 传输安装或保留已有插件目录时，不保证排除已有测试；Git 忽略规则不会删除已安装文件。开发者应克隆仓库运行测试，而不是使用已排除测试的源码 ZIP。开发测试目录说明见 [tests/README.md](../tests/README.md)（仅 Git 检出中可用）。

## 外部依赖（必须由管理员准备）

1. 在运行 AstrBot 的容器/主机安装 **ffmpeg**，确保 AstrBot 进程的 `PATH` 能找到它。
2. 在 **AstrBot 实际使用的同一个 Python 解释器/虚拟环境** 安装插件 Python 依赖。插件管理器通常读取 `requirements.txt`；手动维护环境时可执行：

   ```bash
   python -m pip install -r requirements.txt
   ```

   此处及下文的 `python` 必须替换成 AstrBot 的实际解释器路径，不能在宿主机另一个 Python 环境中执行。当前 Playwright 依赖范围为 `>=1.58,<2`，验证环境为 1.58.0。
3. 在相同容器、Python 环境、运行用户下，手动安装与已安装 Playwright Python 包**匹配**的 Chromium：

   ```bash
   python -m playwright install chromium
   ```

   更新 Playwright 后应重新运行此命令；系统自带 Chrome 或另一个 Playwright 版本的浏览器不保证可用。Chromium 二进制**不随插件/wheel 捆绑**，Python 依赖安装本身不提供浏览器。
4. Linux 还需要 Chromium 所需的系统库。需要管理员授权时，**仅在人工、明确授权的特权环境构建/维护阶段**执行：

   ```bash
   python -m playwright install --with-deps chromium
   ```

   该命令可能调用系统包管理器、要求 root/sudo、修改操作系统软件。它不是插件运行时代码、启动钩子或自动修复步骤；不要赋予机器人进程特权。也可由管理员依据 Playwright 文档预装系统库，再由实际运行用户执行不带 `--with-deps` 的浏览器安装命令。

容器安装要发生在运行 AstrBot 的容器/镜像内，而不是无关宿主环境。系统库和 ffmpeg 应在管理员维护的镜像中准备，浏览器缓存应持久保留并让运行用户可读写。若设置 `PLAYWRIGHT_BROWSERS_PATH`，安装和运行必须使用同一值；默认缓存也必须属于运行用户。不要仅以 root 下载到 `/root` 后就假定普通用户能使用。

插件内置 `assets/fonts/*.woff2`，供 HTML 页面内联加载；字体不等于浏览器，也不替代浏览器系统库。

## Docker 部署：媒体目录共享

AstrBot 与 NapCat、Lagrange 等 OneBot v11 协议端运行在不同容器或不同主机时，**通过本地文件路径发送的媒体，必须也能被协议端读取**。两个容器网络互通，不代表文件互通：AstrBot 里的 `file:///AstrBot/data/...` 不会自动出现在协议端容器中。常见表现是解析和下载成功，但发送时报文件不存在或媒体发送失败。

### 同一宿主机的两个容器

推荐将同一宿主机目录挂载到两个容器的**相同绝对路径**。下面是应分别合入两个服务的 Compose 配置片段（不是完整部署文件）：

```yaml
services:
  astrbot:
    volumes:
      - /srv/astrbot/data:/AstrBot/data

  onebot:
    volumes:
      - /srv/astrbot/data/plugin_data/astrbot_plugin_parser_x:/AstrBot/data/plugin_data/astrbot_plugin_parser_x:ro
```

- `/srv/astrbot/data` 是示例宿主机路径，需换成自己的实际数据目录；`astrbot`、`onebot` 也需换成实际服务名。
- 示例按 AstrBot 数据目录 `/AstrBot/data` 编写，插件媒体缓存位于其下 `plugin_data/astrbot_plugin_parser_x/cache_dir`。如部署路径不同，以实际发送的本地路径为准，并保证协议端看到同一路径；**大小写也必须一致**。
- 协议端只需只读挂载本插件数据目录，不必共享整个 AstrBot 数据目录。该目录中的媒体仍可能包含私人内容，只应提供给可信协议端。
- 先确保宿主机源目录存在；两侧运行用户都需要目录遍历及文件读取权限，不建议直接使用 `chmod 777`。只读挂载不会绕过宿主机权限。
- 使用 `docker run` 或面板部署时，配置等价的目录映射即可；容器名和容器内路径是两回事。

### 不同主机与其他传输方式

不同主机上的同名目录不等于共享目录。需要管理员配置可信的共享存储，并在协议端提供对应路径；或者使用协议端实际支持的 URL/Base64 传输方式。**插件不会自动建立共享存储或自动改用另一种传输方式**，URL 也必须能由实际取件方访问。不要为取件而将含 Cookie、配置或私人媒体的目录公开到互联网。

### 验证与变更注意事项

用一个真实缓存媒体文件，分别在 AstrBot 与协议端容器内确认：**相同绝对路径存在、文件大小一致、协议端运行用户可读**。不能只以 root 用户能读取作为权限验证。路径共享正常也不代表所有媒体都能发送，大小限制、格式兼容和上游发送错误仍需分别排查。

修改挂载通常需要重建容器，而不只是重启。操作前先备份数据与协议端登录态，确认镜像包含现用的 AstrBot 内核、ffmpeg、浏览器及系统依赖；仅安装在旧容器可写层里的更新或软件，重建后可能丢失。**本文只提供配置说明，不要求直接重建现有容器。**

## 人工验证

在 AstrBot 实际运行环境中执行：

```bash
ffmpeg -version
python -m pip show playwright
python -m playwright --version
```

以下可选离线冒烟只启动本地浏览器，不访问外部页面；也必须由管理员手动运行，不会安装任何软件：

```bash
python - <<'PY'
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('<html><body>Parser X browser smoke</body></html>')
            assert await page.title() == ''
            print('Chromium launch and local page: OK')
        finally:
            await browser.close()

asyncio.run(main())
PY
```

缺浏览器时先核对解释器/用户/缓存路径并人工安装；缺共享库时交由管理员维护系统环境。不要在插件业务请求中自动下载或提权。

## Wheel 的边界与完整性

wheel 使用 `astrbot_plugin_parser_x` 包命名空间，而不是把 `core` 泄漏为顶层包。构建包含入口、解析器、配置 schema、AstrBot 元数据、调试页面、本地字体和许可。Python 依赖元数据动态读取 `requirements.txt`，避免两份依赖漂移。运行数据、Cookie 配置、浏览器下载、测试目录和开发缓存不应进入发布包。

**pip/wheel 不是受支持的 AstrBot 插件注册/安装入口**：`pip install` 只安装 Python 包，不会在 AstrBot WebUI 注册、启用或复制插件至受管理目录。正常用户应使用插件管理器；完整 wheel 供构建审计和 Python 分发使用，且仍依赖外部 AstrBot、ffmpeg、匹配 Chromium 与系统库。

构建测试仅在**一次性副本**中执行，避免在有登录配置的工作目录生成/复用 `build` 或 `egg-info`。建议独立测试容器，勿将生产数据目录挂载进去：

```bash
python -m pip install 'setuptools>=61' wheel pytest packaging
python -m pytest -q tests/contracts/test_factory_packaging.py
```

测试在临时目录复制白名单源文件后调用构建后端，检查 wheel/sdist 的命名空间、入口/资源、依赖一致性及运行数据排除；不下载浏览器、不联网调用平台接口、不加载生产插件，也不修改生产环境。

## 登录态

Cookie 为敏感登录凭据，不可写入公开构建产物。当前 10 个 Cookie 字段、配置及风控限制见 [README](../README.md)。尤其小红书：统一 `comments.enabled` 开启且填写 Cookie 后会对符合条件的作品尝试评论请求，没有独立开关；有账号验证、限制乃至封禁风险。不能接受风险时勿填写 Cookie，或关闭会影响所有平台的统一开关。
