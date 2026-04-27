# output.py - 结果输出


def print_account(email: str, password: str) -> None:
    """
    控制台结构化输出：
    ==================== ACCOUNT ====================
    Email   : user@example.com
    Password: Abc123!@#xyz456
    =================================================
    """
    border = "=" * 49
    mid_title = "=" * 20 + " ACCOUNT " + "=" * 20
    print(mid_title)
    print(f"Email   : {email}")
    print(f"Password: {password}")
    print(border)


def save_account(email: str, password: str, filepath: str = "accounts.txt") -> None:
    """以追加模式写入 accounts.txt，格式：email:password"""
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(f"{email}:{password}\n")


def mask_card_number(card_number: str) -> str:
    """返回脱敏卡号，仅显示后四位，其余替换为 *"""
    if len(card_number) <= 4:
        return card_number
    return "*" * (len(card_number) - 4) + card_number[-4:]
