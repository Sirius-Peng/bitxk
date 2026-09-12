# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 精简命令行版（不含图形界面）。

一份 spec 同时产出两个可执行文件，覆盖两类用户：

* ``bitxk-gui`` —— 无控制台窗口。双击即开图形界面（Windows 上不会闪黑框）。
* ``bitxk``     —— 带控制台。给命令行 / 脚本 / 计划任务用。

在 macOS 上会额外为 GUI 版本生成 ``.app`` 包（双击进 Dock 图标）；
同时保留一个可直接执行的二进制，方便终端里调用。

构建：
    pyinstaller --clean --noconfirm packaging/bitxk.spec
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

PROJECT_ROOT = Path(SPECPATH).parent
IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"

APP_NAME = "BIT-Course-Helper-cli"
VERSION = "0.1.0"

# tkinter 不会自动被收全（尤其 ttk 的主题资源），显式声明。
hidden_imports = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
    "tkinter.messagebox",
    "tkinter.filedialog",
    "tkinter.scrolledtext",
    # CDP 驱动浏览器用
    "websockets",
    "websockets.sync.client",
    "websockets.asyncio.client",
    # 加密（SSO 登录方式用到）
    "Crypto",
    "Crypto.Cipher",
    "Crypto.Cipher.AES",
    "Crypto.Util.Padding",
    # 出口模块都是动态导入的，静态分析扫不到
    "bitxk.gui",
    "bitxk.browser",
    "bitxk.notify",
    "bitxk.poller",
    "bitxk.client",
    "bitxk.auth",
    "tomllib",
]
hidden_imports += collect_submodules("websockets")

# 明确排除，能显著减小体积。
#
# cryptography / bcrypt / cffi 这一组值得单独说明：它们**不是**我们的依赖 ——
# 本项目密码加密用的是 pycryptodome（Crypto 包）。它们只是恰好装在这台机器上，
# 被 PyInstaller 的打包钩子顺带收了进来，白白占了约 11MB。
# 已实测：屏蔽这几个模块后 ECB / CBC 两种加密模式依旧完全正常。
excludes = [
    # 无关的加密实现（我们不用它）
    "cryptography",
    "bcrypt",
    "cffi",
    "_cffi_backend",
    "nacl",
    # 数据处理 / 绘图，用不到
    "matplotlib", "numpy", "pandas", "scipy", "PIL",
    # 其它 GUI 框架
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    # 交互式环境
    "IPython", "jupyter", "notebook", "jupyter_client", "jupyter_core",
    # 开发期工具
    "pytest", "setuptools", "pip", "wheel", "pkg_resources",
    "docutils", "sphinx", "readline",
    # 标准库里的大件
    "test", "unittest", "lib2to3", "pydoc", "doctest", "pdb",
    "sqlite3", "xmlrpc", "ftplib", "imaplib", "smtplib", "poplib",
    "tarfile", "curses", "distutils", "ensurepip", "venv",
    # 图形界面相关（本版本刻意不含 GUI，约省 10MB）
    "tkinter", "_tkinter", "Tkinter", "turtle", "turtledemo", "idlelib",
]

# Windows 没有 strip 命令，强行开启会让 PyInstaller 抛 FileNotFoundError。
STRIP_BINARIES = IS_MACOS or sys.platform.startswith('linux')

block_cipher = None

a = Analysis(
    [str(PROJECT_ROOT / "packaging" / "entry.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        # 让用户能直接在发行包里看到配置模板与说明
        (str(PROJECT_ROOT / "README.md"), "."),
        (str(PROJECT_ROOT / "packaging" / "config.example.toml"), "."),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---------------------------------------------------------------------------
# 1) 命令行版本：带控制台
# ---------------------------------------------------------------------------
exe_console = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="bitxk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=STRIP_BINARIES,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe_console,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=STRIP_BINARIES,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
