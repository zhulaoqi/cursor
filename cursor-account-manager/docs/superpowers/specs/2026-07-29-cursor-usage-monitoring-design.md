# Cursor 多账号订阅与用量监控详细设计

> 文档类型：需求分析与技术设计
> 日期：2026-07-29
> 状态：已完成方案确认，待实施
> 适用项目：`cursor-account-manager`
> 一期边界：只新增 `cursor_usage_snapshot` 一张业务表；不开发新的前端页面

---

## 1. 背景

团队已采购多个 Cursor 个人订阅，包含 Pro（标价 20 USD/月）、Pro+（标价 60 USD/月）以及可能存在的更高档套餐。当前需要人工逐个登录账号，在 Spending、Billing & Invoices 页面查看套餐和用量，无法集中回答以下问题：

1. 每个账号当前属于哪个订阅档位；
2. 每个账号在自己的滚动账期内用了多少；
3. 哪些账号连续多个完整账期用量偏低；
4. 账号升级、降级、退款后，如何避免把不同统计粒度的数据错误合并；
5. 如何在每个账号 reset 前可靠保留本账期的最终用量。

不同账号的订阅生效时间不同。例如账号 A 的账期可能是 7 月 7 日至 8 月 7 日，账号 B 可能是 7 月 19 日至 8 月 19 日。因此不能使用全公司统一的月初、月末作为用量结算点。

---

## 2. 目标与非目标

### 2.1 一期目标

| 编号 | 目标 |
|---|---|
| G1 | 自动采集账号当前订阅档位、订阅状态、账期起止和总用量百分比 |
| G2 | 将每次成功采集结果写入 aicoding MySQL 的时序快照表 |
| G3 | 支持每日或每 N 小时的常规快照，用于观察用量趋势 |
| G4 | 按每个账号自己的 `billing_cycle_end` 动态执行 reset 前期末采集 |
| G5 | 检测账期切换，并将上一账期的一条快照确认为账期最终记录 |
| G6 | 通过连续快照识别系统可观测到的套餐变化，不新增订阅事件表 |
| G7 | 提供按完整账期计算的低用量等级口径 |
| G8 | 与现有 `cursor_accounts`、`cursor_billing_ledger_summary` 通过 email 关联 |
| G9 | 提供失败重试、熔断、告警、幂等和可审计能力 |

### 2.2 一期非目标

1. 不开发新的用量查询页面、图表或管理按钮；
2. 不新增 `cursor_subscription_event`、调度任务表或分析结果表；
3. 不修改现有退款和净支出计算公式；
4. 不使用含税账单金额反推订阅档位；
5. 不承诺得到 Cursor 未提供的精确订阅变更时刻；
6. 不将自然月账单金额伪装成滚动账期的精确成本；
7. 不替代现有使用明细 ODS、账单下载和 Billing Ledger 任务。

---

## 3. 名词和统计粒度

### 3.1 名词

| 名词 | 定义 |
|---|---|
| Billing Cycle | Cursor 返回的账号当前用量周期，使用 `billing_cycle_start` 和 `billing_cycle_end` 表示 |
| periodic | 常规时序快照，默认每天一次，也可配置为每 N 小时 |
| pre_reset | 在当前账期 reset 前安全窗口内执行的期末候选快照 |
| 账期最终记录 | 账期切换被确认后，选中的一条 `is_cycle_final=1` 快照 |
| plan_tier | 明确的套餐档位，如 `pro`、`pro_plus`、`ultra`、`free`、`unknown` |
| 首次检测时间 | 系统第一次在快照中看到某项变化的 `collected_at`，不等同于精确事件发生时间 |

### 3.2 必须区分的粒度

1. 用量：按账号自己的滚动 Billing Cycle；
2. 费用和退款：现有 Ledger 按 `billing_month` 自然月汇总；
3. 人员和部门：`cursor_accounts` 当前主数据。

一期综合分析可以同时展示三类信息，但必须保留各自时间粒度。若要把退款精确分摊到某个滚动账期，需要发票明细及事件时间，现有月汇总表无法无损完成。

---

## 4. 当前代码能力与差距

### 4.1 可直接复用的能力

| 能力 | 当前实现 | 复用方式 |
|---|---|---|
| Token 缓存、刷新、浏览器登录兜底 | `cam/token_manager.py` | 采集前调用 `get_valid_token` |
| Cursor API | `cam/api_client.py:124-136` | 复用 Usage、Plan、Usage Limit 接口 |
| 单账号容错采集 | `cam/fetcher.py:22-175` | 复用或抽取其调用、重试和错误分类 |
| 当前用量字段解析参考 | `cam/exporter.py:244-270` | 已识别账期和 `planUsage.totalPercentUsed` |
| Spending 页面套餐名解析 | `cam/plan_scraper.py:204-220` | 仅作为 API 无明确档位时的低频兜底 |
| 套餐/按量每日刷新 | `cam/spending_refresh.py` | 可复用批量账号准备、日志和结果汇总模式 |
| 常驻调度循环和跨平台文件锁 | `cam/scheduler.py` | 增加 periodic 和 pre-reset 到期扫描 |
| MySQL 连接池和 upsert | `cam/billing_ledger_store.py` | 复用连接参数和存储实现模式 |
| 运行与账号级日志 | `cam/sync_log_store.py` | 记录批次和失败账号，不向快照表写失败假数据 |
| 费用和退款月汇总 | `cursor_billing_ledger_summary` | 继续作为成本事实来源 |

### 4.2 当前缺失

| 缺失项 | 影响 |
|---|---|
| 当前套餐信息只覆盖 SQLite 账号当前值，没有 MySQL 时序历史 | 无法分析套餐变化和历史浪费 |
| 没有 `cursor_usage_snapshot` | 无法保存用量趋势和期末结果 |
| 现有调度仅支持固定每日时刻 | 无法按账号不同 reset 时间执行 |
| 没有账期切换结算逻辑 | reset 后上一周期最终值可能丢失 |
| 没有低用量连续账期等级 | 无法稳定识别长期浪费 |
| 没有批量认证熔断 | Cursor 认证故障时可能触发大量账号强制重登 |
| MySQL `cursor_accounts` 未被当前代码直接读取 | 需要明确监控账号全集与本地可登录账号的关系 |
| `sync_log_store.has_run_for_trigger` 只判断 run 是否存在，不区分成功/失败，也没有账号+slot 尝试查询 | 不能直接支撑 periodic 持久退避，必须扩展日志查询接口 |
| BI 当前套餐金额主要通过 Spending 页面 DOM 获取，API 套餐名仅用于导出 | 新采集器采用 API 优先前必须用真实 fixture 对齐两种口径 |
| 当前配置文件存在数据库密码默认值硬编码风险 | 实施前必须改为空默认值并启动校验，密钥只允许来自部署环境 |

### 4.3 账号全集与可采集账号

监控账号全集以 aicoding 的 `cursor_accounts` 为准；实际登录凭据和 token 仍来自本地 SQLite `accounts`、`tokens` 表。

执行时按规范化 email 取交集：

```text
cursor_accounts（应监控）
        ∩
SQLite accounts（具备登录资料）
        =
本轮可采集账号
```

存在于 `cursor_accounts` 但不在本地 SQLite 的账号必须计入运行结果，状态为 `not_collectable`，不得静默遗漏。存在于本地 SQLite 但不在 `cursor_accounts` 的账号默认不写业务快照，并记录为 `orphan_local_account`，避免产生无法归属的数据。

一期固定的主数据查询契约为：

```sql
SELECT id, email, applicant, department
FROM cursor_accounts
WHERE email IS NOT NULL AND TRIM(email) <> '';
```

需求给出的表结构没有账号启停字段，因此一期默认所有有效 email 都需要监控，不猜测其他状态列。上线预检必须读取 `information_schema.columns/statistics` 并验证：

1. 上述四个字段真实存在；
2. email 原始唯一键存在；
3. `lower(trim(email))` 后仍无重复；若有重复则本轮失败并输出冲突记录，不能任取一条；
4. email 的字符集和 collation 可与新表一致；
5. Ledger 的 email 也能按同一规范关联。

若现网后来增加明确的启停字段，应通过下一版需求更新查询契约，不能在实现中隐式猜测。

---

## 5. 方案选择

### 5.1 采用方案

采用“现有常驻调度器 + 数据库驱动到期扫描”：

1. periodic 使用固定周期批量运行；
2. pre-reset 每 15 分钟扫描一次最新账期信息；
3. 只对进入安全窗口的账号创建内存任务；
4. 快照表唯一键和进程锁共同保证幂等；
5. 不持久化独立调度任务。

现有 `run_scheduler_loop()` 会同步执行 BI、Spending 和 Ledger 长任务，不能依赖它的 while 循环准时扫描 pre-reset。改造后由独立的 `UsageSchedulerCoordinator` 计时线程负责用量任务；`run_scheduler_loop()` 或 Web startup 只负责启动/停止 coordinator，旧任务即使阻塞也不影响用量扫描：

1. periodic 和 pre-reset 各使用一个独立的单 worker 执行器及独立 `next_due_at/running_future`；
2. coordinator 自己的计时线程每 5–30 秒检查两类任务，不等待任务完成；
3. pre-reset 每 15 分钟准时扫描，优先级高于 periodic；
4. periodic 构建账号队列时先排除已进入 pre-reset 目标窗口的账号；每个 periodic worker 真正获取账号锁前必须用最新 cycle end 再检查一次，等待期间已进入窗口则立即跳过并交给 pre-reset；
5. 两个执行器遇到同一 email 时由 `UsageAccountLock` 最终互斥；
6. coordinator 使用进程内单例保护，避免 CLI 调度循环与 Web startup 在同一进程重复启动；
7. 关闭服务时停止提交新任务，等待当前 pre-reset 有界完成，再取消尚未开始的 periodic worker；
8. 任务完成回调负责写汇总日志，异常不得杀死 coordinator 计时线程。

### 5.2 未采用方案

| 方案 | 不采用原因 |
|---|---|
| 两个外部 Cron | pre-reset 仍需频繁全表扫描，任务状态与应用日志割裂 |
| 每账号持久化动态任务 | 需要新增任务表或队列，与一期单表约束冲突 |
| 只在每天固定时刻抓一次 | 无法可靠覆盖每个账号不同的 reset 时间 |
| 所有数据都通过页面抓取 | 速度慢、DOM 易变、384 账号浏览器负载和认证风险过高 |

---

## 6. 总体架构

```text
cursor_accounts (MySQL) ── 监控账号全集 ─┐
                                        ├─ AccountResolver
SQLite accounts/tokens ── 登录资料 ─────┘
                                               │
                                               ▼
                                     UsageScheduleService
                                    ┌──────────┴──────────┐
                                    │                     │
                              periodic 批次          pre-reset 到期扫描
                                    │                     │
                                    └──────────┬──────────┘
                                               ▼
                                    UsageSnapshotCollector
                                    TokenManager + CursorClient
                                               │
                                               ▼
                                      UsagePayloadNormalizer
                                               │
                                               ▼
                                      UsageSnapshotStore
                                               │
                         ┌─────────────────────┴─────────────────────┐
                         ▼                                           ▼
              cursor_usage_snapshot                    CycleReconciler
              原始快照 + 最终记录                       账期切换与最终记录
                         │
                         ▼
           SQL/BI：档位、趋势、浪费等级、人员/成本关联
```

### 6.1 模块职责

#### AccountResolver

- 从 MySQL 获取应该监控的 email；
- 从 SQLite 获取可登录账号；
- 执行 `trim + lower` 规范化和交集匹配；
- 返回可采集、不可采集和本地孤儿账号三类结果。

#### UsageSnapshotCollector

- 获取有效 token；
- 优先调用 Cursor API；
- 校验返回数据完整性；
- 返回领域对象，不直接写库；
- API 无明确套餐档位时才允许使用现有 Spending 页面解析作低频兜底。

#### UsagePayloadNormalizer

- 解析并规范化账期、百分比、套餐名和订阅状态；
- 保留原始 payload；
- 将字段缺失与合法的 0% 明确区分；
- 输出解析版本 `parser_version`。

#### UsageSnapshotStore

- 幂等写入 periodic；
- 条件 upsert pre-reset；
- 查询各账号最新已知账期；
- 在事务中选择并标记账期最终记录；
- 查询浪费分析所需的最终账期数据。

#### UsageScheduleService

- 触发 periodic；
- 每隔固定时间扫描 pre-reset 候选；
- 控制并发、锁、重试和熔断；
- 写现有运行日志并发送告警。

#### CycleReconciler

- 比较当前成功快照和数据库最新账期；
- 检测 `billing_cycle_start` 变化；
- 关闭旧账期；
- 选择 pre-reset 或 periodic fallback 作为最终记录。

---

## 7. 数据来源与字段规范

### 7.1 数据源优先级

| 目标字段 | 首选来源 | 兜底来源 | 禁止来源 |
|---|---|---|---|
| `billing_cycle_start/end` | `GetCurrentPeriodUsage` | 无；缺失则拒绝写入 | 当前服务器日期推算 |
| `total_used_pct` | `planUsage.totalPercentUsed` | 经过版本化规则解析的等价 API 字段 | 页面肉眼数字、账单金额 |
| `plan_tier` | `GetPlanInfo` 的明确名称 | Stripe membership；低频 Spending 页面 `Current Plan` | 含税金额反推 |
| `plan_status` | Stripe/API 明确状态 | `unknown` | 根据是否有用量猜测 |

当前 `cam/exporter.py:259-267` 已证明项目使用以下结构：

```text
usage.billingCycleStart
usage.billingCycleEnd
usage.planUsage.totalPercentUsed
```

实现前必须保存至少一份真实 API fixture，确认：

1. 时间戳单位是秒还是毫秒；
2. `totalPercentUsed` 是 0–100 还是 0–1；
3. Pro+ 的真实返回名称；
4. Ultra 或未来档位的命名；
5. reset 瞬间接口返回旧周期还是新周期。

不得仅因为数值小于等于 1 就自动乘以 100；真实的 0.5% 与比例 0.5 无法仅靠数值区分。百分比单位必须由接口契约或 fixture 确认，并写入版本化解析规则。

### 7.2 套餐档位规范化

建议规范值：

| 原始值示例 | `plan_tier` |
|---|---|
| Pro | `pro` |
| Pro Plus、Pro+、ProPlus | `pro_plus` |
| Ultra | `ultra` |
| Team、Business、Enterprise | 对应小写规范名 |
| Free、Hobby、未开通 | `free` |
| 新值或解析失败 | `unknown`，原文保存在 `plan_tier_raw` |

档位映射必须按名称，不按 20、60、200 或含税后的 21.32、63.96、213.20 映射。

### 7.3 订阅事件的一期语义

一期不新增事件表。通过同一账号按时间排序后的快照，使用 `LAG(plan_tier)`、`LAG(billing_cycle_start)` 推导“系统检测到的变化”：

- `plan_tier` 变化：检测到套餐变化；
- `billing_cycle_start` 变化：检测到账期切换；
- 两者同时变化：可能是升级、降级或续期边界变更。

推导结果的事件时间只能称为 `detected_at=collected_at`。如果 Cursor API 未返回真实 `effective_at`，不得将 `detected_at` 命名为精确生效时间。

---

## 8. 新表设计

### 8.1 设计原则

1. 一张表同时承载时序快照和账期最终记录；
2. periodic 一个账期允许多条；
3. pre-reset 一个账期只保留一条最新成功结果；
4. 失败不写入快照表；
5. 0% 是合法业务值，NULL 表示缺失；
6. 原始数据不得包含 access token、refresh token、Cookie 或 IMAP 密码。

### 8.2 建议 DDL

建表前先执行 `SHOW TABLE STATUS` 和 `SHOW FULL COLUMNS`，确认 `cursor_accounts.email` 与 `cursor_billing_ledger_summary.email` 的真实字符集和 collation。以下 `<EMAIL_COLLATION>` 必须替换为与现有 email 列一致的值，不能盲目使用 `utf8mb4_0900_ai_ci`。

```sql
CREATE TABLE IF NOT EXISTS cursor_usage_snapshot (
    id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    email                VARCHAR(320) NOT NULL COMMENT '规范化 Cursor 账号邮箱',

    plan_tier            VARCHAR(32) NOT NULL DEFAULT 'unknown'
                           COMMENT 'pro/pro_plus/ultra/free/unknown...',
    plan_tier_raw        VARCHAR(128) NULL COMMENT 'Cursor 返回的原始套餐名',
    plan_status          VARCHAR(32) NOT NULL DEFAULT 'unknown',
    plan_source          VARCHAR(32) NOT NULL DEFAULT 'api'
                           COMMENT 'api/stripe/spending_page/unknown',

    billing_cycle_start  DATETIME(3) NOT NULL COMMENT 'UTC，无时区 DATETIME',
    billing_cycle_end    DATETIME(3) NOT NULL COMMENT 'UTC，无时区 DATETIME',
    total_used_pct       DECIMAL(5,2) NOT NULL COMMENT '0.00~100.00',

    snapshot_type        VARCHAR(16) NOT NULL
                           COMMENT 'periodic/pre_reset',
    snapshot_slot        DATETIME(3) NOT NULL
                           COMMENT '幂等时间槽；periodic 为频率槽，pre_reset 为 cycle_start',
    collected_at         DATETIME(3) NOT NULL COMMENT '实际成功采集时间 UTC',

    is_cycle_final       TINYINT(1) NOT NULL DEFAULT 0,
    final_source         VARCHAR(32) NULL
                           COMMENT 'pre_reset/periodic_fallback',
    finalized_at         DATETIME(3) NULL COMMENT '确认账期切换并结算的时间 UTC',

    source_endpoint      VARCHAR(255) NULL,
    parser_version       VARCHAR(32) NOT NULL,
    raw_payload          JSON NULL,

    created_at           DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at           DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                           ON UPDATE CURRENT_TIMESTAMP(3),

    PRIMARY KEY (id),
    UNIQUE KEY uk_usage_snapshot_slot (
        email, billing_cycle_start, snapshot_type, snapshot_slot
    ),
    KEY idx_usage_email_collected (email, collected_at),
    KEY idx_usage_email_cycle_end (email, billing_cycle_end),
    KEY idx_usage_due_scan (billing_cycle_end, snapshot_type, collected_at),
    KEY idx_usage_final (email, is_cycle_final, billing_cycle_end),

    CONSTRAINT chk_usage_snapshot_type
        CHECK (snapshot_type IN ('periodic', 'pre_reset')),
    CONSTRAINT chk_usage_pct
        CHECK (total_used_pct >= 0 AND total_used_pct <= 100),
    CONSTRAINT chk_usage_cycle
        CHECK (billing_cycle_end > billing_cycle_start),
    CONSTRAINT chk_usage_final_state
        CHECK (
            (is_cycle_final = 0 AND final_source IS NULL AND finalized_at IS NULL)
            OR
            (is_cycle_final = 1
             AND final_source IN ('pre_reset', 'periodic_fallback')
             AND finalized_at IS NOT NULL)
        )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COLLATE=<EMAIL_COLLATION>
  COMMENT='Cursor 账号订阅档位与账期用量时序快照';
```

兼容性说明：

- MySQL 5.7 不支持 `utf8mb4_0900_ai_ci`，也不会强制执行 CHECK；
- 若实际环境是 MySQL 5.7，校验必须同时在 Python 层执行；
- 是否增加物理外键应以现网表引擎、字符集和运维约束为准；一期建议逻辑关联，不增加外键，避免采集任务被主数据短暂不一致阻塞。

`IF NOT EXISTS` 只保证重复执行不报错，不保证旧表结构正确。建表后必须逐项校验字段类型、NULL 约束、默认值、唯一键、普通索引、`ENGINE=InnoDB`、字符集和 collation；MySQL 8.x 还要核对 CHECK 定义。任一不一致时拒绝启动采集并要求执行版本化迁移。后续结构变化使用带版本号的迁移脚本，不在运行期静默 ALTER。

### 8.3 为什么不能使用原建议唯一键

原建议：

```text
(email, billing_cycle_start, snapshot_type)
```

会导致一个账期只能写一条 periodic，与“每日或每 N 小时记录趋势”冲突。

修正后：

```text
(email, billing_cycle_start, snapshot_type, snapshot_slot)
```

periodic 的 `snapshot_slot` 随采集频率变化；pre-reset 的 `snapshot_slot` 恒等于该账期 `billing_cycle_start`，因此同一账期只会保留一条 pre-reset。

账期的稳定身份定义为：

```text
cycle_key = (email, billing_cycle_start)
```

`billing_cycle_end` 是该账期的可修正属性，不参与账期身份。Cursor 若在同一个 start 下修正 end，只更新后续快照和调度依据，不得触发账期切换，也不得生成第二条 pre-reset。只有 `billing_cycle_start` 变化才能关闭旧账期。

### 8.4 snapshot_slot 生成

1. 在业务时区 Asia/Shanghai 中按周期向下取整；
2. 再转换为 UTC 无时区 DATETIME 写库。

示例：

- 每 24 小时：当天业务日槽；
- 每 6 小时：00:00、06:00、12:00、18:00 四个槽；
- pre-reset：直接使用 API 返回的 `billing_cycle_start`。

手工重跑默认复用当前时间槽，不重复插入。若确需保留诊断重跑，应另用调试输出，不污染业务快照。

### 8.5 pre-reset 条件 upsert

同一窗口内只有“更晚且字段校验成功”的结果可以覆盖。为避免 MySQL `ON DUPLICATE KEY UPDATE` 从左到右求值导致不同字段使用不同版本，本期不使用多字段条件表达式拼接，而采用同一事务内的显式比较：

1. 按唯一键 `SELECT ... FOR UPDATE`；
2. 无旧行时插入完整领域对象；
3. 有旧行且 `incoming.collected_at <= stored.collected_at` 时返回幂等成功，不更新；
4. 有旧行且 incoming 更新时，一次 `UPDATE` 覆盖全部采集字段：套餐、状态、来源、cycle end、百分比、采集时间、端点、解析版本和 raw payload；
5. `is_cycle_final/final_source/finalized_at` 不由普通 upsert 改写；
6. 提交事务。

该方式同时兼容 MySQL 5.7 和 8.x，且不会出现 `collected_at` 已更新后，后续字段比较条件变成 false 的问题。

### 8.6 最终记录唯一性

MySQL 没有通用的条件唯一索引，一期由事务保证同账号同账期只有一条 `is_cycle_final=1`：

1. 已持有该 email 的跨进程账号锁；
2. 使用 `uk_usage_snapshot_slot` 的 `(email, billing_cycle_start)` 前缀执行 `SELECT id ... FOR UPDATE`，锁定旧账期全部快照及插入范围；
3. 从旧周期最后一条成功快照取得 `authoritative_cycle_end`；
4. 将旧账期所有行 `is_cycle_final=0` 且清空 `final_source/finalized_at`；
5. 只在 `collected_at <= authoritative_cycle_end` 时选择 pre-reset；
6. 没有合法 pre-reset 时，选择 `collected_at <= authoritative_cycle_end` 的最新 periodic；
7. 将选中行的规范化 `billing_cycle_end` 同步为 authoritative end，并更新 `is_cycle_final=1`、`final_source`、`finalized_at`；
8. 没有合法候选时记录 `missing_cycle_final`；
9. 提交事务。

周期切换重跑时重复执行同一事务，结果必须一致。正常采集一旦观察到更新的 `billing_cycle_start`，后续迟到的旧周期写入应被拒绝并记为 `stale_cycle_write`；只有显式 repair 命令可以绕过。跨进程账号锁与 InnoDB 范围锁共同防止 finalization 和迟到写入竞争。

---

## 9. 采集与调度设计

### 9.1 Bootstrap

以下情况立即执行 bootstrap periodic：

1. 新账号首次进入 `cursor_accounts`；
2. 账号存在但快照表没有任何成功记录；
3. 最新成功快照超过可配置的最大陈旧时间。

Bootstrap 目的是发现真实账期，不根据开户时间或账单月份推算。

对应配置：

```text
USAGE_BOOTSTRAP_STALE_HOURS=36
```

### 9.2 常规 periodic

默认配置：

```text
USAGE_PERIODIC_INTERVAL_HOURS=24
USAGE_SNAPSHOT_CONCURRENCY=10
USAGE_BOOTSTRAP_STALE_HOURS=36
```

调度循环先在 Asia/Shanghai 计算当前 `snapshot_slot`，再按账号查询该 slot 是否已存在。只有 slot 缺失或超过 `USAGE_BOOTSTRAP_STALE_HOURS` 的账号才进入采集队列，因此常驻循环每 30 秒运行也不会反复调用 384 个账号。

非 24 小时间隔以业务日 00:00 为固定锚点。例如 6 小时周期只产生 00:00、06:00、12:00、18:00 四个 slot。服务重启后重算当前 slot；当前 slot 已存在则跳过，缺失则补采，不补造更早的普通 periodic。

当前 slot 缺失不代表可以每 30 秒无限重试。每个账号/slot 的失败尝试写入现有 sync run/account log，`trigger_type` 中包含 slot。现有 `has_run_for_trigger` 不能满足该需求，需要给 `SyncLogStore` 增加按 `account_email + trigger_type/slot` 查询尝试次数和最后失败时间的接口，并明确只把成功账号视为完成。候选选择同时要求：

```text
该账号/slot 尝试次数 < USAGE_PERIODIC_MAX_ATTEMPTS_PER_SLOT
且距离上次失败 >= USAGE_PERIODIC_RETRY_MINUTES
```

该判定使用持久运行日志，因此服务重启后仍生效；进程内退避只作为优化，不能作为唯一依据。

候选查询语义：

```text
cursor_accounts
LEFT JOIN 当前 snapshot_slot 的 cursor_usage_snapshot
WHERE 当前 slot 不存在
  AND 账号/slot 未达到最大尝试次数
  AND 距离最近一次失败已超过最小重试间隔
```

“从未成功”或“超过 stale 阈值”用于提高告警等级，不绕过 slot 的重试上限。

流程：

1. AccountResolver 生成账号清单；
2. 创建现有 sync run；
3. 有界并发采集；
4. 每个账号独立校验并写入；
5. 成功一个提交一个，避免一个账号失败回滚全批；
6. 汇总成功、失败、不可采集、熔断跳过数量；
7. 在账号锁和数据库事务内执行“比较旧周期 → 必要时结算旧周期 → 写入新快照”；
8. 完成运行日志和告警。

### 9.3 pre-reset 到期扫描

默认配置：

```text
USAGE_PRE_RESET_SCAN_INTERVAL_MIN=15
USAGE_PRE_RESET_WINDOW_START_MIN=360
USAGE_PRE_RESET_TARGET_OFFSET_MIN=180
USAGE_PRE_RESET_WINDOW_END_MIN=30
```

语义：

- 安全窗口：reset 前 6 小时到前 30 分钟；
- 目标采集时刻：reset 前 3 小时；
- 调度器每 15 分钟扫描；
- 到达目标时刻后立即采集；
- 失败后在安全窗口内继续重试；
- 若服务在目标时刻停机，只要恢复时仍在安全窗口内就立即补抓。

候选账号必须满足：

1. 有最新成功快照及合法的 `billing_cycle_end`；
2. 当前时间已进入目标时刻；
3. `now <= billing_cycle_end - window_end`，即进入 reset 前 30 分钟后停止采集；
4. 本账期 pre-reset 不存在，或 `stored.collected_at < corrected_cycle_end - target_offset`；
5. 账号当前可登录；
6. 未被批量认证熔断阻止。

一期默认成功一次即完成该账期 pre-reset。唯一例外是同一 start 的 end 被延后，导致原 pre-reset 早于修正后的目标时刻；此时上述可计算条件会让账号在新目标窗口再次进入候选，并更新同一行。不依赖表中不存在的“需重试”状态。

### 9.4 账期切换检测

每次成功采集后调用一个原子存储操作 `reconcile_and_write(incoming)`：

1. 获取独立的跨进程 email 锁；
2. 开启数据库事务；
3. 在写入 incoming 前，查询并锁定该账号最新的已知 `billing_cycle_start`；
4. 无历史周期时直接写 incoming；
5. incoming start 等于历史 start 时写同一周期；end 不同只视为 end 修正；
6. incoming start 晚于历史 start 时，先结算旧周期，再写 incoming；
7. incoming start 早于历史 start 时拒绝普通写入，标记 `stale_cycle_write`；
8. 提交后释放账号锁。

#### 正常续期

```text
旧 start != 新 start
plan_tier 相同
```

关闭旧周期，开始新周期。

#### 升级或降级导致周期改变

```text
旧 start != 新 start
plan_tier 也变化
```

关闭旧周期；新周期从 API 实际返回时间开始。不得按照账单金额或自然月补造周期。

#### 套餐变化但周期未变

```text
start 相同
plan_tier 变化
```

保留前后快照，记录首次检测时刻；该账期最终档位以最终记录为准。分析时可同时展示“账期内发生档位变化”标记。

#### 账期结束时间被修正

```text
start 相同
end 变化
```

不关闭账期。以最新成功快照中的 end 重新计算 pre-reset 到期时间；同账期 pre-reset 仍命中同一唯一键。若 end 延后且原 pre-reset 早于新目标时刻，在新的安全窗口重采。若 end 缩短，finalization 只接受 `collected_at <= authoritative_cycle_end` 的 pre-reset。

#### reset 后仍返回旧周期

在接口真正返回新 `billing_cycle_start` 前不提前关闭旧周期。调度器允许短期延迟，下一次 periodic/bootstrap 再确认。

### 9.5 期末失败和 fallback

当发现新周期时：

1. 从旧周期最后一条成功快照取得 `authoritative_cycle_end`；
2. 若旧周期存在 `collected_at <= authoritative_cycle_end` 的 pre-reset，选它为最终记录；
3. 否则选择 `collected_at <= authoritative_cycle_end` 的最后一条 periodic；
4. 将选中行的规范化 `billing_cycle_end` 同步为 authoritative end；原采集时 end 保留在 raw payload，不篡改原始证据；
5. 使用 periodic 时标记 `final_source=periodic_fallback` 并发送数据质量告警；
6. 若没有任何符合时间边界的快照，记录 `missing_cycle_final`，不得生成 0% 记录。

fallback 是数据质量降级，不等同于真实期末快照。所有查询必须展示 `final_source`。

---

## 10. 并发、幂等与锁

### 10.1 进程级锁

复用 `cam.scheduler._try_lock` 的跨平台文件锁模式，新增任务级锁：

```text
USAGE_PERIODIC_LOCK_FILE=data/cam_usage_periodic.lock
USAGE_PRE_RESET_LOCK_FILE=data/cam_usage_pre_reset.lock
```

periodic 和 pre-reset 可以使用不同任务锁，以免长时间 periodic 阻塞关键的 pre-reset。任务锁只防止同类批次重入，不负责账号互斥。

### 10.2 账号级互斥

新增独立的 `UsageAccountLock`，使用规范化 email 的 SHA-256 作为文件名，对“获取 token → API 请求 → 账期协调 → 写库”的完整账号操作加跨进程文件锁。

不得在外层复用 `TokenManager._lock_for()`：当前实现是不可重入的 `threading.Lock`，而 `get_valid_token()` 和 `force_relogin()` 内部会再次获取该锁，外层复用会死锁；仅依赖该锁又无法覆盖 API 和写库阶段。

锁文件统一放在 `USAGE_ACCOUNT_LOCK_DIR`，等待时间由 `USAGE_ACCOUNT_LOCK_TIMEOUT_SEC` 控制。pre-reset 获取账号锁失败时按其剩余安全窗口重试；periodic 获取失败按普通退避处理。

快照唯一键作为最终幂等兜底。锁超时时本轮账号标记 `account_lock_busy`，由下一轮重试。

### 10.3 并发建议

| 阶段 | 默认并发 | 说明 |
|---|---:|---|
| API 快照采集 | 10 | 低于现有明细 API 并发，避免批量认证压力 |
| Spending 页面兜底 | 2–4 | 复用现有单 Chromium 多 Context 限制 |
| 浏览器重登 | 使用现有配置 | 不在本需求中放大并发 |
| MySQL 写入 | 单账号短事务 | 不使用全批大事务 |

---

## 11. 错误处理、熔断与告警

### 11.1 错误分类

| 类型 | 示例 | 行为 |
|---|---|---|
| authentication | 401、403、token 无效 | 有界刷新；受批量熔断控制 |
| network | timeout、connection closed、5xx | 指数退避重试 |
| parse_contract | 核心字段缺失、类型变化 | 拒绝写入，保存脱敏诊断 |
| validation | 百分比越界、end <= start | 拒绝写入 |
| database | 连接失败、死锁、超时 | 数据库级重试，不重复浏览器登录 |
| account_mapping | MySQL 有账号但本地无凭据 | `not_collectable` |
| scheduler_late | 已越过 reset 且无 pre-reset | 等待周期切换后 fallback |

### 11.2 批量认证熔断

现有 `fetcher.py` 会在单账号接口返回 401/403 时强制重登。对 384 个账号批量运行时，若 Cursor 认证系统整体异常，逐账号清 profile 会放大故障。

新增进程级共享认证熔断器，同时服务 periodic 和 pre-reset：

```text
USAGE_AUTH_BREAKER_MIN_SAMPLES=10
USAGE_AUTH_BREAKER_FAILURE_RATIO=0.30
USAGE_AUTH_BREAKER_COOLDOWN_MIN=30
USAGE_AUTH_BREAKER_WINDOW_SIZE=50
USAGE_AUTH_BREAKER_WINDOW_MIN=10
```

统计口径为“账号级采集终态”，每次账号采集最多贡献一个样本：

- auth success：使用缓存 token、refresh 或登录后完成核心 API；
- auth failure：最终因 401/403、refresh 认证拒绝或登录页认证链路失败而结束；
- 网络、解析、数据库失败不进入认证比例分母；
- 同一账号内部重试次数不重复计样本。

计数器由进程内互斥锁保护，periodic 和 pre-reset 原子共享。只保留“最近 50 个账号终态或最近 10 分钟（取较小集合）”的滑动窗口，每次写入样本前清理过期项，避免长期成功历史稀释突发故障。当有效窗口样本数达到 10 且 auth failure 比例达到 30%：

1. 停止为后续账号触发强制重登；
2. 保留已有 token/profile；
3. 当前批次标记 `auth_circuit_open`；
4. 发送一次聚合告警，不发送 384 条重复告警；
5. 记录 `opened_at`，cooldown 从打开时刻起算；
6. cooldown 到期后进入 half-open，只允许一个账号执行 refresh/登录探测；
7. half-open 成功则清空窗口并关闭 breaker，失败则重新打开并重置 `opened_at`。

breaker 打开时，已有且仍有效的缓存 token 可以继续调用 API；禁止 refresh 和浏览器重登。breaker 生命周期是当前服务进程级，进程重启后重新统计，但启动后的 periodic 仍受持久失败退避约束，避免立即形成重登风暴。

该设计不能直接复用当前 `fetch_one` 的黑盒行为，因为它会吞掉 typed error，并可能在内部多次强制重登。实现时需要：

1. 为 `TokenManager.get_valid_token()` 增加 `auth_policy` 或 `allow_browser_login` 参数；
2. 在进入 refresh/browser login 前调用共享 breaker；
3. 为 `force_relogin()` 增加同样的 guard；
4. 新 collector 直接调用 `CursorClient`，将 401/403、网络、解析失败保留为 typed result；
5. 每个已提交 worker 在开始、刷新前和重登前检查共享 breaker；
6. breaker 打开后停止提交新 worker，并让尚未进入认证动作的 worker快速退出；
7. 已经开始的少量浏览器登录允许有界完成，最大数量受采集并发限制。

这要求对 `token_manager.py` 做向后兼容扩展，但不能改变现有调用方默认行为。

### 11.3 数据校验

成功快照至少满足：

```text
email 非空且规范化
billing_cycle_start 非空
billing_cycle_end 非空
billing_cycle_end > billing_cycle_start
total_used_pct 非空且 0 <= value <= 100
snapshot_type 合法
collected_at 位于合理时间范围
raw_payload 不含凭据
```

`plan_tier=unknown` 可以写入，因为用量数据仍有价值，但必须记录解析警告并进入数据质量统计。

### 11.4 告警分级

| 等级 | 场景 |
|---|---|
| info | periodic 批次完成、账期正常切换 |
| warning | 单账号采集失败、使用 periodic fallback、套餐未知 |
| error | pre-reset 安全窗口即将结束仍失败、数据库不可用 |
| critical | 批量认证熔断、连续多轮无任何成功快照 |

pre-reset 告警至少包含 email、cycle_end、当前时间、剩余窗口、失败分类和下一次重试时间。

---

## 12. 浪费等级设计

### 12.1 计算范围

基础计算只使用：

```text
is_cycle_final = 1
```

当前未结束账期、失败记录、UNKNOWN 和非最终 periodic 均不参与连续低用量次数。在计算等级前还必须验证：最新已知的已结束周期已经存在 final；若存在已结束但未 final 的周期，直接输出 UNKNOWN，不得回退使用更早 final。

### 12.2 默认阈值

```text
USAGE_LOW_THRESHOLD_PCT=30
```

阈值必须可配置。若未来不同套餐需要不同阈值，可将应用配置扩展为 `pro=30,pro_plus=30,ultra=30`，一期不新增配置表。

### 12.3 等级

| 等级 | 规则 | 业务含义 |
|---|---|---|
| `UNKNOWN` | 没有完整账期或最终记录缺失 | 数据不足，不评价 |
| `L0` | 最近完整账期使用率不低于阈值 | 当前无低用量信号 |
| `L1` | 最近连续 1 个完整账期低于阈值 | 单月偏低，观察 |
| `L2` | 最近连续 2 个完整账期低于阈值 | 持续偏低，建议复核 |
| `L3` | 最近连续 3 个及以上完整账期低于阈值 | 长期偏低，优先优化套餐 |

默认按“当前档位最近一个连续片段中的连续完整账期”计算。升级或降级后，当前档位的连续次数重新开始；即使以后又回到旧档位，也不能把旧档位的两段历史拼接。

具体算法：

1. 从该账号最新成功快照取得 `current_plan_tier`；
2. 若 `current_plan_tier='unknown'`，直接输出 `waste_level=UNKNOWN`，不得用历史 unknown 记录计算 L0–L3；
3. 若当前档位尚无完整 final，输出 UNKNOWN；
4. 按 `billing_cycle_end DESC` 读取 final，并从最新一条向前遍历；
5. 遇到档位变化立即停止，保证只取当前连续档位段；
6. 相邻周期 start/end 超过连续性容差，或中间存在已结束但未 final 的已知周期，输出 UNKNOWN；
7. 在连续档位段内，遇到第一条不低于阈值的记录停止累计；
8. 将累计数映射为 L0/L1/L2/L3。

由于只有快照表，若系统完整停机并错过整个账期，且没有该账期的任何快照，无法百分之百证明该周期存在。通过每日采集、陈旧告警及 `USAGE_CYCLE_CONTINUITY_TOLERANCE_HOURS` 降低此风险。

### 12.4 推荐输出字段

```text
email
applicant
department
current_plan_tier
latest_cycle_start
latest_cycle_end
latest_final_used_pct
latest_final_source
low_usage_streak
waste_level
data_quality_status
```

### 12.5 参考查询逻辑

查询分两步进行：

1. SQL 为每个 email 取最新成功快照、全部已知周期和按结束时间倒序的 final；
2. 后端纯函数按上一节算法划分“当前连续档位段”和“连续低用量段”。

不使用按 `(email, plan_tier)` 独立分区后直接关联账号的写法，因为一个账号会返回多个历史档位，而且 Pro→Ultra→Pro 会把两段 Pro 错误拼接。连续低用量也不能简单 `SUM(is_low)`。

---

## 13. 三表分析关系

### 13.1 订阅档位与浪费

```text
cursor_accounts
  LEFT JOIN cursor_usage_snapshot(final)
    ON normalized email
```

用于回答：

- 谁当前是 Pro、Pro+、Ultra；
- 哪些账号最近一个完整账期偏低；
- 哪些账号连续两个月或三个月以上偏低。

### 13.2 成本与退款

```text
cursor_accounts
  LEFT JOIN cursor_billing_ledger_summary
    ON normalized email
```

用于回答自然月净支出和退款。

### 13.3 综合展示

综合结果可以按 email 并列展示：

1. 最近完整滚动账期的档位和使用率；
2. 指定自然月的 amount、refund、net spend；
3. 人员和部门。

禁止仅用 `DATE_FORMAT(billing_cycle_start, '%Y-%m') = billing_month` 后把结果称为“该账期精确成本”。若业务接受近似展示，列名必须明确为“账期开始月净支出（自然月口径）”。

---

## 14. 配置设计

建议新增：

```ini
# Cursor 用量快照
USAGE_SNAPSHOT_ENABLE=true
USAGE_PERIODIC_INTERVAL_HOURS=24
USAGE_SNAPSHOT_CONCURRENCY=10
USAGE_BOOTSTRAP_STALE_HOURS=36
USAGE_PERIODIC_RETRY_MINUTES=30
USAGE_PERIODIC_MAX_ATTEMPTS_PER_SLOT=3

# reset 前期末采集
USAGE_PRE_RESET_SCAN_INTERVAL_MIN=15
USAGE_PRE_RESET_WINDOW_START_MIN=360
USAGE_PRE_RESET_TARGET_OFFSET_MIN=180
USAGE_PRE_RESET_WINDOW_END_MIN=30

# 低用量等级
USAGE_LOW_THRESHOLD_PCT=30
USAGE_CYCLE_CONTINUITY_TOLERANCE_HOURS=48

# 锁
USAGE_PERIODIC_LOCK_FILE=data/cam_usage_periodic.lock
USAGE_PRE_RESET_LOCK_FILE=data/cam_usage_pre_reset.lock
USAGE_ACCOUNT_LOCK_DIR=data/usage-account-locks
USAGE_ACCOUNT_LOCK_TIMEOUT_SEC=5

# 认证熔断
USAGE_AUTH_BREAKER_MIN_SAMPLES=10
USAGE_AUTH_BREAKER_FAILURE_RATIO=0.30
USAGE_AUTH_BREAKER_COOLDOWN_MIN=30
USAGE_AUTH_BREAKER_WINDOW_SIZE=50
USAGE_AUTH_BREAKER_WINDOW_MIN=10
```

MySQL 连接复用现有 `LEDGER_DB_*` 配置，不新增第二套账号密码。代码和 `.env.example` 不得提供真实密码默认值；`LEDGER_DB_PASSWORD` 为空时，在启用快照或 Ledger 任务的启动预检阶段直接报配置错误。

配置校验：

```text
window_start > target_offset > window_end >= 0
scan_interval > 0
periodic_interval > 0
bootstrap_stale_hours >= periodic_interval
periodic_retry_minutes > 0
periodic_max_attempts_per_slot >= 1
0 <= low_threshold <= 100
cycle_continuity_tolerance_hours >= 0
0 < failure_ratio <= 1
breaker_window_size >= breaker_min_samples
breaker_window_minutes > 0
concurrency >= 1
account_lock_timeout_sec >= 0
```

---

## 15. 预期代码边界

本节只定义模块边界，不是实施步骤。

### 15.1 建议新增

| 文件 | 职责 |
|---|---|
| `cam/usage_snapshot_models.py` | 快照、采集结果、账号映射结果领域对象 |
| `cam/usage_snapshot_parser.py` | API 原始字段解析、套餐规范化、校验和脱敏 |
| `cam/usage_snapshot_store.py` | MySQL DDL、写入、查询、账期最终记录事务 |
| `cam/usage_snapshot_locks.py` | 基于 email 哈希的跨进程文件锁，不复用 TokenManager 内部锁 |
| `cam/usage_snapshot_refresh.py` | periodic/pre-reset 编排、重试、熔断、运行日志 |
| `tests/test_usage_snapshot_parser.py` | 真实结构 fixture 的解析和校验 |
| `tests/test_usage_snapshot_store.py` | 唯一键、条件 upsert、finalization |
| `tests/test_usage_snapshot_refresh.py` | 调度候选、fallback、熔断 |
| `tests/test_usage_waste_level.py` | L0/L1/L2/L3/UNKNOWN 连续性 |

### 15.2 建议修改

| 文件 | 修改范围 |
|---|---|
| `cam/config.py` | 新增并校验用量配置 |
| `.env.example` | 增加无敏感值的配置说明 |
| `cam/scheduler.py` | 接入 periodic 和 pre-reset 扫描 |
| `cam/cli.py` | 增加手工运行和指定 email 的运维命令 |
| `cam/api_client.py` | 仅在需要时补充接口元数据，不改变现有接口语义 |
| `cam/token_manager.py` | 向后兼容增加 auth policy，在 refresh/重登前检查共享熔断器 |
| `cam/sync_log_store.py` | 增加账号+slot 尝试次数、最后失败时间和成功完成状态查询，不能复用仅判断 run 存在的接口 |
| `README.md` | 增加命令、调度和数据表说明 |

### 15.3 明确不修改

```text
cam/static/index.html
现有 Web UI 路由和页面
现有 Billing Ledger 退款计算
现有发票 PDF 下载流程
```

---

## 16. CLI 与运维入口

一期建议至少提供：

```bash
# 手工执行全量 periodic
python -m cam usage-snapshot --all --type periodic

# 指定账号诊断
python -m cam usage-snapshot --email user@example.com --type periodic

# 手工执行当前已到期的 pre-reset
python -m cam usage-pre-reset-due

# 只查看候选，不采集
python -m cam usage-pre-reset-due --dry-run

# 重算/修复上一账期最终记录
python -m cam usage-finalize --email user@example.com --cycle-start ...
```

手工命令与调度任务必须走同一 service 和 store，不能各自实现一套写入逻辑。

---

## 17. 测试设计

当前仓库测试基线为 24 个 unittest 文件、117 个用例；探索时存在 6 个既存失败/错误，主要位于前端结构、账单页 mock 和 StarRocks 连接池 mock。实施前必须在当前分支重新执行完整测试并保存基线，区分既存失败与本需求新增回归。不得把“新增用量测试通过”表述为“全仓测试全部通过”。

### 17.1 解析测试

1. 正常 API payload；
2. 0% 合法值；
3. 100%；
4. 百分比越界；
5. 秒和毫秒时间戳 fixture；
6. 缺失 cycle start/end；
7. Pro、Pro+、Ultra、未知新套餐；
8. raw payload 脱敏；
9. parser version 保留。

### 17.2 存储测试

1. 同一 periodic slot 重跑不新增；
2. 下一 periodic slot 正常新增；
3. 同账期 pre-reset 只保留一条；
4. 更早结果不能覆盖更晚结果；
5. 更晚成功结果可以覆盖；
6. finalization 优先 pre-reset；
7. 无 pre-reset 时选择 reset 前最后 periodic；
8. 重复 finalization 幂等；
9. 两个账号互不影响；
10. email 大小写和空格规范化；
11. 同 start 的 end 修正不产生新账期或第二条 pre-reset；
12. 新周期写入后拒绝迟到旧周期普通写入；
13. finalization 与迟到写入双事务并发只有一种合法结果；
14. `is_cycle_final/final_source/finalized_at` 状态组合一致；
15. 死锁或锁超时按数据库策略有界重试。

### 17.3 调度测试

1. 未进入窗口不调度；
2. 到达目标偏移时调度；
3. 失败后窗口内重试；
4. 窗口外不反复尝试；
5. 服务停机恢复后补抓；
6. 不同账号 cycle_end 不同，候选不同；
7. periodic 与 pre-reset 同 email 互斥；
8. 账号无本地登录资料时 `not_collectable`；
9. 批量认证失败达到阈值后熔断；
10. 熔断不清空剩余账号 profile；
11. 当前 periodic slot 已存在时常驻循环不重复采集；
12. 服务重启只补当前 slot，不补造过往 periodic；
13. 到达 `cycle_end - window_end` 后不再发起 pre-reset；
14. periodic 与 pre-reset 在两个进程中仍按 email 互斥；
15. 外层账号锁不复用 TokenManager 的不可重入锁，调用不死锁；
16. 人为阻塞 periodic worker 时，pre-reset 扫描仍按独立时钟触发；
17. periodic 排队期间进入 pre-reset 窗口时，worker 获取锁前重新检查并跳过；
18. 服务关闭时优先等待 pre-reset，并取消未开始 periodic。

### 17.4 账期和等级测试

1. 正常续期；
2. 升级同时改变账期；
3. 降级但账期未变；
4. 当前账期不参与浪费等级；
5. 一次低用量为 L1；
6. 连续两次为 L2；
7. 连续三次及以上为 L3；
8. 中间一次达标后连续次数归零；
9. 套餐变化后按新档位重新计数；
10. 缺失最终数据为 UNKNOWN，不是 L1；
11. Pro→Ultra→Pro 不拼接两段 Pro；
12. 当前新档位尚无完整账期时为 UNKNOWN；
13. 已结束但未 final 的最新已知周期使结果为 UNKNOWN；
14. 相邻周期超过连续性容差时为 UNKNOWN；
15. 当前套餐为 unknown 时直接为 UNKNOWN，不计算 L0–L3。

### 17.5 集成验证

选择至少以下测试账号：

1. Pro；
2. Pro+；
3. Ultra 或更高档；
4. 即将 reset；
5. 有升级或降级历史；
6. 当前无付费套餐。

对照 Cursor 页面确认套餐、账期和百分比。测试环境可缩短调度窗口，但生产默认配置不可因测试而改变。

### 17.6 真实 MySQL 兼容与并发测试

不能只使用 SQLite 或 mock 验证存储语义。部署目标版本必须执行集成测试；若生产版本尚未确认，至少分别验证 MySQL 5.7 和 8.x：

1. `JSON`、`DATETIME(3)` 和目标 collation；
2. CHECK 在对应版本的执行或忽略行为；
3. pre-reset 显式事务比较不依赖 `VALUES()` 方言；
4. `SELECT ... FOR UPDATE` 对唯一索引前缀的范围锁；
5. 两连接同时 finalization；
6. finalization 与迟到 insert/update 竞争；
7. 死锁检测和重试；
8. 连接中断后的事务回滚；
9. 同 start 下 end 修正；
10. 多进程文件锁在 Windows 生产部署方式下真实生效；
11. breaker 的并发样本计数原子；
12. open→half-open→closed/re-open 生命周期；
13. breaker 打开时允许有效缓存 token、禁止 refresh 和重登；
14. 长期大量成功后突发连续认证失败仍能在滑动窗口内打开 breaker；
15. 超过时间或数量窗口的样本会被清理。

---

## 18. 验收标准

| 编号 | 验收条件 |
|---|---|
| AC1 | 监控账号覆盖 `cursor_accounts`，缺凭据账号有明确结果 |
| AC2 | 单次 periodic 能为成功账号写入合法快照 |
| AC3 | 同一账期可保存多条 periodic |
| AC4 | 同一账期最多保留一条 pre-reset 业务记录 |
| AC5 | 不同 cycle_end 的账号在不同时间进入候选 |
| AC6 | reset 前任务失败会在安全窗口内重试 |
| AC7 | 发现新账期后旧账期恰有一条最终记录，或明确记录缺失 |
| AC8 | fallback 可追溯，不能伪装为 pre-reset |
| AC9 | 套餐档位来自明确名称，不由账单金额推断 |
| AC10 | L0/L1/L2/L3/UNKNOWN 结果符合连续完整账期规则 |
| AC11 | 退款和净支出继续使用现有 Ledger 结果 |
| AC12 | 批量认证故障触发熔断，不产生大规模 profile 清理 |
| AC13 | raw payload 不含 token、Cookie、IMAP 密码 |
| AC14 | 一期没有新增或修改前端页面功能 |
| AC15 | 同 start 的 end 修正不触发结算，也不产生第二条 pre-reset |
| AC16 | periodic 和 pre-reset 跨进程执行时，同一 email 不并发 |
| AC17 | 当前 periodic slot 已存在时不重复调用 Cursor |
| AC18 | 最近已结束周期缺少 final 时浪费等级为 UNKNOWN |

---

## 19. 上线与回滚

### 19.1 分阶段上线

1. 先建表并关闭调度；
2. 对 3–5 个测试账号手工 periodic；
3. 校验 API fixture、字段单位和页面显示；
4. 开启全量 periodic，观察至少 2 天；
5. 对即将 reset 的少量账号开启 pre-reset；
6. 验证账期切换和 finalization；
7. 全量开启；
8. 积累两个完整账期后正式使用 L2/L3 结论。

在历史数据不足时，系统只能输出 UNKNOWN 或 L1，不能提前声称“连续两个月低用量”。

### 19.2 回滚

1. 设置 `USAGE_SNAPSHOT_ENABLE=false`；
2. 停止新增采集，不影响现有 Token、账单和 BI 同步；
3. 保留快照表供审计；
4. 回滚代码不删除表、不删除历史数据；
5. 若解析规则错误，使用 `parser_version` 定位受影响记录，再制定修复脚本。

---

## 20. 数据保留与容量

按 384 个账号、每天一条 periodic 估算：

```text
384 × 365 ≈ 140,160 条/年
```

pre-reset 每账号每月约一条，约 4,608 条/年。记录量本身较小，主要空间来自 `raw_payload`。

一期建议：

- periodic 至少保留 24 个月；
- `is_cycle_final=1` 记录长期保留；
- 删除策略后续根据真实 JSON 平均大小制定；
- 清理时绝不能删除被标记为最终记录的行。

---

## 21. 风险与决策

| 风险 | 影响 | 决策 |
|---|---|---|
| Cursor 内部 API 字段变化 | 解析失败或错误百分比 | fixture + parser_version + 核心校验，拒绝写假数据 |
| 认证系统整体故障 | 大量重登、profile 被清 | periodic/pre-reset 共享的进程级认证熔断 |
| 调度器在 reset 前停机 | 缺少 pre-reset | 安全窗口补抓 + periodic fallback |
| 页面 DOM 改版 | 套餐兜底失效 | API 为主，页面只低频兜底 |
| collation 不一致 | join 慢或报错 | 建表前读取现网实际值并保持一致 |
| MySQL 版本不同 | CHECK/collation/UPSERT 语法不兼容 | 部署前版本检查，Python 层始终校验 |
| 账单月与滚动账期混淆 | 成本分析结论错误 | 并列展示并标明粒度，不做伪精确归因 |
| 订阅变化发生在两次采集之间 | 事件时间不精确 | 只声明首次检测时间 |
| 仅一张新表 | 无持久任务和独立事件实体 | 使用扫描调度和快照差分，满足一期约束 |

---

## 22. 后续演进

以下能力不属于一期：

1. 用量与浪费查询页面；
2. 飞书自动推送降档建议；
3. `cursor_subscription_event` 精确事件表；
4. 持久化动态调度任务和多实例抢占；
5. 按套餐配置不同阈值的管理表；
6. 发票明细级滚动账期成本归因；
7. 自动执行订阅降级或取消。

只有在一期数据稳定、业务口径验证后再进入后续阶段。
