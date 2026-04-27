# 用户入职自动化

从表单用户信息自动创建 **AWS WorkMail 邮箱**，并邀请加入 **Claude 团队**。

## 环境要求

- Python 3.10+
- AWS 账号（WorkMail 已开通）
- Claude 团队版 Admin 权限

## 快速开始

```bash
# 1. 进入项目目录
cd user-onboarding

# 2. 创建虚拟环境
python3 -m venv .venv

# 3. 激活虚拟环境
source .venv/bin/activate   # macOS / Linux
# .venv\Scripts\activate   # Windows

# 4. 安装依赖
pip install -r requirements.txt

# 5. 复制配置
cp .env.example .env
# 编辑 .env 填入 AWS WorkMail 组织 ID、Claude Admin API Key

# 6. 运行
python main.py --email user@example.com --display-name "张三" --password YourPass123!
```

## 配置说明

| 变量 | 必填 | 说明 |
|------|------|------|
| `AWS_WORKMAIL_ORGANIZATION_ID` | 是* | WorkMail 组织 ID（格式 m-32位十六进制） |
| `AWS_REGION` | 否 | AWS 区域，默认 us-east-1 |
| `AWS_ACCESS_KEY_ID` | 是** | AWS Access Key ID |
| `AWS_SECRET_ACCESS_KEY` | 是** | AWS Secret Access Key |
| `AWS_PROFILE` | 否 | 已通过 `aws configure` 配置的 profile 名称 |
| `CLAUDE_ADMIN_API_KEY` | 是* | Claude Admin API Key（sk-ant-admin- 开头） |

\* 仅在使用对应功能时需要
\** WorkMail 需要可用的 AWS 凭证。可用环境变量、`~/.aws/credentials`、`AWS_PROFILE` 或 IAM Role

## 使用示例

```bash
# 完整流程：创建 WorkMail + 邀请 Claude
python main.py --email zhangsan@company.com --display-name "张三" --password Pass123!

# 仅创建 WorkMail 邮箱
python main.py --email zhangsan@company.com --display-name "张三" --password Pass123! --workmail-only

# 仅邀请 Claude 团队（邮箱已存在）
python main.py --email zhangsan@company.com --claude-only

# 指定 Claude 角色
python main.py --email dev@company.com --claude-only --claude-role claude_code_user
```

## 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| `--email` | 是 | 用户邮箱 |
| `--display-name` | 否 | 显示名称，默认取邮箱前缀 |
| `--password` | 创建邮箱时 | WorkMail 初始密码 |
| `--first-name` | 否 | 名 |
| `--last-name` | 否 | 姓 |
| `--workmail-only` | 否 | 仅执行 WorkMail |
| `--claude-only` | 否 | 仅执行 Claude 邀请 |
| `--claude-role` | 否 | user / developer / billing / claude_code_user / managed |

## 项目结构

```
user-onboarding/
├── main.py           # CLI 入口
├── workmail_client.py # AWS WorkMail API
├── claude_client.py   # Claude Admin API
├── requirements.txt
├── .env.example
└── README.md
```

## 获取配置

- **WorkMail 组织 ID**：AWS 控制台 → WorkMail → 组织设置
- **AWS 凭证**：
  - 方式 1：运行 `aws configure`
  - 方式 2：在 `.env` 中设置 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
  - 方式 3：设置 `AWS_PROFILE=your-profile`
- **Claude Admin API Key**：Claude 控制台 → 组织设置 → API Keys → 创建 Admin Key
