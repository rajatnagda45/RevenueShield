"use client";

import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { PageTitle, Panel } from "@/components/ui";

interface ReplayReport {
  batch_id: string;
  markdown?: string;
  replay_stats: Record<string, unknown>;
  agent: Record<string, unknown>;
  scorecard: { lift: Record<string, unknown>; compliance: { policy_violations: number } };
}

export default function ReplayPage() {
  const [form, setForm] = useState({ n_cases: 150, days: 7, tick_hours: 12, seed: 7, holdout_percent: 10, agent_enabled: true, provider: "auto", degradation_episode: true, duplicate_webhook_rate: 0.1, llm_outage: true, batch_id: "" });
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<ReplayReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<ReplayReport>("/simulation/replay", { ...form, batch_id: form.batch_id || null });
      setReport(r);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const set = (k: keyof typeof form, v: unknown) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <div>
      <PageTitle title="Batch replay" subtitle="Replay a seeded batch of Razorpay-shaped leaks through the real pipeline on a virtual clock, with chaos (duplicate webhooks, an LLM outage window), then read the scorecard." />
      <div className="grid gap-4 lg:grid-cols-3">
        <Panel title="Configuration">
          <div className="grid grid-cols-2 gap-3 text-sm">
            {(["n_cases", "days", "tick_hours", "seed", "holdout_percent", "duplicate_webhook_rate"] as const).map((k) => (
              <label key={k} className="flex flex-col gap-1">
                <span className="muted text-xs">{k}</span>
                <input type="number" step={k === "duplicate_webhook_rate" ? 0.05 : 1} value={form[k] as number} onChange={(e) => set(k, Number(e.target.value))} />
              </label>
            ))}
            <label className="flex flex-col gap-1">
              <span className="muted text-xs">provider</span>
              <select value={form.provider} onChange={(e) => set("provider", e.target.value)}>
                <option value="auto">auto</option>
                <option value="anthropic">anthropic (Claude)</option>
                <option value="openai">openai (GPT-4o)</option>
                <option value="null">null (rules)</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="muted text-xs">batch_id (optional)</span>
              <input value={form.batch_id} onChange={(e) => set("batch_id", e.target.value)} />
            </label>
            {(["agent_enabled", "degradation_episode", "llm_outage"] as const).map((k) => (
              <label key={k} className="flex items-center gap-2 col-span-2">
                <input type="checkbox" checked={form[k] as boolean} onChange={(e) => set(k, e.target.checked)} />
                <span>{k}</span>
              </label>
            ))}
          </div>
          <button className="btn primary mt-4 w-full" onClick={run} disabled={busy}>
            {busy ? "Replaying… (this drives the whole pipeline; a few hundred cases take a minute)" : "Run replay"}
          </button>
          {error && (
            <p className="mt-2 text-sm" style={{ color: "var(--status-critical)" }}>
              {error}
            </p>
          )}
        </Panel>
        <Panel title="Report" className="lg:col-span-2">
          {!report && <p className="muted text-sm">Run a replay to see the report. The batch then appears in the Scorecard and Cases pages.</p>}
          {report && (
            <div>
              <p className="text-sm mb-3">
                Batch <span className="mono">{report.batch_id}</span> ·{" "}
                <Link href="/scorecard" className="underline">
                  open in scorecard
                </Link>
              </p>
              <pre className="json mono" style={{ maxHeight: 640, whiteSpace: "pre-wrap" }}>
                {report.markdown}
              </pre>
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
