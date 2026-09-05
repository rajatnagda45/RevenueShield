"""API package router aggregation."""
from fastapi import APIRouter
from app.api.webhooks import router as webhooks_router
from app.api.recovery_cases import router as recovery_cases_router
from app.api.learning import router as learning_router
from app.api.admin_sync import router as admin_sync_router
from app.api.ml import router as ml_router
from app.api.interventions import router as interventions_router
from app.api.communications import router as communications_router
from app.api.plans import router as plans_router
from app.api.promise_to_pay import router as promise_router
from app.api.voice import router as voice_router
from app.api.dashboard import router as dashboard_router
from app.api.scorecard import router as scorecard_router
from app.api.ledger import router as ledger_router
from app.api.surfaces import router as surfaces_router
from app.api.degradation import router as degradation_router
from app.api.compliance import router as compliance_router
from app.api.agent import router as agent_router
from app.api.approvals import router as approvals_router
from app.api.simulation import router as simulation_router
from app.api.jobs import router as jobs_router
from app.api.cases_v2 import router as cases_v2_router

api_router = APIRouter()
api_router.include_router(simulation_router)
api_router.include_router(jobs_router)
api_router.include_router(cases_v2_router)
api_router.include_router(compliance_router)
api_router.include_router(agent_router)
api_router.include_router(approvals_router)
api_router.include_router(scorecard_router)
api_router.include_router(ledger_router)
api_router.include_router(surfaces_router)
api_router.include_router(degradation_router)
api_router.include_router(webhooks_router, prefix="/webhooks", tags=["Webhooks"])
api_router.include_router(recovery_cases_router, prefix="/recovery-cases", tags=["Recovery Cases"])
api_router.include_router(learning_router, prefix="/learning", tags=["Learning Dataset"])
api_router.include_router(admin_sync_router, prefix="/admin/razorpay", tags=["Admin Razorpay Sync"])
api_router.include_router(dashboard_router, tags=["Command Center Dashboard"])
api_router.include_router(ml_router, tags=["Machine Learning & Predictions"])
api_router.include_router(interventions_router, tags=["Interventions"])
api_router.include_router(communications_router, tags=["WhatsApp Recovery Communications"])
api_router.include_router(plans_router, tags=["Recovery Plans & Sequencer"])
api_router.include_router(promise_router, tags=["Promise to Pay & Escalation"])
api_router.include_router(voice_router, tags=["Twilio Voice Recovery"])

__all__ = [
    "api_router",
    "webhooks_router",
    "recovery_cases_router",
    "learning_router",
    "admin_sync_router",
    "dashboard_router",
    "ml_router",
    "interventions_router",
    "communications_router",
    "plans_router",
    "promise_router",
    "voice_router",
]
