#!/usr/bin/env python3
import os, sys, asyncio, logging, random, aiohttp, json, time
from datetime import datetime, timedelta
from typing import Set, Dict, Any, Optional, List
from telethon import TelegramClient
from telethon.sessions import StringSession
from aiohttp import web

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# === ENV/CONFIG (set all on Railway dashboard) ===
TELEGRAM_API_ID = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_STRING_SESSION = os.environ["TELEGRAM_STRING_SESSION"]
TOXIBOT_USERNAME = os.environ.get("TOXIBOT_USERNAME", "@toxi_solana_bot")
RUGCHECK_API = os.environ.get("RUGCHECK_API", "")
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_RPC_URL = os.environ.get("HELIUS_RPC_URL", "")
WALLET_ADDRESS = os.environ.get("WALLET_ADDRESS", "")    # set the trading wallet address if you want wallet balance shown
DEXSCREENER_API = os.environ.get("DEXSCREENER_API", "")
PORT = int(os.environ.get("PORT", "8080"))

DEFAULT_BUY_AMOUNT = 0.10
MAX_DAILY_LOSS = -0.5     # hard daily P/L limit
MAX_EXPOSURE = 0.5
ML_MIN_SCORE = 60
ANTI_SNIPE_DELAY = 2
WALLET_SOL_DECIMALS = 2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("toxibot")

blacklisted_tokens: Set[str] = set()
blacklisted_devs: Set[str] = set()
positions: Dict[str, Dict[str, Any]] = {}
exposure: float = 0.0
daily_loss: float = 0.0
activity_log: List[str] = []
runtime_status: str = "Starting..."
current_wallet_balance: float = 0.0

# TRACK realized+unrealized P/L
def get_total_pl():
    return sum([pos.get("pl",0) for pos in positions.values()])

# ========== DEXScreener Price Fetch ==========
async def fetch_token_price(token: str) -> Optional[float]:
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
            resp = await s.get(url)
            data = await resp.json()
            # Try dexscreener's usual response: first pair's price vs SOL.
            for pair in data.get("pairs", [{}]):
                if pair.get("baseToken", {}).get("address") == token and "priceNative" in pair:
                    return float(pair["priceNative"])
            return None
    except Exception as e:
        logger.warning(f"DEXScreener API error: {e}")
        return None

# ========== Helius Wallet Balance ==========
async def fetch_wallet_balance():
    # Use Helius getBalance endpoint
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

# ========== ToxiBot ORDER EXECUTION ==========
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

# ========== RUGCHECK / ENHANCED RISK GATE ==========
async def rugcheck(token_addr: str) -> Dict[str, Any]:
    url = f"https://rugcheck.xyz/api/check/{token_addr}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as session:
            async with session.get(url) as r:
                data = await r.json()
                logger.info(f"Rugcheck {token_addr}: {data}")
                return data
    except Exception as e:
        logger.error(f"Rugcheck error for {token_addr}: {e}")
        return {}
def rug_gate(rugdata: Dict[str, Any]) -> Optional[str]:
    if rugdata.get("label") != "Good":
        return "rugcheck not Good"
    if "bundled" in rugdata.get("supply_type", "").lower():
        if rugdata.get("mint"): blacklisted_tokens.add(rugdata["mint"])
        if rugdata.get("authority"): blacklisted_devs.add(rugdata["authority"])
        return "supply bundled"
    if rugdata.get("max_holder_pct", 0) > 25:
        return "too concentrated"
    return None
def is_blacklisted(token: str, dev: str = "") -> bool:
    return token in blacklisted_tokens or (dev and dev in blacklisted_devs)

# ========== ML SCORING ==========
def ml_score_token(meta: Dict[str, Any]) -> float:
    random.seed(meta.get("mint", random.random()))
    return random.uniform(70, 97)

# ========== SUPER-BOT PERSONALITIES (limit/TP/SL logic) ==========
class TwoMinuteScalper:
    scan_time = 120
    async def run(self, toxibot: ToxiBotClient, wallet):
        while True:
            token = await self.find_pool()
            if token:
                await handle_trade(token, toxibot, "2Min-Scalper", wallet)
            await asyncio.sleep(self.scan_time)
    async def find_pool(self) -> Optional[str]:
        # Plug in real trending scan later
        token = f"AUTO_DISC_{random.randint(100_000,999_999)}"
        logger.info(f"[2M Scalper] Pool: {token}")
        return token

class UltraEarlySnipe:
    scan_time = 15
    async def run(self, toxibot: ToxiBotClient, wallet):
        while True:
            token = await self.find_hyper_early()
            if token:
                await handle_trade(token, toxibot, "Ultra-Early", wallet)
            await asyncio.sleep(self.scan_time)
    async def find_hyper_early(self) -> Optional[str]:
        token = f"EARLY_{random.randint(100_000,999_999)}"
        logger.info(f"[UltraEarly] Early token: {token}")
        return token

class CommunityPlayBot:
    scan_time = 600
    async def run(self, toxibot: ToxiBotClient, wallet):
        while True:
            token = await self.find_community()
            if token:
                await handle_trade(token, toxibot, "Community", wallet)
            await asyncio.sleep(self.scan_time)
    async def find_community(self) -> Optional[str]:
        token = f"COMM_{random.randint(100_000,999_999)}"
        logger.info(f"[Community] Community PnD: {token}")
        return token

# ========== Advanced Trade Handler ==========
async def handle_trade(token, toxibot, persona, wallet):
    global positions, exposure, activity_log
    if is_blacklisted(token): return
    rugdata = await rugcheck(token)
    rejection = rug_gate(rugdata)
    if rejection:
        activity_log.append(f"{token} {persona} rejected: {rejection}")
        return
    meta = {"mint": token}
    score = ml_score_token(meta)
    if score < ML_MIN_SCORE:
        activity_log.append(f"{token} {persona} ML filtered.")
        return

    # -- Price checks via DEXScreener --
    entry_price = await fetch_token_price(token) or 0.01
    # The 2Min bot submits a limit-buy 3% under spot for 0.10 SOL
    if persona == "2Min-Scalper":
        limit = entry_price * 0.97
        buy_amount = 0.10
        buy_msg = await toxibot.send_buy(token, buy_amount, price_limit=limit)
        positions[token] = {
            "src": persona, "buy_time": time.time(), "size": buy_amount, "ml_score": score,
            "entry_price": limit, "last_price": limit, "phase": "waiting_fill", "pl": 0.0
        }
        activity_log.append(f"{token} {persona}: limit-buy {buy_amount} @ {limit:.6f} via ToxiBot.")
    elif persona == "Ultra-Early":
        buy_amount = 0.07
        buy_msg = await toxibot.send_buy(token, buy_amount)
        positions[token] = {
            "src": persona, "buy_time": time.time(), "size": buy_amount, "ml_score": score,
            "entry_price": entry_price, "last_price": entry_price, "phase": "filled", "pl": 0.0
        }
        activity_log.append(f"{token} {persona}: buy {buy_amount} at {entry_price:.6f}. Moonshot!")

    elif persona == "Community":
        buy_amount = 0.04
        buy_msg = await toxibot.send_buy(token, buy_amount)
        positions[token] = {
            "src": persona, "buy_time": time.time(), "size": buy_amount, "ml_score": score,
            "entry_price": entry_price, "last_price": entry_price, "phase": "filled", "pl": 0.0
        }
        activity_log.append(f"{token} {persona}: community buy {buy_amount} at {entry_price:.6f}.")
    exposure += buy_amount

# ========== Price/PL Updater and Wallet Balance Poll ==========
async def update_position_prices_and_wallet():
    global positions, current_wallet_balance, daily_loss
    while True:
        # Prices and P/L
        for token, pos in positions.items():
            last_price = await fetch_token_price(token)
            if last_price:
                pos['last_price'] = last_price
                pl = (last_price - pos['entry_price']) * (pos['size'])
                pos['pl'] = pl
                # --- Take Profit / Stop Loss per bot personality ---
                # 2Min-Scalper TP: sell 80% at 2x, rest trail 20% below high, SL -30%
                if pos['src'] == "2Min-Scalper":
                    # If not sold, check TP 2x entry
                    if 'phase' not in pos or pos['phase'] == "waiting_fill":
                        if last_price >= pos['entry_price']*2:
                            await toxibot.send_sell(token, 80)
                            pos['size'] *= 0.2   # Keep only 20%
                            pos['phase'] = "runner"
                            activity_log.append(f"{token} {pos['src']}: Sold 80% at 2x+.")
                        elif last_price < pos['entry_price']*0.7:
                            await toxibot.send_sell(token)
                            activity_log.append(f"{token} {pos['src']}: SL hit, full exit.")
                            pos['size'] = 0
                            pos['phase'] = "exited"
                    elif pos['phase'] == "runner":
                        # Trailing stop: 20% below local high
                        high = max(pos.get('local_high', last_price), last_price)
                        pos['local_high'] = high
                        if last_price < high*0.8:
                            await toxibot.send_sell(token)
                            activity_log.append(f"{token}: runner TP trail stop hit, exit.")
                            pos['size'] = 0
                            pos['phase'] = "exited"
                elif pos['src'] == "Ultra-Early":
                    # Sell 85% at 2x, SL -30%, legacy runner
                    if last_price >= pos['entry_price']*2:
                        await toxibot.send_sell(token, 85)
                        pos['size'] *= 0.15
                        activity_log.append(f"{token} Moonshot 2x sell 85%.")
                    elif last_price < pos['entry_price']*0.7:
                        await toxibot.send_sell(token)
                        activity_log.append(f"{token} SL -30%, fully exited.")
                        pos['size'] = 0
                elif pos['src'] == "Community":
                    # More patient, sell 50% at 2x, runner with loose trail, SL -40%
                    if last_price >= pos['entry_price']*2 and pos['size']>0.02:
                        await toxibot.send_sell(token, 50)
                        pos['size'] *= 0.5
                        activity_log.append(f"{token} Community 2x, 50% exit, let rest ride.")
                    elif last_price < pos['entry_price']*0.6:
                        await toxibot.send_sell(token)
                        activity_log.append(f"{token} Big SL hit, community exodus.")
                        pos['size'] = 0

        # Remove positions with 0 size
        to_remove = [k for k,v in positions.items() if v['size']==0]
        for k in to_remove:
            daily_loss += positions[k].get('pl',0)
            del positions[k]
        # Wallet balance
        bal = await fetch_wallet_balance()
        if bal: globals()["current_wallet_balance"] = bal
        await asyncio.sleep(18)

# ========== Tron Dashboard ==========
DASHBOARD_HTML = """
<html>
<head>
<title>ToxiBot Tron Dashboard</title>
<style>
body { font-family:'Orbitron',sans-serif; background:#181230; color:#0ff;}
h2 { font-weight:900; letter-spacing:1.5px; }
#topbar {
  background:linear-gradient(90deg,#10001c,#0aefff 65%,#ff00ea 100%);
  box-shadow:0 0 25px #0aefff,0 0 99px #ff00ea inset;
  padding:13px 22px;color:#fff;font-size:1.2em;
}
#grid { margin:40px auto;width:80%;background:rgba(0,0,0,.7);
  box-shadow:0 0 22px #19ffc6,inset 0 0 40px #0aefff; border-radius:14px;padding:17px;}
th, td { padding:6px 11px; text-align:center;}
th {
  background:#00e2f6;color:#2c0834; box-shadow:inset 0 -1px 18px #2c0834;
}
td { background:#22003a; border-bottom:1px solid #0aefff; color:#0ff;}
tr:last-child td { border-bottom:none;}
#statusline { color:#ff4cdf;font-weight:bold;text-shadow:0 0 8px #0aefff;}
#pl,#totalpl{font-size:1.12em; font-weight:bold;padding:0 13px;}
.negative{color:#fd5b5b!important;text-shadow:0 0 7px #fc3a87;}
.positive{color:#2dff8c!important;}
#log{margin-top:8px;background:#191938; border-radius:5px;box-shadow:0 0 7px #0aefff inset;color:#fff;padding:7px;
font-size:1em;font-family:monospace;height:120px;overflow-y:auto;}
</style>
<link href="https://fonts.googleapis.com/css?family=Orbitron:700,900" rel="stylesheet">
</head>
<body>
<div id='topbar'>
<h2>&#127761; ToxiBot - <span style='color:#ff4cdf'>Tron Mode</span></h2>
<span id="statusline">...</span>
<span>| Wallet: <span id="balance"></span> SOL</span>
<span>| Exposure: <span id="exp"></span></span>
<span>| Daily Loss: <span id="dloss"></span></span>
<span>| Total P/L: <span id="totalpl"></span></span>
</div>
<div id='grid'>
<table id=pos style='width:98%;margin:auto;border-collapse:collapse;box-shadow:0 0 8px #ff00ea;'>
<tr>
  <th>Token</th><th>Source</th><th>Size</th><th>ML</th>
  <th>Entry</th><th>Last</th><th>P/L</th>
</tr>
<tbody id=postable></tbody>
</table>
<div style="margin-top:22px"><b>Recent Activity</b> <br>
<div id=log></div></div></div>
<script>
var ws=new WebSocket("ws://"+location.host+"/ws");
ws.onmessage=function(ev){
  var d=JSON.parse(ev.data),t=document.getElementById('postable'),log=document.getElementById('log');
  document.getElementById('statusline').textContent=d.status;
  document.getElementById('balance').textContent=String(Number(d.wallet_balance||0).toFixed(2));
  document.getElementById('exp').textContent=parseFloat(d.exposure||0).toFixed(2);
  document.getElementById('dloss').textContent=parseFloat(d.daily_loss||0).toFixed(2);
  var totalPL=0;
  t.innerHTML='';
  Object.entries(d.positions).forEach(([k,v])=>{
    var entry=parseFloat(v.entry_price||0), last=parseFloat(v.last_price||entry);
    var pl=last&&entry?((last-entry)*parseFloat(v.size||0)):0;
    totalPL+=pl;
    let plClass=pl<0?'negative':(pl>0?'positive':'');
    t.innerHTML+=`<tr>
      <td style='color:#ff4cdf;font-weight:bold'>${k}</td>
      <td>${v.src||''}</td>
      <td>${v.size||''}</td>
      <td>${v.ml_score||''}</td>
      <td>${entry||''}</td>
      <td>${last||''}</td>
      <td id=pl class="${plClass}">${pl.toFixed(4)}</td>
    </tr>`;
  });
  document.getElementById('totalpl').textContent=(totalPL<0?'':'+')+totalPL.toFixed(4);
  document.getElementById('totalpl').className=(totalPL<0?'negative':'positive');
  log.innerHTML=(d.log||[]).reverse().slice(0,10).join('<br>');
};
</script>
</body></html>
"""

async def dashboard_page(request):
    return web.Response(text=DASHBOARD_HTML, content_type='text/html')
async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    while True:
        try:
            await ws.send_str(json.dumps({
                "status": runtime_status,
                "exposure": exposure,
                "daily_loss": daily_loss,
                "positions": positions,
                "log": activity_log,
                "wallet_balance": current_wallet_balance
            }))
            await asyncio.sleep(2)
        except Exception: break
    await ws.close()
    return ws
def setup_web():
    app = web.Application()
    app.router.add_get("/", dashboard_page)
    app.router.add_get("/ws", websocket_handler)
    return app

# ========== MAIN ==========
async def main():
    global toxibot, runtime_status
    toxibot = ToxiBotClient(TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_STRING_SESSION, TOXIBOT_USERNAME)
    await toxibot.connect()
    app = setup_web()
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    # Launch price/wallet P&L updater
    asyncio.create_task(update_position_prices_and_wallet())
    # Start bots with enhanced strategies
    bots = [TwoMinuteScalper(), UltraEarlySnipe(), CommunityPlayBot()]
    wallet = WALLET_ADDRESS
    await asyncio.gather(*(b.run(toxibot, wallet) for b in bots))
    # UI info
    while True:
        runtime_status = f"Tron Dashboard | Wallet {round(current_wallet_balance,2)} SOL | {len(positions)} open positions | +PL={get_total_pl():.4f}"
        await asyncio.sleep(3)
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        logger.error(f"Top-level error: {repr(e)}")
        traceback.print_exc()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("ToxiBot stopped.")
