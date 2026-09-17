# 打包说明

本目录是把 `bitxk` 打成可分发程序所需的全部文件。你不需要装任何打包工具，
脚本会自己建虚拟环境、装依赖。

## 一键打包

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-release.ps1
```

也可以直接右键 `build-release.ps1` → 「使用 PowerShell 运行」。

### macOS / Linux

```bash
bash packaging/build-release.sh
```

两条命令做的事完全一样，跑完在 **`dist-release/`** 下拿到成品。

### 可选参数

| Windows | macOS / Linux | 作用 |
| --- | --- | --- |
| `-Version 0.2.0` | `--version 0.2.0` | 指定版本号，默认从 `bitxk/__init__.py` 读 |
| `-SkipInstaller` | — | 不生成 Inno Setup 安装包（仅 Windows） |
| `-SkipPortable` | `--skip-portable` | 不生成单文件便携版（快很多） |
| — | `--skip-app` | 不生成 .app（仅 macOS） |

## 产物

```
dist-release/
  BIT-Course-Helper-0.1.0-windows-x64.zip       解压即用，主要分发物
  BIT-Course-Helper-0.1.0-setup.exe             安装包，按用户装，免管理员
  BIT-Course-Helper-0.1.0-windows-portable-gui.exe   免解压单文件版（图形界面）
  BIT-Course-Helper-0.1.0-windows-portable-cli.exe   免解压单文件版（命令行）
  SHA256SUMS.txt                                校验和
```

macOS 上是 `-macos-arm64.tar.gz`（或 `-macos-x64`），没有 setup.exe。

> **为什么 macOS 用 tar.gz 不用 zip**：包里有 `.app` 框架的符号链接，zip 会把
> 符号链接按实体文件重复打包，体积直接翻倍（24MB → 50MB+）。

### 发行包里故意不放精简命令行版

`bitxk-cli.spec`（不含 tkinter 的精简命令行版）**仍然会被构建**，但不会放进发行包。

原因是体积：文件夹版里已经有 `bitxk-gui` 和 `bitxk` 两个程序，它们共用同一份
`_internal/`；把精简版目录再塞进发行包，等于多带一整套运行库（约 22MB），
发行包会从 25MB 直接涨到 50MB，而功能上没有任何增量 ——
`bitxk` 命令行程序本来就在文件夹版里。

单文件便携版则不同：两个 exe 加起来才 27MB，且免解压，所以保留在 `portable/`。
它是可选的 —— 觉得大就加 `-SkipPortable` / `--skip-portable`，发行包能从
40MB 降到约 25MB，功能完全不受影响（文件夹版里本来就有图形界面和命令行两个程序）。

## 脚本内部流程

两个脚本结构一致，都是为了**尽早失败**：

1. **检查 Python** —— 要求 ≥ 3.11，并且必须带 `tkinter`（图形界面依赖）。
2. **建虚拟环境** `.venv-build/`，装 `requests` `pycryptodome` `websockets` `pyinstaller`。
   环境建完会留一个 `.venv-ok` 标记；没有标记说明上次建到一半失败了，
   下次会自动重建 —— 避免用缺依赖的旧环境打出一个"看起来成功"的包。
3. **源码自检** —— 导入 `bitxk` 全部模块。源码有问题在这里就停下，
   不用等 PyInstaller 跑到一半才报错。
4. **清掉旧产物** —— `build/` `dist/` `dist-release/` 全部删除，避免旧文件混进新包。
5. **打文件夹版** —— `bitxk.spec`（GUI + CLI）和 `bitxk-cli.spec`（精简 CLI，不含 tkinter）。
6. **打单文件版** —— `bitxk-onefile.spec`，同时产出 GUI 与 CLI 两个 exe。
7. **组装发行目录** —— 拷贝产物、README、LICENSE、配置模板，清掉 `config.toml`／
   `.bitxk_session.json`／`__pycache__` 等不该分发的东西，生成 `QUICKSTART.txt`。
8. **编译安装包**（仅 Windows）—— 调 Inno Setup 的 `ISCC.exe`。
   找不到就跳过并提示安装方式，不影响其他产物。
9. **生成压缩包 + SHA256 校验和**。

任一步失败脚本立即退出并打印日志尾部，不会留下半成品冒充成功。

## 文件清单

| 文件 | 作用 |
| --- | --- |
| `build-release.ps1` | **Windows 一键打包**（入口） |
| `build-release.sh` | **macOS / Linux 一键打包**（入口） |
| `bitxk.spec` | PyInstaller 配置：文件夹版，含 GUI |
| `bitxk-cli.spec` | PyInstaller 配置：精简命令行版，排除 tkinter |
| `bitxk-onefile.spec` | PyInstaller 配置：单文件版，一 spec 出两个 exe |
| `installer.iss` | Inno Setup 安装包脚本 |
| `entry.py` | 打包后的程序入口，负责分发到 GUI 或 CLI |
| `config.example.toml` | 配置模板，会随发行包一起分发 |

> 除非你要改打包行为本身，否则只需要跑入口脚本，不用动其余文件。

## 单独编译安装包

`build-release.ps1` 会自动做这步。要手动跑：

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" `
    /DAppVersion=0.1.0 `
    /DSourceDir="D:\path\to\dist-release-stage" `
    /O"D:\path\to\output" `
    packaging\installer.iss
```

没装 Inno Setup 的话：

```powershell
winget install --id JRSoftware.InnoSetup -e
```

`installer.iss` 也接受 `/DNoChinese=1` —— 本机没有官方中文语言包时用它切英文界面，
`build-release.ps1` 会自动检测并加上。

## 关于体积

发行包只有 30MB 左右，因为**不打包浏览器内核**。「用浏览器登录」是直接驱动用户
机器上已有的 Chrome / Edge / Chromium / Brave，所以 PyInstaller 配置里排除了
numpy、pandas、PIL、tkinter 之外的其他 GUI 框架、以及 `cryptography` / `bcrypt` /
`cffi`（不是本项目的依赖，占了约 11MB）。

macOS 上还会执行 `strip` 精简二进制；Windows 上不能 `strip`，会在打包时因找不到
`strip` 而失败，所以 spec 里按平台做了判断。

## 构建环境要求

| | 要求 |
| --- | --- |
| Python | ≥ 3.11（3.13 已验证），必须带 tkinter |
| 网络 | 首次建虚拟环境需要能访问 PyPI |
| Windows 安装包 | Inno Setup 6（可选，缺了只是少一个产物） |
| 磁盘 | 约 600MB（虚拟环境 + 中间产物） |

**PyInstaller 不能交叉编译**：Windows 的 exe 必须在 Windows 上打，macOS 的
必须在 macOS 上打。想同时出两端产物，就在两台机器上各跑一遍对应脚本。

## 排错

| 现象 | 原因与处理 |
| --- | --- |
| pip 报 `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` | python.org 版 Python 的 openssl 默认 CA 路径不存在，而新建的虚拟环境里没有 `certifi`。脚本会自动装 `certifi` 解决；若仍失败见下条 |
| 上面这条持续出现 | 镜像站不可达或需代理。可临时换源：`pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple`，或设 `HTTPS_PROXY` |
| 提示找不到 Python 或版本过低 | 装 https://www.python.org/downloads/ ，勾选 *Add python.exe to PATH* |
| 提示缺少 tkinter | Windows：重装官方 Python 并勾选 tcl/tk；macOS：`brew install python-tk`；Ubuntu：`sudo apt install python3-tk` |
| pip 装依赖超时 | 换国内镜像：`pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple` |
| 源码自检失败 | 先用 `pytest` 排查，源码都跑不起来就不用谈打包了 |
| 提示未找到 Inno Setup | 见上一节；只是少一个安装包，其余产物照常 |
| 打包出的程序启动就闪退 | 用命令行版 `bitxk.exe` 跑一次，它会把 traceback 打在控制台 |
| Windows 上中文显示成乱码 | 已内置 ASCII 降级（GBK 控制台打不出 `✓` 会崩）；若仍异常请附上完整输出 |
| 改过 `build-release.ps1` 后报语法错误 | 该文件**必须带 UTF-8 BOM**。PowerShell 5.1 在中文系统上会把无 BOM 的 `.ps1` 按 GBK 读，里面的中文字符串会解析崩。用编辑器另存为「UTF-8 with BOM」即可 |
| Windows 上报 `$LASTEXITCODE` 判断不对 | 原生命令的 stderr 在 `$ErrorActionPreference='Stop'` 下会被升级成终止错误。脚本里用 `Probe { }` 包住这类调用（PyInstaller / ISCC 的进度日志都写在 stderr） |

## 发布流程

```bash
# 1. 改版本号
#    bitxk/__init__.py 里的 __version__

# 2. 跑测试
pytest -q

# 3. 两端各跑一次打包脚本
#    macOS:   bash packaging/build-release.sh
#    Windows: powershell -ExecutionPolicy Bypass -File packaging\build-release.ps1

# 4. 校验和确认
cat dist-release/SHA256SUMS.txt

# 5. 传到 Release
gh release create v0.1.0 dist-release/* --title "v0.1.0" --notes "..."
```

发布前建议在真机上至少确认一遍：GUI 能打开、`--version` 正常、
`check` 能连上校园网、浏览器登录能读到学号。
