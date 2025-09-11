from database import get_db_connection

def get_momentum_portfolio():
    """
    Returns top momentum stocks as objects containing:
      - symbol (string)
      - ret    (float)  -> 252-day return %
      - ltp    (float)  -> latest close as LTP
    """
    conn = get_db_connection()
    cur = conn.cursor()

    # load all tickers
    cur.execute("SELECT tradingsymbol FROM kite_ticker_tickers")
    symbols = [r[0] for r in cur.fetchall()]

    results = []
    for sym in symbols:
        # fetch most recent 252 closes (most recent first)
        cur.execute(
            "SELECT close FROM kite_historical_data WHERE tradingsymbol=%s ORDER BY \"timestamp\" DESC LIMIT 252",
            (sym,)
        )
        rows = cur.fetchall()
        if len(rows) == 252:
            latest = float(rows[0][0])
            oldest  = float(rows[-1][0])
            ret = (latest / oldest - 1) * 100 if oldest != 0 else 0.0
            results.append({"symbol": sym, "ret": round(ret, 2), "ltp": round(latest, 2)})

    # sort by return descending and return top 15
    results.sort(key=lambda x: x["ret"], reverse=True)
    top15 = results[:15]

    cur.close()
    conn.close()
    return {"top_momentum_stocks": top15}