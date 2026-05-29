import asyncio, json, os, sys, time, threading
from pathlib import Path
import httpx

# ── Config ────────────────────────────────────────────────────────────────────
def _load_config() -> dict:
    defaults = {
        "language":              "en_US",
        "request_timeout_ms":    8000,
        "poll_interval_ms":      1000,
        "round_interval_ms":     2000,
        "max_consecutive_errors": 3,
        "mining_duration_s":     60,
        "game_api_base_url":     "https://api.km.cocweb3.com",
    }
    for p in [Path("config.json"), Path(__file__).parent / "config.json"]:
        if p.exists():
            try:
                defaults.update(json.loads(p.read_text("utf-8")))
                break
            except Exception:
                pass
    return defaults

CFG = _load_config()

GAME_API_BASE_URL      = CFG["game_api_base_url"]
REQUEST_TIMEOUT_MS     = int(CFG["request_timeout_ms"])
POLL_INTERVAL_MS       = int(CFG["poll_interval_ms"])
ROUND_INTERVAL_MS      = int(CFG["round_interval_ms"])
MAX_CONSECUTIVE_ERRORS = int(CFG["max_consecutive_errors"])
MINING_DURATION_S      = int(CFG["mining_duration_s"])
LANG                   = CFG["language"]

# ── Profiles ──────────────────────────────────────────────────────────────────
def load_profiles() -> list[dict]:
    for p in [Path("profiles.json"), Path(__file__).parent / "profiles.json"]:
        if p.exists():
            try:
                data = json.loads(p.read_text("utf-8"))
                profiles = data if isinstance(data, list) else data.get("profiles", [])
                valid = [x for x in profiles if x.get("api_code", "").strip()]
                if valid:
                    return valid
            except Exception:
                pass
    sys.exit("[ERROR] profiles.json not found or empty")

# ── Error codes ───────────────────────────────────────────────────────────────
CODE_SUCCESS               = 0
CODE_INSUFFICIENT_STAMINA  = 2003
CODE_DIAMOND_NOT_ENOUGH    = 2008
CODE_MINING_API_NOT_ACTIVE = 2014
CODE_INVALID_PARAMS        = 400
CODE_MINING_NOT_FINISHED   = 2018
CRITICAL_STOP_CODES = {CODE_DIAMOND_NOT_ENOUGH, CODE_MINING_API_NOT_ACTIVE, CODE_INVALID_PARAMS}
RETRY_CODES         = {CODE_MINING_NOT_FINISHED}

# ── Ore map ───────────────────────────────────────────────────────────────────
ORE_NAME_MAP: dict = {
    "1": {"en_US": "Gold",      "zh_CN": "金矿石",    "ja_JP": "金鉱石"},
    "2": {"en_US": "Copper",    "zh_CN": "铜矿石",    "ja_JP": "銅鉱石"},
    "3": {"en_US": "Iron",      "zh_CN": "铁矿石",    "ja_JP": "鉄鉱石"},
    "4": {"en_US": "Cobalt",    "zh_CN": "钴矿石",    "ja_JP": "コバルト鉱石"},
    "5": {"en_US": "Uranium",   "zh_CN": "铀矿石",    "ja_JP": "ウラン鉱石"},
    "6": {"en_US": "Ismium",    "zh_CN": "伊斯密矿石", "ja_JP": "イズミウム鉱石"},
    "7": {"en_US": "Iridium",   "zh_CN": "铱矿石",    "ja_JP": "イリジウム鉱石"},
    "8": {"en_US": "Tourmaline","zh_CN": "电气石矿石", "ja_JP": "電気石鉱石"},
}

def load_ore_map():
    for p in [Path("config/ore-type-map.json"), Path(__file__).parent / "config" / "ore-type-map.json"]:
        if p.exists():
            try:
                ORE_NAME_MAP.update(json.loads(p.read_text("utf-8")).get("oreTypeNameMap", {}))
                return
            except Exception:
                pass

def ore_name(ore_type):
    e = ORE_NAME_MAP.get(str(ore_type))
    return (e.get(LANG) or e.get("en_US") or f"Ore#{ore_type}") if e else f"Ore#{ore_type}"

def format_reward(bars: dict, exp: int) -> str:
    parts = [f"{name}×{cnt}" for name, cnt in bars.items() if cnt]
    if exp: parts.append(f"EXP+{exp}")
    return " | ".join(parts) if parts else "—"

# ── ANSI / Dashboard ──────────────────────────────────────────────────────────
RST  = "\x1b[0m"; DIM = "\x1b[2m"; BLD = "\x1b[1m"
CYAN = "\x1b[96m"; GRN = "\x1b[92m"; RED = "\x1b[91m"
YLW  = "\x1b[93m"; MGT = "\x1b[95m"

_lock = threading.Lock()

def _goto(r, c=1): return f"\x1b[{r};{c}H"
def _clr():        return "\x1b[2K"
def _hide():       sys.stdout.write("\x1b[?25l"); sys.stdout.flush()
def _show():       sys.stdout.write("\x1b[?25h"); sys.stdout.flush()
def _write(*parts):
    with _lock:
        sys.stdout.write("".join(parts)); sys.stdout.flush()

W       = 118
LOG_MAX = 10

R_TOP    = 1
R_TTL    = 2
R_SEP1   = 3
R_ACC    = 4
R_S      = 5
R_D      = 6
R_RND    = 7
R_RWD    = 8
R_STA    = 9
R_SEP2   = 10
R_LOGTT  = 11
R_LOGSEP = 12
R_LOG0   = 13
R_CURSOR = R_LOG0 + LOG_MAX + 1

_total_accs = 0
_log_buf: list[tuple] = []
_state = {
    "acc":     "-",
    "stamina": "-",
    "diamond": "-",
    "round":   0,
    "status":  "Starting...",
    "reward":  "—",
}

def _border(): return DIM + "+" + "-"*W + "+" + RST

def _row(label, val, label_color="", val_color=""):
    label_w = 14
    val_w   = W - label_w - 3
    label_s = label.ljust(label_w)
    val_s   = str(val)
    if len(val_s) > val_w: val_s = val_s[:val_w-2] + ".."
    val_s = val_s.ljust(val_w)
    return DIM+"|"+RST + f" {label_color}{label_s}{RST}: " + val_color + val_s + RST + DIM+"|"+RST

def _status_color(s):
    s = str(s)
    if any(x in s for x in ("Done", "Claimed", "FINISHED")): return GRN
    if any(x in s for x in ("Error", "Stopped", "STOPPED")):  return RED
    if any(x in s for x in ("Wait", "Delay", "Sleeping")):    return DIM
    if "Mining" in s: return CYAN
    return YLW

def _draw_static():
    title  = f"  ClawQuest Mining Bot  |  {GAME_API_BASE_URL}  |  Mining: {MINING_DURATION_S}s/round  |  Lang: {LANG}  "
    loghdr = f"  LIVE LOG  |  Accounts: {_total_accs}  |  Sequential mode  "
    lines  = [
        "\x1b[2J\x1b[H",
        _goto(R_TOP)    + _border(),
        _goto(R_TTL)    + DIM+"|"+RST + BLD+CYAN + title.ljust(W)[:W] + RST + DIM+"|"+RST,
        _goto(R_SEP1)   + _border(),
        _goto(R_ACC)    + _row("Account",  _state["acc"],     DIM, MGT),
        _goto(R_S)      + _row("Stamina",  _state["stamina"], DIM, YLW),
        _goto(R_D)      + _row("Diamond",  _state["diamond"], DIM, CYAN),
        _goto(R_RND)    + _row("Round",    _state["round"],   DIM, MGT),
        _goto(R_RWD)    + _row("Reward",   _state["reward"],  DIM, GRN),
        _goto(R_STA)    + _row("Status",   _state["status"],  DIM, _status_color(_state["status"])),
        _goto(R_SEP2)   + _border(),
        _goto(R_LOGTT)  + DIM+"|"+RST + BLD + loghdr.ljust(W)[:W] + RST + DIM+"|"+RST,
        _goto(R_LOGSEP) + DIM + "|" + "-"*W + "|" + RST,
    ]
    for i in range(LOG_MAX):
        lines.append(_goto(R_LOG0 + i) + " " * (W + 2))
    lines.append(_goto(R_CURSOR))
    _write(*lines)

def _redraw_dynamic():
    st  = _state["status"]
    clr = _status_color(st)
    _write(
        _goto(R_ACC) + _clr() + _row("Account", _state["acc"],     DIM, MGT),
        _goto(R_S)   + _clr() + _row("Stamina", _state["stamina"], DIM, YLW),
        _goto(R_D)   + _clr() + _row("Diamond", _state["diamond"], DIM, CYAN),
        _goto(R_RND) + _clr() + _row("Round",   _state["round"],   DIM, MGT),
        _goto(R_RWD) + _clr() + _row("Reward",  _state["reward"],  DIM, GRN),
        _goto(R_STA) + _clr() + _row("Status",  st,                DIM, clr),
        _goto(R_CURSOR),
    )

def update(**kwargs):
    _state.update(kwargs)
    _redraw_dynamic()

def add_log(msg):
    global _log_buf
    ts = time.strftime('%H:%M:%S')
    _log_buf.append((ts, msg))
    if len(_log_buf) > LOG_MAX: _log_buf = _log_buf[-LOG_MAX:]
    buf = list(_log_buf)
    out = []
    for i, (t, m) in enumerate(buf):
        entry   = f" {t}  {m}"
        if len(entry) > W: entry = entry[:W-2]+".."
        colored = f" {DIM}{t}{RST}  {entry[len(t)+3:]}"
        out.append(_goto(R_LOG0+i) + _clr() + colored.ljust(W+2))
    for i in range(len(buf), LOG_MAX):
        out.append(_goto(R_LOG0+i) + _clr() + " "*(W+2))
    out.append(_goto(R_CURSOR))
    _write(*out)

# ── HTTP ──────────────────────────────────────────────────────────────────────
def _make_client(api_code: str, proxy: str | None):
    raw = (proxy or "").strip().lower()
    use_proxy = proxy.strip() if proxy and raw not in ("", "null", "none") else None
    return httpx.AsyncClient(
        base_url=GAME_API_BASE_URL,
        headers={"X-Api-Code": api_code, "Content-Type": "application/json"},
        proxy=use_proxy,
        timeout=REQUEST_TIMEOUT_MS / 1000,
    )

async def api_post(client, path, body={}):
    r = await client.post(path, json=body)
    r.raise_for_status()
    return r.json()

def extract_code(body):
    c = body.get("code")
    return int(c) if isinstance(c, (int, float)) else None

# ── Mine one account ──────────────────────────────────────────────────────────
async def mine_account(idx: int, total: int, profile: dict) -> str:
    """
    Returns:
      "no_stamina"  — hết stamina, chuyển acc tiếp theo
      "critical"    — lỗi nghiêm trọng, dừng hẳn
      "done"        — stopped by user
    """
    api_code  = profile["api_code"].strip()
    proxy     = profile.get("proxy", "").strip() or None
    label     = profile.get("label", f"Acc#{idx}")
    acc_str   = f"[{idx}/{total}] {label}"

    consecutive_errors = 0
    round_num          = 0
    total_bars: dict   = {}
    total_exp          = 0

    update(acc=acc_str, stamina="-", diamond="-", round=0, reward="—", status="Connecting...")
    add_log(f"▶ {acc_str} started")

    async with _make_client(api_code, proxy) as client:
        while True:
            try:
                # ── Stamina check ──────────────────────────────────────────────
                st_resp = await api_post(client, "/api/getStamina")
                st_data = st_resp.get("data", {})
                stamina = int(st_data.get("stamina", 0))
                max_sta = int(st_data.get("maxStamina", 0))
                diamond = int(st_data.get("diamonds", 0))
                update(stamina=f"{stamina}/{max_sta}", diamond=str(diamond))

                if stamina <= 0:
                    add_log(f"✖ {label} — no stamina ({stamina}/{max_sta}), switching acc")
                    update(status="No Stamina — next acc")
                    return "no_stamina"

                # ── Start mining ───────────────────────────────────────────────
                update(status="Start Mining...")
                try:
                    start_resp = await api_post(client, "/api/startMining")
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 500:
                        add_log(f"HTTP 500 on startMining — no stamina likely")
                        update(status="No Stamina — next acc")
                        return "no_stamina"
                    raise

                consecutive_errors = 0

                # ── Wait: prefer server estimatedEndAt, fallback to config ──────
                wait_s = MINING_DURATION_S
                try:
                    end_at = int(start_resp["data"]["estimatedEndAt"])
                    now_ms = int(time.time() * 1000)
                    if end_at > now_ms:
                        wait_s = max(1, (end_at - now_ms) // 1000 + 10)
                except Exception:
                    pass
                update(status=f"Mining — Wait {wait_s}s")
                await asyncio.sleep(wait_s)

                # ── Claim reward ───────────────────────────────────────────────
                update(status="Claiming...")
                while True:
                    end_resp = await api_post(client, "/api/endMining")
                    e_code   = extract_code(end_resp)

                    if e_code in (None, CODE_SUCCESS):
                        round_num += 1
                        res_list = []
                        try: res_list = end_resp["data"]["reward"]["resResults"] or []
                        except Exception: pass
                        exp = 0
                        try: exp = int(end_resp["data"]["reward"]["collectedExp"])
                        except Exception: pass
                        total_exp += exp

                        round_bars = {}
                        for r in res_list:
                            name = ore_name(r.get("oreType", 0))
                            cnt  = int(r.get("barCount", 0))
                            round_bars[name] = cnt
                            total_bars[name] = total_bars.get(name, 0) + cnt

                        update(
                            round  = round_num,
                            status = f"Claimed R{round_num}",
                            reward = format_reward(total_bars, total_exp),
                        )
                        add_log(f"✔ {label} R{round_num}: {format_reward(round_bars, exp)}")
                        break

                    if e_code in RETRY_CODES:
                        update(status="Polling reward...")
                        await asyncio.sleep(POLL_INTERVAL_MS / 1000)
                        continue

                    if e_code in CRITICAL_STOP_CODES:
                        raise RuntimeError(f"Critical code={e_code}")

                    raise RuntimeError(f"endMining code={e_code}")

                # ── Delay between rounds ───────────────────────────────────────
                delay_s = ROUND_INTERVAL_MS // 1000
                update(status=f"Sleeping {delay_s}s...")
                await asyncio.sleep(delay_s)

            except httpx.TimeoutException:
                consecutive_errors += 1
                update(status=f"Timeout ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS})")
                add_log(f"Timeout #{consecutive_errors}")
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    add_log(f"✖ {label} — timeout limit reached")
                    return "no_stamina"
                await asyncio.sleep(POLL_INTERVAL_MS / 1000)

            except Exception as e:
                msg = str(e)
                if "2003" in msg:
                    add_log(f"✖ {label} — stamina insufficient (API), switching acc")
                    update(status="No Stamina — next acc")
                    return "no_stamina"
                if any(c in msg for c in ["2008", "2014", "400", "Critical"]):
                    add_log(f"✖ {label} — critical: {msg[:50]}")
                    update(status="STOPPED — critical error")
                    return "critical"
                consecutive_errors += 1
                update(status=f"Error ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS})")
                add_log(f"Err: {msg[:55]}")
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    return "no_stamina"
                await asyncio.sleep(POLL_INTERVAL_MS / 1000)

# ── Main loop ─────────────────────────────────────────────────────────────────
async def run_all(profiles: list[dict]):
    total = len(profiles)
    for idx, profile in enumerate(profiles, 1):
        result = await mine_account(idx, total, profile)
        if result == "critical":
            add_log("Critical error — bot stopped")
            break
        if idx < total:
            label_next = profiles[idx].get("label", f"Acc#{idx+1}")
            add_log(f"→ Switching to [{idx+1}/{total}] {label_next}")
    update(status="FINISHED — all accounts done")
    add_log("All accounts processed")

# ── Entry ─────────────────────────────────────────────────────────────────────
def main():
    global _total_accs
    load_ore_map()
    profiles     = load_profiles()
    _total_accs  = len(profiles)

    os.system("cls" if os.name == "nt" else "clear")
    _hide()
    try:
        _draw_static()
        add_log(f"Loaded {_total_accs} account(s) from profiles.json")
        asyncio.run(run_all(profiles))
        time.sleep(1)
    except KeyboardInterrupt:
        add_log("Interrupted by user")
        update(status="STOPPED — user interrupt")
        time.sleep(0.5)
    finally:
        _show()
        _write(_goto(R_CURSOR) + "\n")

if __name__ == "__main__":
    main()