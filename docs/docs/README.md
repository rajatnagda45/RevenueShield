# Project Documentation

This directory contains architecture records, configuration guides, and design specifications for the Revenue Recovery AI platform.

## Documentation Index

- [Database Architecture & Core Data Model](database.md): Entity relationship models, schema lifecycle, and database configuration.
- [Razorpay Test Mode Setup & Webhook Guide](razorpay-setup.md): Complete guide to configuring API keys, local HTTPS tunneling, and webhook secrets.
- [Webhook & Event Ingestion Pipeline](event-ingestion.md): In-depth architectural breakdown of HMAC signature verification, idempotency, event normalization, and RecoveryCase transitions.
- [Diagnosis & Root Cause Engine](diagnosis-engine.md): Architecture of the rule-based diagnosis engine (v1), controlled root cause taxonomy, revenue risk scoring, baseline recovery probability prediction, and customer feature store.
- [Recovery Decision & Next Best Action Engine](recovery-decision-engine.md): Multi-factor scoring for candidate actions, ranked alternatives, explainable supporting factors, and future ML integration path.
- [Policy & Compliance Engine](policy-engine.md): Regulatory compliance rules, quiet hours (20:00–08:00), attempt limits, promise-to-pay pauses, and payment capture stopping rules.
- [Bounded Recovery Execution Engine](execution-engine.md): Execution lifecycle, pre-flight safety guard (10 checks), dual-mode execution (dry-run & Razorpay test mode), idempotency guarantees, and money tracking invariants.
- [Recovery Outcome & Learning Data Engine](learning-data-engine.md): Feedback loop architecture, conservative causal attribution, time-to-recovery calculation, point-in-time feature snapshotting, anti-leakage validation, and future value-based ML optimization formulation.
- [Production Analytics SQL Queries](analytics-queries.sql): Ready-to-run PostgreSQL queries for revenue recovery rates, action performance, attribution breakdowns, and training data health.
- [Razorpay Historical Data Ingestion](razorpay-ingestion.md): Historical batch synchronization architecture, pagination, rate limits, state monotonicity guards, and unified webhook/API normalization.
- [Razorpay Ingested Data Analytics Queries](razorpay-data-queries.sql): Ready-to-run PostgreSQL queries for analyzing payment volumes, failure distributions, issuing banks, error steps, and recovery conversion.
- [Predictive Recovery Engine & Expected Value Optimization](predictive-recovery-engine.md): ML feature schemas, training pipelines, data sufficiency gates, model registry lifecycle, action scoring, and fallback heuristics.
- [Smart Payment Recovery Intervention](smart-intervention.md): End-to-end payment link intervention architecture, Razorpay payment link client, notification abstraction, stopping rules, and webhook reconciliation.
- [WhatsApp Recovery Agent Engine](whatsapp-recovery.md): Provider-agnostic WhatsApp recovery layer, deterministic English and Hinglish templating, DND quiet hours, cooldown scheduling, idempotency guards, and payment capture stopping rules.
- [Twilio WhatsApp Sandbox Integration](twilio-whatsapp.md): Complete setup guide for real Twilio WhatsApp Sandbox outreach, sandbox recipient safety, and backoff retries.
- [Real-Time Recovery Demonstration Guide](realtime-recovery-demo.md): Step-by-step instructions for demonstrating live payment failure, Twilio WhatsApp dispatch, Razorpay payment capture, and stopping rule validation.
