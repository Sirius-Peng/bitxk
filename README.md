# bitxk

北京理工大学（金智 wisedu `xsxkapp` 选课系统）**课程余量轮询 + 自动选课**命令行工具。

适用场景：补选 / 退换课 / 试听退换课阶段，某门课已经选满，你希望有人退课时**自动帮你补上**。

---

## 目录

- [免责声明](#免责声明)
- [功能](#功能)
- [它到底是怎么工作的](#它到底是怎么工作的)
- [安装](#安装)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [命令参考](#命令参考)
- [兜底方案：手动导入登录态](#兜底方案手动导入登录态)
- [轮询策略与请求节制](#轮询策略与请求节制)
- [故障排查](#故障排查)
- [项目结构](#项目结构)
- [开发](#开发)
- [参考资料](#参考资料)

---

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
| 统一身份认证登录 | 自动完成 SSO 登录（含密码 AES 加密），拿到选课系统 Token |
| 登录态持久化 | 会话缓存到本地（权限 `600`），重启后优先复用，失效自动重登 |
| 课程余量轮询 | 按配置间隔查询教学班余量，带随机抖动 |
| 自动选课 | 一旦出现余量**立即**提交选课，按课程优先级排序 |
| 多课程并发 | 多门课共享一个全局限速器，不会因为课多就把请求打爆 |
| 智能退避 | 被限流时冷却并自动放大间隔；网络抖动指数退避重试 |
| 失效自愈 | Token 过期自动重新登录并继续，无需人工干预 |
| 冲突去重 | 已确认时间冲突的教学班不再重复提交 |
| 手动登录态兜底 | SSO 改版导致自动登录失效时，可导入浏览器 Cookie + Token |
| 安全试跑 | `--dry-run` 只观察余量、绝不提交选课 |
| 到点提醒 | 抢到课响铃 + 系统通知（macOS / Linux / Windows） |
| 环境自检 | `bitxk check` 一条命令确认网络与页面结构是否仍适配 |

---

## 它到底是怎么工作的

### 1. 登录链路

```
GET  https://sso.bit.edu.cn/cas/login?service=<选课系统 casLogin>
       HTML 里藏着：
         login-croypto        -> base64 字符串，作为 AES 密钥
         login-page-flowkey   -> 作为 execution
POST 同一 URL（application/x-www-form-urlencoded）
       username / password(密文) / execution / croypto /
       captcha_code / _eventId=submit / type=UsernamePassword
       └─ 302 回跳 → https://xk.bit.edu.cn/xsxkapp/.../bitXsxkLogin/casLogin.do?bitXsxkLogin=<key>
GET  .../student/register.do?number=<key>
       └─ {"data": {"token": "...", "name": "..."}}   ← 之后所有请求带 header `Token`
```

密码加密（`bitxk/auth.py`）：

- `ecb`（**默认**，当前 BIT 在用）：`AES-ECB`，`key = base64decode(login-croypto)`，PKCS#7 填充，密文再 base64。
- `cbc`（旧版 wisedu，部分学校仍在用）：`AES-128-CBC`，明文 = 64 位随机串 + 密码，`iv` = 16 位随机串。
  若登录页出现 `pwdEncryptSalt` 而非 `login-croypto`，工具会自动切到该模式；也可用 `--encrypt-mode cbc` 手动指定。

### 2. 业务接口

所有业务请求都在 `https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/` 下，鉴权靠 header `Token`。

| 用途 | 端点 |
|---|---|
| 换取 Token | `student/register.do?number=<key>` |
| 学生信息 / 批次列表 | `student/xkxf.do`（降级 `elective/studentstatus.do`） |
| 校公选课查询 | `elective/publicCourse.do` |
| 方案内 / 体育课查询 | `elective/programCourse.do` |
| **提交选课** | `elective/volunteer.do` |
| 已选课程 | `elective/course.do` |

这套系统的参数传递方式比较特别：查询参数是一个 **JSON 字符串**，直接放进 URL 的 query string：

```
POST .../elective/publicCourse.do
     ?querySetting={'data':{'studentCode':'...','electiveBatchCode':'...',
                            'teachingClassType':'XGXK','checkCapacity':'0',
                            'queryContent':'科幻文学'},'pageSize':'50','pageNumber':'0','order':''}
```

**关键细节**：轮询时必须用 `checkCapacity='0'`。若用 `'1'`（只返回有余量的课），满员时课程列表会直接为空，你就无法观察余量变化，也就无法在有人退课的瞬间发现它。

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

## 安装

需要 **Python ≥ 3.11**（用到标准库 `tomllib`）。

```bash
git clone <本仓库地址> bitxk && cd bitxk

# 方式一：装成命令（推荐）
pip install -e .

# 方式二：只装依赖，用 python -m bitxk 运行
pip install -r requirements.txt
python -m bitxk --help
```

依赖只有两个：`requests`、`pycryptodome`。

---

## 快速开始

```bash
# 1. 生成配置模板
bitxk init

# 2. 编辑 config.toml，填学号密码和想选的课
$EDITOR config.toml

# 3. 自检：确认网络通、登录页结构没变
bitxk check

# 4. 测试登录
bitxk login

# 5. 看一眼当前余量（不轮询、不提交）
bitxk list

# 6. 安全试跑：轮询但绝不提交
bitxk grab --dry-run

# 7. 正式开抢
bitxk grab
```

> **密码安全**：不建议把密码写进 `config.toml`。用环境变量更稳妥：
>
> ```bash
> export BITXK_USERNAME=1120200001
> export BITXK_PASSWORD='你的密码'
> bitxk grab
> ```

---

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

| 值 | 含义 | 查询接口 |
|---|---|---|
| `XGXK` | 校公选课 | `publicCourse.do` |
| `TYKC` | 体育课程 | `programCourse.do` |
| `FANKC` | 培养方案内课程 | `programCourse.do` |
| `TJKC` | 系统推荐课程 | `programCourse.do` |
| `XGKC` | 专业课 / 跨专业 | `programCourse.do` |
| `TJK` | 通识课 | `programCourse.do` |

写错 `type` 会导致查不到课却看不出原因，所以工具在启动时会校验并直接报错。

---

## 命令参考

```
bitxk init                     生成 config.toml 模板
bitxk check                    环境自检（无需账号）
bitxk login                    验证登录并列出可选批次
bitxk list                     打印当前各教学班余量
bitxk grab                     轮询并自动选课
bitxk grab --once              只查一轮（等同 list）
bitxk grab --dry-run           轮询但绝不提交选课（安全试跑）
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
```

退出码：`0` 成功 / `1` 未抢到 / `2` 登录失败 / `3` 不在选课时间 / `4` 配置错误 / `5` 其它错误。

---

## 兜底方案：手动导入登录态

如果学校改了 SSO 登录页结构，自动登录会失效（`bitxk check` 会提前告诉你）。此时用浏览器里的登录态兜底：

1. 浏览器登录 `https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/*default/index.do`
2. 按 `F12` → **Network** → 随便点一门课的「选课」按钮
3. 在请求头里找到：
   - `Token: xxxxx` ← 整个值复制出来
   - `Cookie: JSESSIONID=...` ← 整行复制出来
4. 运行：

```bash
bitxk grab --token 'xxxxx' --cookie 'JSESSIONID=...; route=...'
```

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

### `bitxk check` 报「登录页结构已变更」

学校改版了 SSO 页面。先用[手动导入登录态](#兜底方案手动导入登录态)顶上，然后欢迎提 issue。

### 登录返回 401 / 「账号或密码错误」

1. 先在浏览器里手动登录一次，确认密码没记错；
2. 若浏览器能登而脚本不能，试 `bitxk grab --encrypt-mode cbc`；
3. 连续失败会触发验证码，工具检测到后会**主动停止**而不是继续硬试（避免锁账号）。

### 提示「当前不在可选课时间内」

正常行为，直接退出（退出码 3）。选课批次没开放时，接口不会返回任何 `canSelect=1` 的批次。用 `bitxk login` 可以看到所有批次及其时间窗。

### 一直显示「已满」但浏览器里明明有余量

大概率是容量字段命名与预期不同。用 `-v` 跑一次，日志里会打印原始响应字段；`TeachingClass` 会记录 `capacity_source` 说明它是从哪个字段推出来的。请带日志提 issue。

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
├── cli.py            命令行界面（init/check/login/list/grab）
├── auth.py           SSO 登录、密码加密、会话持久化、手动导入
├── http.py           HTTP 传输层：限速、重试、限流识别
├── client.py         选课系统业务接口（查询/选课/批次）
├── poller.py         轮询引擎（本项目核心）
├── models.py         数据模型与容量推断
├── config.py         TOML 配置加载与校验
├── notify.py         响铃与系统通知
└── exceptions.py     分层异常

tests/                163 个测试，全部用假 HTTP 层，不发真实请求
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
