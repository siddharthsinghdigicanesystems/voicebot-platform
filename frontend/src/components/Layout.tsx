import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../auth";

export function Layout() {
  const { me, logout } = useAuth();
  const loc = useLocation();
  const navItem = (to: string, label: string) => (
    <NavLink
      to={to}
      className={({ isActive }) =>
        `px-3 py-1.5 rounded-lg text-sm transition-colors ${
          isActive ? "bg-accent/15 text-accent" : "text-muted hover:text-white"
        }`
      }
    >
      {label}
    </NavLink>
  );

  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-border/60 bg-card/30 backdrop-blur-sm">
        <div className="max-w-6xl mx-auto px-6 py-3 flex items-center gap-6">
          <Link to="/" className="font-semibold tracking-tight text-white">
            VoiceBot
          </Link>
          <nav className="flex items-center gap-1">
            {navItem("/calls", "Calls")}
            {navItem("/campaigns", "Campaigns")}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-xs text-muted">
            <span>{me?.username}</span>
            <button
              onClick={() => {
                logout();
                window.location.href = "/login";
              }}
              className="hover:text-white"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="flex-1 max-w-6xl w-full mx-auto px-6 py-8">
        <Outlet key={loc.pathname} />
      </main>

      <footer className="text-center text-xs text-muted py-6 border-t border-border/40">
        VoiceBot Platform · Tata Voice Streaming + GPT-4o Realtime
      </footer>
    </div>
  );
}
