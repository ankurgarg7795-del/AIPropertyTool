// Typed client for the FastAPI backend (see backend/app/schemas.py and docs/API.md).

export type Facing = "N" | "S" | "E" | "W" | "NE" | "NW" | "SE" | "SW";

export interface SearchFilters {
  transaction_type: "sale" | "rent" | null;
  property_types: string[];
  bhk_min: number | null;
  bhk_max: number | null;
  price_min_inr: number | null;
  price_max_inr: number | null;
  carpet_area_min_sqft: number | null;
  cities: string[];
  localities: string[];
  facing: Facing[];
  furnishing: string[];
  possession_status: string | null;
  max_maintenance_monthly_inr: number | null;
  must_have_amenities: string[];
  verified_only: boolean;
}

export interface ParsedQuery {
  filters: SearchFilters;
  soft_preferences: string[];
  semantic_query: string;
  clarification: string | null;
}

export interface SearchHit {
  listing_id: string;
  score: number;
  breakdown: Record<string, number>;
  title: string;
  price_inr: number | null;
  bhk: number | null;
  carpet_area_sqft: number | null;
  city: string | null;
  locality: string | null;
  facing: string | null;
  verified: boolean;
  why: string[];
}

export interface SearchResponse {
  query: string;
  parsed: ParsedQuery;
  total_candidates: number;
  hits: SearchHit[];
  relaxed_filters: string[];
  llm_used: boolean;
}

export interface ListingData {
  transaction_type: string;
  property_type: string;
  bhk: number | null;
  carpet_area_sqft: number | null;
  super_built_up_area_sqft: number | null;
  built_up_area_sqft: number | null;
  floor_number: number | null;
  total_floors: number | null;
  facing: string | null;
  furnishing: string | null;
  price_inr: number | null;
  maintenance_monthly_inr: number | null;
  possession_status: string | null;
  amenities: string[];
  address: { project_name: string | null; locality: string | null; city: string | null };
  rera_number: string | null;
  highlights: string[];
  seo_title: string;
  seo_description: string;
  missing_fields: string[];
  clarifying_questions: string[];
}

export interface Listing {
  id: string;
  status: string;
  data: ListingData;
  quality_score: number;
  verification: { status: string; badge: string | null; buyer_summary: string } | null;
}

export interface UploadListingResponse {
  listing: Listing;
  status: string;
  ai_used: boolean;
  clarifying_questions: string[];
  warnings: string[];
  timings_ms: Record<string, number>;
}

export interface ChatAction {
  type: "show_listings" | "offer_slots" | "booking_confirmed" | "handoff_human" | "request_document";
  payload: Record<string, unknown>;
}

export interface ChatResponse {
  session_id: string;
  state: string;
  reply: string;
  lead_score: number;
  slots: Record<string, unknown>;
  actions: ChatAction[];
}

async function postJSON<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail ?? `HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

export const api = {
  search: (query: string, opts: { userId?: string; filters?: SearchFilters; signal?: AbortSignal } = {}) =>
    postJSON<SearchResponse>(
      "/api/v1/ai/search",
      { query, user_id: opts.userId, filters_override: opts.filters ?? null, limit: 12 },
      opts.signal,
    ),

  chat: (message: string, sessionId: string | null, listingId?: string) =>
    postJSON<ChatResponse>("/api/v1/ai/agent-chat", { message, session_id: sessionId, listing_id: listingId }),

  /** Multipart upload with progress (fetch has no upload progress, so XHR). */
  uploadListing: (form: FormData, onProgress: (pct: number) => void) =>
    new Promise<UploadListingResponse>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/v1/ai/upload-listing");
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100));
      xhr.onload = () => {
        const body = JSON.parse(xhr.responseText || "{}");
        if (xhr.status >= 200 && xhr.status < 300) resolve(body);
        else reject(new Error(typeof body.detail === "string" ? body.detail : `HTTP ${xhr.status}`));
      };
      xhr.onerror = () => reject(new Error("Network error"));
      xhr.send(form);
    }),
};

export function formatINR(v: number | null | undefined): string {
  if (v == null) return "Price on request";
  if (v >= 1e7) return `₹${(v / 1e7).toFixed(2)} Cr`;
  if (v >= 1e5) return `₹${(v / 1e5).toFixed(1)} L`;
  return `₹${v.toLocaleString("en-IN")}`;
}

export function getAnonUserId(): string {
  try {
    let id = localStorage.getItem("apt_uid");
    if (!id) {
      id = `anon_${crypto.randomUUID().slice(0, 8)}`;
      localStorage.setItem("apt_uid", id);
    }
    return id;
  } catch {
    return "anon_ephemeral";
  }
}
