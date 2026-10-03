"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, formatINR, type UploadListingResponse } from "@/lib/api";

const MEDIA_ACCEPT = "image/jpeg,image/png,image/webp,video/*,application/pdf,audio/*";
const DOC_ACCEPT = "application/pdf,image/jpeg,image/png";
const MAX_MB = 200;

type Picked = { file: File; url?: string };

// Minimal typing for the Web Speech API (not in lib.dom for all TS versions).
type SpeechRec = {
  lang: string; continuous: boolean; interimResults: boolean;
  onresult: ((e: { resultIndex: number; results: ArrayLike<{ 0: { transcript: string }; isFinal: boolean }> }) => void) | null;
  onend: (() => void) | null; start(): void; stop(): void;
};

function DropZone({ label, hint, accept, files, onAdd, onRemove }: {
  label: string; hint: string; accept: string; files: Picked[];
  onAdd: (f: File[]) => void; onRemove: (i: number) => void;
}) {
  const [over, setOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <div className={`dropzone ${over ? "over" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); onAdd(Array.from(e.dataTransfer.files)); }}
      onClick={() => inputRef.current?.click()} role="button" tabIndex={0}
      onKeyDown={(e) => e.key === "Enter" && inputRef.current?.click()}>
      <input ref={inputRef} type="file" multiple accept={accept} hidden
        onChange={(e) => { onAdd(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
      <strong>{label}</strong>
      <div className="muted small">{hint}</div>
      {files.length > 0 && (
        <ul className="thumbs" onClick={(e) => e.stopPropagation()}>
          {files.map((p, i) => (
            <li key={`${p.file.name}-${i}`}>
              {p.url ? <img src={p.url} alt={p.file.name} /> : <span className="filetype">{p.file.name.split(".").pop()}</span>}
              <span className="small">{p.file.name}</span>
              <button type="button" className="ghost" onClick={() => onRemove(i)} aria-label={`Remove ${p.file.name}`}>✕</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function ListingUploader() {
  const [media, setMedia] = useState<Picked[]>([]);
  const [docs, setDocs] = useState<Picked[]>([]);
  const [notes, setNotes] = useState("");
  const [role, setRole] = useState<"owner" | "agent" | "developer">("owner");
  const [recording, setRecording] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  const [result, setResult] = useState<UploadListingResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const speechRef = useRef<SpeechRec | null>(null);
  const chunks = useRef<Blob[]>([]);

  const add = useCallback((setter: typeof setMedia) => (files: File[]) => {
    const ok = files.filter((f) => f.size <= MAX_MB * 1024 * 1024);
    if (ok.length < files.length) setError(`Files over ${MAX_MB} MB were skipped`);
    setter((prev) => [...prev, ...ok.map((file) => ({ file, url: file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined }))]);
  }, []);
  const remove = (setter: typeof setMedia) => (i: number) =>
    setter((prev) => { if (prev[i]?.url) URL.revokeObjectURL(prev[i].url!); return prev.filter((_, j) => j !== i); });

  useEffect(() => () => [...media, ...docs].forEach((p) => p.url && URL.revokeObjectURL(p.url)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []);

  async function toggleRecording() {
    if (recording) {
      recRef.current?.stop();
      speechRef.current?.stop();
      setRecording(false);
      return;
    }
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      chunks.current = [];
      rec.ondataavailable = (e) => e.data.size && chunks.current.push(e.data);
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const type = rec.mimeType || "audio/webm";
        const file = new File(chunks.current, `voice-note-${Date.now()}.${type.includes("mp4") ? "m4a" : "webm"}`, { type });
        setMedia((prev) => [...prev, { file }]);
      };
      rec.start();
      recRef.current = rec;
      // Live transcript in the browser when available (Chrome/Edge); the server
      // also transcribes the audio file, so this is a fast path, not a dependency.
      const W = window as unknown as { SpeechRecognition?: new () => SpeechRec; webkitSpeechRecognition?: new () => SpeechRec };
      const SR = W.SpeechRecognition ?? W.webkitSpeechRecognition;
      if (SR) {
        const sr = new SR();
        sr.lang = "en-IN";
        sr.continuous = true;
        sr.interimResults = false;
        sr.onresult = (e) => {
          let text = "";
          for (let i = e.resultIndex; i < e.results.length; i++) if (e.results[i].isFinal) text += e.results[i][0].transcript + " ";
          if (text) setTranscript((t) => (t + " " + text).trim());
        };
        sr.start();
        speechRef.current = sr;
      }
      setRecording(true);
    } catch {
      setError("Microphone permission denied");
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!media.length && !docs.length && !notes.trim() && !transcript.trim()) {
      setError("Add photos, a video, a voice note or a few words about the property");
      return;
    }
    setError(null);
    setResult(null);
    const form = new FormData();
    media.forEach((p) => form.append("files", p.file));
    docs.forEach((p) => form.append("documents", p.file));
    if (notes.trim()) form.append("notes", notes.trim());
    if (transcript.trim()) form.append("transcript", transcript.trim());
    form.append("owner_role", role);
    setProgress(0);
    try {
      setResult(await api.uploadListing(form, setProgress));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setProgress(null);
    }
  }

  const d = result?.listing.data;
  return (
    <form className="uploader" onSubmit={submit}>
      <h1>List your property in one step</h1>
      <p className="muted">Drop photos, a walkthrough video, a floor plan or just talk. The AI fills in every field. Listing is free.</p>

      <div className="roles">
        {(["owner", "agent", "developer"] as const).map((r) => (
          <label key={r} className={`chip ${role === r ? "active" : ""}`}>
            <input type="radio" name="role" value={r} checked={role === r} onChange={() => setRole(r)} hidden />
            {r === "owner" ? "Owner" : r === "agent" ? "Agent / Broker" : "Developer"}
          </label>
        ))}
      </div>

      <DropZone label="Photos, videos, floor plans, voice notes" hint="JPG/PNG/WebP, MP4/MOV, PDF, MP3/M4A — up to 200 MB each"
        accept={MEDIA_ACCEPT} files={media} onAdd={add(setMedia)} onRemove={remove(setMedia)} />

      <div className="row">
        <button type="button" className={recording ? "danger" : "secondary"} onClick={toggleRecording}>
          {recording ? "■ Stop recording" : "🎙 Describe it by voice"}
        </button>
        {transcript && <span className="muted small">Heard: “{transcript.slice(0, 120)}{transcript.length > 120 ? "…" : ""}”</span>}
      </div>

      <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3} maxLength={5000}
        placeholder="Optional: anything the photos don't show — price, maintenance, possession date…" />

      <DropZone label="Legal documents (optional) → AI-Verified badge" hint="RERA certificate, title/sale deed, OC, tax receipt. Stored encrypted; never shown publicly."
        accept={DOC_ACCEPT} files={docs} onAdd={add(setDocs)} onRemove={remove(setDocs)} />

      {error && <div className="error">{error}</div>}
      {progress !== null && (
        <div className="progress" aria-label="Upload progress">
          <div style={{ width: `${progress}%` }} />
          <span className="small">{progress < 100 ? `Uploading ${progress}%` : "AI is reading your media…"}</span>
        </div>
      )}
      <button type="submit" disabled={progress !== null}>Create listing</button>

      {result && d && (
        <section className="card result">
          <div className="card-head">
            <strong>{d.seo_title}</strong>
            <span className={`badge ${result.status === "live" ? "" : "warn"}`}>{result.status.replace("_", " ")}</span>
            {result.listing.verification?.badge && <span className="badge">✓ {result.listing.verification.badge}</span>}
          </div>
          <div className="facts">
            <span className="price">{formatINR(d.price_inr)}</span>
            {d.bhk != null && <span>{d.bhk} BHK</span>}
            {(d.carpet_area_sqft ?? d.super_built_up_area_sqft) && <span>{Math.round((d.carpet_area_sqft ?? d.super_built_up_area_sqft)!)} sq ft</span>}
            {d.floor_number != null && <span>Floor {d.floor_number}{d.total_floors ? `/${d.total_floors}` : ""}</span>}
            {d.facing && <span>{d.facing} facing</span>}
            <span>{[d.address.locality, d.address.city].filter(Boolean).join(", ")}</span>
          </div>
          <p>{d.seo_description}</p>
          {d.amenities.length > 0 && <div className="chips">{d.amenities.map((a) => <span key={a} className="chip soft">{a}</span>)}</div>}
          {result.clarifying_questions.length > 0 && (
            <div className="questions">
              <strong>A few quick questions to go live:</strong>
              <ul>{result.clarifying_questions.map((q) => <li key={q}>{q}</li>)}</ul>
              <span className="muted small">Answer in the notes box (or by voice) and submit again.</span>
            </div>
          )}
          {result.listing.verification && <p className="small">Legal check: {result.listing.verification.buyer_summary}</p>}
          <div className="muted small">
            Quality {(result.listing.quality_score * 100).toFixed(0)}/100 · {result.ai_used ? "AI extraction" : "offline parser"} ·{" "}
            {Object.entries(result.timings_ms).map(([k, v]) => `${k} ${v}ms`).join(", ")}
          </div>
          {result.warnings.length > 0 && <ul className="warnings small">{result.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
        </section>
      )}
    </form>
  );
}
