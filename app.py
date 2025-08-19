import os
import io
import logging
import time
from datetime import datetime, timedelta

import requests
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Headless plotting
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ----------------- Logging -----------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg-monitor")

# ----------------- Config -----------------
TELE_TOKEN = os.environ.get("TELE_TOKEN")
if not TELE_TOKEN:
    raise RuntimeError("Missing TELE_TOKEN env var")

PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
BUCKET_SECS = int(os.environ.get("PRICE_BUCKET_SECONDS", "5"))  # quantize ts so duplicates collapse

# ----------------- DB imports -----------------
from db import (
    init_db,
    Session,
    ensure_user_chat,
    ensure_token,
    upsert_monitor,
    deactivate_monitor,
    active_monitors,
    store_price,
    update_monitor_prev,
    get_token_symbol,
    get_prices_since,
    delete_prices_older_than,
)

# ----------------- Data fetcher -----------------
class MonitorToken:
    BASE_URL = "https://api.dexscreener.com"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "dex-monitor/1.0"})

    def get_token_pairs(self, chain_id: str, token_address: str):
        url = f"{self.BASE_URL}/tokens/v1/{chain_id}/{token_address}"
        r = self.session.get(url, timeout=10)
        r.raise_for_status()
        res = r.json()
        if isinstance(res, dict) and "pairs" in res:
            return res["pairs"]
        return res  # assume list

monitor_api = MonitorToken()

# ----------------- Jobs -----------------
async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """Repeats every N seconds per /monitor request."""
    data = context.job.data
    chat_id = data["chat_id"]
    token_address = data["token_address"]
    chain_id = data.get("chain_id", "solana")
    threshold = float(data.get("pct_chng_threshold", 3))
    prev_price = data.get("prev_price")
    monitor_id = data.get("monitor_id")

    try:
        pairs = monitor_api.get_token_pairs(chain_id, token_address)
        if not pairs:
            await context.bot.send_message(chat_id, text=f"❌ No pairs found for {token_address}. Stopping.")
            context.job.schedule_removal()
            return

        token = pairs[0]
        symbol = token.get("baseToken", {}).get("symbol", "?")
        price_usd_str = token.get("priceUsd")
        if price_usd_str is None:
            await context.bot.send_message(chat_id, text=f"⚠️ No priceUsd for {symbol}. Stopping.")
            context.job.schedule_removal()
            return

        price_usd_new = float(price_usd_str)
        now_s = int(time.time())
        # bucket to 8-second steps so multiple monitors share same ts
        ts_bucket = datetime.utcfromtimestamp((now_s // BUCKET_SECS) * BUCKET_SECS)

        if prev_price is None:
            await context.bot.send_message(chat_id, text=f"📊 {symbol}/USD ({ts_bucket} UTC): ${price_usd_new}")
        else:
            pct = round((price_usd_new - prev_price) / (prev_price or 1e-9) * 100, 2)
            if abs(pct) >= threshold:
                await context.bot.send_message(
                    chat_id,
                    text=f"⚡ {symbol}/USD: {pct}% | ({ts_bucket} UTC): ${price_usd_new}"
                )

        # Persist new state
        data["prev_price"] = price_usd_new

        # DB writes
        async with Session() as s:
            async with s.begin():
                await store_price(s, chain_id, token_address, ts_bucket, price_usd_new)
                if monitor_id is not None:
                    await update_monitor_prev(s, monitor_id, price_usd_new, ts_bucket)
                if symbol and symbol != "?":
                    await ensure_token(s, chain_id, token_address, symbol)

    except Exception as e:
        log.exception("monitor_job error")
        await context.bot.send_message(chat_id, text=f"❌ Monitoring stopped for {token_address} ({e})")
        context.job.schedule_removal()

async def retention_job(context: ContextTypes.DEFAULT_TYPE):
    """Deletes price rows older than 24h (runs hourly)."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    try:
        async with Session() as s:
            async with s.begin():
                deleted = await delete_prices_older_than(s, cutoff)
        if deleted:
            log.info("Retention: deleted %d rows older than %s", deleted, cutoff.isoformat())
    except Exception:
        log.exception("Retention job failed")

# ----------------- Handlers -----------------
async def start(update, context):
    await update.message.reply_text(
        "Send /monitor <tokenAddress> [threshold(%)] [chain]\n"
        "Example: /monitor 7S2... 5 solana\n"
        "Use /stop <tokenAddress> to stop.\n"
        "Use /chart <tokenAddress> [hours] [chain] to view a line chart."
    )

async def monitor(update, context):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /monitor <tokenAddress> [threshold(%)] [chain]")
        return

    token_address = context.args[0]
    try:
        threshold = float(context.args[1]) if len(context.args) >= 2 else 3.0
    except ValueError:
        threshold = 3.0
    chain_id = context.args[2] if len(context.args) >= 3 else "solana"

    # Persist user/chat/token + create/refresh active monitor
    async with Session() as s:
        async with s.begin():
            await ensure_user_chat(
                s,
                user_id=user.id,
                username=user.username,
                chat_id=chat_id,
                chat_type=update.effective_chat.type,
                title=getattr(update.effective_chat, "title", None),
            )
            await ensure_token(s, chain_id, token_address, symbol=None)
            monitor_id = await upsert_monitor(s, chat_id, chain_id, token_address, threshold)

    # Schedule/replace job
    job_name = f"monitor-{chat_id}-{token_address}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    context.job_queue.run_repeating(
        monitor_job,
        interval=BUCKET_SECS,
        first=0.0,
        name=job_name,
        data={
            "chat_id": chat_id,
            "token_address": token_address,
            "chain_id": chain_id,
            "pct_chng_threshold": threshold,
            "prev_price": None,
            "monitor_id": monitor_id,
        },
    )
    await update.message.reply_text(
        f"✅ Started monitoring {token_address} (chain={chain_id}, threshold={threshold}%)."
    )

async def stop(update, context):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /stop <tokenAddress>")
        return
    token_address = context.args[0]
    chain_id = context.args[1] if len(context.args) >= 2 else "solana"

    # Deactivate in DB
    async with Session() as s:
        async with s.begin():
            _ = await deactivate_monitor(s, chat_id, chain_id, token_address)

    # Remove any scheduled job
    job_name = f"monitor-{chat_id}-{token_address}"
    jobs = context.job_queue.get_jobs_by_name(job_name)
    if not jobs:
        await update.message.reply_text(f"ℹ️ No active monitor for {token_address}.")
        return
    for j in jobs:
        j.schedule_removal()
    await update.message.reply_text(f"🛑 Stopped monitoring {token_address}.")

def _parse_hours(arg: str) -> float:
    s = arg.strip().lower()
    if s.endswith("h"):
        return float(s[:-1] or "0")
    if s.endswith("m"):
        return float(s[:-1] or "0") / 60.0
    return float(s)  # plain number = hours

async def chart(update, context):
    """Usage: /chart <tokenAddr> [hours] [chain]"""
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /chart <tokenAddress> [hours] [chain]")
        return

    token_address = context.args[0]
    hours = 24.0
    chain_id = "solana"
    if len(context.args) >= 2:
        try:
            hours = _parse_hours(context.args[1])
            if len(context.args) >= 3:
                chain_id = context.args[2]
        except ValueError:
            # second arg is probably the chain
            chain_id = context.args[1]
            if len(context.args) >= 3:
                hours = _parse_hours(context.args[2])

    since_ts = datetime.utcnow() - timedelta(hours=hours)

    async with Session() as s:
        async with s.begin():
            pts = await get_prices_since(s, chain_id, token_address, since_ts)
            symbol = await get_token_symbol(s, chain_id, token_address)

    if not pts:
        await update.message.reply_text("No price data yet for that token in the selected window.")
        return

    times, prices = zip(*pts)
    # how much history we actually have
    span = (times[-1] - times[0]).total_seconds()
    span_h, span_m = int(span // 3600), int((span % 3600) // 60)

    # plot
    fig, ax = plt.subplots(figsize=(8, 3))
    ax.plot(times, prices, linewidth=1.25)
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel("Price (USD)")
    name = symbol or (token_address[:6] + "…" + token_address[-4:])
    ax.set_title(f"{name} on {chain_id} — past {span_h}h {span_m}m")
    ax.grid(True, linewidth=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.autofmt_xdate()

    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)

    caption = f"{name} ({chain_id}) — {len(prices)} points • past {span_h}h {span_m}m"
    await update.message.reply_photo(photo=InputFile(buf, filename="chart.png"), caption=caption)

# ----------------- FastAPI + PTB Application -----------------
app = FastAPI()
tg = ApplicationBuilder().token(TELE_TOKEN).build()

# register handlers
tg.add_handler(CommandHandler("start", start))
tg.add_handler(CommandHandler("monitor", monitor))
tg.add_handler(CommandHandler("stop", stop))
tg.add_handler(CommandHandler("chart", chart))

# error handler
async def error_handler(update, context):
    import traceback
    log.error("Exception while handling update: %s", traceback.format_exc())
tg.add_error_handler(error_handler)

@app.on_event("startup")
async def on_startup():
    await init_db()
    await tg.initialize()
    await tg.start()
    if PUBLIC_URL:
        url = f"{PUBLIC_URL}/webhook"
        await tg.bot.set_webhook(url)
        log.info("Webhook set to %s", url)
    else:
        log.warning("PUBLIC_URL/RENDER_EXTERNAL_URL not set; webhook not configured.")

    # Reschedule active monitors from DB
    async with Session() as s:
        mons = await active_monitors(s)
        for m in mons:
            job_name = f"monitor-{m['chat_id']}-{m['token_address']}"
            for j in tg.job_queue.get_jobs_by_name(job_name):
                j.schedule_removal()
            tg.job_queue.run_repeating(
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

    # Start hourly retention job
    tg.job_queue.run_repeating(retention_job, interval=3600.0, first=60.0, name="retention-24h")

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await tg.bot.delete_webhook()
    except Exception:
        pass
    await tg.stop()
    await tg.shutdown()

@app.get("/", response_class=PlainTextResponse)
async def root():
    return "ok"

@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "healthy"

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, tg.bot)
    await tg.process_update(update)
    return {"ok": True}

# Local dev (polling)
if __name__ == "__main__":
    import asyncio
    async def _main():
        await tg.initialize()
        await tg.start()
        await tg.bot.delete_webhook(drop_pending_updates=True)
        await tg.run_polling()
    asyncio.run(_main())
