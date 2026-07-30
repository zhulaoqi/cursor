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
- ✅ **账期净支出导出**：按 Billing Invoices 列表计算 `Σ Amount − Σ Status 退款额`，独立 Excel（与 PDF 拉取并存）
- ✅ **每账号独立目录**：`dump` 命令一键生成账号文件夹 + 发票 PDF + 全账号汇总
- ✅ **BI 日同步（StarRocks）**：按日落库 ODS 原始明细，支持失败账号补拉
- ✅ **同步监控页**：查看今日同步状态、阶段时间、失败账号并一键补拉

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
email,imap_password,imap_host,imap_port,feishu_email
cursor183@eclicktech.com.cn,S0oefdkzkUfTnlMY,imap.feishu.cn,993,owner@example.com
cursor184@eclicktech.com.cn,AnotherImapPwd,,,owner@example.com
```

每个飞书邮箱需要在飞书邮箱管理台 → 设置 → IMAP/SMTP → 生成**客户端专用密码**，把该密码填入 `imap_password` 列。

> `feishu_email` 必填，用于账单同步 ODS 底表的归属字段。
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
BROWSER_LOGIN_CONCURRENCY=5        # 浏览器登录并发
INVOICE_DOWNLOAD_CONCURRENCY=8     # 账单 PDF 下载并发
API_CONCURRENCY=30                 # API 拉取并发

# Turnstile 兜底（任选其一或全填）
CAPSOLVER_API_KEY=
TWOCAPTCHA_API_KEY=

HEADLESS=true
VERIFICATION_CODE_TIMEOUT=120

# BI 日同步（StarRocks）
BI_SYNC_ENABLE=true
BI_SYNC_DB_URL=jdbc:mysql://host:9030/database
BI_SYNC_DB_USERNAME=
BI_SYNC_DB_PASSWORD=
BI_SYNC_CRON=30 1 * * *
BI_SYNC_LOCK_FILE=/tmp/cam_bi_sync.lock

# 告警机器人（可选）
ALERT_BOT_ENABLE=false
ALERT_BOT_PROVIDER=feishu
ALERT_TO_EMAILS=alice@company.com,bob@company.com
```

> 非飞书类邮箱（如 awsapps / WorkMail）若未显式传 `timeout`，系统会自动延长等待窗口（至少 240s）以适配慢投递。

---

## Cursor 用量监控

用量监控是独立于 BI 日同步与账期净支出的后台能力；启用后会采集 Cursor 当前滚动账期的套餐与用量快照，并在账期结束时形成可用于低用量分析的最终记录。

### 配置与启动边界

在 `.env` 中配置以下开关与参数：

```ini
# 总开关；设为 false 时不启动用量调度器
USAGE_SNAPSHOT_ENABLE=true

# periodic：按固定间隔记录当前账期的普通快照
USAGE_PERIODIC_INTERVAL_HOURS=24
USAGE_BOOTSTRAP_STALE_HOURS=36
USAGE_SNAPSHOT_CONCURRENCY=10

# pre-reset：扫描即将结束的账期，并在目标窗口采集最终候选快照
USAGE_PRE_RESET_SCAN_INTERVAL_MIN=15
USAGE_PRE_RESET_WINDOW_START_MIN=360
USAGE_PRE_RESET_TARGET_OFFSET_MIN=180
USAGE_PRE_RESET_WINDOW_END_MIN=30

# 跨进程任务锁和账号锁
USAGE_PERIODIC_LOCK_FILE=data/cam_usage_periodic.lock
USAGE_PRE_RESET_LOCK_FILE=data/cam_usage_pre_reset.lock
USAGE_ACCOUNT_LOCK_DIR=data/usage-account-locks
```

`periodic` 用于观察账期内的趋势，同一账期可有多个时间槽快照；`pre-reset` 面向账期临近重置时的最终快照，按账号和账期去重。系统以 API 返回的账期起止时间为准，不以本地日历推断重置时间。

数据使用 `cursor_accounts` 关联账号归属，`cursor_usage_snapshot` 保存 UTC 毫秒级用量快照和账期最终状态，`cursor_billing_ledger_summary` 保留既有自然月账单汇总。三者通过规范化 email 关联，但时间粒度不同：快照对应滚动账期，Ledger 对应自然月。

### CLI 运维命令

```bash
# 采集所有可监控账号的 periodic 快照
.venv/bin/python -m cam usage-snapshot --all --type periodic

# 仅采集指定账号（--email 可重复）
.venv/bin/python -m cam usage-snapshot --email user@example.com --type periodic

# 执行到期的 pre-reset 采集；先用 dry-run 预览
.venv/bin/python -m cam usage-pre-reset-due --dry-run
.venv/bin/python -m cam usage-pre-reset-due

# 显式修复某个账期的结算状态（需保留操作者和原因）
.venv/bin/python -m cam usage-finalize \
  --email user@example.com \
  --cycle-start 2026-07-01T00:00:00Z \
  --actor operator@example.com \
  --reason "人工核对后的修复"
```

上述命令以当前工作区的 `cam/cli.py` 实现为准。若目标分支尚未合入该 CLI 改动，请仅将其视为规划命令，不要据此执行真实环境操作。

### 数据口径与等级

- 套餐档位取自 Cursor 返回的套餐名称并规范化；**不得**根据金额、发票金额或 Ledger 金额推断套餐。
- `pre_reset` 是优先使用的账期最终来源。若账期切换时没有可用的 `pre-reset`，系统可能以该账期最后一条 `periodic` 快照结算并标记 `periodic_fallback`。这表示降级来源，不能与真实 pre-reset 采集等同。
- 低用量等级按当前连续套餐档位段中已经完成且数据连续的账期计算：`L0` 为无连续低用量账期，`L1`/`L2`/`L3` 分别为连续 1/2/至少 3 个低用量账期；套餐未知、账期未结算或数据断裂时为 `UNKNOWN`，不应据此作业务判断。
- `cursor_billing_ledger_summary` 的金额是自然月账单口径，不是 Cursor 滚动账期的精确成本；不得将其作为滚动账期用量或套餐成本的精确结论。

### MySQL 测试与安全

真实 MySQL 集成测试仅在完整设置以下环境变量后运行：`CAM_TEST_MYSQL_HOST`、`CAM_TEST_MYSQL_PORT`、`CAM_TEST_MYSQL_USER`、`CAM_TEST_MYSQL_PASSWORD`、`CAM_TEST_MYSQL_DATABASE`。测试库名必须包含 `test`；发布矩阵还需设置 `CAM_REQUIRE_MYSQL_TESTS=1` 和 `CAM_TEST_MYSQL_EXPECTED_VERSION=5.7` 或 `8.0`。未配置时测试会明确跳过，不代表已验证真实 MySQL。

数据库密码、IMAP 密码、Token、Cookie、代理凭据和告警密钥只能放在未提交的 `.env` 或部署环境密钥管理中；不要写入 README、账号 CSV、日志、原始 payload 或版本库。

Windows 服务运行账户必须对 `USAGE_ACCOUNT_LOCK_DIR`、`USAGE_PERIODIC_LOCK_FILE` 和 `USAGE_PRE_RESET_LOCK_FILE` 的父目录拥有创建及写入权限。建议在服务启动前创建 `data\usage-account-locks`，并使用服务账户实际验证锁目录可写。

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
2. **确认账号**：勾选要拉取的账号，在筛选区选择**账单月份**
3. **开始拉取**（可选）：开启/关闭「下载发票 PDF」「Token 汇总」等，完成后下载 ZIP / Excel
4. **导出账期净支出**（独立功能）：选中账号后点吸附条上的「导出账期净支出」，按 Billing 列表计算  
   `账期真实总支出 = Σ(Paid/Refunded 的 Amount 列) − Σ(Refunded 的 Status 括号退款额)`，下载双 Sheet Excel（汇总 + 原始明细）。与 PDF 拉取互不冲突。

### 账号表格式（CSV 示例）

```csv
email,imap_password,imap_host,imap_port,feishu_email
cursor183@eclicktech.com.cn,S0oefdkzkUfTnlMY,imap.feishu.cn,993,owner@example.com
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

```

`--what` 可选值：`usage` / `plan` / `limit` / `events` / `stripe` / `all`

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

### 5. BI 日同步（StarRocks）

```bash
# 手动执行一次（默认同步昨天，按北京时间）
.venv/bin/python -m cam sync-daily

# 指定日期执行
.venv/bin/python -m cam sync-daily --biz-date 2026-05-11

# 按 run_id 重跑失败账号
.venv/bin/python -m cam sync-retry --run-id 20260511_ab12cd34

# 调度模式：执行一次调度任务
.venv/bin/python -m cam sync-scheduler-once

# 调度模式：常驻循环（MVP）
.venv/bin/python -m cam sync-scheduler-loop
```

Web UI 中可点击“同步监控”查看：
- 今日同步是否成功
- 开始/结束时间
- 阶段日志
- 失败账号与一键补拉

ODS 表新增字段 SQL：

```sql
ALTER TABLE dataeye_customer.ods_cursor_usage_events_di
ADD COLUMN feishu_email VARCHAR(320) NULL COMMENT '飞书邮箱，来源于账号库上传或手动新增';

ALTER TABLE dataeye_customer.ods_cursor_usage_events_di
ADD COLUMN plan_amount DECIMAL(10,2) NULL COMMENT '套餐金额数字，来源于账号看板 Current Plan，例如 Ultra $20/mo 取 20';
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

---

## 技术文档

- **[docs/技术说明与学习定位.md](docs/技术说明与学习定位.md)** — 技术栈、架构分层、业务功能图解、API 与配置速查（新人学习入口）
- `docs/starrocks-daily-sync-tech-design.md` — BI 日同步详细设计
