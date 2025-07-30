#!/usr/bin/env python3
import os, sys, asyncio, logging, json, time, random, aiohttp, websockets, collections
from telethon import TelegramClient
from telethon.sessions import StringSession
from aiohttp import web
from typing import Set, Dict, Any, Optional, List

# === PARAMETERS TO EDIT ===
ULTRA_MIN_LIQ = 8
ULTRA_BUY_AMOUNT = 0.07
ULTRA_TP_X = 2.0
ULTRA_SL_X = 0.7
ULTRA_MIN_RISES = 2
ULTRA_AGE_MAX_S = 120

SCALPER_BUY_AMOUNT = 0.10
SCALPER_MIN_LIQ = 8
SCALPER_TP_X = 2.0
SCALPER_SL_X = 0.7
SCALPER_TRAIL = 0.2
SCALPER_MAX_POOLAGE = 20*60

COMMUNITY_BUY_AMOUNT = 0.04
COMM_HOLDER_THRESHOLD = 250
COMM_MAX_CONC = 0.10
COMM_TP1_MULT = 2.0
COMM_SL_PCT = 0.6
COMM_TRAIL = 0.4
COMM_HOLD_SECONDS = 2*24*60*60
COMM_MIN_SIGNALS = 2

ANTI_SNIPE_DELAY = 2
ML_MIN_SCORE = 60

# === ENV VARS ===
TELEGRAM_API_ID = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_STRING_SESSION = os.environ["TELEGRAM_STRING_SESSION"]
TOXIBOT_USERNAME = os.environ.get("TOXIBOT_USERNAME", "@toxi_solana_bot")
RUGCHECK_API = os.environ.get("RUGCHECK_API", "")
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_RPC_URL = os.environ.get("HELIUS_RPC_URL", "")
WALLET_ADDRESS = os.environ.get("WALLET_ADDRESS", "")
MORALIS_API_KEY = os.environ.get("MORALIS_API_KEY", "")
BITQUERY_API_KEY = os.environ.get("BITQUERY_API_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("toxibot")

blacklisted_tokens: Set[str] = set()
blacklisted_devs: Set[str] = set()
positions: Dict[str, Dict[str, Any]] = {}
activity_log: List[str] = []
exposure: float = 0.0
daily_loss: float = 0.0
runtime_status: str = "Starting..."
current_wallet_balance: float = 0.0

# ==== Aggregation for leaderboard, per-bot stats ====
community_signal_votes = collections.defaultdict(lambda: {"sources": set(), "first_seen": time.time()})
community_token_queue = asyncio.Queue()

def get_total_pl():
    return sum([pos.get("pl", 0) for pos in positions.values()])

# ==== UTILITIES ====
async def fetch_token_price(token: str) -> Optional[float]:
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
            resp = await s.get(url)
            data = await resp.json()
        for pair in data.get("pairs", [{}]):
            if pair.get("baseToken", {}).get("address", "") == token and "priceNative" in pair:
                return float(pair["priceNative"])
    except Exception as e:
        logger.warning(f"DEXScreener price error: {e}")
    return None

async def fetch_pool_age(token: str) -> Optional[float]:
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            resp = await s.get(url)
            data = await resp.json()
        for pair in data.get("pairs", []):
            if pair.get("baseToken", {}).get("address") == token:
                ts = pair.get("createdAtTimestamp") or pair.get("pairCreatedAt")
                if ts:
                    age_sec = time.time() - (int(ts)//1000 if len(str(ts)) > 10 else int(ts))
                    return age_sec
    except Exception:
        pass
    return None

async def fetch_volumes(token: str) -> dict:
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            resp = await s.get(url)
            data = await resp.json()
        for pair in data.get("pairs", []):
            if pair.get("baseToken", {}).get("address") == token:
                return {
                    "liq": float(pair.get("liquidity", {}).get("base", 0)),
                    "vol_1h": float(pair.get("volume", {}).get("h1", 0)),
                    "vol_6h": float(pair.get("volume", {}).get("h6", 0)),
                    "base_liq": float(pair.get("liquidity", {}).get("base", 0)),
                }
    except Exception:
        pass
    return {"liq":0,"vol_1h":0,"vol_6h":0,"base_liq":0}

def estimate_short_vs_long_volume(vol_1h, vol_6h):
    avg_15min = vol_6h / 24 if vol_6h else 0.01
    return vol_1h > 2 * avg_15min if avg_15min else False

async def fetch_holders_and_conc(token: str) -> dict:
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
            resp = await s.get(url)
            data = await resp.json()
        for pair in data.get("pairs", []):
            if pair.get("baseToken", {}).get("address") == token:
                holders = int(pair.get("holders", 0) or 0)
                maxconc = float(pair.get("holderConcentration", 0.0) or 0)
                return {"holders": holders, "max_holder_pct": maxconc}
    except Exception:
        pass
    return {"holders": 0, "max_holder_pct": 99.}

async def fetch_liquidity_and_buyers(token: str) -> dict:
    result = {"liq": 0.0, "buyers": 0, "holders": 0}
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
        async with aiohttp.ClientSession() as s:
            resp = await s.get(url)
            data = await resp.json()
        for pair in data.get("pairs", []):
            if pair.get("baseToken", {}).get("address", "") == token:
                result["liq"] = float(pair.get("liquidity", {}).get("base", 0.0))
                result["buyers"] = int(pair.get("buyTxns", 0) or 0)
    except Exception as e:
        logger.warning(f"[UltraEarly] Error fetching liq/buyers/holders: {e}")
    return result

async def fetch_wallet_balance():
    if not WALLET_ADDRESS or not HELIUS_API_KEY:
        return 0.0
    try:
        url = f"https://rpc.helius.xyz/?api-key={HELIUS_API_KEY}"
        req = [{
            "jsonrpc":"2.0","id":1,"method":"getBalance","params":[WALLET_ADDRESS]
        }]
        async with aiohttp.ClientSession() as s:
            resp = await s.post(url, data=json.dumps(req))
            res = await resp.json()
        lamports = res[0].get("result",{}).get("value",0)
        return lamports/1e9
    except Exception as e:
        logger.warning(f"Helius Wallet getBalance error: {e}")
        return 0.0

# ==== FEEDS ====
async def pumpfun_newtoken_feed(callback):
    uri = "wss://pumpportal.fun/api/data"
    try:
        async with websockets.connect(uri) as ws:
            payload = {"method": "subscribeNewToken"}
            await ws.send(json.dumps(payload))
            while True:
                try:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    token = data.get("params", {}).get("mintAddress") or data.get("params", {}).get("coinAddress")
                    if token:
                        await callback(token, "pumpfun")
                except Exception as e:
                    logger.warning(f"Pump.fun WS err: {e}, reconnecting in 2s")
                    await asyncio.sleep(2)
                    break
    except Exception as e:
        logger.error(f"Pump.fun websocket top error: {e}")

async def moralis_trending_feed(callback):
    api_key = MORALIS_API_KEY
    if not api_key:
        logger.warning("Moralis (trending) feed not enabled (no API key).")
        return
    url = "https://solana-gateway.moralis.io/account/mainnet/trending"
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.get(url, headers={"X-API-Key": api_key})
                trend = await r.json()
            for item in trend.get("result", []):
                if "mint" in item:
                    await callback(item["mint"], "moralis")
        except Exception as e:
            logger.error(f"Moralis feed error: {e}")
        await asyncio.sleep(120)

async def bitquery_trending_feed(callback):
    api_key = BITQUERY_API_KEY
    if not api_key:
        logger.warning("Bitquery trending feed not enabled (no OAuth token).")
        return
    url = "https://streaming.bitquery.io/graphql"
    q = {"query": "... {Solana{DEXTrades(limit:10){baseCurrency{address}}}}"}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    while True:
        try:
            logger.info(f"Using Bitquery key: {api_key[:8]}... (len={len(api_key)})")
            logger.info(f"Headers: {headers}")
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=q, headers=headers) as r:
                    logger.info(f"Bitquery HTTP status: {r.status}")
                    if r.status != 200:
                        text = await r.text()
                        logger.error(f"Bitquery HTTP error: {r.status}, text: {text}")
                        await asyncio.sleep(180)
                        continue
                    try:
                        data = await r.json()
                    except Exception:
                        text = await r.text()
                        logger.error(f"Bitquery non-JSON response: {text}")
                        await asyncio.sleep(180)
                        continue
            logger.info(f"Bitquery received JSON: {data}")
            if not data or "data" not in data or "Solana" not in data["data"]:
                logger.error(f"Bitquery response missing 'data'/'Solana': {data}")
                await asyncio.sleep(180)
                continue
            for trade in data["data"]["Solana"].get("DEXTrades", []):
                addr = trade.get("baseCurrency", {}).get("address", "")
                if addr:
                    await callback(addr, "bitquery")
        except Exception as e:
            logger.error(f"Bitquery feed error: {e}")
        await asyncio.sleep(180)

# COMMUNITY PERSONALITY VOTE AGGREGATOR
async def community_candidate_callback(token, src):
    now = time.time()
    if src and token:
        rec = community_signal_votes[token]
        rec["sources"].add(src)
        if "first_seen" not in rec:
            rec["first_seen"] = now
        voted = len(rec["sources"])
        logger.info(f"[CommunityBot] {token} in {rec['sources']} ({voted}/{COMM_MIN_SIGNALS})")
        if voted >= COMM_MIN_SIGNALS:
            await community_token_queue.put(token)

# ==== TOXIBOT/TELEGRAM ====
class ToxiBotClient:
    def __init__(self, api_id, api_hash, session_id, username):
        self._client = TelegramClient(StringSession(session_id), api_id, api_hash, connection_retries=5)
        self.bot_username = username

    async def connect(self):
        await self._client.start()
        logger.info("Connected to ToxiBot (Telegram).")

    async def send_buy(self, mint: str, amount: float, price_limit=None):
        cmd = f"/buy {mint} {amount}".strip()
        if price_limit:
            cmd += f" limit {price_limit:.7f}"
        logger.info(f"Sending to ToxiBot: {cmd}")
        return await self._client.send_message(self.bot_username, cmd)

    async def send_sell(self, mint: str, perc: int = 100):
        cmd = f"/sell {mint} {perc}%"
        logger.info(f"Sending to ToxiBot: {cmd}")
        return await self._client.send_message(self.bot_username, cmd)

# ==== RUGCHECK & ML ====
async def rugcheck(token_addr: str) -> Dict[str, Any]:
    url = f"https://rugcheck.xyz/api/check/{token_addr}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as session:
            async with session.get(url) as r:
                if r.headers.get('content-type','').startswith('application/json'):
                    data = await r.json()
                else:
                    logger.warning(f"Rugcheck returned HTML for {token_addr}")
                    data = {}
        logger.info(f"Rugcheck {token_addr}: {data}")
        return data
    except Exception as e:
        logger.error(f"Rugcheck error for {token_addr}: {e}")
        return {}

def rug_gate(rug: Dict[str, Any]) -> Optional[str]:
    if rug.get("label") != "Good":
        return "rugcheck not Good"
    if "bundled" in rug.get("supply_type", "").lower():
        if rug.get("mint"):
            blacklisted_tokens.add(rug["mint"])
        if rug.get("authority"):
            blacklisted_devs.add(rug["authority"])
        return "supply bundled"
    if rug.get("max_holder_pct", 0) > 25:
        return "too concentrated"
    return None

def is_blacklisted(token: str, dev: str = "") -> bool:
    return token in blacklisted_tokens or (dev and dev in blacklisted_devs)

def ml_score_token(meta: Dict[str, Any]) -> float:
    random.seed(meta.get("mint", random.random()))
    return random.uniform(70, 97)

# ==== ULTRA-EARLY (pump.fun) ====
async def ultra_early_handler(token, toxibot):
    if is_blacklisted(token):
        return
    rug = await rugcheck(token)
    if rug_gate(rug):
        activity_log.append(f"{token} UltraEarly: Rug gated.")
        return
    if token in positions:
        activity_log.append(f"{token} UltraEarly: Already traded, skipping.")
        return
    rises, last_liq, last_buyers = 0, 0, 0
    for i in range(3):
        stats = await fetch_liquidity_and_buyers(token)
        if stats['liq'] >= ULTRA_MIN_LIQ and stats['liq'] > last_liq:
            rises += 1
        last_liq, last_buyers = stats['liq'], stats['buyers']
        await asyncio.sleep(2)
    if rises < ULTRA_MIN_RISES:
        activity_log.append(f"{token} UltraEarly: Liquidity not rapidly rising, skipping.")
        return
    entry_price = await fetch_token_price(token) or 0.01
    await toxibot.send_buy(token, ULTRA_BUY_AMOUNT)
    positions[token] = {
        "src": "pumpfun",
        "buy_time": time.time(),
        "size": ULTRA_BUY_AMOUNT,
        "ml_score": ml_score_token({"mint":token}),
        "entry_price": entry_price,
        "last_price": entry_price,
        "phase": "filled",
        "pl": 0.0,
        "local_high": entry_price,
        "hard_sl": entry_price * ULTRA_SL_X,
        "runner_trail": 0.3,
        "dev": rug.get("authority")
    }
    activity_log.append(f"{token} UltraEarly: BUY {ULTRA_BUY_AMOUNT} @ {entry_price:.5f}")

# ==== SCALPER
async def scalper_handler(token, src, toxibot):
    if is_blacklisted(token):
        return
    if token in positions:
        activity_log.append(f"{token} [Scalper] Already traded. Skipping.")
        return
    pool_stats = await fetch_volumes(token)
    pool_age = await fetch_pool_age(token) or 9999
    liq_ok = pool_stats["liq"] >= SCALPER_MIN_LIQ
    vol_ok = estimate_short_vs_long_volume(pool_stats["vol_1h"], pool_stats["vol_6h"])
    age_ok = 0 <= pool_age < SCALPER_MAX_POOLAGE
    if not (liq_ok and age_ok and vol_ok):
        activity_log.append(f"{token} [Scalper] Entry FAIL: Liq:{liq_ok}, Age:{age_ok}, Vol:{vol_ok}")
        return
    rug = await rugcheck(token)
    if rug_gate(rug):
        activity_log.append(f"{token} [Scalper] Rug gated.")
        return
    entry_price = await fetch_token_price(token) or 0.01
    limit_price = entry_price * 0.97
    await toxibot.send_buy(token, SCALPER_BUY_AMOUNT, price_limit=limit_price)
    positions[token] = {
        "src": src,
        "buy_time": time.time(),
        "size": SCALPER_BUY_AMOUNT,
        "ml_score": ml_score_token({"mint": token}),
        "entry_price": limit_price,
        "last_price": limit_price,
        "phase": "waiting_fill",
        "pl": 0.0,
        "local_high": limit_price,
        "hard_sl": limit_price * SCALPER_SL_X,
        "liq_ref": pool_stats["base_liq"],
        "dev": rug.get("authority"),
    }
    activity_log.append(f"{token} Scalper: limit-buy {SCALPER_BUY_AMOUNT} @ {limit_price:.5f}")

# ==== COMMUNITY/WHALE
recent_rugdevs = set()
async def community_trade_manager(toxibot):
    while True:
        token = await community_token_queue.get()
        if is_blacklisted(token):
            continue
        rug = await rugcheck(token)
        dev = rug.get("authority")
        if rug_gate(rug) or (dev and dev in recent_rugdevs):
            activity_log.append(f"{token} [Community] rejected: Ruggate or rugdev.")
            continue
        holders_ = await fetch_holders_and_conc(token)
        if holders_["holders"] < COMM_HOLDER_THRESHOLD or holders_["max_holder_pct"] > COMM_MAX_CONC:
            activity_log.append(f"{token} [Community] fails holder/distribution screen.")
            continue
        if token in positions:
            activity_log.append(f"{token} [Community] position open. No averaging down.")
            continue
        entry_price = await fetch_token_price(token) or 0.01
        await toxibot.send_buy(token, COMMUNITY_BUY_AMOUNT)
        now = time.time()
        positions[token] = {
            "src": "community",
            "buy_time": now,
            "size": COMMUNITY_BUY_AMOUNT,
            "ml_score": ml_score_token({"mint": token}),
            "entry_price": entry_price,
            "last_price": entry_price,
            "phase": "filled",
            "pl": 0.0,
            "local_high": entry_price,
            "hard_sl": entry_price * COMM_SL_PCT,
            "dev": dev,
            "hold_until": now + COMM_HOLD_SECONDS
        }
        activity_log.append(f"{token} [Community] Buy {COMMUNITY_BUY_AMOUNT} @ {entry_price:.6f}")

# ==== process_token ====
async def process_token(token, src):
    if src == "pumpfun":
        await ultra_early_handler(token, toxibot)
    elif src in ("moralis", "bitquery"):
        await scalper_handler(token, src, toxibot)

# ==== Price Update & Exit Logic ====
async def update_position_prices_and_wallet():
    global positions, current_wallet_balance, daily_loss
    while True:
        for token, pos in list(positions.items()):
            last_price = await fetch_token_price(token)
            if last_price:
                pos['last_price'] = last_price
                pos['local_high'] = max(pos.get("local_high", last_price), last_price)
                pl = (last_price - pos['entry_price']) * (pos['size'])
                pos['pl'] = pl

                if pos["src"] == "pumpfun":
                    if last_price >= pos['entry_price'] * ULTRA_TP_X and pos['phase'] == "filled":
                        await toxibot.send_sell(token, 85)
                        pos['size'] *= 0.15
                        pos['phase'] = "runner"
                        activity_log.append(f"{token} UltraEarly: Sold 85% at 2x (runner armed).")
                    elif last_price <= pos["hard_sl"]:
                        await toxibot.send_sell(token, 100)
                        activity_log.append(f"{token} UltraEarly: SL -30%, full exit, dev blacklisted.")
                        if pos.get("dev"):
                            blacklisted_devs.add(pos["dev"])
                        pos['size'] = 0
                        pos['phase'] = "exited"
                        continue
                    elif pos["phase"] == "runner":
                        if last_price < pos["local_high"] * (1 - pos["runner_trail"]):
                            await toxibot.send_sell(token, 100)
                            activity_log.append(f"{token} UltraEarly: Runner trailed stopped at {last_price:.5f}.")
                            pos['size'] = 0
                            pos['phase'] = "exited"
                            continue

                elif pos["src"] in ("moralis", "bitquery"):
                    pool_stats = await fetch_volumes(token)
                    if pool_stats["liq"] < pos.get("liq_ref", 0)*0.6:
                        await toxibot.send_sell(token)
                        activity_log.append(f"{token} Scalper: Liq drop >40%. Blacklist dev. Exit!")
                        if pos.get("dev"):
                            blacklisted_devs.add(pos["dev"])
                        pos['size'] = 0
                        pos['phase'] = "exited"
                        continue
                    if ('phase' not in pos or pos['phase'] == "waiting_fill") and last_price >= pos['entry_price']*SCALPER_TP_X:
                        await toxibot.send_sell(token, 80)
                        pos['size'] *= 0.2
                        pos['phase'] = "runner"
                        activity_log.append(f"{token} Scalper: Sold 80% at 2x+. Runner.")
                    elif pos.get("phase", "") == "runner":
                        if last_price < pos['local_high']*(1 - SCALPER_TRAIL):
                            await toxibot.send_sell(token)
                            activity_log.append(f"{token} Scalper: Runner trailed out. Exited.")
                            pos['size'] = 0
                            pos['phase'] = "exited"
                        elif last_price < pos['hard_sl']:
                            await toxibot.send_sell(token)
                            activity_log.append(f"{token} Scalper: Hard SL hit. Blacklist dev.")
                            if pos.get("dev"):
                                blacklisted_devs.add(pos["dev"])
                            pos['size'] = 0
                            pos['phase'] = "exited"

                elif pos["src"] == "community" and pos["phase"] == "filled":
                    if last_price >= pos['entry_price'] * COMM_TP1_MULT:
                        await toxibot.send_sell(token, 50)
                        pos['size'] *= 0.5
                        pos['phase'] = "runner"
                        activity_log.append(f"{token} [Community] Sold 50% at 2x! Runner.")
                    elif last_price <= pos['hard_sl']:
                        await toxibot.send_sell(token)
                        activity_log.append(f"{token} [Community] -40% SL. Blacklist. Out.")
                        if pos.get("dev"):
                            blacklisted_devs.add(pos["dev"])
                        pos['size'] = 0
                        pos['phase'] = "exited"
                        continue
                    elif time.time() > pos.get("hold_until", 0):
                        await toxibot.send_sell(token)
                        activity_log.append(f"{token} [Community] 2 day exit.")
                        pos['size'] = 0
                        pos['phase'] = "exited"
                        continue
                    elif pos.get("phase", "") == "runner":
                        if last_price < pos["local_high"] * (1 - COMM_TRAIL):
                            await toxibot.send_sell(token)
                            activity_log.append(f"{token} [Community] Runner trailed out.")
                            pos['size'] = 0
                            pos['phase'] = "exited"

        to_remove = [k for k,v in positions.items() if v['size']==0]
        for k in to_remove:
            daily_loss += positions[k].get('pl',0)
            del positions[k]
        bal = await fetch_wallet_balance()
        if bal:
            globals()["current_wallet_balance"] = bal
        await asyncio.sleep(18)

# ==== DASHBOARD_HTML ====
DASHBOARD_HTML = r"""

|Wins/Total:|
|--|
|Win%:|
|Net P/L:|
|Wins/Total:|
|--|
|Win%:|
|Net P/L:|
|Wins/Total:|
|--|
|Win%:|
|Net P/L:|
|Token|Source|Size|ML|Entry|Last|P/L|P/L %|Phase|Age|
|--|--|--|--|--|--|--|--|--|--|
"""
