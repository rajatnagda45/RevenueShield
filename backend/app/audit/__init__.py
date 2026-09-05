"""Tamper-evident audit trail (hash chain) and verification."""
from app.audit.chain import AuditChain, install_audit_chain

__all__ = ["AuditChain", "install_audit_chain"]
