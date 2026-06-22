import { useCallback, useEffect, useRef, useState } from "react";
import { PipecatClient } from "@pipecat-ai/client-js";
import { WebSocketTransport } from "@pipecat-ai/websocket-transport";
import { AGENT_URL } from "./api";
import VerificationGate from "./VerificationGate";

type Role = "user" | "assistant";
interface Turn {
  role: Role;
  text: string;
}
type BotState = "idle" | "listening" | "thinking" | "speaking";

const STATE_LABEL: Record<BotState, string> = {
  idle: "Idle",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
};

export default function App() {
  const clientRef = useRef<PipecatClient | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const assistantOpenRef = useRef(false);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);

  const [status, setStatus] = useState("disconnected");
  const [botState, setBotState] = useState<BotState>("idle");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  // Single-use ticket from the verification gate; required to start a call.
  const [ticket, setTicket] = useState<string | null>(null);
  // Which agent the invitation routes this caller to (null = default).
  const [agent, setAgent] = useState<string | null>(null);
  // Opaque caller id (invitation token) for cross-call memory.
  const [cid, setCid] = useState<string | null>(null);

  const connected =
    status === "connected" || status === "ready" || botState !== "idle";

  const appendTurn = useCallback((role: Role, text: string) => {
    const clean = (text || "").trim();
    if (!clean) return;
    setTurns((prev) => {
      const last = prev[prev.length - 1];
      // Merge streamed assistant sentences into the current assistant turn.
      if (role === "assistant" && assistantOpenRef.current && last?.role === "assistant") {
        const next = prev.slice();
        next[next.length - 1] = { role, text: `${last.text} ${clean}`.trim() };
        return next;
      }
      return [...prev, { role, text: clean }];
    });
  }, []);

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  const ensureClient = useCallback(() => {
    if (clientRef.current) return clientRef.current;
    const client = new PipecatClient({
      transport: new WebSocketTransport(),
      enableMic: true,
      enableCam: false,
      callbacks: {
        onTransportStateChanged: (s: any) => setStatus(String(s)),
        onConnected: () => setStatus("connected"),
        onDisconnected: () => {
          setStatus("disconnected");
          setBotState("idle");
        },
        onBotReady: () => setBotState("listening"),
        onUserStartedSpeaking: () => {
          assistantOpenRef.current = false;
          setBotState("listening");
        },
        onUserStoppedSpeaking: () => setBotState("thinking"),
        onBotStartedSpeaking: () => setBotState("speaking"),
        onBotStoppedSpeaking: () => setBotState("listening"),
        onUserTranscript: (t: any) => {
          if (t?.final) appendTurn("user", t.text);
        },
        onBotTranscript: (t: any) => {
          appendTurn("assistant", t?.text ?? "");
          assistantOpenRef.current = true;
        },
        onTrackStarted: (track: MediaStreamTrack, p: any) => {
          if (!p?.local && track.kind === "audio" && audioRef.current) {
            audioRef.current.srcObject = new MediaStream([track]);
          }
        },
      },
    });
    clientRef.current = client;
    return client;
  }, [appendTurn]);

  const connect = useCallback(async () => {
    if (!ticket) return;
    setBusy(true);
    setTurns([]);
    const client = ensureClient();
    try {
      await client.initDevices();
      // Pipecat runner's /start returns where to connect the WebSocket. The
      // ticket from the verification gate is enforced server-side by AuthGate.
      const res: any = await client.startBot({
        endpoint: `${AGENT_URL}/start?ticket=${encodeURIComponent(ticket)}`,
        requestData: { transport: "websocket" },
      });
      const wsBase = AGENT_URL.replace(/^http/i, "ws");
      const token = res?.token;
      const params = new URLSearchParams();
      if (token) params.set("token", token);
      if (agent) params.set("agent", agent); // route this call to the chosen agent
      if (cid) params.set("cid", cid); // caller id for cross-call memory
      const qs = params.toString();
      const wsUrl = `${wsBase}/ws-client${qs ? `?${qs}` : ""}`;
      await client.connect({ wsUrl });
    } catch (e) {
      console.error("connect failed", e);
      setStatus("error");
      // Ticket likely expired/used — send the user back through verification.
      setTicket(null);
    } finally {
      setBusy(false);
    }
  }, [ensureClient, ticket, agent, cid]);

  const disconnect = useCallback(async () => {
    try {
      await clientRef.current?.disconnect();
    } catch (e) {
      console.error(e);
    }
  }, []);

  return (
    <div className="app">
      <header className="header">
        <h1>Voice AI</h1>
        <span className={`pill pill--${connected ? "on" : "off"}`}>{status}</span>
      </header>

      <main className="stage">
        {!ticket ? (
          <VerificationGate
            onVerified={(t, a, c) => {
              setAgent(a);
              setCid(c);
              setTicket(t);
            }}
          />
        ) : (
          <>
            <div className={`orb orb--${botState}`}>
              <div className="orb__core" />
              <div className="orb__label">{STATE_LABEL[botState]}</div>
            </div>

            <div className="controls">
              {!connected ? (
                <button className="btn btn--primary" onClick={connect} disabled={busy}>
                  {busy ? "Connecting…" : "Start conversation"}
                </button>
              ) : (
                <button className="btn btn--ghost" onClick={disconnect}>
                  End conversation
                </button>
              )}
            </div>

            <section className="transcript" aria-label="transcript">
              {turns.length === 0 && (
                <p className="transcript__empty">
                  Verified ✓ — press start and speak. Whisper transcribes, Claude
                  replies, Piper speaks.
                </p>
              )}
              {turns.map((t, i) => (
                <div key={i} className={`bubble bubble--${t.role}`}>
                  <span className="bubble__who">{t.role === "user" ? "You" : "AI"}</span>
                  <span className="bubble__text">{t.text}</span>
                </div>
              ))}
              <div ref={transcriptEndRef} />
            </section>
          </>
        )}
      </main>

      <footer className="footer">
        Self-hosted Whisper + Piper · Claude brain · interrupt anytime by speaking
      </footer>

      {/* Bot audio playback */}
      <audio ref={audioRef} autoPlay playsInline />
    </div>
  );
}
