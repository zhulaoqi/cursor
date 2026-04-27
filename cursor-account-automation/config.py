"""config.py - 配置加载与校验

动态参数：--email / --imap-password（CLI 传入，每次注册不同）
固定参数：.env（信用卡、订阅计划等）
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass

from dotenv import load_dotenv


_FIRST_NAMES = [
    "James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael",
    "Linda", "David", "Elizabeth", "William", "Barbara", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Daniel", "Karen", "Alex",
    "Chris", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley",
]

_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Wilson", "Anderson", "Taylor",
    "Thomas", "Moore", "Jackson", "Martin", "Lee", "White", "Harris",
    "Clark", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
]


def _random_name() -> str:
    return random.choice(_FIRST_NAMES + _LAST_NAMES)


class ConfigError(Exception):
    """必填配置项缺失时抛出。"""
    pass


@dataclass
class Config:
    # 注册信息（CLI 传入）
    email: str = ""
    first_name: str = ""
    last_name: str = ""

    # IMAP（读验证码用，服务器和密码在 .env，所有邮箱共用）
    imap_host: str = "imap.feishu.cn"
    imap_port: int = 993
    imap_password: str = ""

    # 信用卡
    card_number: str = ""
    card_exp_month: str = ""
    card_exp_year: str = ""
    card_cvv: str = ""
    card_holder: str = ""
    card_zip: str = ""

    # 订阅
    plan_name: str = "Pro"

    # 运行模式
    headless: bool = False

    # 可选
    proxy: str | None = None


_REQUIRED_ENV_FIELDS = [
    "CARD_NUMBER",
    "CARD_EXP_MONTH",
    "CARD_EXP_YEAR",
    "CARD_CVV",
    "CARD_HOLDER",
    "CARD_ZIP",
]


def load_config(
    *,
    email: str,
    imap_password: str,
    first_name: str = "",
    last_name: str = "",
    headless: bool = False,
) -> Config:
    """加载配置。

    email / imap_password / first_name / last_name 通过 CLI 传入，
    其余从 .env 读取。姓名不传则自动随机生成。
    """
    load_dotenv()

    for key in _REQUIRED_ENV_FIELDS:
        if not os.environ.get(key):
            raise ConfigError(f"Missing required config: {key}")

    if not email:
        raise ConfigError("缺少注册邮箱，请通过 --email 传入")
    if not imap_password:
        raise ConfigError("缺少 IMAP 密码，请通过 --imap-password 传入")

    if not first_name:
        first_name = _random_name()
    if not last_name:
        last_name = _random_name()

    return Config(
        email=email,
        first_name=first_name,
        last_name=last_name,
        headless=headless,
        imap_host=os.environ.get("IMAP_HOST", "imap.feishu.cn"),
        imap_port=int(os.environ.get("IMAP_PORT", "993")),
        imap_password=imap_password,
        card_number=os.environ["CARD_NUMBER"],
        card_exp_month=os.environ["CARD_EXP_MONTH"],
        card_exp_year=os.environ["CARD_EXP_YEAR"],
        card_cvv=os.environ["CARD_CVV"],
        card_holder=os.environ["CARD_HOLDER"],
        card_zip=os.environ["CARD_ZIP"],
        plan_name=os.environ.get("PLAN_NAME", "Pro"),
        proxy=os.environ.get("PROXY") or None,
    )
