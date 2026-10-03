"""Apply ``sql/NNN_*.sql`` files in order, once each.

    python -m app.db.migrate            # uses DATABASE_URL
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)
SQL_DIR = Path(os.environ.get("APT_SQL_DIR") or Path(__file__).resolve().parents[2] / "sql")
LOCK_ID = 74_210_001  # pg_advisory_lock key so concurrent replicas don't race


async def migrate(dsn: str, sql_dir: Path = SQL_DIR) -> list[str]:
    applied: list[str] = []
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            await conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                                      version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
            done = {r[0] for r in await (await conn.execute("SELECT version FROM schema_migrations")).fetchall()}
            for f in sorted(sql_dir.glob("[0-9][0-9][0-9]_*.sql")):
                if f.stem in done:
                    continue
                async with conn.transaction():
                    await conn.execute(f.read_text())
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.stem,))
                log.info("applied migration %s", f.stem)
                applied.append(f.stem)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    return applied


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(asyncio.run(migrate(os.environ["DATABASE_URL"])) or "up to date")
