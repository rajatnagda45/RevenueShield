"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { CaseItem, useApi } from "@/lib/api";
import { inr, short, when } from "@/lib/format";
import { Badge, Empty, ErrorBox, PageTitle, Panel } from "@/components/ui";

export default function CasesPage() {
  const [status, setStatus] = useState("");
  const [surface, setSurface] = useState("");
  const [arm, setArm] = useState("");
  const [q, setQ] = useState("");
  const qs = useMemo(() => {
    const p = new URLSearchParams({ limit: "100" });
    if (status) p.set("status", status);
    if (surface) p.set("surface", surface);
    if (arm) p.set("arm", arm);
    if (q) p.set("q", q);
    return `?${p.toString()}`;
  }, [status, surface, arm, q]);
  const { data, error, loading } = useApi<{ total: number; items: CaseItem[] }>(`/v2/cases${qs}`);

  return (
    <div>
      <PageTitle
        title="Recovery cases"
        subtitle="Every leak the system is working on, across all four surfaces, with its experiment arm."
        actions={
          <>
            <input placeholder="Customer name…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search customer" />
            <select value={surface} onChange={(e) => setSurface(e.target.value)} aria-label="Surface">
              <option value="">All surfaces</option>
              {["PAYMENT_FAILURE", "SUBSCRIPTION_MANDATE_FAILURE", "CHECKOUT_ABANDONMENT", "RECEIVABLE_OVERDUE"].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
              <option value="">All statuses</option>
              {["OPEN", "IN_PROGRESS", "PAUSED", "RECOVERED", "CLOSED"].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <select value={arm} onChange={(e) => setArm(e.target.value)} aria-label="Arm">
              <option value="">Both arms</option>
              <option value="TREATMENT">Treatment</option>
              <option value="HOLDOUT">Holdout</option>
            </select>
          </>
        }
      />
      <ErrorBox error={error} />
      <Panel right={<span className="text-xs muted">{data ? `${data.items.length} of ${data.total}` : ""}</span>}>
        {loading && !data && <Empty text="Loading…" />}
        {data && data.items.length === 0 && <Empty text="No cases match. Run a replay to populate the system." />}
        {data && data.items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="data">
              <thead>
                <tr>
                  <th>Case</th>
                  <th>Customer</th>
                  <th>Surface</th>
                  <th>Diagnosis</th>
                  <th>Arm</th>
                  <th>Status</th>
                  <th className="num">At risk</th>
                  <th className="num">Recovered</th>
                  <th>Opened</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((c) => (
                  <tr key={c.id}>
                    <td>
                      <Link href={`/cases/${c.id}`} className="mono underline">
                        {short(c.id)}
                      </Link>
                    </td>
                    <td>
                      <div>{c.customer.name || "—"}</div>
                      <div className="text-xs muted">
                        {c.customer.segment} · {c.customer.phone_masked}
                      </div>
                    </td>
                    <td className="text-xs">{c.surface}</td>
                    <td className="text-xs">{c.diagnosis || "—"}</td>
                    <td>
                      <Badge value={c.experiment_arm} />
                    </td>
                    <td>
                      <Badge value={c.status} />
                    </td>
                    <td className="num">{inr(c.amount_at_risk)}</td>
                    <td className="num">{inr(c.recovered_amount)}</td>
                    <td className="text-xs muted">{when(c.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
