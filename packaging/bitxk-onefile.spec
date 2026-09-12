# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 单文件便携版（Windows）。

产出**两个** exe，因为 Windows 上"一个 exe 同时干 GUI 和 CLI"做不到：

* ``bitxk-gui.exe`` —— Subsystem=GUI，不挂控制台。双击开图形界面，
  **不带任何参数时**不会弹黑框；但它的 stdout 是丢弃的，命令行用不了。
* ``bitxk.exe``     —— Subsystem=Console，有控制台。给命令行 / 脚本用，
  输出能正常打印（这是 ``console=True`` 才能保证的）。

证据：``console=False`` 的 PyInstaller 产物，PE 头的 Subsystem 是 2（GUI）。
实测直接调用 ``bitxk.exe --version`` 没有输出，只有 ``> file`` 重定向
或 ``cmd /c`` 包一层才拿得到 —— 所以不能拿它当 CLI 用。

构建：
    pyinstaller --clean --noconfirm packaging/bitxk-onefile.spec
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

PROJECT_ROOT = Path(SPECPATH).parent
IS_MACOS = sys.platform == "darwin"

hidden_imports = [
    "tkinter", "tkinter.ttk", "tkinter.font", "tkinter.messagebox",
    "tkinter.filedialog", "tkinter.scrolledtext",
    "websockets", "websockets.sync.client", "websockets.asyncio.client",
    "Crypto", "Crypto.Cipher", "Crypto.Cipher.AES", "Crypto.Util.Padding",
    "bitxk.gui", "bitxk.browser", "bitxk.notify", "bitxk.poller",
    "bitxk.client", "bitxk.auth",
    "tomllib",
]
hidden_imports += collect_submodules("websockets")

excludes = [
    "cryptography", "bcrypt", "cffi", "_cffi_backend", "nacl",
    "matplotlib", "numpy", "pandas", "scipy", "PIL",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "IPython", "jupyter", "notebook", "jupyter_client", "jupyter_core",
    "pytest", "setuptools", "pip", "wheel", "docutils", "sphinx", "readline",
    "test", "unittest", "lib2to3", "pydoc", "doctest", "pdb",
    "sqlite3", "xmlrpc", "ftplib", "imaplib", "smtplib", "poplib",
    "tarfile", "curses", "distutils", "ensurepip", "venv",
]

STRIP_BINARIES = IS_MACOS or sys.platform.startswith("linux")

a = Analysis(
    [str(PROJECT_ROOT / "packaging" / "entry.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
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
    cipher=None,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# ---- GUI 版：无控制台，双击即开 ----
exe_gui = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="bitxk-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=STRIP_BINARIES,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ---- CLI 版：带控制台，输出能正常打印 ----
exe_cli = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="bitxk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=STRIP_BINARIES,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
