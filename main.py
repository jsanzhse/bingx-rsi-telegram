import os
import time
import requests
from datetime import datetime, timezone

BASE = "https://open-api.bingx.com"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RSI_PERIOD = 14
RSI_LIMIT = float(os.getenv("RSI_LIMIT", "30"))
SHORT_RSI_LIMIT = float(os.getenv("SHORT_RSI_LIMIT", "70"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "80"))
TIMEFRAMES = [("1h", "1H"), ("4h", "4H"), ("1d", "1D"), ("1w", "1W")]
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "BingX-RSI-Telegram-Scanner/1.0"})

def get_json(path, params=None):
    response = SESSION.get(BASE + path, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    if data.get("code") not in (0, "0", None):
        raise RuntimeError(f"BingX respondió con código {data.get('code')}: {data.get('msg', '')}")
    return data

def get_symbols():
    data = get_json("/openApi/swap/v2/quote/contracts")
    rows = data.get("data", [])
    symbols = []
    for item in rows:
        symbol = item.get("symbol", "")
        status = str(item.get("status", "1")).lower()
        if symbol.endswith("-USDT") and status in ("1", "true", "trading", "online"):
            symbols.append(symbol)
    if not symbols:
        # Fallback: contracts endpoint may not include a status field in some responses.
        symbols = [x.get("symbol", "") for x in rows if x.get("symbol", "").endswith("-USDT")]
    return sorted(set(symbols))

def get_24h_tickers():
    try:
        data = get_json("/openApi/swap/v2/quote/ticker")
        rows = data.get("data", [])
        volume = {}
        for row in rows:
            symbol = row.get("symbol", "")
            if symbol.endswith("-USDT"):
                raw = row.get("quoteVolume", row.get("volume", 0))
                try:
                    volume[symbol] = float(raw or 0)
                except (TypeError, ValueError):
                    volume[symbol] = 0
        return volume
    except Exception:
        return {}

def candles(symbol, interval):
    data = get_json("/openApi/swap/v3/quote/klines",
                    {"symbol": symbol, "interval": interval, "limit": 100})
    rows = data.get("data", [])
    closes = []
    for row in rows:
        if isinstance(row, dict):
            value = row.get("close")
        elif isinstance(row, list) and len(row) > 4:
            value = row[4]
        else:
            continue
        try:
            closes.append(float(value))
        except (TypeError, ValueError):
            pass
    return closes

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 0.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Faltan los Secrets TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID.")
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    response = SESSION.post(url, json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True}, timeout=20)
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError("Telegram no aceptó el mensaje. Verifica el chat ID y el token en GitHub Secrets.")
def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Configura TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en GitHub Secrets antes de ejecutar.")
    symbols = get_symbols()
    volumes = get_24h_tickers()
    symbols.sort(key=lambda s: volumes.get(s, 0), reverse=True)
    symbols = symbols[:MAX_SYMBOLS]
    print(f"Escaneando {len(symbols)} contratos USDT de BingX; límite RSI < {RSI_LIMIT}.")
    found = {label: [] for _, label in TIMEFRAMES}
    errors = 0
    for interval, label in TIMEFRAMES:
        for symbol in symbols:
            try:
                closes = candles(symbol, interval)
                value = rsi(closes, RSI_PERIOD)
                if value is not None and value < RSI_LIMIT:
                    found[label].append((value, symbol, closes[-1]))
            except Exception as exc:
                errors += 1
                print(f"Error {symbol} {label}: {exc}")
            time.sleep(0.12)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sections = [f"🔎 BingX RSI(14) < {RSI_LIMIT}\nActualizado: {stamp}\nContratos revisados por temporalidad: {len(symbols)}"]
    total = 0
    for _, label in TIMEFRAMES:
        results = sorted(found[label], key=lambda x: x[0])
        total += len(results)
        sections.append(f"\n📊 {label} — {len(results)} señales")
        if not results:
            sections.append("Sin contratos por debajo del umbral.")
        else:
            for value, symbol, price in results:
                sections.append(f"{symbol} | RSI {value:.2f} | Precio {price:g}")
    sections.append(f"\nNota: señales técnicas, no recomendaciones de compra/venta. Errores de consulta: {errors}.")
    message = "\n".join(sections)
    # Telegram message limit is 4096 chars; split at line boundaries.
    chunks, current = [], ""
    for line in message.splitlines():
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)
    for chunk in chunks:
        send_telegram(chunk)
    print(f"Finalizado. Señales encontradas: {total}; errores: {errors}")
    

if __name__ == "__main__":
    main()
