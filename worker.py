# worker.py
import asyncio
import logging

from telegram.ext import ApplicationBuilder, CommandHandler

# Reuse your existing code
from app import (
    TELE_TOKEN, BUCKET_SECS,
    start, monitor, stop, chart,
    monitor_job, retention_job,
)
from db import init_db, Session, active_monitors

log = logging.getLogger("worker")
logging.basicConfig(level=logging.INFO)

async def main():
    # 1) DB schema
    await init_db()

    # 2) Build a fresh PTB app (separate from the FastAPI one)
    app = ApplicationBuilder().token(TELE_TOKEN).build()

    # 3) Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("monitor", monitor))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("chart", chart))

    # 4) Start, ensure no webhook, then reschedule jobs from DB
    await app.initialize()
    await app.start()
    await app.bot.delete_webhook(drop_pending_updates=True)

    # Reschedule all active monitors
    async with Session() as s:
        mons = await active_monitors(s)
        for m in mons:
            job_name = f"monitor-{m['chat_id']}-{m['token_address']}"
            for j in app.job_queue.get_jobs_by_name(job_name):
                j.schedule_removal()
            app.job_queue.run_repeating(
                monitor_job,
                interval=BUCKET_SECS,
                first=0.0,
                name=job_name,
                data={
                    "chat_id": m["chat_id"],
                    "token_address": m["token_address"],
                    "chain_id": m["chain_id"],
                    "pct_chng_threshold": float(m["threshold_pct"]),
                    "prev_price": float(m["prev_price_usd"]) if m["prev_price_usd"] is not None else None,
                    "monitor_id": int(m["id"]),
                },
            )
        log.info("Rescheduled %d active monitors from DB", len(mons))

    # 5) Hourly retention task
    app.job_queue.run_repeating(retention_job, interval=3600.0, first=60.0, name="retention-24h")

    # 6) Poll forever
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
