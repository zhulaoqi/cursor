# Implementation Plan: cursor-account-automation

## Overview

按模块逐步实现 Cursor 账号自动化注册工具，每个任务对应一个独立模块，最终在 `main.py` 中完成流程编排与串联。测试任务紧跟对应实现任务，确保尽早发现问题。

## Tasks

- [x] 1. 初始化项目结构与依赖配置
  - 创建项目根目录结构，包含所有模块占位文件
  - 创建 `requirements.txt`，列出依赖：`playwright`, `python-dotenv`, `hypothesis`, `pytest`, `pytest-mock`
  - 创建 `.env.example`，包含所有配置项占位符
  - 创建 `tests/` 目录及 `tests/conftest.py`（共享 fixtures）
  - _Requirements: 1.4, 8.1, 8.3, 8.4_

- [x] 2. 实现 `config.py`：配置加载与校验
  - [x] 2.1 实现 `Config` dataclass 和 `load_config()` 函数
    - 使用 `python-dotenv` 加载 `.env` 文件
    - 校验所有必填字段，缺失时抛出 `ConfigError` 并在错误信息中注明缺失字段名
    - 为 `IMAP_HOST`、`IMAP_PORT`、`PLAN_NAME` 设置默认值
    - _Requirements: 1.2, 1.3, 1.4, 4.8_

  - [ ]* 2.2 为 `load_config()` 编写属性测试（Property 1）
    - **Property 1: 配置加载完整性**
    - **Validates: Requirements 1.2, 1.4**

  - [ ]* 2.3 为缺失配置项报错编写属性测试（Property 2）
    - **Property 2: 缺失配置项报错**
    - **Validates: Requirements 1.3**

- [x] 3. 实现 `utils.py`：工具函数
  - [x] 3.1 实现 `step_logger` 装饰器
    - 函数执行前输出 `[STEP N] <描述> ...`，成功后追加 `OK`，异常时追加 `FAILED`
    - _Requirements: 7.3, 7.4_

  - [x] 3.2 实现 `retry` 装饰器
    - 支持 `max_attempts`、`delay_sec`、`exceptions` 参数
    - _Requirements: 3.2_

  - [ ]* 3.3 为 `step_logger` 编写属性测试（Property 8）
    - **Property 8: 步骤日志格式**
    - **Validates: Requirements 7.3, 7.4**

- [x] 4. 实现 `output.py`：结果输出
  - [x] 4.1 实现 `print_account()`、`save_account()`、`mask_card_number()`
    - `print_account()` 以结构化格式输出邮箱和密码到控制台
    - `save_account()` 以追加模式写入 `accounts.txt`，格式 `email:password`
    - `mask_card_number()` 仅保留后四位，其余替换为 `*`
    - _Requirements: 5.7, 7.1, 7.2_

  - [ ]* 4.2 为 `print_account()` 编写属性测试（Property 6）
    - **Property 6: 账号信息控制台输出完整性**
    - **Validates: Requirements 7.1**

  - [ ]* 4.3 为 `save_account()` 编写属性测试（Property 7）
    - **Property 7: 账号信息文件持久化**
    - **Validates: Requirements 7.2**

  - [ ]* 4.4 为 `mask_card_number()` 编写属性测试（Property 5）
    - **Property 5: 信用卡号脱敏**
    - **Validates: Requirements 5.7**

- [x] 5. Checkpoint — 确保所有测试通过
  - 确保所有测试通过，如有问题请向用户确认。

- [x] 6. 实现 `browser.py`：Playwright 浏览器管理
  - [x] 6.1 实现 `create_browser()` 和 `close_browser()`
    - 启动有头 Chromium，配置自定义 user-agent
    - 支持可选代理参数（`proxy` 字段）
    - 返回 `(browser, context, page)` 三元组
    - _Requirements: 2.1, 3.4_

- [x] 7. 实现 `registration.py`：注册流程
  - [x] 7.1 实现 `generate_password()`
    - 生成长度 ≥ 12 的随机密码，包含大小写字母、数字、特殊字符各至少一个
    - _Requirements: 2.4_

  - [ ]* 7.2 为 `generate_password()` 编写属性测试（Property 3）
    - **Property 3: 随机密码强度**
    - **Validates: Requirements 2.4**

  - [x] 7.3 实现 `navigate_to_signup()` 和 `fill_signup_form()`
    - 导航到 `https://cursor.com/signup`
    - 填写邮箱、密码字段并提交表单
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 7.4 实现 `wait_for_cloudflare()`
    - 通过 title 或特征元素检测 Cloudflare 验证页面
    - 等待最多 60 秒，超时抛出 `CloudflareTimeoutError`
    - _Requirements: 3.1, 3.2, 3.3_

  - [ ]* 7.5 为 `wait_for_cloudflare()` 编写单元测试
    - mock 页面始终显示 Cloudflare 挑战，验证 60s 后抛出 `CloudflareTimeoutError`
    - _Requirements: 3.3_

- [x] 8. 实现 `email_client.py`：IMAP 验证码获取
  - [x] 8.1 实现 `connect_imap()`
    - 使用 `imaplib.IMAP4_SSL` 连接 `imap.feishu.cn:993`
    - 认证失败时抛出 `IMAPAuthError`
    - _Requirements: 4.1, 4.2_

  - [ ]* 8.2 为 `connect_imap()` 编写单元测试
    - 验证使用 SSL 且默认端口为 993
    - mock IMAP 返回认证失败，验证抛出 `IMAPAuthError`
    - _Requirements: 4.1, 4.2_

  - [x] 8.3 实现 `extract_code_from_email()`
    - 使用正则 `r'\b(\d{6})\b'` 从邮件正文提取 6 位验证码
    - _Requirements: 4.5_

  - [ ]* 8.4 为 `extract_code_from_email()` 编写属性测试（Property 4）
    - **Property 4: 验证码提取正确性**
    - **Validates: Requirements 4.5**

  - [x] 8.5 实现 `poll_for_verification_code()`
    - 每 5 秒轮询一次 INBOX，过滤来自 `no-reply@cursor.com` 的邮件
    - 120 秒内未找到时抛出 `VerificationCodeTimeoutError`
    - _Requirements: 4.3, 4.4, 4.6_

  - [ ]* 8.6 为 `poll_for_verification_code()` 编写单元测试
    - mock IMAP 始终返回空收件箱，验证超时后抛出 `VerificationCodeTimeoutError`
    - _Requirements: 4.6_

- [x] 9. 实现 `payment.py`：信用卡绑定
  - [x] 9.1 实现 `navigate_to_billing()` 和 `fill_stripe_iframe()`
    - 导航到信用卡绑定页面
    - 使用 `page.frame_locator()` 穿透 Stripe 嵌套 iframe，填写卡号、有效期、CVV
    - _Requirements: 5.1, 5.3_

  - [x] 9.2 实现 `fill_billing_fields()`、`submit_payment()`、`verify_payment_success()`
    - 填写 iframe 外部的持卡人姓名和账单邮编
    - 点击提交按钮，等待页面响应
    - 检查成功跳转或成功提示元素，返回布尔值
    - _Requirements: 5.2, 5.3, 5.4, 5.5_

  - [ ]* 9.3 为支付失败场景编写单元测试
    - mock `verify_payment_success()` 返回 False，验证抛出 `PaymentError`
    - _Requirements: 5.6_

- [x] 10. 实现 `subscription.py`：付费计划订阅
  - [x] 10.1 实现 `navigate_to_plans()`、`select_plan()`、`confirm_subscription()`
    - 导航到订阅计划页面
    - 找到 `plan_name` 对应的订阅按钮并点击
    - 自动处理确认弹窗或付款确认页面
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [x] 10.2 实现 `verify_subscription_success()`
    - 验证订阅状态页面显示正确的计划名称与有效期
    - _Requirements: 6.5, 6.7_

  - [ ]* 10.3 为订阅失败场景编写单元测试
    - mock `verify_subscription_success()` 返回 False，验证抛出 `SubscriptionError`
    - _Requirements: 6.6_

- [x] 11. 实现 `main.py`：流程编排与 CLI 入口
  - [x] 11.1 实现 CLI 解析与配置加载
    - 支持 `python main.py` 启动
    - 调用 `load_config()`，捕获 `ConfigError` 并输出错误信息后以非零状态码退出
    - _Requirements: 1.1, 1.3_

  - [x] 11.2 串联完整自动化流程
    - 按顺序调用：`create_browser` → `navigate_to_signup` → `fill_signup_form` → `wait_for_cloudflare` → `connect_imap` → `poll_for_verification_code` → 填入验证码 → `navigate_to_billing` → `fill_stripe_iframe` → `fill_billing_fields` → `submit_payment` → `navigate_to_plans` → `select_plan` → `confirm_subscription` → `print_account` → `save_account`
    - 用 `step_logger` 装饰各阶段调用，统一格式化进度日志
    - 在顶层捕获所有未预期异常，输出 traceback 并以非零状态码退出
    - _Requirements: 2.1, 4.7, 7.3, 7.4_

- [x] 12. 创建 `README.md`
  - 说明所有必要配置项及其格式
  - 列出飞书 IMAP 配置、信用卡信息、订阅计划等用户需自行提供的信息
  - 包含安装命令：`pip install -r requirements.txt` 和 `playwright install`
  - 包含 `.env.example` 示例内容
  - _Requirements: 8.1, 8.2, 8.3, 8.4_

- [x] 13. Final Checkpoint — 确保所有测试通过
  - 确保所有测试通过，如有问题请向用户确认。

## Notes

- 标有 `*` 的子任务为可选测试任务，可跳过以加快 MVP 进度
- 每个任务均引用具体需求条款以保证可追溯性
- 属性测试使用 `hypothesis`，每个属性对应一个独立子任务
- 单元测试使用 `pytest` + `pytest-mock`，覆盖错误路径和边界条件
- Playwright 相关模块（`browser.py`、`registration.py`、`payment.py`、`subscription.py`）的集成测试需要真实浏览器环境，建议手动验证
