# 账单列表净支出汇总 — 技术分析设计

> **文档类型：** 技术设计（Design）  
> **日期：** 2026-05-19  
> **状态：** 待评审 / 待实施  
> **关联需求：** 新增「账期净支出」能力：从 Cursor Billing **Invoices** 列表解析 Paid / Refunded 行并汇总为 Excel。**与现有发票 PDF 下载为独立功能，互不替代、可并存。**

---

## 1. 背景与目标

### 1.1 业务背景

仓库内已有两条能力线，**本次只新增第三条，不改动前两条**：

| 能力 | 入口 | 产出 |
|------|------|------|
| Token / 使用明细拉取 | 「开始拉取」+ 勾选汇总 | `汇总.xlsx` 等 |
| 发票 PDF 下载 | 「开始拉取」+ 勾选发票 PDF | `invoices/*.pdf` |
| **账期净支出（本期）** | **新按钮「导出账期净支出」** | **账期净支出 Excel** |

业务方新增诉求：在选定账期月份下，读取 Billing 页 **Invoices** 表格中的状态与金额，计算「账期真实总支出」并导出 Excel（含原始明细便于核对）。**未要求取消或替代 PDF 下载**；用户仍可照常使用「开始拉取」下载 PDF。

### 1.2 页面结构（以实际 UI 为准）

Cursor Dashboard → Billing → **Invoices**，右上角月份下拉（如 `2026年4月`）。表格为 **5 列**（与截图一致）：

| 列（EN） | 列（中文） | 说明 |
|----------|------------|------|
| Date | 日期 | 如 `2026年4月14日` |
| Description | 描述 | 常为空 |
| Status | 状态 | `Paid` 或 `Refunded (12.07 USD)` |
| Amount | 金额 | 如 `63.96 USD`、`21.32 USD` |
| Invoice | 发票 | `View` 外链（`invoice.stripe.com`） |

**真实样例（2026 年 4 月，单账号）：**

| 日期 | 描述 | 状态 | 金额 | 发票 |
|------|------|------|------|------|
| 2026年4月14日 | — | Paid | **63.96 USD** | View |
| 2026年4月02日 | — | **Refunded (12.07 USD)** | 21.32 USD | View |

**真实总支出（单账号、单账期）** 定义为：

```
账期真实总支出 = Σ(各行 Amount 列) − Σ(Refunded 行 Status 括号内退款额)
```

**计入规则（与业务口径一致）：**

| 状态 | Amount 列 | Status 括号内退款额 |
|------|-----------|---------------------|
| **Paid** | **计入**（加） | — |
| **Refunded** | **计入**（加，表示该行对应原支付/账单金额） | **扣除**（减，实际退款额） |
| Open 等 | 不计入 | — |

**上例（2026-04）：**

```
63.96 + 21.32 − 12.07 = 73.21 USD
```

即：Paid 行的 Amount **与** Refunded 行的 Amount **均累加**，再减去 Refunded 行 Status 中 `Refunded (12.07 USD)` 解析出的 **12.07**。  
明细 Sheet 须保留 Amount 列、Status 退款额、以及汇总用的「账期真实总支出」计算式字段，便于财务核对。

### 1.3 产品目标

| 目标 | 说明 |
|------|------|
| G1 | 按 **5 列表格** 解析：所有 Paid/Refunded 行的 **Amount 列均加总**；Refunded 行另减 Status 括号退款额 |
| G2 | 按用户选择的 **账单月份**（复用现有 `selectedMonth` / 页面月份下拉）过滤 |
| G3 | 输出 **Excel**：汇总表 + 原始明细表（含列表 Amount 与计入金额，便于审计） |
| G4 | **独立任务**：本按钮触发的任务仅生成净支出 Excel，**不改动**现有 PDF / Token 拉取逻辑 |
| G5 | 入口：账号库「开始拉取」**左侧**新按钮；进度页 / SSE / 下载 **复用** 现有拉取链路 |

### 1.4 非目标（本期不做）

- **不替代、不关闭** 现有「开始拉取」+ 发票 PDF 下载（两条链路并存）。
- 不替代现有 Token 汇总 / 使用明细拉取（`with_summary` 路径不变）。
- 不修改 BI 同步、套餐/按量定时任务。
- 不持久化账单明细到 SQLite（仅当次任务产出 Excel；若后续要历史查询可另开需求）。
- 不保证 Stripe 页面 UI 大改版后的零维护（需保留 JS 解析兜底与测试夹具）。

---

## 2. 现状架构梳理

### 2.1 相关模块

```
index.html (Alpine)
    │  selectedMonth、selectedEmails、startRun()
    ▼
POST /api/run  (web_server.py)
    │  fetch 阶段：fetcher.fetch_one (API，可选)
    ▼
exporter.export_per_account
    │  Phase 1: _download_invoices_all → PDF（浏览器）
    │  Phase 2: Excel / raw JSON
    │  Phase 3: 汇总.xlsx（Token）
```

账单列表解析核心在 `cam/exporter.py`：

| 符号 | 职责 |
|------|------|
| `_BILLING_URLS` | `dashboard/billing`、`settings/billing` |
| `_STATUS_JS` | 遍历 `tr`，取 `invoice.stripe.com` 链接、状态、日期 |
| `_select_billing_month_in_ctx` | 切换月份下拉 |
| `_filter_paid_billing_items` | 仅保留 `paid` / `refunded` 状态，按月份过滤 |
| `_normalize_status_text` | 含 `Refunded (12.07 USD)` → `refunded` |

已有测试 `tests/test_invoice_paid_filter.py` 证明 **refunded 行会被保留**，但：

- **未解析金额**（`12.07` 丢弃）；
- 后续逻辑只为 **下载 PDF** 服务，未做加减汇总。

### 2.2 可复用资产

| 资产 | 复用方式 |
|------|----------|
| 月份选择器 `selectedMonth` + `_billing_month_key` | 请求体 `month` 字段不变 |
| `POST /api/run` + SSE `GET /api/stream/{task_id}` | 同一任务模型 |
| 进度页 `view === 'run'`、`runItems`、`logLines` | 新增 phase 文案映射 |
| `GET /api/download/{token}`、`download_zip` | 任务结束下发 `download_token` |
| Playwright 单 Chromium 多 Context | 与 PDF 下载相同并发模型 |
| `TokenManager.get_valid_token` | 仅需登录态 Cookie，无需 usage API |

### 2.3 差距分析

| 能力 | 现状 | 目标 |
|------|------|------|
| 列表金额 | `_STATUS_JS` 无 `amount` 字段 | 扩展 JS + Python 解析 |
| 净支出计算 | 无 | 新增 `billing_ledger` 模块 |
| Excel 结构 | Token 汇总 / 发票 PDF 嵌入 | 专用「账期净支出」工作簿 |
| 任务类型 | `with_invoices` / `with_summary` 组合 | 新增 `with_billing_ledger` 或 `run_mode` |
| 前端入口 | 仅「开始拉取」 | 新增「导出账期净支出」按钮 |

---

## 3. 领域模型与计算规则

### 3.1 数据结构（建议）

```python
@dataclass(frozen=True)
class BillingListRow:
    """账单页 Invoices 表格单行（解析后）。"""
    date_text: str              # Date 列，如 2026年4月14日
    description_text: str       # Description 列（常为空）
    billing_month: str          # 由 date_text 归一化 YYYY-MM
    status_raw: str             # Status 列原文
    status: str                 # paid | refunded | open | ...
    list_amount_usd: Decimal | None   # Amount 列，如 63.96 / 21.32（Paid/Refunded 均参与加总）
    list_amount_raw: str        # Amount 列原文
    refund_in_status_usd: Decimal | None  # 仅 refunded：从 Status 解析，如 12.07（参与扣减）
    invoice_url: str            # Invoice / View 链接

@dataclass(frozen=True)
class BillingLedgerSummary:
  email: str
  feishu_email: str         # 来自账号库，便于对照
  billing_month: str        # YYYY-MM（用户选择）
  amount_total_usd: Decimal   # Σ Amount 列（Paid + Refunded）
  refund_total_usd: Decimal   # Σ Refunded 行 Status 括号退款额
  net_spend_usd: Decimal      # amount_total - refund_total
  row_count: int
  parse_warnings: list[str] # 如「某行无法解析金额」
```

### 3.2 状态与金额解析规则

**状态归一化**（在现有 `_normalize_status_text` 上扩展）：

| 原始示例 | `status` | Amount 列 | Status 退款额 |
|----------|----------|-----------|---------------|
| Paid | `paid` | **加** | — |
| Refunded (12.07 USD) | `refunded` | **加** | **减** 12.07 |
| Open / 未支付 | `open` | 不计入 | — |

**金额解析步骤：**

1. **Amount 列**（Paid / Refunded）：解析为 `list_amount_usd`，参与 **支付侧加总** `amount_total`。
2. **Refunded 行 Status**：解析 `Refunded (12.07 USD)` → `refund_in_status_usd`，参与 **退款侧扣减** `refund_total`。
3. 解析失败：对应项记入 `parse_warnings`，缺失项按 0 处理或整行跳过（实现时二选一，默认缺失 Amount 则该行不加、缺失括号退款则 Refunded 行只加 Amount 不减退款并 warning）。

Status 括号金额正则（示例）：

```python
_REFUND_IN_STATUS_RE = re.compile(
    r"refunded\s*\(\s*(?P<amt>\d+(?:\.\d+)?)\s*(?:USD|usd)?\s*\)",
    re.I,
)
```

Amount 列正则：`(?P<amt>\d+(?:\.\d+)?)\s*USD`（与列表展示 `63.96 USD` 一致）。

**净支出公式（固定，与业务口径一致）：**

```python
amount_total = sum(
    r.list_amount_usd
    for r in rows
    if r.status in ("paid", "refunded") and r.list_amount_usd is not None
)
refund_total = sum(
    r.refund_in_status_usd
    for r in rows
    if r.status == "refunded" and r.refund_in_status_usd is not None
)
net_spend_usd = amount_total - refund_total
# 上例：63.96 + 21.32 - 12.07 = 73.21
```

边界：

- 同月多笔支付、多笔退款：全部累加后相减。
- 仅有退款无支付：净支出为 **负数**（合法，Excel 原样展示）。
- 金额为 0 或缺失：跳过并告警，避免静默算错。

### 3.3 月份过滤

与现有一致，双重保障：

1. **UI 层**：用户必须选择 `selectedMonth`（与 PDF 拉取相同校验）；
2. **页面层**：`_select_billing_month_in_ctx` 切换下拉；
3. **行级兜底**：`_billing_month_key(row.date_text) == selected_month`。

---

## 4. Excel 输出设计

### 4.1 工作簿结构

单文件：`账期净支出_{YYYY-MM}_{task_label}.xlsx`（与现有 `汇总.xlsx` 命名风格一致）

**Sheet 1：`账期净支出汇总`**

| 列名（中文） | 字段 | 说明 |
|--------------|------|------|
| 账号邮箱 | email | Cursor 登录邮箱 |
| 飞书邮箱 | feishu_email | 账号库字段 |
| 账期月份 | billing_month | 用户选择的 YYYY-MM |
| Amount 列合计 (USD) | amount_total_usd | Σ(Paid + Refunded 行的 Amount 列) |
| Status 退款合计 (USD) | refund_total_usd | Σ(Refunded 行 Status 括号内金额) |
| **账期真实总支出 (USD)** | net_spend_usd | **amount_total − refund_total** |
| 账单行数 | row_count | 参与计算的明细行数 |
| 解析备注 | parse_warnings | 逗号拼接或首条警告 |

**Sheet 2：`账单原始明细`**

| 列名 | 说明 |
|------|------|
| 账号邮箱 | |
| 账期月份 | 用户选择月（非行日期所属月，便于筛选） |
| 列表日期 | date_text |
| 描述 | description_text |
| 日期所属账期 | billing_month（由 date_text 解析） |
| 状态原文 | status_raw |
| 状态 | paid/refunded/... |
| 列表金额列 (USD) | list_amount_usd（Amount 列） |
| 列表金额原文 | list_amount_raw |
| 状态内退款额 (USD) | refund_in_status_usd（仅 Refunded 行，扣减项） |
| 发票链接 | invoice_url |
| 行级备注 | 如「Amount 已计入加总」「退款额已扣减」 |

### 4.2 与现有导出的关系（并列，非互斥）

| 用户操作 | fetch API | 本任务是否下 PDF | Token 汇总 | 净支出 Excel |
|----------|-----------|------------------|------------|--------------|
| 「开始拉取」按原勾选 | 按勾选 | 按 `with_invoices` | 按 `with_summary` | 否 |
| **「导出账期净支出」** | 跳过（仅 token） | **本任务不执行**（用户仍可另开 PDF 任务） | 否 | **是** |

实现上使用 **独立布尔** `with_billing_ledger: bool` + **独立按钮**，与「开始拉取」解耦；**不要求**用户关闭 PDF 勾选，两条功能长期并存。

---

## 5. 后端流程设计

### 5.1 API 契约（扩展 `RunRequest`）

```python
class RunRequest(BaseModel):
    accounts: List[AccountRow]
    month: Optional[str] = None          # 必填（账本模式）
    date_from: Optional[str] = None      # 账本模式忽略
    date_to: Optional[str] = None
    with_invoices: bool = True
    with_summary: bool = True
    with_raw: bool = False
    with_billing_ledger: bool = False    # 新增
```

**校验（`with_billing_ledger=True` 时）：**

- `month` 必须为合法 `YYYY-MM`；
- 强制 `with_invoices=False`、`with_summary=False`、`with_raw=False`（服务端覆盖或 400 提示）；
- `accounts` 非空。

### 5.2 任务状态机（复用 SSE）

| 阶段 | `phase` | 说明 |
|------|---------|------|
| 登录 | `fetching` | 仅 `get_valid_token`（可不调 Cursor API） |
| 解析列表 | `ledger` | 新增：抓取账单页并计算 |
| 完成 | `done` | 该账号完成 |
| 失败 | `error` | |
| 全局汇总 | `summary` | 写 Excel |
| 就绪 | `ready` | 带 `download_token` |

`global_phase` 可增加 `ledger_packaging`。

Worker 伪代码：

```python
if req.with_billing_ledger:
    fetch_targets = ()  # 跳过 API 拉取
    for acc in accounts:
        push(phase="fetching")
        token = manager.get_valid_token(acc)
        push(phase="ledger")
        rows = scrape_billing_list(token, month=req.month)
        summary = compute_ledger(acc, rows, month)
        all_summaries.append(summary)
        push(phase="done")
    write_billing_ledger_xlsx(all_summaries, all_rows, out_dir / "账期净支出.xlsx")
    download_token = register_file(...)
```

### 5.3 模块划分（建议）

```
cam/
  billing_ledger.py          # 新建：解析、汇总、写 Excel
  exporter.py                # 扩展 _STATUS_JS；或抽 _BILLING_LIST_JS 常量
  web_server.py              # RunRequest、worker 分支
  static/index.html          # 按钮、startBillingLedgerRun()
```

**`billing_ledger.py` 职责：**

- `parse_billing_rows(raw_rows) -> list[BillingListRow]`
- `summarize_account_ledger(email, feishu, month, rows) -> BillingLedgerSummary`
- `export_billing_ledger_workbook(path, summaries, detail_rows) -> Path`

**`exporter.py` 扩展：**

- `_BILLING_LIST_JS`：按 **Date / Description / Status / Amount / Invoice** 五列抓取（见附录 A）；
- `fetch_billing_list_rows(page, month) -> list[dict]`：返回含 `date`、`description`、`status`、`amountText`、`url` 的原始列表。

### 5.4 并发与性能

| 项 | 建议 |
|----|------|
| 并发 | 复用 `INVOICE_DOWNLOAD_CONCURRENCY`（仅打开 Billing 列表页，**本任务不进入 Stripe 下载 PDF**） |
| 单账号耗时 | 预期 5–15s（无 Stripe 逐条下载，通常快于 PDF 批量任务） |
| 300 账号 | 与现有账单 PDF 批量同级，可接受；进度条按账号推进 |

---

## 6. 前端交互设计

### 6.1 入口

位置：`.sticky-selection-bar` 内，**「开始拉取」左侧** 新增按钮：

```text
[ 导出账期净支出 (N) ]  [ 开始拉取 (N) ]
```

样式：`btn btn-primary` 或 `btn-secondary`，与 CTA 区分（净支出为次要主流程）。

### 6.2 前置校验

与 `startRun()` 对齐：

- `selectedEmails.size > 0`；
- **`selectedMonth` 必选**（复用账单月份选择器；未选则 toast/alert：「请先选择账单月份」）。

**不依赖**「发票 PDF / Token 汇总」勾选状态。

### 6.3 请求体

```javascript
async startBillingLedgerExport() {
  // 校验 selectedMonth
  await fetch('/api/run', {
    body: JSON.stringify({
      accounts: [...],
      month: this.selectedMonth,
      with_billing_ledger: true,
      with_invoices: false,
      with_summary: false,
      with_raw: false,
    }),
  });
  this.view = 'run';
  this.listenSSE(task_id);
}
```

### 6.4 进度文案映射（`index.html`）

| phase | 展示 |
|-------|------|
| `fetching` | 获取登录态… |
| `ledger` | 解析账单列表… |
| `done` | 完成 |
| `summary` | 生成 Excel… |

`runStatusText()` 增加 `ledger` 计数分支。

### 6.5 下载

任务 `ready` 事件后：

- `download_token` → `GET /api/download/{token}`（单文件 Excel）；
- 按钮文案：「下载账期净支出 Excel」。

无需 ZIP（除非未来同时导出多表；本期单文件即可）。

---

## 7. 测试策略

### 7.1 单元测试（`tests/test_billing_ledger.py`）

| 用例 | 输入 | 期望 |
|------|------|------|
| 解析退款金额 | `Refunded (12.07 USD)` | status=refunded, amount=12.07 |
| 解析支付 | `Paid` + 列 `$20` | paid, 20 |
| 净支出 | paid 20 + refunded amount 15 − status refund 12.07 | net=22.93 |
| 截图样例 | 63.96 + 21.32 − 12.07 | net=73.21 |
| 忽略 open | open 行 | 不计入 |
| 缺金额 | refunded 无数字 | warning，不计入 |

### 7.2 扩展 `test_invoice_paid_filter.py`

保证 `_filter` 或新函数对 **带金额的 refunded 文案** 仍识别为 refunded。

### 7.3 集成 / 手工

- 选 2–3 个测试账号 + 已知有退款的月份；
- 对比 Stripe 页面手算与 Excel 净支出列；
- 确认本任务目录无 `invoices/*.pdf`；与另开的 PDF 拉取任务互不影响。

---

## 8. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| Stripe/Cursor 改 DOM | 解析失败 | 保留日期过滤兜底；明细表暴露原始行；日志 + parse_warnings |
| Refunded 只减 Status 不加 Amount | 净支出偏低 | **强制** Refunded 行 Amount **加**、Status 括号 **减**；单测 `63.96+21.32-12.07` |
| Amount / Status 列顺序变化 | 解析错位 | 优先按表头 `Date/Status/Amount` 定位列索引，td 下标作兜底 |
| 退款显示为负金额 | 重复扣减 | 括号内金额取绝对值计入 refund_total |
| 用户未选月份 | 数据错月 | 前端 + 后端双校验 |
| 与 PDF 任务同时跑 | 浏览器争用 | 共用 Semaphore 限制 Context；两功能代码路径分离 |

---

## 9. 实施顺序建议（高层）

1. **解析层**：扩展 `_STATUS_JS` + Python 金额/汇总（可单测先行）；
2. **导出层**：`billing_ledger.py` + Excel 双 Sheet；
3. **API 层**：`with_billing_ledger` 分支 + SSE phase；
4. **前端**：按钮 + 校验 + 文案；
5. **联调 & 文档**：README / `.env.example` 无需新配置项（复用 invoice 并发即可）。

详细任务拆解见 companion 文档：  
`docs/superpowers/plans/2026-05-19-billing-ledger-export-implementation.md`

---

## 10. 开放问题（评审时可确认）

1. **净支出为负** 是否在汇总表用红色展示？（前端 Excel 样式可选，非必须）
2. 是否需要在汇总表增加 **币种** 列（当前假设 USD）？
3. 多笔同金额支付是否要去重（默认 **不去重**，以列表行为准）？
4. 失败账号：整行标记错误 vs 部分解析成功仍输出明细？（建议：**部分成功也输出明细**，汇总行标 warn）

---

## 附录 A：`_BILLING_LIST_JS` 扩展草案（五列）

```javascript
() => {
  const out = [];
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const amountColRe = /^\d+(?:\.\d+)?\s*USD$/i;
  for (const tr of document.querySelectorAll('tr')) {
    const a = tr.querySelector('a[href*="invoice.stripe.com"]');
    if (!a) continue;
    const tds = [...tr.querySelectorAll('td')].map(td => norm(td.innerText || td.textContent));
    // 默认列序：Date | Description | Status | Amount | Invoice(View)
    const date = tds[0] || '';
    const description = tds[1] || '';
    let status = tds[2] || '';
    let amountText = tds[3] || '';
    // 兜底：Status 含 Paid/Refunded；Amount 形如 63.96 USD
    if (!/paid|refund|open|支付|退款/i.test(status)) {
      for (const t of tds) {
        if (/paid|refund|open|支付|退款/i.test(t)) status = t;
      }
    }
    if (!amountColRe.test(amountText)) {
      for (const t of tds) {
        if (amountColRe.test(t) || /USD/i.test(t) && /\d/.test(t)) {
          if (t !== status && !/refunded\s*\(/i.test(t)) { amountText = t; break; }
        }
      }
    }
    out.push({ url: a.href || '', date, description, status, amountText });
  }
  return out;
}
```

---

## 附录 B：与「发票 PDF 拉取」并列关系

| 维度 | 发票 PDF（现有） | 账期净支出（新增） |
|------|------------------|-------------------|
| 关系 | 并存 | 并存，**不替代** |
| 浏览器深度 | Billing 列表 + 进 Stripe 下载 | 仅 Billing **Invoices** 列表 |
| 输出 | PDF（+ 可选其它勾选物） | 账期净支出 Excel |
| Paid 金额 | — | **Amount 列**（加） |
| Refunded | — | **Amount 列（加）** + **Status 括号（减）** |
| 月份 | selectedMonth | 同左 |
| 用户入口 | 开始拉取 + 勾选 | **新按钮** + 必选月份 |
