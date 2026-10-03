"""PostgreSQL implementation of ``Database`` (psycopg 3, async pool, pgvector).

Schema: ``sql/001_schema.sql`` + ``003_app_wiring.sql``. Listings are keyed
publicly by ``properties.public_id``; every other id is the table's uuid
rendered as text.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.db.base import Candidate, Database, OtpRecord, RefreshRecord, SlotTaken
from app.schemas import (
    BuyerProfile, ConciergeSession, Event, Listing, ListingExtraction, Notification, PricePoint,
    SearchFilters, User, VerificationResult,
)

CHANNEL_TO_DB = {"web": "in_app", "app": "in_app", "whatsapp": "whatsapp"}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _uuid_or_none(v: str | None) -> str | None:
    return v if v and _UUID_RE.match(v) else None


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _possession_date(s: str | None) -> date | None:
    m = re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$", s or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    except ValueError:
        return None


def _in_range(v: int | None, lo: int, hi: int) -> int | None:
    return v if v is not None and lo <= v <= hi else None


def _listing_select(extra_cols: str = "") -> str:
    return LISTING_SELECT.replace("SELECT p.public_id,", f"SELECT {extra_cols}p.public_id,", 1)


LISTING_SELECT = """
SELECT p.public_id, p.owner_id::text AS owner_id, p.listed_by_role::text AS owner_role, p.status::text AS status,
       p.ai_extraction, p.quality_score, p.media_keys, p.created_at, p.updated_at,
       v.result AS verification,
       COALESCE((SELECT jsonb_agg(jsonb_build_object('price_inr', h.price_inr, 'at', h.changed_at)
                                  ORDER BY h.changed_at)
                 FROM property_price_history h WHERE h.property_id = p.id), '[]'::jsonb) AS price_history
FROM properties p
LEFT JOIN LATERAL (SELECT result FROM verifications vv WHERE vv.property_id = p.id
                   ORDER BY vv.created_at DESC LIMIT 1) v ON true
"""


def _row_to_listing(r: dict[str, Any]) -> Listing:
    status = r["status"] if r["status"] in ("draft", "pending_review", "live", "sold", "archived") else "archived"
    return Listing(
        id=r["public_id"], owner_id=r["owner_id"], owner_role=r["owner_role"], status=status,
        data=ListingExtraction.model_validate(r["ai_extraction"]),
        media=list(r["media_keys"] or []),
        verification=VerificationResult.model_validate(r["verification"]) if r["verification"] else None,
        quality_score=float(r["quality_score"]),
        price_history=[PricePoint(price_inr=float(p["price_inr"]), at=p["at"]) for p in r["price_history"]],
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


class PostgresDatabase(Database):
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10, embedding_model: str = "hashing"):
        self.pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False,
                                        kwargs={"row_factory": dict_row, "autocommit": True})
        self.embedding_model = embedding_model

    async def open(self) -> None:
        await self.pool.open(wait=True)

    async def close(self) -> None:
        await self.pool.close()

    # ---- listings ------------------------------------------------------- #
    async def upsert_listing(self, listing, embedding, search_tokens, tags):
        d = listing.data
        price = d.price_inr if d.price_inr and d.price_inr > 0 else None
        pincode = d.address.pincode if d.address.pincode and re.fullmatch(r"\d{6}", d.address.pincode) else None
        params = {
            "public_id": listing.id, "owner_id": listing.owner_id, "role": listing.owner_role,
            "status": listing.status, "txn": d.transaction_type, "ptype": d.property_type,
            "bhk": _in_range(d.bhk, 0, 20), "bathrooms": d.bathrooms, "balconies": d.balconies,
            "carpet": d.carpet_area_sqft, "built": d.built_up_area_sqft, "super": d.super_built_up_area_sqft,
            "floor": d.floor_number, "total_floors": d.total_floors, "facing": d.facing,
            "furnishing": d.furnishing, "price": price, "maint": d.maintenance_monthly_inr,
            "possession": d.possession_status, "possession_date": _possession_date(d.possession_date),
            "age": d.age_years, "parking": d.parking_slots,
            "amenities": [a.lower() for a in d.amenities], "rera": d.rera_number,
            "project": d.address.project_name, "locality": d.address.locality, "city": d.address.city,
            "state": d.address.state, "pincode": pincode,
            "light": _in_range(d.natural_light_score, 0, 10), "tags": tags,
            "title": d.seo_title, "description": d.seo_description, "highlights": d.highlights,
            "keywords": d.seo_keywords, "extraction": Jsonb(d.model_dump(mode="json")),
            "quality": listing.quality_score,
            "vstatus": listing.verification.status if listing.verification else "pending",
            "media": listing.media, "tokens": search_tokens,
            "published": listing.updated_at if listing.status == "live" else None,
            "created": listing.created_at, "updated": listing.updated_at,
        }
        async with self.pool.connection() as conn, conn.transaction():
            row = await (await conn.execute("""
                INSERT INTO properties (public_id, owner_id, listed_by_role, status, transaction_type, property_type,
                    bhk, bathrooms, balconies, carpet_area_sqft, built_up_area_sqft, super_built_up_area_sqft,
                    floor_number, total_floors, facing, furnishing, price_inr, maintenance_monthly_inr,
                    possession_status, possession_date, age_years, parking_slots, amenities, rera_number,
                    project_name, locality, city, state, pincode, natural_light_score, preference_tags, title,
                    description, highlights, seo_keywords, ai_extraction, quality_score, verification_status,
                    media_keys, search_tokens, published_at, created_at, updated_at)
                VALUES (%(public_id)s, %(owner_id)s, %(role)s, %(status)s, %(txn)s, %(ptype)s,
                    %(bhk)s, %(bathrooms)s, %(balconies)s, %(carpet)s, %(built)s, %(super)s,
                    %(floor)s, %(total_floors)s, %(facing)s, %(furnishing)s, %(price)s, %(maint)s,
                    %(possession)s, %(possession_date)s, %(age)s, %(parking)s, %(amenities)s, %(rera)s,
                    %(project)s, %(locality)s, %(city)s, %(state)s, %(pincode)s, %(light)s, %(tags)s, %(title)s,
                    %(description)s, %(highlights)s, %(keywords)s, %(extraction)s, %(quality)s, %(vstatus)s,
                    %(media)s, %(tokens)s, %(published)s, %(created)s, %(updated)s)
                ON CONFLICT (public_id) DO UPDATE SET
                    status = EXCLUDED.status, transaction_type = EXCLUDED.transaction_type,
                    property_type = EXCLUDED.property_type, bhk = EXCLUDED.bhk, bathrooms = EXCLUDED.bathrooms,
                    balconies = EXCLUDED.balconies, carpet_area_sqft = EXCLUDED.carpet_area_sqft,
                    built_up_area_sqft = EXCLUDED.built_up_area_sqft,
                    super_built_up_area_sqft = EXCLUDED.super_built_up_area_sqft,
                    floor_number = EXCLUDED.floor_number, total_floors = EXCLUDED.total_floors,
                    facing = EXCLUDED.facing, furnishing = EXCLUDED.furnishing, price_inr = EXCLUDED.price_inr,
                    maintenance_monthly_inr = EXCLUDED.maintenance_monthly_inr,
                    possession_status = EXCLUDED.possession_status, possession_date = EXCLUDED.possession_date,
                    age_years = EXCLUDED.age_years, parking_slots = EXCLUDED.parking_slots,
                    amenities = EXCLUDED.amenities, rera_number = EXCLUDED.rera_number,
                    project_name = EXCLUDED.project_name, locality = EXCLUDED.locality, city = EXCLUDED.city,
                    state = EXCLUDED.state, pincode = EXCLUDED.pincode,
                    natural_light_score = EXCLUDED.natural_light_score, preference_tags = EXCLUDED.preference_tags,
                    title = EXCLUDED.title, description = EXCLUDED.description, highlights = EXCLUDED.highlights,
                    seo_keywords = EXCLUDED.seo_keywords, ai_extraction = EXCLUDED.ai_extraction,
                    quality_score = EXCLUDED.quality_score, verification_status = EXCLUDED.verification_status,
                    media_keys = EXCLUDED.media_keys, search_tokens = EXCLUDED.search_tokens,
                    published_at = COALESCE(properties.published_at, EXCLUDED.published_at),
                    updated_at = EXCLUDED.updated_at
                RETURNING id""", params)).fetchone()
            pid = row["id"]
            await conn.execute("""
                INSERT INTO property_embeddings (property_id, kind, model, embedding)
                VALUES (%s, 'text', %s, %s::vector)
                ON CONFLICT (property_id, kind) DO UPDATE
                SET model = EXCLUDED.model, embedding = EXCLUDED.embedding, updated_at = now()""",
                (pid, self.embedding_model, _vec(embedding)))
            for p in listing.price_history:
                await conn.execute("""INSERT INTO property_price_history (property_id, price_inr, changed_at)
                                      VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""", (pid, p.price_inr, p.at))
            if listing.verification:
                new = listing.verification.model_dump(mode="json")
                latest = await (await conn.execute(
                    "SELECT result FROM verifications WHERE property_id = %s ORDER BY created_at DESC LIMIT 1",
                    (pid,))).fetchone()
                if not latest or latest["result"] != new:
                    v = listing.verification
                    await conn.execute("""
                        INSERT INTO verifications (property_id, status, score, checks, buyer_summary, badge, result)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (pid, v.status, v.score, Jsonb([c.model_dump() for c in v.checks]), v.buyer_summary,
                         v.badge, Jsonb(new)))

    async def get_listing(self, listing_id):
        async with self.pool.connection() as conn:
            r = await (await conn.execute(LISTING_SELECT + " WHERE p.public_id = %s", (listing_id,))).fetchone()
        return _row_to_listing(r) if r else None

    @staticmethod
    def _filter_sql(f: SearchFilters) -> tuple[str, dict[str, Any]]:
        where = ["p.status = 'live'"]
        args: dict[str, Any] = {}

        def add(clause: str, **kw: Any) -> None:
            where.append(clause)
            args.update(kw)

        if f.transaction_type:
            add("p.transaction_type = %(txn)s::transaction_type", txn=f.transaction_type)
        if f.property_types:
            add("p.property_type = ANY(%(ptypes)s::property_type[])", ptypes=f.property_types)
        if f.bhk_min is not None:
            add("p.bhk >= %(bhk_min)s", bhk_min=f.bhk_min)
        if f.bhk_max is not None:
            add("p.bhk <= %(bhk_max)s", bhk_max=f.bhk_max)
        if f.price_max_inr is not None:
            add("p.price_inr <= %(pmax)s", pmax=f.price_max_inr)
        if f.price_min_inr is not None:
            add("p.price_inr >= %(pmin)s", pmin=f.price_min_inr)
        if f.carpet_area_min_sqft is not None:
            add("COALESCE(p.carpet_area_sqft, p.built_up_area_sqft, p.super_built_up_area_sqft, 0) >= %(amin)s",
                amin=f.carpet_area_min_sqft)
        if f.cities:
            add("lower(p.city) = ANY(%(cities)s)", cities=[c.lower() for c in f.cities])
        if f.localities:
            add("lower(p.locality) = ANY(%(locs)s)", locs=[x.lower() for x in f.localities])
        if f.facing:
            add("p.facing = ANY(%(facing)s::facing_dir[])", facing=f.facing)
        if f.furnishing:
            add("p.furnishing = ANY(%(furn)s::furnishing_type[])", furn=f.furnishing)
        if f.possession_status:
            add("p.possession_status = %(poss)s::possession_type", poss=f.possession_status)
        if f.max_maintenance_monthly_inr is not None:
            add("COALESCE(p.maintenance_monthly_inr, 0) <= %(maint)s", maint=f.max_maintenance_monthly_inr)
        if f.must_have_amenities:
            add("p.amenities @> %(amen)s::text[]", amen=[a.lower() for a in f.must_have_amenities])
        if f.verified_only:
            add("p.verification_status = 'verified'")
        return " AND ".join(where), args

    async def listing_candidates(self, filters, query_vec, query_tokens, recall):
        where, args = self._filter_sql(filters)
        tsq = " | ".join(sorted({t for t in query_tokens if re.fullmatch(r"[a-z0-9]+", t)}))
        args.update(q=_vec(query_vec), recall=recall, tsq=tsq)
        async with self.pool.connection() as conn:
            total = (await (await conn.execute(
                f"SELECT count(*) AS n FROM properties p WHERE {where}", args)).fetchone())["n"]
            if not total:
                return [], 0
            # HNSW-backed nearest neighbours inside the filtered set; keyword rank on the app's tokens.
            rows = await (await conn.execute(f"""
                WITH d AS (
                    SELECT p.id, 1 - (e.embedding <=> %(q)s::vector) AS sem,
                           CASE WHEN %(tsq)s = '' THEN 0
                                ELSE ts_rank_cd(p.search_tokens_tsv, to_tsquery('simple', %(tsq)s)) END AS kw
                    FROM properties p JOIN property_embeddings e ON e.property_id = p.id AND e.kind = 'text'
                    WHERE {where}
                    ORDER BY e.embedding <=> %(q)s::vector
                    LIMIT %(recall)s)
                {_listing_select("d.sem, d.kw, ")} JOIN d ON d.id = p.id""", args)).fetchall()
        cands = [Candidate(_row_to_listing(r), max(0.0, float(r["sem"])), float(r["kw"])) for r in rows]
        cands.sort(key=lambda c: c.semantic, reverse=True)
        return cands, int(total)

    async def count_listings(self):
        async with self.pool.connection() as conn:
            return (await (await conn.execute("SELECT count(*) AS n FROM properties")).fetchone())["n"]

    # ---- users & auth --------------------------------------------------- #
    @staticmethod
    def _user(r: dict[str, Any]) -> User:
        return User(id=str(r["id"]), phone_e164=r["phone_e164"], full_name=r["full_name"],
                    roles=list(r["roles"]), whatsapp_opt_in=r["whatsapp_opt_in"], created_at=r["created_at"])

    _USER_COLS = "id, phone_e164, full_name, roles::text[] AS roles, whatsapp_opt_in, created_at"

    async def get_user(self, user_id):
        if not _uuid_or_none(user_id):
            return None
        async with self.pool.connection() as conn:
            r = await (await conn.execute(f"SELECT {self._USER_COLS} FROM users WHERE id = %s", (user_id,))).fetchone()
        return self._user(r) if r else None

    async def get_user_by_phone(self, phone_e164):
        async with self.pool.connection() as conn:
            r = await (await conn.execute(f"SELECT {self._USER_COLS} FROM users WHERE phone_e164 = %s",
                                          (phone_e164,))).fetchone()
        return self._user(r) if r else None

    async def create_user(self, phone_e164, roles, full_name=None):
        async with self.pool.connection() as conn:
            r = await (await conn.execute(
                f"INSERT INTO users (phone_e164, roles, full_name) VALUES (%s, %s::user_role[], %s) "
                f"RETURNING {self._USER_COLS}", (phone_e164, roles, full_name))).fetchone()
        return self._user(r)

    async def add_role(self, user_id, role):
        async with self.pool.connection() as conn:
            await conn.execute("""UPDATE users SET roles = array_append(roles, %s::user_role), updated_at = now()
                                  WHERE id = %s AND NOT (%s::user_role = ANY(roles))""", (role, user_id, role))
        return await self.get_user(user_id)

    async def get_otp(self, phone_e164):
        async with self.pool.connection() as conn:
            r = await (await conn.execute("SELECT * FROM auth_otps WHERE phone_e164 = %s", (phone_e164,))).fetchone()
        return OtpRecord.model_validate(r) if r else None

    async def put_otp(self, rec):
        async with self.pool.connection() as conn:
            await conn.execute("""
                INSERT INTO auth_otps (phone_e164, code_hash, expires_at, attempts, sent_count, window_start)
                VALUES (%(phone_e164)s, %(code_hash)s, %(expires_at)s, %(attempts)s, %(sent_count)s, %(window_start)s)
                ON CONFLICT (phone_e164) DO UPDATE SET code_hash = EXCLUDED.code_hash,
                    expires_at = EXCLUDED.expires_at, attempts = EXCLUDED.attempts,
                    sent_count = EXCLUDED.sent_count, window_start = EXCLUDED.window_start""", rec.model_dump())

    async def delete_otp(self, phone_e164):
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM auth_otps WHERE phone_e164 = %s", (phone_e164,))

    async def put_refresh(self, rec):
        async with self.pool.connection() as conn:
            await conn.execute("""INSERT INTO auth_refresh_tokens (token_hash, user_id, family_id, expires_at)
                                  VALUES (%s, %s, %s, %s)""",
                               (rec.token_hash, rec.user_id, rec.family_id, rec.expires_at))

    async def get_refresh(self, token_hash):
        async with self.pool.connection() as conn:
            r = await (await conn.execute(
                """SELECT token_hash, user_id::text AS user_id, family_id::text AS family_id, expires_at, revoked_at
                   FROM auth_refresh_tokens WHERE token_hash = %s""", (token_hash,))).fetchone()
        return RefreshRecord.model_validate(r) if r else None

    async def revoke_refresh(self, token_hash):
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE auth_refresh_tokens SET revoked_at = now() "
                               "WHERE token_hash = %s AND revoked_at IS NULL", (token_hash,))

    async def revoke_refresh_family(self, family_id):
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE auth_refresh_tokens SET revoked_at = now() "
                               "WHERE family_id = %s AND revoked_at IS NULL", (family_id,))

    # ---- profiles & notifications --------------------------------------- #
    _PROFILE_SELECT = """
        SELECT b.user_id::text AS user_id, b.hard_filters, b.soft_preferences, b.searches, b.viewed_listing_ids,
               b.channels::text[] AS channels, b.updated_at, u.phone_e164, u.whatsapp_opt_in
        FROM buyer_profiles b JOIN users u ON u.id = b.user_id"""

    @staticmethod
    def _profile(r: dict[str, Any]) -> BuyerProfile:
        return BuyerProfile(user_id=r["user_id"], filters=SearchFilters.model_validate(r["hard_filters"]),
                            soft_preferences=list(r["soft_preferences"]), searches=r["searches"],
                            viewed_listing_ids=list(r["viewed_listing_ids"]), channels=list(r["channels"]),
                            phone_e164=r["phone_e164"], whatsapp_opt_in=r["whatsapp_opt_in"],
                            updated_at=r["updated_at"])

    async def get_profile(self, user_id):
        if not _uuid_or_none(user_id):
            return None
        async with self.pool.connection() as conn:
            r = await (await conn.execute(self._PROFILE_SELECT + " WHERE b.user_id = %s", (user_id,))).fetchone()
        return self._profile(r) if r else None

    async def save_profile(self, profile):
        if not _uuid_or_none(profile.user_id):
            return  # anonymous visitors have no persisted profile
        async with self.pool.connection() as conn:
            await conn.execute("""
                INSERT INTO buyer_profiles (user_id, hard_filters, soft_preferences, budget_max_inr, channels,
                                            searches, viewed_listing_ids, last_active_at, updated_at)
                VALUES (%s, %s, %s, %s, %s::channel_type[], %s, %s, now(), %s)
                ON CONFLICT (user_id) DO UPDATE SET hard_filters = EXCLUDED.hard_filters,
                    soft_preferences = EXCLUDED.soft_preferences, budget_max_inr = EXCLUDED.budget_max_inr,
                    channels = EXCLUDED.channels, searches = EXCLUDED.searches,
                    viewed_listing_ids = EXCLUDED.viewed_listing_ids, last_active_at = now(),
                    updated_at = EXCLUDED.updated_at""",
                (profile.user_id, Jsonb(profile.filters.model_dump(mode="json")), profile.soft_preferences,
                 profile.filters.price_max_inr, profile.channels, profile.searches, profile.viewed_listing_ids,
                 profile.updated_at))

    async def list_profiles(self):
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(self._PROFILE_SELECT)).fetchall()
        return [self._profile(r) for r in rows]

    async def add_notification(self, n):
        if not _uuid_or_none(n.user_id):
            return False
        async with self.pool.connection() as conn:
            r = await (await conn.execute("""
                INSERT INTO notifications (user_id, kind, channel, dedup_key, payload, scheduled_for, status)
                VALUES (%s, %s, %s::channel_type, %s, %s, %s, %s)
                ON CONFLICT (dedup_key) DO NOTHING RETURNING id""",
                (n.user_id, n.kind, n.channel, n.dedup_key, Jsonb(n.payload), n.scheduled_for, n.status))).fetchone()
        return r is not None

    async def list_notifications(self, user_id):
        sql = """SELECT id::text AS id, user_id::text AS user_id, kind, channel::text AS channel, dedup_key,
                        payload, scheduled_for, status FROM notifications"""
        async with self.pool.connection() as conn:
            if user_id is None:
                rows = await (await conn.execute(sql + " ORDER BY created_at")).fetchall()
            elif not _uuid_or_none(user_id):
                rows = []
            else:
                rows = await (await conn.execute(sql + " WHERE user_id = %s ORDER BY created_at",
                                                 (user_id,))).fetchall()
        return [Notification.model_validate(r) for r in rows]

    # ---- scheduling ----------------------------------------------------- #
    async def busy_slots(self, host_id):
        if not _uuid_or_none(host_id):
            return set()
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT lower(slot) AS s FROM appointments WHERE host_id = %s AND status = 'confirmed'",
                (host_id,))).fetchall()
        return {r["s"] for r in rows}

    async def create_booking(self, booking):
        async with self.pool.connection() as conn:
            try:
                async with conn.transaction():
                    prop = await (await conn.execute("SELECT id FROM properties WHERE public_id = %s",
                                                     (booking.listing_id,))).fetchone()
                    lead = await (await conn.execute("""
                        INSERT INTO leads (property_id, buyer_id, conversation_id, assigned_to, stage, score, source)
                        VALUES (%s, %s, (SELECT id FROM ai_conversations WHERE id::text = %s), %s,
                                'visit_scheduled', %s, 'concierge')
                        RETURNING id""",
                        (prop["id"], _uuid_or_none(booking.buyer_id), booking.session_id, booking.host_id,
                         booking.lead_score))).fetchone()
                    await conn.execute("""
                        INSERT INTO appointments (lead_id, property_id, host_id, slot, status, public_id)
                        VALUES (%s, %s, %s, tstzrange(%s, %s), 'confirmed', %s)""",
                        (lead["id"], prop["id"], booking.host_id, booking.slot.start, booking.slot.end,
                         booking.booking_id))
            except psycopg.errors.ExclusionViolation as e:
                raise SlotTaken(booking.slot.slot_id) from e
        return booking

    # ---- sessions ------------------------------------------------------- #
    async def _with_history(self, conn, r: dict[str, Any]) -> ConciergeSession:
        s = ConciergeSession.model_validate(r["session_state"])
        msgs = await (await conn.execute(
            """SELECT role, content FROM (SELECT id, role, content FROM ai_messages WHERE conversation_id = %s
               ORDER BY id DESC LIMIT 10) m ORDER BY id""", (r["id"],))).fetchall()
        s.history = [{"role": m["role"], "content": m["content"]} for m in msgs]
        return s

    async def get_session(self, session_id):
        if not _uuid_or_none(session_id):
            return None
        async with self.pool.connection() as conn:
            r = await (await conn.execute("SELECT id, session_state FROM ai_conversations WHERE id = %s",
                                          (session_id,))).fetchone()
            return await self._with_history(conn, r) if r else None

    async def find_session(self, channel, external_thread):
        async with self.pool.connection() as conn:
            r = await (await conn.execute(
                """SELECT id, session_state FROM ai_conversations WHERE channel = %s::channel_type
                   AND external_thread = %s ORDER BY last_message_at DESC LIMIT 1""",
                (CHANNEL_TO_DB.get(channel, "in_app"), external_thread))).fetchone()
            return await self._with_history(conn, r) if r else None

    async def save_session(self, session, new_messages):
        state = session.model_dump(mode="json", exclude={"history"})
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("""
                INSERT INTO ai_conversations (id, user_id, property_id, channel, external_thread, agent_kind,
                                              fsm_state, slots, lead_score, session_state, last_message_at)
                VALUES (%s, %s, (SELECT id FROM properties WHERE public_id = %s), %s::channel_type, %s, 'concierge',
                        %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET user_id = EXCLUDED.user_id, property_id = EXCLUDED.property_id,
                    fsm_state = EXCLUDED.fsm_state, slots = EXCLUDED.slots, lead_score = EXCLUDED.lead_score,
                    session_state = EXCLUDED.session_state, last_message_at = now()""",
                (session.id, _uuid_or_none(session.user_id), session.listing_id,
                 CHANNEL_TO_DB.get(session.channel, "in_app"), session.external_thread, session.state.value,
                 Jsonb(state["slots"]), session.lead_score, Jsonb(state)))
            for m in new_messages:
                await conn.execute("""INSERT INTO ai_messages (conversation_id, role, content, fsm_state_after)
                                      VALUES (%s, %s, %s, %s)""",
                                   (session.id, m["role"], m["content"], session.state.value))

    # ---- events --------------------------------------------------------- #
    async def append_event(self, event):
        async with self.pool.connection() as conn:
            await conn.execute("""INSERT INTO outbox_events (aggregate_type, aggregate_id, type, payload)
                                  VALUES (%s, %s, %s, %s)""",
                               (event.type.split(".")[0], event.partitionkey or event.subject or event.id,
                                event.type, Jsonb(json.loads(event.model_dump_json()))))

    async def recent_events(self, limit):
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT payload FROM outbox_events ORDER BY created_at DESC, id LIMIT %s", (limit,))).fetchall()
        return [Event.model_validate(r["payload"]) for r in reversed(rows)]
