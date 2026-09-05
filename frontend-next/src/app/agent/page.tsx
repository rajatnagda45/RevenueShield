"use client";

import Link from "next/link";
import { useState } from "react";
import { AgentRun, useApi } from "@/lib/api";
import { num, short, when } from "@/lib/format";
import { Badge, Empty, ErrorBox, PageTitle, Panel, StatTile } from "@/components/ui";

interface ToolSpec {
  name: string;
  description: string;
  mutating: boolean;
}

export default function AgentPage() {
  const [status, setStatus] = useState("");
  const { data: runs, error } = useApi<AgentRun[]>(`/agent/runs?limit=200${status ? `&status=${status}` : ""}`);
  const { data: tools } = useApi<ToolSpec[]>("/agent/tools");

  const total = runs?.length ?? 0;
  const degraded = runs?.filter((r) => r.degraded_to_rules).length ?? 0;
  const byAction: Record<string, number> = {};
  for (const r of runs || []) {
    const a = (r.final_plan as { action?: string } | null)?.action ?? "—";
    byAction[a] = (byAction[a] || 0) + 1;
  }
  const cost = (runs || []).reduce((s, r) => s + r.cost_usd, 0);

  return (
    <div>
      <PageTitle
        title="Recovery agent"
        subtitle="An LLM plans inside a bounded tool surface; deterministic validators and the policy engine decide what may execute. Every run is traced."
        actions={
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
            <option value="">All statuses</option>
            {["EXECUTED", "AWAITING_APPROVAL", "BLOCKED", "HANDOFF", "NO_ACTION", "FAILED"].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        }
      />
      <ErrorBox error={error} />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile label="Runs" value={num(total)} />
        <StatTile label="Degraded to rules" value={num(degraded)} sub="LLM unavailable → deterministic planner" tone={degraded ? "warning" : undefined} />
        <StatTile label="Distinct actions" value={num(Object.keys(byAction).length)} sub={Object.entries(byAction).map(([k, v]) => `${k} ${v}`).join(" · ")} />
        <StatTile label="LLM cost" value={`$${cost.toFixed(4)}`} sub="list prices, from usage in each trace" />
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-3">
        <Panel title="Runs" className="lg:col-span-2">
          {runs && runs.length === 0 && <Empty text="No runs yet." />}
          <table className="data">
            <thead>
              <tr>
                <th>When</th>
                <th>Case</th>
                <th>Status</th>
                <th>Action</th>
                <th>Planner</th>
                <th className="num">Turns</th>
              </tr>
            </thead>
            <tbody>
              {(runs || []).map((r) => (
                <tr key={r.id}>
                  <td className="text-xs muted">{when(r.started_at)}</td>
                  <td>
                    <Link href={`/cases/${r.case_id}`} className="mono text-xs underline">
                      {short(r.case_id)}
                    </Link>
                  </td>
                  <td>
                    <Badge value={r.status} />
                  </td>
                  <td className="mono text-xs">{(r.final_plan as { action?: string } | null)?.action ?? "—"}</td>
                  <td className="text-xs">
                    {r.provider}
                    {r.degraded_to_rules ? " (degraded)" : ""}
                  </td>
                  <td className="num">{r.turns}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
        <Panel title="Bounded tool surface">
          <ul className="flex flex-col gap-2 text-xs">
            {(tools || []).map((t) => (
              <li key={t.name}>
                <span className="mono font-semibold">{t.name}</span> <span className="muted">{t.mutating ? "· mutating (policy-gated)" : "· read-only"}</span>
                <div className="muted">{t.description}</div>
              </li>
            ))}
          </ul>
        </Panel>
      </div>
    </div>
  );
}
