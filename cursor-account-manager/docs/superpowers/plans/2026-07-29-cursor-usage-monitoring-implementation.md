# Cursor Usage Monitoring Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不开发新页面、只新增 `cursor_usage_snapshot` 一张业务表的前提下，实现 Cursor 多账号套餐档位、滚动账期用量、periodic/pre-reset 快照、账期结算和低用量等级。

**Architecture:** 采集层复用 `TokenManager` 与 `CursorClient`，通过独立的解析、存储和编排模块写入 aicoding MySQL。用量调度由独立 `UsageSchedulerCoordinator` 线程运行，避免现有 BI/Spending/Ledger 同步任务阻塞 reset 前采集；账号级跨进程锁、数据库事务和滑动窗口认证熔断共同保证安全性。

**Tech Stack:** Python 3.10+、标准库 `unittest`、requests/patchright、PyMySQL + DBUtils、SQLite 运行日志、MySQL aicoding、Click CLI。

**Design Spec:** `docs/superpowers/specs/2026-07-29-cursor-usage-monitoring-design.md`

**实施状态（2026-07-29，本地开发）：**
- [x] Task 0 以及 Task 2–10 的代码、单元测试和本地开发收尾已完成。
- [ ] Task 1 仍缺少从真实 Cursor API 脱敏取得的套餐及 reset 前后契约样例；不得用合成数据替代这一验收证据。
- [ ] Task 11 的最终验收状态未在本次文档收尾中变更；README 仅按当前工作区 CLI 实现记录命令，并明确未合入目标分支时不得执行。
- [ ] Task 12 仍未完成：真实 MySQL 5.7/8.0、Windows 服务锁与调度、测试账号/预生产窗口、生产分阶段开启和回滚均未执行。

---

## 文件结构与职责

### 新增文件

| 文件 | 单一职责 |
|---|---|
| `cam/usage_snapshot_models.py` | 快照、采集结果、账号映射、浪费等级等领域对象 |
| `cam/usage_snapshot_parser.py` | API payload 解析、套餐规范化、时间戳和百分比校验、脱敏 |
| `cam/usage_snapshot_store.py` | MySQL DDL/结构预检、快照幂等写入、账期结算、分析查询 |
| `cam/usage_snapshot_locks.py` | 基于 email 哈希的跨进程账号文件锁 |
| `cam/usage_auth_breaker.py` | 进程级滑动窗口认证熔断器 |
| `cam/usage_snapshot_collector.py` | Token/API 调用、typed error、API 优先与页面低频兜底 |
| `cam/usage_snapshot_refresh.py` | AccountResolver、periodic/pre-reset 批次、退避和运行日志 |
| `cam/usage_scheduler.py` | 独立计时线程、两个单 worker 执行器和生命周期 |
| `cam/usage_waste.py` | L0/L1/L2/L3/UNKNOWN 纯函数分析 |
| `tests/fixtures/cursor_usage/*.json` | 脱敏的真实 API 契约样例 |
| `tests/test_usage_snapshot_models.py` | 领域对象与状态约束 |
| `tests/test_usage_snapshot_parser.py` | 原始 payload 解析 |
| `tests/test_usage_snapshot_store.py` | 存储 SQL、幂等和结算单元测试 |
| `tests/test_usage_snapshot_store_mysql.py` | 真实 MySQL 兼容与事务测试 |
| `tests/test_usage_snapshot_locks.py` | 进程/线程锁测试 |
| `tests/test_usage_auth_breaker.py` | 滑动窗口和 half-open 测试 |
| `tests/test_usage_snapshot_collector.py` | Token/API/fallback/typed error |
| `tests/test_usage_snapshot_refresh.py` | periodic/pre-reset 编排 |
| `tests/test_usage_scheduler.py` | 独立计时与优先级 |
| `tests/test_usage_waste.py` | 连续档位段和低用量等级 |
| `tests/test_usage_cli.py` | CLI 参数和 service 调用 |

### 修改文件

| 文件 | 修改范围 |
|---|---|
| `cam/models.py` | 增加认证熔断 typed exception，不承载快照业务模型 |
| `cam/config.py` | 增加用量配置、校验函数，移除数据库密码默认值 |
| `.env.example` | 增加无密钥的用量配置 |
| `cam/token_manager.py` | 向后兼容增加 `auth_policy`，在 refresh/重登前检查 |
| `cam/sync_log_store.py` | 增加账号+slot 尝试状态查询和必要索引 |
| `cam/scheduler.py` | 启停独立 UsageSchedulerCoordinator，不在旧 while 中执行用量长任务 |
| `cam/web_server.py` | startup/shutdown 绑定 coordinator 生命周期，不新增页面/API |
| `cam/cli.py` | 增加手工采集、dry-run 和修复命令 |
| `README.md` | 配置、命令、数据口径和运维说明 |

### 明确不修改

```text
cam/static/index.html
Billing Ledger 退款公式
发票 PDF 下载流程
StarRocks ODS 表语义
现有 Web 页面交互
```

---

# Chunk 1: 数据契约与持久化

## Task 0: 环境与工作区安全预检（已完成）

**Files:**
- No file changes

- [ ] **Step 1: 记录当前工作区，不覆盖用户未提交修改**

Run:

```bash
git status --short --branch
git diff -- cursor-account-manager/cam/config.py cursor-account-manager/.env.example cursor-account-manager/cam/starrocks_loader.py cursor-account-manager/windows-setup.ps1
```

Expected: 保存基线；后续编辑 `cam/config.py`、`.env.example` 前重新读取最新内容并保留用户已有修改。

- [ ] **Step 2: 准备 Unix/macOS Python 环境**

从 Git 仓库根目录进入项目；后续所有命令默认工作目录均为 `cursor-account-manager`。若项目内 `.venv/bin/python` 不存在：

```bash
cd cursor-account-manager
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

验证：

```bash
.venv/bin/python --version
```

Expected: Python 3.10+。

- [ ] **Step 3: 记录 Windows 对应解释器**

Windows 所有命令将 `.venv/bin/python` 替换为：

```powershell
.\.venv\Scripts\python.exe
```

- [ ] **Step 4: 明确版本控制边界**

每个 Task 只形成可审查检查点。除非用户在实施会话中明确要求，否则不得 `git add` 或 `git commit`；尤其不能把当前工作区已有修改一并提交。

---

## Task 1: 固化测试基线和真实 API fixture（未完成：真实契约样例）

**Files:**
- Create: `tests/fixtures/cursor_usage/pro.json`
- Create: `tests/fixtures/cursor_usage/pro_plus.json`
- Create: `tests/fixtures/cursor_usage/free.json`
- Create: `tests/fixtures/cursor_usage/reset_before.json`
- Create: `tests/fixtures/cursor_usage/reset_after.json`
- Create: `tests/fixtures/cursor_usage/README.md`
- Modify: `.gitignore`（仅当 fixture 路径被误忽略）

- [ ] **Step 1: 记录实现前全仓测试基线**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected: 记录测试总数、通过数及既存失败。当前探索基线约为 117 个用例、6 个既存失败/错误，但必须以实施当天实际输出为准。

- [ ] **Step 2: 用 3 个测试账号抓取脱敏 payload**

使用现有 `CursorClient` 临时诊断命令或受控 Python REPL 获取：

```python
{
    "usage": client.get_current_period_usage(),
    "plan": client.get_plan_info(),
    "stripe": client.get_stripe_info(),
}
```

要求：

- Pro、Pro+、Free 至少各一份；
- 现网存在 Ultra 时，`ultra.json` 为必需；没有可用 Ultra 账号时在 README 明确记录，并用无业务数据的合成 fixture 只验证名称规范化；
- 同一测试账号 reset 前后各抓一份，确认 `billingCycleStart/End` 的真实切换行为；
- 删除 email、user id、payment id、token、Cookie；
- 保留真实字段名、值类型、时间戳单位和百分比量纲；
- 不提交任何凭据。

- [ ] **Step 3: 编写 fixture 说明**

`tests/fixtures/cursor_usage/README.md` 必须记录：

```markdown
- captured_at
- Cursor 页面显示档位
- 页面显示账期
- 页面显示使用率
- billingCycleStart/End 的单位
- totalPercentUsed 是 0~100 还是 0~1
- 已脱敏字段列表
```

- [ ] **Step 4: 人工核对 fixture**

Expected:

- API 账期与页面一致；
- 套餐名称映射有证据；
- 百分比单位不靠猜测；
- JSON 不包含 `token`、`cookie`、`authorization`、`paymentId`。

- [ ] **Step 5: 检查点**

```bash
git diff --check
git status --short
```

Expected: 仅出现本 Task 预期 fixture/说明改动；不提交。

---

## Task 2: 定义领域模型和解析器（已完成）

**Files:**
- Create: `cam/usage_snapshot_models.py`
- Create: `cam/usage_snapshot_parser.py`
- Create: `tests/test_usage_snapshot_models.py`
- Create: `tests/test_usage_snapshot_parser.py`

- [ ] **Step 1: 编写领域模型失败测试**

核心测试：

```python
def test_snapshot_rejects_invalid_cycle(self):
    with self.assertRaises(ValueError):
        UsageSnapshot(
            email="a@example.com",
            plan_tier="pro",
            plan_tier_raw="Pro",
            plan_status="active",
            plan_source="api",
            billing_cycle_start=UTC_END,
            billing_cycle_end=UTC_START,
            total_used_pct=Decimal("10.00"),
            snapshot_type=SnapshotType.PERIODIC,
            snapshot_slot=UTC_START,
            collected_at=UTC_START,
            source_endpoint="GetCurrentPeriodUsage",
            parser_version="v1",
            raw_payload={},
        )

def test_zero_percent_is_valid(self):
    snapshot = make_snapshot(total_used_pct=Decimal("0.00"))
    self.assertEqual(snapshot.total_used_pct, Decimal("0.00"))

def test_unknown_plan_is_valid_but_explicit(self):
    snapshot = make_snapshot(plan_tier="unknown")
    self.assertEqual(snapshot.plan_tier, "unknown")
```

- [ ] **Step 2: 运行并确认失败**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_models.py' -v
```

Expected: FAIL，模块尚不存在。

- [ ] **Step 3: 实现领域对象**

最小接口：

```python
class SnapshotType(str, Enum):
    PERIODIC = "periodic"
    PRE_RESET = "pre_reset"

@dataclass(frozen=True)
class UsageSnapshot:
    email: str
    plan_tier: str
    plan_tier_raw: str
    plan_status: str
    plan_source: str
    billing_cycle_start: datetime
    billing_cycle_end: datetime
    total_used_pct: Decimal
    snapshot_type: SnapshotType
    snapshot_slot: datetime
    collected_at: datetime
    source_endpoint: str
    parser_version: str
    raw_payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.email:
            raise ValueError("email is required")
        if self.billing_cycle_end <= self.billing_cycle_start:
            raise ValueError("billing_cycle_end must be after start")
        if not Decimal("0") <= self.total_used_pct <= Decimal("100"):
            raise ValueError("total_used_pct out of range")
```

同时定义：

```python
CollectionStatus
CollectionResult
AccountMappingResult
AuthOutcome
FinalSource
WasteLevel
```

- [ ] **Step 4: 编写解析器失败测试**

覆盖：

```python
def test_parse_real_pro_plus_fixture(self): ...
def test_parse_millisecond_cycle_timestamp(self): ...
def test_does_not_guess_percent_scale(self): ...
def test_missing_cycle_is_contract_error(self): ...
def test_normalize_plan_tier_variants(self): ...
def test_amount_without_explicit_plan_name_stays_unknown(self): ...
def test_sanitize_raw_payload_removes_credentials_recursively(self): ...
```

- [ ] **Step 4A: 运行解析测试并确认失败**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_parser.py' -v
```

Expected: FAIL，解析器尚未实现。

- [ ] **Step 5: 实现解析器**

固定公开接口：

```python
PARSER_VERSION = "usage-v1"

def parse_usage_snapshot(
    *,
    email: str,
    usage_payload: Mapping[str, Any],
    plan_payload: Mapping[str, Any],
    stripe_payload: Mapping[str, Any],
    snapshot_type: SnapshotType,
    snapshot_slot: datetime,
    collected_at: datetime,
) -> UsageSnapshot:
    ...

def normalize_plan_tier(raw: object) -> tuple[str, str]:
    ...

def parse_api_datetime(raw: object, *, unit: Literal["ms", "s"]) -> datetime:
    ...

def sanitize_payload(value: Any) -> Any:
    ...
```

规则：

- 时间统一 UTC aware datetime，入库层再去 tz；
- 百分比单位由 fixture 固定，不根据数值大小猜测；
- plan 优先 `GetPlanInfo` 明确名称，其次 Stripe；
- 页面 fallback 不在 parser 内调用；
- 原始 payload 合并前必须递归脱敏。

- [ ] **Step 6: 运行解析和模型测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_*.py' -v
```

Expected: PASS。

- [ ] **Step 7: 检查点**

```bash
git diff --check
git status --short
```

---

## Task 3: 增加配置并移除密码默认值（已完成）

**Files:**
- Modify: `cam/config.py`
- Modify: `.env.example`
- Modify: `tests/test_config_concurrency.py`
- Create: `tests/test_usage_config.py`

- [ ] **Step 1: 编写配置失败测试**

```python
def test_usage_defaults(self):
    settings = load_with_empty_usage_env()
    self.assertEqual(settings.usage_periodic_interval_hours, 24)
    self.assertEqual(settings.usage_pre_reset_target_offset_min, 180)
    self.assertEqual(settings.usage_auth_breaker_window_size, 50)

def test_window_order_is_validated(self):
    with self.assertRaisesRegex(ValueError, "window_start"):
        load_with_env(
            USAGE_PRE_RESET_WINDOW_START_MIN="60",
            USAGE_PRE_RESET_TARGET_OFFSET_MIN="180",
        )

def test_ledger_password_has_no_source_default(self):
    with patch.dict(os.environ, {"LEDGER_DB_PASSWORD": ""}, clear=False):
        self.assertEqual(load_settings().ledger_db_password, "")

def test_explicit_invalid_usage_integer_fails(self):
    with self.assertRaisesRegex(ValueError, "USAGE_SNAPSHOT_CONCURRENCY"):
        load_with_env(USAGE_SNAPSHOT_CONCURRENCY="abc")
```

- [ ] **Step 2: 运行并确认失败**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_config.py' -v
```

Expected: FAIL，新字段和校验尚不存在。

- [ ] **Step 3: 扩展 Settings**

完整新增字段以技术设计 §14 为准，至少包含：

```python
usage_snapshot_enable: bool
usage_periodic_interval_hours: int
usage_snapshot_concurrency: int
usage_bootstrap_stale_hours: int
usage_periodic_retry_minutes: int
usage_periodic_max_attempts_per_slot: int
usage_pre_reset_scan_interval_min: int
usage_pre_reset_window_start_min: int
usage_pre_reset_target_offset_min: int
usage_pre_reset_window_end_min: int
usage_low_threshold_pct: Decimal
usage_cycle_continuity_tolerance_hours: int
usage_periodic_lock_file: str
usage_pre_reset_lock_file: str
usage_account_lock_dir: Path
usage_account_lock_timeout_sec: int
usage_auth_breaker_min_samples: int
usage_auth_breaker_failure_ratio: float
usage_auth_breaker_cooldown_min: int
usage_auth_breaker_window_size: int
usage_auth_breaker_window_min: int
```

增加纯函数：

```python
def validate_settings(settings: Settings) -> None:
    if not (
        settings.usage_pre_reset_window_start_min
        > settings.usage_pre_reset_target_offset_min
        > settings.usage_pre_reset_window_end_min
        >= 0
    ):
        raise ValueError("invalid usage pre-reset window_start/target/window_end")
```

使用表驱动测试覆盖技术设计 §14 全部约束：

```text
window_start > target > window_end >= 0
scan/periodic/retry/window/cooldown > 0
bootstrap_stale >= periodic_interval
max_attempts/concurrency/min_samples/window_size >= 1
window_size >= min_samples
0 <= low_threshold <= 100
0 < failure_ratio <= 1
account_lock_timeout >= 0
```

现有 `_env_int` 会对非法字符串回退默认值；新增 Usage 配置必须使用严格解析 helper，只有“环境变量未设置或空字符串”才能使用默认值，显式非法值必须启动失败。

- [ ] **Step 4: 移除真实数据库密码默认值**

```python
ledger_db_password=os.environ.get("LEDGER_DB_PASSWORD", "").strip()
```

只在实际启用 Ledger 或 Usage Snapshot 数据库任务时做启动预检；不能让纯解析测试因无密码失败。

- [ ] **Step 5: 更新 `.env.example`**

加入技术设计 §14 全部变量，`LEDGER_DB_PASSWORD=` 保持空值。

- [ ] **Step 6: 运行配置测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*config*.py' -v
```

Expected: PASS。

- [ ] **Step 7: 检查点**

```bash
git diff --check
git status --short
```

---

## Task 4: 实现 MySQL 快照存储和账期结算（已完成）

**Files:**
- Create: `cam/usage_snapshot_store.py`
- Create: `tests/test_usage_snapshot_store.py`
- Create: `tests/test_usage_snapshot_store_mysql.py`

- [ ] **Step 1: 编写 SQL/映射单元失败测试**

使用 fake connection/cursor 覆盖：

```python
def test_ensure_table_uses_if_not_exists_and_innodb(self): ...
def test_schema_validation_rejects_wrong_collation(self): ...
def test_monitor_contract_requires_four_account_columns(self): ...
def test_monitor_contract_requires_unique_email_index(self): ...
def test_monitor_contract_rejects_normalized_email_duplicates(self): ...
def test_monitor_contract_checks_ledger_email_collation(self): ...
def test_periodic_same_slot_is_idempotent(self): ...
def test_periodic_different_slot_inserts_new_row(self): ...
def test_pre_reset_same_cycle_has_one_row_and_newer_replaces(self): ...
def test_pre_reset_older_value_does_not_overwrite(self): ...
def test_same_start_changed_end_is_same_cycle(self): ...
def test_new_cycle_finalizes_old_before_insert(self): ...
def test_stale_cycle_write_is_rejected(self): ...
def test_finalize_is_idempotent_when_repeated(self): ...
def test_finalize_without_candidate_returns_missing_cycle_final(self): ...
def test_finalize_prefers_pre_reset_and_marks_final_source(self): ...
def test_finalize_falls_back_to_periodic_and_marks_final_source(self): ...
```

- [ ] **Step 2: 运行并确认失败**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_store.py' -v
```

Expected: FAIL，store 尚不存在。

- [ ] **Step 3: 实现连接池和结构预检**

公开接口：

```python
class UsageSnapshotStore:
    def ensure_schema(self) -> None: ...
    def validate_schema(self) -> None: ...
    def validate_monitor_contract(self) -> None: ...
    def list_monitor_accounts(self) -> list[dict]: ...
    def get_latest_cycle(self, email: str) -> dict | None: ...
    def get_latest_snapshot(self, email: str) -> dict | None: ...
    def has_periodic_slot(self, email: str, slot: datetime) -> bool: ...
    def list_pre_reset_due(self, now: datetime) -> list[dict]: ...
    def reconcile_and_write(self, snapshot: UsageSnapshot, *, repair: bool = False) -> WriteResult: ...
    def repair_finalize_cycle(
        self, *, email: str, cycle_start: datetime, actor: str, reason: str
    ) -> FinalizeResult: ...
    def list_final_cycles(self, email: str) -> list[dict]: ...
```

`ensure_schema()`：

- 执行技术设计 §8.2 DDL；
- 查询 `information_schema`；
- 校验 InnoDB、charset/collation、字段、索引、MySQL 8 CHECK；
- 不符时抛出 `SchemaMismatchError`，不静默 ALTER。

`validate_monitor_contract()` 必须验证：

- `cursor_accounts.id/email/applicant/department` 四列存在；
- email 原始唯一索引存在；
- `lower(trim(email))` 无重复；
- `cursor_accounts.email`、Ledger email 和 snapshot email 的 charset/collation 一致；
- `cursor_billing_ledger_summary` 关键列存在；
- 任一不符时启动失败，不任取重复账号。

- [ ] **Step 4: 实现显式 pre-reset 幂等事务**

伪代码必须落实为单事务：

```python
stored = select_unique_key_for_update(snapshot.key)
if stored and snapshot.collected_at <= stored.collected_at:
    return WriteResult.IDEMPOTENT
if stored:
    update_all_collection_fields(snapshot)  # 不修改 final 三字段
else:
    insert_snapshot(snapshot)
```

禁止使用依赖 MySQL 左到右求值的多字段条件 UPSERT。

- [ ] **Step 5: 实现 reconcile/finalization**

事务顺序：

```python
latest_cycle = select_latest_cycle_for_update(email)
if latest_cycle is None:
    insert(snapshot)
elif snapshot.start == latest_cycle.start:
    upsert_same_cycle(snapshot)
elif snapshot.start > latest_cycle.start:
    finalize_cycle(latest_cycle.start)
    insert(snapshot)
else:
    raise StaleCycleWriteError(...)
```

`finalize_cycle`：

- 最新成功快照的 end 为 authoritative end；
- 只选择 `collected_at <= authoritative end` 的候选；
- 优先合法 pre-reset，否则 periodic fallback；
- 同步最终行规范化 end；
- 无候选时返回 `missing_cycle_final`；
- 同一周期恰好一条 final。

`repair_finalize_cycle()` 必须：

- 复用同一 finalization 事务；
- 强制要求 `actor/reason`；
- 将 repair 操作写入 sync stage/account log；
- 重复执行幂等；
- 不允许修改其他 email/cycle；
- 不能绕过 authoritative end 的候选合法性。

- [ ] **Step 6: 增加真实 MySQL 集成测试**

本地开发可在环境缺失时 SKIP；发布门禁设置 `CAM_REQUIRE_MYSQL_TESTS=1`，此时缺少数据库环境必须 FAIL，不能永久跳过：

```text
CAM_TEST_MYSQL_HOST
CAM_TEST_MYSQL_PORT
CAM_TEST_MYSQL_USER
CAM_TEST_MYSQL_PASSWORD
CAM_TEST_MYSQL_DATABASE
CAM_REQUIRE_MYSQL_TESTS
```

覆盖：

- MySQL 5.7/8.x 兼容；
- 两连接 finalization；
- finalization 与迟到写竞争；
- rollback/deadlock retry；
- `DATETIME(3)` 精度；
- end 延长/缩短。

必须分别对 MySQL 5.7 和 8.x 运行，使用两个独立目标：

```bash
CAM_REQUIRE_MYSQL_TESTS=1 CAM_TEST_MYSQL_HOST=<mysql57> \
  .venv/bin/python -m unittest tests/test_usage_snapshot_store_mysql.py -v

CAM_REQUIRE_MYSQL_TESTS=1 CAM_TEST_MYSQL_HOST=<mysql80> \
  .venv/bin/python -m unittest tests/test_usage_snapshot_store_mysql.py -v
```

Expected: 两套环境均 PASS；不能用一套结果代替版本矩阵。

- [ ] **Step 7: 运行存储测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_store*.py' -v
```

Expected: 单元测试 PASS；无集成数据库时 MySQL 用例明确 SKIPPED。

- [ ] **Step 8: 检查点**

```bash
git diff --check
git status --short
```

---

# Chunk 2: 账号解析、重试与认证安全

## Task 5: 扩展运行日志并实现账号全集解析（已完成）

**Files:**
- Modify: `cam/sync_log_store.py`
- Create: `tests/test_usage_retry_log.py`
- Create: `tests/test_usage_account_resolver.py`
- Modify: `cam/usage_snapshot_refresh.py`（首次创建）

- [ ] **Step 1: 编写日志查询失败测试**

```python
def test_attempt_state_counts_account_failures_for_exact_slot(self): ...
def test_success_marks_slot_complete(self): ...
def test_failed_run_does_not_mark_account_success(self): ...
def test_different_slot_does_not_affect_retry(self): ...
def test_restart_uses_persisted_attempts_for_exact_slot(self): ...
def test_trigger_key_normalizes_same_instant_to_utc(self): ...
```

目标接口：

```python
@dataclass(frozen=True)
class AccountAttemptState:
    attempts: int
    last_failed_at: int | None
    succeeded: bool

def get_account_attempt_state(
    self, *, account_email: str, trigger_type: str
) -> AccountAttemptState:
    ...
```

SQL 必须 JOIN `sync_job_account_log` 与 `sync_job_run`，不能复用只判断 run 是否存在的 `has_run_for_trigger()`。

slot trigger key 必须是稳定、可逆且使用 UTC 的规范值：

```python
def usage_periodic_trigger_type(slot: datetime) -> str:
    utc = slot.astimezone(timezone.utc)
    return f"usage_periodic:{utc.strftime('%Y%m%dT%H%M%S.%fZ')}"
```

禁止使用固定 `"usage_periodic"`，否则失败次数会跨 slot 累计。pre-reset 使用 `usage_pre_reset:<cycle_start_utc>`。

- [ ] **Step 2: 运行日志测试并确认失败**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_retry_log.py' -v
```

Expected: FAIL，查询接口尚不存在。

- [ ] **Step 3: 实现持久尝试状态查询**

实现必须：

1. 规范化 `account_email=trim().lower()`；
2. JOIN `sync_job_account_log a` 与 `sync_job_run r ON r.run_id=a.run_id`；
3. 精确匹配带 slot 的 `r.trigger_type`；
4. `attempts` 统计该账号/slot 的终态日志数；
5. `succeeded` 仅在账号日志存在 `status='success'` 时为真，不能因 run 存在而为真；
6. `last_failed_at` 取失败账号日志的最大 `ended_at`；
7. 无记录返回 `attempts=0, last_failed_at=None, succeeded=False`。

- [ ] **Step 4: 增加必要 SQLite 索引**

```sql
CREATE INDEX IF NOT EXISTS idx_sync_job_run_trigger
ON sync_job_run(trigger_type, run_id);

CREATE INDEX IF NOT EXISTS idx_sync_job_account_email_run
ON sync_job_account_log(account_email, run_id, ended_at);
```

- [ ] **Step 5: 运行日志测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_retry_log.py' -v
```

Expected: PASS。

- [ ] **Step 6: 编写 AccountResolver 失败测试**

覆盖：

- MySQL 与 SQLite email 交集；
- MySQL 有、本地无 → `not_collectable`；
- 本地有、MySQL 无 → `orphan_local_account`；
- trim/lower；
- 规范化后重复 email → 失败；
- `applicant/department` 保留。

- [ ] **Step 6A: 运行 resolver 测试并确认失败**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_account_resolver.py' -v
```

Expected: FAIL，resolver 尚未实现。

- [ ] **Step 7: 实现 AccountResolver**

放在 `usage_snapshot_refresh.py`：

```python
class AccountResolver:
    def __init__(self, usage_store: UsageSnapshotStore, token_store: TokenStore):
        ...

    def resolve(self) -> AccountMappingResult:
        mysql_rows = self.usage_store.list_monitor_accounts()
        local_rows = self.token_store.list_accounts()
        ...
```

严格执行设计 §4.3 查询契约，不猜测启停字段。

- [ ] **Step 8: 运行 resolver 测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_account_resolver.py' -v
```

Expected: PASS。

- [ ] **Step 9: 检查点**

```bash
git diff --check
git status --short
```

---

## Task 6: 实现跨进程账号锁和认证熔断器（已完成）

**Files:**
- Create: `cam/usage_snapshot_locks.py`
- Create: `cam/usage_auth_breaker.py`
- Create: `tests/test_usage_snapshot_locks.py`
- Create: `tests/test_usage_auth_breaker.py`

- [ ] **Step 1: 编写账号锁失败测试**

```python
def test_lock_filename_uses_email_sha256(self): ...
def test_same_email_cannot_be_acquired_twice_cross_process(self): ...
def test_different_emails_can_run_concurrently(self): ...
def test_timeout_returns_account_lock_busy(self): ...
def test_same_task_lock_allows_only_one_process(self): ...
```

- [ ] **Step 1A: 运行锁测试并确认失败**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_locks.py' -v
```

Expected: FAIL，锁模块尚不存在。

- [ ] **Step 2: 实现 UsageAccountLock**

公开接口：

```python
@contextmanager
def usage_account_lock(
    email: str, *, lock_dir: Path, timeout_sec: float
) -> Iterator[bool]:
    ...

@contextmanager
def usage_task_lock(path: Path) -> Iterator[bool]:
    ...
```

要求：

- `sha256(normalized_email)` 文件名；
- POSIX 使用 `fcntl.flock`；
- Windows 使用 `msvcrt.locking`；
- Windows 创建锁文件后确保至少写入 1 byte，并在 lock/unlock 前 `seek(0)`；
- 不导入或复用 `token_manager._lock_for`；
- finally 中可靠释放；
- 不在文件中写 email 明文。

- [ ] **Step 3: 编写熔断失败测试**

```python
def test_opens_at_failure_ratio_after_min_samples(self): ...
def test_old_successes_are_pruned_by_size_and_time(self): ...
def test_open_allows_cached_token_but_denies_refresh(self): ...
def test_half_open_allows_one_probe(self): ...
def test_half_open_success_closes_and_clears_window(self): ...
def test_half_open_failure_reopens(self): ...
def test_concurrent_recording_is_atomic(self): ...
def test_only_one_aggregated_alert_per_open_period(self): ...
```

- [ ] **Step 3A: 运行熔断测试并确认失败**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_auth_breaker.py' -v
```

Expected: FAIL，熔断器尚未实现。

- [ ] **Step 4: 实现 UsageAuthBreaker**

核心接口：

```python
class UsageAuthBreaker:
    def allow_cached_token(self) -> bool:
        return True

    def allow_refresh_or_login(self) -> bool:
        ...

    def record(self, outcome: AuthOutcome, *, now: datetime) -> None:
        ...

    def snapshot(self) -> BreakerSnapshot:
        ...
```

内部使用 `deque[(timestamp, account_email, outcome)]`，每次读写：

1. 清理超过 `window_min` 的样本；
2. 截断到最近 `window_size`；
3. 每次账号采集只记录一个终态；
4. 用 `threading.Lock` 保护状态。

- [ ] **Step 5: 运行锁和熔断测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_locks.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_usage_auth_breaker.py' -v
```

Expected: PASS。

- [ ] **Step 6: 检查点**

```bash
git diff --check
git status --short
```

---

## Task 7: 扩展 TokenManager 并实现采集器（已完成）

**Files:**
- Modify: `cam/models.py`
- Modify: `cam/token_manager.py`
- Create: `cam/usage_snapshot_collector.py`
- Create: `tests/test_token_manager_auth_policy.py`
- Create: `tests/test_usage_snapshot_collector.py`

- [ ] **Step 1: 编写 TokenManager auth policy 失败测试**

```python
def test_valid_cached_token_does_not_consult_breaker(self): ...
def test_expired_token_denied_before_refresh(self): ...
def test_browser_login_denied_before_profile_cleanup(self): ...
def test_default_policy_preserves_existing_behavior(self): ...
```

- [ ] **Step 1A: 运行 policy 测试并确认失败**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_token_manager_auth_policy.py' -v
```

Expected: FAIL，policy 尚未实现。

- [ ] **Step 2: 增加 typed exception 和 policy 协议**

`models.py`：

```python
class AuthCircuitOpenError(TokenAcquisitionError):
    pass
```

`token_manager.py`：

```python
class AuthPolicy(Protocol):
    def allow_refresh_or_login(self) -> bool: ...

class AllowAllAuthPolicy:
    def allow_refresh_or_login(self) -> bool:
        return True
```

- [ ] **Step 3: 向后兼容修改 TokenManager**

签名：

```python
def get_valid_token(
    self,
    account: Account,
    *,
    force_refresh: bool = False,
    auth_policy: AuthPolicy | None = None,
) -> str:
    ...

def force_relogin(
    self, account: Account, *, auth_policy: AuthPolicy | None = None
) -> str:
    ...
```

检查点：

- 有效缓存 token 直接返回；
- refresh 前检查；
- browser login 前再次检查；
- force fresh/profile 清理前检查；
- `auth_policy=None` 等价 AllowAll，现有调用方不变。

- [ ] **Step 4: 运行 TokenManager 新旧测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_token_manager_auth_policy.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_fetcher_token_retry.py' -v
```

Expected: PASS。

- [ ] **Step 5: 编写 collector 失败测试**

覆盖：

- API usage/plan/stripe 全成功；
- plan API 无名称时 Stripe fallback；
- API 都无名称时低频 Spending 页面 fallback；
- 401 返回 typed auth failure；
- network 与 parse error 不计 auth failure；
- breaker open 时有效缓存 token 仍可采集；
- raw payload 已脱敏；
- collector 不调用 `fetch_one` 的强制重登黑盒；
- 多线程同时 fallback 时活跃浏览器解析数不超过配置上限。

- [ ] **Step 5A: 运行 collector 测试并确认失败**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_collector.py' -v
```

Expected: FAIL，collector 尚未实现。

- [ ] **Step 6: 实现 collector**

公开接口：

```python
class UsageSnapshotCollector:
    def collect(
        self,
        account: Account,
        *,
        snapshot_type: SnapshotType,
        snapshot_slot: datetime,
        auth_policy: AuthPolicy,
    ) -> CollectionResult:
        ...
```

调用顺序：

```python
token = manager.get_valid_token(account, auth_policy=auth_policy)
client = CursorClient(token)
usage = client.get_current_period_usage()
plan = client.get_plan_info()
stripe = client.get_stripe_info()
return parse_usage_snapshot(...)
```

页面兜底需满足：

- 仅 plan tier 不明确时调用；
- 通过注入的 `PlanTierFallback` 协议调用；
- 生产实现 `SpendingPlanTierFallback` 使用进程级共享 semaphore，默认上限 2–4，并调用现有 `fetch_spending_panel_from_dashboard`；
- 所有 collector 实例共享同一 limiter，不能每个实例各建 semaphore；
- 用量和账期仍必须来自 API；
- 页面失败不把有效 usage 快照改成 0。

接口：

```python
class PlanTierFallback(Protocol):
    def resolve(self, account: Account) -> str | None: ...

class SpendingPlanTierFallback:
    def __init__(self, semaphore: threading.Semaphore): ...
    def resolve(self, account: Account) -> str | None: ...
```

- [ ] **Step 7: 运行 collector 测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_collector.py' -v
```

Expected: PASS。

- [ ] **Step 8: 检查点**

```bash
git diff --check
git status --short
```

---

# Chunk 3: 周期任务、调度与分析

## Task 8: 实现 periodic 和 pre-reset 批次（已完成）

**Files:**
- Modify: `cam/usage_snapshot_refresh.py`
- Create: `tests/test_usage_snapshot_refresh.py`

- [ ] **Step 1: 编写时间槽纯函数失败测试**

```python
def test_daily_slot_anchors_at_beijing_midnight(self): ...
def test_six_hour_slot_anchors_at_00_06_12_18(self): ...
def test_pre_reset_slot_equals_cycle_start(self): ...
```

- [ ] **Step 1A: 运行时间槽测试并确认失败**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_refresh.py' -v
```

Expected: FAIL，时间槽函数尚未实现。

接口：

```python
def periodic_slot(now: datetime, interval_hours: int, biz_tz: ZoneInfo) -> datetime:
    ...

def pre_reset_target(cycle_end: datetime, target_offset_min: int) -> datetime:
    ...
```

- [ ] **Step 2: 编写 periodic 编排失败测试**

覆盖：

- 当前 slot 已有快照则跳过；
- 失败后未到最小重试间隔跳过；
- 超过 slot 最大次数跳过并告警；
- 服务重启只补当前 slot；
- MySQL 主数据无本地凭据记 `not_collectable`；
- 账号进入 pre-reset 窗口时 periodic 排除；
- worker 等待后进入窗口，在获取账号锁前再次检查并跳过；
- 单账号失败不影响其他账号；
- 两进程同时 periodic 时任务锁只允许一个批次；
- 前一进程释放账号锁后，后一进程在锁内发现 slot 已存在，不调用 Cursor；
- breaker 打开后不再提交新账号 worker；
- breaker 打开期间只发送一次聚合告警，未开始账号不清 profile。
- periodic 和 pre-reset 使用同一个进程级 breaker 实例，跨两类批次累计样本可触发熔断。

- [ ] **Step 2A: 运行 periodic 编排测试并确认失败**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_refresh.py' -v
```

Expected: FAIL，periodic 编排尚未实现。

- [ ] **Step 3: 实现 run_periodic**

```python
def run_usage_periodic(
    *,
    now: datetime | None = None,
    emails: tuple[str, ...] | None = None,
) -> UsageRunSummary:
    ...
```

函数内部必须从当前 UTC slot 生成 `usage_periodic_trigger_type(slot)`，并先获取 `USAGE_PERIODIC_LOCK_FILE` 任务级跨进程锁。

账号不能一次性向 executor 提交全部 384 个任务。使用有界增量提交：最多保持 `concurrency` 个 in-flight future；每完成一个才检查 breaker 并决定是否提交下一个。breaker 打开后停止新增提交。

在 `usage_snapshot_refresh.py` 定义进程级 composition root：

```python
@dataclass
class UsageRuntime:
    breaker: UsageAuthBreaker
    collector: UsageSnapshotCollector
    store: UsageSnapshotStore

def get_usage_runtime() -> UsageRuntime:
    """进程内单例；periodic/pre-reset 必须共享同一 breaker。"""
    ...
```

每账号顺序：

1. 重新检查 pre-reset 窗口；
2. 获取 `UsageAccountLock`；
3. 在锁内重新检查当前 slot 是否已存在；
4. 在锁内再次检查 pre-reset 窗口；
5. 已完成或已进入窗口则跳过，不调用 Cursor；
6. collector.collect；
7. store.reconcile_and_write；
8. breaker.record 账号终态；
9. account log；
10. 释放锁。

- [ ] **Step 4: 编写 pre-reset 编排失败测试**

覆盖：

- `now < end-target` 不到期；
- `end-target <= now <= end-window_end` 到期；
- 最后 30 分钟不再启动；
- 已有合法 pre-reset 跳过；
- end 延后且旧 collected_at 早于新 target → 重采；
- end 缩短且旧 pre-reset 晚于 authoritative end → 结算时不用；
- 失败在安全窗口内按扫描周期重试；
- account lock busy 不写失败快照；
- 只写成功且完整的数据；
- 两进程同时扫描时 `USAGE_PRE_RESET_LOCK_FILE` 只允许一个批次；
- 账号锁内发现合法 pre-reset 已存在时不调用 Cursor；
- 临近窗口结束告警包含 email、cycle_end、剩余窗口、错误分类、下次重试；
- fallback/missing final 分别发送可断言的聚合告警。
- 两个账号具有不同 cycle_end 时，仅进入目标窗口的账号入选。
- 两个独立进程分别执行 periodic/pre-reset 时，同一 email 不并发调用 Cursor。

- [ ] **Step 4A: 运行 pre-reset 编排测试并确认失败**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_refresh.py' -v
```

Expected: FAIL，pre-reset 编排尚未实现。

- [ ] **Step 5: 实现 due 查询与 run_pre_reset_due**

```python
def run_usage_pre_reset_due(
    *, now: datetime | None = None, dry_run: bool = False
) -> UsageRunSummary:
    ...
```

该函数必须先获取 `USAGE_PRE_RESET_LOCK_FILE` 任务锁，并使用与 periodic 相同的有界增量提交和 breaker 停止规则。

`dry_run=True` 只返回：

```text
email
cycle_start
cycle_end
target_at
window_closes_at
reason
```

不得获取 token 或写业务表。

- [ ] **Step 6: 实现显式 repair service**

```python
def repair_usage_final(
    *,
    email: str,
    cycle_start: datetime,
    actor: str,
    reason: str,
) -> FinalizeResult:
    ...
```

要求：

- 获取账号锁；
- 调用 `store.repair_finalize_cycle`；
- 写运行和账号审计日志；
- 重复执行幂等；
- 无合法候选返回明确错误，不制造 0%。

- [ ] **Step 7: 运行 refresh 测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_refresh.py' -v
```

Expected: PASS。

- [ ] **Step 8: 检查点**

```bash
git diff --check
git status --short
```

---

## Task 9: 实现独立 UsageSchedulerCoordinator（已完成）

**Files:**
- Create: `cam/usage_scheduler.py`
- Modify: `cam/scheduler.py`
- Modify: `cam/web_server.py`
- Create: `tests/test_usage_scheduler.py`
- Modify: `tests/test_scheduler_billing_ledger.py`

- [ ] **Step 1: 编写 coordinator 失败测试**

```python
def test_periodic_and_pre_reset_use_independent_executors(self): ...
def test_blocked_legacy_scheduler_does_not_block_pre_reset_tick(self): ...
def test_blocked_periodic_does_not_block_pre_reset_tick(self): ...
def test_pre_reset_tick_has_priority(self): ...
def test_start_is_idempotent_in_same_process(self): ...
def test_stop_waits_pre_reset_and_cancels_pending_periodic(self): ...
def test_worker_exception_does_not_kill_timer_thread(self): ...
def test_usage_only_enable_starts_coordinator_without_legacy_jobs(self): ...
def test_repeated_startup_shutdown_leaves_no_usage_threads(self): ...
def test_reload_style_second_start_does_not_duplicate_threads(self): ...
```

- [ ] **Step 1A: 运行 coordinator 测试并确认失败**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_scheduler.py' -v
```

Expected: FAIL，coordinator 尚未实现。

- [ ] **Step 2: 实现 coordinator**

骨架：

```python
class UsageSchedulerCoordinator:
    def __init__(self, *, poll_interval_sec: int = 15):
        self._periodic_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cam-usage-periodic"
        )
        self._pre_reset_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cam-usage-pre-reset"
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None: ...
    def stop(self, *, timeout_sec: float = 30) -> None: ...
    def tick(self, now: datetime) -> None: ...
```

模块级：

```python
def start_usage_scheduler_once() -> UsageSchedulerCoordinator: ...
def stop_usage_scheduler(*, timeout_sec: float = 30) -> None: ...
```

用锁保护单例，重复 startup 不创建第二条计时线程。

- [ ] **Step 3: 接入旧调度器**

让现有 legacy loop 可停止：

```python
def run_scheduler_loop(
    poll_interval_sec: int = 30,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    stopper = stop_event or threading.Event()
    coordinator = start_usage_scheduler_once()
    try:
        while not stopper.is_set():
            run_legacy_due_tasks()
            stopper.wait(max(5, poll_interval_sec))
    finally:
        coordinator.stop(...)
```

关键：coordinator 自己计时，不能由旧 while 调用 `tick`，否则旧 BI/Spending/Ledger 仍会阻塞。

`run_scheduler_loop` 在 `usage_snapshot_enable=true` 且三个 legacy enable 均为 false 时也必须启动 coordinator；反之全部关闭时直接退出或不启动线程。

- [ ] **Step 4: 接入 Web 生命周期**

`web_server.py`：

```python
_legacy_scheduler_stop = threading.Event()
_legacy_scheduler_thread: threading.Thread | None = None

@app.on_event("startup")
async def _start_embedded_scheduler() -> None:
    ...

@app.on_event("shutdown")
async def _stop_embedded_scheduler() -> None:
    _legacy_scheduler_stop.set()
    stop_usage_scheduler(timeout_sec=30)
    if _legacy_scheduler_thread:
        _legacy_scheduler_thread.join(timeout=35)
```

要求：

- enable 判断加入 `usage_snapshot_enable`；
- startup 调用进程内幂等 start；
- shutdown 同时停止 coordinator 和可停止 legacy loop；
- 连续 startup/shutdown 测试后没有 `cam-usage-*` 残留线程；
- reload 的新进程不共享旧进程线程，单进程内仍防重复；
- 不增加任何页面、按钮或新 Web API。

- [ ] **Step 5: 运行调度测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_scheduler.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_scheduler_billing_ledger.py' -v
```

Expected: PASS，现有 Ledger 调度行为不变。

- [ ] **Step 6: 检查点**

```bash
git diff --check
git status --short
```

---

## Task 10: 实现浪费等级分析（已完成）

**Files:**
- Create: `cam/usage_waste.py`
- Modify: `cam/usage_snapshot_store.py`
- Create: `tests/test_usage_waste.py`
- Modify: `tests/test_usage_snapshot_store.py`

- [ ] **Step 1: 编写等级失败测试**

```python
def test_no_complete_cycle_is_unknown(self): ...
def test_unknown_current_plan_is_unknown(self): ...
def test_latest_healthy_cycle_is_l0(self): ...
def test_one_low_cycle_is_l1(self): ...
def test_two_low_cycles_is_l2(self): ...
def test_three_or_more_low_cycles_is_l3(self): ...
def test_healthy_cycle_breaks_low_streak(self): ...
def test_plan_change_resets_segment(self): ...
def test_pro_ultra_pro_does_not_join_two_pro_segments(self): ...
def test_current_new_tier_without_final_is_unknown(self): ...
def test_known_ended_cycle_without_final_is_unknown(self): ...
def test_gap_beyond_tolerance_is_unknown(self): ...
```

- [ ] **Step 2: 编写存储查询失败测试**

```python
def test_load_waste_inputs_returns_people_latest_known_and_finals(self): ...
def test_load_waste_inputs_does_not_join_ledger_month_as_cycle_cost(self): ...
```

- [ ] **Step 3: 运行并确认失败**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_waste.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_store.py' -v
```

Expected: FAIL，分析模块和 `load_waste_inputs` 尚未实现。

- [ ] **Step 4: 实现纯函数**

```python
def classify_waste(
    *,
    current_plan_tier: str,
    known_cycles: Sequence[KnownCycle],
    final_cycles: Sequence[FinalCycle],
    low_threshold_pct: Decimal,
    continuity_tolerance: timedelta,
) -> WasteAssessment:
    ...
```

算法严格按技术设计 §12：

1. 当前 tier unknown → UNKNOWN；
2. 最新已结束已知周期无 final → UNKNOWN；
3. 从最新 final 向前取当前连续档位段；
4. 周期不连续 → UNKNOWN；
5. 遇到第一条健康周期停止累计；
6. 0/1/2/3+ → L0/L1/L2/L3。

- [ ] **Step 5: 增加账号关联查询服务**

在 `UsageSnapshotStore` 增加：

```python
def load_waste_inputs(self, email: str | None = None) -> list[WasteInput]:
    ...
```

存储层只返回：

```text
cursor_accounts 人员字段
latest successful snapshot
known cycles
final cycles
```

等级计算留在 Python 纯函数，不写复杂且容易拼错档位段的窗口 SQL。

- [ ] **Step 6: 运行测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_waste.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_usage_snapshot_store.py' -v
```

Expected: PASS。

- [ ] **Step 7: 检查点**

```bash
git diff --check
git status --short
```

---

# Chunk 4: CLI、文档、验证与上线

## Task 11: 增加 CLI 运维入口

**Files:**
- Modify: `cam/cli.py`
- Create: `tests/test_usage_cli.py`

- [ ] **Step 1: 编写 CLI 失败测试**

使用 `click.testing.CliRunner`：

```python
def test_usage_snapshot_all_calls_periodic_service(self): ...
def test_usage_snapshot_email_filters_accounts(self): ...
def test_pre_reset_dry_run_has_no_writes(self): ...
def test_usage_finalize_requires_email_and_cycle_start(self): ...
```

- [ ] **Step 1A: 运行 CLI 测试并确认失败**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_cli.py' -v
```

Expected: FAIL，CLI 命令尚未实现。

- [ ] **Step 2: 实现命令**

```bash
python -m cam usage-snapshot --all --type periodic
python -m cam usage-snapshot --email user@example.com --type periodic
python -m cam usage-pre-reset-due
python -m cam usage-pre-reset-due --dry-run
python -m cam usage-finalize --email user@example.com --cycle-start ISO8601
```

要求：

- `--all` 与 `--email` 互斥；
- 默认 JSON 输出；
- repair/finalize 必须显式参数；
- 不在 CLI 重写采集逻辑；
- 返回码非 0 表示全批失败或配置/结构错误，部分失败输出状态但保留可审计明细。

- [ ] **Step 3: 运行 CLI 测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage_cli.py' -v
```

Expected: PASS。

- [ ] **Step 4: 检查点**

```bash
git diff --check
git status --short
```

---

## Task 12: 更新文档并执行完整验证（未完成：真实环境验证）

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-29-cursor-usage-monitoring-design.md`（仅当实施事实与设计有差异）
- Modify: `docs/superpowers/plans/2026-07-29-cursor-usage-monitoring-implementation.md`（勾选执行状态）

- [ ] **Step 1: 更新 README**

必须说明：

- 三张 MySQL 表的关联和时间粒度；
- periodic/pre-reset 默认配置；
- CLI 命令；
- 不按金额判断套餐；
- `periodic_fallback` 是降级数据；
- L0/L1/L2/L3/UNKNOWN；
- 账单自然月不能称为滚动账期精确成本；
- Windows 服务需要可写的 lock 目录；
- 密钥只能放 `.env`/部署环境。

- [ ] **Step 2: 运行新增用量测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_usage*.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_token_manager_auth_policy.py' -v
```

Expected: 全部 PASS，MySQL 集成测试在无环境时只能是明确 SKIPPED。

- [ ] **Step 2A: 执行发布门禁 MySQL 版本矩阵**

```bash
CAM_REQUIRE_MYSQL_TESTS=1 CAM_TEST_MYSQL_HOST=<mysql57> \
  .venv/bin/python -m unittest tests/test_usage_snapshot_store_mysql.py -v

CAM_REQUIRE_MYSQL_TESTS=1 CAM_TEST_MYSQL_HOST=<mysql80> \
  .venv/bin/python -m unittest tests/test_usage_snapshot_store_mysql.py -v
```

Expected: 两套环境全部 PASS，0 SKIPPED。

- [ ] **Step 3: 运行相关回归测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_fetcher_token_retry.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_scheduler_billing_ledger.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_spending_refresh.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_billing_ledger_refresh.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_config_concurrency.py' -v
```

Expected: PASS；若实施前基线已失败，必须确认没有新增失败并单独记录。

- [ ] **Step 4: 运行全仓测试**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected:

- 新增测试全部通过；
- 与 Task 1 基线比较；
- 不得声称既存失败已修复，除非本需求确实修改相关代码并有证据；
- 任一新增失败必须修复后才能上线。

- [ ] **Step 5: 静态和语法检查**

Run:

```bash
.venv/bin/python -m compileall cam tests
git diff --check
```

Expected: exit code 0。

- [ ] **Step 6: 测试库手工采集**

按顺序：

```bash
.venv/bin/python -m cam usage-snapshot --email <pro-test> --type periodic
.venv/bin/python -m cam usage-snapshot --email <pro-plus-test> --type periodic
.venv/bin/python -m cam usage-pre-reset-due --dry-run
```

核对：

- MySQL 快照值与 Cursor 页面一致；
- email 可关联 `cursor_accounts`；
- raw payload 无凭据；
- 重跑同 slot 不新增；
- 日志包含 run/account 结果。

- [ ] **Step 7: 预生产窗口验证**

选择一个 24 小时内 reset 的测试账号：

1. 确认 dry-run 目标时刻；
2. 等待 coordinator 自动抓 pre-reset；
3. 同 slot 重试不新增第二行；
4. reset 后 periodic 检测新 start；
5. 上一周期恰好一条 final；
6. final_source 与实际采集路径一致。

- [ ] **Step 7A: Windows 生产机实机验证**

PowerShell：

```powershell
$py = ".\.venv\Scripts\python.exe"
New-Item -ItemType Directory -Force ".\data\usage-account-locks" | Out-Null
& $py -m unittest tests.test_usage_snapshot_locks -v
& $py -m unittest tests.test_usage_scheduler -v
& $py -m cam usage-pre-reset-due --dry-run
schtasks /Query /TN CamWebService /V /FO LIST
```

必须验证：

1. `data\usage-account-locks` 对服务账号可写；
2. 锁文件至少 1 byte，`msvcrt.locking` 前后均从 offset 0 操作；
3. 测试启动的两个独立 Python 进程对同一 email/任务只有一个获得锁；
4. 启停 `CamWebService` 后无遗留 `cam-usage-*` 线程/进程；
5. usage-only 配置能启动 coordinator；
6. pre-reset dry-run 和实际触发时间误差不超过一个 scan interval；
7. legacy BI/Spending/Ledger 长任务运行时 pre-reset 仍准时触发。

- [ ] **Step 8: 分阶段开启**

```text
阶段 A：USAGE_SNAPSHOT_ENABLE=false，只部署代码和建表预检
阶段 B：3~5 个账号手工 periodic
阶段 C：全量 periodic，观察 2 天
阶段 D：少量 pre-reset
阶段 E：全量 pre-reset
阶段 F：积累完整周期后启用 L1；至少 2/3 个周期后使用 L2/L3
```

- [ ] **Step 9: 回滚演练**

1. 设置 `USAGE_SNAPSHOT_ENABLE=false`；
2. 确认旧 BI/Spending/Ledger 继续运行；
3. 不删除 `cursor_usage_snapshot`；
4. coordinator 停止提交新任务；
5. 历史 final 保留。

- [ ] **Step 10: 最终工作区检查**

```bash
git diff --check
git status --short --branch
```

Expected: 所有变更已核验；是否提交由用户另行明确决定。

---

## AC1–AC18 证据追踪

- **AC1 账号覆盖：** Task 4 主数据预检 + Task 5 AccountResolver 测试 + Task 12 手工关联。
- **AC2 periodic 写入：** Task 8 periodic 测试 + Task 12 测试库手工采集。
- **AC3 多 periodic：** Task 4 不同 slot 存储测试。
- **AC4 单 pre-reset：** Task 4 唯一键/事务测试 + Task 8 双进程测试。
- **AC5 不同 end 不同到期：** Task 8 due 查询测试 + Task 12 dry-run。
- **AC6 窗口内重试：** Task 5 slot 持久状态 + Task 8 pre-reset 重试测试。
- **AC7 唯一 final/明确缺失：** Task 4 finalization 并发测试。
- **AC8 fallback 可追溯：** Task 4 `final_source` 测试 + Task 8 告警测试。
- **AC9 档位不按金额推断：** Task 1 fixture + Task 2 parser 测试。
- **AC10 浪费等级：** Task 10 全部等级和连续档位段测试。
- **AC11 Ledger 公式不变：** Task 12 `test_billing_ledger_refresh.py` 回归。
- **AC12 认证熔断：** Task 6 breaker + Task 7 policy + Task 8 增量提交/聚合告警测试。
- **AC13 raw 脱敏：** Task 1 fixture 审查 + Task 2 递归脱敏测试。
- **AC14 无前端改动：** Task 0/各检查点 git diff + 最终文件审查。
- **AC15 end 修正：** Task 4 end 延长/缩短存储测试 + Task 8 重采测试。
- **AC16 跨进程 email 互斥：** Task 6 双进程账号锁 + Windows 实机验证。
- **AC17 slot 不重复调用：** Task 8 任务锁、账号锁内二次检查和双进程测试。
- **AC18 缺 final 为 UNKNOWN：** Task 10 数据断裂测试。

---

## 最终完成定义

以下条件全部满足才算完成：

- [ ] 技术设计中的 AC1–AC18 全部有测试或手工证据；
- [ ] 只新增 `cursor_usage_snapshot` 一张业务表；
- [ ] 未修改前端页面；
- [ ] Pro/Pro+/Free/unknown 及现网实际存在的更高档位有真实解析证据；无可用 Ultra 时有书面记录和合成名称映射测试；
- [ ] reset 前后 fixture 证明账期切换字段行为；
- [ ] periodic 一个账期可多条且 slot 幂等；
- [ ] pre-reset 每账期一条且 end 修正可重采；
- [ ] 新 start 触发旧周期原子结算；
- [ ] 非法迟到写入被拒绝；
- [ ] periodic fallback 明确标记；
- [ ] current tier unknown 或周期数据断裂时浪费等级为 UNKNOWN；
- [ ] 认证故障不会触发大规模 profile 清理；
- [ ] 旧调度长任务不会阻塞 pre-reset；
- [ ] 密码、token、Cookie、IMAP 密码未进入代码、日志和 raw payload；
- [ ] 新增测试全部通过；
- [ ] 全仓结果与实施前基线相比没有新增失败；
- [ ] Windows 生产机完成真实锁和调度验证；
- [ ] 分阶段上线和回滚演练完成。
