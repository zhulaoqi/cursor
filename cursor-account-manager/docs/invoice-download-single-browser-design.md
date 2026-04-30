# 账单下载并发改造设计（单 Chromium + 多 Context）

本文档定义账单下载链路从“多浏览器进程并发”迁移到“单浏览器进程多上下文并发”的技术方案与实施计划，目标是根因解决并发场景下页面空白、月份控件丢失、账号结果不稳定的问题。

## 1. 背景与问题

当前实现（`cam/exporter.py`）采用：

- `ThreadPoolExecutor(max_workers=N)`；
- 每个账号在线程内 `asyncio.run(...)`；
- 每个账号独立 `async_playwright() + chromium.launch()`。

在 6~8 账号并发时，Windows 环境出现稳定失败：

- 部分账号日志出现“未找到精确 `YYYY年M月` 过滤器按钮”；
- 诊断日志显示“当前可见 button 文本为空数组”；
- 说明并非仅过滤器定位失败，而是页面 React 未完成 hydrate（空白骨架态）。

根因是物理资源竞争：多个 Chromium 进程并发启动和渲染，导致 renderer 被饿死，属于架构级问题，非重试可彻底修复。

## 2. 目标与非目标

### 2.1 目标

- 在并发下载账单时，消除多 Chromium 进程竞争；
- 保持账号隔离，不发生 cookie/session 串号；
- 提升成功率与时延稳定性；
- 保持现有导出接口不变（调用方最小改动）。

### 2.2 非目标

- 不改登录模块（`cam/browser_login.py`）；
- 不改 token 获取链路（`token_manager.get_valid_token`）；
- 不新增外部依赖。

## 3. 方案概览

## 3.1 旧模型

- 每账号启动一个 Chromium 进程；
- 并发数上升时，进程级资源争抢严重。

## 3.2 新模型（目标模型）

- 全流程只 `launch` 一次 Chromium；
- 每账号分配一个独立 `BrowserContext`；
- 使用 `asyncio.Semaphore` 控制同时活跃 context 数；
  - 总并发：`INVOICE_DOWNLOAD_CONCURRENCY`
  - 页面活跃上限：`INVOICE_ACTIVE_CONTEXT_LIMIT`（<=0 表示不额外限流）
- 每个 context 注入该账号自己的 `WorkosCursorSessionToken`，再执行：
  - 账单列表抓取；
  - 指定月份过滤；
  - Stripe 页面 PDF 下载；
  - context 回收。

## 4. 账号隔离与安全性

`BrowserContext` 是 Playwright 的原生隔离单元，具备独立：

- Cookie jar；
- LocalStorage / SessionStorage / IndexedDB；
- Service Worker / Cache；
- 页面会话状态。

因此“单 Chromium + 多 Context”在账号隔离语义上与“多 Chromium 进程”等价，不会导致账号信息串用。

## 5. 对登录逻辑的影响评估（必须保证零影响）

本改造不触碰登录链路，边界如下：

- 登录模块：`cam/browser_login.py` 使用 `patchright.sync_api` + `launch_persistent_context`；
- 账单下载模块：`cam/exporter.py` 使用 `patchright.async_api`；
- 两者并发控制分离：
  - 登录：`BROWSER_LOGIN_CONCURRENCY`
  - 账单下载：`INVOICE_DOWNLOAD_CONCURRENCY`
- 唯一耦合点是 `token_manager.get_valid_token` 返回 token 字符串，此接口和语义不变。

结论：该改造对登录功能、验证码流程、PKCE、账号可用性管理均无行为影响。

## 6. 性能与并发策略

### 6.1 预期收益

- 启动成本：`N 次 launch -> 1 次 launch`；
- 渲染稳定性：降低 renderer 饥饿概率；
- 内存峰值显著下降；
- 成功率从“受机器状态影响明显”提升到“稳定可控”。

### 6.2 并发建议

- 默认建议：`INVOICE_DOWNLOAD_CONCURRENCY=4`；
- 常用稳定区间：`4~8`；
- 若机器高配可尝试提升到 `10~12`，但需观察 CPU 与失败率；
- 并发越大不一定越快，应以“成功率优先、吞吐次之”调参。
- 实现侧安全阈值改为配置：账单阶段
  `active_context_limit=min(INVOICE_DOWNLOAD_CONCURRENCY, snapshot_count, INVOICE_ACTIVE_CONTEXT_LIMIT)`
  （当 `INVOICE_ACTIVE_CONTEXT_LIMIT<=0` 时不额外限流），用于避免同机同刻渲染过多
  Cursor Billing SPA 导致页面空白/未 hydrate。

## 7. 代码改造点

## 7.1 `cam/exporter.py`

1. 重写 `_download_invoices_all(...)`：
   - 改为单 `asyncio.run(...)` 驱动；
   - 外层统一创建 `async_playwright` 与 `browser`；
   - `gather + semaphore` 调度多账号任务。

2. 账号处理逻辑拆分为异步函数（建议 `_download_one_account_with_context`）：
   - 创建 `browser.new_context(accept_downloads=True)`；
   - 注入账号 cookie；
   - 调用现有账单抓取 + PDF 下载流程；
   - 捕获异常并回传单账号空结果；
   - `finally` 保证 `context.close()`。

3. `_download_account_all_pdfs(...)` 支持复用外部 browser/context（或拆分为 context 版本），避免内部再次 launch。

## 7.2 配置与文档

- `cam/config.py`：
  - 保持 `INVOICE_DOWNLOAD_CONCURRENCY` 配置名；
  - 默认值建议从 `8` 调整为 `4`（如确认执行）。
- `.env.example`、`README.md`、`DESIGN.md`：
  - 更新并发语义为“活跃 context 数”。

## 8. 测试与验收

### 8.1 单元测试新增建议

- 仅 launch 一次 Chromium；
- 每账号 context 注入的 cookie 值正确；
- semaphore 生效（最大活跃 context 不超过配置）；
- 单账号失败不影响其他账号结果。

### 8.2 回归测试

- 现有测试全量通过；
- 核心场景人工验证：
  - 6 账号并发拉取 2026-01；
  - 输出中每账号账单数与单账号执行一致；
  - 无“页面空白导致月份控件消失”类错误。

## 9. 实施开发规划

按以下阶段推进（每阶段可独立验证）：

### 阶段 A：基线整理

- 移除临时补偿性逻辑（仅在确认进入根因改造后执行）；
- 保留可观测日志但降低噪声；
- 跑全量测试，建立基线。

### 阶段 B：核心并发模型切换

- 在 `exporter.py` 引入单浏览器多上下文并发实现；
- 保持 `export_per_account` 入参与返回结构不变；
- 跑单测 + 手工小样本验证（2~3 账号）。

### 阶段 C：配置与文档更新

- 更新默认并发建议与注释；
- 同步更新 `README.md` / `DESIGN.md` / 本文档；
- 说明与登录链路的隔离边界。

### 阶段 D：稳定性验证

- 6 账号并发实测（目标月份有账单）；
- 对比旧模型：成功率、总耗时、失败日志类型；
- 视结果微调并发默认值（4/6/8）。

## 10. 风险与回滚

- 风险：若站点行为变化导致 context 复用策略不稳定；
- 缓解：保留旧实现分支作为临时开关（仅调试时启用）；
- 回滚：可快速恢复到旧 `ThreadPool + multi-launch` 模式（不建议长期使用）。

---

维护约定：

- 该文档是账单下载并发模型的唯一设计来源；
- 并发相关改造（含临时补偿逻辑）必须先更新本文档再合入代码。
