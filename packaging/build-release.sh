#!/usr/bin/env bash
# =============================================================================
#  BIT 选课助手 —— 一键打包脚本（macOS / Linux）
#
#  一条命令走完全流程：
#     检查环境 → 建虚拟环境 → 装依赖 → 打包四种产物 → 组装发行目录
#     → 生成 .app（macOS）→ 生成 Release 压缩包 → 校验
#
#  产物（全部落在 dist-release/ 下）：
#     BIT-Course-Helper-<版本>-macos-arm64.tar.gz     （或 -macos-x64 / -linux-x64）
#     bitxk-gui                                       单文件便携版（图形界面）
#     bitxk                                           单文件便携版（命令行）
#     SHA256SUMS.txt                                  校验和
#
#  用法：
#     bash packaging/build-release.sh
#     bash packaging/build-release.sh --version 0.1.0
#     bash packaging/build-release.sh --skip-portable
#
#  说明：为什么 macOS 用 tar.gz 而不是 zip —— 压缩包里有符号链接（.app 框架），
#       zip 会把符号链接按实体文件重复打包，体积翻倍。
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. 定位项目根目录
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
APP_NAME="BIT-Course-Helper"
WORK_DIR="$ROOT/build"
DIST_DIR="$ROOT/dist"
OUT_DIR="$ROOT/dist-release"

if [ ! -f "$ROOT/bitxk/__init__.py" ]; then
    echo "找不到项目根目录（应包含 bitxk/__init__.py）。当前推断为：$ROOT" >&2
    exit 1
fi

VERSION=""
SKIP_PORTABLE=0
SKIP_APP=0
while [ $# -gt 0 ]; do
    case "$1" in
        --version)       VERSION="$2"; shift 2 ;;
        --skip-portable) SKIP_PORTABLE=1; shift ;;
        --skip-app)      SKIP_APP=1; shift ;;
        -h|--help)       sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 1 ;;
    esac
done

step() { printf '\n=== [%s] %s ===\n' "$1" "$2"; }
ok()   { printf '    [OK] %s\n' "$1"; }
warn() { printf '    [!]  %s\n' "$1"; }
fail() { printf '\n[失败] %s\n' "$1" >&2; exit 1; }

echo ''
echo '============================================================'
echo ' BIT 选课助手 · macOS / Linux 打包脚本'
echo '============================================================'
echo "项目目录：$ROOT"

# 版本号
if [ -z "$VERSION" ]; then
    VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$ROOT/bitxk/__init__.py" | head -1)"
    [ -n "$VERSION" ] || VERSION="0.0.0"
fi
echo "版本号  ：$VERSION"

# 平台标识
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Darwin)
        case "$ARCH" in
            arm64) PLATFORM="macos-arm64" ;;
            x86_64) PLATFORM="macos-x64" ;;
            *) PLATFORM="macos-$ARCH" ;;
        esac ;;
    Linux) PLATFORM="linux-$ARCH" ;;
    *) PLATFORM="$(echo "$OS-$ARCH" | tr '[:upper:]' '[:lower:]')" ;;
esac
echo "平台    ：$PLATFORM"
echo "产物    ：dist-release/$APP_NAME-$VERSION-$PLATFORM.tar.gz"

mkdir -p "$WORK_DIR"

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
step 1 '检查 Python'

PYTHON=""
for cand in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            PYTHON="$cand"; break
        fi
    fi
done
[ -n "$PYTHON" ] || fail '未找到 Python 3.11 或更高版本。请先安装：https://www.python.org/downloads/'
ok "$PYTHON -> $("$PYTHON" --version 2>&1)"

# 打包需要 tkinter（图形界面）
if ! "$PYTHON" -c 'import tkinter' 2>/dev/null; then
    if [ "$OS" = "Darwin" ]; then
        fail '这个 Python 缺少 tkinter。用 Homebrew 安装：brew install python-tk'
    else
        fail '这个 Python 缺少 tkinter。Debian/Ubuntu：sudo apt install python3-tk'
    fi
fi
ok 'tkinter 可用'

# ---------------------------------------------------------------------------
# 2. 虚拟环境 + 依赖
# ---------------------------------------------------------------------------
step 2 '准备虚拟环境与依赖'

VENV="$ROOT/.venv-build"
VENV_PY="$VENV/bin/python"
VENV_STAMP="$VENV/.venv-ok"

# 虚拟环境存在但没有完成标记 —— 说明上次建到一半失败了，重建，
# 避免用一个缺依赖的旧环境打出自欺欺人的包
if [ -x "$VENV_PY" ] && [ ! -f "$VENV_STAMP" ]; then
    warn '检测到未完成的虚拟环境，重建'
    rm -rf "$VENV"
fi
if [ ! -x "$VENV_PY" ]; then
    "$PYTHON" -m venv "$VENV"
    [ -x "$VENV_PY" ] || fail "虚拟环境创建失败：$VENV_PY 不存在"
fi
ok "venv: $VENV_PY"

# --- 根证书自举 -------------------------------------------------------------
# python.org 版的 Python 常常没有可用的系统 CA（openssl 默认路径
# .../etc/openssl/cert.pem 不存在），而新建的虚拟环境里也没有 certifi，
# 于是 pip 走 HTTPS 时会报：
#   CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate
# 系统 Python 因为装了 certifi 反而是好的 —— 这就是"昨天能装、今天装不上"的原因。
#
# 处理办法：先把 certifi 装进虚拟环境，再把它显式喂给 pip（--cert / PIP_CERT）。
# pip 用的是自带的 vendored certifi，装了 certifi 也不会自动生效，必须显式指定。
pip_flags=(--quiet --disable-pip-version-check)

# 从 pip 自己的配置里读出镜像 host，这样换任何源都能自适应，
# 不用把某个镜像地址硬编码进脚本
read -r -a _pip_hosts <<< "$("$VENV_PY" -m pip config list 2>/dev/null \
    | sed -n 's/^[^=]*index[-_]url[^=]*=[^h]*https\{0,1\}:\/\/\([^/]*\).*/\1/p' | head -1)"
trusted=()
[ -n "${_pip_hosts[0]:-}" ] && trusted+=(--trusted-host "${_pip_hosts[0]}")
# 找不到就用官方源兜底
[ ${#trusted[@]} -eq 0 ] && trusted=(--trusted-host pypi.org --trusted-host files.pythonhosted.org)

if ! "$VENV_PY" -c 'import certifi' >/dev/null 2>&1; then
    # 最快的办法：系统 Python 通常本来就装了 certifi，直接把那个文件拿来用，
    # 完全不用联网（新建的虚拟环境只是没继承它而已）
    sys_cert="$("$PYTHON" -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
    if [ -n "$sys_cert" ] && [ -f "$sys_cert" ]; then
        warn "虚拟环境缺少根证书，复用系统 Python 的：$sys_cert"
        "$VENV_PY" -m pip install "${pip_flags[@]}" --cert "$sys_cert" certifi >/dev/null 2>&1 || true
    fi
fi

if ! "$VENV_PY" -c 'import certifi' >/dev/null 2>&1; then
    warn '需要下载根证书（首次可能需要一小会儿）'
    # 这一步不得不放宽校验：正是因为没有证书才装不上证书
    "$VENV_PY" -m pip install "${pip_flags[@]}" "${trusted[@]}" certifi \
        || "$VENV_PY" -m pip install "${pip_flags[@]}" \
               --trusted-host pypi.org --trusted-host files.pythonhosted.org certifi \
        || fail '无法获取 certifi（根证书）。请检查网络/代理，或确认 pip 镜像可用。'
    "$VENV_PY" -c 'import certifi' >/dev/null 2>&1 || fail 'certifi 安装后仍无法导入'
fi
export PIP_CERT="$("$VENV_PY" -c 'import certifi; print(certifi.where())')"
[ -f "$PIP_CERT" ] || fail "certifi 证书文件不存在：$PIP_CERT"
ok "根证书就绪：$PIP_CERT"

"$VENV_PY" -m pip install "${pip_flags[@]}" --upgrade pip \
    || fail 'pip 升级失败（网络或镜像问题）'
"$VENV_PY" -m pip install "${pip_flags[@]}" \
    requests pycryptodome websockets pyinstaller \
    || fail '依赖安装失败（网络或镜像问题）'
"$VENV_PY" - <<'PY' || fail '依赖安装后仍无法导入，请检查上面的 pip 输出'
import PyInstaller, requests, Crypto, websockets, tkinter
print('    [OK] 依赖就绪 PyInstaller ' + PyInstaller.__version__)
PY

# 打包前先确认源码本身能导入 —— 早失败好过 PyInstaller 跑到一半才炸
( cd "$ROOT" && "$VENV_PY" - <<'PY'
import bitxk, bitxk.gui, bitxk.cli, bitxk.poller, bitxk.client, bitxk.browser, bitxk.auth
print('    [OK] 源码可导入 bitxk ' + bitxk.__version__ + '（全部模块）')
PY
) || fail 'bitxk 包导入失败，请确认项目文件完整并先用 pytest 排查'

# 全部就绪，留下完成标记
: > "$VENV_STAMP"

# ---------------------------------------------------------------------------
# 3. 清理旧产物
# ---------------------------------------------------------------------------
step 3 '清理旧产物'
rm -rf "$DIST_DIR" "$OUT_DIR" "$WORK_DIR"
mkdir -p "$WORK_DIR"
ok '已清理 build/ dist/ dist-release/'

# ---------------------------------------------------------------------------
# 4. 打包：文件夹版（GUI + CLI）
# ---------------------------------------------------------------------------
step 4 '打包文件夹版（GUI + 命令行）'
for spec in bitxk.spec bitxk-cli.spec; do
    [ -f "$ROOT/packaging/$spec" ] || fail "缺少 spec 文件：packaging/$spec"
    "$VENV_PY" -m PyInstaller --clean --noconfirm \
        --distpath "$DIST_DIR" --workpath "$WORK_DIR" \
        "$ROOT/packaging/$spec" > "$WORK_DIR/$spec.log" 2>&1 \
        || { echo "    PyInstaller 失败，日志尾部：" >&2; tail -15 "$WORK_DIR/$spec.log" >&2; exit 1; }
    ok "$spec"
done

GUI_DIR="$DIST_DIR/$APP_NAME"
CLI_DIR="$DIST_DIR/$APP_NAME-cli"

# ---------------------------------------------------------------------------
# 5. 打包：单文件便携版
# ---------------------------------------------------------------------------
ONE_DIR="$WORK_DIR/dist-onefile"
if [ "$SKIP_PORTABLE" -eq 0 ]; then
    step 5 '打包单文件便携版'
    "$VENV_PY" -m PyInstaller --clean --noconfirm \
        --distpath "$ONE_DIR" --workpath "$WORK_DIR/onefile" \
        "$ROOT/packaging/bitxk-onefile.spec" > "$WORK_DIR/onefile.log" 2>&1 \
        || { echo "    PyInstaller 失败，日志尾部：" >&2; tail -15 "$WORK_DIR/onefile.log" >&2; exit 1; }
    ok 'bitxk-gui / bitxk'
else
    step 5 '跳过单文件便携版（--skip-portable）'
fi

# ---------------------------------------------------------------------------
# 6. 组装发行目录
# ---------------------------------------------------------------------------
step 6 '组装发行目录'
STAGE="$WORK_DIR/release"
rm -rf "$STAGE"
mkdir -p "$STAGE"

# macOS 上 PyInstaller 生成的是 .app + 同名可执行文件，一起拷
if [ -d "$GUI_DIR" ]; then
    (cd "$GUI_DIR" && cp -R . "$STAGE"/)
else
    fail "未找到 $GUI_DIR"
fi
cp "$ROOT/README.md" "$STAGE/" 2>/dev/null || warn 'README.md 缺失'
cp "$ROOT/LICENSE"   "$STAGE/" 2>/dev/null || warn 'LICENSE 缺失'
cp "$ROOT/packaging/config.example.toml" "$STAGE/"

# 注意：这里**不**再放一份精简命令行版的目录。
# 文件夹版里已经同时有 bitxk-gui 和 bitxk 两个程序，共用同一份 _internal/；
# 而精简版要再带一整套 _internal/（约 22MB），发行包会直接翻倍。
# 单文件便携版（portable/）则只多带 13MB，两者不冲突。
if [ -d "$CLI_DIR" ]; then
    ok '精简命令行版已构建，不放进发行包以避免体积翻倍'
fi

if [ "$SKIP_PORTABLE" -eq 0 ]; then
    mkdir -p "$STAGE/portable"
    for exe in bitxk-gui bitxk; do
        [ -f "$ONE_DIR/$exe" ] && cp "$ONE_DIR/$exe" "$STAGE/portable/" || warn "缺少 $exe"
    done
    chmod +x "$STAGE/portable/"* 2>/dev/null || true
    ok 'portable/ 已加入'
fi

# 清掉不该发行的东西
find "$STAGE" \( -name 'config.toml' -o -name '*.pyc' -o -name '.bitxk_session.json' \) \
    -delete 2>/dev/null || true
find "$STAGE" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
ok '已清理配置文件/缓存'

cat > "$STAGE/QUICKSTART.txt" <<EOF
$APP_NAME v$VERSION — $PLATFORM
========================================

包含内容
--------
  $APP_NAME.app / bitxk-gui     图形界面，双击这个
  bitxk                         命令行版（和图形界面共用同一份运行库）
  portable/                      单文件便携版（免解压，各一个文件）
  config.example.toml           配置模板
  README.md / LICENSE

目录里有 bitxk-gui 和 bitxk 两个程序，它们公用同一个 _internal/，
所以两个都能用、体积只算一份。

无需安装浏览器内核 —— "用浏览器登录"直接驱动你机器上已有的
Chrome / Edge / Chromium / Brave，这也是本包只有 30MB 左右的原因。

macOS 首次运行
--------------
  未签名的应用会被 Gatekeeper 拦下。任选一种：
    1) 右键点图标 → 打开 → 再点「打开」
    2) 终端执行：xattr -dr com.apple.quarantine "$APP_NAME.app"

快速开始
--------
1. 打开图形界面
2. 点「用浏览器登录」。会开一个浏览器窗口，请在里面登录，
   并等页面跳到选课系统首页（能看到「开始选课」按钮）后再关闭窗口。
   学号、姓名、批次都会从页面自动读取，不需要手工填写。
3. 在右栏查询课程：「查询」+ 勾选「查全部类型」会遍历全部 8 种类型，
   并自动翻页取全（单个类型可能有 200+ 门课）。
   未开放的课程类型会显示为空，这是正常的，不是错误。
4. 双击查询结果里的一行，把课程加进左侧的任务清单。
5. 保持「试跑」勾选，点「开始抢课」——只观察余量，不会提交。
6. 确认课程无误后，取消「试跑」，正式开始。

命令行用法
----------
  bitxk --help
  bitxk browser-login            用浏览器登录一次
  bitxk list                     查看余量
  bitxk grab --dry-run           只观察不提交
  bitxk grab                     正式抢课

选课系统地址
------------
  http://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/*default/index.do

可原样粘进 config.toml 的 api_base；工具会自动升级成 https ——
这是必须的，因为 http 下的 POST 会被服务器 302 降级成 GET 并丢掉参数。

注意
----
  * 选课系统只能在校园网内访问。若不可达，工具会保留你的登录态并明确
    说明，不会要你重新输密码。
  * token 约 15-20 分钟失效。轮询时会自动重登，但界面放置久了可能
    需要重新点一次「用浏览器登录」。
  * 请勿把轮询间隔调到 1 秒以下。默认 2 秒是刻意保守的取值。
EOF
ok 'QUICKSTART.txt'

# ---------------------------------------------------------------------------
# 7. 生成 Release 压缩包
# ---------------------------------------------------------------------------
step 7 '生成 Release 压缩包'
mkdir -p "$OUT_DIR"

TARBALL="$OUT_DIR/$APP_NAME-$VERSION-$PLATFORM.tar.gz"
# 用 -C 进到 STAGE 再打包，避免压缩包里多一层 work/release/ 前缀
tar -czf "$TARBALL" -C "$STAGE" .
ok "$(basename "$TARBALL")  ($(du -h "$TARBALL" | cut -f1))"

if [ "$SKIP_PORTABLE" -eq 0 ]; then
    [ -f "$ONE_DIR/bitxk-gui" ] && cp "$ONE_DIR/bitxk-gui" \
        "$OUT_DIR/$APP_NAME-$VERSION-$PLATFORM-portable-gui"
    [ -f "$ONE_DIR/bitxk" ] && cp "$ONE_DIR/bitxk" \
        "$OUT_DIR/$APP_NAME-$VERSION-$PLATFORM-portable-cli"
    :
fi

# ---------------------------------------------------------------------------
# 8. 校验和
# ---------------------------------------------------------------------------
step 8 '生成校验和'
SUMS="$OUT_DIR/SHA256SUMS.txt"
: > "$SUMS"
( cd "$OUT_DIR" && for f in *; do
    [ "$f" = "SHA256SUMS.txt" ] && continue
    [ -f "$f" ] || continue
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$f" >> "$SUMS"
    else
        sha256sum "$f" >> "$SUMS"
    fi
done )
sed 's/^/    /' "$SUMS"
ok 'SHA256SUMS.txt'

# ---------------------------------------------------------------------------
# 9. 汇总
# ---------------------------------------------------------------------------
echo ''
echo '============================================================'
echo ' 打包完成'
echo '============================================================'
echo "输出目录：$OUT_DIR"
echo ''
ls -lh "$OUT_DIR" | tail -n +2 | awk '{ printf "  %-56s %s\n", $9, $5 }'
echo ''
echo '提示：'
echo "  * 分发 $TARBALL 即可（解压即用）"
echo '  * portable-* 是免解压的单文件版，启动比文件夹版慢约 1 秒'
echo '  * SHA256SUMS.txt 可用于校验下载完整性'
echo ''
