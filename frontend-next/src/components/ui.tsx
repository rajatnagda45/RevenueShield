"use client";

import { ReactNode, useState } from "react";

export function PageTitle({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex items-start justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold">{title}</h1>
        {subtitle && <p className="muted mt-1 max-w-3xl">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Panel({ title, children, className = "", right }: { title?: string; children: ReactNode; className?: string; right?: ReactNode }) {
  return (
    <section className={`panel p-4 ${className}`}>
      {(title || right) && (
        <div className="mb-3 flex items-center justify-between">
          {title && <h2 className="text-sm font-semibold muted uppercase tracking-wide">{title}</h2>}
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

/** Stat tile: a single headline number. Not a chart; no colour needed beyond text tokens. */
export function StatTile({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "good" | "warning" | "critical" }) {
  const color = tone === "good" ? "var(--status-good)" : tone === "warning" ? "var(--status-warning)" : tone === "critical" ? "var(--status-critical)" : undefined;
  return (
    <div className="panel p-4">
      <div className="text-xs muted uppercase tracking-wide">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums" style={color ? { color } : undefined}>
        {value}
      </div>
      {sub && <div className="mt-1 text-xs muted">{sub}</div>}
    </div>
  );
}

/**
 * Meter: a thin horizontal bar with a 4px rounded data end, anchored at the baseline. One or two
 * series (treatment vs holdout) with a legend + direct labels, per the mark spec.
 */
export function Meter({
  label,
  value,
  max = 1,
  color = "var(--series-1)",
  valueLabel,
}: {
  label: string;
  value: number;
  max?: number;
  color?: string;
  valueLabel?: string;
}) {
  const w = Math.max(0, Math.min(100, (value / (max || 1)) * 100));
  return (
    <div className="flex items-center gap-3 py-1" title={`${label}: ${valueLabel ?? value}`}>
      <div className="w-44 shrink-0 truncate text-sm">{label}</div>
      <div className="relative h-3 flex-1 rounded-sm" style={{ background: "var(--surface-2)" }} role="img" aria-label={`${label} ${valueLabel ?? value}`}>
        <div className="absolute inset-y-0 left-0" style={{ width: `${w}%`, background: color, borderRadius: "0 4px 4px 0" }} />
      </div>
      <div className="w-24 shrink-0 text-right text-sm tabular-nums">{valueLabel ?? value}</div>
    </div>
  );
}

export function Legend({ items }: { items: Array<{ label: string; color: string }> }) {
  return (
    <div className="flex flex-wrap gap-4 text-xs muted" aria-label="Legend">
      {items.map((i) => (
        <span key={i.label} className="inline-flex items-center gap-2">
          <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: i.color }} />
          {i.label}
        </span>
      ))}
    </div>
  );
}

const STATUS_TONE: Record<string, string> = {
  RECOVERED: "var(--status-good)",
  EXECUTED: "var(--status-good)",
  SUCCEEDED: "var(--status-good)",
  APPROVED: "var(--status-good)",
  CLOSED: "var(--text-muted)",
  OPEN: "var(--series-1)",
  IN_PROGRESS: "var(--series-1)",
  WAITING: "var(--series-1)",
  QUEUED: "var(--series-1)",
  RUNNING: "var(--series-1)",
  PENDING: "var(--status-warning)",
  AWAITING_APPROVAL: "var(--status-warning)",
  PAUSED: "var(--status-warning)",
  HELD: "var(--status-warning)",
  SUSPECTED: "var(--status-warning)",
  RECOVERING: "var(--status-warning)",
  BLOCKED: "var(--status-serious)",
  REJECTED: "var(--status-serious)",
  CONFIRMED: "var(--status-critical)",
  FAILED: "var(--status-critical)",
  DEAD: "var(--status-critical)",
  EXPIRED: "var(--text-muted)",
  HANDOFF: "var(--series-7)",
  NO_ACTION: "var(--text-muted)",
  HOLDOUT: "var(--series-2)",
  TREATMENT: "var(--series-1)",
};

export function Badge({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted">—</span>;
  return (
    <span className="badge">
      <span className="dot" aria-hidden style={{ color: STATUS_TONE[value] || "var(--text-muted)" }} />
      {value}
    </span>
  );
}

export function Json({ value, open = false }: { value: unknown; open?: boolean }) {
  const [show, setShow] = useState(open);
  return (
    <div>
      <button className="btn text-xs" onClick={() => setShow((s) => !s)}>
        {show ? "Hide JSON" : "Show JSON"}
      </button>
      {show && <pre className="json mono mt-2">{JSON.stringify(value, null, 2)}</pre>}
    </div>
  );
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className="panel p-3 text-sm" style={{ borderColor: "var(--status-critical)" }} role="alert">
      <strong>Could not load:</strong> {error}. Is the API running at the configured base URL?
    </div>
  );
}

export function Empty({ text }: { text: string }) {
  return <div className="muted py-6 text-center text-sm">{text}</div>;
}
