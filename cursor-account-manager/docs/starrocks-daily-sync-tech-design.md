# Cursor Account Manager 每日明细落库（StarRocks）技术详细设计

## 1. 目标与范围

本设计文档仅覆盖“技术方案与架构设计”，不包含排期拆分与里程碑计划（见独立实施计划文档）。

目标：
- 将明细数据按每天 1 次刷新落入 StarRocks。
- 支撑动态账号池（当前约 344 个）每日稳定采集与入库。
- 提供可追溯（run_id）、可重跑（按日覆盖）、可观测（SQLite 日志 + Web 监控）的链路。

---

## 2. 现状与新增能力边界

现状（项目已具备）：
- CSV 账号批量拉取（`fetcher`，CSV 优先，API 降级）。
- FastAPI + Alpine Web 端运行状态展示。
- SQLite（`tokens.db`）用于 token、账号与审计日志。

本次新增：
- StarRocks ODS + DWD 明细层。
- 每日同步任务运行日志（SQLite 独立表）。
- Web 端新增“每日同步监控页”。
- 调度策略与稳定性机制。

---

## 3. 规模与SLA设计基线

## 3.1 数据规模估算（当前约 344 账号）

- 单账号日事件量：300 ~ 3,000 条（经验区间）。
- 日总量：10 万 ~ 100 万条。
- 年总量：3,650 万 ~ 3.65 亿条。

结论：采用 StarRocks 按天分区、哈希分桶、批量导入即可稳定承载。

## 3.2 SLA建议

- 调度时点：每日 01:30（UTC+8）。
- 单次任务时长目标：30~120 分钟。
- 账号维度成功率目标：>= 99.5%。

## 3.3 动态账号池规则（新增/删除/修改）

- 账号来源不固定，允许用户随时上传账号（新增、更新、停用）。
- 每次任务启动时，先从 SQLite 读取一次账号快照（`snapshot`），本次 run 仅处理该快照中的账号。
- 任务运行中若有新账号上传，不打断当前 run，默认下一次调度生效。
- 对“紧急新增账号”，提供手动触发 `sync-daily --biz-date <date> --emails ...` 补拉入口。
- 账号被停用后，不再进入后续 run；历史数据保留，不做物理删除。

---

## 4. 总体架构设计

## 4.1 数据流

1. 调度触发每日任务（含 run_id）。
2. 从 SQLite 读取启用账号列表并冻结为 run 快照。
3. 并发拉取账号 usage CSV 明细。
4. 解析与标准化（时间、token、费用、模型等）。
5. 批量写入 StarRocks ODS。
6. ODS -> DWD SQL 转换（按 `dt` 覆盖）。
7. 运行状态/阶段日志写入 SQLite。
8. Web 页面查询 SQLite 展示“今日同步状态”。

## 4.2 分层

- ODS：接收原始/半原始明细，保留可追溯字段。
- DWD：查询用事实明细表，业务去重、口径统一。
- OPS（SQLite）：任务运行态、阶段日志、账号失败明细。

---

## 5. StarRocks 表设计与DDL

> 已按需求包含以下属性：  
> `replication_num=2`、`in_memory=false`、`enable_persistent_index=false`、  
> `replicated_storage=true`、`storage_medium=SSD`、`compression=LZ4`

## 5.1 数据库

```sql
CREATE DATABASE IF NOT EXISTS cam_bi;
```

## 5.2 ODS表（每日入口）

```sql
CREATE TABLE IF NOT EXISTS cam_bi.ods_cursor_usage_events_di (
    dt                      DATE            NOT NULL COMMENT '业务日期(北京时间)',
    account_email           VARCHAR(128)    NOT NULL,
    event_time              DATETIME        NOT NULL COMMENT 'UTC时间',
    model_name              VARCHAR(128)    NULL,
    run_id                  VARCHAR(64)     NOT NULL COMMENT '任务批次ID',
    source_file             VARCHAR(255)    NULL COMMENT '来源CSV文件',
    event_time_bj           DATETIME        NOT NULL COMMENT '北京时间',
    request_id              VARCHAR(128)    NULL,
    project_name            VARCHAR(255)    NULL,
    message_role            VARCHAR(32)     NULL,

    input_tokens            BIGINT          NULL,
    output_tokens           BIGINT          NULL,
    cache_read_tokens       BIGINT          NULL,
    cache_write_tokens      BIGINT          NULL,
    total_tokens            BIGINT          NULL,

    cost_usd                DECIMAL(18,6)  NULL,
    billed_amount_usd       DECIMAL(18,6)  NULL,
    discount_percent        DECIMAL(8,4)   NULL,

    raw_event_json          JSON            NULL,
    ingest_time             DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
)
ENGINE=OLAP
DUPLICATE KEY(dt, account_email, event_time, model_name)
PARTITION BY RANGE(dt) ()
DISTRIBUTED BY HASH(account_email) BUCKETS 16
PROPERTIES (
  "dynamic_partition.enable" = "true",
  "dynamic_partition.time_unit" = "DAY",
  "dynamic_partition.start" = "-90",
  "dynamic_partition.end" = "7",
  "dynamic_partition.prefix" = "p",
  "dynamic_partition.buckets" = "16",
  "replication_num" = "2",
  "in_memory" = "false",
  "enable_persistent_index" = "false",
  "replicated_storage" = "true",
  "storage_medium" = "SSD",
  "compression" = "LZ4"
);
```

## 5.3 DWD表（查询事实层）

```sql
CREATE TABLE IF NOT EXISTS cam_bi.dwd_cursor_usage_detail_di (
    dt                      DATE            NOT NULL,
    account_email           VARCHAR(128)    NOT NULL,
    event_unique_key        VARCHAR(256)    NOT NULL COMMENT '幂等键(md5)',
    event_time              DATETIME        NOT NULL,
    event_time_bj           DATETIME        NOT NULL,
    request_id              VARCHAR(128)    NULL,
    model_name              VARCHAR(128)    NULL,
    project_name            VARCHAR(255)    NULL,

    input_tokens            BIGINT          NULL,
    output_tokens           BIGINT          NULL,
    cache_read_tokens       BIGINT          NULL,
    cache_write_tokens      BIGINT          NULL,
    total_tokens            BIGINT          NULL,

    cost_usd                DECIMAL(18,6)  NULL,
    billed_amount_usd       DECIMAL(18,6)  NULL,
    discount_percent        DECIMAL(8,4)   NULL,

    src_run_id              VARCHAR(64)     NOT NULL,
    etl_time                DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
)
ENGINE=OLAP
UNIQUE KEY(dt, account_email, event_unique_key)
PARTITION BY RANGE(dt) ()
DISTRIBUTED BY HASH(account_email) BUCKETS 16
PROPERTIES (
  "enable_unique_key_merge_on_write" = "true",
  "dynamic_partition.enable" = "true",
  "dynamic_partition.time_unit" = "DAY",
  "dynamic_partition.start" = "-365",
  "dynamic_partition.end" = "7",
  "dynamic_partition.prefix" = "p",
  "dynamic_partition.buckets" = "16",
  "replication_num" = "2",
  "in_memory" = "false",
  "enable_persistent_index" = "false",
  "replicated_storage" = "true",
  "storage_medium" = "SSD",
  "compression" = "LZ4"
);
```

> 注意：StarRocks 要求 Key 列必须是表定义最前面的连续列。若历史表是按旧字段顺序创建的，需要先重建表（或新表迁移）再执行装载。

## 5.4 每日装载SQL（按日覆盖）

```sql
DELETE FROM cam_bi.dwd_cursor_usage_detail_di
WHERE dt = '2026-05-11';

INSERT INTO cam_bi.dwd_cursor_usage_detail_di (
    dt, account_email, event_time, event_time_bj, event_unique_key, request_id, model_name, project_name,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, total_tokens,
    cost_usd, billed_amount_usd, discount_percent, src_run_id
)
SELECT
    dt,
    account_email,
    event_time,
    event_time_bj,
    md5(concat_ws('|',
        account_email,
        ifnull(request_id, ''),
        cast(event_time as string),
        ifnull(model_name, ''),
        cast(ifnull(total_tokens, 0) as string),
        cast(ifnull(cost_usd, 0) as string)
    )) AS event_unique_key,
    request_id,
    model_name,
    project_name,
    input_tokens,
    output_tokens,
    cache_read_tokens,
    cache_write_tokens,
    total_tokens,
    cost_usd,
    billed_amount_usd,
    discount_percent,
    run_id
FROM cam_bi.ods_cursor_usage_events_di
WHERE dt = '2026-05-11';
```

---

## 6. SQLite 运行日志设计

## 6.1 主任务表

```sql
CREATE TABLE IF NOT EXISTS sync_job_run (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT NOT NULL UNIQUE,
    biz_date                TEXT NOT NULL,
    trigger_type            TEXT NOT NULL,   -- scheduler/manual/retry
    status                  TEXT NOT NULL,   -- pending/running/success/partial_failed/failed
    started_at              INTEGER NOT NULL,
    ended_at                INTEGER,
    duration_sec            INTEGER,
    account_total           INTEGER DEFAULT 0,
    account_snapshot_total  INTEGER DEFAULT 0,
    new_account_count       INTEGER DEFAULT 0,
    account_success         INTEGER DEFAULT 0,
    account_failed          INTEGER DEFAULT 0,
    event_total             INTEGER DEFAULT 0,
    ods_rows                INTEGER DEFAULT 0,
    dwd_rows                INTEGER DEFAULT 0,
    error_summary           TEXT DEFAULT '',
    created_at              INTEGER NOT NULL,
    updated_at              INTEGER NOT NULL
);
```

## 6.2 账号级日志表

```sql
CREATE TABLE IF NOT EXISTS sync_job_account_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT NOT NULL,
    account_email           TEXT NOT NULL,
    account_source          TEXT DEFAULT '', -- upload/manual/import_api
    is_new_account          INTEGER DEFAULT 0,
    status                  TEXT NOT NULL,   -- success/failed/skipped
    started_at              INTEGER NOT NULL,
    ended_at                INTEGER,
    duration_sec            INTEGER,
    fetch_rows              INTEGER DEFAULT 0,
    load_rows               INTEGER DEFAULT 0,
    error_message           TEXT DEFAULT '',
    created_at              INTEGER NOT NULL
);
```

## 6.3 阶段日志表（开始/结束时间）

```sql
CREATE TABLE IF NOT EXISTS sync_job_stage_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT NOT NULL,
    stage                   TEXT NOT NULL,   -- init/fetch/parse/load_ods/load_dwd/finalize
    status                  TEXT NOT NULL,   -- start/success/failed
    message                 TEXT DEFAULT '',
    ts                      INTEGER NOT NULL
);
```

---

## 7. Web 页面技术设计（前端+后端）

## 7.1 后端API（FastAPI）

- `GET /api/sync/today`：今天最近一次 run 概览。
- `GET /api/sync/runs?limit=30`：近 N 次运行列表。
- `GET /api/sync/run/{run_id}`：单次运行详情（阶段日志+失败账号）。
- `POST /api/sync/run`：手动触发日任务。
- `POST /api/sync/retry/{run_id}`：重跑失败账号。

## 7.2 前端视图扩展（`cam/static/index.html`）

在现有 `view: 'list' | 'upload' | 'run'` 基础上新增：
- `view: 'sync'`

页面结构：
1. 今日状态卡：状态、开始、结束、总耗时、快照账号数、成功/失败账号数。
2. 阶段时间线：`init -> fetch -> parse -> load_ods -> load_dwd -> finalize`。
3. 失败账号表：错误原因、一键重试。
4. 新增账号列表：展示本次 run 中“新增账号”及来源。
5. 历史运行列表：可进入 run 详情。

## 7.3 UI/UX约束（落地标准）

- 状态语义色统一（绿/蓝/黄/红）。
- 时间统一展示北京时间。
- 操作控件触达面积 >= 44px。
- 错误文案支持折叠/展开。
- 状态区使用 `aria-live="polite"`。

---

## 8. 调度技术选型与稳定性设计

## 8.1 选型

推荐标准方案：
- 调度：Airflow（HA）。
- 执行：`python -m cam sync-daily`。
- 存储：StarRocks + SQLite。
- 告警：飞书/企业微信机器人。

MVP 方案（暂不引入 Airflow）：
- `systemd timer + flock + 自动重试 + 告警`。

## 8.2 稳定性机制（必须）

1. 幂等：按 `biz_date` 覆盖写入。
2. 账号级重试：指数退避（2/4/8 分钟）。
3. 断点补偿：失败账号可单独重跑。
4. 超时控制：单账号超时熔断（如 600 秒）。
5. 并发控制：建议 20~40。
6. 动态账号一致性：run 内使用账号快照，避免运行中账号集变化导致口径漂移。
7. 可观测：阶段耗时、行数核对、成功率告警。

---

## 9. 配置设计

敏感信息通过环境变量注入，不写入仓库明文：

```ini
BI_SYNC_DB_URL=jdbc:mysql://fe-c-211cbbee7a09d77e-internal.starrocks.aliyuncs.com:9030/dataeye_customer
BI_SYNC_DB_USERNAME=pro
BI_SYNC_DB_PASSWORD=******

BI_SYNC_ENABLE=true
BI_SYNC_CRON=30 1 * * *
BI_SYNC_BIZ_TZ=Asia/Shanghai
BI_SYNC_RETRY_TIMES=3
BI_SYNC_ACCOUNT_TIMEOUT_SEC=600
BI_SYNC_BATCH_SIZE=5000
```

告警机器人配置（用于任务失败/超时通知）：

```ini
ALERT_BOT_CLIENT_ID=cli_a933b8d6d7b81bc3
ALERT_BOT_SECRET=hDkboxuFdSm2uledMDCSJeffeC7I5UUR
ALERT_BOT_PROVIDER=feishu
ALERT_BOT_ENABLE=true
```

安全建议：
- 以上机器人密钥仅用于当前接入，建议上线后立即轮换。
- 生产环境统一使用环境变量或密钥管理服务，不在代码中硬编码。

---

## 10. 代码落地映射

建议新增：
- `cam/bi_sync.py`
- `cam/starrocks_loader.py`
- `cam/sync_log_store.py`
- `cam/scheduler.py`（可选）

建议扩展：
- `cam/cli.py`（`sync-daily`、`sync-retry`）
- `cam/web_server.py`（`/api/sync/*`）
- `cam/static/index.html`（`sync` 视图）
- `cam/config.py`（BI 同步配置）

