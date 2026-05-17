# 账单同步飞书邮箱与套餐字段 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在账号库和 StarRocks ODS 明细中新增必填 `feishu_email` 与套餐数字 `plan_amount`，并保证上传、手动新增、BI 同步和告警链路一致。

**Architecture:** `feishu_email` 作为账号库业务归属字段，在上传/手动新增/保存接口强校验后持久化到 SQLite `accounts`，BI 同步账号快照读取后写入 ODS。`plan_amount` 在 BI 同步拉取账号看板套餐信息时解析，只保留金额数字，随每条 usage 明细写入 ODS；解析失败不阻断同步但写空并记录日志。

**Tech Stack:** Python, FastAPI, SQLite, StarRocks MySQL protocol via `pymysql`, Alpine.js, unittest.

---

## 文件结构

- Modify: `cam/token_store.py`
  - 给 SQLite `accounts` 表新增 `feishu_email` 字段。
  - 保存、查询、搜索账号时返回该字段。
  - 启动时做轻量迁移：缺列则 `ALTER TABLE accounts ADD COLUMN feishu_email TEXT NOT NULL DEFAULT ''`。

- Modify: `cam/web_server.py`
  - `AccountRow` 增加必填 `feishu_email`。
  - `/api/upload` 解析上传文件时读取并校验 `feishu_email`。
  - `/api/accounts/save` 保存账号时校验 `feishu_email` 非空、格式像邮箱。
  - 上传模板/手动新增保存链路不得丢字段。

- Modify: `cam/static/index.html`
  - 上传预览表格增加“飞书邮箱”列。
  - 手动新增表单增加“飞书邮箱（必填）”输入框。
  - 前端上传预览与手动新增时做必填和基本邮箱格式校验。
  - 账号列表展示 `feishu_email`。

- Modify: `cam/models.py`
  - 如 `Account` 模型需要承载 `feishu_email`，新增字段并提供默认空字符串。

- Modify: `cam/bi_sync.py`
  - `SnapshotAccount` 增加 `feishu_email`。
  - 构建账号快照时校验 `feishu_email` 必填；缺失账号记失败，不写 ODS。
  - BI 同步获取套餐数据并解析 `plan_amount`。
  - `_rows_from_usage_csv()` / `_rows_from_usage_events()` 增加 `feishu_email`、`plan_amount`。

- Modify: `cam/fetcher.py` / `cam/api_client.py`
  - 复用已有账号看板/plan 拉取能力，确保 BI 同步能拿到 Current Plan 文案。
  - 若已有 `fetch_one(... what=("plan",))` 可以拿到套餐，优先复用；不要新增浏览器抓取链路，除非 API 无法提供。

- Modify: `cam/starrocks_loader.py`
  - ODS DDL 增加 `feishu_email`、`plan_amount`。
  - ODS INSERT 字段列表和参数映射增加两个字段。
  - `_normalize_ods_row()` 保证 `feishu_email` 非空，`plan_amount` 为可写入 DECIMAL/NULL。

- Modify: `.env.example`
  - 如新增套餐解析开关或超时配置，再补充；默认不新增配置。

- Modify: `tests/*`
  - 增加上传校验、保存接口校验、BI 同步写入字段、套餐解析单测。

---

## 先执行的 SQL 脚本

### StarRocks ODS 新增字段

```sql
ALTER TABLE dataeye_customer.ods_cursor_usage_events_di
ADD COLUMN feishu_email VARCHAR(320) NULL COMMENT '飞书邮箱，来源于账号库上传或手动新增';

ALTER TABLE dataeye_customer.ods_cursor_usage_events_di
ADD COLUMN plan_amount DECIMAL(10,2) NULL COMMENT '套餐金额数字，来源于账号看板 Current Plan，例如 Ultra $20/mo 取 20';
```

说明：

- `feishu_email` 业务必填由上传、手动新增、保存接口、BI 同步前置校验保证。
- StarRocks 字段仍允许 `NULL`，避免历史分区/历史数据/表结构演进带来写入风险。
- 不在运行任务中自动执行 `ALTER TABLE`。

### SQLite 账号库新增字段

```sql
ALTER TABLE accounts
ADD COLUMN feishu_email TEXT NOT NULL DEFAULT '';
```

实现时必须做存在性判断，避免重复执行时报 `duplicate column name`。

---

## Chunk 1: 账号库字段与迁移

### Task 1: SQLite `accounts.feishu_email` 迁移

**Files:**
- Modify: `cam/token_store.py`
- Test: `tests/test_account_feishu_email_store.py`

- [ ] **Step 1: 写失败测试：初始化老库后自动补列**

创建临时 SQLite，先建一个不含 `feishu_email` 的 `accounts` 表，再初始化 `TokenStore`。

预期：

```python
cols = list_account_columns(conn)
assert "feishu_email" in cols
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python3 -m unittest tests.test_account_feishu_email_store
```

Expected: FAIL，提示缺少 `feishu_email`。

- [ ] **Step 3: 实现迁移**

在 `TokenStore._init_schema()` 后增加 `_ensure_account_columns()`：

```python
def _ensure_account_columns(self, conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "feishu_email" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN feishu_email TEXT NOT NULL DEFAULT ''")
```

- [ ] **Step 4: 扩展 CRUD**

`upsert_account()` 增加参数：

```python
feishu_email: str
```

INSERT/UPDATE 增加字段：

```sql
feishu_email = excluded.feishu_email
```

- [ ] **Step 5: 跑测试**

Run:

```bash
python3 -m unittest tests.test_account_feishu_email_store
```

Expected: PASS。

---

## Chunk 2: 上传和手动新增校验

### Task 2: 后端上传解析校验 `feishu_email`

**Files:**
- Modify: `cam/web_server.py`
- Test: `tests/test_web_server_csv_normalization.py` 或新增 `tests/test_account_upload_feishu_email.py`

- [ ] **Step 1: 写失败测试：CSV 缺少 `feishu_email` 报错**

输入：

```csv
email,imap_password
a@example.com,pw
```

预期 `/api/upload` 返回错误信息包含：

```text
CSV 缺少必需列: feishu_email
```

- [ ] **Step 2: 写失败测试：行内 `feishu_email` 为空报错**

输入：

```csv
email,imap_password,feishu_email
a@example.com,pw,
```

预期错误：

```text
第 2 行 feishu_email 不能为空
```

- [ ] **Step 3: 实现上传解析**

在上传解析字段中读取：

```python
feishu_email = (row.get("feishu_email") or row.get("飞书邮箱") or "").strip().lower()
```

校验：

```python
if not feishu_email:
    raise ValueError(f"第 {line_no} 行 feishu_email 不能为空")
if "@" not in feishu_email:
    raise ValueError(f"第 {line_no} 行 feishu_email 格式不正确")
```

- [ ] **Step 4: `AccountRow` 增加必填字段**

```python
class AccountRow(BaseModel):
    email: str
    imap_password: str
    feishu_email: str
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
```

- [ ] **Step 5: 保存接口校验**

在 `/api/accounts/save` 中校验：

```python
feishu_email = acc.feishu_email.strip().lower()
if not feishu_email:
    raise HTTPException(status_code=400, detail=f"{acc.email} 缺少飞书邮箱")
```

- [ ] **Step 6: 跑测试**

Run:

```bash
python3 -m unittest tests.test_account_upload_feishu_email
```

Expected: PASS。

### Task 3: 前端新增字段与校验

**Files:**
- Modify: `cam/static/index.html`

- [ ] **Step 1: 上传预览表格增加列**

表头增加：

```html
<th>飞书邮箱</th>
```

行内展示：

```html
<td x-text="account.feishu_email"></td>
```

- [ ] **Step 2: 手动新增表单增加输入框**

新增状态：

```javascript
manualFeishuEmail: '',
```

表单增加：

```html
<input x-model="manualFeishuEmail" placeholder="飞书邮箱（必填）" />
```

- [ ] **Step 3: 前端手动新增校验**

在 `addManualAccount()` 中：

```javascript
const feishuEmail = (this.manualFeishuEmail || '').trim().toLowerCase();
if (!feishuEmail) {
  this.uploadError = '飞书邮箱不能为空';
  return;
}
if (!feishuEmail.includes('@')) {
  this.uploadError = '飞书邮箱格式不正确';
  return;
}
```

- [ ] **Step 4: 保存 payload 增加字段**

账号对象统一包含：

```javascript
{
  email,
  imap_password,
  imap_host,
  imap_port,
  feishu_email: feishuEmail,
}
```

- [ ] **Step 5: 手工验证**

浏览器验证：

- 缺飞书邮箱不能添加到预览。
- 上传缺列报错。
- 上传空值报错。
- 保存后账号列表能展示飞书邮箱。

---

## Chunk 3: 套餐解析

### Task 4: `plan_amount` 解析函数

**Files:**
- Modify: `cam/bi_sync.py`
- Test: `tests/test_bi_sync_plan_amount.py`

- [ ] **Step 1: 写失败测试**

测试用例：

```python
assert _extract_plan_amount("Ultra $20/mo") == Decimal("20")
assert _extract_plan_amount("$60/mo") == Decimal("60")
assert _extract_plan_amount("Ultra 20") == Decimal("20")
assert _extract_plan_amount("Free") is None
assert _extract_plan_amount("") is None
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python3 -m unittest tests.test_bi_sync_plan_amount
```

Expected: FAIL，函数不存在。

- [ ] **Step 3: 实现解析函数**

```python
from decimal import Decimal, InvalidOperation
import re

def _extract_plan_amount(value: Any) -> Optional[Decimal]:
    text = str(value or "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None
```

- [ ] **Step 4: 跑测试**

Run:

```bash
python3 -m unittest tests.test_bi_sync_plan_amount
```

Expected: PASS。

### Task 5: BI 同步获取套餐

**Files:**
- Modify: `cam/bi_sync.py`
- Possibly Modify: `cam/fetcher.py`, `cam/api_client.py`
- Test: `tests/test_bi_sync_concurrency.py`

- [ ] **Step 1: 确认现有 `fetcher.fetch_one()` 能取 plan**

优先使用：

```python
fetcher.fetch_one(acc, what=("usage_events", "plan"), ...)
```

不要先引入浏览器抓页面。

- [ ] **Step 2: 从 `snap.plan` 中提取套餐文案**

候选字段：

```python
("currentPlan", "current_plan", "plan", "name", "planName", "subscription")
```

如字段是 dict，继续取：

```python
("name", "planName", "displayName")
```

- [ ] **Step 3: 解析金额**

```python
plan_amount = _extract_plan_amount(plan_text)
```

- [ ] **Step 4: 将 `plan_amount` 放入 `AccountFetchResult`**

扩展 dataclass：

```python
class AccountFetchResult:
    ...
    plan_amount: Optional[Decimal]
```

- [ ] **Step 5: 更新单测**

`fake_fetch_one()` 返回：

```python
SimpleNamespace(errors={}, usage_csv_text="csv", usage_events=[], plan={"name": "Ultra $20/mo"})
```

断言 loader 收到的 rows 包含：

```python
row["plan_amount"] == Decimal("20")
```

---

## Chunk 4: ODS 字段写入

### Task 6: StarRocks ODS DDL 与 INSERT

**Files:**
- Modify: `cam/starrocks_loader.py`
- Test: `tests/test_starrocks_loader_ods_fields.py`

- [ ] **Step 1: 写测试：INSERT SQL 包含新字段**

用 fake cursor 捕获 `executemany()` SQL，断言包含：

```text
feishu_email
plan_amount
```

- [ ] **Step 2: 更新 ODS DDL**

在 ODS DDL 中增加：

```sql
feishu_email VARCHAR(320) NULL,
plan_amount  DECIMAL(10,2) NULL,
```

- [ ] **Step 3: 更新 ODS INSERT**

字段列表增加：

```sql
feishu_email, plan_amount
```

参数增加：

```sql
%(feishu_email)s, %(plan_amount)s
```

- [ ] **Step 4: 更新 `_normalize_ods_row()`**

```python
row["feishu_email"] = str(row.get("feishu_email") or "").strip().lower()
if not row["feishu_email"]:
    raise ValueError("feishu_email 不能为空")
row["plan_amount"] = _fit_decimal(row.get("plan_amount"), precision=10, scale=2)
```

- [ ] **Step 5: 跑测试**

Run:

```bash
python3 -m unittest tests.test_starrocks_loader_ods_fields
```

Expected: PASS。

### Task 7: BI rows 增加字段

**Files:**
- Modify: `cam/bi_sync.py`
- Test: `tests/test_bi_sync_plan_amount.py`

- [ ] **Step 1: 扩展 `SnapshotAccount`**

```python
@dataclass(frozen=True)
class SnapshotAccount:
    account: Account
    source: str
    is_new: bool
    feishu_email: str
```

- [ ] **Step 2: 构造快照时校验飞书邮箱**

```python
feishu_email = str(row.get("feishu_email") or "").strip().lower()
if not feishu_email:
    # 建议不要进入 fetch 阶段，直接记账号失败
```

实现上可以在 `_snapshot_accounts()` 里先保留字段，具体失败在 fetch result 处理处记录，避免整个任务因为一个历史账号缺字段而中断。

- [ ] **Step 3: rows 构造增加字段**

`_rows_from_usage_csv()` 和 `_rows_from_usage_events()` 增加参数：

```python
feishu_email: str
plan_amount: Optional[Decimal]
```

每行增加：

```python
"feishu_email": feishu_email,
"plan_amount": plan_amount,
```

- [ ] **Step 4: 缺飞书邮箱账号失败**

错误码：

```text
E_ACCOUNT_METADATA: feishu_email is required
```

账号日志应记录失败，不写 ODS。

- [ ] **Step 5: 跑测试**

Run:

```bash
python3 -m unittest tests.test_bi_sync_plan_amount tests.test_bi_sync_concurrency
```

Expected: PASS。

---

## Chunk 5: API、UI、模板一致性

### Task 8: 账号列表与搜索返回飞书邮箱

**Files:**
- Modify: `cam/token_store.py`
- Modify: `cam/web_server.py`
- Modify: `cam/static/index.html`

- [ ] **Step 1: 后端返回字段**

`/api/accounts`、搜索账号接口返回：

```json
{
  "email": "...",
  "feishu_email": "..."
}
```

- [ ] **Step 2: 前端账号列表增加列**

列名：

```text
飞书邮箱
```

- [ ] **Step 3: 搜索下拉不需要展示飞书邮箱**

账单调度补拉下拉仍显示 Cursor 账号邮箱即可，不扩大本次范围。

### Task 9: 上传模板更新

**Files:**
- Modify: `cam/static/index.html`
- Test: manual browser check

- [ ] **Step 1: 下载模板增加列**

模板列：

```csv
email,imap_password,imap_host,imap_port,feishu_email
```

- [ ] **Step 2: 前端帮助文案更新**

说明：

```text
feishu_email 为必填，用于账单同步底表归属字段。
```

---

## Chunk 6: 文档与上线验证

### Task 10: 文档更新

**Files:**
- Modify: `README.md`
- Modify: `.env.example` only if new config is introduced
- Optional Modify: `docs/starrocks-daily-sync-tech-design.md`

- [ ] **Step 1: README 增加上传模板说明**

补充 `feishu_email` 必填。

- [ ] **Step 2: README 增加 StarRocks ALTER SQL**

放置本计划中的 SQL。

### Task 11: 全量验证

- [ ] **Step 1: 单元测试**

Run:

```bash
python3 -m unittest \
  tests.test_account_feishu_email_store \
  tests.test_account_upload_feishu_email \
  tests.test_bi_sync_plan_amount \
  tests.test_bi_sync_concurrency \
  tests.test_config_concurrency
```

Expected: PASS。

- [ ] **Step 2: 编译检查**

Run:

```bash
python3 -m py_compile \
  cam/token_store.py \
  cam/web_server.py \
  cam/bi_sync.py \
  cam/starrocks_loader.py
```

Expected: exit code 0。

- [ ] **Step 3: 手工 UI 验证**

检查：

- 上传缺 `feishu_email` 阻止。
- 上传 `feishu_email` 空值阻止。
- 手动新增缺 `feishu_email` 阻止。
- 保存后账号库显示飞书邮箱。
- 账单调度运行成功。

- [ ] **Step 4: StarRocks 数据验证**

执行：

```sql
SELECT
  dt,
  account_email,
  feishu_email,
  plan_amount,
  COUNT(*) AS rows
FROM dataeye_customer.ods_cursor_usage_events_di
WHERE dt = '2026-05-11'
GROUP BY dt, account_email, feishu_email, plan_amount
ORDER BY account_email;
```

预期：

- `feishu_email` 非空。
- `plan_amount` 为数字或 `NULL`。
- 行数与同步日志 `ods_rows` 一致。

---

## 风险与处理

- **历史账号缺 `feishu_email`**：同步该账号失败并提示 `E_ACCOUNT_METADATA`，要求补齐账号库。
- **StarRocks ALTER TABLE 慢**：DDL 单独执行，不在同步任务中执行。
- **套餐字段解析不稳定**：优先从 API/结构化字段取值；解析不到写 `NULL` 并记录 warning。
- **SQLite 老库重复迁移**：用 `PRAGMA table_info(accounts)` 判断列是否存在。
- **上传模板旧格式**：缺列直接报错，避免新数据缺业务归属字段。

---

## 执行顺序建议

1. 先执行 StarRocks ALTER SQL。
2. 实现 SQLite 迁移和后端保存校验。
3. 实现前端上传/手动新增必填校验。
4. 实现套餐解析和 ODS 写入。
5. 跑单测和手工上传验证。
6. 再跑一次账单调度，检查 ODS 新字段。

Plan complete and saved to `docs/superpowers/plans/2026-05-14-billing-sync-feishu-plan-fields.md`. Ready to execute?
