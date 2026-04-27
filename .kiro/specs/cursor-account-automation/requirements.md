# Requirements Document

## Introduction

Cursor 账号自动化注册工具，使用 Python + Playwright 实现全流程自动化：打开 Cursor 注册页面、创建账号、处理 Cloudflare 验证、获取邮箱验证码、绑定信用卡、订阅付费计划，最终在控制台输出创建好的账号信息。工具以命令行方式启动，面向自动化运维人员使用。

## Glossary

- **Tool**: 本自动化注册工具，基于 Python + Playwright 实现的命令行程序
- **Browser**: Playwright 控制的 Chromium/Firefox 浏览器实例
- **Cursor_Site**: Cursor 官方网站（cursor.com）的注册与订阅页面
- **Account**: 由工具创建的 Cursor 用户账号，包含邮箱和密码
- **Cloudflare**: Cursor 网站使用的 DDoS 防护与验证服务
- **Email_Provider**: 飞书（Feishu）企业邮箱，通过 IMAP 协议自动收取验证邮件，IMAP 服务器为 `imap.feishu.cn:993`（SSL）
- **IMAP_Client**: 工具内嵌的 IMAP 客户端模块，负责连接飞书邮箱并轮询收件箱
- **Verification_Code**: Cursor 发送到注册邮箱的数字验证码
- **Credit_Card**: 用于绑定 Cursor 付费计划的信用卡信息
- **Config**: 工具运行所需的配置文件（如 `.env` 或 `config.yaml`），包含邮箱、信用卡等敏感信息
- **CLI**: 命令行界面，工具的启动与交互方式

## Requirements

### Requirement 1: 命令行启动与配置加载

**User Story:** As a 自动化运维工程师, I want to 通过命令行启动工具并从配置文件加载参数, so that 我可以无需修改代码即可运行自动化流程。

#### Acceptance Criteria

1. THE Tool SHALL 支持通过命令行命令（如 `python main.py`）启动
2. WHEN 配置文件存在时, THE Tool SHALL 从配置文件中读取邮箱、信用卡、代理等参数
3. IF 必要配置项缺失, THEN THE Tool SHALL 在启动时输出明确的错误信息并退出，说明缺少哪个配置项
4. THE Tool SHALL 支持通过环境变量或 `.env` 文件传入敏感配置（邮箱密码、信用卡号等）

---

### Requirement 2: 浏览器自动化与页面导航

**User Story:** As a 自动化运维工程师, I want to 工具自动打开 Cursor 注册页面并完成页面交互, so that 整个注册流程无需人工干预。

#### Acceptance Criteria

1. WHEN 工具启动后, THE Browser SHALL 自动打开 Cursor_Site 的注册页面
2. THE Tool SHALL 使用 Playwright 控制 Browser 填写注册表单（邮箱、密码字段）
3. WHEN 注册表单填写完成后, THE Tool SHALL 自动提交表单
4. THE Tool SHALL 生成随机强密码用于账号注册（至少 12 位，包含大小写字母、数字和特殊字符）

---

### Requirement 3: Cloudflare 验证处理

**User Story:** As a 自动化运维工程师, I want to 工具能够处理 Cloudflare 验证挑战, so that 自动化流程不会因验证拦截而中断。

#### Acceptance Criteria

1. WHEN Browser 遇到 Cloudflare 验证页面时, THE Tool SHALL 检测到验证挑战的存在
2. WHEN Cloudflare 验证被检测到时, THE Tool SHALL 等待最多 60 秒以完成验证（支持自动或人工介入）
3. IF Cloudflare 验证在超时时间内未完成, THEN THE Tool SHALL 记录错误日志并终止当前流程
4. WHERE 支持无头浏览器模式时, THE Tool SHALL 优先使用有头（headed）模式以提高 Cloudflare 通过率

---

### Requirement 4: 邮箱验证码全自动获取

**User Story:** As a 自动化运维工程师, I want to 工具通过 IMAP 自动连接飞书邮箱并获取验证码, so that 邮箱验证步骤完全无需人工操作。

#### Acceptance Criteria

1. THE IMAP_Client SHALL 使用 SSL 协议连接飞书 IMAP 服务器 `imap.feishu.cn`，端口 `993`
2. THE IMAP_Client SHALL 使用 Config 中配置的飞书邮箱账号和密码完成 IMAP 认证
3. WHEN Cursor_Site 触发验证邮件发送后, THE IMAP_Client SHALL 立即开始以每 5 秒一次的频率轮询收件箱
4. WHEN 验证邮件到达收件箱时, THE IMAP_Client SHALL 在 120 秒内检测到该邮件
5. WHEN 验证邮件被检测到后, THE Tool SHALL 从邮件正文中通过正则表达式提取 Verification_Code（6 位数字）
6. IF 在 120 秒内未检测到验证邮件, THEN THE Tool SHALL 记录超时错误并终止流程
7. WHEN Verification_Code 被提取后, THE Tool SHALL 全自动将验证码填入 Cursor_Site 的验证输入框并提交，无需任何人工介入
8. THE Tool SHALL 在 Config 中支持配置以下飞书 IMAP 参数：`IMAP_HOST`（默认 `imap.feishu.cn`）、`IMAP_PORT`（默认 `993`）、`IMAP_USER`、`IMAP_PASSWORD`

---

### Requirement 5: 信用卡全自动绑定

**User Story:** As a 自动化运维工程师, I want to 工具全自动完成信用卡信息填写与提交，无需任何人工介入, so that 账号可以完成付费计划的绑定。

#### Acceptance Criteria

1. WHEN 账号注册并完成邮箱验证后, THE Tool SHALL 全自动导航到 Cursor_Site 的信用卡绑定页面
2. THE Tool SHALL 从 Config 中读取 Credit_Card 信息（卡号、有效期月份、有效期年份、CVV、持卡人姓名、账单邮编）
3. THE Tool SHALL 全自动识别并填写信用卡绑定页面上的所有必填字段，包括嵌套在 iframe 中的 Stripe 支付表单字段
4. WHEN 所有 Credit_Card 字段填写完成后, THE Tool SHALL 全自动点击提交按钮完成绑定，无需人工确认
5. WHEN 信用卡绑定请求提交后, THE Tool SHALL 等待页面响应并验证绑定结果（成功跳转或成功提示）
6. IF 信用卡绑定失败, THEN THE Tool SHALL 记录错误信息（包含页面返回的失败原因）并终止流程
7. THE Tool SHALL 不在日志或控制台中明文输出完整信用卡号（仅显示后四位）

---

### Requirement 6: 付费计划全自动订阅

**User Story:** As a 自动化运维工程师, I want to 工具全自动选择并完成付费计划订阅，无需任何人工介入, so that 账号激活后即可使用付费功能。

#### Acceptance Criteria

1. WHEN 信用卡绑定成功后, THE Tool SHALL 全自动导航到 Cursor_Site 的订阅计划页面
2. THE Tool SHALL 从 Config 中读取目标订阅计划名称（如 `"Pro"`）
3. WHEN 目标计划在页面中被找到时, THE Tool SHALL 全自动点击对应的订阅按钮
4. WHEN 订阅确认弹窗或付款确认页面出现时, THE Tool SHALL 全自动点击确认按钮完成付款，无需人工确认
5. WHEN 订阅请求提交后, THE Tool SHALL 等待页面响应并验证订阅结果（成功跳转或成功提示）
6. IF 订阅失败, THEN THE Tool SHALL 记录错误信息（包含页面返回的失败原因）并终止流程
7. WHEN 订阅成功后, THE Tool SHALL 验证账号订阅状态页面显示正确的计划名称与有效期

---

### Requirement 7: 结果输出

**User Story:** As a 自动化运维工程师, I want to 工具在完成后将账号信息输出到控制台, so that 我可以立即获取并使用创建好的账号。

#### Acceptance Criteria

1. WHEN 全流程成功完成后, THE Tool SHALL 在控制台以结构化格式输出 Account 的邮箱和密码
2. THE Tool SHALL 将 Account 信息同时写入本地文件（如 `accounts.txt`）以便后续使用
3. WHEN 任意步骤失败时, THE Tool SHALL 在控制台输出包含步骤名称和错误原因的错误信息
4. THE Tool SHALL 在每个主要步骤开始和完成时输出进度日志，格式为 `[STEP N] 步骤描述 ... OK/FAILED`

---

### Requirement 8: 文档与配置说明

**User Story:** As a 自动化运维工程师, I want to 通过 README 了解工具的配置要求和使用方法, so that 我可以快速上手并正确配置工具。

#### Acceptance Criteria

1. THE Tool SHALL 提供 `README.md` 文件，说明所有必要配置项及其格式
2. THE README SHALL 列出需要用户自行提供的信息：飞书邮箱账号/密码/IMAP 配置（`imap.feishu.cn:993`）、信用卡完整信息（卡号、有效期、CVV、持卡人姓名、账单邮编）、目标订阅计划名称
3. THE README SHALL 包含安装依赖的命令（`pip install -r requirements.txt` 及 `playwright install`）
4. THE README SHALL 包含 `.env.example` 文件的示例内容，展示所有配置项的占位符
