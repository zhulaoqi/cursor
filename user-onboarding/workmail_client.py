# workmail_client.py - AWS WorkMail 创建邮箱

from __future__ import annotations

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError


class WorkMailError(Exception):
    pass


def create_workmail_user(
    *,
    organization_id: str,
    email: str,
    display_name: str,
    password: str,
    first_name: str = "",
    last_name: str = "",
    region: str = "us-east-1",
) -> str:
    """创建 WorkMail 用户并开通邮箱。

    Returns:
        UserId
    """
    client = boto3.client("workmail", region_name=region)

    # 用户名取邮箱 @ 前部分
    name = email.split("@")[0] if "@" in email else email
    if not first_name and not last_name and display_name:
        parts = display_name.strip().split(None, 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

    params = {
        "OrganizationId": organization_id,
        "Name": name[:64],
        "DisplayName": display_name[:256] if display_name else name,
        "Password": password,
    }
    if first_name:
        params["FirstName"] = first_name[:64]
    if last_name:
        params["LastName"] = last_name[:64]

    try:
        resp = client.create_user(**params)
    except NoCredentialsError as e:
        raise WorkMailError(
            "AWS 凭证缺失：未找到可用 credentials。\n"
            "请配置以下任一方式：\n"
            "1. 在 `.env` 或环境变量中设置 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`\n"
            "2. 使用 `aws configure` 写入 `~/.aws/credentials`\n"
            "3. 设置 `AWS_PROFILE` 指向已配置 profile\n"
            "4. 在 EC2/ECS/Lambda 上使用 IAM Role"
        ) from e
    except PartialCredentialsError as e:
        raise WorkMailError(
            f"AWS 凭证不完整：{e}。\n"
            "请同时配置 `AWS_ACCESS_KEY_ID` 和 `AWS_SECRET_ACCESS_KEY`。"
        ) from e
    except ClientError as e:
        raise WorkMailError(f"create_user 失败: {e}") from e

    user_id = resp["UserId"]

    try:
        client.register_to_work_mail(
            OrganizationId=organization_id,
            EntityId=user_id,
            Email=email,
        )
    except NoCredentialsError as e:
        raise WorkMailError("AWS 凭证缺失，无法调用 register_to_work_mail") from e
    except PartialCredentialsError as e:
        raise WorkMailError(f"AWS 凭证不完整：{e}") from e
    except ClientError as e:
        raise WorkMailError(f"register_to_work_mail 失败: {e}") from e

    return user_id
