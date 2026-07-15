import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type CallSummary } from "../api";

function fmtDuration(s: number | null): string {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60).toString().padStart(2, "0");
  return `${m}:${sec}`;
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString();
}

function OutcomeBadge({ outcome }: { outcome: string | null }) {
  if (!outcome) return <span className="badge-info">in progress</span>;
  if (outcome === "completed") return <span className="badge-ok">completed</span>;
  if (outcome === "transferred") return <span className="badge-info">transferred</span>;
  if (outcome === "error") return <span className="badge-err">error</span>;
  return <span className="badge-warn">{outcome}</span>;
}

export function CallsPage() {
  const [items, setItems] = useState<CallSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [direction, setDirection] = useState<string>("");
  const [q, setQ] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setErr(null);
    try {
      const r = await api.listCalls({
        ...(direction ? { direction } : {}),
        ...(q ? { q } : {}),
        limit: 100,
      });
      setItems(r.items);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [direction]);

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-semibold">Calls</h1>
        <div className="flex items-center gap-2">
          <select
            className="input !w-auto"
            value={direction}
            onChange={(e) => setDirection(e.target.value)}
          >
            <option value="">All directions</option>
            <option value="inbound">Inbound</option>
            <option value="outbound">Outbound</option>
          </select>
          <input
            className="input !w-56"
            placeholder="Search by phone…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && load()}
          />
          <button className="btn-ghost" onClick={load}>Refresh</button>
        </div>
      </div>

      <div className="card !p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-[#0d1430] text-muted text-xs uppercase tracking-wider">
            <tr>
              <th className="text-left px-4 py-3 font-medium">When</th>
              <th className="text-left px-4 py-3 font-medium">Direction</th>
              <th className="text-left px-4 py-3 font-medium">From</th>
              <th className="text-left px-4 py-3 font-medium">To</th>
              <th className="text-left px-4 py-3 font-medium">Duration</th>
              <th className="text-left px-4 py-3 font-medium">Outcome</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {loading && items.length === 0 && (
              <tr><td colSpan={7} className="px-4 py-8 text-center text-muted">Loading…</td></tr>
            )}
            {err && (
              <tr><td colSpan={7} className="px-4 py-8 text-center text-rose-300">{err}</td></tr>
            )}
            {!loading && items.length === 0 && !err && (
              <tr><td colSpan={7} className="px-4 py-8 text-center text-muted">
                No calls yet. Open <a className="text-accent hover:underline" target="_blank" rel="noreferrer" href="http://localhost:8081">the demo page</a> and say hello.
              </td></tr>
            )}
            {items.map((c) => (
              <tr key={c.id} className="border-t border-border hover:bg-[#0d1430]/50 transition-colors">
                <td className="px-4 py-3 text-muted whitespace-nowrap">{fmtDate(c.started_at)}</td>
                <td className="px-4 py-3">
                  <span className={c.direction === "inbound" ? "badge-info" : "badge-ok"}>
                    {c.direction}
                  </span>
                </td>
                <td className="px-4 py-3 font-mono text-xs">{c.from_number}</td>
                <td className="px-4 py-3 font-mono text-xs">{c.to_number}</td>
                <td className="px-4 py-3 font-mono">{fmtDuration(c.duration_seconds)}</td>
                <td className="px-4 py-3"><OutcomeBadge outcome={c.outcome} /></td>
                <td className="px-4 py-3 text-right pr-4">
                  <Link to={`/calls/${c.id}`} className="text-accent hover:underline text-xs">
                    Open →
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
