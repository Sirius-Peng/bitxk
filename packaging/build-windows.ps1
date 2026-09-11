# ============================================================================
#  BIT 选课助手 —— Windows 一键构建脚本
#
#  用法：
#    1. 把整个项目文件夹放到这台 Windows 机器上
#    2. 右键本文件 → 「使用 PowerShell 运行」
#       （或在 PowerShell 里执行： powershell -ExecutionPolicy Bypass -File build-windows.ps1）
#    3. 等它跑完，产物在 dist\ 目录下
#
#  脚本会自动完成：检查 Python → 建虚拟环境 → 装依赖 → 装 PyInstaller
#  → 打包出两个 exe → 打成 zip。不需要你手动装任何东西。
# ============================================================================

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Version   = "0.1.0"
$AppName   = "BIT-Course-Helper"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path (Join-Path $ProjectRoot "bitxk"))) {
    $ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "    [!]  $msg" -ForegroundColor Yellow }
function Fail($msg) {
    Write-Host "`n[失败] $msg" -ForegroundColor Red
    exit 1
}

Write-Host "==============================================" -ForegroundColor White
Write-Host " BIT 选课助手  Windows 构建脚本  v$Version" -ForegroundColor White
Write-Host "==============================================" -ForegroundColor White
Write-Host "项目目录: $ProjectRoot"

# ---------------------------------------------------------------- 1. Python
Write-Step "检查 Python"
$python = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $ver -match "Python 3\.(\d+)") {
            $minor = [int]$Matches[1]
            if ($minor -ge 11) { $python = $candidate; break }
        }
    } catch { }
}
if (-not $python) {
    Fail @"
没有找到 Python 3.11 或更高版本。
请先从 https://www.python.org/downloads/ 安装（安装时务必勾选 "Add python.exe to PATH"），
然后重新运行本脚本。
"@
}
Write-Ok "$python -> $(& $python --version 2>&1)"

# ---------------------------------------------------------------- 2. 虚拟环境
Write-Step "创建虚拟环境（避免污染系统 Python）"
$venv = Join-Path $ProjectRoot ".venv-build"
if (-not (Test-Path $venv)) {
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "创建虚拟环境失败" }
}
$venvPy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Fail "虚拟环境不完整：$venvPy 不存在" }
Write-Ok $venvPy

# ---------------------------------------------------------------- 3. 依赖
Write-Step "安装依赖（首次约需 1-3 分钟）"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install --quiet requests pycryptodome websockets pyinstaller
if ($LASTEXITCODE -ne 0) { Fail "依赖安装失败，请检查网络（或配置 pip 镜像）" }
Write-Ok "requests / pycryptodome / websockets / pyinstaller"

# ---------------------------------------------------------------- 4. 自检
Write-Step "构建前自检"
Push-Location $ProjectRoot
& $venvPy -c "import tkinter; print('tkinter OK')" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    Fail "这个 Python 没有带 tkinter（图形界面需要它）。请重装官方版 Python 并勾选 tcl/tk 组件。"
}
Write-Ok "tkinter 可用（图形界面依赖）"
& $venvPy -c "import bitxk; print('bitxk', bitxk.__version__)"
if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "无法导入 bitxk，请确认项目文件完整" }

# ---------------------------------------------------------------- 5. 打包
Write-Step "PyInstaller 打包（约需 1-3 分钟）"
Remove-Item -Recurse -Force (Join-Path $ProjectRoot "dist") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $ProjectRoot "build") -ErrorAction SilentlyContinue
& $venvPy -m PyInstaller --clean --noconfirm `
    --distpath (Join-Path $ProjectRoot "dist") `
    --workpath (Join-Path $ProjectRoot "build") `
    (Join-Path $ProjectRoot "packaging\bitxk.spec")
if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "打包失败，请把上面的报错发给我" }

$outDir = Join-Path $ProjectRoot "dist\$AppName"
if (-not (Test-Path $outDir)) { Pop-Location; Fail "打包产物目录不存在：$outDir" }
Write-Ok "产物目录 $outDir"

# ---------------------------------------------------------------- 6. 验证
Write-Step "验证产物能跑"
$exe = Join-Path $outDir "bitxk.exe"
& $exe --version
if ($LASTEXITCODE -ne 0) { Write-Warn2 "bitxk.exe --version 返回非 0，请手工确认" }
else { Write-Ok "命令行版本可执行" }

if (Test-Path (Join-Path $outDir "bitxk-gui.exe")) {
    Write-Ok "图形界面版本已生成（bitxk-gui.exe）"
} else {
    Write-Warn2 "没有生成 bitxk-gui.exe"
}

# ---------------------------------------------------------------- 7. 打包 zip
Write-Step "打成 zip"
$zip = Join-Path $ProjectRoot "dist\BIT-Course-Helper-$Version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Copy-Item (Join-Path $ProjectRoot "packaging\config.example.toml") $outDir -ErrorAction SilentlyContinue
Compress-Archive -Path $outDir -DestinationPath $zip -CompressionLevel Optimal
Write-Ok $zip

# ---------------------------------------------------------------- 完成
Write-Host "`n==============================================" -ForegroundColor Green
Write-Host " 构建完成" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host "发行包: $zip"
Write-Host "`n请把这个 zip 文件回传到 mac，路径："
Write-Host "  /Users/nh_y/Desktop/BIT_Course/dist/"
Write-Host "`n或者直接告诉我 zip 放哪了。" -ForegroundColor White
Pop-Location
