"""The Recovery Agent: an LLM planner with bounded, policy-gated tools and a deterministic fallback.

Where AI adds judgment (reading a customer's situation, choosing and personalising the next move,
negotiating inside an envelope, knowing when to hand off) an LLM plans. Where correctness matters
(policy, consent, money, audit) deterministic code decides. The agent never touches Razorpay or
Twilio directly; every mutating tool call passes the PolicyEngine, the ContactPolicy and the
ExecutionGuard, and every run leaves a full trace.
"""
