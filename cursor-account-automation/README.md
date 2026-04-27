# Cursor 账号自动注册 + 订阅

基于 **DrissionPage** + **turnstilePatch** 的 Cursor 全流程自动化系统。  
覆盖：注册 → 登录 → 绑卡 → 订阅。

## 技术方案

| 层面 | 方案 | 说明 |
|------|------|------|
| 浏览器自动化 | DrissionPage (`Chromium` + `tab`) | 框架层面移除 `navigator.webdriver` |
| **Turnstile 求解** | **CapSolver API (推荐)** | 在提交前主动生成并注入有效 token，~$0.001/次 |
| Turnstile 免费方案 | CDP patch + Shadow DOM | 14 项反检测 patch + 可见复选框点击 |
| 表单填写 | `tab.actions.click().input()` | 链式 API + 鼠标移动模拟 |
| 邮箱验证码 | IMAP 轮询 | 自动获取 Cursor 发送的 6 位验证码 |
| Token 获取 | loginDeepControl + auth poll | 注册后通过 PKCE 流获取 accessToken/refreshToken |
| 本地写入 | Cursor SQLite 数据库 | 写入 Token 后重启 Cursor 即自动登录 |
| 订阅绑卡 | Stripe Checkout 自动填写 | 支持 Hosted Checkout 和 Embedded Elements 两种模式 |
| 代理转发 | asyncio 本地代理 | 自动处理带认证的住宅代理 |

## 环境要求

- Python 3.10+
- Chrome / Chromium 浏览器（系统安装）
- macOS / Windows / Linux

## 快速开始

```bash
# 1. 创建虚拟环境
python3 -m venv .venv

# 2. 激活
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置 .env（信用卡、代理等）
cp .env.example .env
# 编辑 .env 填写信用卡信息

# 5. 运行完整流程
python -u main.py --email cursor67@example.com --imap-password YOUR_IMAP_PWD
```

## 使用示例

```bash
# 完整流程：注册 + 登录 + 绑卡订阅（推荐）
python -u main.py --email cursor67@example.com --imap-password YOUR_IMAP_PWD

# 仅注册（不登录、不订阅）
python -u main.py --email cursor67@example.com --imap-password YOUR_IMAP_PWD --register-only

# 仅登录 + 订阅（已有账号，跳过注册）
python -u main.py --email cursor67@example.com --imap-password YOUR_IMAP_PWD --skip-register

# 注册 + 登录（跳过订阅）
python -u main.py --email cursor67@example.com --imap-password YOUR_IMAP_PWD --skip-subscribe

# 使用代理（推荐，住宅代理绕过 Cloudflare）
python -u main.py --email cursor67@example.com --imap-password YOUR_IMAP_PWD \
    --proxy http://user:pass@brd.superproxy.io:33335

# 指定姓名
python -u main.py --email cursor67@example.com --imap-password YOUR_IMAP_PWD \
    --first-name John --last-name Doe

# 无头模式（服务器部署）
python -u main.py --email cursor67@example.com --imap-password YOUR_IMAP_PWD --headless

# 传统密码流程（不推荐，密码页 Turnstile 更严格）
python -u main.py --email cursor67@example.com --imap-password YOUR_IMAP_PWD --use-password
```

### 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| `--email` | 是 | 注册/登录邮箱 |
| `--imap-password` | 是 | 该邮箱的 IMAP 授权密码 |
| `--first-name` | 否 | 名（不传则随机） |
| `--last-name` | 否 | 姓（不传则随机） |
| `--proxy` | 否 | 代理地址，支持 `http://user:pass@host:port` |
| `--headless` | 否 | 无头模式 |
| `--use-password` | 否 | 走密码流程（默认走邮箱验证码） |
| `--register-only` | 否 | 仅注册，不登录不订阅 |
| `--skip-register` | 否 | 跳过注册，直接登录 + 订阅 |
| `--skip-subscribe` | 否 | 跳过订阅步骤 |

### .env 配置

```env
# Turnstile 求解（解决"访问被阻止"，强烈推荐）
# 注册 CapSolver 送免费额度：https://dashboard.capsolver.com
CAPSOLVER_API_KEY=CAP-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 信用卡信息（绑卡必填）
CARD_NUMBER=4111111111111111
CARD_EXP_MONTH=09
CARD_EXP_YEAR=2027
CARD_CVV=123
CARD_HOLDER=ZHANG SAN
CARD_ZIP=100000

# 订阅计划（Pro / Pro+ / Ultra）
PLAN_NAME=Pro

# 代理（推荐：住宅代理）
PROXY=http://user:pass@brd.superproxy.io:33335
```

## 执行流程

```
阶段一：注册
  → 预热浏览器（积累信任分）
  → 打开注册页 → 若配置 CapSolver，优先主动求解 Turnstile
  → 填姓名/邮箱 → 提交
  → 密码页优先主动求解 Turnstile
  → 点击「使用邮箱验证码继续」
  → 邮箱验证码（IMAP 自动获取）
  → 注册完成

阶段二：登录
  → 打开登录页 → 填邮箱
  → 进入密码页后优先主动求解 Turnstile
  → 走「邮箱验证码登录」路径
  → IMAP 获取验证码 → 填入
  → 登录完成

阶段三：获取 Token
  → loginDeepControl 页面（PKCE 认证）
  → 轮询 auth poll API 获取 accessToken / refreshToken
  → 写入 Cursor 本地 SQLite 数据库

阶段四：绑卡 & 订阅
  → 打开 billing 页面
  → 选择目标套餐（Pro / Pro+ / Ultra）
  → Stripe Checkout 页面填写信用卡
  → 提交支付 → 验证订阅成功
```

## 项目结构

```
cursor-account-automation/
├── main.py              # CLI 入口 & 四阶段流程编排
├── browser.py           # DrissionPage 浏览器管理 + CDP patch 注入
├── registration.py      # 注册 / 登录 / Turnstile 处理
├── subscription.py      # 绑卡 / 订阅（Stripe Checkout）
├── cursor_auth.py       # Token 获取 (auth poll) & Cursor 数据库写入
├── email_client.py      # IMAP 邮箱验证码获取
├── config.py            # 配置加载（.env + CLI 参数）
├── output.py            # 结果输出 & 账号持久化
├── proxy_helper.py      # 带认证代理 → 本地转发
├── turnstilePatch/      # Chrome 扩展源码（已改为 CDP 注入）
│   ├── manifest.json
│   └── patch.js
├── .env                 # 固定配置（信用卡、代理等）
├── requirements.txt     # Python 依赖
└── accounts.txt         # 注册成功的账号记录（自动生成）
```

## 反阻止策略

| 优先级 | 策略 | 说明 |
|--------|------|------|
| **1** | **CapSolver 外部求解** | 浏览器自身验证失败时自动调用，生成有效 token 注入页面 |
| 2 | 邮箱验证码路径 | 跳过密码页，绕过二次严格 Turnstile |
| 3 | CDP 反检测 patch (14项) | screenX/screenY、webdriver、console.debug、Error.stack、plugins、languages 等 |
| 4 | Shadow DOM 遍历 | 穿透 Turnstile iframe 找到并点击可见 checkbox |
| 5 | 住宅代理 | 支持 `http://user:pass@host:port`（自动本地转发） |
| 6 | 行为模拟 | 鼠标移动、随机等待、滚动预热 |
| 7 | Session 清理 | 重试前自动清空 cookies/cache/storage |
| 8 | 自动重试 | 被阻止时递增等待后重试（最多 5 次） |

### 为什么需要 CapSolver？

Cloudflare Turnstile 的无感验证会检测 CDP（Chrome DevTools Protocol）调试端口的存在。DrissionPage 必须通过 CDP 控制浏览器，因此 Turnstile 会返回**失败 token**（看起来有 token，但服务端验证为 fail）。

CapSolver 使用自己的浏览器基础设施生成真正有效的 token，绕过了所有浏览器端检测。

| 项目 | 详情 |
|------|------|
| 注册地址 | https://dashboard.capsolver.com |
| 费用 | ~$0.001/次（约 ¥7/1000 次） |
| 注册 | 新用户有免费额度 |
| 速度 | 5-15 秒/次 |
