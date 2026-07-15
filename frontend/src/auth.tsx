/** Auth context: provides `me`, login, logout. Routes consume via useAuth(). */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, ApiError, type Me, tokenStore } from "./api";

type AuthState = {
  me: Me | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
};

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    if (!tokenStore.get()) {
      setMe(null);
      setLoading(false);
      return;
    }
    try {
      const m = await api.me();
      setMe(m);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) tokenStore.clear();
      setMe(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    const r = await api.login({ username, password });
    tokenStore.set(r.access_token);
    await refresh();
  }, [refresh]);

  const logout = useCallback(() => {
    tokenStore.clear();
    setMe(null);
  }, []);

  const value = useMemo(() => ({ me, loading, login, logout }), [me, loading, login, logout]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("AuthProvider missing");
  return v;
}
