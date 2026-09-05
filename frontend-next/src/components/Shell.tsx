"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useApi } from "@/lib/api";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/scorecard", label: "Scorecard" },
  { href: "/cases", label: "Cases" },
  { href: "/approvals", label: "Approvals" },
  { href: "/agent", label: "Agent runs" },
  { href: "/degradation", label: "Degradation" },
  { href: "/replay", label: "Replay" },
  { href: "/jobs", label: "Jobs" },
  { href: "/audit", label: "Audit" },
];

interface Ready {
  status: string;
  database: string;
  model_status: string;
  jobs?: { queued: number; running: number; dead: number; last_completed_at: string | null };
  audit_chain?: { ok: boolean; head_sequence: number };
  llm?: { provider: string; circuit: string };
  experiments?: { enabled: boolean; holdout_percent: number };
}

export function Shell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const { data: ready, error } = useApi<Ready>("/health/ready");
  return (
    <div className="flex min-h-screen">
      <aside className="w-56 shrink-0 border-r border-border bg-surface-1 px-4 py-5 flex flex-col gap-6">
        <div>
          <div className="flex items-center gap-2">
            <span aria-hidden className="inline-block h-6 w-6 rounded-md" style={{ background: "var(--series-1)" }} />
            <div>
              <div className="font-semibold leading-tight">RevenueShield</div>
              <div className="text-xs muted">Command Center v2</div>
            </div>
          </div>
        </div>
        <nav className="flex flex-col gap-1" aria-label="Primary">
          {NAV.map((n) => {
            const active = n.href === "/" ? path === "/" : path.startsWith(n.href);
            return (
              <Link
                key={n.href}
                href={n.href}
                className={`rounded-md px-3 py-2 text-sm ${active ? "bg-surface-2 font-semibold" : "hover:bg-surface-2"}`}
                aria-current={active ? "page" : undefined}
              >
                {n.label}
              </Link>
            );
          })}
        </nav>
        <div className="mt-auto text-xs muted flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <span
              aria-hidden
              className="inline-block h-2 w-2 rounded-full"
              style={{ background: error ? "var(--status-critical)" : ready ? "var(--status-good)" : "var(--status-warning)" }}
            />
            <span>{error ? "API unreachable" : ready ? "API ready" : "Connecting…"}</span>
          </div>
          {ready?.llm && <div>Planner: {ready.llm.provider} · circuit {ready.llm.circuit}</div>}
          {ready?.experiments && <div>Holdout: {ready.experiments.enabled ? `${ready.experiments.holdout_percent}%` : "off"}</div>}
          {ready?.jobs && (
            <div>
              Jobs: {ready.jobs.queued} queued · {ready.jobs.dead} dead
            </div>
          )}
          {ready?.audit_chain && <div>Audit chain: {ready.audit_chain.ok ? "intact" : "BROKEN"} · #{ready.audit_chain.head_sequence}</div>}
        </div>
      </aside>
      <main className="flex-1 min-w-0 px-8 py-6">{children}</main>
    </div>
  );
}
