"""main.py - 用户入职自动化

流程：
  1. 从表单/参数获取用户信息
  2. 创建 AWS WorkMail 邮箱
  3. 邀请用户加入 Claude 团队

用法：
  python main.py --email zhangsan --display-name "张三" --password Pass123!
  python main.py --email zhangsan@any.com --display-name "张三" --password Pass123!
  （--email 只取 @ 前的用户名，WorkMail 域名从 .env 的 AWS_WORKMAIL_DOMAIN 读取）
  python main.py --email zhangsan --display-name "张三" --password Pass123! --workmail-only
  python main.py --email zhangsan@blastdrama.awsapps.com --claude-only
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from workmail_client import WorkMailError, create_workmail_user
from claude_client import ClaudeError, invite_to_claude_team


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="用户入职：WorkMail 邮箱 + Claude 团队邀请")
    parser.add_argument("--email", required=True, help="用户名或邮箱（自动提取@前用户名，拼接 WORKMAIL_DOMAIN）")
    parser.add_argument("--display-name", default="", help="显示名称")
    parser.add_argument("--password", default="", help="WorkMail 初始密码（创建邮箱时必填）")
    parser.add_argument("--first-name", default="", help="名")
    parser.add_argument("--last-name", default="", help="姓")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--workmail-only", action="store_true", help="仅创建 WorkMail 邮箱")
    mode.add_argument("--claude-only", action="store_true", help="仅邀请 Claude 团队")

    parser.add_argument("--claude-role", default="user",
                        help="Claude 角色: user|developer|billing|claude_code_user|managed")
    return parser.parse_args()


def run() -> None:
    load_dotenv()
    args = parse_args()

    raw_email = args.email.strip()
    if not raw_email:
        print("错误：请提供邮箱或用户名", file=sys.stderr)
        sys.exit(1)

    username = raw_email.split("@")[0] if "@" in raw_email else raw_email
    workmail_domain = os.environ.get("AWS_WORKMAIL_DOMAIN", "")
    workmail_email = f"{username}@{workmail_domain}" if workmail_domain else raw_email

    display_name = args.display_name.strip() or username
    do_workmail = not args.claude_only
    do_claude = not args.workmail_only

    if do_workmail:
        org_id = os.environ.get("AWS_WORKMAIL_ORGANIZATION_ID")
        if not org_id:
            print("错误：请设置 AWS_WORKMAIL_ORGANIZATION_ID", file=sys.stderr)
            sys.exit(1)
        if not workmail_domain:
            print("错误：请在 .env 中设置 AWS_WORKMAIL_DOMAIN（如 blastdrama.awsapps.com）", file=sys.stderr)
            sys.exit(1)
        if not args.password:
            print("错误：创建 WorkMail 邮箱需要 --password", file=sys.stderr)
            sys.exit(1)

        print(f"[1/2] 创建 WorkMail 邮箱: {workmail_email} ...")
        try:
            user_id = create_workmail_user(
                organization_id=org_id,
                email=workmail_email,
                display_name=display_name,
                password=args.password,
                first_name=args.first_name,
                last_name=args.last_name,
                region=os.environ.get("AWS_REGION", "us-east-1"),
            )
            print(f"  ✓ WorkMail 邮箱已创建: {workmail_email} (UserId: {user_id})")
        except WorkMailError as e:
            print(f"  ✗ {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("[1/2] 跳过 WorkMail（--claude-only）")

    invite_email = workmail_email if do_workmail else raw_email

    if do_claude:
        print(f"[2/2] 邀请 Claude 团队: {invite_email} ...")
        try:
            result = invite_to_claude_team(
                email=invite_email,
                role=args.claude_role,
                api_key=os.environ.get("CLAUDE_ADMIN_API_KEY"),
            )
            invite_id = result.get("id", "?")
            print(f"  ✓ 邀请已发送: {invite_email} (invite_id: {invite_id})")
        except ClaudeError as e:
            print(f"  ✗ {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("[2/2] 跳过 Claude（--workmail-only）")

    print("\n完成")


if __name__ == "__main__":
    run()
