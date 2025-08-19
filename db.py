# db.py
import os
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)

async def run_sql_file(path: str):
    async with engine.begin() as conn:
        with open(path, "r", encoding="utf-8") as f:
            sql = f.read()
        # Split on semicolons and execute statements individually
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        for stmt in statements:
            # use driver-level execution (avoids preparing a multi-statement query)
            await conn.exec_driver_sql(stmt)

async def init_db():
    await run_sql_file("001_init.sql")
