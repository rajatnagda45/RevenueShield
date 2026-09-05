"use client";

import { useMemo, useState } from "react";
import { Scorecard, useApi } from "@/lib/api";
import { inr, num, pct } from "@/lib/format";
import { Empty, ErrorBox, Json, PageTitle, Panel, StatTile } from "@/components/ui";

interface Batch {
  batch_id: string;
  cases: number;
}

export default function ScorecardPage() {
  const { data: batches } = useApi<Batch[]>("/scorecard/batches");
  const [batch, setBatch] = useState("");
  const [surface, setSurface] = useState("");
  const qs = useMemo(() => {
    const p = new URLSearchParams();
    if (batch) p.set("batch_id", batch);
    if (surface) p.set("surface", surface);
    const s = p.toString();
    return s ? `?${s}` : "";
  }, [batch, surface]);
  const { data: sc, error, loading } = useApi<Scorecard>(`/scorecard${qs}`);
  const t = sc?.arms?.TREATMENT;
  const h = sc?.arms?.HOLDOUT;

  return (
    <div>
      <PageTitle
        title="Batch scorecard"
        subtitle="The artefact judges asked for: money recovered across a batch, split by experiment arm, with compliance and audit evidence."
        actions={
          <>
            <select value={surface} onChange={(e) => setSurface(e.target.value)} aria-label="Surface">
              <option value="">All surfaces</option>
              {["PAYMENT_FAILURE", "SUBSCRIPTION_MANDATE_FAILURE", "CHECKOUT_ABANDONMENT", "RECEIVABLE_OVERDUE"].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <select value={batch} onChange={(e) => setBatch(e.target.value)} aria-label="Batch">
              <option value="">All batches</option>
              {(batches || []).map((b) => (
                <option key={b.batch_id} value={b.batch_id}>
                  {b.batch_id} ({b.cases})
                </option>
              ))}
            </select>
          </>
        }
      />
      <ErrorBox error={error} />
      {loading && !sc && <Empty text="Computing…" />}
      {sc && (
        <div className="flex flex-col gap-6">
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <StatTile label="Cases in scope" value={num(sc.scope.cases)} sub={`${t?.cases ?? 0} treatment · ${h?.cases ?? 0} holdout`} />
            <StatTile label="Recovered (treatment)" value={inr(t?.amount_recovered, { compact: true })} sub={`settled ${inr(t?.amount_settled, { compact: true })}`} />
            <StatTile label="Absolute lift" value={sc.lift.available ? `+${((sc.lift.absolute_lift_case_rate ?? 0) * 100).toFixed(1)} pts` : "—"} sub={sc.lift.available ? `p = ${sc.lift.p_value}` : sc.lift.reason} tone={sc.lift.significant_at_5pct ? "good" : undefined} />
            <StatTile label="Incremental rupees" value={inr(sc.lift.incremental_rupees_recovered, { compact: true })} sub={sc.lift.available ? `CI ${inr(sc.lift.incremental_rupees_ci95?.[0], { compact: true })} – ${inr(sc.lift.incremental_rupees_ci95?.[1], { compact: true })}` : ""} tone="good" />
          </div>

          <Panel title="Arms">
            <table className="data">
              <thead>
                <tr>
                  <th>Arm</th>
                  <th className="num">Cases</th>
                  <th className="num">Recovered</th>
                  <th className="num">Case rate</th>
                  <th className="num">95% CI</th>
                  <th className="num">At risk</th>
                  <th className="num">Recovered ₹</th>
                  <th className="num">Rupee rate</th>
                  <th className="num">Settled</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(sc.arms).map(([arm, a]) => (
                  <tr key={arm}>
                    <td>{arm}</td>
                    <td className="num">{num(a.cases)}</td>
                    <td className="num">{num(a.recovered_cases)}</td>
                    <td className="num">{pct(a.case_recovery_rate)}</td>
                    <td className="num">
                      {pct(a.case_recovery_rate_ci95[0])} – {pct(a.case_recovery_rate_ci95[1])}
                    </td>
                    <td className="num">{inr(a.amount_at_risk)}</td>
                    <td className="num">{inr(a.amount_recovered)}</td>
                    <td className="num">{pct(a.rupee_recovery_rate)}</td>
                    <td className="num">{inr(a.amount_settled)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>

          <div className="grid gap-4 lg:grid-cols-3">
            <Panel title="Compliance">
              <dl className="grid grid-cols-2 gap-y-2 text-sm">
                <dt className="muted">Policy violations</dt>
                <dd className="tabular-nums font-semibold" style={{ color: sc.compliance.policy_violations ? "var(--status-critical)" : "var(--status-good)" }}>
                  {num(sc.compliance.policy_violations)}
                </dd>
                <dt className="muted">Holdout outreach</dt>
                <dd className="tabular-nums">{num(sc.compliance.holdout_outreach_count)}</dd>
                <dt className="muted">Outreach after recovery</dt>
                <dd className="tabular-nums">{num(sc.compliance.stopping_rule_breaches)}</dd>
                <dt className="muted">Blocks enforced</dt>
                <dd className="tabular-nums">{num(sc.compliance.policy_blocks_enforced)}</dd>
              </dl>
              {sc.compliance.contact_blocks_by_rule && Object.keys(sc.compliance.contact_blocks_by_rule).length > 0 && (
                <div className="mt-3 text-xs">
                  <div className="muted mb-1">Contact-policy blocks by rule</div>
                  {Object.entries(sc.compliance.contact_blocks_by_rule).map(([r, n]) => (
                    <div key={r} className="flex justify-between">
                      <span className="mono">{r}</span>
                      <span className="tabular-nums">{n}</span>
                    </div>
                  ))}
                </div>
              )}
            </Panel>
            <Panel title="Audit">
              <dl className="grid grid-cols-2 gap-y-2 text-sm">
                <dt className="muted">Executed actions</dt>
                <dd className="tabular-nums">{num(sc.audit.executed_actions)}</dd>
                <dt className="muted">With audit rows</dt>
                <dd className="tabular-nums">{num(sc.audit.audited_actions)}</dd>
                <dt className="muted">Coverage</dt>
                <dd className="tabular-nums font-semibold">{pct(sc.audit.coverage, 0)}</dd>
                <dt className="muted">Hash chain</dt>
                <dd>{sc.compliance.audit_chain?.ok ? "intact" : "BROKEN"} ({num(sc.compliance.audit_chain?.chained_rows)} rows)</dd>
              </dl>
            </Panel>
            <Panel title="Cost & timing">
              <dl className="grid grid-cols-2 gap-y-2 text-sm">
                <dt className="muted">Outreach cost</dt>
                <dd className="tabular-nums">{inr(sc.costs.total_cost)}</dd>
                <dt className="muted">Per recovered ₹</dt>
                <dd className="tabular-nums">{sc.costs.cost_per_recovered_rupee ?? "—"}</dd>
                <dt className="muted">ROI multiple</dt>
                <dd className="tabular-nums">{sc.costs.roi_multiple ?? "—"}</dd>
                <dt className="muted">TTR p50 / p90 (h)</dt>
                <dd className="tabular-nums">
                  {sc.timing.time_to_recovery_hours?.p50 ?? "—"} / {sc.timing.time_to_recovery_hours?.p90 ?? "—"}
                </dd>
              </dl>
              <div className="mt-3 text-xs">
                {Object.entries(sc.costs.by_channel).map(([c, v]) => (
                  <div key={c} className="flex justify-between">
                    <span>{c}</span>
                    <span className="tabular-nums">
                      {num(v.actions)} · {inr(v.cost)}
                    </span>
                  </div>
                ))}
              </div>
            </Panel>
          </div>

          {sc.breakdowns.lift_by_surface && (
            <Panel title="Lift by surface (treatment vs holdout)">
              <table className="data">
                <thead>
                  <tr>
                    <th>Surface</th>
                    <th className="num">Treatment</th>
                    <th className="num">Holdout</th>
                    <th className="num">Lift</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(sc.breakdowns.lift_by_surface as unknown as Record<string, { treatment_cases: number; treatment_recovered: number; treatment_rate: number | null; holdout_cases: number; holdout_recovered: number; holdout_rate: number | null; absolute_lift: number | null }>).map(([k, v]) => (
                    <tr key={k}>
                      <td>{k}</td>
                      <td className="num">
                        {v.treatment_recovered}/{v.treatment_cases} · {pct(v.treatment_rate)}
                      </td>
                      <td className="num">
                        {v.holdout_recovered}/{v.holdout_cases} · {pct(v.holdout_rate)}
                      </td>
                      <td className="num" style={{ color: v.absolute_lift === null ? undefined : v.absolute_lift >= 0 ? "var(--status-good)" : "var(--status-serious)" }}>
                        {v.absolute_lift === null ? "n/a" : `${v.absolute_lift >= 0 ? "+" : ""}${(v.absolute_lift * 100).toFixed(1)} pts`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
          )}

          <div className="grid gap-4 lg:grid-cols-2">
            {(["by_surface", "by_root_cause", "by_status", "by_arm"] as const).map((key) => (
              <Panel key={key} title={key.replace("by_", "By ").replace("_", " ")}>
                <table className="data">
                  <thead>
                    <tr>
                      <th>Segment</th>
                      <th className="num">Cases</th>
                      <th className="num">Recovered</th>
                      <th className="num">Rate</th>
                      <th className="num">At risk</th>
                      <th className="num">Recovered ₹</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(sc.breakdowns[key] || {}).map(([k, v]) => (
                      <tr key={k}>
                        <td>{k}</td>
                        <td className="num">{num(v.cases)}</td>
                        <td className="num">{num(v.recovered_cases)}</td>
                        <td className="num">{pct(v.case_recovery_rate)}</td>
                        <td className="num">{inr(v.amount_at_risk, { compact: true })}</td>
                        <td className="num">{inr(v.amount_recovered, { compact: true })}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Panel>
            ))}
          </div>

          <Panel title="Methodology">
            <ul className="text-xs muted list-disc pl-5">
              {Object.entries(sc.methodology).map(([k, v]) => (
                <li key={k}>
                  <strong>{k}:</strong> {v}
                </li>
              ))}
            </ul>
            <div className="mt-3">
              <Json value={sc} />
            </div>
          </Panel>
        </div>
      )}
    </div>
  );
}
