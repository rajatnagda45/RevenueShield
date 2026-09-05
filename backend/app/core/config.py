from typing import Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    APP_NAME: str = "Revenue Recovery AI"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    PORT: int = 8000
    HOST: str = "0.0.0.0"
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:8000,http://localhost:8000,http://localhost:5173,https://revenue-shield-mu.vercel.app"

    # Database Configuration
    DATABASE_URL: str = "postgresql+psycopg2://postgres:postgrespassword@localhost:5432/revenue_recovery"

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def normalize_database_url(cls, v: Optional[str]) -> str:
        if not v:
            return "postgresql+psycopg2://postgres:postgrespassword@localhost:5432/revenue_recovery"
        val = str(v).strip()
        if val.startswith("postgres://"):
            return val.replace("postgres://", "postgresql+psycopg2://", 1)
        if val.startswith("postgresql://") and not val.startswith("postgresql+"):
            return val.replace("postgresql://", "postgresql+psycopg2://", 1)
        return val

    # Razorpay Test / Live Mode Credentials (Loaded from environment)
    RAZORPAY_KEY_ID: Optional[str] = None
    RAZORPAY_KEY_SECRET: Optional[str] = None
    RAZORPAY_WEBHOOK_SECRET: Optional[str] = None

    # Execution Mode: "dry_run" (simulated provider) or "razorpay_test" (live Razorpay Test API)
    EXECUTION_MODE: str = "dry_run"

    # Communication & WhatsApp Configuration
    COMMUNICATION_MODE: str = "twilio"  # "dry_run", "development", or "twilio"
    WHATSAPP_MODE: str = "REAL"  # "REAL" or "DRY_RUN"
    TWILIO_WHATSAPP_MODE: str = "SANDBOX"  # "SANDBOX" or "PRODUCTION"
    MAX_WHATSAPP_ATTEMPTS: int = 3
    WHATSAPP_COOLDOWN_MINUTES: int = 1440  # 24 hours cooldown between messages
    DND_START_TIME: str = "20:00"
    DND_END_TIME: str = "08:00"
    WHATSAPP_DND_START_HOUR: int = 20  # 20:00 (8 PM)
    WHATSAPP_DND_END_HOUR: int = 8  # 08:00 (8 AM)
    DEFAULT_TIMEZONE: str = "Asia/Kolkata"

    # Twilio Voice & WhatsApp Credentials
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    TWILIO_PHONE_NUMBER: Optional[str] = None  # e.g., "+14155552671"
    TWILIO_WEBHOOK_BASE_URL: Optional[str] = None  # Public tunnel or domain for Twilio webhooks
    TWILIO_API_KEY_SID: Optional[str] = None
    TWILIO_API_KEY_SECRET: Optional[str] = None
    TWILIO_WHATSAPP_FROM: Optional[str] = None  # e.g., "whatsapp:+14155238886"
    TWILIO_WHATSAPP_TO: Optional[str] = None    # e.g., "whatsapp:+919876543210"
    TWILIO_WHATSAPP_NUMBER: Optional[str] = None
    TWILIO_STATUS_CALLBACK_URL: Optional[str] = None
    MAX_VOICE_ATTEMPTS: int = 3
    VOICE_COOLDOWN_MINUTES: int = 60

    # SMTP Email Recovery Configuration
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM_EMAIL: Optional[str] = None
    SMTP_FROM_NAME: str = "RevenueShield Recovery"
    SMTP_USE_TLS: bool = True

    # Intelligent Recovery Sequencer Settings
    MAX_RECOVERY_STEPS: int = 3
    RECOVERY_REEVALUATION_HOURS: int = 24
    MAX_RECOVERY_DURATION_HOURS: int = 72

    # Self-Learning Feedback Loop Settings
    ATTRIBUTION_WINDOW_HOURS: int = 24
    RETRAINING_SCHEDULE_THRESHOLD: int = 100

    # Promise-to-Pay & Escalation Settings
    PROMISE_MIN_AMOUNT: float = 10000.0
    PROMISE_MIN_OVERDUE_HOURS: int = 24
    PROMISE_MAX_DAYS_AHEAD: int = 7
    PROMISE_EXPIRATION_GRACE_HOURS: int = 24

    # Internal Server-to-Server API Security
    INTERNAL_API_SECRET: Optional[str] = None

    # Experimentation & Measurement (v2)
    EXPERIMENTS_ENABLED: bool = True
    EXPERIMENT_KEY: str = "recovery_v2"
    EXPERIMENT_SALT: str = "revenueshield-2026"
    HOLDOUT_PERCENT: float = 10.0
    HOLDOUT_PERCENT_BY_SURFACE: str = ""  # JSON, e.g. {"CHECKOUT_ABANDONMENT": 20}
    CHANNEL_UNIT_COST_INR: str = ""  # JSON override of per-channel unit costs

    # Background jobs (v2): transactional outbox + worker
    JOBS_ENABLED: bool = True
    WORKER_POLL_SECONDS: float = 5.0
    WORKER_BATCH_SIZE: int = 20
    JOB_MAX_ATTEMPTS: int = 5
    JOB_BACKOFF_BASE_SECONDS: float = 30.0
    JOB_BACKOFF_MAX_SECONDS: float = 1800.0
    JOB_LOCK_TIMEOUT_MINUTES: int = 15

    # Observability (v2)
    LOG_JSON: bool = False
    LOG_LEVEL: str = "INFO"

    # Recovery Agent (v2): planner, negotiation envelope, approvals
    LLM_PROVIDER: str = "auto"                 # auto | anthropic | openai | null  (auto: Claude key, else OpenAI key, else rules)
    ANTHROPIC_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_BASE_URL: Optional[str] = None      # optional: Azure/OpenAI-compatible gateway
    LLM_MODEL_PLANNER: str = "claude-opus-5"   # used by the Anthropic provider; also by OpenAI if it names a GPT model
    LLM_MODEL_PLANNER_OPENAI: str = "gpt-4o"   # used by the OpenAI provider when LLM_MODEL_PLANNER is a Claude model
    LLM_EFFORT: str = "medium"                 # low | medium | high
    LLM_TIMEOUT_SECONDS: float = 45.0
    LLM_MAX_TURNS: int = 6
    LLM_CIRCUIT_BREAKER_SECONDS: int = 60
    AGENT_DRIVES_PLANS: bool = False           # when true, the scheduler lets the agent choose each plan step
    NEGOTIATION_MAX_INSTALLMENTS: int = 3
    NEGOTIATION_MIN_FIRST_PAYMENT_PCT: float = 30.0
    NEGOTIATION_MAX_EXTENSION_DAYS: int = 14
    NEGOTIATION_MAX_WAIVER_PCT: float = 0.0
    APPROVAL_VOICE_AMOUNT_THRESHOLD: float = 50000.0
    APPROVAL_CLOSE_CASE_AMOUNT_THRESHOLD: float = 1000.0
    APPROVAL_SLA_HOURS: int = 24

    # Compliance v2: contact windows (customer-local), frequency caps, DND, holidays
    CONTACT_VOICE_START_HOUR: int = 8      # RBI Fair Practices Code: recovery calls 08:00-19:00
    CONTACT_VOICE_END_HOUR: int = 19
    CONTACT_MESSAGE_START_HOUR: int = 8    # WhatsApp / SMS
    CONTACT_MESSAGE_END_HOUR: int = 21
    CONTACT_MAX_TOUCHES_7D: int = 3        # across all channels
    CONTACT_MAX_VOICE_3D: int = 1
    CONTACT_MIN_GAP_HOURS: int = 20
    CONTACT_HOLIDAYS: str = "2026-10-02,2026-10-20,2026-11-08,2026-12-25,2027-01-26"  # ISO dates, comma separated
    DND_REGISTRY_NUMBERS: str = ""         # comma separated; production swaps in a DLT scrubbing provider

    # Payment Degradation Monitor (v2)
    DEGRADATION_WINDOW_MINUTES: int = 15
    DEGRADATION_BASELINE_DAYS: int = 7
    DEGRADATION_MIN_SAMPLES: int = 20          # payments in the window before a cell can be judged
    DEGRADATION_MIN_BASELINE_SAMPLES: int = 50 # cell baseline needs this many payments, else global baseline
    DEGRADATION_BASELINE_FLOOR: float = 0.05   # never assume a bank fails less than 5% (avoids zero-variance alarms)
    DEGRADATION_Z_THRESHOLD: float = 3.0
    DEGRADATION_MIN_ABS_DELTA: float = 0.15    # observed rate must exceed baseline by 15 points
    DEGRADATION_CLEAN_WINDOWS_TO_CLOSE: int = 2
    DEGRADATION_RECHECK_MINUTES: int = 30      # how long a held plan waits before re-checking the incident
    DEGRADATION_RELEASE_JITTER_SECONDS: int = 900

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
