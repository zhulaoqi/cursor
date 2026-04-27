# Cursor 账号自动注册 — 技术调研报告

> 文档日期：2026-03-11（第三版，完整失败原因分析）
> 项目：cursor-account-automation

---

## 一、目标

自动化完成 Cursor 账号注册流程：

```
打开注册页 → 填写姓名/邮箱 → 设置密码 → 邮箱验证码 → 注册完成
```

---

## 二、所有尝试方案及成功率

### 2.1 浏览器自动化方案

| # | 方案 | Cloudflare Challenge | 第一页 | 密码页 | 整体成功率 | 状态 |
|---|------|---------------------|--------|--------|-----------|------|
| 1 | **DrissionPage + CDP** | ⚠️ 需代理预热 | ✅ ~90% | ⚠️ retry 后 ~60% | **~50%** | 唯一成功过 |
| 2 | **CloakBrowser (Playwright 反检测)** | ✅ 1秒通过 | ✅ ~80% | ❌ 0%（5轮测试） | **0%** | 失败 |
| 3 | SeleniumBase UC Mode | ✅ 通过 | ✅ 通过 | ❌ 被阻止 | **0%** | 失败 |
| 4 | Patchright (Playwright 反检测分支) | ✅ 通过 | ❌ 表单无法提交 | — | **0%** | 失败 |
| 5 | Playwright + CDP 直连 | ✅ 通过 | ⚠️ 偶尔通过 | ❌ 始终被阻止 | **0%** | 失败 |
| 6 | SeleniumBase UC + disconnect | ✅ 通过 | ✅ 通过 | ❌ 被阻止 | **0%** | 失败 |

### 2.2 CapSolver Token 注入方案

| # | 方案 | 结果 | 服务器错误码 | 成功率 |
|---|------|------|-------------|--------|
| 7 | CapSolver ProxyLess（无代理） | ❌ 被拒 | `bot_detection_failed` | **0%** |
| 8 | CapSolver + 代理（IP 统一） | ❌ 被拒 | `bot_detection_failed` | **0%** |
| 9 | CapSolver + action 参数 | ❌ 被拒 | `bot_detection_failed` | **0%** |
| 10 | CapSolver 不带 action | ❌ 被拒 | `bot_detection_failed` | **0%** |
| 11 | CapSolver + getResponse 覆盖 | ❌ 被拒 | `bot_detection_failed` | **0%** |
| 12 | CapSolver + 路由拦截替换 form body | ❌ 被拒 | `bot_detection_failed` | **0%** |

### 2.3 纯 API 方案（无浏览器）

| # | 方案 | 结果 | 原因 | 成功率 |
|---|------|------|------|--------|
| 13 | curl / requests 直调 Clerk API | ❌ 无法访问 | Cloudflare 管理型 Challenge 拦截 | **0%** |
| 14 | curl_cffi (TLS 指纹伪装) | ❌ 无法访问 | 同上 | **0%** |

### 2.4 其他尝试

| # | 方案 | 结果 | 成功率 |
|---|------|------|--------|
| 15 | CloakBrowser + 40 秒行为模拟 | ❌ policy_denied | **0%** |
| 16 | CloakBrowser + Turnstile auto-retry 5 轮 | ❌ policy_denied | **0%** |
| 17 | Turnstile token reset + 行为积累 + 重试 | ⚠️ 偶尔成功 | **~60%**（仅 DrissionPage） |
| 18 | Canvas/WebGL/AudioContext 指纹伪造 | ❌ 检测率反而更高 | **负面效果** |

---

## 三、失败核心原因（技术分析）

### 3.1 根本矛盾

```
自动化 = 需要控制浏览器 → 必须建立控制通道 (CDP/WebDriver)
                                    ↓
                    控制通道本身就是 Turnstile 的检测目标
                                    ↓
                       无法在控制浏览器的同时不被检测
```

**这是一个逻辑上的死结**：要自动化就必须连接浏览器，连接浏览器就会被检测。

### 3.2 Turnstile 的检测机制

Turnstile 不是传统验证码（识别图片），而是**无感 ML 评分系统**。它在页面加载时采集浏览器底层信号，综合打分决定通过/失败。

#### 3.2.1 无法伪造的检测信号

| 信号类型 | 检测内容 | 为什么无法伪造 |
|---------|---------|---------------|
| **CDP 协议连接** | 浏览器进程是否有 DevTools 调试连接 | 进程级别的 IPC 通信，JS 层无法隐藏 |
| **自动化 API 调用模式** | CDP 命令序列特征（每个框架都有独特指纹） | Playwright/Selenium/DrissionPage 各有不同的命令序列，ML 模型已训练识别 |
| **事件来源验证** | 鼠标/键盘事件是硬件触发还是 API 合成 | `Event.isTrusted` 属性由浏览器内核设置，JS 无法覆盖 |
| **JS 引擎内部状态** | V8 stack trace、Error 对象特征 | 反检测补丁本身会在 stack trace 中留下痕迹 |
| **渲染时序指纹** | 页面渲染帧率、GPU 合成模式 | 自动化浏览器的渲染路径与正常浏览器不同 |
| **内存/性能特征** | heap 大小、GC 频率 | CDP 连接本身消耗额外资源，产生可测量差异 |

#### 3.2.2 为什么 JS 注入/补丁无效

```javascript
// 我们尝试过的补丁：
navigator.webdriver = false          // ← Turnstile 不查这个
chrome.runtime = {...}               // ← Turnstile 不查这个
Object.defineProperty(navigator...)  // ← Turnstile 通过 stack trace 检测到 defineProperty 被调用
```

**Turnstile 不检查这些"初级"标记。** 它检测的是浏览器引擎内核的运行状态，这些状态在 JavaScript 层面不可见、不可修改。

### 3.3 各方案具体失败原因

#### DrissionPage（部分成功）
```
DrissionPage 通过 CDP 控制 Chrome
    ↓
Turnstile ML 识别 CDP → 输出 fail token（policy_denied）
    ↓
但 Turnstile 有评分阈值（不是 0/1 判断，而是概率评分）
    ↓
retry 时积累了更多行为数据 → 评分有时刚好过阈值 → 偶尔成功
```

**成功率 ~60% 的原因**：Turnstile 是概率评分，不是绝对判定。CDP 会扣分，但足够多的行为数据可以部分弥补。

#### CloakBrowser（完全失败）
```
CloakBrowser = 修改 Chromium 源码 + Playwright 控制
    ↓
Chromium 源码补丁隐藏了部分 CDP 指标 → 第一页能过
    ↓
但 Playwright 底层仍走 CDP 协议 → 密码页检测到
    ↓
且源码补丁引入了新的不一致特征（如修改后的 API 行为与原版不同）
    ↓
Turnstile ML 识别到异常组合 → 比原始 CDP 评分更低 → 0% 通过
```

**比 DrissionPage 更差的原因**：补丁制造的"不一致"反而成为新的检测特征。

#### CapSolver Token 注入（完全失败）
```
CapSolver 在自己的浏览器中求解 Turnstile
    ↓
生成了有效 token（在 CapSolver 的环境中确实有效）
    ↓
我们替换到自己的表单提交中
    ↓
Cloudflare siteverify 校验失败：
  - token 绑定了生成时的环境上下文（浏览器指纹/session/IP）
  - 提交环境与生成环境不匹配
    ↓
bot_detection_failed（即使 IP 统一也失败）
```

**失败原因**：Turnstile token 不仅仅是一个随机字符串。它**编码了生成时的浏览器环境信息**。在不同浏览器中提交会导致校验失败。

#### 纯 API 方案（完全失败）
```
curl / requests → 访问 authenticator.cursor.sh
    ↓
Cloudflare 返回管理型 Challenge 页面（"Just a moment..."）
    ↓
Challenge 需要执行复杂的 JS proof-of-work + 浏览器指纹采集
    ↓
HTTP 客户端无法执行 JS → 无法获得 cf_clearance cookie → 无法访问注册页
```

**失败原因**：连注册页面都打不开，更不用说调 API。

### 3.4 指纹伪造为什么适得其反

我们尝试了 Canvas/WebGL/AudioContext 指纹伪造，结果检测率**反而升高**。

```
正常浏览器: Canvas.toDataURL() 每次返回相同结果（确定性）
伪造后:      Canvas.toDataURL() 加了随机噪声，每次不同
    ↓
Turnstile 调用两次 toDataURL，结果不一致 → 检测到伪造 → 扣分
```

**教训**：Turnstile 的 ML 模型不是检查某个值是否"正确"，而是检查行为是否"一致"。伪造制造不一致，反而暴露。

---

## 四、Cursor 防护架构

### 4.1 双层防护

```
              用户浏览器
                  │
                  ▼
       ┌─────────────────────┐
       │  Cloudflare CDN/WAF  │  第一层：管理型 Challenge
       │  (managed challenge)  │  - 拦截所有非浏览器请求（curl/requests）
       │                       │  - IP 频率限制
       │                       │  - 通过后发放 cf_clearance cookie
       └──────────┬──────────┘
                  │
                  ▼
       ┌─────────────────────┐
       │   Clerk + Turnstile  │  第二层：无感 ML 验证
       │                       │  - sitekey: 0x4AAAAAAAMNIvC45A4Wjjln
       │                       │  - action: -sign-up-password
       │                       │  - 每 8 秒自动重试
       │                       │  - 评分 → pass token / fail token
       └──────────┬──────────┘
                  │
                  ▼
       ┌─────────────────────┐
       │   Clerk 服务端策略    │  第三层：业务策略
       │                       │  - 校验 token (siteverify)
       │                       │  - policy_denied / bot_detection_failed
       └─────────────────────┘
```

### 4.2 服务端两种拒绝方式

| 错误码 | 消息 | 触发条件 |
|--------|------|---------|
| `policy_denied` | "访问被阻止，请联系支持" | 浏览器原生 Turnstile 输出 fail token |
| `bot_detection_failed` | "无法验证用户为真人" | 外部 token 注入，siteverify 校验失败 |

### 4.3 已提取的 API 信息

- **端点**：`POST https://authenticator.cursor.sh/sign-up/password?state=...`
- **格式**：`multipart/form-data`
- **Token 字段名**：`1_bot_detection_token`

---

## 五、成本估算

### 当前唯一可用方案（DrissionPage + 代理 + retry）

| 注册量/月 | 代理费用 | CapSolver（辅助） | 合计 | 人民币 |
|----------|---------|------------------|------|--------|
| 10 个 | < $1 | < $0.1 | ~$1 | ~¥7 |
| 50 个 | ~$3 | ~$0.2 | ~$4 | ~¥30 |
| 200 个 | ~$12 | ~$1 | ~$15 | ~¥110 |
| 1000 个 | ~$60 | ~$4 | ~$70 | ~¥500 |

> 注：密码页成功率 ~60%，实际需要约 1.5-2 倍的尝试次数，费用相应翻倍。

---

## 六、风险提示

| 风险 | 严重程度 | 说明 |
|------|---------|------|
| Cursor ToS 违规 | 🔴 高 | 自动化注册违反服务条款，账号可能被批量封禁 |
| 成功率不稳定 | 🔴 高 | 密码页通过率 ~60%，受 IP 信誉、行为评分等多因素影响 |
| Clerk 升级检测 | 🟡 中 | 检测策略可能随时更新，已有方案可能失效 |
| CapSolver 已证实无效 | 🟡 中 | 外部 token 被 Cloudflare 拒绝，不可作为核心依赖 |

---

## 七、结论

### 技术现状

| 结论 | 说明 |
|------|------|
| **密码页无法 100% 可靠突破** | 所有自动化方案都被 Turnstile ML 检测，成功率最高 ~60% |
| **外部求解服务无效** | CapSolver token 注入被 Cloudflare siteverify 拒绝 |
| **纯 API 方案不可行** | Cloudflare 管理型 Challenge 要求真实浏览器 |
| **指纹伪造适得其反** | 制造不一致反而降低评分 |
| **根本原因是逻辑死结** | 自动化需要控制通道，控制通道本身是检测目标 |

### 可选方向

| 方向 | 可行性 | 说明 |
|------|--------|------|
| DrissionPage + retry（当前方案）| ⭐⭐⭐ | 成功率 ~60%，适合小批量 |
| 人工辅助密码页 | ⭐⭐⭐⭐ | 自动填表 + 人工点验证，其余全自动 |
| Camoufox (Firefox 无 CDP) | ⭐⭐ | 理论可行，未验证，开发量大 |
| OS 级输入模拟 (PyAutoGUI) | ⭐ | 无 CDP，但无法 headless，极难工程化 |
| 放弃注册，用 OAuth 登录 | ⭐⭐⭐ | Google/GitHub 登录可能绕过密码页 |

---

## 八、已验证技术细节（备查）

### Turnstile 完整配置

```json
{
    "sitekey": "0x4AAAAAAAMNIvC45A4Wjjln",
    "action": "-sign-up-password",
    "theme": "dark",
    "language": "auto",
    "response-field": false,
    "size": "normal",
    "retry": "auto",
    "retry-interval": 8000,
    "refresh-expired": "auto",
    "refresh-timeout": "auto",
    "execution": "render",
    "appearance": "interaction-only",
    "feedback-enabled": true
}
```

### 测试环境

- macOS (darwin arm64)
- Python 3.13
- DrissionPage + Chromium
- CloakBrowser 0.3.13 (Chromium 145)
- Playwright 1.58.0
- CapSolver API
- Bright Data 住宅代理
