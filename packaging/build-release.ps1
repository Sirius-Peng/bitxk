# =============================================================================
#  BIT 选课助手 —— 一键打包脚本（Windows）
#
#  一条命令走完全流程：
#     检查环境 → 建虚拟环境 → 装依赖 → 打包四种产物 → 组装发行目录
#     → 编译安装包 → 生成 Release zip → 校验
#
#  产物（全部落在 dist-release\ 下）：
#     BIT-Course-Helper-<版本>-windows-x64.zip    文件夹版（解压即用）
#     BIT-Course-Helper-<版本>-setup.exe          Inno Setup 安装包
#     bitxk-gui.exe                               单文件便携版（图形界面）
#     bitxk.exe                                   单文件便携版（命令行）
#     SHA256SUMS.txt                              校验和
#
#  用法：
#     右键「使用 PowerShell 运行」
#     或： powershell -ExecutionPolicy Bypass -File packaging\build-release.ps1
#
#  可选参数：
#     -Version 0.1.0      指定版本号（默认读 bitxk\__init__.py）
#     -SkipInstaller      跳过 Inno Setup 安装包
#     -SkipPortable       跳过单文件便携版
#     -Clean              先清掉 build\ 中间产物再开始（默认就清）
# =============================================================================

[CmdletBinding()]
param(
    [string]$Version = "",
    [switch]$SkipInstaller,
    [switch]$SkipPortable
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------------------
# 0. 定位项目根目录
# ---------------------------------------------------------------------------
# 用 $PSScriptRoot 而不是 $MyInvocation.MyCommand.Path —— 后者在
# 「powershell -File 脚本」这种调用方式下是空的，会把根目录误判成 C:\。
$Candidates = @()
if ($PSScriptRoot)          { $Candidates += $PSScriptRoot }
if ($PSCommandPath)         { $Candidates += (Split-Path -Parent $PSCommandPath) }
$Candidates += (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$Candidates += (Get-Location).Path

# 从每个候选目录向上找 bitxk\__init__.py，最多找 4 层；
# 每层也看一下 src\ 子目录 —— 从 GitHub 下载的源码包解压后常常多套一层
$Root = $null
foreach ($start in $Candidates) {
    if (-not $start -or -not (Test-Path $start)) { continue }
    $dir = $start
    for ($i = 0; $i -lt 4 -and $dir; $i++) {
        foreach ($probe in @($dir, (Join-Path $dir 'src'))) {
            if (Test-Path (Join-Path $probe 'bitxk\__init__.py')) { $Root = $probe; break }
        }
        if ($Root) { break }
        $parent = Split-Path -Parent $dir
        if ($parent -eq $dir) { break }
        $dir = $parent
    }
    if ($Root) { break }
}
if (-not $Root) {
    throw "找不到项目根目录（应由 bitxk\__init__.py 标识）。`n请把本脚本放在项目的 packaging\ 目录下运行。"
}

$AppName = 'BIT-Course-Helper'
$WorkDir = Join-Path $Root 'build'
$DistDir = Join-Path $Root 'dist'
$OutDir = Join-Path $Root 'dist-release'

function Step($n, $msg) {
    Write-Host ''
    Write-Host "=== [$n] $msg ===" -ForegroundColor Cyan
}
function Ok($msg)   { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    [!]  $msg" -ForegroundColor Yellow }
function Fail($msg) {
    Write-Host ''
    Write-Host "[失败] $msg" -ForegroundColor Red
    exit 1
}

# 运行一段"可能会往 stderr 说话"的代码。
#
# PowerShell 5.1 里 $ErrorActionPreference='Stop' 会把原生命令写到 stderr 的
# 普通输出（比如 python 的 Traceback）升级成**终止错误** —— 于是"探测某个模块
# 在不在"这种正常失败会直接把脚本打断，永远走不到兜底分支。
# 这里临时切回 Continue，只管退出码。
function Probe {
    param([Parameter(Mandatory)][scriptblock]$Body)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Body } finally { $ErrorActionPreference = $old }
}

# 跑一段 python 代码，返回 @{ Ok=..; Output=.. }，**保留输出**以便失败时能看见原因
function Invoke-Py {
    param([Parameter(Mandatory)][string]$Python, [Parameter(Mandatory)][string]$Code)
    $out = Probe { & $Python -c $Code 2>&1 }
    return @{ Ok = ($script:LASTEXITCODE -eq 0); Output = ($out | ForEach-Object { "$_" }) }
}

# 探测某个模块能否导入（失败时不打印，仅用于分支判断）
function Test-PyModule {
    param([string]$Python, [string]$Module)
    return (Invoke-Py -Python $Python -Code "import $Module").Ok
}

# 跑一个外部命令，输出**实时**写进日志；失败时打印日志尾部并中止。
#
# 为什么不用 Start-Process -RedirectStandardOutput：
#   那个文件要等进程结束才真正落盘，中途去看永远是空的 —— 打包卡住时
#   完全没法判断卡在哪一步。这里用管道 + StreamWriter，逐行实时刷新，
#   同时把输出显示在控制台，用户能看见进度。
function Invoke-Logged {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$LogName
    )
    $log = Join-Path $WorkDir $LogName
    $writer = New-Object System.IO.StreamWriter($log, $false, (New-Object System.Text.UTF8Encoding $false))
    $writer.AutoFlush = $true
    try {
        # 必须用 Probe 包住：PyInstaller / ISCC 的进度日志是写到 **stderr** 的，
        # 在 $ErrorActionPreference='Stop' 下每一条都会被升级成终止错误并打断脚本。
        $output = Probe { & $Exe @Arguments 2>&1 }
        $code = $script:LASTEXITCODE
        foreach ($line in $output) {
            $text = "$line"
            $writer.WriteLine($text)
            Write-Host "      $text" -ForegroundColor DarkGray
        }
    } finally {
        $writer.Close()
    }
    if ($code -ne 0) {
        Write-Host "    命令失败（exit=$code）：$Exe $($Arguments -join ' ')" -ForegroundColor Red
        Write-Host "    完整日志：$log" -ForegroundColor Red
        exit 1
    }
    return $code
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor White
Write-Host ' BIT 选课助手 · Windows 打包脚本' -ForegroundColor White
Write-Host '============================================================' -ForegroundColor White
Write-Host "项目目录：$Root"

# 版本号：优先用参数，否则从源码读
if (-not $Version) {
    $initPy = Join-Path $Root 'bitxk\__init__.py'
    $m = Select-String -Path $initPy -Pattern '__version__\s*=\s*"([^"]+)"'
    if ($m) { $Version = $m.Matches[0].Groups[1].Value } else { $Version = '0.0.0' }
}
Write-Host "版本号  ：$Version"

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
Step 1 '检查 Python'

$Python = $null
foreach ($cand in @('python', 'python3', 'py')) {
    try {
        $out = & $cand --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$out" -match 'Python 3\.(\d+)') {
            if ([int]$Matches[1] -ge 11) { $Python = $cand; break }
        }
    } catch { }
}
if (-not $Python) {
    Fail @'
未找到 Python 3.11 或更高版本。
请从 https://www.python.org/downloads/ 安装（安装时务必勾选 "Add python.exe to PATH"），
然后重新运行本脚本。
'@
}
Ok "$Python -> $(& $Python --version 2>&1)"

# 打包需要 tkinter（图形界面）与 zip 模块
& $Python -c "import tkinter, zipfile" 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail '这个 Python 缺少 tkinter（图形界面依赖）。请重装官方版 Python 并勾选 tcl/tk 组件。'
}
Ok 'tkinter 可用'

# ---------------------------------------------------------------------------
# 2. 虚拟环境 + 依赖
# ---------------------------------------------------------------------------
Step 2 '准备虚拟环境与依赖'

$Venv = Join-Path $Root '.venv-build'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$VenvStamp = Join-Path $Venv '.venv-ok'

# 虚拟环境存在但没有完成标记 —— 说明上次建到一半失败了，重建，
# 避免用一个缺依赖的旧环境打出自欺欺人的包
if ((Test-Path $VenvPy) -and -not (Test-Path $VenvStamp)) {
    Warn '检测到未完成的虚拟环境，重建'
    Remove-Item -Recurse -Force $Venv -ErrorAction SilentlyContinue
}
if (-not (Test-Path $VenvPy)) {
    & $Python -m venv $Venv
    if (-not (Test-Path $VenvPy)) { Fail "虚拟环境创建失败：$VenvPy 不存在" }
}
Ok "venv: $VenvPy"

# --- 根证书自举 -------------------------------------------------------------
# python.org 版的 Python 常常没有可用的系统 CA（openssl 默认路径
# ...\etc\openssl\cert.pem 不存在），而新建的虚拟环境里也没有 certifi，
# 于是 pip 走 HTTPS 时会报：
#   CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate
# 系统 Python 因为装了 certifi 反而是好的 —— 这就是"昨天能装、今天装不上"的原因。
#
# 处理办法：先把 certifi 装进虚拟环境，再把它显式喂给 pip（--cert / PIP_CERT）。
# pip 用的是自带的 vendored certifi，装了 certifi 也不会自动生效，必须显式指定。
# 不加 --quiet：镜像慢的时候要能看见进度，否则像卡死一样毫无信息。
$PipFlags = @('-m', 'pip', 'install', '--disable-pip-version-check', '--progress-bar', 'off')

# 从 pip 自己的配置里读出镜像 host，这样换任何源都能自适应
$pipHost = $null
$cfgLine = Probe { & $VenvPy -m pip config list 2>&1 | Select-String -Pattern 'index[-_]url' | Select-Object -First 1 }
if ($cfgLine -and ("$cfgLine" -match 'https?://([^/''"]+)')) { $pipHost = $Matches[1] }
$trustedHosts = if ($pipHost) { @($pipHost) } else { @('pypi.org', 'files.pythonhosted.org') }
Ok "pip 镜像：$(if ($pipHost) { $pipHost } else { '（默认官方源）' })"

if (-not (Test-PyModule -Python $VenvPy -Module 'certifi')) {
    # 最快的办法：系统 Python 通常本来就装了 certifi，直接把那个文件拿来用，
    # 完全不用联网（新建的虚拟环境只是没继承它而已）
    $sysCert = Probe { & $Python -c "import certifi; print(certifi.where())" 2>&1 | Select-Object -First 1 }
    if ($sysCert) { $sysCert = "$sysCert".Trim() }
    if ($sysCert -and (Test-Path $sysCert)) {
        Warn "虚拟环境缺少根证书，复用系统 Python 的：$sysCert"
        Probe { & $VenvPy @PipFlags '--cert' $sysCert 'certifi' 2>&1 | Out-Null }
    }
}

if (-not (Test-PyModule -Python $VenvPy -Module 'certifi')) {
    Warn '需要下载根证书（首次可能需要一小会儿）'
    $gotCertifi = $false
    foreach ($m in ($trustedHosts + @('pypi.org', 'files.pythonhosted.org'))) {
        # 这一步不得不放宽校验：正是因为没有证书才装不上证书
        $code = Probe { & $VenvPy @PipFlags '--trusted-host' $m 'certifi' 2>&1 | Out-Null; $script:LASTEXITCODE }
        if ($code -eq 0) { $gotCertifi = $true; break }
    }
    if (-not $gotCertifi) { Fail '无法获取 certifi（根证书）。请检查网络/代理，或确认 pip 镜像可用。' }
    if (-not (Test-PyModule -Python $VenvPy -Module 'certifi')) { Fail 'certifi 安装后仍无法导入' }
}
# 喂给 pip —— 之后所有 pip 调用都会自动带上
$env:PIP_CERT = "$(Probe { & $VenvPy -c "import certifi; print(certifi.where())" 2>&1 | Select-Object -First 1 })".Trim()
if (-not (Test-Path $env:PIP_CERT)) { Fail "certifi 证书文件不存在：$env:PIP_CERT" }
Ok "根证书就绪：$env:PIP_CERT"

Invoke-Logged -Exe $VenvPy -LogName 'pip.log' -Arguments ($PipFlags + @('--upgrade', 'pip'))
Invoke-Logged -Exe $VenvPy -LogName 'pip-deps.log' -Arguments ($PipFlags + @(
    'requests', 'pycryptodome', 'websockets', 'pyinstaller'
))
$deps = Invoke-Py -Python $VenvPy -Code "import PyInstaller, requests, Crypto, websockets, tkinter; print('依赖就绪 PyInstaller ' + PyInstaller.__version__)"
if (-not $deps.Ok) {
    $deps.Output | ForEach-Object { Write-Host "      $_" -ForegroundColor Red }
    Fail '依赖安装后仍无法导入（上面是 Python 的原话）'
}
Ok $deps.Output[0]

# 打包前先确认源码本身能导入 —— 早失败好过 PyInstaller 跑到一半才炸
Push-Location $Root
$src = Invoke-Py -Python $VenvPy -Code "import bitxk, bitxk.gui, bitxk.cli, bitxk.poller, bitxk.client, bitxk.browser, bitxk.auth; print('源码可导入 bitxk ' + bitxk.__version__ + '（全部模块）')"
Pop-Location
$importOk = $src.Ok
if (-not $importOk) { $src.Output | ForEach-Object { Write-Host "      $_" -ForegroundColor Red } }
if (-not $importOk) { Fail '无法导入 bitxk 包，请确认项目文件完整（bitxk\ 目录）并先用 pytest 排查' }

# 全部就绪，留下完成标记
[System.IO.File]::WriteAllText($VenvStamp, 'ok')

# ---------------------------------------------------------------------------
# 3. 清掉旧产物
# ---------------------------------------------------------------------------
Step 3 '清理旧产物'
foreach ($d in @($DistDir, $OutDir, $WorkDir)) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue }
}
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
Ok '已清理 build\ dist\ dist-release\'

# ---------------------------------------------------------------------------
# 4. 打包：文件夹版（GUI + CLI）
# ---------------------------------------------------------------------------
Step 4 '打包文件夹版（GUI + 命令行）'
foreach ($spec in @('bitxk.spec', 'bitxk-cli.spec')) {
    $specPath = Join-Path $Root "packaging\$spec"
    if (-not (Test-Path $specPath)) { Fail "缺少 spec 文件：$specPath" }
    Invoke-Logged -Exe $VenvPy -LogName "$spec.log" -Arguments @(
        '-m', 'PyInstaller', '--clean', '--noconfirm',
        '--distpath', $DistDir, '--workpath', $WorkDir, $specPath
    )
    Ok $spec
}

$GuiDir = Join-Path $DistDir $AppName
$CliDir = Join-Path $DistDir "$AppName-cli"
if (-not (Test-Path (Join-Path $GuiDir 'bitxk-gui.exe'))) { Fail "未生成 $GuiDir\bitxk-gui.exe" }
Ok "产物：$GuiDir"

# ---------------------------------------------------------------------------
# 5. 打包：单文件便携版
# ---------------------------------------------------------------------------
$OneDir = Join-Path $WorkDir 'dist-onefile'
if (-not $SkipPortable) {
    Step 5 '打包单文件便携版'
    Invoke-Logged -Exe $VenvPy -LogName 'onefile.log' -Arguments @(
        '-m', 'PyInstaller', '--clean', '--noconfirm',
        '--distpath', $OneDir, '--workpath', (Join-Path $WorkDir 'onefile'), `
        (Join-Path $Root 'packaging\bitxk-onefile.spec')
    )
    if (-not (Test-Path (Join-Path $OneDir 'bitxk-gui.exe'))) { Fail '未生成单文件 bitxk-gui.exe' }
    Ok 'bitxk-gui.exe / bitxk.exe'
} else {
    Step 5 '跳过单文件便携版（-SkipPortable）'
}

# ---------------------------------------------------------------------------
# 6. 组装发行目录
# ---------------------------------------------------------------------------
Step 6 '组装发行目录'
$Stage = Join-Path $WorkDir 'release'
Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

# 6.1 文件夹版内容
Copy-Item -Recurse -Force (Join-Path $GuiDir '*') $Stage
Copy-Item -Force (Join-Path $Root 'README.md') $Stage
Copy-Item -Force (Join-Path $Root 'LICENSE')   $Stage
Copy-Item -Force (Join-Path $Root 'packaging\config.example.toml') $Stage

# 注意：这里**不**再放一份精简命令行版的目录。
# 文件夹版里已经同时有 bitxk-gui.exe 和 bitxk.exe，共用同一份 _internal\；
# 而精简版要再带一整套 _internal\（约 22MB），发行包会直接翻倍。
# 单文件便携版（portable\）则只多带 14MB，两者不冲突。
if (Test-Path $CliDir) { Ok "精简命令行版已构建（$CliDir），不放进发行包以避免体积翻倍" }

# 6.2 单文件便携版
if (-not $SkipPortable) {
    $pf = Join-Path $Stage 'portable'
    New-Item -ItemType Directory -Force -Path $pf | Out-Null
    Copy-Item -Force (Join-Path $OneDir 'bitxk-gui.exe') $pf
    Copy-Item -Force (Join-Path $OneDir 'bitxk.exe')     $pf
    Ok 'portable\ 已加入'
}

# 6.3 清掉不该发行的东西（调试时生成的配置、会话、编译缓存）
Get-ChildItem -Path $Stage -Recurse -Include 'config.toml', '*.pyc', '.bitxk_session.json' -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item -Force $_.FullName }
Get-ChildItem -Path $Stage -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item -Recurse -Force $_.FullName }
Ok '已清理配置文件/缓存'

# 6.4 使用说明
$quick = @"
$AppName v$Version — Windows x64
$('=' * (28 + $AppName.Length + $Version.Length))

包含内容
--------
  bitxk-gui.exe              图形界面，双击这个
  bitxk.exe                  命令行版（和图形界面共用同一份运行库）
  portable\                  单文件便携版（免解压，各一个文件）
  config.example.toml        配置模板
  README.md / LICENSE

目录里有 bitxk-gui.exe 和 bitxk.exe 两个程序，它们公用同一个 _internal\，
所以两个都能用、体积只算一份。

无需安装浏览器内核 —— "用浏览器登录"直接驱动你机器上已有的
Chrome / Edge / Chromium，这也是本包只有 30MB 左右的原因。

快速开始
--------
1. 双击 bitxk-gui.exe
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
  bitxk.exe --help
  bitxk.exe browser-login            用浏览器登录一次
  bitxk.exe list                     查看余量
  bitxk.exe grab --dry-run           只观察不提交
  bitxk.exe grab                     正式抢课

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
  * 二进制未做代码签名，首次运行 Windows 会提示，点「更多信息」→
    「仍要运行」。
  * 请勿把轮询间隔调到 1 秒以下。默认 2 秒是刻意保守的取值。
"@
$quickPath = Join-Path $Stage 'QUICKSTART.txt'
[System.IO.File]::WriteAllText($quickPath, $quick, (New-Object System.Text.UTF8Encoding $false))
Ok 'QUICKSTART.txt'

# ---------------------------------------------------------------------------
# 7. 编译安装包（Inno Setup）
# ---------------------------------------------------------------------------
$SetupExe = $null
if (-not $SkipInstaller) {
    Step 7 '编译安装包'

    $Iscc = $null
    foreach ($cand in @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    )) {
        if (Test-Path $cand) { $Iscc = $cand; break }
    }

    if (-not $Iscc) {
        Warn '未找到 Inno Setup，跳过安装包。'
        Warn '安装方式：winget install --id JRSoftware.InnoSetup -e'
        Warn '（文件夹版与便携版不受影响）'
    } else {
        $issPath = Join-Path $Root 'packaging\installer.iss'
        $isdir = Join-Path $WorkDir 'installer'
        New-Item -ItemType Directory -Force -Path $isdir | Out-Null

        $defines = @("/DAppVersion=$Version", "/DSourceDir=$Stage", "/O$isdir")
        $langFile = Join-Path (Split-Path $Iscc -Parent) 'Languages\ChineseSimplified.isl'
        if (-not (Test-Path $langFile)) {
            $defines += '/DNoChinese=1'
            Warn '无中文语言包，安装界面用英文'
        }

        # ISCC 失败不算致命 —— 其他产物已经有了，只是少一个安装包
        $isccLog = Join-Path $WorkDir 'iscc.log'
        $writer = New-Object System.IO.StreamWriter($isccLog, $false, (New-Object System.Text.UTF8Encoding $false))
        $writer.AutoFlush = $true
        try {
            # ISCC 同样把进度写到 stderr，必须用 Probe 包住
            $isccOut = Probe { & $Iscc @defines $issPath 2>&1 }
            $isccCode = $script:LASTEXITCODE
            foreach ($line in $isccOut) { $writer.WriteLine("$line") }
        } finally {
            $writer.Close()
        }
        if ($isccCode -ne 0) {
            Warn "ISCC 退出码 $isccCode，安装包未生成"
            Get-Content $isccLog -Tail 12 -Encoding UTF8 -ErrorAction SilentlyContinue |
                ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
        } else {
            $found = Get-ChildItem $isdir -Filter '*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($found) {
                $SetupExe = $found.FullName
                Ok "$($found.Name)  ($([math]::Round($found.Length/1MB,1)) MB)"
            } else {
                Warn 'ISCC 成功但没有产物'
            }
        }
    }
} else {
    Step 7 '跳过安装包（-SkipInstaller）'
}

# ---------------------------------------------------------------------------
# 8. 生成 Release zip
# ---------------------------------------------------------------------------
Step 8 '生成 Release 压缩包'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$ZipPath = Join-Path $OutDir "$AppName-$Version-windows-x64.zip"
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $Stage, $ZipPath, [System.IO.Compression.CompressionLevel]::Optimal, $false)
Ok "$(Split-Path $ZipPath -Leaf)  ($([math]::Round((Get-Item $ZipPath).Length/1MB,1)) MB)"

# 便携版与安装包也复制到 dist-release（方便一起发）
if (-not $SkipPortable) {
    Copy-Item -Force (Join-Path $OneDir 'bitxk-gui.exe') (Join-Path $OutDir "$AppName-$Version-windows-portable-gui.exe")
    Copy-Item -Force (Join-Path $OneDir 'bitxk.exe')     (Join-Path $OutDir "$AppName-$Version-windows-portable-cli.exe")
}
if ($SetupExe) {
    Copy-Item -Force $SetupExe (Join-Path $OutDir "$AppName-$Version-setup.exe")
}

# ---------------------------------------------------------------------------
# 9. 校验和
# ---------------------------------------------------------------------------
Step 9 '生成校验和'
$sums = Get-ChildItem $OutDir -File | Where-Object { $_.Name -ne 'SHA256SUMS.txt' } | ForEach-Object {
    $h = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
    "$h  $($_.Name)"
}
$sumsPath = Join-Path $OutDir 'SHA256SUMS.txt'
[System.IO.File]::WriteAllLines($sumsPath, $sums, (New-Object System.Text.UTF8Encoding $false))
$sums | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
Ok 'SHA256SUMS.txt'

# ---------------------------------------------------------------------------
# 10. 汇总
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '============================================================' -ForegroundColor Green
Write-Host ' 打包完成' -ForegroundColor Green
Write-Host '============================================================' -ForegroundColor Green
Write-Host "输出目录：$OutDir"
Write-Host ''
Get-ChildItem $OutDir -File | Sort-Object Name | ForEach-Object {
    Write-Host ("  {0,-56} {1,7:N1} MB" -f $_.Name, ($_.Length / 1MB))
}
Write-Host ''
Write-Host '提示：'
Write-Host '  * 直接分发 zip 即可（解压即用）'
Write-Host '  * setup.exe 是安装包，默认按用户安装，不需要管理员权限'
Write-Host '  * portable-*.exe 是免解压的单文件版，启动比文件夹版慢约 1 秒'
Write-Host '  * SHA256SUMS.txt 可用于校验下载完整性'
Write-Host ''
