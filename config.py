import os


class Config:
    # =========================
    # CORE APP
    # =========================
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")

    # =========================
    # DATABASE (Render Postgres)
    # =========================
    DATABASE_URL = os.getenv("DATABASE_URL")

    if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # =========================
    # OPENAI
    # =========================
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

    # =========================
    # STRIPE
    # =========================
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

    STRIPE_STARTER_PRICE_ID = os.getenv("STRIPE_STARTER_PRICE_ID")
    STRIPE_PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID")

    # =========================
    # APP URL (VERY IMPORTANT)
    # =========================
    APP_BASE_URL = os.getenv("APP_BASE_URL")

    # =========================
    # WHATSAPP
    # =========================
    PUBLIC_WHATSAPP_NUMBER = os.getenv("PUBLIC_WHATSAPP_NUMBER")