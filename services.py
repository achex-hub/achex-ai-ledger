# services.py
import os
import json
import stripe
import csv
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
from sqlalchemy import func
from openai import OpenAI

from config import Config
from models import db, User, Transaction

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from twilio.rest import Client

def send_whatsapp_message(to_number, message):
    client = Client(
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN")
    )

    client.messages.create(
        from_=f"whatsapp:{os.getenv('TWILIO_WHATSAPP_NUMBER')}",
        body=message,
        to=to_number
    )

EXPORT_DIR = Path("exports")
EXPORT_DIR.mkdir(exist_ok=True)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

client = OpenAI(api_key=Config.OPENAI_API_KEY)

PLAN_LIMITS = {
    "free": 2,      # keep 2 while testing
    "starter": 5,
    "pro": 9999999,
}

PLAN_PRICE_IDS = {
    "starter": os.getenv("STRIPE_STARTER_PRICE_ID"),
    "pro": os.getenv("STRIPE_PRO_PRICE_ID"),
}


def create_checkout_session(phone_number: str, plan: str) -> str:
    price_id = PLAN_PRICE_IDS.get(plan)

    if not price_id:
        raise ValueError(f"No Stripe price ID configured for plan: {plan}")

    base_url = os.getenv("APP_BASE_URL")
    if not base_url:
        raise ValueError("APP_BASE_URL is not configured")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[
            {
                "price": price_id,
                "quantity": 1,
            }
        ],
        success_url=f"{base_url}/stripe-success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/stripe-cancel",
        client_reference_id=phone_number,
        metadata={
            "plan": plan,
        },
    )

    print("Created Stripe checkout session:")
    print("  phone_number:", phone_number)
    print("  plan:", plan)
    print("  session_id:", session.id)
    print("  client_reference_id:", session.client_reference_id)
    print("  session_url:", session.url)

    return session.url


def get_or_create_user(phone_number: str) -> User:
    user = User.query.filter_by(phone_number=phone_number).first()
    if user:
        return user

    user = User(phone_number=phone_number, plan="free", status="active")
    db.session.add(user)
    db.session.commit()
    return user


def user_can_add_transaction(user: User) -> bool:
    limit = PLAN_LIMITS.get(user.plan, 50)
    return (user.monthly_transaction_count + 1) <= limit


def normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def parse_transaction_with_ai(message_text: str) -> dict:
    prompt = f"""
You are a financial transaction parser.

Extract transaction details from the user's message.

Return valid JSON only with this schema:
{{
  "item": "string",
  "quantity": number,
  "unit_price": number,
  "total": number,
  "transaction_type": "income" or "expense"
}}

Rules:
- If the user indicates a sale, it is income.
- If the user indicates a purchase, cost, expense, or payment, it is expense.
- If quantity is not stated, use 1.
- If only one number is provided, treat it as both unit_price and total when quantity = 1.
- If quantity > 1 and total is present but unit_price is missing, set unit_price = total / quantity.
- Make item concise and clean.
- Return JSON only. No markdown.
- If parsing is not possible, return:
{{
  "item": "",
  "quantity": 0,
  "unit_price": 0,
  "total": 0,
  "transaction_type": "unknown"
}}

User message:
\"{message_text}\"
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt
    )

    raw_output = response.output_text.strip()

    try:
        parsed = json.loads(raw_output)
    except json.JSONDecodeError:
        parsed = {
            "item": "",
            "quantity": 0,
            "unit_price": 0,
            "total": 0,
            "transaction_type": "unknown"
        }

    quantity = float(parsed.get("quantity", 1) or 1)
    unit_price = float(parsed.get("unit_price", 0) or 0)
    total = float(parsed.get("total", 0) or 0)

    if quantity <= 0:
        quantity = 1

    if unit_price <= 0 and total > 0:
        if quantity == 1:
            unit_price = total
        else:
            unit_price = total / quantity

    if total <= 0 and unit_price > 0 and quantity > 0:
        total = unit_price * quantity

    parsed["quantity"] = quantity
    parsed["unit_price"] = unit_price
    parsed["total"] = total

    return parsed


def save_transaction(user: User, parsed: dict, raw_message: str) -> Transaction:
    transaction = Transaction(
        user_id=user.id,
        type=parsed["transaction_type"],
        item=parsed["item"],
        quantity=float(parsed.get("quantity", 1) or 1),
        unit_price=float(parsed.get("unit_price", 0) or 0),
        total=float(parsed.get("total", 0) or 0),
        currency="USD",
        raw_message=raw_message
    )

    db.session.add(transaction)
    user.monthly_transaction_count += 1
    print("User monthly count:", user.monthly_transaction_count)
    db.session.commit()

    print("Saved transaction total:", transaction.total)
    print("Saved transaction unit_price:", transaction.unit_price)

    return transaction


def get_summary_for_range(user: User, start_dt: datetime, end_dt: datetime) -> dict:
    income_total = db.session.query(func.coalesce(func.sum(Transaction.total), 0)).filter(
        Transaction.user_id == user.id,
        Transaction.type == "income",
        Transaction.created_at >= start_dt,
        Transaction.created_at < end_dt
    ).scalar()

    expense_total = db.session.query(func.coalesce(func.sum(Transaction.total), 0)).filter(
        Transaction.user_id == user.id,
        Transaction.type == "expense",
        Transaction.created_at >= start_dt,
        Transaction.created_at < end_dt
    ).scalar()

    profit = float(income_total) - float(expense_total)
    transaction_count = get_transaction_count_for_range(user, start_dt, end_dt)
    top_items = get_top_items_for_range(user, start_dt, end_dt)

    return {
        "income": float(income_total),
        "expenses": float(expense_total),
        "profit": profit,
        "count": transaction_count,
        "top_items": top_items,
        "start": start_dt,
        "end": end_dt,
    }

def get_today_summary(user: User) -> dict:
    now = datetime.now(timezone.utc)
    start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    end_of_day = start_of_day + timedelta(days=1)
    return get_summary_for_range(user, start_of_day, end_of_day)


def get_week_summary(user: User) -> dict:
    now = datetime.now(timezone.utc)
    start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    start_of_week = start_of_day - timedelta(days=start_of_day.weekday())
    end_of_week = start_of_week + timedelta(days=7)
    return get_summary_for_range(user, start_of_week, end_of_week)


def get_month_summary(user: User) -> dict:
    now = datetime.now(timezone.utc)
    start_of_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)

    return get_summary_for_range(user, start_of_month, next_month)


def get_year_summary(user: User) -> dict:
    now = datetime.now(timezone.utc)
    start_of_year = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    next_year = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    return get_summary_for_range(user, start_of_year, next_year)

def get_transactions_for_range(user: User, start_dt: datetime, end_dt: datetime):
    transactions = Transaction.query.filter(
        Transaction.user_id == user.id,
        Transaction.created_at >= start_dt,
        Transaction.created_at < end_dt
    ).order_by(Transaction.created_at.asc()).all()

    return transactions

def parse_date_range_command(text: str):
    """
    Supports:
    - summary 2026-03-01 to 2026-03-24
    - range 2026-03-01 to 2026-03-24
    """
    normalized = " ".join(text.strip().lower().split())

    if " to " not in normalized:
        return None

    if not (normalized.startswith("summary ") or normalized.startswith("range ")):
        return None

    try:
        if normalized.startswith("summary "):
            date_part = normalized[len("summary "):]
        else:
            date_part = normalized[len("range "):]

        start_str, end_str = date_part.split(" to ", 1)

        start_dt = datetime.strptime(start_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)

        if end_dt <= start_dt:
            return None

        return start_dt, end_dt
    except Exception:
        return None

def get_transaction_count_for_range(user: User, start_dt: datetime, end_dt: datetime) -> int:
    count = db.session.query(func.count(Transaction.id)).filter(
        Transaction.user_id == user.id,
        Transaction.created_at >= start_dt,
        Transaction.created_at < end_dt
    ).scalar()

    return int(count or 0)


def get_top_items_for_range(user: User, start_dt: datetime, end_dt: datetime, limit: int = 5):
    rows = db.session.query(
        Transaction.item,
        func.count(Transaction.id).label("count"),
        func.coalesce(func.sum(Transaction.total), 0).label("amount")
    ).filter(
        Transaction.user_id == user.id,
        Transaction.created_at >= start_dt,
        Transaction.created_at < end_dt
    ).group_by(Transaction.item).order_by(func.sum(Transaction.total).desc()).limit(limit).all()

    return [
        {
            "item": row.item,
            "count": int(row.count),
            "amount": float(row.amount),
        }
        for row in rows
    ]    

def format_summary_message(summary: dict, label: str = "Summary") -> str:
    lines = [
        label,
        f"Revenue: ${summary['income']:.2f}",
        f"Expenses: ${summary['expenses']:.2f}",
        f"Profit: ${summary['profit']:.2f}",
        f"Transactions: {summary['count']}",
    ]

    if summary.get("top_items"):
        lines.append("")
        lines.append("Top items:")
        for item in summary["top_items"][:3]:
            lines.append(
                f"- {item['item'].title()}: ${item['amount']:.2f} ({item['count']} txns)"
            )

    lines.append("")
    lines.append("Send transactions anytime to keep tracking.")

    return "\n".join(lines)

def help_message() -> str:
    return (
        "Welcome to achex AI Ledger.\n\n"
        "Track your business by sending simple WhatsApp messages.\n\n"
        "Examples:\n"
        "- Sold coffee 10\n"
        "- Bought milk 5\n\n"
        "Useful commands:\n"
        "- summary\n"
        "- week\n"
        "- month\n"
        "- export csv month\n\n"
        "Send your first transaction now."
    )

def export_transactions_csv(user: User, start_dt: datetime, end_dt: datetime) -> str:
    transactions = get_transactions_for_range(user, start_dt, end_dt)

    filename = f"{uuid.uuid4().hex}.csv"
    filepath = EXPORT_DIR / filename

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Date",
            "Type",
            "Item",
            "Quantity",
            "Unit Price",
            "Total",
            "Currency",
            "Raw Message",
        ])

        for tx in transactions:
            writer.writerow([
                tx.created_at.isoformat(),
                tx.type,
                tx.item,
                tx.quantity,
                tx.unit_price,
                tx.total,
                tx.currency,
                tx.raw_message,
            ])

    return filename

def export_summary_pdf(user: User, start_dt: datetime, end_dt: datetime, label: str) -> str:
    summary = get_summary_for_range(user, start_dt, end_dt)
    transactions = get_transactions_for_range(user, start_dt, end_dt)

    filename = f"{uuid.uuid4().hex}.pdf"
    filepath = EXPORT_DIR / filename

    c = canvas.Canvas(str(filepath), pagesize=letter)
    width, height = letter

    y = height - 50
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, "achex AI Ledger Export")

    y -= 30
    c.setFont("Helvetica", 12)
    c.drawString(50, y, label)

    y -= 25
    c.drawString(50, y, f"Revenue: ${summary['income']:.2f}")
    y -= 20
    c.drawString(50, y, f"Expenses: ${summary['expenses']:.2f}")
    y -= 20
    c.drawString(50, y, f"Profit: ${summary['profit']:.2f}")
    y -= 20
    c.drawString(50, y, f"Transactions: {summary['count']}")

    y -= 30
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Transactions")

    y -= 20
    c.setFont("Helvetica", 10)

    for tx in transactions:
        line = f"{tx.created_at.date()} | {tx.type} | {tx.item} | ${tx.total:.2f}"
        c.drawString(50, y, line)
        y -= 15

        if y < 50:
            c.showPage()
            y = height - 50
            c.setFont("Helvetica", 10)

    c.save()
    return filename

def parse_export_command(text: str):
    """
    Supports:
    - export csv month
    - export pdf month
    - export csv year
    - export pdf last 30 days
    - export csv 2026-03-01 to 2026-03-24
    """
    normalized = " ".join(text.strip().lower().split())

    if not normalized.startswith("export "):
        return None

    parts = normalized.split(maxsplit=2)
    if len(parts) < 3:
        return None

    _, export_type, period = parts

    if export_type not in ["csv", "pdf"]:
        return None

    return export_type, period

def reset_monthly_usage_if_needed(user: User):
    now = datetime.now(timezone.utc)

    if not user.last_reset_date:
        user.last_reset_date = now
        db.session.commit()
        return

    if now.month != user.last_reset_date.month or now.year != user.last_reset_date.year:
        user.monthly_transaction_count = 0
        user.last_reset_date = now
        db.session.commit()

def upgrade_message(user: User) -> str:
    base_url = os.getenv("APP_BASE_URL")

    starter_link = f"{base_url}/upgrade/starter/{user.phone_number}"
    pro_link = f"{base_url}/upgrade/pro/{user.phone_number}"

    return (
        "You've reached your free limit.\n\n"
        "Keep tracking your business without interruption:\n\n"
        f"Starter — $9/month\n{starter_link}\n\n"
        f"Pro — $29/month\n{pro_link}\n\n"
        "No app. No spreadsheets. Just send messages on WhatsApp."
    )

def generate_upgrade_link(phone, plan):
    base_url = os.getenv("APP_BASE_URL")
    return f"{base_url}/upgrade/{plan}/{phone}"

def handle_general_question(user, message: str) -> str:
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an assistant for achex AI Ledger. "
                        "Help small business owners understand how to use the app. "
                        "Be concise, practical, and friendly. "
                        "Do NOT hallucinate features."
                    )
                },
                {
                    "role": "user",
                    "content": message
                }
            ],
            max_tokens=120
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        print("AI fallback error:", str(e))
        return "Type 'help' to see what you can do."
