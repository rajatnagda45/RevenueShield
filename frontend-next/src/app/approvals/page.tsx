"use client";

import Link from "next/link";
import { useState } from "react";
import { Approval, api, useApi } from "@/lib/api";
import { short, when } from "@/lib/format";
import { Badge, Empty, ErrorBox, Json, PageTitle, Panel } from "@/components/ui";

export default function ApprovalsPage() {
  const [status, setStatus] = useState("PENDING");
  const { data, error, refresh } = useApi<Approval[]>(`/approvals?status=${status}`);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState("");

  const decide = async (id: string, verb: "approve" | "reject") => {
    setBusy(id);
    try {
      await api.post(`/approvals/${id}/${verb}`, { operator: "operator@merchant", note: note || undefined, dry_run: true });
      refresh();
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      <PageTitle
        title="Approval queue"
        subtitle="Bounded autonomy: the agent proposes, a human decides for high-value calls, merchant escalations, waivers and closures. Approvals expire after the SLA."
        actions={
          <>
            <input placeholder="Decision note (optional)" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Decision note" />
            <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
              {["PENDING", "APPROVED", "REJECTED", "EXPIRED", "ALL"].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </>
        }
      />
      <ErrorBox error={error} />
      <Panel>
        {data && data.length === 0 && <Empty text="Nothing waiting for a human." />}
        <div className="flex flex-col gap-3">
          {(data || []).map((a) => (
            <div key={a.id} className="panel p-3">
              <div className="flex flex-wrap items-center gap-3">
                <Badge value={a.status} />
                <span className="mono font-semibold">{a.action}</span>
                <Link href={`/cases/${a.case_id}`} className="mono text-xs underline">
                  case {short(a.case_id)}
                </Link>
                <span className="muted text-xs">requested {when(a.requested_at)} · expires {when(a.expires_at)}</span>
                {a.status === "PENDING" && (
                  <span className="ml-auto flex gap-2">
                    <button className="btn primary" disabled={busy === a.id} onClick={() => decide(a.id, "approve")}>
                      Approve
                    </button>
                    <button className="btn" disabled={busy === a.id} onClick={() => decide(a.id, "reject")}>
                      Reject
                    </button>
                  </span>
                )}
              </div>
              <p className="mt-2 text-sm">{a.reason}</p>
              {(a.payload as { plan?: { rationale?: string } })?.plan?.rationale && <p className="mt-1 text-xs muted">Agent rationale: {(a.payload as { plan?: { rationale?: string } }).plan?.rationale}</p>}
              {a.decided_by && (
                <p className="mt-1 text-xs muted">
                  Decided by {a.decided_by} at {when(a.decided_at)} {a.decision_note ? `— ${a.decision_note}` : ""}
                </p>
              )}
              {a.execution_result && (
                <div className="mt-2">
                  <Json value={a.execution_result} />
                </div>
              )}
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}
