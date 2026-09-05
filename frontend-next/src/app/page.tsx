"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { Scorecard, useApi } from "@/lib/api";
import { inr, num, pct } from "@/lib/format";
import { Badge, Empty, ErrorBox, Legend, Meter, PageTitle, Panel, StatTile } from "@/components/ui";

interface Batch {
  batch_id: string;
  cases: number;
  first_case_at: string | null;
  last_case_at: string | null;
}

export default function Overview() {
  const { data: batches } = useApi<Batch[]>("/scorecard/batches");
  const [batch, setBatch] = useState<string>("");
  const scope = useMemo(() => (batch ? `?batch_id=${encodeURIComponent(batch)}` : ""), [batch]);
  const { data: sc, error, loading } = useApi<Scorecard>(`/scorecard${scope}`);
  const { data: approvals } = useApi<Array<{ id: string }>>("/approvals?status=PENDING");
  const { data: incidents } = useApi<Array<{ id: string; status: string; bank: string; payment_method: string }>>("/degradation/incidents");

  const t = sc?.arms?.TREATMENT;
  const h = sc?.arms?.HOLDOUT;
  const active = (incidents || []).filter((i) => i.status === "SUSPECTED" || i.status === "CONFIRMED");

  return (
    <div>
      <PageTitle
        title="Revenue recovered, measured"
        subtitle="Every case is randomly assigned to treatment or holdout. The number that matters is the incremental recovery the system caused, not what customers would have paid anyway."
        actions={
          <select value={batch} onChange={(e) => setBatch(e.target.value)} aria-label="Batch">
            <option value="">All cases</option>
            {(batches || []).map((b) => (
              <option key={b.batch_id} value={b.batch_id}>
                {b.batch_id} ({b.cases})
              </option>
            ))}
          </select>
        }
      />
      <ErrorBox error={error} />
      {loading && !sc && <Empty text="Loading scorecard…" />}
      {sc && (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            <StatTile label="Incremental recovered" value={inr(sc.lift.incremental_rupees_recovered, { compact: true })} sub={sc.lift.available ? `95% CI ${inr(sc.lift.incremental_rupees_ci95?.[0], { compact: true })} – ${inr(sc.lift.incremental_rupees_ci95?.[1], { compact: true })}` : sc.lift.reason} tone="good" />
            <StatTile label="Treatment recovery" value={pct(t?.case_recovery_rate)} sub={`${t?.recovered_cases ?? 0} of ${t?.cases ?? 0} cases`} />
            <StatTile label="Holdout recovery" value={pct(h?.case_recovery_rate)} sub={`${h?.recovered_cases ?? 0} of ${h?.cases ?? 0} cases`} />
            <StatTile label="Policy violations" value={num(sc.compliance.policy_violations)} sub={`${num(sc.compliance.policy_blocks_enforced)} blocks enforced`} tone={sc.compliance.policy_violations === 0 ? "good" : "critical"} />
            <StatTile label="Audit coverage" value={pct(sc.audit.coverage, 0)} sub={`${num(sc.audit.audit_rows)} rows · chain ${sc.compliance.audit_chain?.ok ? "intact" : "BROKEN"}`} tone={sc.audit.coverage >= 1 ? "good" : "warning"} />
          </div>

          <div className="mt-6 grid gap-4 lg:grid-cols-3">
            <Panel title="Recovery rate by arm" className="lg:col-span-2">
              <Legend items={[{ label: "Treatment", color: "var(--series-1)" }, { label: "Holdout", color: "var(--series-2)" }]} />
              <div className="mt-2">
                <Meter label="Treatment (cases)" value={t?.case_recovery_rate ?? 0} color="var(--series-1)" valueLabel={pct(t?.case_recovery_rate)} />
                <Meter label="Holdout (cases)" value={h?.case_recovery_rate ?? 0} color="var(--series-2)" valueLabel={pct(h?.case_recovery_rate)} />
                <Meter label="Treatment (rupees)" value={t?.rupee_recovery_rate ?? 0} color="var(--series-1)" valueLabel={pct(t?.rupee_recovery_rate)} />
                <Meter label="Holdout (rupees)" value={h?.rupee_recovery_rate ?? 0} color="var(--series-2)" valueLabel={pct(h?.rupee_recovery_rate)} />
              </div>
              {sc.lift.available && (
                <p className="mt-3 text-sm">
                  {sc.lift.interpretation} <span className="muted">z = {sc.lift.z_statistic}, p = {sc.lift.p_value}{sc.lift.significant_at_5pct ? " (significant at 5%)" : ""}.</span>
                </p>
              )}
              {sc.warnings.length > 0 && (
                <ul className="mt-2 list-disc pl-5 text-xs muted">
                  {sc.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              )}
            </Panel>
            <Panel title="Needs a human">
              <div className="flex flex-col gap-3 text-sm">
                <Link href="/approvals" className="flex items-center justify-between">
                  <span>Pending approvals</span>
                  <span className="font-semibold tabular-nums">{approvals ? approvals.length : "—"}</span>
                </Link>
                <Link href="/degradation" className="flex items-center justify-between">
                  <span>Active issuer incidents</span>
                  <span className="font-semibold tabular-nums">{incidents ? active.length : "—"}</span>
                </Link>
                {active.slice(0, 3).map((i) => (
                  <div key={i.id} className="flex items-center justify-between text-xs">
                    <span>
                      {i.bank} / {i.payment_method}
                    </span>
                    <Badge value={i.status} />
                  </div>
                ))}
                <div className="flex items-center justify-between">
                  <span>Settled of recovered</span>
                  <span className="font-semibold tabular-nums">{pct(t?.settlement_coverage, 0)}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span>Cost per recovered rupee</span>
                  <span className="font-semibold tabular-nums">{sc.costs.cost_per_recovered_rupee ?? "—"}</span>
                </div>
              </div>
            </Panel>
          </div>

          <div className="mt-6 grid gap-4 lg:grid-cols-2">
            <Panel title="By leak surface">
              <table className="data">
                <thead>
                  <tr>
                    <th>Surface</th>
                    <th className="num">Cases</th>
                    <th className="num">Recovered</th>
                    <th className="num">Rate</th>
                    <th className="num">At risk</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(sc.breakdowns.by_surface || {}).map(([k, v]) => (
                    <tr key={k}>
                      <td>{k}</td>
                      <td className="num">{num(v.cases)}</td>
                      <td className="num">{num(v.recovered_cases)}</td>
                      <td className="num">{pct(v.case_recovery_rate)}</td>
                      <td className="num">{inr(v.amount_at_risk, { compact: true })}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
            <Panel title="By root cause">
              <table className="data">
                <thead>
                  <tr>
                    <th>Root cause</th>
                    <th className="num">Cases</th>
                    <th className="num">Recovered</th>
                    <th className="num">Rate</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(sc.breakdowns.by_root_cause || {}).map(([k, v]) => (
                    <tr key={k}>
                      <td>{k}</td>
                      <td className="num">{num(v.cases)}</td>
                      <td className="num">{num(v.recovered_cases)}</td>
                      <td className="num">{pct(v.case_recovery_rate)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
