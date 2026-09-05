"""Render a replay report as Markdown for the README / pitch deck."""
from __future__ import annotations

from typing import Any, Dict


def _rs(x: Any) -> str:
    try:
        return f"Rs {float(x):,.2f}"
    except Exception:
        return str(x)


def render_markdown(report: Dict[str, Any]) -> str:
    sc = report.get("scorecard", {})
    arms = sc.get("arms", {})
    t, h = arms.get("TREATMENT", {}), arms.get("HOLDOUT", {})
    lift = sc.get("lift", {})
    comp = sc.get("compliance", {})
    audit = sc.get("audit", {})
    costs = sc.get("costs", {})
    agent = report.get("agent", {})
    stats = report.get("replay_stats", {})
    scen = report.get("scenario", {})

    lines = [
        f"# Batch replay report - `{report.get('batch_id')}`",
        "",
        f"Seed {report['config'].get('seed')} | {scen.get('cases')} cases over {report['config'].get('days')} virtual days | holdout {report['config'].get('holdout_percent')}% | agent {'on' if agent.get('enabled') else 'off'} ({agent.get('provider')}) | wall clock {stats.get('wall_seconds')}s",
        "",
        "## Money recovered (the bar)",
        "",
        "| Arm | Cases | Recovered cases | Case recovery rate | At risk | Recovered | Settled |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| Treatment | {t.get('cases', 0)} | {t.get('recovered_cases', 0)} | {t.get('case_recovery_rate', 0):.1%} | {_rs(t.get('amount_at_risk', 0))} | {_rs(t.get('amount_recovered', 0))} | {_rs(t.get('amount_settled', 0))} |",
        f"| Holdout | {h.get('cases', 0)} | {h.get('recovered_cases', 0)} | {h.get('case_recovery_rate', 0):.1%} | {_rs(h.get('amount_at_risk', 0))} | {_rs(h.get('amount_recovered', 0))} | {_rs(h.get('amount_settled', 0))} |",
        "",
    ]
    if lift.get("available"):
        ci = lift.get("incremental_rupees_ci95", [None, None])
        rel = lift.get("relative_lift_case_rate")
        rel_txt = f"{rel:.0%}" if rel is not None else "n/a"
        lines += [
            f"**Incremental recovery attributable to the system: {_rs(lift.get('incremental_rupees_recovered'))}** "
            f"(95% CI {_rs(ci[0])} to {_rs(ci[1])}); lift +{lift.get('absolute_lift_case_rate', 0) * 100:.1f} pts "
            f"(relative {rel_txt}), z = {lift.get('z_statistic')}, p = {lift.get('p_value')}.",
            "",
        ]
    lines += [
        "## Compliance and audit",
        "",
        f"- Policy violations: **{comp.get('policy_violations', 0)}** (holdout outreach {comp.get('holdout_outreach_count', 0)}, outreach after recovery {comp.get('stopping_rule_breaches', 0)})",
        f"- Policy blocks enforced: {comp.get('policy_blocks_enforced', 0)} plan steps; contact-policy blocks by rule: {comp.get('contact_blocks_by_rule', {})}",
        f"- Audit coverage of executed actions: **{audit.get('coverage', 0):.0%}** ({audit.get('audited_actions', 0)}/{audit.get('executed_actions', 0)}), {audit.get('audit_rows', 0)} audit rows, hash chain {'intact' if report.get('audit_chain', {}).get('ok') else 'BROKEN'}",
        f"- Duplicate webhooks injected: {stats.get('duplicates_injected', 0)}, side effects: **{stats.get('duplicate_side_effects', 0)}**; opt-outs honoured: {stats.get('opt_outs', 0)}",
        "",
        "## Payment degradation",
        "",
        f"- Incidents detected: {report.get('degradation', {}).get('incidents', 0)} {report.get('degradation', {}).get('cells', [])} · status {report.get('degradation', {}).get('by_status', {})} · cases held {report.get('degradation', {}).get('affected_cases', 0)} · failures diagnosed systemic (no customer contact): {report.get('degradation', {}).get('systemic_diagnoses', 0)}",
        "",
        "## Agent",
        "",
        f"- Runs: {agent.get('runs', 0)}; degraded to rules during the simulated LLM outage: {agent.get('degraded_to_rules', 0)} (refusals {agent.get('outage_refusals', 0)}); approvals: {agent.get('approvals', {})}",
        f"- Actions chosen: {agent.get('by_action', {})}",
        f"- Tokens in/out: {agent.get('input_tokens', 0)}/{agent.get('output_tokens', 0)}; cost USD {agent.get('cost_usd', 0)}",
        "",
        "## Cost and timing",
        "",
        f"- Outreach cost: {_rs(costs.get('total_cost', 0))}; cost per recovered rupee: {costs.get('cost_per_recovered_rupee')}; ROI multiple: {costs.get('roi_multiple')}",
        f"- Time to recovery (hours): {sc.get('timing', {}).get('time_to_recovery_hours')}",
        f"- Settled: {_rs(report.get('settlement', {}).get('settled_amount', 0))} across {report.get('settlement', {}).get('matched_count', 0)} payments",
        "",
        "## By surface",
        "",
        "| Surface | Cases | Recovered | Rate | At risk | Recovered |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for surface, row in sc.get("breakdowns", {}).get("by_surface", {}).items():
        lines.append(f"| {surface} | {row['cases']} | {row['recovered_cases']} | {row['case_recovery_rate']:.1%} | {_rs(row['amount_at_risk'])} | {_rs(row['amount_recovered'])} |")
    lines += ["", "## Lift by surface (treatment vs holdout)", "", "| Surface | Treatment (rec/cases, rate) | Holdout (rec/cases, rate) | Lift |", "|---|---:|---:|---:|"]
    for surface, row in sc.get("breakdowns", {}).get("lift_by_surface", {}).items():
        t_rate = f"{row['treatment_rate']:.1%}" if row.get("treatment_rate") is not None else "n/a"
        h_rate = f"{row['holdout_rate']:.1%}" if row.get("holdout_rate") is not None else "n/a"
        lift_txt = f"{row['absolute_lift'] * 100:+.1f} pts" if row.get("absolute_lift") is not None else "n/a"
        lines.append(f"| {surface} | {row['treatment_recovered']}/{row['treatment_cases']}, {t_rate} | {row['holdout_recovered']}/{row['holdout_cases']}, {h_rate} | {lift_txt} |")
    lines += ["", "## By root cause", "", "| Root cause | Cases | Recovered | Rate |", "|---|---:|---:|---:|"]
    for cause, row in sc.get("breakdowns", {}).get("by_root_cause", {}).items():
        lines.append(f"| {cause} | {row['cases']} | {row['recovered_cases']} | {row['case_recovery_rate']:.1%} |")
    if sc.get("warnings"):
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in sc["warnings"]]
    lines += ["", "_Methodology:_ " + "; ".join(f"{k}: {v}" for k, v in sc.get("methodology", {}).items()), ""]
    return "\n".join(lines)
