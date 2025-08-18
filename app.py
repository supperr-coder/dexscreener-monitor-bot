import os
import logging
import time
from datetime import datetime

import requests
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ----------------- Config & Logging -----------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg-monitor")

TELE_TOKEN = os.environ.get("TELE_TOKEN")  # <- set in Render
if not TELE_TOKEN:
    raise RuntimeError("Missing TELE_TOKEN env var")

# Will use PUBLIC_URL if you set it; otherwise falls back to Render's auto var
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")

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

# ----------------- Bot handlers & jobs -----------------
async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs every N seconds per /monitor request.
    Stores state (prev price) in context.job.data.
    """
    data = context.job.data
    chat_id = data["chat_id"]
    token_address = data["token_address"]
    chain_id = data.get("chain_id", "solana")
    threshold = float(data.get("pct_chng_threshold", 3))
    prev_price = data.get("prev_price")

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
        ts = datetime.fromtimestamp(int(time.time()))

        if prev_price is None:
            await context.bot.send_message(chat_id, text=f"📊 {symbol}/USD ({ts}): ${price_usd_new}")
        else:
            pct = round((price_usd_new - prev_price) / prev_price * 100, 2)
            if abs(pct) >= threshold:
                await context.bot.send_message(
                    chat_id,
                    text=f"⚡ {symbol}/USD: {pct}% | ({ts}): ${price_usd_new}"
                )

        data["prev_price"] = price_usd_new

    except Exception as e:
        log.exception("monitor_job error")
        await context.bot.send_message(chat_id, text=f"❌ Monitoring stopped for {token_address} ({e})")
        context.job.schedule_removal()

async def start(update, context):
    await update.message.reply_text(
        "Send /monitor <tokenAddress> [threshold(%)] [chain]\n"
        "Example: /monitor 7S2... 5 solana\n"
        "Use /stop <tokenAddress> to stop."
    )

async def monitor(update, context):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /monitor <tokenAddress> [threshold(%)] [chain]")
        return

    token_address = context.args[0]
    try:
        threshold = float(context.args[1]) if len(context.args) >= 2 else 3.0
    except ValueError:
        threshold = 3.0
    chain_id = context.args[2] if len(context.args) >= 3 else "solana"

    job_name = f"monitor-{chat_id}-{token_address}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    context.job_queue.run_repeating(
        monitor_job,
        interval=8.0,
        first=0.0,
        name=job_name,
        data={
            "chat_id": chat_id,
            "token_address": token_address,
            "chain_id": chain_id,
            "pct_chng_threshold": threshold,
            "prev_price": None,
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
    job_name = f"monitor-{chat_id}-{token_address}"
    jobs = context.job_queue.get_jobs_by_name(job_name)
    if not jobs:
        await update.message.reply_text(f"ℹ️ No active monitor for {token_address}.")
        return
    for j in jobs:
        j.schedule_removal()
    await update.message.reply_text(f"🛑 Stopped monitoring {token_address}.")

# ----------------- FastAPI + PTB Application -----------------
app = FastAPI()
tg = ApplicationBuilder().token(TELE_TOKEN).build()

# register handlers
tg.add_handler(CommandHandler("start", start))
tg.add_handler(CommandHandler("monitor", monitor))
tg.add_handler(CommandHandler("stop", stop))

@app.on_event("startup")
async def on_startup():
    await tg.initialize()
    await tg.start()
    # Set webhook if we have a public URL (Render sets RENDER_EXTERNAL_URL automatically)
    if PUBLIC_URL:
        url = f"{PUBLIC_URL}/webhook"
        await tg.bot.set_webhook(url)
        log.info("Webhook set to %s", url)
    else:
        log.warning("PUBLIC_URL/RENDER_EXTERNAL_URL not set; webhook not configured.")

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
    # quick signal you can ping from uptime monitors
    return "healthy"

@app.post("/webhook")
async def webhook(request: Request):
    # Telegram will POST updates here
    data = await request.json()
    update = Update.de_json(data, tg.bot)
    await tg.process_update(update)
    return {"ok": True}

# ------------- Local dev helper (optional) -------------
if __name__ == "__main__":
    # Local dev via polling (no webhook)
    import asyncio
    async def _main():
        await tg.initialize()
        await tg.start()
        await tg.bot.delete_webhook(drop_pending_updates=True)
        await tg.run_polling()
    asyncio.run(_main())
