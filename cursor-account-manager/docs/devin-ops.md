# Devin 平台运维手册

> 平台：<https://app.devin.ai/> ｜ 页面：Web 服务 `/devin`（顶部「Cursor / Devin」菜单切换）
> 采集内容：套餐（如 Max $200/月）、续费日、周额度已用 % 与重置时间、按需用量与剩余余额、消息用量上限。

## 1. 整体结构

| 层 | 文件 | 作用 |
|---|---|---|
| 配置 | `cam/devin_config.py` | `DEVIN_*` 环境变量，每次调用重新读取 |
| 存储 | `cam/devin_store.py` | SQLite（与 `tokens.db` 同库）：`devin_accounts` / `devin_snapshots`（含 `raw_json`）/ `devin_cycles`（账期）/ `devin_runs` |
| 采集 | `cam/devin_collector.py` | 独立 Chrome profile（`devin__<邮箱>`）登录 → 取验证码 → 抓页面 + `api.devin.ai` 响应 |
| 解析 | `cam/devin_parser.py` | API JSON 优先、页面文本兜底，合并（来源 `api` / `dom` / `api+dom`） |
| 调度/批跑 | `cam/devin_runner.py`、`cam/devin_scheduler.py` | 每日凌晨批跑、重试、双层锁、飞书播报 |
| 接口 | `cam/devin_api.py` | `/api/devin/*` |
| 前端 | `cam/static/devin.html`、`platforms.js` | 用量监控 / 账号管理 / 导入账号 / 采集记录 |
| 发票导出 | `cam/devin_invoices.py` | 实时读取发票页 → 直接导出 xlsx（不入库） |
| 运维 CLI | `cam/devin_cli.py` | `python -m cam devin-*` |

与 Cursor 完全独立：账号库独立（`devin_accounts`），不会读写 Cursor 的账号 CSV 或 `usage_snapshots`。

新增平台菜单：在 `cam/static/platforms.js` 的 `CAM_PLATFORMS` 数组里加一行，并给新页面放一个 `<nav data-platform-nav data-active="xxx">`。

## 2. 每日调度

- 随 Web 服务启动（`python -m cam web`），默认每天 **02:30**（`DEVIN_DAILY_AT`，时区 `BI_SYNC_BIZ_TZ`=Asia/Shanghai）。
  错开：BI 同步 01:30、额度刷新 03:00、Cursor 用量 06:00。
- 并发 `DEVIN_CONCURRENCY`（默认 3），单账号最多 `DEVIN_MAX_ATTEMPTS`（默认 2）次；IMAP 认证失败、账号不存在不重试。
- 防重复：进程内锁 + 文件锁 `DEVIN_LOCK_FILE`；手动采集与定时采集互斥。
- 服务异常退出导致 `running` 记录卡住时，下次启动自动标记为失败。
- 关闭：`DEVIN_SCHEDULE_ENABLE=false`。

### 飞书播报（定时任务结束后）
卡片字段：平台、触发类型、日期、状态（成功 / 部分失败 / 全部失败）、run_id、账号总数 / 成功 / 失败 / 跳过、耗时；
详情：失败原因分布、失败账号清单（最多 15 个）、套餐分布、周额度 ≥80% 的账号。
账号数为 0 不发送。需要 `ALERT_BOT_ENABLE=true`；`DEVIN_ALERT_ENABLE=false` 可单独关闭 Devin 播报。
页面「采集记录」→「重发最近一次飞书通知」可补发。

## 3. 账期（入库）与发票（直接导出）

**账期：保存在数据库 `devin_cycles`。**
- 每个账号每个计费周期一行，主键 `(email, cycle_end)`；`cycle_end` = 页面上的 "Renews on …" 续费日，
  `cycle_start` = 续费日往前推一个计费周期（月付 −1 月，年付 −1 年，月末自动截断，如 3/31 → 2/28）。
- 每次**成功**采集自动刷新：周额度/按需已用的「最新值」与「峰值」、按需余额、采样次数、首末次采集时间。
  周期结束后最后一次采样 ≈ 该账期最终用量。续费后 `renews_at` 前移，自动新开一行，旧账期保留。
- Free 套餐没有续费日，不产生账期；失败的采集不写账期。
- 查看：页面「账期与发票」、`GET /api/devin/cycles`、`scripts/devin-ops.sh cycles`、Excel 导出里的「账期」工作表。
- 用量监控表里的「账期」列显示起止日期与剩余天数（≤3 天标红），7 天内续费的账号数显示在顶部状态条。

**发票：实时读取，直接导出 Excel，不写数据库。**
- 页面「账期与发票」→「导出全部账号发票」，或账号详情抽屉里的「导出发票」（单账号）；
  CLI：`scripts/devin-ops.sh invoices [--email X]`（`.ps1`：`invoices -Email a,b`）。
- 流程：登录 → 打开 `/settings/invoices` → 解析表格（日期 / 状态 / 金额）→ 取每行 "View invoice" 外链
  → 若页面 API 返回了发票号/PDF 地址则补全，并下载 PDF。
- 输出：`DEVIN_EXPORT_DIR`（默认 `data/exports/devin`）下 `devin-invoices-YYYYMMDD-HHMMSS.xlsx`，
  含「发票明细」（含可点击的发票链接）、「账号汇总」、「读取失败」三个工作表；PDF 在 `invoice-pdf/<邮箱>/`。
- 金额以页面可见文本为准；API 只用来补充发票号、链接和 PDF。
- 导出与每日采集共用进程内锁，二者不会同时跑（同一账号共用一个 Chrome profile）。导出期间触发的定时采集会被跳过并提示 `lock_busy`。
- 停用账号不会被读取。

## 4. 账号导入

页面 `/devin#import`，或 CLI：`scripts/devin-ops.sh import accounts.xlsx [--overwrite]`。

| 列（支持别名） | 说明 |
|---|---|
| `email` | Devin 登录邮箱（必填，自动转小写） |
| `imap_password` / `imap_pwd` / `password` | 该邮箱的 IMAP 授权码（用于收登录验证码） |
| `imap_host` / `imap_port` | 缺省用 `DEFAULT_IMAP_HOST/PORT` |
| `owner_email` / `feishu_email` / `飞书邮箱` | 负责人 |
| `note` / `备注` | 备注 |

模板下载：页面「导入账号」里的模板按钮，或 `GET /api/devin/accounts/template.xlsx`。
已存在账号默认跳过，`--overwrite`（页面「覆盖已存在」）才会更新；备注为空不会覆盖旧备注。

## 5. 常用命令

Linux / macOS / CentOS：`scripts/devin-ops.sh <命令>`；Windows：`.\scripts\devin-ops.ps1 <命令>`。

| 命令 | 作用 |
|---|---|
| `import <文件>` | 导入账号 |
| `status [--failed]` | 账号与最近一次结果（`-Failed`） |
| `collect [--email X]` | 立即采集，手动口径，默认不发飞书 |
| `daily` | 按定时任务口径采集并发飞书（cron 用） |
| `retry-failed` | 只重采最近失败的账号并发飞书 |
| `probe <邮箱> [--headed] [--save]` | 单账号探针：打印解析结果与原始数据；`--headed` 肉眼观察 |
| `notify-test [--send]` | 模拟数据生成播报卡片；`--send` 真发 |
| `cycles [--email X]` | 查看已入库的账期 |
| `invoices [--email X]` | 实时读取发票并导出 Excel（不入库） |
| `next-run` | 下次定时采集时间 |
| `artifacts [N]` | 失败现场（截图 + 页面文本） |
| `logs [N]` | 服务日志中 devin / ERROR 行 |
| `backup-db` | 备份数据库 |

底层等价命令：`python -m cam devin-import|devin-status|devin-collect|devin-probe|devin-notify-test|devin-cycles|devin-invoices|devin-next-run`。
`devin-collect` 退出码：全部成功 0，否则 1。

## 6. 首次上线检查（Windows 部署机 172.30.91.93）

1. 重新部署（`deploy-to-windows.sh`），确认 `.env` 出现 `DEVIN_*` 段；Windows 部署会保留 `DEVIN_SCHEDULE_ENABLE / DAILY_AT / CONCURRENCY / CODE_SENDER_KEYWORDS / PROXY` 的自定义值。
2. 重启服务（计划任务 `CamWebService`），日志应出现 Devin 调度启动信息；`devin-ops.ps1 next-run` 确认时间。
3. 导入 1 个账号，先 `probe <邮箱> -Headed` 看完整登录；通过后 `probe <邮箱> -Save` 落库并在 `/devin` 页面确认数据。
4. `notify-test -Send` 确认飞书卡片格式与到达。
5. 对同一个账号试一次 `invoices --email <邮箱>`，打开导出的 xlsx 核对金额/链接；解析不到时看 `artifacts` 里的 `no-invoices-parse` 现场。
6. 再批量导入全部账号，第二天早上看飞书播报与「采集记录」。

## 7. 故障排查（按错误码）

| 错误码 | 含义 | 处理 |
|---|---|---|
| `imap_auth` | 邮箱 IMAP 认证失败 | 授权码过期/错误；更新账号后重采（不会自动重试） |
| `code_timeout` | 验证码邮件超时未收到 | 先看邮箱是否收到 Devin 邮件；若收到但未识别，调 `DEVIN_CODE_SENDER_KEYWORDS`；或加大 `DEVIN_CODE_TIMEOUT_SEC` |
| `code_invalid` | 验证码被拒 | 多为取到旧码；`probe --headed` 观察，确认邮件搜索是否取到最新一封 |
| `account_not_found` | 账号不存在 | 邮箱未在 Devin 注册，停用或移除（不会重试） |
| `login_failed` | 登录流程未走通 | 看 `artifacts` 截图；页面改版时需调整 `devin_collector.py` 选择器 |
| `page_parse` | 登录成功但没解析出核心数据（发票页：未识别到发票行） | 看详情抽屉里的原始 JSON 与现场页面文本，补充 `devin_parser.py` 规则 |
| `network` | 网络/代理 | 检查 `DEVIN_PROXY` / `PROXY` |
| `browser` | 浏览器启动失败 | 检查 Chrome / patchright；profile 目录被占用时先停掉同账号任务 |
| `timeout` | 单账号超时 | 机器负载高时降 `DEVIN_CONCURRENCY` |
| `unknown` | 其他 | 看日志与现场文件 |

失败现场：`DEVIN_ARTIFACTS_DIR`（默认 `data/artifacts/devin`），文件名含时间与邮箱，错误信息末尾带 `[现场: 路径]`。

### 已知限制
- 登录页（`/auth/login`、邮箱框 `#auth1-email`、`Log in` 按钮）已对真实站点验证过；
  **登录后的真实 API 字段与页面文案需在第一个真实账号 `probe` 时确认**，解析失败时「详情 → 原始数据」会保留完整 JSON 便于调规则。
- 发票页的真实表格结构与 "View invoice" 是链接还是按钮需在第一个真实账号上确认；代码两种都处理（链接直接读 href，按钮则点击并捕获新开页面 URL）。
- 登录有效期内走 cookie 快路径，不再收验证码；cookie 失效会自动重新走验证码登录。

## 8. 备份与清理

- `backup-db` 使用 SQLite 在线备份（WAL 安全）。
- 页面删除账号默认保留历史快照，需同时清除时调 `DELETE /api/devin/accounts/{email}?purge=true`；`devin_snapshots` 每次采集（含失败）一行；`devin_cycles` 每账期一行（量很小，无需清理）；`DEVIN_EXPORT_DIR` 里的发票 xlsx/PDF 不会自动清理，长期运行可按需清理 90 天前数据。
- Chrome profile 位于 `CAM_CHROME_PROFILES_DIR`（目录名 `devin__*`），删除账号后可手动清理。
