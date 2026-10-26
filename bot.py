#!/usr/bin/env python3
# ──────────────────────────────────────────────────────────────
#  🚀 IVAS MS Realtime OTP Forwarder — Render Production Edition
#  Endpoint: wss://www.ivasms.com/live/socket.io/?...
# ──────────────────────────────────────────────────────────────

import asyncio
import json
import logging
import os
import re
import ssl
import sys
import time
import traceback
from datetime import datetime
from typing import Dict, List, Optional, Set

import phonenumbers
import websockets
from aiohttp import web
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError, RetryAfter

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8350157654:AAERJoVCuICl0hasoIIb1X7SLgH1kp9MuZ8")

# FIXED LINE 31 — closing quote added
_otp_groups_raw = os.environ.get("OTP_GROUP_IDS", "-1003870313704")
OTP_GROUP_IDS: List[int] = [int(x.strip()) for x in _otp_groups_raw.split(",") if x.strip()]

# IVAS_ACCOUNTS via env (JSON). Default = your live endpoint.
_DEFAULT_IVAS = {
    "Bronxop": {
        "uri": "wss://www.ivasms.com/live/socket.io/?UserName=Bronxop&Email=beeopopo%40gmail.com&EIO=4&transport=websocket"
    }
}
try:
    IVAS_ACCOUNTS: Dict[str, Dict] = json.loads(os.environ.get("IVAS_ACCOUNTS", "null")) or _DEFAULT_IVAS
except Exception:
    IVAS_ACCOUNTS = _DEFAULT_IVAS

PORT = int(os.environ.get("PORT", 8080))

# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logger = logging.getLogger("ivas")

# ══════════════════════════════════════════════════════════════
#  STATE / METRICS
# ══════════════════════════════════════════════════════════════
START_TIME = time.time()
STATS = {
    "sms_received": 0,
    "otp_found": 0,
    "sent_ok": 0,
    "sent_fail": 0,
    "reconnects": 0,
}

# ══════════════════════════════════════════════════════════════
#  TELEGRAM BOT
# ══════════════════════════════════════════════════════════════
_bot: Optional[Bot] = None

def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=BOT_TOKEN)
    return _bot


def get_otp_keyboard() -> InlineKeyboardMarkup:
    btns = [
        InlineKeyboardButton("Numbers", url="https://t.me/dtzotopanel"),
        InlineKeyboardButton("Owner",   url="https://t.me/DarkTechZone0"),
        InlineKeyboardButton("WhatsApp", url="https://whatsapp.com/channel/0029VbCgB63LCoX5aiV5qp1t"),
        InlineKeyboardButton("Channel", url="https://t.me/DarkTechZone2"),
    ]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    return InlineKeyboardMarkup(rows)


async def send_otp_to_groups(msg: str):
    bot = get_bot()
    for gid in OTP_GROUP_IDS:
        for _ in range(3):
            try:
                await bot.send_message(
                    chat_id=gid,
                    text=msg,
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_otp_keyboard(),
                    disable_web_page_preview=True,
                )
                STATS["sent_ok"] += 1
                logger.info(f"✅ OTP → {gid}")
                break
            except RetryAfter as e:
                wait = e.retry_after + 1
                logger.warning(f"⏳ FloodWait {wait}s on {gid}")
                await asyncio.sleep(wait)
            except TelegramError as e:
                STATS["sent_fail"] += 1
                logger.error(f"❌ send fail {gid}: {e}")
                break
            except Exception as e:
                STATS["sent_fail"] += 1
                logger.error(f"❌ send err {gid}: {e}")
                break


# ══════════════════════════════════════════════════════════════
#  PHONE / OTP HELPERS
# ══════════════════════════════════════════════════════════════
def get_flag_emoji(region: str) -> str:
    if not region or len(region) != 2:
        return "🌍"
    return chr(ord(region[0]) + 127397) + chr(ord(region[1]) + 127397)


def mask_phone(number_str: str) -> str:
    digits = re.sub(r"\D", "", number_str or "")
    if not digits:
        return number_str or "unknown"
    try:
        p = phonenumbers.parse(number_str, None)
        cc, national = str(p.country_code), str(p.national_number)
    except Exception:
        cc = digits[:3] if digits.startswith("1") else digits[:2]
        national = digits[len(cc):]
    if len(national) < 6:
        masked = national[:2] + "****" + national[-2:] if len(national) > 4 else national + "****"
    else:
        masked = national[:2] + "****" + national[-4:]
    return f"+{cc}{masked}"


def country_from_number(number: str) -> str:
    try:
        p = phonenumbers.parse(number if number.startswith("+") else "+" + number, None)
        return phonenumbers.region_code_for_number(p) or "XX"
    except Exception:
        return "XX"


def country_name(region: str) -> str:
    try:
        if region and len(region) == 2:
            cc = phonenumbers.country_code_for_region(region)
            dummy = phonenumbers.parse(f"+{cc}123456789")
            return phonenumbers.geocoder.description_for_number(dummy, "en") or region
    except Exception:
        pass
    return region or "Unknown"


def extract_otp(message: str) -> Optional[str]:
    if not message:
        return None
    m = re.search(r"(?<!\d)(\d{3,4})[\- ](\d{3,4})(?!\d)", message)
    if m:
        comb = m.group(1) + m.group(2)
        if 6 <= len(comb) <= 8:
            return comb
    for pat in (r"(?<!\d)\d{6}(?!\d)", r"(?<!\d)\d{5}(?!\d)", r"(?<!\d)\d{4}(?!\d)"):
        m = re.search(pat, message)
        if m:
            return m.group(0)
    return None


def format_otp_rich(number: str, service: str, otp: Optional[str],
                    sms_text: str, ts: Optional[datetime] = None) -> str:
    ts = ts or datetime.now()
    region = country_from_number(number)
    flag = get_flag_emoji(region)
    cname = country_name(region)
    masked = mask_phone(number)
    svc = (service or "Unknown").capitalize()
    otp_disp = otp or "N/A"
    if otp_disp.isdigit() and len(otp_disp) == 6:
        otp_disp = f"{otp_disp[:3]}-{otp_disp[3:]}"
    return (
        f"🔔 <b>New {cname} {svc} OTP!</b>\n\n"
        f"🕰 <b>Time:</b> <code>{ts.strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
        f"{flag} <b>Country:</b> {cname}\n"
        f"🟢 <b>Service:</b> {svc}\n"
        f"📞 <b>Number:</b> <code>{masked}</code>\n"
        f"🔑 <b>OTP:</b> <code>{otp_disp}</code>\n\n"
        f"📩 <b>Full Message:</b>\n<pre>{(sms_text or '')[:500]}</pre>\n\n"
        f"<i>Powered By Dark Tech Zone</i>"
    )


# ══════════════════════════════════════════════════════════════
#  IVAS MS SOCKET.IO v4 CLIENT
# ══════════════════════════════════════════════════════════════
class IvasClient:
    def __init__(self, name: str, uri: str):
        self.name = name
        self.uri = uri
        self.seen: Set[str] = set()
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.ping_interval = 25.0
        self.stop = False
        self.connected = False

    def _is_dup(self, key: str) -> bool:
        if key in self.seen:
            return True
        self.seen.add(key)
        if len(self.seen) > 2000:
            self.seen = set(list(self.seen)[-1000:])
        return False

    async def _ping_loop(self):
        try:
            while not self.stop and self.ws and not self.ws.closed:
                await asyncio.sleep(self.ping_interval)
                try:
                    await self.ws.send("2")
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def _handle_event(self, payload):
        if not isinstance(payload, list) or len(payload) < 2:
            return
        data = payload[1]
        if not isinstance(data, dict):
            return

        number = str(
            data.get("recipient")
            or data.get("num")
            or data.get("number")
            or data.get("phone")
            or ""
        ).strip()
        text = data.get("message") or data.get("sms") or data.get("text") or data.get("body") or ""
        service = (
            data.get("originator")
            or data.get("cli")
            or data.get("sender")
            or data.get("service")
            or "Unknown"
        )

        if not number or not text:
            return

        STATS["sms_received"] += 1
        uniq = f"{number}|{text[:40]}"
        if self._is_dup(uniq):
            return

        otp = extract_otp(text)
        if otp:
            STATS["otp_found"] += 1

        logger.info(f"📨 [{self.name}] {number} | {service} | OTP={otp}")
        rich = format_otp_rich(number, service, otp, text)
        await send_otp_to_groups(rich)

    async def run(self):
        ssl_ctx = ssl._create_unverified_context()
        backoff = 3
        while not self.stop:
            try:
                logger.info(f"🔌 [{self.name}] connecting...")
                headers = {
                    "User-Agent": "Mozilla/5.0",
                    "Origin": "https://www.ivasms.com",
                }
                async with websockets.connect(
                    self.uri,
                    ssl=ssl_ctx,
                    ping_interval=None,
                    ping_timeout=None,
                    max_size=2**23,
                    open_timeout=20,
                    close_timeout=10,
                    extra_headers=headers,
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    logger.info(f"✅ [{self.name}] connected")

                    first = await asyncio.wait_for(ws.recv(), timeout=15)
                    logger.info(f"📥 [{self.name}] open: {first[:100]}")
                    if first.startswith("0"):
                        try:
                            info = json.loads(first[1:])
                            self.ping_interval = info.get("pingInterval", 25000) / 1000.0
                        except Exception:
                            pass

                    await ws.send("40")
                    await asyncio.sleep(0.3)
                    for join_pkt in ('40/livesms,', '42/livesms,["livesms"]'):
                        try:
                            await ws.send(join_pkt)
                        except Exception:
                            pass

                    ping_task = asyncio.create_task(self._ping_loop())
                    try:
                        while not self.stop:
                            msg = await ws.recv()
                            await self._dispatch(msg)
                    except websockets.ConnectionClosed:
                        logger.warning(f"⚠️ [{self.name}] closed")
                    finally:
                        ping_task.cancel()
                        self.ws = None
                        self.connected = False

                backoff = 3
            except asyncio.CancelledError:
                raise
            except Exception as e:
                STATS["reconnects"] += 1
                logger.error(f"❌ [{self.name}] {e} → retry in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _dispatch(self, msg: str):
        if not msg:
            return
        if msg == "2":
            try:
                await self.ws.send("3")
            except Exception:
                pass
            return
        if msg == "3":
            return
        if msg.startswith("2"):
            return
        if msg.startswith("42"):
            body = msg[2:]
            if body.startswith("/"):
                c = body.find(",")
                if c != -1:
                    body = body[c + 1:]
            try:
                payload = json.loads(body)
            except Exception as e:
                logger.debug(f"[{self.name}] bad json: {e} | {body[:120]}")
                return
            await self._handle_event(payload)
            return
        if msg.startswith("40"):
            logger.info(f"🟢 [{self.name}] socket.io connected")
            return
        if msg.startswith("41") or msg.startswith("44"):
            logger.warning(f"⚠️ [{self.name}] socket.io disconnect/err: {msg}")


# ══════════════════════════════════════════════════════════════
#  HTTP HEALTH SERVER
# ══════════════════════════════════════════════════════════════
async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "stats": STATS,
        "groups": len(OTP_GROUP_IDS),
        "accounts": len(IVAS_ACCOUNTS),
    })


async def root_handler(request: web.Request) -> web.Response:
    return web.Response(text="IVAS OTP Forwarder is running 🚀", content_type="text/plain")


async def start_http_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 HTTP server on :{PORT}")
    return runner


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
async def main():
    logger.info("═" * 60)
    logger.info("🚀 IVAS MS REALTIME OTP FORWARDER — Render Edition")
    logger.info(f"   Groups: {len(OTP_GROUP_IDS)} | Accounts: {len(IVAS_ACCOUNTS)}")
    logger.info("═" * 60)

    if not IVAS_ACCOUNTS:
        logger.error("❌ No IVAS_ACCOUNTS configured")
        sys.exit(1)

    runner = await start_http_server()
    clients = [IvasClient(name, cfg["uri"]) for name, cfg in IVAS_ACCOUNTS.items()]
    tasks = [asyncio.create_task(c.run()) for c in clients]

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("🛑 Shutdown")
    finally:
        for c in clients:
            c.stop = True
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
        logger.info("✅ Clean exit")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.error(traceback.format_exc())
        sys.exit(1)
