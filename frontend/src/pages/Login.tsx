import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { ApiError } from "../api";

export function LoginPage() {
  const { login, me } = useAuth();
  const nav = useNavigate();
  const [u, setU] = useState("admin");
  const [p, setP] = useState("admin");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (me) {
    nav("/", { replace: true });
    return null;
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await login(u, p);
      nav("/", { replace: true });
    } catch (e) {
      setErr(e instanceof ApiError && e.status === 401 ? "Invalid credentials" : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <form onSubmit={submit} className="card w-full max-w-md space-y-4">
        <div>
          <h1 className="text-xl font-semibold">VoiceBot</h1>
          <p className="text-sm text-muted">Sign in to manage calls and campaigns.</p>
        </div>
        <div>
          <label className="text-xs text-muted">Username</label>
          <input className="input mt-1" value={u} onChange={(e) => setU(e.target.value)} autoFocus />
        </div>
        <div>
          <label className="text-xs text-muted">Password</label>
          <input className="input mt-1" type="password" value={p} onChange={(e) => setP(e.target.value)} />
        </div>
        {err && <div className="text-sm text-rose-300">{err}</div>}
        <button className="btn-primary w-full" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="text-xs text-muted">
          Default: <code className="text-white/80">admin</code> / <code className="text-white/80">admin</code> · change in <code className="text-white/80">.env</code>.
        </p>
      </form>
    </div>
  );
}
