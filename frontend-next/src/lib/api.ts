"use client";

import { useCallback, useEffect, useState } from "react";

export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const INTERNAL_SECRET = process.env.NEXT_PUBLIC_INTERNAL_SECRET || "";

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown) {
    super(typeof body === "string" ? body : (body as { detail?: string })?.detail || `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(init?.headers as Record<string, string> | undefined) };
  if (INTERNAL_SECRET) headers["X-Internal-Secret"] = INTERNAL_SECRET;
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers, cache: "no-store" });
  const text = await res.text();
  let body: unknown = text;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    /* plain text */
  }
  if (!res.ok) throw new ApiError(res.status, body);
  return body as T;
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: unknown) => request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
};

interface ApiState<T> {
  key: string | null;
  data: T | null;
  error: string | null;
}

/**
 * Minimal data hook. `loading` is derived (the current request key differs from the last settled
 * key), so the effect only sets state when a request settles.
 */
export function useApi<T>(path: string | null) {
  const [tick, setTick] = useState(0);
  const [state, setState] = useState<ApiState<T>>({ key: null, data: null, error: null });
  const key = path ? `${path}|${tick}` : null;
  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!path || !key) return;
    let alive = true;
    api
      .get<T>(path)
      .then((d) => alive && setState({ key, data: d, error: null }))
      .catch((e: Error) => alive && setState((s) => ({ key, data: s.data, error: e.message })));
    return () => {
      alive = false;
    };
  }, [path, key]);

  const loading = !!key && state.key !== key;
  return { data: state.data, error: state.error, loading, refresh };
}

// ---------------------------------------------------------------- types (subset of the backend contracts)

export interface ArmStats {
  cases: number;
  recovered_cases: number;
  case_recovery_rate: number;
  case_recovery_rate_ci95: [number, number];
  amount_at_risk: number;
  amount_recovered: number;
  amount_settled: number;
  rupee_recovery_rate: number;
  settlement_coverage: number;
}

export interface Scorecard {
  scope: { batch_id: string | null; start: string | null; end: string | null; leak_surface: string | null; cases: number };
  arms: Record<string, ArmStats>;
  lift: {
    available: boolean;
    reason?: string;
    absolute_lift_case_rate?: number;
    relative_lift_case_rate?: number | null;
    z_statistic?: number | null;
    p_value?: number | null;
    significant_at_5pct?: boolean;
    incremental_rupees_recovered?: number;
    incremental_rupees_ci95?: [number | null, number | null];
    interpretation?: string;
  };
  costs: { total_cost: number; by_channel: Record<string, { actions: number; cost: number }>; cost_per_recovered_rupee: number | null; roi_multiple: number | null };
  compliance: {
    policy_violations: number;
    violations_by_rule: Record<string, number>;
    holdout_outreach_count: number;
    policy_blocks_enforced: number;
    stopping_rule_breaches: number;
    contact_blocks_by_rule?: Record<string, number>;
    audit_chain?: { ok: boolean; chained_rows: number; first_break: unknown };
  };
  audit: { executed_actions: number; audited_actions: number; coverage: number; audit_rows: number };
  timing: { time_to_recovery_hours?: { p50: number | null; p90: number | null }; attributable_recoveries?: number };
  breakdowns: Record<string, Record<string, { cases: number; recovered_cases: number; amount_at_risk: number; amount_recovered: number; case_recovery_rate: number }>>;
  warnings: string[];
  methodology: Record<string, string>;
}

export interface CaseItem {
  id: string;
  status: string;
  surface: string;
  experiment_arm: string | null;
  batch_id: string | null;
  amount_at_risk: number;
  recovered_amount: number;
  currency: string;
  diagnosis: string | null;
  recovery_probability: number | null;
  risk_score: number | null;
  customer: { name: string | null; segment: string | null; phone_masked: string | null };
  metadata: Record<string, unknown>;
  created_at: string | null;
  closed_at: string | null;
}

export interface AgentRun {
  id: string;
  case_id: string;
  provider: string;
  model: string;
  status: string;
  degraded_to_rules: boolean;
  turns: number;
  trace: Array<Record<string, unknown>>;
  final_plan: Record<string, unknown> | null;
  validation: { ok: boolean; errors: string[]; warnings: string[] } | null;
  execution_result: Record<string, unknown> | null;
  approval_id: string | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  started_at: string | null;
  completed_at: string | null;
  dossier?: Record<string, unknown>;
}

export interface Approval {
  id: string;
  case_id: string;
  agent_run_id: string | null;
  action: string;
  reason: string;
  status: string;
  payload: Record<string, unknown>;
  requested_at: string | null;
  expires_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
  decision_note: string | null;
  execution_result: Record<string, unknown> | null;
}
