// Client for the agent's /admin/* API. The admin token travels as a ?token=
// query param on every request (uniform across GET/POST, preflight-free reads).
import { AGENT_URL } from "../api";

export interface ProviderInfo {
  id: string;
  label: string;
  kind: string; // "anthropic" | "openai"
  configured: boolean;
  needs_base_url: boolean;
  base_url?: string;
  key_env?: string;
  models: string[];
}
export interface PersonaInfo {
  id: string;
  label: string;
  description: string;
  prompt: string | null; // null for "custom"
}
export interface ConfigResponse {
  settings: Record<string, string>;
  llm_api_key_set: boolean;
  editable_keys: string[];
  guardrails_enabled: boolean;
  providers: ProviderInfo[];
  personas: PersonaInfo[];
  whisper: { device: string; compute_type: string };
  admin_token_is_default: boolean;
}
export interface SessionRow {
  session_id: string;
  status?: string;
  [k: string]: string | undefined;
}
export interface Turn {
  session_id: string;
  role: string;
  content: string;
  created_at: string;
}
export interface LogRec {
  ts: string;
  level: string;
  module: string;
  message: string;
}
export interface Invitation {
  id: number;
  token: string;
  channel: string; // email | sms
  destination_masked: string;
  label: string | null;
  agent_slug: string | null;
  status: string; // sent | verified | consumed | revoked
  calls_allowed: number;
  calls_used: number;
  attempts: number;
  has_code: boolean;
  code?: string | null; // only present in dev mode
  expired: boolean;
  created_at: string | null;
  sent_at: string | null;
  verified_at: string | null;
  last_call_at: string | null;
}

export interface CallRow {
  session_id: string;
  caller: string | null;
  agent_slug: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  turn_count: number;
  recording_id: number | null;
}
export interface CallDetail extends CallRow {
  transcript: string;
}
export interface Recording {
  id: number;
  session_id: string;
  sample_rate: number | null;
  channels: number | null;
  bytes: number | null;
  duration_seconds: number | null;
  created_at: string | null;
}

export interface Agent {
  id: number;
  slug: string;
  name: string;
  description: string | null;
  enabled: boolean;
  is_default: boolean;
  llm_provider: string | null;
  llm_model: string | null;
  llm_base_url: string | null;
  agent_persona: string | null;
  system_prompt: string | null;
  tts_voice: string | null;
  whisper_model: string | null;
  memory_enabled: string | null;
  memory_max_messages: string | null;
  history_max_messages: string | null;
  llm_api_key_set: boolean;
}

export interface EmailSettings {
  smtp_host: string;
  smtp_port: number;
  smtp_user: string;
  smtp_from: string;
  smtp_use_tls: boolean;
  password_set: boolean;
  configured: boolean;
}

export class Unauthorized extends Error {}

function qs(token: string, extra: Record<string, string | number | undefined> = {}) {
  const p = new URLSearchParams({ token });
  for (const [k, v] of Object.entries(extra)) {
    if (v !== undefined && v !== "") p.set(k, String(v));
  }
  return p.toString();
}

async function handle(res: Response) {
  if (res.status === 401) throw new Unauthorized("Invalid admin token");
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.detail || body?.error || `Request failed (${res.status})`);
  }
  return res.json();
}

async function get<T>(path: string, token: string, params = {}): Promise<T> {
  return handle(await fetch(`${AGENT_URL}/admin${path}?${qs(token, params)}`));
}

async function post<T>(path: string, token: string, body?: unknown): Promise<T> {
  return handle(
    await fetch(`${AGENT_URL}/admin${path}?${qs(token)}`, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    })
  );
}

export const adminApi = {
  ping: (t: string) => get<{ ok: boolean; admin_token_is_default: boolean }>("/ping", t),
  getConfig: (t: string) => get<ConfigResponse>("/config", t),
  saveConfig: (t: string, patch: Record<string, string>) =>
    post<{ settings: Record<string, string>; llm_api_key_set: boolean }>("/config", t, patch),
  resetConfig: (t: string) =>
    post<{ settings: Record<string, string>; llm_api_key_set: boolean }>("/config/reset", t),
  getSessions: (t: string) =>
    get<{ active: number; sessions: SessionRow[] }>("/sessions", t),
  getTranscripts: (t: string, session_id?: string) =>
    get<{ turns: Turn[] }>("/transcripts", t, { session_id, limit: 100 }),
  getLogs: (t: string, level?: string) =>
    get<{ logs: LogRec[] }>("/logs", t, { level, limit: 300 }),
  getCalls: (t: string) => get<{ calls: CallRow[]; active: number }>("/calls", t),
  getCall: (t: string, sid: string) => get<{ call: CallDetail }>(`/calls/${encodeURIComponent(sid)}`, t),
  getRecordings: (t: string, session_id?: string) =>
    get<{ recordings: Recording[] }>("/recordings", t, { session_id }),
  recordingAudioUrl: (t: string, id: number) =>
    `${AGENT_URL}/admin/recordings/${id}/audio?token=${encodeURIComponent(t)}`,
  getInvitations: (t: string) =>
    get<{ invitations: Invitation[]; dev_mode: boolean }>("/invitations", t),
  createInvitation: (t: string, method: string, value: string, label?: string, agent_slug?: string) =>
    post<{ invitation: Invitation }>("/invitations", t, { method, value, label, agent_slug }),
  getAgents: (t: string) => get<{ agents: Agent[] }>("/agents", t),
  createAgent: (t: string, body: Record<string, unknown>) =>
    post<{ agent: Agent }>("/agents", t, body),
  updateAgent: (t: string, id: number, body: Record<string, unknown>) =>
    post<{ agent: Agent }>(`/agents/${id}`, t, body),
  setDefaultAgent: (t: string, id: number) => post<{ ok: boolean }>(`/agents/${id}/default`, t),
  deleteAgent: (t: string, id: number) => post<{ ok: boolean }>(`/agents/${id}/delete`, t),
  reenableInvitation: (t: string, id: number) =>
    post<{ invitation: Invitation }>(`/invitations/${id}/reenable`, t),
  revokeInvitation: (t: string, id: number) => post<{ ok: boolean }>(`/invitations/${id}/revoke`, t),
  getEmail: (t: string) => get<EmailSettings>("/email", t),
  saveEmail: (t: string, body: Record<string, unknown>) => post<EmailSettings>("/email", t, body),
  testEmail: (t: string, to: string) =>
    post<{ ok: boolean; sent_to: string }>("/email/test", t, { to }),
};
