"use client";

import { useParams } from "next/navigation";
import { useState } from "react";
import { AgentRun, CaseItem, api, useApi } from "@/lib/api";
import { inr, num, pct, when } from "@/lib/format";
import { Badge, Empty, ErrorBox, Json, PageTitle, Panel, StatTile } from "@/components/ui";

interface CaseDetail extends CaseItem {
  dossier: {
    case: Record<string, unknown> & { outstanding_amount: number; touches_used: number; systemic_hold: boolean; human_handoff: boolean };
    customer: Record<string, unknown> & { consent: Record<string, string>; touches_last_30d: Array<Record<string, unknown>> };
    diagnosis: { category: string; explanation: string | null; confidence: number | null; recovery_probability: number | null; risk_score: number | null };
    recommendation: Record<string, unknown>;
    constraints: { action_verdicts: Array<{ action: string; allowed: boolean; rule: string | null; reason: string }>; allowed_actions: string[] };
  };
  ledger: { totals: Record<string, number>; recovered_net_of_refunds: number; settled: number; cost: number; outstanding: number };
}

interface LedgerEntry {
  id: string;
  entry_type: string;
  amount: number;
  channel: string | null;
  provider_reference: string;
  occurred_at: string | null;
}

interface AuditRow {
  sequence: number | null;
  action: string;
  actor_type: string;
  actor_id: string | null;
  entity_type: string;
  metadata: Record<string, unknown>;
  timestamp: string | null;
  row_hash: string | null;
}

export default function CaseDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const { data: c, error, refresh } = useApi<CaseDetail>(`/v2/cases/${id}`);
  const { data: ledger, refresh: refreshLedger } = useApi<{ entries: LedgerEntry[] }>(`/ledger/cases/${id}`);
  const { data: audit, refresh: refreshAudit } = useApi<AuditRow[]>(`/audit/cases/${id}`);
  const { data: runs, refresh: refreshRuns } = useApi<AgentRun[]>(`/agent/runs?case_id=${id}`);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const refreshAll = () => {
    refresh();
    refreshLedger();
    refreshAudit();
    refreshRuns();
  };

  const runAgent = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const run = await api.post<AgentRun>(`/agent/cases/${id}/run`, { dry_run: true });
      setMsg(`Agent run ${run.status}: ${(run.final_plan as { action?: string } | null)?.action}`);
      refreshAll();
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const handoff = async () => {
    setBusy(true);
    try {
      await api.post(`/compliance/handoff/${id}`, { reason: "Operator requested review from Command Center", operator: "operator" });
      refreshAll();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <PageTitle
        title={`Case ${id.slice(0, 8)}`}
        subtitle={c ? `${c.surface} · ${c.customer.name ?? "unknown customer"} · ${c.customer.segment ?? ""}` : undefined}
        actions={
          <>
            <button className="btn primary" onClick={runAgent} disabled={busy || !c || ["RECOVERED", "CLOSED"].includes(c.status)}>
              Run recovery agent
            </button>
            <button className="btn" onClick={handoff} disabled={busy || !c || c.dossier?.case.human_handoff}>
              Hand off to human
            </button>
          </>
        }
      />
      <ErrorBox error={error} />
      {msg && <div className="panel p-3 mb-4 text-sm">{msg}</div>}
      {!c && !error && <Empty text="Loading case…" />}
      {c && (
        <div className="flex flex-col gap-6">
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            <StatTile label="Status" value={c.status} sub={c.experiment_arm ? `${c.experiment_arm} arm` : ""} />
            <StatTile label="Outstanding" value={inr(c.dossier.case.outstanding_amount)} sub={`at risk ${inr(c.amount_at_risk)}`} />
            <StatTile label="Recovered" value={inr(c.ledger.recovered_net_of_refunds)} sub={`settled ${inr(c.ledger.settled)}`} tone={c.ledger.recovered_net_of_refunds > 0 ? "good" : undefined} />
            <StatTile label="Diagnosis" value={c.dossier.diagnosis.category} sub={`p(recover) ${pct(c.dossier.diagnosis.recovery_probability)} · risk ${num(c.dossier.diagnosis.risk_score)}`} />
            <StatTile label="Touches used" value={num(c.dossier.case.touches_used)} sub={c.dossier.case.systemic_hold ? "held: issuer degraded" : c.dossier.case.human_handoff ? "frozen: human handoff" : "automation active"} tone={c.dossier.case.human_handoff ? "warning" : undefined} />
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Panel title="What the agent may do right now" className="lg:col-span-2">
              <table className="data">
                <thead>
                  <tr>
                    <th>Action</th>
                    <th>Verdict</th>
                    <th>Rule / reason</th>
                  </tr>
                </thead>
                <tbody>
                  {c.dossier.constraints.action_verdicts.map((v) => (
                    <tr key={v.action}>
                      <td className="mono text-xs">{v.action}</td>
                      <td>
                        <Badge value={v.allowed ? "APPROVED" : "BLOCKED"} />
                      </td>
                      <td className="text-xs muted">{v.allowed ? "permitted" : `${v.rule}: ${v.reason}`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
            <Panel title="Customer & consent">
              <dl className="grid grid-cols-2 gap-y-1 text-sm">
                <dt className="muted">Phone</dt>
                <dd>{(c.dossier.customer.phone_masked as string) || "—"}</dd>
                <dt className="muted">Email</dt>
                <dd className="truncate">{(c.dossier.customer.email_masked as string) || "—"}</dd>
                <dt className="muted">Language</dt>
                <dd>{c.dossier.customer.preferred_language as string}</dd>
                <dt className="muted">Timezone</dt>
                <dd>{c.dossier.customer.timezone as string}</dd>
              </dl>
              <div className="mt-3 text-xs">
                <div className="muted mb-1">Consent</div>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(c.dossier.customer.consent || {}).map(([ch, st]) => (
                    <span key={ch} className="badge">
                      <span className="dot" aria-hidden style={{ color: st === "OPT_OUT" ? "var(--status-critical)" : "var(--status-good)" }} />
                      {ch}: {st}
                    </span>
                  ))}
                </div>
              </div>
              <div className="mt-3 text-xs">
                <div className="muted mb-1">Rule/ML recommendation</div>
                <div>
                  {String(c.dossier.recommendation.action_type ?? "—")} · EV {inr(c.dossier.recommendation.expected_recovery_value as number)} · p {pct(c.dossier.recommendation.expected_recovery_probability as number)}
                </div>
              </div>
              <div className="mt-3">
                <Json value={c.metadata} />
              </div>
            </Panel>
          </div>

          <Panel title={`Agent runs (${runs?.length ?? 0})`}>
            {!runs || runs.length === 0 ? (
              <Empty text="No agent runs yet. Use “Run recovery agent” to plan the next move." />
            ) : (
              <div className="flex flex-col gap-3">
                {runs.map((r) => (
                  <details key={r.id} className="panel p-3" open={r === runs[0]}>
                    <summary className="flex cursor-pointer flex-wrap items-center gap-3 text-sm">
                      <Badge value={r.status} />
                      <span className="mono text-xs">{(r.final_plan as { action?: string } | null)?.action ?? "—"}</span>
                      <span className="muted text-xs">
                        {r.provider} · {r.model} · {r.turns} turn(s) {r.degraded_to_rules ? "· degraded to rules" : ""} · {when(r.started_at)}
                      </span>
                      <span className="muted text-xs ml-auto">
                        {num(r.input_tokens)}/{num(r.output_tokens)} tokens · ${r.cost_usd}
                      </span>
                    </summary>
                    <p className="mt-2 text-sm">{String((r.final_plan as { rationale?: string } | null)?.rationale ?? "")}</p>
                    {r.validation && !r.validation.ok && <p className="mt-1 text-xs" style={{ color: "var(--status-serious)" }}>Validation: {r.validation.errors.join("; ")}</p>}
                    <ol className="mt-2 flex flex-col gap-1 text-xs">
                      {r.trace.map((t, i) => (
                        <li key={i} className="flex gap-2">
                          <span className="mono muted w-40 shrink-0">{String(t.event)}</span>
                          <span className="truncate">
                            {t.tool ? `${String(t.tool)} → ${t.ok ? "ok" : `blocked ${String(t.blocked_rule ?? t.error ?? "")}`}` : t.text ? String(t.text).slice(0, 160) : t.reason ? String(t.reason) : t.action ? `${String(t.action)} ${t.ok ? "ok" : "blocked"}` : ""}
                          </span>
                        </li>
                      ))}
                    </ol>
                    <div className="mt-2">
                      <Json value={{ final_plan: r.final_plan, execution_result: r.execution_result }} />
                    </div>
                  </details>
                ))}
              </div>
            )}
          </Panel>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="Ledger">
              <table className="data">
                <thead>
                  <tr>
                    <th>Type</th>
                    <th className="num">Amount</th>
                    <th>Channel</th>
                    <th>Reference</th>
                    <th>When</th>
                  </tr>
                </thead>
                <tbody>
                  {(ledger?.entries || []).map((e) => (
                    <tr key={e.id}>
                      <td className="text-xs">{e.entry_type}</td>
                      <td className="num">{inr(e.amount)}</td>
                      <td className="text-xs">{e.channel || "—"}</td>
                      <td className="mono text-xs truncate max-w-40">{e.provider_reference}</td>
                      <td className="text-xs muted">{when(e.occurred_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
            <Panel title={`Audit trail (${audit?.length ?? 0} rows, hash-chained)`}>
              <div className="max-h-[480px] overflow-auto">
                <table className="data">
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>Action</th>
                      <th>Actor</th>
                      <th>Hash</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(audit || []).map((a, i) => (
                      <tr key={`${a.sequence}-${i}`}>
                        <td className="num muted text-xs">{a.sequence ?? "legacy"}</td>
                        <td className="text-xs">
                          <div>{a.action}</div>
                          <div className="muted">{when(a.timestamp)}</div>
                        </td>
                        <td className="text-xs">
                          {a.actor_type}
                          <div className="muted truncate max-w-40">{a.actor_id}</div>
                        </td>
                        <td className="mono text-xs muted">{a.row_hash ? a.row_hash.slice(0, 10) : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          </div>
        </div>
      )}
    </div>
  );
}
