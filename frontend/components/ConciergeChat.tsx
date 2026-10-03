"use client";

import { useEffect, useRef, useState } from "react";
import { api, type ChatAction } from "@/lib/api";

type Msg = { role: "user" | "ai"; text: string; actions?: ChatAction[] };
type Slot = { slot_id: string; label: string };

export default function ConciergeChat({ listingId, title, onClose }: { listingId: string; title: string; onClose: () => void }) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [state, setState] = useState("GREETING");
  const [score, setScore] = useState(0);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const started = useRef(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => endRef.current?.scrollIntoView({ behavior: "smooth" }), [msgs]);

  async function send(text: string, silent = false) {
    if (!silent) setMsgs((m) => [...m, { role: "user", text }]);
    setBusy(true);
    try {
      const r = await api.chat(text, sessionId, listingId);
      setSessionId(r.session_id);
      setState(r.state);
      setScore(r.lead_score);
      setMsgs((m) => [...m, { role: "ai", text: r.reply, actions: r.actions }]);
    } catch (e) {
      setMsgs((m) => [...m, { role: "ai", text: `Sorry, something went wrong (${(e as Error).message}).` }]);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (started.current) return; // StrictMode double-invoke guard
    started.current = true;
    send("Hi, I'm interested in this property", true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <aside className="drawer" role="dialog" aria-label={`Concierge for ${title}`}>
      <header>
        <div>
          <strong>Concierge</strong>
          <div className="muted small">{title}</div>
        </div>
        <div className="small muted">{state.replace("_", " ").toLowerCase()} · lead {score}</div>
        <button className="ghost" onClick={onClose} aria-label="Close">✕</button>
      </header>
      <div className="drawer-body">
        {msgs.map((m, i) => (
          <div key={i} className={`bubble ${m.role}`}>
            <div style={{ whiteSpace: "pre-wrap" }}>{m.text}</div>
            {m.actions?.map((a, j) => {
              if (a.type === "offer_slots") {
                const slots = (a.payload.slots as Slot[]) ?? [];
                return (
                  <div key={j} className="chips">
                    {slots.map((s, k) => (
                      <button key={s.slot_id} className="chip" disabled={busy || i !== msgs.length - 1}
                        onClick={() => send(String(k + 1))}>{s.label}</button>
                    ))}
                  </div>
                );
              }
              if (a.type === "booking_confirmed") return <div key={j} className="badge">📅 Visit booked</div>;
              if (a.type === "handoff_human") return <div key={j} className="badge warn">Relationship manager notified</div>;
              return null;
            })}
          </div>
        ))}
        {busy && <div className="bubble ai muted">…</div>}
        <div ref={endRef} />
      </div>
      <form className="composer" onSubmit={(e) => { e.preventDefault(); const t = input.trim(); if (t) { setInput(""); send(t); } }}>
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask anything or answer here…" disabled={busy} />
        <button type="submit" disabled={busy || !input.trim()}>Send</button>
      </form>
    </aside>
  );
}
