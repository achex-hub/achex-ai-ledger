from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utc_now():
    return datetime.now(timezone.utc)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    phone_number = db.Column(db.String(32), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), unique=True, nullable=True, index=True)
    name = db.Column(db.String(120), nullable=True)
    plan = db.Column(db.String(50), default="free", nullable=False)
    status = db.Column(db.String(50), default="active", nullable=False)
    monthly_transaction_count = db.Column(db.Integer, default=0, nullable=False)
    last_reset_date = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)

class Transaction(db.Model):
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    type = db.Column(db.String(20), nullable=False)
    item = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Float, default=1, nullable=False)
    unit_price = db.Column(db.Float, default=0, nullable=False)
    total = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default="USD", nullable=False)
    raw_message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)

def reset_monthly_usage_if_needed(user: User):
    now = datetime.now(timezone.utc)

    if not user.last_reset_date:
        user.last_reset_date = now
        return

    if now.month != user.last_reset_date.month:
        user.monthly_transaction_count = 0
        user.last_reset_date = now
        db.session.commit()
