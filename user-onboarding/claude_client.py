# claude_client.py - Claude 团队版邀请用户

from __future__ import annotations

import requests


class ClaudeError(Exception):
    pass


def invite_to_claude_team(
    *,
    email: str,
    role: str = "user",
    api_key: str | None = None,
) -> dict:
    """通过 Claude Admin API 邀请用户加入团队。

    Args:
        email: 被邀请人邮箱
        role: user | developer | billing | claude_code_user | managed
        api_key: Admin API Key (sk-ant-admin-...)，不传则从环境变量 CLAUDE_ADMIN_API_KEY 读取

    Returns:
        API 返回的 invite 信息
    """
    if not api_key:
        import os
        api_key = os.environ.get("CLAUDE_ADMIN_API_KEY")
    if not api_key:
        raise ClaudeError("缺少 Claude Admin API Key，请设置 CLAUDE_ADMIN_API_KEY")

    url = "https://api.anthropic.com/v1/organizations/invites"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {"email": email, "role": role}

    resp = requests.post(url, headers=headers, json=payload, timeout=30)

    if resp.status_code >= 400:
        raise ClaudeError(
            f"Claude API 错误 {resp.status_code}: {resp.text}"
        )

    return resp.json()
