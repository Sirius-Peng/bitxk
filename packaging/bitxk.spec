# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

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

APP_NAME = "BIT-Course-Helper"
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

# 明确排除，能显著减小体积
excludes = [
    "matplotlib", "numpy", "pandas", "scipy", "PIL", "PyQt5", "PyQt6",
    "PySide2", "PySide6", "wx", "IPython", "jupyter", "notebook",
    "pytest", "setuptools", "pip", "wheel", "docutils", "sphinx",
    "test", "unittest",
]

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
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ---------------------------------------------------------------------------
# 2) 图形界面版本：无控制台
# ---------------------------------------------------------------------------
exe_gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="bitxk-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # Windows 上双击不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe_console,
    exe_gui,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

# ---------------------------------------------------------------------------
# 3) macOS 额外产出 .app（双击即用）
# ---------------------------------------------------------------------------
if IS_MACOS:
    app = BUNDLE(
        exe_gui,
        a.binaries,
        a.zipfiles,
        a.datas,
        name=f"{APP_NAME}.app",
        icon=None,
        bundle_identifier="cn.edu.bit.bitxk.helper",
        info_plist={
            "CFBundleName": "BIT 选课助手",
            "CFBundleDisplayName": "BIT 选课助手",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            # 需要联网访问校内外服务
            "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": True},
        },
    )
