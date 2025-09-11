from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import yfinance as yf
import math
from pydantic import BaseModel
from typing import List, Union

# Create a FastAPI instance dedicated to charts endpoints
charts_app = FastAPI()
templates = Jinja2Templates(directory="templates")

class CandleData(BaseModel):
    time: Union[str, int]
    open: float
    high: float
    low: float
    close: float
    volume: float
    ema9: Union[float, None] = None
    ema14: Union[float, None] = None
    ema50: Union[float, None] = None

def calculate_ema(data, period: int):
    ema = []
    multiplier = 2 / (period + 1)
    # Initialize with the first closing price
    ema.append(data[0]['close'])
    
    for i in range(1, len(data)):
        ema_value = (data[i]['close'] - ema[i-1]) * multiplier + ema[i-1]
        ema.append(ema_value)
    return ema

@charts_app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Optional: If you want a direct root route in this sub-app, 
    # you can serve a template or a simple message
    return templates.TemplateResponse("index.html", {"request": request})

@charts_app.get("/api/ticker/{ticker}", response_model=List[CandleData])
def get_ticker_data(
    ticker: str, 
    interval: str = Query("1d"), 
    ema_periods: str = Query("9,14,50")
):
    stock = yf.Ticker(ticker)
    # Determine how far back to fetch based on interval
    if interval in ["15m", "60m"]:
        period = "60d"
    elif interval in ["1d", "1wk", "1mo"]:
        period = "5y"
    else:
        period = "1mo"
    
    hist = stock.history(period=period, interval=interval)
    if hist.empty and interval in ["15m", "60m"]:
        # Fallback if no data was returned for short intervals
        period = "30d"
        hist = stock.history(period=period, interval=interval)
    
    formatted_data = []
    raw_data = []
    for timestamp, row in hist.iterrows():
        try:
            open_val = float(row["Open"])
            high_val = float(row["High"])
            low_val = float(row["Low"])
            close_val = float(row["Close"])
            volume_val = float(row["Volume"])
        except Exception:
            continue

        # Filter out non-finite data
        if not all(map(math.isfinite, [open_val, high_val, low_val, close_val, volume_val])):
            continue

        # For intraday intervals, use a UNIX timestamp; otherwise, date string
        if interval in ["15m", "60m"]:
            time_val = int(timestamp.timestamp())
        else:
            time_val = timestamp.strftime("%Y-%m-%d")
        
        data_point = {
            "time": time_val,
            "open": open_val,
            "high": high_val,
            "low": low_val,
            "close": close_val,
            "volume": volume_val
        }
        raw_data.append(data_point)
        formatted_data.append(data_point)
    
    # Calculate EMAs if there's enough data
    if len(raw_data) > 50:
        periods = list(map(int, ema_periods.split(',')))  # e.g. [9,14,50]
        ema9 = calculate_ema(raw_data, 9)
        ema14 = calculate_ema(raw_data, 14)
        ema50 = calculate_ema(raw_data, 50)
        
        for i in range(len(formatted_data)):
            if i >= 8:  # i >= 9 - 1
                formatted_data[i]['ema9'] = ema9[i]
            if i >= 13:  # i >= 14 - 1
                formatted_data[i]['ema14'] = ema14[i]
            if i >= 49:  # i >= 50 - 1
                formatted_data[i]['ema50'] = ema50[i]
    
    return formatted_data

@charts_app.get("/api/symbols", response_model=List[str])
def get_symbols():
    try:
        with open("symbols.txt") as f:
            symbols = [line.strip() for line in f if line.strip()]
    except Exception:
        symbols = []
    return symbols

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(charts_app, host="0.0.0.0", port=8000, reload=True)
