# Cursor Account Manager

批量管理已注册的 Cursor 账号：**自动登录 → token 管理 → 拉取使用数据 / 订阅 / 发票 → 多格式导出**。

支持两种使用方式：
- **命令行（CLI）**：适合脚本化、定时任务
- **Web UI（二期）**：浏览器上传账号表，可视化拉取 & 一键下载 Excel

独立项目，与 `cursor-account-automation/`（负责注册）解耦。

---

## 特性

- ✅ **Web UI**：上传 CSV/Excel → 实时进度 → 下载汇总 Excel
- ✅ **CSV 批量账号**：一行一个账号（飞书邮箱 + IMAP 授权密码）
- ✅ **Token 持久化**：SQLite `tokens.db`，自动管理过期 / 刷新 / 重登
- ✅ **浏览器登录兜底**：patchright（undetected Playwright）反检测，串行执行防资源爆炸
- ✅ **Cookie 快路径**：已登录 profile 直接读 `WorkosCursorSessionToken`，跳过邮箱验证码，约 10s 完成
- ✅ **批量 API 调用**：拉取 usage / plan / 配额 / 使用事件（全量分页）/ 订阅 / 发票，并发可控
- ✅ **Excel 多 Sheet 导出**：账号概览 / 使用明细（含 token 数 / 成本 USD）/ 发票清单
- ✅ **每账号独立目录**：`dump` 命令一键生成账号文件夹 + 发票 PDF + 全账号汇总

---

## 安装

```bash
# 1. 进入项目目录
cd cursor-account-manager

# 2. 创建虚拟环境
python3 -m venv .venv

# 3. 安装依赖（所有命令都要用 .venv/bin/python 调用）
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m patchright install chromium

# 4. 准备配置
cp .env.example .env
# 编辑 .env（至少填 PROXY，飞书账号不需要信用卡字段）
```

> **注意**：所有 `python` 命令都必须用 `.venv/bin/python`，不要用系统 Python。

---

## 配置

### `data/accounts.csv`

```csv
email,imap_password,imap_host,imap_port
cursor183@eclicktech.com.cn,S0oefdkzkUfTnlMY,imap.feishu.cn,993
cursor184@eclicktech.com.cn,AnotherImapPwd,,
```

每个飞书邮箱需要在飞书邮箱管理台 → 设置 → IMAP/SMTP → 生成**客户端专用密码**，把该密码填入 `imap_password` 列。

> `imap_host`、`imap_port` 不填则使用 `.env` 的默认值 `imap.feishu.cn:993`。

### `.env`

```ini
DEFAULT_IMAP_HOST=imap.feishu.cn
DEFAULT_IMAP_PORT=993
# 不同邮箱服务商垃圾箱命名不同，可按需调整（逗号分隔）
IMAP_SEARCH_FOLDERS=INBOX,Junk,Spam

# 强烈建议配代理，降低 Cloudflare 阻断率
PROXY=http://user:pass@host:port

# 并发控制
BROWSER_LOGIN_CONCURRENCY=1   # 浏览器登录串行
API_CONCURRENCY=10            # API 拉取并发

# Turnstile 兜底（任选其一或全填）
CAPSOLVER_API_KEY=
TWOCAPTCHA_API_KEY=

HEADLESS=true
VERIFICATION_CODE_TIMEOUT=120
```

> 非飞书类邮箱（如 awsapps / WorkMail）若未显式传 `timeout`，系统会自动延长等待窗口（至少 240s）以适配慢投递。

---

## 使用方式一：Web UI（推荐）

### 启动服务

```bash
cd cursor-account-manager
.venv/bin/python -m cam web
# 浏览器打开 → http://localhost:8765/
```

停止服务：`Ctrl+C`

### 操作流程

1. **上传账号表**：拖拽或点击上传 `.csv` / `.xlsx`，也可手动逐条添加
2. **确认账号**：勾选要拉取的账号，选择数据月份，开启/关闭"下载发票 PDF"
3. **一键拉取**：实时进度展示，完成后下载汇总 Excel

### 账号表格式（CSV 示例）

```csv
email,imap_password,imap_host,imap_port
cursor183@eclicktech.com.cn,S0oefdkzkUfTnlMY,imap.feishu.cn,993
```

页面内也提供"下载模板"按钮。

---

## 使用方式二：命令行（CLI）

### 1. 登录（首次 / token 过期时）

```bash
# 所有账号逐个浏览器登录，写入 tokens.db（串行，每个约 10-60s）
.venv/bin/python -m cam login --all

# 只登录指定账号
.venv/bin/python -m cam login --email cursor183@eclicktech.com.cn

# 忽略现有 token，强制重登
.venv/bin/python -m cam login --all --force
```

### 2. 一键导出（推荐）

```bash
# 每账号一个文件夹（Excel + 发票 PDF + raw JSON）+ 全账号汇总
.venv/bin/python -m cam dump --all

# 不下载发票 PDF、不保存 raw JSON（只要 Excel）
.venv/bin/python -m cam dump --all --no-invoices --no-raw

# 指定输出目录
.venv/bin/python -m cam dump --all --out-dir ~/cursor-backup
```

**输出结构：**

```
data/exports/accounts/
├── _summary.xlsx                              ← 全账号汇总（3 Sheet）
├── cursor183@eclicktech.com.cn/
│   ├── cursor183@eclicktech.com.cn.xlsx      ← 账号概览 / 使用明细 / 发票
│   ├── raw.json                              ← 原始 API 响应
│   └── invoices/*.pdf                        ← 发票 PDF
└── cursor184@.../
    └── ...
```

### 3. 分步操作

```bash
# 只拉数据，写入 data/exports/raw/
.venv/bin/python -m cam fetch --all

# 只拉指定类型
.venv/bin/python -m cam fetch --all --what usage,plan,events

# 从已有 JSON 生成 Excel（不重新拉）
.venv/bin/python -m cam export --from-dir data/exports/raw --out data/exports/report.xlsx

# 下载发票 PDF
.venv/bin/python -m cam invoices --all --download --out data/exports/invoices/
```

`--what` 可选值：`usage` / `plan` / `limit` / `events` / `stripe` / `invoices` / `all`

### 4. 维护

```bash
# 查看所有账号 token 状态
.venv/bin/python -m cam status

# 清除某账号的 token（下次自动重新登录）
.venv/bin/python -m cam reset --email cursor183@eclicktech.com.cn --yes

# 清除所有账号 token
.venv/bin/python -m cam reset --all --yes

# 同时删除浏览器 profile（彻底重置）
.venv/bin/python -m cam reset --email cursor183@eclicktech.com.cn --profile --yes

# 独立测试 IMAP 连接（不启动浏览器）
.venv/bin/python -m cam test-imap --email cursor183@eclicktech.com.cn
```

---

## Excel 输出说明

每份 Excel 包含 3 个 Sheet：

| Sheet | 内容 | 主要列 |
|---|---|---|
| **账号概览** | 每账号一行 | 套餐、订阅状态、计费周期、已用/剩余/额度（USD）、使用率 % |
| **使用明细** | 所有 API 调用事件 | 时间、模型、输入/输出/缓存 Tokens、总成本 / 扣费（USD）、折扣 % |
| **发票** | 发票清单 | 发票号、状态、金额（USD）、PDF 链接 |

特性：首行冻结 + 自动筛选 + 列宽自适应，金额统一转换为 USD。

---

## 架构概览

```
  ┌─────────────────────────────┐
  │  Web UI / CLI               │
  │  上传 CSV → 选账号 → 运行   │
  └──────────────┬──────────────┘
                 ▼
  ┌─────────────────────────────┐
  │  TokenManager               │
  │  1. 读 tokens.db 缓存       │
  │  2. 过期 → cookie 快路径    │  ← patchright Chrome profile
  │     (10s，跳过验证码)       │
  │  3. profile 无 session      │
  │     → 邮箱验证码登录        │  ← IMAP 飞书邮箱
  └──────────────┬──────────────┘
                 ▼
  ┌─────────────────────────────┐
  │  CursorClient               │
  │  api2.cursor.sh  Bearer JWT │
  │  cursor.com      Cookie     │
  └──────────────┬──────────────┘
                 ▼
  ┌─────────────────────────────┐
  │  Exporter                   │
  │  JSON / Excel(多Sheet) / PDF│
  └─────────────────────────────┘
```

---

## 常见问题

**Q: 第一次 login 所有账号要多久？**

- 有旧 session（已登录过）：约 10s / 账号（cookie 快路径）
- 全新账号需走 IMAP 验证码：约 40-60s / 账号
- 50 账号全新登录 ≈ 30-50 分钟（串行）。后续跑 `fetch` / `dump` 走 token 缓存，秒级。

**Q: 为什么不用 `python` 而要用 `.venv/bin/python`？**

项目的依赖（patchright、openpyxl、fastapi 等）装在虚拟环境里，系统 Python 没有这些包。
也可以先 `source .venv/bin/activate` 激活虚拟环境，激活后直接用 `python` 即可。

**Q: refresh_token 什么时候会失效？**

- Cursor 后端主动吊销
- 账号被标记异常（IP 突变、多地登录）

本项目用 Cookie 路径，没有 refresh_token，token 过期时自动触发浏览器重登（命中 profile 缓存约 10s）。

**Q: 为什么浏览器登录要串行？**

1. 50 个 Chromium 同时跑会爆内存
2. 同 IP 短时间内多账号登录容易被 Cloudflare 关联标记
3. IMAP 有并发连接数上限

**Q: 发票接口返回空？**

Cursor 的发票 API 路径历史变过几次，本项目同时尝试了多个已知路径。若均为空，建议登录网页版 → Dashboard → Billing → Manage Subscription 进 Stripe Portal 手动下载。

**Q: 长期运行如何避免被封？**

- 配代理（`PROXY=`），每批账号换 IP
- `API_CONCURRENCY` 调到 3-5
- 用 cron 定时跑 `dump --all`，一天一次即可
