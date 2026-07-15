import { useEffect, useState } from "react";
import { api, SUPPORTED_LANGUAGES, SUPPORTED_VOICES, type Campaign } from "../api";

function StatusBadge({ status }: { status: string }) {
  if (status === "running") return <span className="badge-ok">running</span>;
  if (status === "paused") return <span className="badge-warn">paused</span>;
  if (status === "completed") return <span className="badge-info">completed</span>;
  return <span className="badge-info">{status}</span>;
}

export function CampaignsPage() {
  const [items, setItems] = useState<Campaign[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [phones, setPhones] = useState("+919812345678\n+919823456789");
  const [language, setLanguage] = useState<string>("en");
  const [voice, setVoice] = useState<string>("");
  const [brand, setBrand] = useState<string>("");

  async function load() {
    try {
      setItems(await api.listCampaigns());
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setCreating(true);
    try {
      const contacts = phones
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean)
        .map((phone) => ({ phone }));
      await api.createCampaign({
        name: name.trim(),
        contacts,
        language,
        ...(voice ? { voice } : {}),
        ...(brand.trim() ? { brand: brand.trim() } : {}),
      });
      setName("");
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setCreating(false);
    }
  }

  async function toggle(c: Campaign) {
    try {
      if (c.status === "running") await api.pauseCampaign(c.id);
      else await api.startCampaign(c.id);
      await load();
    } catch (e) {
      setErr(String(e));
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Outbound campaigns</h1>

      <form onSubmit={create} className="card grid md:grid-cols-6 gap-4">
        <div className="md:col-span-2">
          <label className="text-xs text-muted">Name</label>
          <input
            className="input mt-1"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="May appointment confirmations"
          />
        </div>
        <div className="md:col-span-2">
          <label className="text-xs text-muted">Brand (optional)</label>
          <input
            className="input mt-1"
            value={brand}
            onChange={(e) => setBrand(e.target.value)}
            placeholder="Acme Health"
          />
        </div>
        <div>
          <label className="text-xs text-muted">Language</label>
          <select
            className="input mt-1"
            value={language}
            onChange={(e) => setLanguage(e.target.value)}
          >
            {SUPPORTED_LANGUAGES.map((l) => (
              <option key={l.code} value={l.code}>
                {l.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-xs text-muted">Voice</label>
          <select
            className="input mt-1"
            value={voice}
            onChange={(e) => setVoice(e.target.value)}
          >
            <option value="">default</option>
            {SUPPORTED_VOICES.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </div>
        <div className="md:col-span-6">
          <label className="text-xs text-muted">Phone numbers (one per line, E.164)</label>
          <textarea
            className="input mt-1 h-20 font-mono text-xs"
            value={phones}
            onChange={(e) => setPhones(e.target.value)}
          />
        </div>
        <div className="md:col-span-6 flex justify-end">
          <button className="btn-primary" disabled={creating || !name.trim()}>
            {creating ? "Creating…" : "Create campaign"}
          </button>
        </div>
      </form>

      {err && <div className="text-rose-300 text-sm">{err}</div>}

      <div className="card !p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-[#0d1430] text-muted text-xs uppercase tracking-wider">
            <tr>
              <th className="text-left px-4 py-3 font-medium">Name</th>
              <th className="text-left px-4 py-3 font-medium">Status</th>
              <th className="text-left px-4 py-3 font-medium">Lang · Voice</th>
              <th className="text-left px-4 py-3 font-medium">Pending</th>
              <th className="text-left px-4 py-3 font-medium">Succeeded</th>
              <th className="text-left px-4 py-3 font-medium">Failed</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 && (
              <tr><td colSpan={7} className="px-4 py-8 text-center text-muted">No campaigns yet.</td></tr>
            )}
            {items.map((c) => (
              <tr key={c.id} className="border-t border-border">
                <td className="px-4 py-3">{c.name}</td>
                <td className="px-4 py-3"><StatusBadge status={c.status} /></td>
                <td className="px-4 py-3 font-mono text-xs text-muted">
                  {c.language}
                  {c.voice ? ` · ${c.voice}` : ""}
                </td>
                <td className="px-4 py-3 font-mono">{c.pending_count}</td>
                <td className="px-4 py-3 font-mono text-emerald-300">{c.succeeded_count}</td>
                <td className="px-4 py-3 font-mono text-rose-300">{c.failed_count}</td>
                <td className="px-4 py-3 text-right">
                  <button className="btn-ghost text-xs" onClick={() => toggle(c)}>
                    {c.status === "running" ? "Pause" : "Start"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
