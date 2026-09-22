import time
import json
import os
import sys
import pandas as pd
import numpy as np
import requests
import ccxt

API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
IS_TESTNET = os.environ.get("IS_TESTNET", "True").lower() == "true"

TOP_N = 10
INDIVIDUAL_STOP_PCT = 0.20
BTC_CIRCUIT_BREAKER_PCT = 0.20
MA_WINDOW = 200
TAKE_PROFIT_MULTIPLIER = 2.0
STATE_FILE = "bot_state.json"

STABLECOINS = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD"}
MEMECOINS = {"DOGE", "SHIB", "PEPE", "FLOKI", "BONK", "WIF"}
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"

if not API_KEY or not API_SECRET:
    print("HATA: API anahtarlari yok")
    sys.exit(1)

exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})
if IS_TESTNET:
    exchange.set_sandbox_mode(True)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"status": "OUT","active_cycle_capital": None,"positions": {},"btc_peak": None,}

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=4)
    os.replace(tmp, STATE_FILE)
def call_with_retry(func, *args, max_retries=3, base_delay=5, **kwargs):
    last = None
    for i in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last = e
            time.sleep(base_delay * (2 ** (i-1)))
    raise last

def get_btc_data_and_ma():
    bars = call_with_retry(exchange.fetch_ohlcv, "BTC/USDT", timeframe="1d", limit=MA_WINDOW+10)
    df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
    df["close"] = df["close"].astype(float)
    df["ma"] = df["close"].rolling(window=MA_WINDOW).mean()
    return df["close"].iloc[-1], df["close"].iloc[-1] > df["ma"].iloc[-1]

def get_real_market_caps(limit=100):
    params = {"vs_currency":"usd","order":"market_cap_desc","per_page":limit,"page":1,"sparkline":"false"}
    resp = call_with_retry(requests.get, COINGECKO_MARKETS_URL, params=params, timeout=15)
    resp.raise_for_status()
    return {item["symbol"].upper(): item["market_cap"] for item in resp.json() if item.get("market_cap")}

def get_top_n_symbols(exclude=None, n=TOP_N):
    exclude = exclude or set()
    caps = get_real_market_caps(100)
    markets = call_with_retry(exchange.load_markets)
    ranked = sorted(caps.items(), key=lambda x: x[1], reverse=True)
    result = []
    for sym,_ in ranked:
        if sym in STABLECOINS or sym in MEMECOINS or sym in exclude:
            continue
        pair = f"{sym}/USDT"
        if pair in markets:
            result.append(pair)
        if len(result) >= n:
            break
    return result
def market_buy(symbol, usdt_amount):
    try:
        order = call_with_retry(exchange.create_market_buy_order, symbol, None, params={"quoteOrderQty":usdt_amount})
        qty = float(order.get("filled",0))
        price = float(order.get("average") or order.get("price") or 0)
        return qty, price
    except Exception as e:
        print(f"HATA {symbol}: {e}")
        return 0.0, 0.0

def market_sell(symbol, qty):
    try:
        order = call_with_retry(exchange.create_market_sell_order, symbol, qty)
        price = float(order.get("average") or order.get("price") or 0)
        proceeds = float(order.get("cost") or (qty*price))
        return price, proceeds
    except Exception as e:
        print(f"HATA {symbol}: {e}")
        return 0.0, 0.0

def get_usdt_cash():
    bal = call_with_retry(exchange.fetch_balance)
    return float(bal.get("free",{}).get("USDT",0.0))

def get_portfolio_value(state, tickers):
    total = get_usdt_cash()
    for sym,pos in state["positions"].items():
        if sym in tickers and tickers[sym].get("last"):
            total += pos["qty"]*tickers[sym]["last"]
    return total
  def run_bot_cycle(state):
    tickers = call_with_retry(exchange.fetch_tickers)
    btc_price, is_bullish = get_btc_data_and_ma()
    usdt_cash = get_usdt_cash()
    portfolio_value = get_portfolio_value(state, tickers)
    print(f"[DURUM] status={state['status']} BTC={btc_price:.2f} Portfoy=${portfolio_value:.2f}")

    if state["active_cycle_capital"] is None:
        state["active_cycle_capital"] = portfolio_value
    if state["btc_peak"] is None or btc_price > state["btc_peak"]:
        state["btc_peak"] = btc_price
    btc_drawdown = (state["btc_peak"]-btc_price)/state["btc_peak"]

    if state["status"] == "OUT":
        if usdt_cash < 10:
            return state
        top_n = get_top_n_symbols()
        per_coin = usdt_cash/len(top_n) if top_n else 0
        new_positions = {}
        for sym in top_n:
            qty,price = market_buy(sym, per_coin)
            if qty>0:
                new_positions[sym] = {"qty":qty,"entry_price":price,"peak_price":price}
        if new_positions:
            state["positions"] = new_positions
            state["status"] = "IN"
            state["active_cycle_capital"] = get_portfolio_value(state, tickers)
            state["btc_peak"] = btc_price
        return state

    if state["status"] == "CIRCUIT_BREAKER_WAIT":
        if is_bullish:
            state["status"] = "OUT"
            state["btc_peak"] = btc_price
        return state

    if state["status"] == "IN":
        if portfolio_value >= state["active_cycle_capital"]*TAKE_PROFIT_MULTIPLIER:
            for sym,pos in list(state["positions"].items()):
                market_sell(sym, pos["qty"])
            state["positions"] = {}
            state["status"] = "OUT"
            state["active_cycle_capital"] = get_usdt_cash()
            state["btc_peak"] = btc_price
            save_state(state)
            return state

        if btc_drawdown >= BTC_CIRCUIT_BREAKER_PCT:
            for sym,pos in list(state["positions"].items()):
                market_sell(sym, pos["qty"])
            state["positions"] = {}
            state["status"] = "CIRCUIT_BREAKER_WAIT"
            state["btc_peak"] = btc_price
            save_state(state)
            return state

        for sym in list(state["positions"].keys()):
            if sym not in tickers or not tickers[sym].get("last"):
                continue
            cur = tickers[sym]["last"]
            pos = state["positions"][sym]
            if cur > pos["peak_price"]:
                pos["peak_price"] = cur
            dd = (pos["peak_price"]-cur)/pos["peak_price"]
            if dd >= INDIVIDUAL_STOP_PCT:
                _,proceeds = market_sell(sym, pos["qty"])
                if proceeds <= 0:
                    continue
                del state["positions"][sym]
                save_state(state)
                exclude_set = {s.split("/")[0] for s in state["positions"].keys()}
                cands = get_top_n_symbols(exclude=exclude_set, n=TOP_N+5)
                for cand in cands:
                    if cand in state["positions"]:
                        continue
                    qty,price = market_buy(cand, proceeds)
                    if qty>0:
                        state["positions"][cand] = {"qty":qty,"entry_price":price,"peak_price":price}
                        break
                save_state(state)
        return state
    return state

def main():
    print("GitHub Actions Tek Seferlik Mod")
    state = load_state()
    try:
        state = run_bot_cycle(state)
        save_state(state)
        print("BITTI - Basarili")
    except Exception as e:
        print(f"HATA {e}")
        save_state(state)
        sys.exit(1)

if __name__ == "__main__":
    main()
