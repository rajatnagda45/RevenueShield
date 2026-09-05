"use client";

import { useState } from "react";
import { api, useApi } from "@/lib/api";
import { num, when } from "@/lib/format";
import { Badge, Empty, ErrorBox, PageTitle, Panel, StatTile } from "@/components/ui";

interface Stats {
  by_status: Record<string, number>;
  by_kind: Record<string, Record<string, number>>;
  last_completed_at: string | null;
  oldest_queued_run_at: string | null;
  dead: number;
  kinds: string[];
  enabled: boolean;
}
interface Job {
  id: string;
  kind: string;
  status: string;
  attempts: number;
  max_attempts: number;
  run_at: string | null;
  last_error: string | null;
  completed_at: string | null;
}

export default function JobsPage() {
  const { data: stats, error, refresh } = useApi<Stats>("/jobs/stats");
  const [status, setStatus] = useState("");
  const { data: jobs, refresh: refreshJobs } = useApi<Job[]>(`/jobs?limit=100${status ? `&status=${status}` : ""}`);
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState<string | null>(null);

  const runOnce = async () => {
    setBusy(true);
    try {
      const r = await api.post<Record<string, number>>("/jobs/run-once", { include_recurring: true });
      setLast(JSON.stringify(r));
      refresh();
      refreshJobs();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <PageTitle
        title="Background jobs"
        subtitle="Transactional outbox: recurring ticks (plans, sweeps, degradation, approvals, settlements) and one-off jobs, retried with backoff and buried as DEAD with their last error."
        actions={
          <>
            <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
              <option value="">All</option>
              {["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "DEAD"].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <button className="btn primary" onClick={runOnce} disabled={busy}>
              Run one worker cycle
            </button>
          </>
        }
      />
      <ErrorBox error={error} />
      {last && <div className="panel p-3 mb-4 text-xs mono">{last}</div>}
      {stats && (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <StatTile label="Queued" value={num(stats.by_status.QUEUED || 0)} sub={stats.oldest_queued_run_at ? `oldest due ${when(stats.oldest_queued_run_at)}` : ""} />
          <StatTile label="Succeeded" value={num(stats.by_status.SUCCEEDED || 0)} sub={stats.last_completed_at ? `last ${when(stats.last_completed_at)}` : ""} tone="good" />
          <StatTile label="Dead" value={num(stats.dead)} tone={stats.dead ? "critical" : undefined} sub="exhausted retries" />
          <StatTile label="Kinds" value={num(stats.kinds.length)} sub={stats.enabled ? "jobs enabled" : "jobs disabled"} />
        </div>
      )}
      <Panel title="Recent jobs" className="mt-6">
        {jobs && jobs.length === 0 && <Empty text="No jobs." />}
        <table className="data">
          <thead>
            <tr>
              <th>Kind</th>
              <th>Status</th>
              <th className="num">Attempts</th>
              <th>Run at</th>
              <th>Completed</th>
              <th>Last error</th>
            </tr>
          </thead>
          <tbody>
            {(jobs || []).map((j) => (
              <tr key={j.id}>
                <td className="mono text-xs">{j.kind}</td>
                <td>
                  <Badge value={j.status} />
                </td>
                <td className="num">
                  {j.attempts}/{j.max_attempts}
                </td>
                <td className="text-xs muted">{when(j.run_at)}</td>
                <td className="text-xs muted">{when(j.completed_at)}</td>
                <td className="text-xs muted truncate max-w-md">{j.last_error ? j.last_error.split("\n")[0] : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}
