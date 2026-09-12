# bitxk

**北京理工大学本科生选课系统**（金智 wisedu `xsxkapp`，`xk.bit.edu.cn/xsxkapp`）
**课程余量轮询 + 自动选课**命令行工具。

适用场景：**补选 / 退换课 / 试听退换课**阶段，某门课已经选满，你希望有人退课时**自动帮你补上**。

两种用法，随便挑：

* **图形界面**（推荐给不熟悉命令行的同学）：`bitxk gui` —— 原生桌面窗口，点点鼠标就能抢课。
* **命令行**：`bitxk grab` —— 适合挂服务器 / 写脚本。

登录方式也独立于 SSO 实现：**用你自己的 Chromium 浏览器登录一次**，
工具只负责把登录态取出来。学校无论怎么改登录页都不影响使用，
短信验证码、2FA、密码管理器也都能正常工作。

> 适用对象仅限**本科生**。研究生教务系统（`grdms.bit.edu.cn`）是另一套 DWR 协议的老系统，
> 接口完全不同，本工具不适用。

---

## 目录

- [免责声明](#免责声明)
- [功能](#功能)
- [它到底是怎么工作的](#它到底是怎么工作的)
- [图形界面](#图形界面)
- [安装](#安装)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [命令参考](#命令参考)
- [校外访问](#校外访问)
- [兜底方案：手动导入登录态](#兜底方案手动导入登录态)
- [轮询策略与请求节制](#轮询策略与请求节制)
- [故障排查](#故障排查)
- [项目结构](#项目结构)
- [开发](#开发)
- [参考资料](#参考资料)
- [许可证](#许可证)

## 免责声明

**请先读完再用。**

1. 本工具为**个人学习与自用**目的编写，自动化操作的是**你自己的账号**。
2. 选课系统是学校的生产系统。**请勿把轮询间隔调到 1 秒以下**，那会对服务器造成不必要的压力，也更容易触发风控导致账号被限制。默认的 2 秒是刻意保守的取值。
3. 本工具**不破解验证码、不绕过任何风控、不伪造身份**。它做的是「用你自己的凭据，按正常接口查询余量并提交选课」，与你在浏览器里手动刷新点按钮是同一件事，只是更快更持久。
4. 抢课本身是个公平性问题：你能抢到，通常意味着有人退课。**请不要用本工具批量囤课或倒卖课程**。
5. 使用本工具产生的任何后果由使用者自负。请遵守北京理工大学的教学管理规定。

---

## 功能

| 功能 | 说明 |
|---|---|
| 图形界面 | `bitxk gui`：原生桌面窗口，课程管理 + 实时余量表 + 运行日志 |
| 浏览器登录 | 用本机 Chromium 登录一次即可，不依赖 SSO 实现细节（推荐） |
| 自动 SSO 登录 | 也支持直接模拟统一身份认证（含密码 AES 加密），无需开浏览器 |
| 登录态持久化 | 会话缓存到本地（权限 `600`），重启后优先复用，失效自动重登 |
| 课程余量轮询 | 按配置间隔查询教学班余量，带随机抖动 |
| 自动选课 | 一旦出现余量**立即**提交选课，按课程优先级排序 |
| 异步结果确认 | 选课接口是「受理制」，提交后会轮询状态接口确认真实成败 |
| 多课程并发 | 多门课共享一个全局限速器，不会因为课多就把请求打爆 |
| 智能退避 | 被限流时冷却并自动放大间隔；网络抖动指数退避重试 |
| 失效自愈 | Token 过期自动重新登录并继续，无需人工干预 |
| 冲突去重 | 已确认时间冲突的教学班不再重复提交 |
| 手动登录态兜底 | 复制浏览器 Cookie + Token 即可导入，三种登录方式逐级兜底 |
| 安全试跑 | `--dry-run` 只观察余量、绝不提交选课 |
| 到点提醒 | 抢到课响铃 + 系统通知（macOS / Linux / Windows） |
| 环境自检 | `bitxk check` 一条命令确认网络与页面结构是否仍适配 |

---

## 它到底是怎么工作的

### 1. 登录方式

工具支持三种登录方式，**按推荐程度排序**：

| 方式 | 命令 | 依赖 SSO 实现？ | 说明 |
|---|---|---|---|
| **浏览器登录** | `bitxk gui` / `bitxk browser-login` | ❌ 不依赖 | 开一个 Chromium 窗口，你登录一次，工具把 Cookie + Token 取出来 |
| 自动 SSO 登录 | `bitxk grab`（填账号密码） | ✅ 依赖 | 直接模拟统一身份认证，不用开浏览器 |
| 手动导入 | `--cookie` + `--token` | ❌ 不依赖 | 从 F12 里复制，任何情况下都能用 |

**为什么推荐浏览器登录**：模拟 SSO 需要复刻密码加密、执行串、风控字段，
学校一改版就失效。而"让真人在真浏览器里登录一次，脚本只负责取出登录态"
不受任何改版影响，短信验证码 / 2FA / 密码管理器也都能正常工作。

实现上用的是 Chromium 自带的 **DevTools Protocol**：

```
发现本机浏览器 → 以 --remote-debugging-port 启动一个独立 profile
  → 导航到选课系统（自动跳统一身份认证）
  → 你在窗口里正常登录
  → 脚本轮询页面，发现登录成功
  → Network.getCookies 取 Cookie + 读 sessionStorage 的 token
  → 关闭浏览器，登录态缓存到本地
```

刻意**不用 Playwright / Selenium**：本机已有的 Chrome / Edge / Chromium
直接就能用，而 Playwright 要额外下 200MB 浏览器。用到的
`websockets` 库只有几十 KB。

浏览器发现顺序：`--browser 路径` → 环境变量 `BITXK_BROWSER` →
系统安装的 Chrome / Edge / Chromium / Brave → Playwright 缓存里的完整 Chromium
（自动跳过没有窗口的 `headless_shell`）。

查看本机检测结果：

```bash
bitxk --list-browsers
```

登录态会缓存在 `~/.bitxk/chrome-profile`（浏览器 profile）和
`config.toml` 同目录的 `.bitxk_session.json`（会话）。删掉它们就相当于退出登录。

### 1.1 自动 SSO 登录的链路

如果你选择不开浏览器、直接填账号密码，走的是下面这条链路。

本科选课系统首页里注入的常量（直接从页面源码读到，非推测）：

```js
BaseUrl   = "https://xk.bit.edu.cn/xsxkapp";
casUrl    = "https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/bitXsxkLogin/casLogin.do";
loginType = "cas";      // 走统一身份认证
```

登录链路：

```
GET  https://sso.bit.edu.cn/cas/login?service=<上面的 casUrl>
       HTML 里藏着：
         login-croypto        -> base64 字符串，作为 AES 密钥
         login-page-flowkey   -> 作为 execution（服务端加密 JWT，必须逐字节原样回填）
POST 同一 URL（application/x-www-form-urlencoded）
       username / password(密文) / execution / croypto /
       captcha_code / captcha_payload / _eventId=submit / type=UsernamePassword
       └─ 成功返回 302，Location 带 ?ticket=ST-xxx
GET  Location（回打选课系统的 casLogin.do 完成换票）
       └─ 跳转链某一跳带 ?bitXsxkLogin=<key>
GET  .../student/register.do?number=<key>
       └─ {"data": {"token": "...", "name": "..."}}   ← 之后所有请求带全小写 header `token`
```

> 实现注意：POST 必须用 `allow_redirects=False` —— ticket 就在 302 的 `Location` 里，
> 自动跟随会把这个响应丢掉。另外**一个 SESSION 只能发一次登录 POST**，
> 失败必须丢弃整个会话重建（第二次会得到 `1320007`）。

密码加密（`bitxk/auth.py`）：

- `ecb`（**默认**，当前 BIT 在用）：`AES-128-ECB`，`key = base64decode(login-croypto)`
  （恰好 16 字节），**无 IV**、**明文就是口令原文**（不加随机前缀）、PKCS#7 填充，密文再 base64。
  该结论来自对生产前端 bundle 的源码级确认，非猜测。
- `cbc`（旧版 wisedu，部分学校仍在用）：`AES-128-CBC`，明文 = 64 位随机串 + 密码，`iv` = 16 位随机串。
  若登录页出现 `pwdEncryptSalt` 而非 `login-croypto`，工具会自动切到该模式；也可用 `--encrypt-mode cbc` 手动指定。

### 2. 业务接口

所有业务请求都在 `https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/` 下，鉴权靠 **全小写 header `token`**（另发 `language`）。

| 用途 | 端点 |
|---|---|
| 换取 Token | `student/register.do?number=<key>` |
| 学生信息 / 批次列表 / 校区码 | `student/<学号>.do` |
| 校公选课查询 | `elective/publicCourse.do` |
| 推荐课程查询 | `elective/recommendedCourse.do` |
| 方案内 / 方案外 / 重修 / 体育 / 辅修 | `elective/programCourse.do` |
| 全校课程 | `elective/course.do` |
| **提交选课 / 退选** | `elective/volunteer.do` |
| **查询处理结果** | `elective/studentstatus.do` |
| 服务端权威的"能不能选" | `util/canchoose.do` |

参数以 **POST 表单体**发送，值是一个 JSON 字符串：

```
POST .../elective/publicCourse.do
Content-Type: application/x-www-form-urlencoded
token: <全小写>
language: zh_cn

querySetting={"data":{"studentCode":"...","campus":"...","electiveBatchCode":"...",
              "isMajor":"1","teachingClassType":"XGXK","checkCapacity":"2",
              "queryContent":"科幻文学"},"pageSize":"10","pageNumber":"0","order":""}
```

三个容易踩的细节：

- **`checkCapacity` 必须用 `"2"`**。`"1"` 会把满员课直接过滤掉，列表里根本看不到目标课，
  也就永远发现不了"有人退课"这件事；`"2"` 是"校验但不过滤"，满员课仍在列表里带 `isFull='1'`。
- **余量没有现成字段**。接口只给 `classCapacity`（容量）和已选人数，剩余量要自己算：
  `剩余 = classCapacity - 已选人数`。`dataList[]` 顶层用 `numberOfFirstVolunteer`（只算第一志愿），
  `tcList[]` 子项用 `numberOfSelected`（已选总数）。判满优先信任服务端的 `isFull == '1'`。
- **`campus` 不是常量**，取自 `student/<学号>.do`；写死会把其他校区的课查漏。

### 2.1 针对本科系统的核对结论

以下每一条都在**本科前端源码**里逐字确认过，不是按 wisedu 通用形态推测的：

| 核对项 | 结果 |
|---|---|
| 8 种 `teachingClassType` | 8 个值全部出现在本科 `grablessons.js`，且对应 8 个页面 Tab |
| Tab DOM id | `aRecommendCourse` / `aProgramCourse` / `aUnProgramCourse` / `aPublicCourse` / `aRetakeCourse` / `aSportCourse` / `aMinorCourse` / `aSchoolCourse` —— 与枚举一一对应 |
| 选课是异步的 | 本科前端确实调用 `addVolunteer` → `initProcessInterval` 轮询确认 |
| 学生信息端点 | 本科 `index.js` 里是 **GET** `student/<学号>.do?timestamp=<ms>`（不是 POST） |
| 批次判定 | `electiveBatchList[i].canSelect == '1'`；另需 `needConfirm != '1'` 或 `isConfirmed == '1'` |
| 换 token | 本科 `loginInUserRegister.js` 调 `student/register.do?number=<uid>`，取 `data.token` |
| `campus` | **不是**学生信息字段 —— 前端是按每个课程行的 `data.campus` 取值。故本工具不臆造，留空由服务端兜底 |
| 账号类型校验 | `data.code` 为空即"无学籍信息"（例如用研究生账号登本科系统），工具会明确报错 |

另外，本科系统在高峰期会用信封 `code == "4"` 拒绝新会话
（文案「在线人数超过上限，请稍后再试！」）。这是**很常见**的情况，
工具把它当作可重试状态退避处理，**不计入连续错误**——
否则会在选课高峰刚开放时就把程序误判退出。

### 2.2 选课是异步的

这是最容易实现错的一点：

```
POST elective/volunteer.do           → code == "1"  仅表示「已受理」，结果未定
  └─ 轮询 elective/studentstatus.do  → code == "1"   ✅ 选课成功
                                       code == "-1"  ❌ 选课失败（真实原因在 msg）
                                       其它          ⏳ 仍在处理，1 秒后重试，最多 10 次
```

前端就是这么做的（`initProcessInterval` + `queryOperateProcess`）。所以本工具的
`SelectionResult` 区分了三种"还没定论"的状态：`ACCEPTED`（已受理）、
`PENDING`（轮询超时，**不等于失败**）、`SUCCESS`（已确认）。

### 2.3 token 失效有三种形态

实测都得处理，只看状态码会漏：

| 形态 | 场景 |
|---|---|
| HTTP **302** → `Location: .../*default/index.do` | 带着首页 cookie 请求时最常见 |
| HTTP **401** + `text/html` `Not login!` | 无 cookie 时的网关响应 |
| HTTP **200** + 应用首页 HTML | `requests` 自动跟完 302 后的落点 |

因此客户端一律**先判状态码、再按内容特征判定**，绝不直接 `resp.json()`。

### 3. 轮询决策

```
每轮:
  刷新所有未完成课程的教学班状态（全局限速，串行）
  挑出「有余量 且 满足老师/教学班筛选 且 未确认冲突」的教学班
  按 (课程优先级, 教学班ID) 排序，依次提交选课
  睡眠 interval + random(0, jitter)

异常:
  Token 过期  → 立即重登，不浪费一整轮
  被限流      → 冷却 N 秒，并把间隔 ×1.5（上限 8 倍）
  网络抖动    → 指数退避重试，连续失败超过阈值才退出
  时间冲突    → 该教学班进黑名单，不再重复提交
  抢到课      → 记录 + 响铃 + 系统通知
```

---

## 图形界面

```bash
bitxk gui
```

打开一个原生桌面窗口：

```
┌────────────────────────────────────────────────────────────────────┐
│ BIT 选课助手      登录态：张三（1120200001）  [用浏览器登录][验证][自检] │
├──────────────────────┬─────────────────────────────────────────────┤
│ 要盯的课程            │ 实时余量                                    │
│  科幻文学   校公选 100│  课程      教学班 教师  容量        状态    │
│  体育/羽毛球 体育   50│  科幻文学  1001  张三  12/40 (余 28) 有余量  │
│  数据结构…  方案内  20│  体育/羽毛… 2001  王五  30/30 (余 0)  已满    │
│  [添加][编辑][删除]   │                                             │
├──────────────────────┴─────────────────────────────────────────────┤
│ 间隔[2.0]秒 [x]试跑（只观察不提交） [ ]抢到一门就停   [开始抢课][停止] │
├────────────────────────────────────────────────────────────────────┤
│ 运行日志                                                            │
│ 15:31:43 ✓ 当前批次：[B1] 2024-2025-1 第一轮选课                     │
│ 15:31:45 → [试跑] 发现余量：科幻文学 / 1001 12/40 (余 28)（未提交）   │
└────────────────────────────────────────────────────────────────────┘
```

界面上能做的事：

1. **用浏览器登录** —— 弹出浏览器窗口，登录一次后自动取回登录态；
2. **添加 / 编辑 / 删除**要盯的课程（双击课程行也能编辑）；
3. **实时余量表** —— 每轮轮询刷新，有余量的课标绿、已满标灰、冲突标黄；
4. **开始抢课 / 停止** —— 抢课在后台线程跑，界面不会卡住；
5. **保存配置** —— 课程列表写回 `config.toml`，下次打开还在。

> 抢课是长任务，所有网络请求都在工作线程里执行，事件通过队列回到主线程渲染，
> 所以点「开始」之后界面依然可以正常操作。
>
> 第一次使用建议保持勾选 **试跑**：它只观察余量、绝不提交选课，
> 确认课程匹配正确后再取消勾选正式开抢。

## 安装

### 方式一：下载现成的发行版（推荐，不用装 Python）

到 [Releases](https://github.com/Sirius-Peng/bitxk/releases) 页面下载对应平台的文件：

| 平台 | 文件 | 解压后 |
|---|---|---|
| Windows | `BIT-Course-Helper-0.1.0-windows-x64.zip` | 双击 `bitxk-gui.exe` |
| macOS | `BIT-Course-Helper-0.1.0-macos.tar.gz` | 双击 `BIT-Course-Helper.app` |

### 关于体积

发行包只有 20MB 出头，**因为它不包含浏览器内核**。

「用浏览器登录」这个功能驱动的是你**本机已经装好的** Chrome / Edge / Chromium / Brave，
工具通过 DevTools Protocol 连过去取登录态。所以：

| 方案 | 包体积 |
|---|---|
| 打包 Playwright / Selenium（自带内核） | ~250MB |
| **本工具（复用系统浏览器）** | **~24MB** |

打包时还做了这些裁剪：

- 排除了 `cryptography` / `bcrypt` / `cffi` —— 它们不是本项目的依赖，
  只是恰好装在构建机上被 PyInstaller 顺手收了进来（省约 11MB）；
- 排除了 `numpy` / `pandas` / `PIL` / 其它 GUI 框架 / 开发期工具；
- 二进制符号表已 strip。

如果你只要命令行、不需要图形界面，包里另附**精简版**（`BIT-Course-Helper-cli/`），
去掉了 tkinter 与 Tcl/Tk，再小约 11MB。

包内结构：

* `BIT-Course-Helper.app`（Windows 上是 `bitxk-gui.exe`）—— **双击就用**，图形界面；
* `BIT-Course-Helper-cli/`（Windows 上是 `bitxk.exe`）—— 命令行版，给脚本 / 计划任务。

> **macOS 首次打开提示"无法验证开发者"**：这是未签名应用的正常提示。
> 右键点图标 → 选「打开」→ 再确认一次即可；或执行
> `xattr -dr com.apple.quarantine /Applications/BIT-Course-Helper.app`。
>
> **Windows SmartScreen 拦截**：点「更多信息」→「仍要运行」。

### 方式二：从源码安装

需要 **Python ≥ 3.11**（用到标准库 `tomllib`）。图形界面用 Python 自带的
`tkinter`，**不需要额外安装任何东西**。

```bash
git clone https://github.com/Sirius-Peng/bitxk.git && cd bitxk

# 装成命令（推荐）
pip install -e .

# 或只装依赖，用 python -m bitxk 运行
pip install -r requirements.txt
python -m bitxk --help
```

依赖只有三个：`requests`、`pycryptodome`、`websockets`
（最后一个用来通过 CDP 驱动浏览器，只需要几十 KB）。

### 方式三：自己打包

```bash
pip install pyinstaller

# 完整版（图形界面 + 命令行）
pyinstaller --clean --noconfirm packaging/bitxk.spec

# 精简命令行版（去掉 tkinter，小约 10MB）
pyinstaller --clean --noconfirm packaging/bitxk-cli.spec

# 产物都在 dist/ 下
```

Windows 上还可以直接右键运行 `packaging/build-windows.ps1`，
它会自动建虚拟环境、装依赖、打包并打成 zip。

## 快速开始

### 图形界面（推荐）

```bash
bitxk init          # 生成配置模板（只需一次）
bitxk gui           # 打开图形界面
```

然后在窗口里：

1. 点 **「用浏览器登录」**，在弹出的浏览器窗口里登录一次 → 关掉浏览器；
2. 点 **「添加」** 加上你想盯的课（课程名要和选课系统里显示的**完全一致**）；
3. 保持 **试跑** 勾选，点 **「开始抢课」**，观察余量是否正确；
4. 确认无误后取消 **试跑** 勾选，正式开抢。

### 命令行

```bash
bitxk init                    # 1. 生成配置模板
$EDITOR config.toml           # 2. 填学号密码和想选的课

bitxk check                   # 3. 自检：网络 + 登录页结构
bitxk --list-browsers         # 4. 看看本机有哪些浏览器可用

# 5. 登录（二选一）
bitxk browser-login           #    a) 浏览器登录（推荐，不受 SSO 改版影响）
bitxk login                   #    b) 直接填账号密码登录

bitxk list                    # 6. 看一眼当前余量
bitxk grab --dry-run          # 7. 安全试跑：轮询但绝不提交
bitxk grab                    # 8. 正式开抢
```

> **密码安全**：不建议把密码写进 `config.toml`。用环境变量更稳妥：
>
> ```bash
> export BITXK_USERNAME=1120200001
> export BITXK_PASSWORD='你的密码'
> bitxk grab
> ```
>
> 或者干脆用 `bitxk browser-login` —— 那样工具**根本不需要你的密码**。

## 配置说明

`config.toml` 全部字段（`bitxk init` 生成的模板里都带注释）：

```toml
[account]
username = ""          # 也可用环境变量 BITXK_USERNAME
password = ""          # 也可用环境变量 BITXK_PASSWORD

[poll]
interval = 2.0              # 轮询间隔（秒），下限 0.5，建议 1.5~3
min_request_interval = 0.8  # 任意两次请求的最小间隔（秒），全局限速
jitter = 0.5                # 随机抖动上限（秒）
max_duration = 14400        # 最长运行时长（秒），0 = 不限
max_consecutive_errors = 30 # 连续失败多少次后退出
rate_limit_cooldown = 30.0  # 被限流后的冷却秒数
relogin_after = 600         # 会话使用多久后主动重登（秒）

[http]
timeout = 10.0
max_retries = 3
proxy = ""                  # 校外可填校园 WebVPN 代理
verify_ssl = true

[notify]
sound = true                # 响铃
stop_on_success = false     # true = 抢到第一门就退出

# 想盯几门课就写几个 [[courses]]
[[courses]]
name = "科幻文学"        # 必须与选课系统里显示的课程名完全一致
type = "XGXK"            # 教学班类型，见下表
priority = 100           # 数字越小越先选
enabled = true
# teachers = ["张三"]    # 可选：只选指定老师的课
# classes = ["1234567"]  # 可选：只选指定教学班 ID

[[courses]]
name = "体育/羽毛球"
type = "TYKC"
priority = 50            # 更想上体育课，所以优先级更高
```

### `type` 取值

共 8 种，与选课系统页面上的 Tab 一一对应：

| 值 | 含义 | 查询接口 |
|---|---|---|
| `XGXK` | 校公选课 | `publicCourse.do` |
| `TYKC` | 体育课程 | `programCourse.do` |
| `FANKC` | 方案内课程 | `programCourse.do` |
| `FAWKC` | 方案外课程 | `programCourse.do` |
| `CXKC` | 重修课程 | `programCourse.do` |
| `FXKC` | 辅修课程 | `programCourse.do`（唯一 `isMajor='0'`） |
| `TJKC` | 推荐课程 | `recommendedCourse.do` |
| `QXKC` | 全校课程 | `course.do` |

> 注意**体育课没有独立端点** —— 它和方案内/方案外/重修/辅修共用 `programCourse.do`，
> 由服务端按 body 里的 `teachingClassType` 分流。

写错 `type` 会导致查不到课却看不出原因，所以工具在启动时会校验并直接报错。

---

## 命令参考

```
bitxk gui                      打开图形界面（推荐）
bitxk init                     生成 config.toml 模板
bitxk check                    环境自检（无需账号）
bitxk --list-browsers          列出本机可用的 Chromium 系浏览器
bitxk browser-login            用浏览器登录并缓存登录态（推荐）
bitxk login                    直接模拟 SSO 登录并列出可选批次
bitxk list                     打印当前各教学班余量
bitxk grab                     轮询并自动选课
bitxk grab --once              只查一轮（等同 list）
bitxk grab --dry-run           轮询但绝不提交选课（安全试跑）
bitxk grab --browser           抢课前先用浏览器登录一次
bitxk grab --interval 3        覆盖轮询间隔
bitxk grab --duration 7200     覆盖最长运行时长
bitxk grab -v                  输出调试日志
```

全局参数：

```
-c/--config <路径>     指定配置文件（默认 ./config.toml）
-u/--username          学号（覆盖配置）
-p/--password          密码（覆盖配置）
--encrypt-mode ecb|cbc SSO 密码加密模式
--cookie '...'         手动导入 Cookie
--token  '...'         手动导入 Token
--student-code <学号>  手动导入模式必填（批次接口形如 student/<学号>.do）
--browser [路径]       用 Chromium 浏览器登录；可选路径指定浏览器
--browser-timeout 300  等待浏览器登录的超时秒数
--list-browsers        列出检测到的浏览器后退出
```

退出码：`0` 成功 / `1` 未抢到 / `2` 登录失败 / `3` 不在选课时间 / `4` 配置错误 / `5` 其它错误。

---

## 校外访问

在**校内网络**（校园网 / 学校 VPN 客户端）下直接运行即可，无需额外配置。

在校外且不想装 VPN 客户端时，有两条路：

1. **先在浏览器登录，再手动导入登录态**（推荐，最省事）——
   见下一节的 `--token` / `--cookie` 用法。
   工具本身仍能直连 `xk.bit.edu.cn`，缺的只是登录态。
2. **走本地代理**——如果你有可用的 HTTP 代理，填到配置里：

   ```toml
   [http]
   proxy = "http://127.0.0.1:7890"
   ```

   注意 `webvpn.bit.edu.cn` 是**网页版网关**（它把目标站点嵌在路径里），
   不是 HTTP 代理，不能直接填进 `proxy`。
   浏览器里通过 WebVPN 登录后复制 Cookie 用第 1 种方式导入，反而更稳。

> 顺带一提：能连通学校服务器本身就说明网络没问题；
> `bitxk check` 会告诉你当前能否直连。

## 兜底方案：手动导入登录态

如果学校改了 SSO 登录页结构，自动登录会失效（`bitxk check` 会提前告诉你）。此时用浏览器里的登录态兜底：

1. 浏览器登录 `https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/*default/index.do`
2. 按 `F12` → **Network** → 随便点一门课的「选课」按钮
3. 在请求头里找到：
   - `Token: xxxxx` ← 整个值复制出来
   - `Cookie: JSESSIONID=...` ← 整行复制出来
4. 运行：

```bash
bitxk grab --student-code '1120200001' \
           --token 'xxxxx' \
           --cookie 'JSESSIONID=...; route=...; _WEU=...'
```

`--student-code` 在手动导入模式下是**必需**的：批次接口是 `student/<学号>.do`，
没有学号就无法确定该请求哪个地址。

`--token` 也支持直接粘贴 CAS 回跳后地址栏里带 `bitXsxkLogin=` 的完整 URL，工具会自动提取。

---

## 轮询策略与请求节制

轮询频率是这个工具唯一可能"伤害别人"的地方，所以设计上刻意保守：

- **默认 2 秒**一轮，且 `PollConfig` 在代码层拒绝 `interval < 0.5`，改配置也绕不过去。
- **全局限速器**保证任意两次请求至少间隔 `min_request_interval`（默认 0.8 秒），即使你配了 10 门课也不会并发轰炸。
- **随机抖动**让请求不会卡在整秒上，避免和别人的脚本形成同步尖峰。
- **被限流就退让**：冷却 30 秒，并把间隔按 1.5 倍逐步放大（最多 8 倍），而不是硬顶。
- **连续失败即退出**，避免在系统维护期间无意义地刷。

一个 2 秒间隔的实例，一小时约 1800 次请求 —— 相比浏览器里手动 F5，这个量级对现代教务系统完全可接受。请不要再往下压。

---

## 故障排查

### 提示「没有在本机找到 Chromium 系浏览器」

装一个 Google Chrome 就行；或者告诉工具它在哪：

```bash
export BITXK_BROWSER="/path/to/chrome"      # 环境变量
bitxk grab --browser "/path/to/chrome"       # 单次指定
```

用 `bitxk --list-browsers` 看检测结果。

### 浏览器登录卡住 / 一直等不到登录完成

1. 确认在弹出的浏览器窗口里**确实登录成功**了（页面应该跳到选课系统首页）；
2. 窗口被其他窗口挡住时脚本仍能检测到，但你看不到，容易被误以为卡住；
3. 超时默认 5 分钟，可用 `--browser-timeout 600` 延长（比如要等短信验证码）。

### GUI 打不开 / 提示「无法启动图形界面」

纯命令行环境（SSH、Docker）没有显示服务，用命令行版本：

```bash
bitxk grab
```

### 抢到课后浏览器 profile 想清掉

浏览器登录的痕迹都在 `~/.bitxk/`：

```bash
rm -rf ~/.bitxk            # 清掉浏览器 profile 与缓存
rm -f .bitxk_session.json  # 清掉当前目录的会话缓存
```

### `bitxk check` 报「登录页结构已变更」

学校改版了 SSO 页面，只影响"直接模拟登录"这一种方式。
**改用浏览器登录即可**（`bitxk browser-login`）——那条路不依赖登录页结构。

### 登录返回 401 / 「账号或密码错误」

> 如果你用的是浏览器登录，不会遇到这一类问题——换那条路即可。


1. 先在浏览器里手动登录一次，确认密码没记错；
2. 若浏览器能登而脚本不能，试 `bitxk grab --encrypt-mode cbc`；
3. 连续失败会触发验证码，工具检测到后会**主动停止**而不是继续硬试（避免锁账号）。

### 提示「当前不在可选课时间内」

正常行为，直接退出（退出码 3）。选课批次没开放时，接口不会返回任何 `canSelect=1` 的批次。用 `bitxk login` 可以看到所有批次及其时间窗。

### 一直显示「已满」但浏览器里明明有余量

大概率是容量字段命名与预期不同。用 `-v` 跑一次，日志里会打印原始响应字段；`TeachingClass` 会记录 `capacity_source` 说明它是从哪个字段推出来的。请带日志提 issue。

### 提示「在线人数已达上限」

选课系统限制并发在线人数，高峰期限流。这是**正常现象**，工具会自动退避重试，
不需要你做任何事；也不会因此退出。想减少撞上的概率，可以把 `interval` 调大一些。

### 提示「未查询到学籍信息」

说明登录成功了，但这个账号在**本科**选课系统里没有学籍 ——
最常见的原因是用研究生账号登录本科系统。请确认用的是本科学号。

### 一直卡在「已受理待确认」

正常现象，说明选课请求已被系统受理、后台正在排队处理。工具会自动轮询
`studentstatus.do` 确认结果。若最终显示「结果未知」，**不代表失败** ——
后台可能仍在处理，下一轮查询课程列表时看到「已选」就说明成功了。

### 提交后返回「时间冲突」但我不觉得冲突

以服务端的判定为准（它掌握你的完整课表，包括其他批次的课）。
也可以运行 `bitxk list` 看该教学班的 `status` 是否已被标为「冲突」。

### 抢到了但我不想要

本工具不做退课，请去选课系统页面手动退。另外建议先 `--dry-run` 试跑确认匹配逻辑符合预期。

### 想只抢第一门就停

```toml
[notify]
stop_on_success = true
```

---

## 项目结构

```
bitxk/
├── __init__.py       包入口与公共 API
├── __main__.py       python -m bitxk
├── cli.py            命令行界面（gui/init/check/browser-login/login/list/grab）
├── gui.py            tkinter 桌面界面（配置面板 + 余量表 + 日志）
├── browser.py        Chromium 发现 / CDP 驱动 / 浏览器登录取登录态
├── auth.py           SSO 登录、密码加密、会话持久化、手动导入
├── http.py           HTTP 传输层：限速、重试、限流识别
├── client.py         选课系统业务接口（查询/选课/批次）
├── poller.py         轮询引擎（本项目核心）
├── models.py         数据模型与容量推断
├── config.py         TOML 配置加载与校验
├── notify.py         响铃与系统通知
└── exceptions.py     分层异常

packaging/
├── entry.py            打包入口：无参数开 GUI，带参数走 CLI
├── bitxk.spec          完整版打包配置
├── bitxk-cli.spec      精简命令行版打包配置
├── build-windows.ps1   Windows 一键构建脚本
└── config.example.toml 发行包里的配置模板

tests/                372 个测试；网络与浏览器默认全部打桩，
                      另有 3 项真实启动浏览器的用例（无浏览器时自动跳过）
```

---

## 开发

```bash
pip install -e ".[dev]"
python -m pytest -q            # 跑测试
python -m pytest -q -k poller  # 只跑轮询引擎测试
```

设计上的两个原则：

1. **业务失败不是异常。** 「容量已满」是轮询中的正常状态，用 `SelectionResult.outcome` 表达；只有「登录失效」「被限流」「网络故障」这类需要上层改变行为的才抛异常。
2. **容量字段不写死。** 选课系统不同版本的字段名不一致（`remainCapacity` / `rl` / `remainNumber` …），`models.py` 用候选键匹配 + 多路推断，并通过 `capacity_source` 暴露推断依据，便于排错。

改动与选课系统交互的代码（`client.py` / `auth.py` / `models.py`）时，请务必让
`tests/` 里对应的契约测试保持通过 —— 它们逐条固化了实测到的接口行为，
是防止改坏的主要防线：

```bash
python -m pytest tests/test_client.py tests/test_auth_contract.py -v
```

---

## 参考资料

本项目在实现前调研了以下开源项目与资料，特此致谢：

- [vc6-1998/BIT-CourseKiller](https://github.com/vc6-1998/BIT-CourseKiller) —— 北理工本科抢课脚本，提供了 SSO 登录与 `xsxkapp` 接口的可用链路，是本次实现的主要参考。
- [lijyve/Fuck-BITcourse](https://github.com/lijyve/Fuck-BITcourse) —— 早期选课系统 `volunteer.do` 调用方式。
- [mhllwmt/Spider_Bit](https://github.com/mhllwmt/Spider_Bit) —— 研究生院 DWR 老系统爬虫（与本科系统不同，仅作历史参考）。
- [only9464/HEU-Wisedu](https://github.com/only9464/HEU-Wisedu) —— 哈工程同款 wisedu 选课系统的桌面客户端，印证了 `xsxkapp` 接口的通用形态。
- [friskit-china/I_NEED_COURSE](https://github.com/friskit-china/I_NEED_COURSE)、[MichaelToLearn/oh_my_course](https://github.com/MichaelToLearn/oh_my_course) —— 北理工抢课工具，其免责声明与「延长时间间隔」的建议值得每个人读一遍。
- [FoskyM: php curl 登录金智统一认证平台](https://space.fosky.top/posts/tech/php-curl-login-wisedu) —— wisedu 统一认证加密流程的逆向分析。

## 许可证

MIT
