import json
import logging
import os
from pathlib import Path
from typing import Any, Dict
from fastapi import FastAPI, Depends, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import engine, get_db
from app.api import api_router
from app.ml.recovery_probability_model import RecoveryProbabilityModelService
from app.observability.logging import RequestContextMiddleware, configure_logging
from app.observability.metrics import metrics
from app.observability.otel import setup_tracing

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="RevenueShield Recovery AI Platform",
    description="Enterprise Autonomous Revenue Recovery, Voice AI, and Decision Engine",
    version="1.0.0",
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
)

# Parse explicit allowed origins from environment configuration
raw_origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
if not raw_origins or "*" in raw_origins:
    allowed_origins = ["*"]
    allow_credentials = False
else:
    allowed_origins = raw_origins
    allow_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=r"https://.*\.vercel\.app|https://.*\.onrender\.com",
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)
setup_tracing(app, engine)


def _effective_llm_provider() -> str:
    try:
        from app.agent.providers.base import effective_provider_name
        return effective_provider_name()
    except Exception:
        return "unknown"


@app.on_event("startup")
def _log_startup_configuration() -> None:
    """One structured line with the effective configuration (never secrets)."""
    logger.info(
        "[STARTUP] "
        + json.dumps({
            "environment": settings.ENVIRONMENT,
            "execution_mode": settings.EXECUTION_MODE,
            "experiments_enabled": settings.EXPERIMENTS_ENABLED,
            "holdout_percent": settings.HOLDOUT_PERCENT,
            "agent_drives_plans": settings.AGENT_DRIVES_PLANS,
            "llm_provider": settings.LLM_PROVIDER,
            "llm_provider_effective": _effective_llm_provider(),
            "llm_model": settings.LLM_MODEL_PLANNER,
            "llm_model_openai": settings.LLM_MODEL_PLANNER_OPENAI,
            "anthropic_key_configured": bool(settings.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY")),
            "openai_key_configured": bool(settings.OPENAI_API_KEY or os.environ.get("OPENAI_API_KEY")),
            "razorpay_configured": bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET),
            "twilio_configured": bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN),
            "jobs_enabled": settings.JOBS_ENABLED,
            "voice_window": f"{settings.CONTACT_VOICE_START_HOUR:02d}:00-{settings.CONTACT_VOICE_END_HOUR:02d}:00",
        })
    )


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics(db: Session = Depends(get_db)) -> Response:
    """Prometheus scrape endpoint."""
    try:
        from app.models.approval import Approval
        from app.models.degradation_incident import DegradationIncident
        active = db.scalar(select(func.count(DegradationIncident.id)).where(DegradationIncident.status.in_(["SUSPECTED", "CONFIRMED"]))) or 0
        pending = db.scalar(select(func.count(Approval.id)).where(Approval.status == "PENDING")) or 0
        metrics.set_gauges(int(active), int(pending))
    except Exception:  # metrics must never fail the scrape
        pass
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)

# Global safe error handler in production
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"[GLOBAL_EXCEPTION] Unhandled error at {request.method} {request.url.path}: {exc}", exc_info=True)
    if settings.ENVIRONMENT == "production":
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "An internal error occurred. Operational reference logged."},
        )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": str(exc)},
    )

app.include_router(api_router)

# Mount Frontend Static Assets & Application
FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend_static")

    @app.get("/portal", tags=["Frontend Portal"], include_in_schema=False)
    @app.get("/portal/{case_id}", tags=["Frontend Portal"], include_in_schema=False)
    def serve_frontend_portal(case_id: str = None) -> FileResponse:
        """Serve the Customer AI Voice Recovery Portal HTML."""
        index_file = FRONTEND_DIR / "index.html"
        return FileResponse(str(index_file))


@app.get("/", tags=["Root"])
def root_endpoint() -> Dict[str, str]:
    """Root endpoint welcoming requests and pointing to documentation."""
    return {
        "service": "RevenueShield Recovery AI Platform",
        "status": "online",
        "health_url": "/health",
        "ready_url": "/health/ready",
        "portal_url": "/portal",
        "webhook_url": "/webhooks/razorpay",
    }


@app.get("/health", response_model=Dict[str, str], tags=["Health"])
def health_check() -> Dict[str, str]:
    """Liveness health check endpoint to verify backend service availability."""
    return {"status": "ok"}


@app.get("/health/db", response_model=Dict[str, str], tags=["Health"])
def database_health_check(db: Session = Depends(get_db)) -> Dict[str, str]:
    """Database connectivity health check without exposing credentials or internal topology."""
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "error", "database": "disconnected"},
        )


@app.get("/health/ready", response_model=Dict[str, Any], tags=["Health"])
def readiness_health_check(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Readiness probe checking database and ML model readiness without leaking internal secrets."""
    db_ok = False
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        logger.warning(f"[HEALTH_READY_DB_ERROR] Database ping failed: {e}")

    model_loaded = RecoveryProbabilityModelService.load_model() is not None

    if not db_ok:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "database": "disconnected",
                "model_status": "active" if model_loaded else "cold_start",
            },
        )

    extras: Dict[str, Any] = {}
    try:
        from app.audit.chain import AuditChain
        from app.jobs.queue import JobQueue
        from app.agent.providers.base import llm_breaker, resolve_provider
        jobs = JobQueue.stats(db)
        chain = AuditChain.verify(db, limit=200)
        extras = {
            "jobs": {"queued": jobs["by_status"].get("QUEUED", 0), "running": jobs["by_status"].get("RUNNING", 0), "dead": jobs["dead"], "last_completed_at": jobs["last_completed_at"]},
            "audit_chain": {"ok": chain["ok"], "head_sequence": chain["head_sequence"]},
            "llm": {"provider": resolve_provider().name, "circuit": llm_breaker().state},
            "experiments": {"enabled": settings.EXPERIMENTS_ENABLED, "holdout_percent": settings.HOLDOUT_PERCENT},
        }
    except Exception as exc:  # readiness must degrade gracefully
        extras = {"extras_error": str(exc)}

    return {
        "status": "ready",
        "database": "connected",
        "model_status": "active" if model_loaded else "cold_start",
        **extras,
    }
