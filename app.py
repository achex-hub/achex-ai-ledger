from flask import Flask, request, redirect, send_from_directory
from twilio.twiml.messaging_response import MessagingResponse
from datetime import datetime, timezone, timedelta
import os
import stripe

from config import Config
from models import db, User
from services import (
    PLAN_LIMITS,
    create_checkout_session,
    export_summary_pdf,
    export_transactions_csv,
    format_summary_message,
    generate_upgrade_link,
    get_month_summary,
    get_or_create_user,
    get_summary_for_range,
    get_today_summary,
    get_week_summary,
    get_year_summary,
    handle_general_question,
    help_message,
    normalize_text,
    parse_date_range_command,
    parse_export_command,
    parse_transaction_with_ai,
    reset_monthly_usage_if_needed,
    save_transaction,
    user_can_add_transaction,
    get_daily_summary, 
    is_premium, 
    generate_insight,
)

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

if not app.config.get("PUBLIC_WHATSAPP_NUMBER"):
    raise ValueError("PUBLIC_WHATSAPP_NUMBER is missing")

if not app.config.get("APP_BASE_URL"):
    raise ValueError("APP_BASE_URL is missing")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

with app.app_context():
    db.create_all()


@app.route("/")
def home():
    return {"status": "ok", "app": "achex AI Ledger"}


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    incoming_message = request.form.get("Body", "").strip()
    from_number = request.form.get("From", "").strip()

    print("Incoming message:", incoming_message)
    print("From number:", from_number)

    resp = MessagingResponse()
    msg = resp.message()

    if not incoming_message or not from_number:
        msg.body("Invalid request.")
        return str(resp)

    user = get_or_create_user(from_number)
    reset_monthly_usage_if_needed(user)

    normalized = normalize_text(incoming_message)

    print("Current user plan:", user.plan)
    print("Current monthly count:", user.monthly_transaction_count)

    # HELP
    if normalized == "help":
        msg.body(help_message())
        return str(resp)

    # SUMMARIES
    if normalized in ["summary", "today", "today summary"]:
        summary = get_today_summary(user)
        msg.body(format_summary_message(summary, "Today's Summary"))
        return str(resp)

    if normalized in ["week", "week summary", "weekly summary"]:
        summary = get_week_summary(user)
        msg.body(format_summary_message(summary, "This Week's Summary"))
        return str(resp)

    if normalized in ["month", "month summary", "monthly summary"]:
        summary = get_month_summary(user)
        msg.body(format_summary_message(summary, "This Month's Summary"))
        return str(resp)

    if normalized in ["year", "year summary", "yearly summary"]:
        summary = get_year_summary(user)
        msg.body(format_summary_message(summary, "This Year's Summary"))
        return str(resp)

    if normalized == "last 7 days":
        now = datetime.now(timezone.utc)
        end_dt = now
        start_dt = now - timedelta(days=7)
        summary = get_summary_for_range(user, start_dt, end_dt)
        msg.body(format_summary_message(summary, "Last 7 Days"))
        return str(resp)

    if normalized == "last 30 days":
        now = datetime.now(timezone.utc)
        end_dt = now
        start_dt = now - timedelta(days=30)
        summary = get_summary_for_range(user, start_dt, end_dt)
        msg.body(format_summary_message(summary, "Last 30 Days"))
        return str(resp)

    range_result = parse_date_range_command(incoming_message)
    if range_result:
        start_dt, end_dt = range_result
        summary = get_summary_for_range(user, start_dt, end_dt)
        label = f"Summary: {start_dt.date()} to {(end_dt - timedelta(days=1)).date()}"
        msg.body(format_summary_message(summary, label))
        return str(resp)

    # EXPORTS
    export_result = parse_export_command(incoming_message)
    if export_result:
        export_type, period = export_result
        now = datetime.now(timezone.utc)

        if period == "month":
            start_dt = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
            if now.month == 12:
                end_dt = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                end_dt = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
            label = "This Month"

        elif period == "year":
            start_dt = datetime(now.year, 1, 1, tzinfo=timezone.utc)
            end_dt = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
            label = "This Year"

        elif period == "last 7 days":
            end_dt = now
            start_dt = now - timedelta(days=7)
            label = "Last 7 Days"

        elif period == "last 30 days":
            end_dt = now
            start_dt = now - timedelta(days=30)
            label = "Last 30 Days"

        else:
            range_result = parse_date_range_command(f"summary {period}")
            if not range_result:
                msg.body(
                    "Invalid export format.\n\n"
                    "Try:\n"
                    "- export csv month\n"
                    "- export pdf month\n"
                    "- export csv 2026-03-01 to 2026-03-24"
                )
                return str(resp)

            start_dt, end_dt = range_result
            label = f"{start_dt.date()} to {(end_dt - timedelta(days=1)).date()}"

        base_url = os.getenv("APP_BASE_URL")
        if not base_url:
            msg.body("APP_BASE_URL is not configured.")
            return str(resp)

        if export_type == "csv":
            filename = export_transactions_csv(user, start_dt, end_dt)
            file_url = f"{base_url}/exports/{filename}"
            msg.body(f"Your CSV export is ready:\n{file_url}")
            return str(resp)

        if export_type == "pdf":
            filename = export_summary_pdf(user, start_dt, end_dt, label)
            file_url = f"{base_url}/exports/{filename}"
            msg.body(f"Your PDF export is ready:\n{file_url}")
            return str(resp)

    # SIMPLE SMART TRIGGERS
    if any(phrase in normalized for phrase in [
        "how does this work",
        "how it works",
        "how does it work",
        "how to use",
        "how do i use",
        "what is this",
        "what does this do",
    ]):
        msg.body(
            "📘 Here's how it works:\n\n"
            "Just send messages like:\n"
            "• Sold coffee 10\n"
            "• Bought sugar 5\n\n"
            "Then type:\n"
            "• summary\n"
            "• week\n"
            "• month\n\n"
            "That's it — no spreadsheets needed."
        )
        return str(resp)

    if any(word in normalized for word in ["price", "cost", "pricing", "plan", "plans"]):
        msg.body(
            "💰 Plans:\n\n"
            "Free → limited usage\n"
            "Starter → $9/month\n"
            "Pro → $29/month\n\n"
            "Type:\n"
            "• upgrade starter\n"
            "• upgrade pro"
        )
        return str(resp)

    if normalized in ["upgrade starter", "upgrade pro"]:
        plan = normalized.split()[1]
        upgrade_link = generate_upgrade_link(from_number, plan)
        msg.body(f"Upgrade to {plan.title()} here:\n{upgrade_link}")
        return str(resp)

    if "how" in normalized:
        msg.body(
            "📘 You can track your business like this:\n\n"
            "• Sold coffee 10\n"
            "• Bought milk 5\n"
            "• summary\n\n"
            "Try one now 👇"
        )
        return str(resp)

    # HARD PAYWALL
    if not user_can_add_transaction(user):
        starter_link = generate_upgrade_link(from_number, "starter")
        pro_link = generate_upgrade_link(from_number, "pro")
        msg.body(
            "🚫 You've reached your monthly limit.\n\n"
            "Upgrade now to continue:\n\n"
            "Starter - 500 Transactions Monthly\n"
            f"{starter_link}\n\n"
            "Pro - Unlimited Transactions\n"
            f"{pro_link}\n\n"
            "Takes 10 seconds."
        )
        return str(resp)

    #SMART QUESTIONS   
    text = incoming_message.lower()

    if "summary" in text:
        summary = get_daily_summary(user)
        msg.body(
            summary +
            "\n\n🚀 Want deeper insights?\nUpgrade to unlock analytics."
        )
        return str(resp)


    if "advice" in text or "insight" in text:
        if not is_premium(user):
            upgrade_link = generate_upgrade_link(from_number, "starter")

            msg.body(
                "🔒 Advanced insights are a premium feature.\n\n"
                "Upgrade to unlock:\n"
                "• Profit analysis\n"
                "• Smart insights\n"
                "• Business tips\n\n"
                f"{upgrade_link}"
            )
            return str(resp)

        insight = generate_insight(user)
        msg.body(insight)
        return str(resp)

    if "how much" in text or "total" in text:
        summary = get_daily_summary(user)
        msg.body(summary)
        return str(resp)

    if "help" in text:
        msg.body(
            "Try:\n"
            "• Sold coffee 10\n"
            "• Bought milk 5\n"
            "• summary\n"
            "• How much did I make today?\n"
            "• insight"
        )
        return str(resp)    

    # PARSE TRANSACTION
    parsed = parse_transaction_with_ai(incoming_message)
    print("Parsed transaction:", parsed)

    if parsed.get("transaction_type") not in ["income", "expense"]:
        ai_reply = handle_general_question(user, incoming_message)
        msg.body(ai_reply)
        return str(resp)

    if not parsed.get("item") or float(parsed.get("total", 0) or 0) <= 0:
        msg.body(
            "I didn't catch that 👀\n\n"
            "Try:\n"
            "• Sold coffee 10\n"
            "• Bought milk 5\n"
            "• summary\n\n"
            "Or type help"
        )
        return str(resp)

    # SOFT UPSELL
    limit = PLAN_LIMITS.get(user.plan, 50)
    next_count = user.monthly_transaction_count + 1

    soft_upsell_text = ""
    if next_count >= 0.8 * limit and next_count < limit:
        soft_upsell_text = (
            "\n\n⚠️ You're close to your monthly limit.\n"
            "Upgrade soon to avoid interruption."
        )

    # SAVE TRANSACTION
    transaction = save_transaction(user, parsed, incoming_message)

    # FRIEND INVITE
    invite_line = ""
    public_number = os.getenv("PUBLIC_WHATSAPP_NUMBER", "17253292575")
    if public_number and user.monthly_transaction_count % 5 == 0:
        invite_line = (
            "\n\n🔥 You're tracking like a pro.\n"
            "Invite a friend:\n"
            f"https://wa.me/{public_number}"
        )

    msg.body(
        f"Recorded: {transaction.type.title()} — {transaction.item.title()} — ${transaction.total:.2f}\n\n"
        "You're tracking your business in real time."
        + soft_upsell_text
        + invite_line
    )
    return str(resp)


@app.route("/pricing")
def pricing():
    return """
    <html>
        <head>
            <title>achex AI Ledger - Pricing</title>
        </head>
        <body style="font-family: Arial, sans-serif; padding: 40px; background: #f7f7f7;">
            <div style="max-width: 900px; margin: auto;">
                <h1 style="text-align: center;">achex AI Ledger</h1>
                <p style="text-align: center; font-size: 18px;">Bookkeeping through WhatsApp. No spreadsheets. No accounting skills required.</p>

                <div style="display: flex; gap: 20px; margin-top: 40px;">
                    <div style="flex: 1; background: white; padding: 30px; border-radius: 12px;">
                        <h2>Free</h2>
                        <p><strong>$0/month</strong></p>
                        <p>Limited monthly transactions</p>
                    </div>

                    <div style="flex: 1; background: white; padding: 30px; border-radius: 12px;">
                        <h2>Starter</h2>
                        <p><strong>$9/month</strong></p>
                        <p>Up to 500 transactions per month</p>
                    </div>

                    <div style="flex: 1; background: white; padding: 30px; border-radius: 12px;">
                        <h2>Pro</h2>
                        <p><strong>$29/month</strong></p>
                        <p>High usage limits and full reporting</p>
                    </div>
                </div>
            </div>
        </body>
    </html>
    """


@app.route("/admin/set-plan", methods=["GET"])
def set_plan():
    phone = request.args.get("phone", "").strip()
    plan = request.args.get("plan", "").strip().lower()

    if not phone or not plan:
        return {"error": "phone and plan are required"}, 400

    if phone.startswith("whatsapp: ") and not phone.startswith("whatsapp:+"):
        phone = phone.replace("whatsapp: ", "whatsapp:+", 1)

    user = User.query.filter_by(phone_number=phone).first()

    if not user:
        return {
            "error": "user not found",
            "phone_received": phone
        }, 404

    user.plan = plan
    db.session.commit()

    return {
        "message": "plan updated",
        "phone": user.phone_number,
        "plan": user.plan
    }


@app.route("/admin/reset-count", methods=["GET"])
def reset_count():
    phone = request.args.get("phone", "").strip()

    if not phone:
        return {"error": "phone is required"}, 400

    if phone.startswith("whatsapp: ") and not phone.startswith("whatsapp:+"):
        phone = phone.replace("whatsapp: ", "whatsapp:+", 1)

    user = User.query.filter_by(phone_number=phone).first()

    if not user:
        return {
            "error": "user not found",
            "phone_received": phone
        }, 404

    user.monthly_transaction_count = 0
    db.session.commit()

    return {
        "message": "monthly count reset",
        "phone": user.phone_number,
        "monthly_transaction_count": user.monthly_transaction_count
    }


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except Exception as e:
        print("Webhook error:", str(e))
        return {"error": str(e)}, 400

    print("EVENT TYPE:", event["type"])

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        phone = session.client_reference_id

        plan = "starter"
        if session.metadata and "plan" in session.metadata:
            plan = session.metadata["plan"]

        print("Stripe payment received:", phone, plan)

        if phone:
            user = User.query.filter_by(phone_number=phone).first()

            if user:
                user.plan = plan
                user.monthly_transaction_count = 0
                db.session.commit()
                print("User upgraded automatically:", user.phone_number, user.plan)
            else:
                print("No user found for phone:", phone)
        else:
            print("No client_reference_id found in Stripe session")

    return {"status": "success"}


@app.route("/stripe-success")
def stripe_success():
    return """
    <html>
        <head>
            <title>achex AI Ledger - Payment Successful</title>
        </head>
        <body style="font-family: Arial, sans-serif; text-align: center; padding: 60px; background: #f7f7f7;">
            <div style="max-width: 600px; margin: auto; background: white; padding: 40px; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.08);">
                <h1 style="margin-bottom: 20px;">✅ Payment Successful</h1>
                <p style="font-size: 18px;">Your achex AI Ledger plan has been upgraded.</p>
                <p style="font-size: 16px;">You can now return to WhatsApp and continue using the service.</p>
            </div>
        </body>
    </html>
    """


@app.route("/stripe-cancel")
def stripe_cancel():
    return """
    <html>
        <head>
            <title>achex AI Ledger - Checkout Cancelled</title>
        </head>
        <body style="font-family: Arial, sans-serif; text-align: center; padding: 60px; background: #f7f7f7;">
            <div style="max-width: 600px; margin: auto; background: white; padding: 40px; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.08);">
                <h1 style="margin-bottom: 20px;">Checkout Cancelled</h1>
                <p style="font-size: 18px;">No payment was completed.</p>
                <p style="font-size: 16px;">You can return to WhatsApp and upgrade whenever you're ready.</p>
            </div>
        </body>
    </html>
    """


@app.route("/admin/set-email", methods=["GET"])
def set_email():
    phone = request.args.get("phone", "").strip()
    email = request.args.get("email", "").strip().lower()

    if not phone or not email:
        return {"error": "phone and email are required"}, 400

    if phone.startswith("whatsapp: ") and not phone.startswith("whatsapp:+"):
        phone = phone.replace("whatsapp: ", "whatsapp:+", 1)

    user = User.query.filter_by(phone_number=phone).first()

    if not user:
        return {
            "error": "user not found",
            "phone_received": phone
        }, 404

    user.email = email
    db.session.commit()

    return {
        "message": "email updated",
        "phone": user.phone_number,
        "email": user.email
    }


@app.route("/upgrade/<plan>/<path:phone>")
def upgrade_checkout(plan, phone):
    try:
        if plan not in ["starter", "pro"]:
            return {"error": "invalid plan"}, 400

        checkout_url = create_checkout_session(phone, plan)
        return redirect(checkout_url)
    except Exception as e:
        print("Upgrade route error:", str(e))
        return {"error": str(e)}, 400


@app.route("/exports/<filename>")
def download_export(filename):
    return send_from_directory("exports", filename, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True)