import { useCallback, useEffect, useRef, useState } from "react";
import {
  Agent,
  adminApi,
  CallDetail,
  CallRow,
  ConfigResponse,
  EmailSettings,
  Invitation,
  LogRec,
  Unauthorized,
} from "./adminApi";

const TOKEN_KEY = "voiceAdminToken";
type Tab = "invitations" | "agents" | "config" | "email" | "calls" | "logs";
const TAB_LABEL: Record<Tab, string> = {
  invitations: "Invitations",
  agents: "Agents",
  config: "Defaults",
  email: "Email",
  calls: "Calls",
  logs: "Logs",
};

export default function AdminApp() {
  const [token, setToken] = useState<string>(() => localStorage.getItem(TOKEN_KEY) || "");
  const [authed, setAuthed] = useState(false);
  const [checking, setChecking] = useState(true);
  const [tab, setTab] = useState<Tab>("invitations");
  const [tokenDefault, setTokenDefault] = useState(false);

  // Validate any stored token on load.
  useEffect(() => {
    let alive = true;
    (async () => {
      if (!token) {
        setChecking(false);
        return;
      }
      try {
        const r = await adminApi.ping(token);
        if (alive) {
          setAuthed(true);
          setTokenDefault(r.admin_token_is_default);
        }
      } catch {
        if (alive) setAuthed(false);
      } finally {
        if (alive) setChecking(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    setToken("");
    setAuthed(false);
  }, []);

  const onAuthFail = useCallback(() => {
    logout();
  }, [logout]);

  if (checking) {
    return <div className="admin admin--center">Checking…</div>;
  }

  if (!authed) {
    return (
      <Login
        onLogin={(t, isDefault) => {
          localStorage.setItem(TOKEN_KEY, t);
          setToken(t);
          setTokenDefault(isDefault);
          setAuthed(true);
        }}
      />
    );
  }

  return (
    <div className="admin">
      <header className="admin__bar">
        <h1>Voice AI · Admin</h1>
        <nav className="admin__tabs">
          {(["invitations", "agents", "config", "email", "calls", "logs"] as Tab[]).map((t) => (
            <button
              key={t}
              className={`admin__tab ${tab === t ? "is-active" : ""}`}
              onClick={() => setTab(t)}
            >
              {TAB_LABEL[t]}
            </button>
          ))}
        </nav>
        <div className="admin__baractions">
          <a className="admin__link" href="/">
            ← App
          </a>
          <button className="admin__link" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>

      {tokenDefault && (
        <div className="admin__warn">
          ⚠ Admin is using the default dev token. Set <code>ADMIN_TOKEN</code> in the
          environment to secure it.
        </div>
      )}

      <main className="admin__body">
        {tab === "invitations" && <InvitationsPanel token={token} onAuthFail={onAuthFail} />}
        {tab === "agents" && <AgentsPanel token={token} onAuthFail={onAuthFail} />}
        {tab === "config" && <ConfigPanel token={token} onAuthFail={onAuthFail} />}
        {tab === "email" && <EmailPanel token={token} onAuthFail={onAuthFail} />}
        {tab === "calls" && <CallsPanel token={token} onAuthFail={onAuthFail} />}
        {tab === "logs" && <LogsPanel token={token} onAuthFail={onAuthFail} />}
      </main>
    </div>
  );
}

function Login({ onLogin }: { onLogin: (t: string, isDefault: boolean) => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await adminApi.ping(value.trim());
      onLogin(value.trim(), r.admin_token_is_default);
    } catch (e: any) {
      setError(e instanceof Unauthorized ? "Invalid token." : e?.message || "Failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="admin admin--center">
      <div className="admin__login">
        <h1>Admin sign-in</h1>
        <p className="admin__muted">Enter the admin token (env ADMIN_TOKEN).</p>
        <input
          className="admin__input"
          type="password"
          placeholder="admin token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && value.trim() && !busy && submit()}
          autoFocus
        />
        {error && <p className="admin__error">{error}</p>}
        <button className="btn btn--primary" onClick={submit} disabled={busy || !value.trim()}>
          {busy ? "Checking…" : "Sign in"}
        </button>
      </div>
    </div>
  );
}

interface PanelProps {
  token: string;
  onAuthFail: () => void;
}

const STATUS_DOT: Record<string, string> = {
  sent: "warn",
  verified: "on",
  consumed: "off",
  revoked: "err",
};

function InvitationsPanel({ token, onAuthFail }: PanelProps) {
  const [items, setItems] = useState<Invitation[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [devMode, setDevMode] = useState(false);
  const [method, setMethod] = useState("email");
  const [value, setValue] = useState("");
  const [label, setLabel] = useState("");
  const [agentSlug, setAgentSlug] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [r, a] = await Promise.all([adminApi.getInvitations(token), adminApi.getAgents(token)]);
      setItems(r.invitations);
      setDevMode(r.dev_mode);
      setAgents(a.agents.filter((x) => x.enabled));
    } catch (e: any) {
      if (e instanceof Unauthorized) return onAuthFail();
      setError(e?.message || "Failed to load invitations.");
    }
  }, [token, onAuthFail]);

  useEffect(() => {
    load();
    const id = setInterval(load, 6000);
    return () => clearInterval(id);
  }, [load]);

  const act = async (fn: () => Promise<any>, ok: string) => {
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      await fn();
      setMsg(ok);
      await load();
    } catch (e: any) {
      if (e instanceof Unauthorized) return onAuthFail();
      setError(e?.message || "Action failed.");
    } finally {
      setBusy(false);
    }
  };

  const create = () =>
    act(async () => {
      await adminApi.createInvitation(
        token, method, value.trim(), label.trim() || undefined, agentSlug || undefined
      );
      setValue("");
      setLabel("");
    }, "Invitation sent.");

  return (
    <div className="panel">
      <div className="card">
        <h2 className="card__title">Send an invitation</h2>
        <p className="admin__muted">
          The recipient gets a one-time code and may start exactly one call. After it, they're
          locked out until you re-enable (which sends a fresh code).
        </p>
        <div className="inv-form">
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            <option value="email">Email</option>
            <option value="order">Order ID</option>
            <option value="phone">Phone</option>
          </select>
          <input
            placeholder={
              method === "email"
                ? "you@example.com"
                : method === "phone"
                ? "+15555550123"
                : "ORD-1001"
            }
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <input
            placeholder="label (optional)"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
          <select value={agentSlug} onChange={(e) => setAgentSlug(e.target.value)} title="Agent">
            <option value="">Default agent</option>
            {agents.map((a) => (
              <option key={a.slug} value={a.slug}>
                {a.name}
                {a.is_default ? " (default)" : ""}
              </option>
            ))}
          </select>
          <button className="btn btn--primary btn--sm" onClick={create} disabled={busy || !value.trim()}>
            {busy ? "Sending…" : "Send invite"}
          </button>
        </div>
        {devMode && (
          <p className="admin__muted">
            Dev mode (no email/SMS provider): the generated code is shown in the table below.
          </p>
        )}
        {msg && <p className="admin__ok">{msg}</p>}
        {error && <p className="admin__error">{error}</p>}
      </div>

      <div className="card">
        <h2 className="card__title">Invitations</h2>
        {items.length === 0 ? (
          <p className="admin__muted">None yet.</p>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Recipient</th>
                <th>Status</th>
                <th>Calls</th>
                {devMode && <th>Code</th>}
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.id}>
                  <td>
                    {i.destination_masked}
                    <span className="inv-chan"> · {i.channel}</span>
                    {i.label && <span className="inv-chan"> · {i.label}</span>}
                    <span className="inv-chan"> · {i.agent_slug || "default"}</span>
                  </td>
                  <td>
                    <span className={`dot dot--${STATUS_DOT[i.status] || "off"}`} />
                    {i.status}
                    {i.expired && i.status === "sent" && <span className="inv-chan"> (expired)</span>}
                  </td>
                  <td className="mono">
                    {i.calls_used}/{i.calls_allowed}
                  </td>
                  {devMode && <td className="mono">{i.has_code ? i.code : "—"}</td>}
                  <td className="inv-actions">
                    {i.status !== "revoked" && (
                      <button
                        className="admin__link"
                        disabled={busy}
                        onClick={() =>
                          act(() => adminApi.reenableInvitation(token, i.id), "Re-enabled + new code sent.")
                        }
                      >
                        {i.status === "consumed" ? "Allow again" : "Re-send"}
                      </button>
                    )}
                    {i.status !== "revoked" && (
                      <button
                        className="admin__link admin__link--danger"
                        disabled={busy}
                        onClick={() => act(() => adminApi.revokeInvitation(token, i.id), "Revoked.")}
                      >
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function EmailPanel({ token, onAuthFail }: PanelProps) {
  const [cfg, setCfg] = useState<EmailSettings | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [tls, setTls] = useState(true);
  const [password, setPassword] = useState("");
  const [pwSet, setPwSet] = useState(false);
  const [testTo, setTestTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const apply = (c: EmailSettings) => {
    setCfg(c);
    setForm({
      smtp_host: c.smtp_host || "",
      smtp_port: String(c.smtp_port || 587),
      smtp_user: c.smtp_user || "",
      smtp_from: c.smtp_from || "",
    });
    setTls(c.smtp_use_tls);
    setPwSet(c.password_set);
    setPassword("");
  };

  const load = useCallback(async () => {
    try {
      apply(await adminApi.getEmail(token));
    } catch (e: any) {
      if (e instanceof Unauthorized) return onAuthFail();
      setError(e?.message || "Failed to load email settings.");
    }
  }, [token, onAuthFail]);
  useEffect(() => {
    load();
  }, [load]);

  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));

  const save = async () => {
    setBusy(true);
    setMsg(null);
    setError(null);
    try {
      const body: Record<string, unknown> = {
        smtp_host: form.smtp_host?.trim() || "",
        smtp_port: Number(form.smtp_port) || 587,
        smtp_user: form.smtp_user?.trim() || "",
        smtp_from: form.smtp_from?.trim() || "",
        smtp_use_tls: tls,
      };
      if (password.trim()) body.smtp_password = password.trim();
      apply(await adminApi.saveEmail(token, body));
      setMsg("Saved.");
    } catch (e: any) {
      if (e instanceof Unauthorized) return onAuthFail();
      setError(e?.message || "Save failed.");
    } finally {
      setBusy(false);
    }
  };

  const sendTest = async () => {
    setBusy(true);
    setMsg(null);
    setError(null);
    try {
      const r = await adminApi.testEmail(token, testTo.trim());
      setMsg(`Test email sent to ${r.sent_to}.`);
    } catch (e: any) {
      if (e instanceof Unauthorized) return onAuthFail();
      setError(e?.message || "Test failed.");
    } finally {
      setBusy(false);
    }
  };

  if (!cfg) return <div className="admin__muted">Loading…</div>;

  return (
    <div className="panel">
      <div className="card">
        <h2 className="card__title">
          Email account (SMTP){" "}
          <span className={`badge ${cfg.configured ? "" : "badge--off"}`}>
            {cfg.configured ? "configured" : "not configured"}
          </span>
        </h2>
        <p className="admin__muted">
          The account invitations are sent from. Until a host is set, codes are shown
          in the Invitations tab (dev mode) instead of emailed. For Gmail/Workspace use
          an app password and host <code>smtp.gmail.com</code>, port <code>587</code>, TLS on.
        </p>

        <label className="ff">
          <span>SMTP host</span>
          <input value={form.smtp_host} onChange={(e) => set("smtp_host", e.target.value)} placeholder="smtp.gmail.com" />
        </label>
        <div className="ff--row">
          <label className="ff">
            <span>Port</span>
            <input value={form.smtp_port} onChange={(e) => set("smtp_port", e.target.value)} placeholder="587" />
          </label>
          <label className="ff" style={{ justifyContent: "flex-end" }}>
            <span>Use STARTTLS</span>
            <input type="checkbox" checked={tls} onChange={(e) => setTls(e.target.checked)} />
          </label>
        </div>
        <label className="ff">
          <span>Username</span>
          <input value={form.smtp_user} onChange={(e) => set("smtp_user", e.target.value)} placeholder="you@example.com" />
        </label>
        <label className="ff">
          <span>Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={pwSet ? "•••••••• (saved — type to replace)" : "app password"}
          />
        </label>
        <label className="ff">
          <span>From address</span>
          <input value={form.smtp_from} onChange={(e) => set("smtp_from", e.target.value)} placeholder="no-reply@example.com" />
        </label>

        {msg && <p className="admin__ok">{msg}</p>}
        {error && <p className="admin__error">{error}</p>}
        <div className="panel__actions">
          <button className="btn btn--primary" onClick={save} disabled={busy}>
            {busy ? "Saving…" : "Save email settings"}
          </button>
        </div>
      </div>

      <div className="card">
        <h2 className="card__title">Send a test email</h2>
        <div className="inv-form">
          <input
            placeholder="recipient@example.com"
            value={testTo}
            onChange={(e) => setTestTo(e.target.value)}
            style={{ flex: 1, minWidth: 220 }}
          />
          <button className="btn btn--ghost btn--sm" onClick={sendTest} disabled={busy || !testTo.trim()}>
            {busy ? "Sending…" : "Send test"}
          </button>
        </div>
        <p className="ff__hint">Verifies the SMTP account can actually deliver mail.</p>
      </div>
    </div>
  );
}

const BLANK_AGENT = {
  name: "", description: "", agent_persona: "", system_prompt: "",
  llm_provider: "", llm_model: "", llm_base_url: "", tts_voice: "", whisper_model: "",
  memory_enabled: "", memory_max_messages: "", history_max_messages: "",
};

function AgentsPanel({ token, onAuthFail }: PanelProps) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [cfg, setCfg] = useState<ConfigResponse | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null); // null = new
  const [form, setForm] = useState<Record<string, string>>({ ...BLANK_AGENT });
  const [apiKey, setApiKey] = useState("");
  const [keySet, setKeySet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [a, c] = await Promise.all([adminApi.getAgents(token), adminApi.getConfig(token)]);
      setAgents(a.agents);
      setCfg(c);
    } catch (e: any) {
      if (e instanceof Unauthorized) return onAuthFail();
      setError(e?.message || "Failed to load agents.");
    }
  }, [token, onAuthFail]);
  useEffect(() => {
    load();
  }, [load]);

  const newAgent = () => {
    setEditingId(null);
    setForm({ ...BLANK_AGENT });
    setApiKey("");
    setKeySet(false);
    setMsg(null);
    setError(null);
  };
  const editAgent = (a: Agent) => {
    setEditingId(a.id);
    setForm({
      name: a.name || "", description: a.description || "",
      agent_persona: a.agent_persona || "", system_prompt: a.system_prompt || "",
      llm_provider: a.llm_provider || "", llm_model: a.llm_model || "",
      llm_base_url: a.llm_base_url || "", tts_voice: a.tts_voice || "",
      whisper_model: a.whisper_model || "",
      memory_enabled: a.memory_enabled || "", memory_max_messages: a.memory_max_messages || "",
      history_max_messages: a.history_max_messages || "",
    });
    setApiKey("");
    setKeySet(a.llm_api_key_set);
    setMsg(null);
    setError(null);
  };

  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));
  const onPersona = (pid: string) => {
    const p = cfg?.personas.find((x) => x.id === pid);
    setForm((f) => ({ ...f, agent_persona: pid, system_prompt: p && p.prompt ? p.prompt : f.system_prompt }));
  };

  const reload = load;
  const act = async (fn: () => Promise<any>, ok: string) => {
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      await fn();
      setMsg(ok);
      await reload();
    } catch (e: any) {
      if (e instanceof Unauthorized) return onAuthFail();
      setError(e?.message || "Action failed.");
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!form.name.trim()) {
      setError("Agent needs a name.");
      return;
    }
    const body: Record<string, unknown> = { ...form };
    if (apiKey.trim()) body.llm_api_key = apiKey.trim();
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      if (editingId == null) {
        const r = await adminApi.createAgent(token, body);
        editAgent(r.agent);
        setMsg("Agent created.");
      } else {
        const r = await adminApi.updateAgent(token, editingId, body);
        editAgent(r.agent);
        setMsg("Saved. Applies to new calls.");
      }
      await reload();
    } catch (e: any) {
      if (e instanceof Unauthorized) return onAuthFail();
      setError(e?.message || "Save failed.");
    } finally {
      setBusy(false);
    }
  };

  const provider = cfg?.providers.find((p) => p.id === form.llm_provider);
  const showBaseUrl = !!provider && provider.kind !== "anthropic";

  return (
    <div className="panel">
      <div className="card">
        <div className="card__title">
          Agents <span className="admin__muted">— a library you can grow over time</span>
          <button className="btn btn--ghost btn--sm" style={{ marginLeft: "auto" }} onClick={newAgent}>
            + New agent
          </button>
        </div>
        {agents.length === 0 ? (
          <p className="admin__muted">No agents yet. Calls use the global Defaults until you add one.</p>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Name</th>
                <th>Slug</th>
                <th>Type / model</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {agents.map((a) => (
                <tr key={a.id} className={editingId === a.id ? "is-sel" : ""}>
                  <td>
                    {a.name} {a.is_default && <span className="badge">default</span>}
                    {!a.enabled && <span className="inv-chan"> (disabled)</span>}
                  </td>
                  <td className="mono">{a.slug}</td>
                  <td className="inv-chan">
                    {a.agent_persona || "—"} · {a.llm_provider || "inherit"}/{a.llm_model || "inherit"}
                  </td>
                  <td className="inv-actions">
                    <button className="admin__link" onClick={() => editAgent(a)} disabled={busy}>
                      Edit
                    </button>
                    {!a.is_default && (
                      <button
                        className="admin__link"
                        disabled={busy}
                        onClick={() => act(() => adminApi.setDefaultAgent(token, a.id), "Default set.")}
                      >
                        Make default
                      </button>
                    )}
                    <button
                      className="admin__link"
                      disabled={busy}
                      onClick={() => act(() => adminApi.updateAgent(token, a.id, { enabled: !a.enabled }), "Updated.")}
                    >
                      {a.enabled ? "Disable" : "Enable"}
                    </button>
                    <button
                      className="admin__link admin__link--danger"
                      disabled={busy}
                      onClick={() => act(() => adminApi.deleteAgent(token, a.id), "Deleted.")}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {cfg && (
        <div className="card">
          <h2 className="card__title">{editingId == null ? "New agent" : `Edit: ${form.name}`}</h2>
          <p className="admin__muted">
            Leave a field blank to inherit the global Defaults. Each agent can be invited
            independently (Invitations tab).
          </p>

          <label className="ff">
            <span>Name</span>
            <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="e.g. Support line" />
          </label>
          <label className="ff">
            <span>Description (optional)</span>
            <input value={form.description} onChange={(e) => set("description", e.target.value)} />
          </label>

          <label className="ff">
            <span>Agent type</span>
            <select value={form.agent_persona} onChange={(e) => onPersona(e.target.value)}>
              <option value="">— inherit —</option>
              {cfg.personas.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <label className="ff">
            <span>Role prompt</span>
            <textarea
              rows={5}
              value={form.system_prompt}
              placeholder="(inherits the global prompt if blank)"
              onChange={(e) => setForm((f) => ({ ...f, system_prompt: e.target.value, agent_persona: "custom" }))}
            />
          </label>

          <label className="ff">
            <span>Provider</span>
            <select value={form.llm_provider} onChange={(e) => set("llm_provider", e.target.value)}>
              <option value="">— inherit —</option>
              {cfg.providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <label className="ff">
            <span>Model</span>
            <input
              value={form.llm_model}
              onChange={(e) => set("llm_model", e.target.value)}
              placeholder={provider?.models[0] || "(inherit)"}
            />
          </label>
          {showBaseUrl && (
            <label className="ff">
              <span>Base URL (OpenAI-compatible)</span>
              <input
                value={form.llm_base_url}
                onChange={(e) => set("llm_base_url", e.target.value)}
                placeholder={provider?.base_url || "http://host:port/v1"}
              />
            </label>
          )}
          {provider && provider.id !== "ollama" && (
            <label className="ff">
              <span>API key {provider.key_env ? `(or env ${provider.key_env})` : ""}</span>
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={keySet ? "•••••••• (saved — type to replace)" : "inherit / not set"}
              />
            </label>
          )}

          <div className="ff--row">
            <label className="ff">
              <span>TTS voice</span>
              <input value={form.tts_voice} onChange={(e) => set("tts_voice", e.target.value)} placeholder="(inherit)" />
            </label>
            <label className="ff">
              <span>Whisper model</span>
              <input value={form.whisper_model} onChange={(e) => set("whisper_model", e.target.value)} placeholder="(inherit)" />
            </label>
          </div>

          <div className="ff--row">
            <label className="ff">
              <span>Memory</span>
              <select value={form.memory_enabled} onChange={(e) => set("memory_enabled", e.target.value)}>
                <option value="">— inherit —</option>
                <option value="true">Remember returning callers</option>
                <option value="false">Off</option>
              </select>
            </label>
            <label className="ff">
              <span>Reload N messages</span>
              <input
                value={form.memory_max_messages}
                onChange={(e) => set("memory_max_messages", e.target.value.replace(/\D/g, ""))}
                placeholder="(inherit)"
              />
            </label>
            <label className="ff">
              <span>Context cap (0=∞)</span>
              <input
                value={form.history_max_messages}
                onChange={(e) => set("history_max_messages", e.target.value.replace(/\D/g, ""))}
                placeholder="(inherit)"
              />
            </label>
          </div>

          {msg && <p className="admin__ok">{msg}</p>}
          {error && <p className="admin__error">{error}</p>}
          <div className="panel__actions">
            <button className="btn btn--primary" onClick={save} disabled={busy}>
              {busy ? "Saving…" : editingId == null ? "Create agent" : "Save changes"}
            </button>
            {editingId != null && (
              <button className="btn btn--ghost" onClick={newAgent} disabled={busy}>
                New agent
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function ConfigPanel({ token, onAuthFail }: PanelProps) {
  const [cfg, setCfg] = useState<ConfigResponse | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [apiKey, setApiKey] = useState(""); // write-only; sent only when typed
  const [keySet, setKeySet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const apply = useCallback((c: ConfigResponse) => {
    setForm({ ...c.settings });
    setKeySet(c.llm_api_key_set);
    setApiKey("");
  }, []);

  const load = useCallback(async () => {
    try {
      const c = await adminApi.getConfig(token);
      setCfg(c);
      apply(c);
    } catch (e: any) {
      if (e instanceof Unauthorized) return onAuthFail();
      setError(e?.message || "Failed to load config.");
    }
  }, [token, onAuthFail, apply]);

  useEffect(() => {
    load();
  }, [load]);

  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));

  // Switching agent type: fill the system prompt from the preset (unless custom).
  const onPersona = (pid: string) => {
    const p = cfg?.personas.find((x) => x.id === pid);
    setForm((f) => ({
      ...f,
      agent_persona: pid,
      system_prompt: p && p.prompt ? p.prompt : f.system_prompt,
    }));
  };

  // Switching provider: prefill a sensible model and clear any base-URL override
  // (so the new provider's default applies unless the user overrides it).
  const onProvider = (pid: string) => {
    const p = cfg?.providers.find((x) => x.id === pid);
    setForm((f) => {
      const next: Record<string, string> = { ...f, llm_provider: pid, llm_base_url: "" };
      if (p && p.models.length && !p.models.includes(f.llm_model)) {
        next.llm_model = p.models[0];
      }
      return next;
    });
  };

  const persist = async (fn: () => Promise<{ settings: Record<string, string>; llm_api_key_set: boolean }>, okMsg: string) => {
    setBusy(true);
    setMsg(null);
    setError(null);
    try {
      const r = await fn();
      if (cfg) setCfg({ ...cfg, settings: r.settings, llm_api_key_set: r.llm_api_key_set });
      setForm({ ...r.settings });
      setKeySet(r.llm_api_key_set);
      setApiKey("");
      setMsg(okMsg);
      load(); // refresh provider "configured" badges
    } catch (e: any) {
      if (e instanceof Unauthorized) return onAuthFail();
      setError(e?.message || "Failed.");
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    const patch: Record<string, string> = { ...form };
    delete patch.llm_api_key; // never send the masked blank
    if (apiKey.trim()) patch.llm_api_key = apiKey.trim();
    return persist(() => adminApi.saveConfig(token, patch), "Saved. Applies to new calls.");
  };
  const clearKey = () =>
    persist(() => adminApi.saveConfig(token, { llm_api_key: "" }), "API key cleared.");
  const reset = () => persist(() => adminApi.resetConfig(token), "Reset to environment defaults.");

  if (!cfg) return <div className="admin__muted">Loading…</div>;

  const provider = cfg.providers.find((p) => p.id === form.llm_provider);
  const showBaseUrl = !!provider && provider.kind !== "anthropic";
  const showKey = !!provider && provider.id !== "ollama"; // ollama is keyless
  const dirty =
    JSON.stringify(form) !== JSON.stringify(cfg.settings) || apiKey.trim() !== "";

  return (
    <div className="panel">
      <div className="card">
        <h2 className="card__title">Brain (LLM)</h2>

        <label className="ff">
          <span>Provider</span>
          <select value={form.llm_provider} onChange={(e) => onProvider(e.target.value)}>
            {cfg.providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label} {p.configured ? "" : "· not configured"}
              </option>
            ))}
          </select>
        </label>
        {provider && !provider.configured && (
          <p className="admin__error">
            {provider.id === "ollama"
              ? `Ollama not reachable at ${provider.base_url}.`
              : provider.key_env
              ? `No API key — set ${provider.key_env} in the env or enter one below.`
              : "Set a base URL below."}
          </p>
        )}

        <label className="ff">
          <span>Model</span>
          <input
            list="model-options"
            value={form.llm_model}
            onChange={(e) => set("llm_model", e.target.value)}
            placeholder={provider?.models[0] || "model name"}
          />
          <datalist id="model-options">
            {(provider?.models || []).map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
          {provider && provider.models.length === 0 && (
            <span className="ff__hint">Enter any model name your server exposes.</span>
          )}
        </label>

        {showBaseUrl && (
          <label className="ff">
            <span>Base URL (OpenAI-compatible /v1)</span>
            <input
              value={form.llm_base_url || ""}
              onChange={(e) => set("llm_base_url", e.target.value)}
              placeholder={provider?.base_url || "http://host:port/v1"}
            />
            <span className="ff__hint">
              Leave blank to use the provider default. Works for OpenAI, Gemini, Ollama,
              vLLM, Triton, or any compatible server.
            </span>
          </label>
        )}

        {showKey && (
          <label className="ff">
            <span>API key {provider?.key_env ? `(or env ${provider.key_env})` : "(optional)"}</span>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={keySet ? "•••••••• (saved — type to replace)" : "not set"}
            />
            <span className="ff__hint">
              Stored server-side; never shown back.{" "}
              {keySet && (
                <button type="button" className="admin__link" onClick={clearKey} disabled={busy}>
                  Clear saved key
                </button>
              )}
            </span>
          </label>
        )}
      </div>

      <div className="card">
        <h2 className="card__title">Agent type &amp; prompt</h2>
        <label className="ff">
          <span>What kind of voice agent is this?</span>
          <select value={form.agent_persona || "assistant"} onChange={(e) => onPersona(e.target.value)}>
            {cfg.personas.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
          <span className="ff__hint">
            {cfg.personas.find((p) => p.id === form.agent_persona)?.description ||
              "Pick a type to fill the prompt, or edit it directly below."}
          </span>
        </label>
        <label className="ff">
          <span>Role prompt (defines the agent; sent to the model)</span>
          <textarea
            rows={6}
            value={form.system_prompt}
            onChange={(e) => {
              // Manual edits make this a custom agent.
              setForm((f) => ({ ...f, system_prompt: e.target.value, agent_persona: "custom" }));
            }}
          />
        </label>
        {cfg.guardrails_enabled && (
          <p className="ff__hint guard">
            🛡 System guardrails are always applied on top of this prompt. The caller
            can't change the agent's role, override its instructions, or reveal its
            configuration during a call.
          </p>
        )}
      </div>

      <div className="card">
        <h2 className="card__title">Speech</h2>
        <label className="ff">
          <span>TTS voice (Piper)</span>
          <input value={form.tts_voice} onChange={(e) => set("tts_voice", e.target.value)} />
        </label>
        <label className="ff">
          <span>Whisper model (STT)</span>
          <input value={form.whisper_model} onChange={(e) => set("whisper_model", e.target.value)} />
          <span className="ff__hint">
            Runs on {cfg.whisper.device} / {cfg.whisper.compute_type}. Device &amp; compute type
            are env-only.
          </span>
        </label>
      </div>

      <div className="card">
        <h2 className="card__title">Memory</h2>
        <label className="ff ff--inline">
          <input
            type="checkbox"
            checked={(form.memory_enabled || "false") === "true"}
            onChange={(e) => set("memory_enabled", e.target.checked ? "true" : "false")}
          />
          <span>Remember returning callers (reload their prior conversation)</span>
        </label>
        <div className="ff--row">
          <label className="ff">
            <span>Prior messages to reload</span>
            <input
              value={form.memory_max_messages ?? "20"}
              onChange={(e) => set("memory_max_messages", e.target.value.replace(/\D/g, ""))}
            />
          </label>
          <label className="ff">
            <span>Max messages kept in a call (0 = unlimited)</span>
            <input
              value={form.history_max_messages ?? "0"}
              onChange={(e) => set("history_max_messages", e.target.value.replace(/\D/g, ""))}
            />
            <span className="ff__hint">Caps prompt growth on long calls.</span>
          </label>
        </div>
      </div>

      {msg && <p className="admin__ok">{msg}</p>}
      {error && <p className="admin__error">{error}</p>}

      <div className="panel__actions">
        <button className="btn btn--primary" onClick={save} disabled={busy || !dirty}>
          {busy ? "Saving…" : "Save changes"}
        </button>
        <button className="btn btn--ghost" onClick={reset} disabled={busy}>
          Reset to defaults
        </button>
      </div>
    </div>
  );
}

function fmtBytes(n: number | null) {
  if (!n) return "—";
  return n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} KB`;
}

function fmtDur(s: number | null) {
  if (s == null) return "—";
  const sec = Math.round(s);
  return sec >= 60 ? `${Math.floor(sec / 60)}m ${sec % 60}s` : `${sec}s`;
}

function CallsPanel({ token, onAuthFail }: PanelProps) {
  const [active, setActive] = useState(0);
  const [calls, setCalls] = useState<CallRow[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<CallDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const tick = useCallback(async () => {
    try {
      const r = await adminApi.getCalls(token);
      setCalls(r.calls);
      setActive(r.active);
      setError(null);
    } catch (e: any) {
      if (e instanceof Unauthorized) return onAuthFail();
      setError(e?.message || "Failed to load.");
    }
  }, [token, onAuthFail]);

  useEffect(() => {
    tick();
    const id = setInterval(tick, 5000);
    return () => clearInterval(id);
  }, [tick]);

  const open = async (sid: string) => {
    setSel(sid);
    setDetail(null);
    try {
      setDetail((await adminApi.getCall(token, sid)).call);
    } catch (e: any) {
      if (e instanceof Unauthorized) return onAuthFail();
      setError(e?.message || "Failed to load call.");
    }
  };

  return (
    <div className="panel">
      <div className="stat">
        <span className="stat__num">{active}</span>
        <span className="stat__label">active call{active === 1 ? "" : "s"}</span>
      </div>

      <div className="card">
        <h2 className="card__title">Calls</h2>
        {calls.length === 0 ? (
          <p className="admin__muted">No calls yet.</p>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Started</th>
                <th>Caller</th>
                <th>Agent</th>
                <th>Dur</th>
                <th>Turns</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {calls.map((c) => (
                <tr key={c.session_id} className={sel === c.session_id ? "is-sel" : ""}>
                  <td>
                    {c.started_at ? new Date(c.started_at).toLocaleString() : "—"}
                    {!c.ended_at && <span className="badge">live</span>}
                  </td>
                  <td>{c.caller || "—"}</td>
                  <td className="inv-chan">
                    {c.agent_slug || "default"}
                    {c.recording_id != null && <span className="inv-chan"> · 🎧</span>}
                  </td>
                  <td className="mono">{fmtDur(c.duration_seconds)}</td>
                  <td className="mono">{c.turn_count}</td>
                  <td>
                    <button className="admin__link" onClick={() => open(c.session_id)}>
                      Open
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {sel && (
        <div className="card">
          <h2 className="card__title">
            Call · <span className="mono">{sel}</span>
            <button
              className="admin__link"
              onClick={() => {
                setSel(null);
                setDetail(null);
              }}
            >
              close
            </button>
          </h2>
          {!detail ? (
            <p className="admin__muted">Loading…</p>
          ) : (
            <>
              {detail.recording_id != null && (
                <div className="rec">
                  <audio
                    controls
                    preload="none"
                    src={adminApi.recordingAudioUrl(token, detail.recording_id)}
                  />
                  <a
                    className="admin__link"
                    href={adminApi.recordingAudioUrl(token, detail.recording_id)}
                    download
                  >
                    Download audio
                  </a>
                </div>
              )}
              {detail.transcript ? (
                <pre className="transcript-text">{detail.transcript}</pre>
              ) : (
                <p className="admin__muted">
                  No transcript text yet (the call may still be in progress).
                </p>
              )}
            </>
          )}
        </div>
      )}
      {error && <p className="admin__error">{error}</p>}
    </div>
  );
}

const LEVELS = ["", "DEBUG", "INFO", "WARNING", "ERROR"];

function LogsPanel({ token, onAuthFail }: PanelProps) {
  const [logs, setLogs] = useState<LogRec[]>([]);
  const [level, setLevel] = useState("");
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  const tick = useCallback(async () => {
    try {
      const r = await adminApi.getLogs(token, level || undefined);
      setLogs(r.logs);
      setError(null);
    } catch (e: any) {
      if (e instanceof Unauthorized) return onAuthFail();
      setError(e?.message || "Failed to load logs.");
    }
  }, [token, level, onAuthFail]);

  useEffect(() => {
    if (paused) return;
    tick();
    const id = setInterval(tick, 3000);
    return () => clearInterval(id);
  }, [tick, paused]);

  useEffect(() => {
    if (!paused) endRef.current?.scrollIntoView();
  }, [logs, paused]);

  return (
    <div className="panel">
      <div className="logs__toolbar">
        <label className="ff ff--inline">
          <span>Level</span>
          <select value={level} onChange={(e) => setLevel(e.target.value)}>
            {LEVELS.map((l) => (
              <option key={l} value={l}>
                {l || "All"}
              </option>
            ))}
          </select>
        </label>
        <button className="btn btn--ghost btn--sm" onClick={() => setPaused((p) => !p)}>
          {paused ? "Resume" : "Pause"}
        </button>
        <span className="admin__muted">{logs.length} lines</span>
      </div>

      <div className="logview">
        {logs.map((l, i) => (
          <div key={i} className={`logline logline--${l.level.toLowerCase()}`}>
            <span className="logline__ts">{l.ts.split("T")[1]?.slice(0, 12)}</span>
            <span className="logline__lvl">{l.level}</span>
            <span className="logline__msg">{l.message}</span>
          </div>
        ))}
        <div ref={endRef} />
      </div>
      {error && <p className="admin__error">{error}</p>}
    </div>
  );
}
