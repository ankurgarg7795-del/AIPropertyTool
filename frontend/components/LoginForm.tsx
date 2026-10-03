"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { useAuth } from "./AuthProvider";

/** Only allow same-site relative redirects after login. */
function safeNext(next: string | null): string {
  return next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
}

export default function LoginForm() {
  const router = useRouter();
  const next = safeNext(useSearchParams().get("next"));
  const { setUser } = useAuth();
  const [phone, setPhone] = useState("");
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [devCode, setDevCode] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api.requestOtp(phone);
      setSentTo(r.phone_e164);
      setDevCode(r.dev_code);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function verify(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setUser(await api.verifyOtp(sentTo!, code));
      router.replace(next);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="login card">
      <h1>Sign in</h1>
      {!sentTo ? (
        <form onSubmit={send}>
          <label className="small muted" htmlFor="phone">Mobile number</label>
          <input id="phone" inputMode="tel" autoComplete="tel" value={phone} onChange={(e) => setPhone(e.target.value)}
            placeholder="98765 43210" required />
          <button type="submit" disabled={busy || phone.trim().length < 8}>Send code</button>
        </form>
      ) : (
        <form onSubmit={verify}>
          <p className="small muted">Enter the 6-digit code sent to {sentTo}.</p>
          {devCode && <p className="small badge warn">Dev mode code: {devCode}</p>}
          <input inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} placeholder="••••••" required autoFocus />
          <button type="submit" disabled={busy || code.length !== 6}>Verify and continue</button>
          <button type="button" className="ghost" onClick={() => { setSentTo(null); setCode(""); }}>Change number</button>
        </form>
      )}
      {error && <div className="error">{error}</div>}
    </div>
  );
}
