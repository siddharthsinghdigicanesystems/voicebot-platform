import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type CallDetail, type TranscriptOut } from "../api";

function fmtDuration(s: number | null): string {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60).toString().padStart(2, "0");
  return `${m}:${sec}`;
}

function TranscriptList({
  segments,
  liveSegments,
}: {
  segments: TranscriptOut[];
  liveSegments: { role: string; text: string }[];
}) {
  const all = [
    ...segments.map((s) => ({ role: s.role, text: s.text, key: s.created_at + s.text.slice(0, 8) })),
    ...liveSegments.map((s, i) => ({ ...s, key: `live-${i}` })),
  ];
  return (
    <div className="space-y-3">
      {all.length === 0 && <div className="text-muted text-sm">No transcript yet.</div>}
      {all.map((s) => (
        <div
          key={s.key}
          className={`rounded-xl p-3 max-w-[88%] ${
            s.role === "assistant"
              ? "bg-accent/10 border border-accent/20"
              : "bg-[#0d1430] border border-border ml-auto"
          }`}
        >
          <div className="text-[10px] uppercase tracking-wider text-muted mb-1">
            {s.role === "assistant" ? "Bot" : s.role === "user" ? "Caller" : s.role}
          </div>
          <div className="text-sm whitespace-pre-wrap">{s.text}</div>
        </div>
      ))}
    </div>
  );
}

export function CallDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [call, setCall] = useState<CallDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [liveSegments, setLiveSegments] = useState<{ role: string; text: string }[]>([]);
  const wsRef = useRef<WebSocket | null>(null);

  async function load() {
    if (!id) return;
    try {
      setCall(await api.getCall(id));
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // Subscribe to live transcript while the call is in progress.
  useEffect(() => {
    if (!id || !call) return;
    if (call.ended_at) return; // already over

    const ws = new WebSocket(api.liveTranscriptUrl(id));
    wsRef.current = ws;
    ws.onmessage = (e) => {
      try {
        const seg = JSON.parse(e.data);
        setLiveSegments((s) => [...s, seg]);
      } catch {
        /* ignore */
      }
    };
    ws.onclose = () => {
      // Refresh once after close to capture the final, persisted segments.
      load();
    };
    return () => {
      ws.close();
      wsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, call?.ended_at]);

  if (err) return <div className="text-rose-300 text-sm">{err}</div>;
  if (!call) return <div className="text-muted text-sm">Loading…</div>;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link to="/calls" className="text-muted hover:text-white text-sm">← Calls</Link>
        <h1 className="text-xl font-semibold ml-auto">
          {call.direction === "inbound" ? "Inbound" : "Outbound"} call
        </h1>
        {!call.ended_at && (
          <span className="badge-info animate-pulse">live</span>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="card">
          <div className="text-xs uppercase tracking-wider text-muted">From</div>
          <div className="font-mono mt-1 text-sm">{call.from_number}</div>
        </div>
        <div className="card">
          <div className="text-xs uppercase tracking-wider text-muted">To</div>
          <div className="font-mono mt-1 text-sm">{call.to_number}</div>
        </div>
        <div className="card">
          <div className="text-xs uppercase tracking-wider text-muted">Duration · Outcome</div>
          <div className="mt-1 text-sm">
            <span className="font-mono">{fmtDuration(call.duration_seconds)}</span>
            <span className="mx-2 text-muted">·</span>
            {call.outcome ?? <span className="text-muted">in progress</span>}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div className="md:col-span-2">
          <h2 className="text-sm uppercase tracking-wider text-muted mb-3">Transcript</h2>
          <TranscriptList segments={call.transcript} liveSegments={liveSegments} />
        </div>

        <div>
          <h2 className="text-sm uppercase tracking-wider text-muted mb-3">Tools called</h2>
          {call.tool_invocations.length === 0 && (
            <div className="text-muted text-sm">None.</div>
          )}
          <div className="space-y-3">
            {call.tool_invocations.map((t, i) => (
              <div key={i} className="card !p-3">
                <div className="text-xs font-mono text-accent">{t.name}</div>
                <pre className="text-[11px] text-muted mt-1 overflow-x-auto">
                  {JSON.stringify(t.arguments, null, 2)}
                </pre>
                <div className="text-[10px] uppercase tracking-wider text-muted mt-2">Result</div>
                <pre className="text-[11px] text-emerald-300/90 overflow-x-auto">
                  {JSON.stringify(t.result, null, 2)}
                </pre>
              </div>
            ))}
          </div>

          {Object.keys(call.facts).length > 0 && (
            <>
              <h2 className="text-sm uppercase tracking-wider text-muted mt-6 mb-3">Outcome facts</h2>
              <pre className="card !p-3 text-[11px] overflow-x-auto">
                {JSON.stringify(call.facts, null, 2)}
              </pre>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
