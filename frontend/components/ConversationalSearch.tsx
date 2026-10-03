"use client";

import { useEffect, useRef, useState } from "react";
import { api, formatINR, getAnonUserId, type SearchFilters, type SearchResponse } from "@/lib/api";
import ConciergeChat from "./ConciergeChat";

type Turn = { id: number; query: string; result?: SearchResponse; error?: string; loading: boolean };

const EXAMPLES = [
  "Show me a sunlit 3BHK near tech hubs under ₹1.5 Cr with low maintenance and east facing",
  "2 BHK in Kharadi Pune, fully furnished, around 80L",
  "Verified 3 BHK in Hyderabad with a swimming pool, ready to move",
];

/** Renders the hard filters the AI understood as removable chips. */
function filterChips(f: SearchFilters): { key: string; label: string; clear: (f: SearchFilters) => SearchFilters }[] {
  const chips = [];
  if (f.bhk_min != null)
    chips.push({ key: "bhk", label: f.bhk_min === f.bhk_max ? `${f.bhk_min} BHK` : `${f.bhk_min}–${f.bhk_max ?? "+"} BHK`,
      clear: (x: SearchFilters) => ({ ...x, bhk_min: null, bhk_max: null }) });
  if (f.price_max_inr != null)
    chips.push({ key: "pmax", label: `≤ ${formatINR(f.price_max_inr)}`, clear: (x: SearchFilters) => ({ ...x, price_max_inr: null }) });
  if (f.price_min_inr != null)
    chips.push({ key: "pmin", label: `≥ ${formatINR(f.price_min_inr)}`, clear: (x: SearchFilters) => ({ ...x, price_min_inr: null }) });
  f.cities.forEach((c) => chips.push({ key: `c-${c}`, label: c, clear: (x: SearchFilters) => ({ ...x, cities: x.cities.filter((y) => y !== c) }) }));
  f.localities.forEach((l) => chips.push({ key: `l-${l}`, label: l, clear: (x: SearchFilters) => ({ ...x, localities: x.localities.filter((y) => y !== l) }) }));
  if (f.facing.length) chips.push({ key: "facing", label: `${f.facing.join("/")} facing`, clear: (x: SearchFilters) => ({ ...x, facing: [] }) });
  if (f.transaction_type) chips.push({ key: "txn", label: f.transaction_type === "rent" ? "For rent" : "For sale", clear: (x: SearchFilters) => ({ ...x, transaction_type: null }) });
  if (f.max_maintenance_monthly_inr != null)
    chips.push({ key: "maint", label: `Maint ≤ ${formatINR(f.max_maintenance_monthly_inr)}`, clear: (x: SearchFilters) => ({ ...x, max_maintenance_monthly_inr: null }) });
  if (f.verified_only) chips.push({ key: "ver", label: "AI-verified only", clear: (x: SearchFilters) => ({ ...x, verified_only: false }) });
  return chips;
}

export default function ConversationalSearch() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [chatListing, setChatListing] = useState<{ id: string; title: string } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => endRef.current?.scrollIntoView({ behavior: "smooth" }), [turns]);

  async function run(query: string, filters?: SearchFilters) {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const id = Date.now();
    setTurns((t) => [...t, { id, query, loading: true }]);
    try {
      const result = await api.search(query, { userId: getAnonUserId(), filters, signal: ctrl.signal });
      setTurns((t) => t.map((x) => (x.id === id ? { ...x, result, loading: false } : x)));
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      setTurns((t) => t.map((x) => (x.id === id ? { ...x, error: (e as Error).message, loading: false } : x)));
    }
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const q = input.trim();
    if (q.length < 2) return;
    setInput("");
    run(q);
  }

  return (
    <div className="search">
      {turns.length === 0 && (
        <div className="empty">
          <h1>Describe your next home.</h1>
          <p className="muted">No dropdowns. Ask the way you'd ask a friend who knows every listing in the city.</p>
          <div className="examples">
            {EXAMPLES.map((ex) => (
              <button key={ex} className="example" onClick={() => run(ex)}>{ex}</button>
            ))}
          </div>
        </div>
      )}

      <div className="thread" aria-live="polite">
        {turns.map((t) => (
          <section key={t.id} className="turn">
            <div className="bubble user">{t.query}</div>
            {t.loading && <div className="bubble ai muted">Understanding your request…</div>}
            {t.error && <div className="bubble ai error">Search failed: {t.error}</div>}
            {t.result && (
              <div className="bubble ai">
                <div className="chips">
                  {filterChips(t.result.parsed.filters).map((c) => (
                    <button key={c.key} className="chip" title="Remove this filter"
                      onClick={() => run(t.query, c.clear(t.result!.parsed.filters))}>
                      {c.label} ✕
                    </button>
                  ))}
                  {t.result.parsed.soft_preferences.map((p) => (
                    <span key={p} className="chip soft">{p}</span>
                  ))}
                </div>
                {t.result.parsed.clarification && <p>{t.result.parsed.clarification}</p>}
                <p className="muted small">
                  {t.result.hits.length} of {t.result.total_candidates} matching homes
                  {t.result.relaxed_filters.length > 0 && <> · few exact matches, relaxed: {t.result.relaxed_filters.join(", ")}</>}
                  {!t.result.llm_used && <> · offline parser</>}
                </p>
                <ul className="results">
                  {t.result.hits.map((h) => (
                    <li key={h.listing_id} className="card">
                      <div className="card-head">
                        <strong>{h.title}</strong>
                        {h.verified && <span className="badge">✓ AI-Verified Legal</span>}
                      </div>
                      <div className="facts">
                        <span className="price">{formatINR(h.price_inr)}</span>
                        {h.carpet_area_sqft && <span>{Math.round(h.carpet_area_sqft)} sq ft</span>}
                        {h.facing && <span>{h.facing} facing</span>}
                        <span>{[h.locality, h.city].filter(Boolean).join(", ")}</span>
                      </div>
                      {h.why.length > 0 && <div className="why">Why: {h.why.join(" · ")}</div>}
                      <div className="card-actions">
                        <span className="muted small">match {(h.score * 100).toFixed(0)}</span>
                        <button onClick={() => setChatListing({ id: h.listing_id, title: h.title })}>
                          Ask concierge / book visit
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>
        ))}
        <div ref={endRef} />
      </div>

      <form className="composer" onSubmit={submit}>
        <input value={input} onChange={(e) => setInput(e.target.value)} maxLength={1000}
          placeholder="e.g. quiet 2 BHK near a metro in Pune under 90 L" aria-label="Describe the home you want" />
        <button type="submit" disabled={input.trim().length < 2}>Search</button>
      </form>

      {chatListing && <ConciergeChat listingId={chatListing.id} title={chatListing.title} onClose={() => setChatListing(null)} />}
    </div>
  );
}
