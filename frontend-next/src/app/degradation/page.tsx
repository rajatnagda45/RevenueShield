"use client";

import { useState } from "react";
import { api, useApi } from "@/lib/api";
import { num, pct, when } from "@/lib/format";
import { Badge, Empty, ErrorBox, PageTitle, Panel } from "@/components/ui";

interface Cell {
  bank: string;
  payment_method: string;
  total: number;
  failed: number;
  failure_rate: number;
  baseline_failure_rate: number;
  baseline_source: string;
  z_score: number;
  degraded: boolean;
}
interface Health {
  reference_time: string;
  window_minutes: number;
  baseline_days: number;
  global_baseline_failure_rate: number;
  cells: Cell[];
}
interface Incident {
  id: string;
  bank: string;
  payment_method: string;
  status: string;
  opened_at: string | null;
  closed_at: string | null;
  observed_failure_rate: number;
  baseline_failure_rate: number;
  z_score: number;
  affected_case_count: number;
  history: Array<{ at: string; failure_rate: number; z: number }>;
}

/** Sequential single-hue ramp (blue, light -> dark) for magnitude; degraded cells get a status ring, not a hue. */
function shade(rate: number): string {
  if (rate >= 0.6) return "var(--seq-700)";
  if (rate >= 0.4) return "var(--seq-550)";
  if (rate >= 0.2) return "var(--seq-400)";
  if (rate >= 0.08) return "var(--seq-250)";
  return "var(--seq-100)";
}

export default function DegradationPage() {
  const { data: health, error, refresh } = useApi<Health>("/degradation/health");
  const { data: incidents, refresh: refreshInc } = useApi<Incident[]>("/degradation/incidents");
  const [busy, setBusy] = useState(false);

  const tick = async () => {
    setBusy(true);
    try {
      await api.post("/degradation/evaluate", {});
      refresh();
      refreshInc();
    } finally {
      setBusy(false);
    }
  };

  const banks = Array.from(new Set((health?.cells || []).map((c) => c.bank))).sort();
  const methods = Array.from(new Set((health?.cells || []).map((c) => c.payment_method))).sort();
  const cell = (b: string, m: string) => health?.cells.find((c) => c.bank === b && c.payment_method === m);

  return (
    <div>
      <PageTitle
        title="Payment degradation"
        subtitle="Is this failure the customer's problem or the bank's? Failure rate per issuer and method in the current window against a trailing baseline. Degraded cells hold retries and outreach instead of pinging customers."
        actions={
          <button className="btn primary" onClick={tick} disabled={busy}>
            Run monitoring tick
          </button>
        }
      />
      <ErrorBox error={error} />
      <div className="grid gap-4 lg:grid-cols-3">
        <Panel title={`Failure-rate matrix · last ${health?.window_minutes ?? 15} min vs ${health?.baseline_days ?? 7}-day baseline`} className="lg:col-span-2">
          {health && health.cells.length === 0 && <Empty text="No payments in the current window." />}
          {health && health.cells.length > 0 && (
            <div className="overflow-x-auto">
              <table className="data">
                <thead>
                  <tr>
                    <th>Bank</th>
                    {methods.map((m) => (
                      <th key={m}>{m}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {banks.map((b) => (
                    <tr key={b}>
                      <td className="font-semibold">{b}</td>
                      {methods.map((m) => {
                        const c = cell(b, m);
                        if (!c) return <td key={m} className="muted text-xs">—</td>;
                        return (
                          <td key={m}>
                            <div
                              className="rounded-md px-2 py-1 text-xs"
                              title={`${b} ${m}: ${c.failed}/${c.total} failed (${pct(c.failure_rate)}), baseline ${pct(c.baseline_failure_rate)} (${c.baseline_source}), z=${c.z_score}`}
                              style={{
                                background: shade(c.failure_rate),
                                color: c.failure_rate >= 0.2 ? "#fff" : "var(--text-primary)",
                                outline: c.degraded ? "2px solid var(--status-critical)" : "none",
                                outlineOffset: 2,
                              }}
                            >
                              <div className="tabular-nums font-semibold">{pct(c.failure_rate)}</div>
                              <div style={{ opacity: 0.85 }}>
                                {c.failed}/{c.total} · z {c.z_score}
                                {c.degraded ? " · DEGRADED" : ""}
                              </div>
                            </div>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted mt-2 text-xs">
                Colour is failure rate (light → dark, single hue). A red ring marks a cell that passed the detection thresholds (n ≥ 20, z ≥ 3, +15 pts). Global baseline {pct(health.global_baseline_failure_rate)}.
              </p>
            </div>
          )}
        </Panel>
        <Panel title="Incidents">
          {incidents && incidents.length === 0 && <Empty text="No incidents recorded." />}
          <div className="flex flex-col gap-3">
            {(incidents || []).map((i) => (
              <div key={i.id} className="panel p-3 text-sm">
                <div className="flex items-center justify-between">
                  <span className="font-semibold">
                    {i.bank} / {i.payment_method}
                  </span>
                  <Badge value={i.status} />
                </div>
                <div className="muted text-xs mt-1">
                  opened {when(i.opened_at)} {i.closed_at ? `· closed ${when(i.closed_at)}` : ""}
                </div>
                <div className="text-xs mt-1">
                  {pct(i.observed_failure_rate)} vs baseline {pct(i.baseline_failure_rate)} · z {i.z_score} · {num(i.affected_case_count)} cases held
                </div>
                {i.history?.length > 1 && (
                  <div className="mt-2 flex items-end gap-0.5" aria-label="Failure rate history" role="img">
                    {i.history.slice(-24).map((h, idx) => (
                      <div key={idx} title={`${h.at}: ${pct(h.failure_rate)}`} className="w-1.5" style={{ height: `${Math.max(2, h.failure_rate * 40)}px`, background: "var(--series-1)", borderRadius: "2px 2px 0 0" }} />
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}
