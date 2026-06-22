import { useState } from "react";
import { inviteTokenFromUrl, verifyCode } from "./api";

// Invitation-only: a user can't start a call themselves. An admin sends a code
// (email/SMS); the user enters it here, identified either by an invite-link
// token (?inv=...) or by the email/phone it was sent to.
export default function VerificationGate({
  onVerified,
}: {
  onVerified: (ticket: string, agent: string | null, cid: string | null) => void;
}) {
  const token = inviteTokenFromUrl();
  const [destination, setDestination] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = code.trim().length > 0 && (!!token || destination.trim().length > 0);

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const res = await verifyCode({
        token: token || undefined,
        destination: token ? undefined : destination.trim(),
        code: code.trim(),
      });
      onVerified(res.ticket, res.agent ?? null, res.cid ?? null);
    } catch (e: any) {
      setError(e?.message || "Could not verify the code.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="gate">
      <h2 className="gate__title">Enter your invitation code</h2>
      <p className="gate__sub">
        Calls are by invitation. Enter the code an administrator sent you
        {token ? "." : " and the email or phone it was sent to."}
      </p>

      {!token && (
        <label className="field">
          <span className="field__label">Email or phone</span>
          <input
            className="field__input"
            type="text"
            placeholder="you@example.com or +15555550123"
            value={destination}
            onChange={(e) => setDestination(e.target.value)}
            autoFocus
          />
        </label>
      )}

      <label className="field">
        <span className="field__label">Invitation code</span>
        <input
          className="field__input field__input--code"
          inputMode="numeric"
          autoComplete="one-time-code"
          placeholder="123456"
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
          onKeyDown={(e) => e.key === "Enter" && canSubmit && !busy && submit()}
          autoFocus={!!token}
        />
      </label>

      {error && <p className="gate__error">{error}</p>}

      <button className="btn btn--primary gate__btn" onClick={submit} disabled={busy || !canSubmit}>
        {busy ? "Verifying…" : "Verify & continue"}
      </button>

      <p className="gate__dev">
        No code? An administrator issues invitations from the{" "}
        <a className="gate__link-a" href="/admin">
          admin console
        </a>
        .
      </p>
    </div>
  );
}
