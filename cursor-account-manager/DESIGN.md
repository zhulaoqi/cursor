# Cursor Account Manager — 设计文档

独立项目，用于批量管理 50+ 个已注册的 Cursor 账号：**自动登录**、**token 管理**、
**批量拉取使用数据 / 订阅信息 / 发票**、**多格式导出**。

与 `cursor-account-automation/`（负责注册）解耦，**不复用其代码**。

---

## 1. 目录结构

```
cursor-account-manager/
├── cam/
│   ├── __init__.py
│   ├── cli.py                # CLI 入口
│   ├── config.py             # .env + 全局常量
│   ├── models.py             # dataclass: Account / TokenRecord / UsageSnapshot
│   ├── logger.py             # 统一日志
│   ├── account_store.py      # 读 accounts.csv
│   ├── token_store.py        # SQLite tokens.db CRUD
│   ├── token_manager.py      # 核心：缓存 / 刷新 / 浏览器兜底
│   ├── email_client.py       # 飞书 IMAP 轮询 Cursor 验证码
│   ├── turnstile_solver.py   # CapSolver / 2Captcha 二合一求解器
│   ├── browser_login.py      # Playwright 登录 + PKCE 拿 token
│   ├── api_client.py         # 调用 Cursor 内部 API
│   └── exporter.py           # JSON / CSV / PDF 导出
├── tests/
├── data/
│   ├── accounts.csv
│   ├── tokens.db             # (.gitignore)
│   └── exports/
│       ├── raw/{email}.json
│       └── invoices/{email}/
├── requirements.txt
├── .env.example
└── README.md
```

## 2. 输入：`accounts.csv`

飞书邮箱，每账号一份 IMAP 授权密码：

```csv
email,imap_password
alice@feishu.cn,xxxx_yyyy_zzzz
bob@feishu.cn,aaaa_bbbb_cccc
```

可选列：`imap_host,imap_port`（不填用 `.env` 里的默认值 `imap.feishu.cn:993`）。

## 3. 存储：`tokens.db` (SQLite)

```sql
CREATE TABLE tokens (
    email                TEXT PRIMARY KEY,
    access_token         TEXT,
    refresh_token        TEXT,
    expires_at           INTEGER,     -- unix ts，由 JWT exp 解析得到
    last_refreshed_at    INTEGER,
    last_login_at        INTEGER,
    consecutive_failures INTEGER DEFAULT 0,
    status               TEXT DEFAULT 'active',  -- active / disabled
    note                 TEXT
);

CREATE TABLE audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    email  TEXT,
    ts     INTEGER,
    action TEXT,   -- refresh_ok / refresh_fail / browser_login / api_401
    detail TEXT
);
```

## 4. Token 获取流程

`token_manager.get_valid_token(email)`：

```
① 读 SQLite
   ├─ 无记录              → ④ 浏览器登录
   ├─ access_token 有效  → 返回（含 5 分钟裕量）
   └─ 过期/无
       ├─ 有 refresh_token → ② 刷新
       └─ 无               → ④ 浏览器登录

② POST https://api2.cursor.sh/oauth/token
   body = {
     grant_type:   "refresh_token",
     client_id:    "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB",
     refresh_token: "<stored>"
   }
   ├─ 200 + 新 token       → 写 SQLite，返回
   └─ 失败 / shouldLogout  → ④

④ browser_login.login(email, imap_pwd)
   - 全局 Semaphore(BROWSER_LOGIN_CONCURRENCY)，默认 1，串行
   - 成功 → 写 SQLite，consecutive_failures = 0
   - 失败 → consecutive_failures += 1
            ≥ 5 → status = 'disabled'，抛 TokenAcquisitionError
```

- per-email `threading.Lock` 防并发重入刷新
- JWT 解析：只拿 payload 的 `exp` 字段，不做签名校验

## 5. 浏览器登录（独立实现，Playwright）

1. 打开 `https://authenticator.cursor.sh/sign-in`
2. 填邮箱 → 点"使用邮箱验证码登录" → 跳 magic-code 页
3. IMAP 轮询飞书邮箱收 `no-reply@cursor.com` 验证码邮件（120s 超时）
4. 填 6 位验证码 → 登录成功
5. PKCE 流程：
   - 本地生成 `code_verifier` / `code_challenge` / `uuid`
   - 访问 `https://www.cursor.com/cn/loginDeepControl?challenge=...&uuid=...&mode=login`
   - 点"Yes, log me in" 确认按钮
   - 同时发起 `GET https://api2.cursor.sh/auth/poll?uuid=...&verifier=...` 轮询
   - 拿到 `{ authId, accessToken, refreshToken }`

Turnstile 遇到时：
- 优先靠 Playwright stealth 脚本自动过
- 过不了 → CapSolver / 2Captcha HTTP API 外部求解

## 6. API 客户端

`CursorClient(access_token, proxy=None)`：

| 方法 | HTTP | Endpoint |
|---|---|---|
| `get_current_period_usage()` | POST | `/aiserver.v1.DashboardService/GetCurrentPeriodUsage` |
| `get_plan_info()` | POST | `/aiserver.v1.DashboardService/GetPlanInfo` |
| `get_usage_limit_status()` | POST | `/aiserver.v1.DashboardService/GetUsageLimitStatusAndActiveGrants` |
| `get_usage_events(page_index=0, page_size=100)` | POST | `/aiserver.v1.DashboardService/GetFilteredUsageEvents` |
| `get_stripe_info()` | GET | `https://cursor.com/api/auth/stripe` |
| `list_invoices()` | GET | `https://cursor.com/api/dashboard/get-invoices`（经 Stripe customer portal） |
| `download_invoice(pdf_url, path)` | GET | 流式写磁盘 |

所有请求：
- Host: `api2.cursor.sh` → `Authorization: Bearer <access_token>`
- Host: `cursor.com` → Cookie: `WorkosCursorSessionToken=<access_token>`
- 401 → 抛 `TokenExpiredError`（token_manager 捕获后强刷）

## 7. CLI

```bash
python -m cam login --all                    # 首次批量浏览器登录
python -m cam login --email xxx@feishu.cn

python -m cam fetch --all                    # 拉全部数据，写入缓存
python -m cam fetch --email xxx --what usage,plan,invoices

python -m cam export --format csv --out data/exports/usage.csv
python -m cam export --format json --out data/exports/raw/
python -m cam invoices --download --out data/exports/invoices/

python -m cam status                         # 账号 token 状态 + 失败次数
python -m cam reset --email xxx              # 清空 token，强制重新登录
```

## 8. 并发策略

| 阶段 | 并发 | 说明 |
|---|---|---|
| 浏览器登录 | `Semaphore(1)` | 防资源爆炸 + 降指纹关联风险 |
| Token 刷新 | `Semaphore(5)` | HTTP，可并发 |
| API 拉数据 | `Semaphore(API_CONCURRENCY=10)` | 可配置 |
| IMAP | 每账号独立连接，用完即关 | 飞书有并发上限 |

单账号失败不中断整体，最后汇总成功 / 失败表格。

## 9. 依赖

```
playwright>=1.40
requests>=2.31
python-dotenv>=1.0
click>=8.1
```

可选：`capsolver`, `2captcha-python`（Turnstile 兜底）。
