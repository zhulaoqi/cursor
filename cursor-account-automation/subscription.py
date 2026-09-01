# subscription.py - 订阅 / 绑卡（Stripe Checkout）
#
# 流程：
#   1. 登录后跳转到 billing 页面
#   2. 选择套餐（Pro / Pro+ / Ultra）
#   3. 在 Stripe Checkout 页填写信用卡信息
#   4. 确认支付并验证订阅成功

import random
import time

BILLING_URL = "https://cursor.com/dashboard"
BILLING_TAB_URL = "https://cursor.com/dashboard"

PLAN_MAP = {
    "pro": "Pro",
    "pro+": "Pro+",
    "ultra": "Ultra",
}


def navigate_to_billing(tab) -> None:
    """导航到 Cursor Billing 页面。"""
    print("[订阅] 打开 billing 页面...")
    tab.get(BILLING_TAB_URL)
    time.sleep(5)

    url = tab.url or ""
    if "login" in url or "sign-in" in url:
        raise RuntimeError("未登录，请先完成登录流程")

    print(f"[订阅] 当前页面: {url[:80]}")


def select_plan(tab, plan_name: str = "Pro") -> None:
    """在 billing 页面选择套餐并触发 Stripe Checkout。

    plan_name: Pro / Pro+ / Ultra
    """
    normalized = plan_name.strip().lower()
    display_name = PLAN_MAP.get(normalized, plan_name)
    print(f"[订阅] 选择套餐: {display_name}")

    # 先点 "Upgrade Now" 或 "Adjust plan" 打开套餐选择弹窗
    for btn_text in ["Upgrade Now", "Adjust plan", "升级"]:
        try:
            btn = tab.ele(f"text:{btn_text}", timeout=3)
            if btn:
                btn.click()
                print(f"[订阅] 点击「{btn_text}」")
                time.sleep(3)
                break
        except Exception:
            continue

    # 在弹窗中选择目标套餐
    # 按钮文案可能是 "Get Pro" / "Upgrade to Pro" / "Select Pro" 等
    plan_btn_candidates = [
        f"Get {display_name}",
        f"Upgrade to {display_name}",
        f"Select {display_name}",
        f"升级到 {display_name}",
        f"Subscribe to {display_name}",
    ]

    clicked = False
    for text in plan_btn_candidates:
        try:
            btn = tab.ele(f"text:{text}", timeout=3)
            if btn:
                tag = btn.tag.lower() if hasattr(btn, "tag") else ""
                if tag in ("script", "style", "noscript"):
                    continue
                btn.click()
                print(f"[订阅] 点击「{text}」")
                clicked = True
                time.sleep(5)
                break
        except Exception:
            continue

    if not clicked:
        # 尝试直接匹配计划名称的链接（定价页跳转模式）
        for text in [display_name]:
            try:
                cards = tab.eles(f"text:{text}")
                for card in cards:
                    tag = card.tag.lower() if hasattr(card, "tag") else ""
                    if tag in ("a", "button"):
                        card.click()
                        print(f"[订阅] 通过链接/按钮选择了 {display_name}")
                        clicked = True
                        time.sleep(5)
                        break
            except Exception:
                continue
            if clicked:
                break

    if not clicked:
        print(f"[订阅] 未找到 {display_name} 套餐按钮，尝试继续...")


def _wait_for_stripe_checkout(tab, timeout: int = 30) -> bool:
    """等待 Stripe Checkout 页面加载。"""
    print("[订阅] 等待 Stripe Checkout 页面...")
    for _ in range(timeout):
        url = tab.url or ""
        if "checkout.stripe.com" in url:
            print(f"[订阅] 已跳转到 Stripe: {url[:80]}")
            time.sleep(3)
            return True
        # Stripe 也可能嵌入在 cursor 页面中（Embedded Checkout）
        try:
            if tab.ele("@name=cardNumber", timeout=1):
                print("[订阅] 检测到嵌入式 Stripe 表单")
                return True
        except Exception:
            pass
        try:
            if tab.ele("tag:iframe@src:stripe.com", timeout=1):
                print("[订阅] 检测到 Stripe iframe")
                return True
        except Exception:
            pass
        time.sleep(1)

    print(f"[订阅] Stripe 未在 {timeout}s 内加载，URL: {(tab.url or '')[:80]}")
    return False


def fill_stripe_card(
    tab,
    card_number: str,
    card_exp_month: str,
    card_exp_year: str,
    card_cvv: str,
    card_holder: str = "",
    card_zip: str = "",
) -> None:
    """在 Stripe Checkout 页面填写信用卡信息。

    支持两种模式：
    1. Stripe Hosted Checkout (checkout.stripe.com) —— 字段直接在页面上
    2. Stripe Elements (嵌入 iframe) —— 需切入 iframe
    """
    print("[绑卡] 填写信用卡信息...")
    time.sleep(2)

    url = tab.url or ""
    exp_str = f"{card_exp_month} / {card_exp_year[-2:]}"

    if "checkout.stripe.com" in url:
        _fill_stripe_hosted(tab, card_number, exp_str, card_cvv, card_holder, card_zip)
    else:
        _fill_stripe_embedded(tab, card_number, exp_str, card_cvv, card_holder, card_zip)


def _fill_stripe_hosted(tab, card_number, exp_str, cvv, holder, zip_code):
    """Stripe Hosted Checkout：字段直接在页面上（非 iframe）。"""
    _type_slowly(tab, "@id=cardNumber", card_number, "卡号")
    _type_slowly(tab, "@id=cardExpiry", exp_str, "有效期")
    _type_slowly(tab, "@id=cardCvc", cvv, "CVV")

    if holder:
        _type_slowly(tab, "@id=billingName", holder, "持卡人")

    if zip_code:
        try:
            zip_field = tab.ele("@id=billingPostalCode", timeout=3) or \
                        tab.ele("@name=postalCode", timeout=2) or \
                        tab.ele("@autocomplete=postal-code", timeout=2)
            if zip_field:
                zip_field.clear()
                zip_field.input(zip_code)
                print(f"[绑卡] 邮编: {zip_code}")
        except Exception:
            pass

    print("[绑卡] 信用卡信息已填写（Hosted Checkout）")


def _fill_stripe_embedded(tab, card_number, exp_str, cvv, holder, zip_code):
    """Stripe Elements (嵌入 iframe 模式)。"""
    # 尝试定位 Stripe iframe
    frame = None
    for selector in [
        "tag:iframe@title=Secure payment input frame",
        "tag:iframe@name:__privateStripeFrame",
        "tag:iframe@src:js.stripe.com",
    ]:
        try:
            frame = tab.get_frame(selector, timeout=5)
            if frame:
                break
        except Exception:
            continue

    if not frame:
        print("[绑卡] 未找到 Stripe iframe，尝试直接操作页面...")
        _fill_stripe_hosted(tab, card_number, exp_str, cvv, holder, zip_code)
        return

    _type_slowly(frame, "@name=cardnumber", card_number, "卡号")
    _type_slowly(frame, "@name=exp-date", exp_str, "有效期")
    _type_slowly(frame, "@name=cvc", cvv, "CVV")

    if holder:
        try:
            name_field = tab.ele("@name=billingName", timeout=3) or \
                         tab.ele("@id=billingName", timeout=2)
            if name_field:
                name_field.clear()
                name_field.input(holder)
                print(f"[绑卡] 持卡人: {holder}")
        except Exception:
            pass

    if zip_code:
        try:
            zip_field = tab.ele("@name=postalCode", timeout=3) or \
                        tab.ele("@autocomplete=postal-code", timeout=2)
            if zip_field:
                zip_field.clear()
                zip_field.input(zip_code)
                print(f"[绑卡] 邮编: {zip_code}")
        except Exception:
            pass

    print("[绑卡] 信用卡信息已填写（Embedded Elements）")


def _type_slowly(context, selector: str, value: str, label: str) -> None:
    """模拟真人打字速度输入。"""
    try:
        field = context.ele(selector, timeout=8)
        if not field:
            print(f"[绑卡] 未找到 {label} 字段: {selector}")
            return
        field.click()
        time.sleep(random.uniform(0.2, 0.5))
        for ch in value:
            field.input(ch)
            time.sleep(random.uniform(0.05, 0.15))
        print(f"[绑卡] {label}: {'*' * (len(value) - 4) + value[-4:] if len(value) > 4 else '***'}")
        time.sleep(random.uniform(0.3, 0.8))
    except Exception as e:
        print(f"[绑卡] {label} 输入失败: {e}")


def submit_payment(tab) -> None:
    """点击 Stripe 的支付/订阅按钮。"""
    btn_texts = [
        "Subscribe",
        "订阅",
        "Pay",
        "支付",
        "Start trial",
        "开始试用",
        "Submit",
        "提交",
    ]

    for text in btn_texts:
        try:
            btn = tab.ele(f"text:{text}", timeout=3)
            if btn:
                tag = btn.tag.lower() if hasattr(btn, "tag") else ""
                if tag in ("script", "style", "noscript", "label"):
                    continue
                btn.click()
                print(f"[订阅] 点击「{text}」提交支付")
                time.sleep(10)
                return
        except Exception:
            continue

    # 兜底：找 type=submit 按钮
    try:
        submit = tab.ele("@type=submit", timeout=5)
        if submit:
            submit.click()
            print("[订阅] 点击 submit 按钮")
            time.sleep(10)
            return
    except Exception:
        pass

    print("[订阅] 未找到支付按钮")


def verify_subscription(tab, timeout: int = 30) -> bool:
    """验证订阅是否成功（检查 billing 页面状态）。"""
    print("[订阅] 验证订阅状态...")

    for _ in range(timeout):
        url = tab.url or ""
        # 支付成功后通常跳回 cursor 的 dashboard
        if "cursor.com" in url and "checkout" not in url:
            break
        time.sleep(1)

    # 重新打开 billing 页面确认
    tab.get(BILLING_TAB_URL)
    time.sleep(5)

    try:
        body = tab.ele("tag:body").text or ""
        for plan in ["Pro", "Pro+", "Ultra"]:
            plan_patterns = [
                f"{plan} Plan",
                f"{plan}方案",
                f"Current plan: {plan}",
            ]
            for pattern in plan_patterns:
                if pattern in body:
                    print(f"[订阅] 确认订阅成功: {plan}")
                    return True

        if "auto renew" in body or "自动续费" in body or "will renew" in body:
            print("[订阅] 检测到有效订阅")
            return True

    except Exception:
        pass

    print("[订阅] 未能确认订阅状态")
    return False
