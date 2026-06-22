// Client for the agent's verification endpoint (see services/agent/auth.py).
// Invitation-only: an admin must have sent a code first; users can't self-start.

export const AGENT_URL =
  (import.meta as any).env?.VITE_AGENT_URL || "http://localhost:7860";

export interface VerifyResult {
  ticket: string;
  expires_in: number;
  agent: string | null; // which agent to connect to (from the invitation)
  cid: string | null; // caller id (invitation token) for cross-call memory
}

export interface VerifyParams {
  token?: string; // from an invite link (?inv=...)
  destination?: string; // the email/phone the code was sent to
  code: string;
}

// An invite link may carry an opaque token: http://host/?inv=<token>
export function inviteTokenFromUrl(): string | null {
  try {
    return new URLSearchParams(window.location.search).get("inv");
  } catch {
    return null;
  }
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${AGENT_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data: any = null;
  try {
    data = await res.json();
  } catch {
    /* non-JSON error body */
  }
  if (!res.ok) {
    const msg = data?.detail || data?.error || `Request failed (${res.status})`;
    throw new Error(typeof msg === "string" ? msg : "Request failed");
  }
  return data as T;
}

export function verifyCode(params: VerifyParams) {
  return postJson<VerifyResult>("/auth/verify", params);
}
