import ccxt
print("Bot basladi")
exchange = ccxt.binance()
btc = exchange.fetch_ticker("BTC/USDT")['last']
print(f"BTC: {btc}")
