/**
 * Tiny API client for the dashboard.
 *
 * Auth token is read from localStorage on every call (so a sign-out anywhere
 * propagates immediately). Errors throw an `ApiError` with the status code so
 * UI code can check `err.status === 401` to handle session expiry uniformly.
 */

const BASE = (import.meta.env.VITE_API_URL ?? "http://localhost:8000") as string;
const TOKEN_KEY = "voicebot.token";

export class ApiError extends Error {
  constructor(public status: number, public body: unknown, message: string) {
    super(message);
  }
}

export const tokenStore = {
  get: (): string | null => localStorage.getItem(TOKEN_KEY),
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

async function request<T>(
  path: string,
  init: RequestInit = {},
  { auth = true }: { auth?: boolean } = {}
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (auth) {
    const tok = tokenStore.get();
    if (tok) headers.set("Authorization", `Bearer ${tok}`);
  }
  const r = await fetch(`${BASE}${path}`, { ...init, headers });
  const text = await r.text();
  let body: unknown = text;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    /* keep raw text */
  }
  if (!r.ok) {
    throw new ApiError(r.status, body, `${r.status} ${r.statusText}: ${path}`);
  }
  return body as T;
}

// ---- Types matching the API ----

export type LoginIn = { username: string; password: string };
export type LoginOut = { access_token: string; expires_in: number; token_type: "bearer" };
export type Me = { id: string; username: string; is_admin: boolean };

export type CallSummary = {
  id: string;
  provider_call_id: string;
  direction: "inbound" | "outbound";
  from_number: string;
  to_number: string;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
  outcome: string | null;
  facts: Record<string, unknown>;
};

export type TranscriptOut = {
  role: "user" | "assistant" | "system";
  text: string;
  created_at: string;
};

export type ToolInvocationOut = {
  name: string;
  arguments: Record<string, unknown>;
  result: unknown;
  created_at: string;
};

export type CallDetail = CallSummary & {
  transcript: TranscriptOut[];
  tool_invocations: ToolInvocationOut[];
};

export type CallList = {
  items: CallSummary[];
  total: number;
  limit: number;
  offset: number;
};

export type Campaign = {
  id: string;
  name: string;
  status: string;
  scheduled_at: string | null;
  max_concurrency: number;
  retry_attempts: number;
  created_at: string;
  brand: string | null;
  system_prompt_override: string | null;
  voice: string | null;
  language: string;
  contacts_count: number;
  pending_count: number;
  succeeded_count: number;
  failed_count: number;
};

export const SUPPORTED_VOICES = [
  "alloy",
  "echo",
  "shimmer",
  "verse",
  "ballad",
  "coral",
  "sage",
] as const;

export const SUPPORTED_LANGUAGES = [
  { code: "en", label: "English" },
  { code: "hinglish", label: "Hinglish" },
  { code: "hi", label: "Hindi" },
] as const;

// ---- Endpoints ----

export const api = {
  login: (body: LoginIn) =>
    request<LoginOut>("/v1/auth/login", { method: "POST", body: JSON.stringify(body) }, { auth: false }),
  me: () => request<Me>("/v1/auth/me"),

  listCalls: (params: { direction?: string; q?: string; limit?: number; offset?: number } = {}) => {
    const u = new URLSearchParams();
    if (params.direction) u.set("direction", params.direction);
    if (params.q) u.set("q", params.q);
    if (params.limit) u.set("limit", String(params.limit));
    if (params.offset) u.set("offset", String(params.offset));
    return request<CallList>(`/v1/calls?${u.toString()}`);
  },
  getCall: (id: string) => request<CallDetail>(`/v1/calls/${id}`),
  liveTranscriptUrl: (callId: string): string => {
    const tok = tokenStore.get() ?? "";
    const wsBase = BASE.replace(/^http/, "ws");
    return `${wsBase}/v1/calls/${callId}/live?token=${encodeURIComponent(tok)}`;
  },

  listCampaigns: () => request<Campaign[]>("/v1/campaigns"),
  createCampaign: (body: {
    name: string;
    contacts: { phone: string; name?: string; payload?: Record<string, unknown> }[];
    max_concurrency?: number;
    retry_attempts?: number;
    brand?: string;
    system_prompt_override?: string;
    voice?: string;
    language?: string;
  }) =>
    request<Campaign>("/v1/campaigns", {
      method: "POST",
      body: JSON.stringify({
        max_concurrency: 5,
        retry_attempts: 2,
        language: "en",
        ...body,
      }),
    }),
  startCampaign: (id: string) => request<Campaign>(`/v1/campaigns/${id}/start`, { method: "POST" }),
  pauseCampaign: (id: string) => request<Campaign>(`/v1/campaigns/${id}/pause`, { method: "POST" }),
};
