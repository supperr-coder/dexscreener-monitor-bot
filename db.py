import os
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text

DATABASE_URL = os.environ["DATABASE_URL"]  # e.g. postgresql+asyncpg://... ?sslmode=require
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)

# ----------------- schema bootstrap -----------------
async def run_sql_file(path: str):
    async with engine.begin() as conn:
        with open(path, "r", encoding="utf-8") as f:
            await conn.execute(text(f.read()))

async def init_db():
    await run_sql_file("001_init.sql")

# ----------------- convenience ops -----------------
async def ensure_user_chat(session: AsyncSession, user_id: int, username: Optional[str],
                           chat_id: int, chat_type: str, title: Optional[str]):
    await session.execute(text("""
        INSERT INTO tg_users(user_id, username)
        VALUES (:uid, :un)
        ON CONFLICT (user_id) DO UPDATE SET
            username = COALESCE(EXCLUDED.username, tg_users.username),
            last_seen_at = now()
    """), {"uid": user_id, "un": username})
    await session.execute(text("""
        INSERT INTO tg_chats(chat_id, chat_type, title)
        VALUES (:cid, :ctype, :title)
        ON CONFLICT (chat_id) DO UPDATE SET
            chat_type = EXCLUDED.chat_type,
            title = COALESCE(EXCLUDED.title, tg_chats.title),
            last_seen_at = now()
    """), {"cid": chat_id, "ctype": chat_type, "title": title})

async def ensure_token(session: AsyncSession, chain_id: str, token_address: str, symbol: Optional[str]):
    await session.execute(text("""
        INSERT INTO tokens(chain_id, token_address, symbol)
        VALUES (:c,:a,:s)
        ON CONFLICT (chain_id, token_address) DO UPDATE SET
            symbol = COALESCE(EXCLUDED.symbol, tokens.symbol)
    """), {"c": chain_id, "a": token_address, "s": symbol})

async def upsert_monitor(session: AsyncSession, chat_id: int, chain_id: str,
                         token_address: str, threshold: float) -> int:
    row = await session.execute(text("""
        INSERT INTO monitors(chat_id, chain_id, token_address, threshold_pct, is_active)
        VALUES (:chat_id, :chain, :addr, :thr, TRUE)
        ON CONFLICT (chat_id, chain_id, token_address) WHERE monitors.is_active
        DO UPDATE SET threshold_pct = EXCLUDED.threshold_pct, updated_at = now()
        RETURNING id
    """), {"chat_id": chat_id, "chain": chain_id, "addr": token_address, "thr": threshold})
    return row.scalar_one()

async def deactivate_monitor(session: AsyncSession, chat_id: int, chain_id: str, token_address: str) -> int:
    res = await session.execute(text("""
        UPDATE monitors
           SET is_active = FALSE, updated_at = now()
         WHERE chat_id = :chat AND chain_id = :chain AND token_address = :addr AND is_active
        RETURNING id
    """), {"chat": chat_id, "chain": chain_id, "addr": token_address})
    return res.scalar_one_or_none() or 0

async def store_price(session: AsyncSession, chain_id: str, token_address: str,
                      ts: datetime, price_usd: float):
    await session.execute(text("""
        INSERT INTO prices(chain_id, token_address, ts, price_usd)
        VALUES (:c,:a,:t,:p)
        ON CONFLICT (chain_id, token_address, ts) DO NOTHING
    """), {"c": chain_id, "a": token_address, "t": ts, "p": price_usd})

async def update_monitor_prev(session: AsyncSession, monitor_id: int, price: float, ts: datetime):
    await session.execute(text("""
        UPDATE monitors
           SET prev_price_usd = :p, prev_price_at = :t, updated_at = now()
         WHERE id = :id
    """), {"p": price, "t": ts, "id": monitor_id})

async def active_monitors(session: AsyncSession) -> List[Dict[str, Any]]:
    rows = await session.execute(text("""
        SELECT id, chat_id, chain_id, token_address, threshold_pct, prev_price_usd
          FROM monitors WHERE is_active = TRUE
    """))
    return [dict(r._mapping) for r in rows]

async def get_token_symbol(session: AsyncSession, chain_id: str, token_address: str) -> Optional[str]:
    row = await session.execute(text("""
        SELECT symbol FROM tokens WHERE chain_id=:c AND token_address=:a
    """), {"c": chain_id, "a": token_address})
    return row.scalar_one_or_none()

async def get_prices_since(session: AsyncSession, chain_id: str, token_address: str,
                           since_ts: datetime) -> List[Tuple[datetime, float]]:
    rows = await session.execute(text("""
        SELECT ts, price_usd
          FROM prices
         WHERE chain_id = :c AND token_address = :a AND ts >= :since
         ORDER BY ts ASC
    """), {"c": chain_id, "a": token_address, "since": since_ts})
    out: List[Tuple[datetime, float]] = []
    for r in rows:
        m = r._mapping
        out.append((m["ts"], float(m["price_usd"])))
    return out

async def delete_prices_older_than(session: AsyncSession, cutoff: datetime) -> int:
    res = await session.execute(text("DELETE FROM prices WHERE ts < :cutoff"), {"cutoff": cutoff})
    # rowcount may be None depending on dialect; coalesce to 0
    return res.rowcount or 0
