"use client";

import { useApi } from "@/lib/api";
import { num, when } from "@/lib/format";
import { ErrorBox, Json, PageTitle, Panel, StatTile } from "@/components/ui";

interface Verify {
  ok: boolean;
  chained_rows: number;
  verified_rows: number;
  legacy_rows: number;
  head_sequence: number;
  head_hash: string | null;
  first_break: { sequence: number; audit_id: string; action: string; problem: string } | null;
  verified_at: string;
}

export default function AuditPage() {
  const { data, error, refresh } = useApi<Verify>("/audit/verify");
  return (
    <div>
      <PageTitle
        title="Audit chain"
        subtitle="Every audit row carries the hash of the previous row. Editing or deleting history breaks every later hash; verification recomputes the whole chain and reports the first broken link."
        actions={
          <button className="btn primary" onClick={refresh}>
            Re-verify now
          </button>
        }
      />
      <ErrorBox error={error} />
      {data && (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <StatTile label="Chain" value={data.ok ? "INTACT" : "BROKEN"} tone={data.ok ? "good" : "critical"} sub={`verified ${when(data.verified_at)}`} />
            <StatTile label="Chained rows" value={num(data.chained_rows)} sub={`${num(data.verified_rows)} verified`} />
            <StatTile label="Legacy rows" value={num(data.legacy_rows)} sub="written before the chain existed" />
            <StatTile label="Head" value={`#${num(data.head_sequence)}`} sub={data.head_hash ? data.head_hash.slice(0, 16) : ""} />
          </div>
          {data.first_break && (
            <Panel title="First broken link" className="mt-6">
              <p className="text-sm" style={{ color: "var(--status-critical)" }}>
                Row #{data.first_break.sequence} ({data.first_break.action}): {data.first_break.problem}
              </p>
              <Json value={data.first_break} open />
            </Panel>
          )}
          <Panel title="How it works" className="mt-6">
            <ol className="list-decimal pl-5 text-sm flex flex-col gap-1">
              <li>
                Each audit row gets a global sequence number, <span className="mono">prev_hash</span>, and <span className="mono">row_hash = sha256(prev_hash || canonical_json(row))</span> in a session hook - no service can forget it.
              </li>
              <li>The sequence has a unique index, so two writers racing for the same slot fail instead of silently forking the chain.</li>
              <li>
                Verification walks the chain in order and recomputes every hash. The scorecard shows the result next to the compliance numbers; readiness exposes the head sequence.
              </li>
            </ol>
          </Panel>
        </>
      )}
    </div>
  );
}
