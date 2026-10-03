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

export interface User {
  id: string;
  phone_e164: string | null;
  full_name: string | null;
  roles: string[];
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function errorFrom(res: Response): Promise<ApiError> {
  const detail = (await res.json().catch(() => null))?.detail;
  return new ApiError(typeof detail === "string" ? detail : `HTTP ${res.status}`, res.status);
}

// One refresh in flight at a time; concurrent 401s wait for the same rotation
// (a second rotation with the already-used cookie would trip reuse detection).
let refreshing: Promise<boolean> | null = null;
export function refreshSession(): Promise<boolean> {
  refreshing ??= fetch("/api/v1/auth/refresh", { method: "POST", credentials: "same-origin" })
    .then((r) => r.ok)
    .catch(() => false)
    .finally(() => { refreshing = null; });
  return refreshing;
}

/** fetch with session cookies; on 401 rotates the refresh token once and retries. */
async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const go = () => fetch(path, { credentials: "same-origin", ...init });
  let res = await go();
  if (res.status === 401 && !path.startsWith("/api/v1/auth/") && (await refreshSession())) res = await go();
  return res;
}

async function postJSON<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw await errorFrom(res);
  return res.json() as Promise<T>;
}

function uploadOnce(form: FormData, onProgress: (pct: number) => void) {
  return new Promise<{ status: number; body: any }>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/v1/ai/upload-listing");
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100));
    xhr.onload = () => resolve({ status: xhr.status, body: JSON.parse(xhr.responseText || "{}") });
    xhr.onerror = () => reject(new Error("Network error"));
    xhr.send(form);
  });
}

export const api = {
  search: (query: string, opts: { filters?: SearchFilters; signal?: AbortSignal } = {}) =>
    postJSON<SearchResponse>(
      "/api/v1/ai/search",
      { query, filters_override: opts.filters ?? null, limit: 12 },
      opts.signal,
    ),

  chat: (message: string, sessionId: string | null, listingId?: string) =>
    postJSON<ChatResponse>("/api/v1/ai/agent-chat", { message, session_id: sessionId, listing_id: listingId }),

  /** Multipart upload with progress (fetch has no upload progress, so XHR). */
  async uploadListing(form: FormData, onProgress: (pct: number) => void): Promise<UploadListingResponse> {
    let r = await uploadOnce(form, onProgress);
    if (r.status === 401 && (await refreshSession())) r = await uploadOnce(form, onProgress);
    if (r.status >= 200 && r.status < 300) return r.body;
    throw new ApiError(typeof r.body.detail === "string" ? r.body.detail : `HTTP ${r.status}`, r.status);
  },

  requestOtp: (phone: string) =>
    postJSON<{ phone_e164: string; expires_in: number; dev_code: string | null }>("/api/v1/auth/otp/request", { phone }),

  verifyOtp: (phone: string, code: string) =>
    postJSON<{ user: User }>("/api/v1/auth/otp/verify", { phone, code }).then((r) => r.user),

  async me(): Promise<User | null> {
    const res = await request("/api/v1/auth/me");
    if (res.status === 401 && (await refreshSession())) {
      const retry = await request("/api/v1/auth/me");
      return retry.ok ? retry.json() : null;
    }
    return res.ok ? res.json() : null;
  },

  logout: () => fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" }).then(() => undefined),
};

export function formatINR(v: number | null | undefined): string {
  if (v == null) return "Price on request";
  if (v >= 1e7) return `₹${(v / 1e7).toFixed(2)} Cr`;
  if (v >= 1e5) return `₹${(v / 1e5).toFixed(1)} L`;
  return `₹${v.toLocaleString("en-IN")}`;
}
