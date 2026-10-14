
import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = "https://open-api.bingx.com"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

RSI_PERIOD = 14
RSI_LIMIT = float(os.getenv("RSI_LIMIT", "30"))
SHORT_RSI_LIMIT = float(os.getenv("SHORT_RSI_LIMIT", "70"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "80"))

TIMEFRAMES = [
    ("1h", "1H"),
    ("4h", "4H"),
    ("1d", "1D"),
    ("1w", "1W"),
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "BingX-RSI-Telegram-Scanner/2.0"
})


def get_json(path, params=None):
    response = SESSION.get(
        BASE + path,
        params=params,
        timeout=25
    )
    response.raise_for_status()

    data = response.json()

    if data.get("code") not in (0, "0", None):
        raise RuntimeError(
            f"BingX API {data.get('code')}: "
            f"{data.get('msg', '')}"
        )

    return data


def get_symbols():
    data = get_json(
        "/openApi/swap/v2/quote/contracts"
    )

    rows = data.get("data", [])
    symbols = []

    for item in rows:
        symbol = item.get("symbol", "")
        status = str(item.get("status", "1")).lower()

        if (
            symbol.endswith("-USDT")
            and status in ("1", "true", "trading", "online")
        ):
            symbols.append(symbol)

    if not symbols:
        symbols = [
            item.get("symbol", "")
            for item in rows
            if item.get("symbol", "").endswith("-USDT")
        ]

    if not symbols:
        raise RuntimeError(
            "BingX no devolvió contratos perpetuos USDT."
        )

    return sorted(set(symbols))


def get_24h_tickers():
    data = get_json(
        "/openApi/swap/v2/quote/ticker"
    )

    rows = data.get("data", [])

    if isinstance(rows, dict):
        rows = [rows]

    volumes = {}

    for row in rows:
        symbol = row.get("symbol", "")

        if not symbol.endswith("-USDT"):
            continue

        try:
            volume = float(
                row.get("quoteVolume", 0) or 0
            )
            volumes[symbol] = volume
        except (TypeError, ValueError):
            continue

    return volumes


def get_last_prices():
    data = get_json(
        "/openApi/swap/v2/quote/ticker"
    )

    rows = data.get("data", [])

    if isinstance(rows, dict):
        rows = [rows]

    prices = {}

    for row in rows:
        symbol = row.get("symbol", "")

        try:
            price = float(
                row.get("lastPrice", 0) or 0
            )

            if symbol and price > 0:
                prices[symbol] = price

        except (TypeError, ValueError):
            continue

    return prices


def candles(symbol, interval):
    data = get_json(
        "/openApi/swap/v3/quote/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": 100,
        }
    )

    rows = data.get("data", [])
    parsed = []

    for row in rows:
        try:
            if isinstance(row, dict):
                close = float(row["close"])
                open_time = int(
                    row.get("time", row.get("openTime", 0))
                    or 0
                )
            elif isinstance(row, list) and len(row) > 4:
                open_time = int(row[0])
                close = float(row[4])
            else:
                continue

            parsed.append((open_time, close))

        except (TypeError, ValueError, KeyError):
            continue

    # Orden cronológico para calcular correctamente el RSI.
    parsed.sort(key=lambda item: item[0])

    return [close for _, close in parsed]


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
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID "
            "en GitHub Secrets."
        )

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    response = SESSION.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=25,
    )

    response.raise_for_status()
    body = response.json()

    if not body.get("ok"):
        raise RuntimeError(
            "Telegram rechazó el mensaje. "
            "Revisa los Secrets y el chat ID."
        )


def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Configura TELEGRAM_BOT_TOKEN y "
            "TELEGRAM_CHAT_ID en GitHub Secrets."
        )

    print("Obteniendo contratos de BingX...")

    symbols = get_symbols()

    try:
        volumes = get_24h_tickers()
        if volumes:
            symbols.sort(
                key=lambda symbol: volumes.get(symbol, 0),
                reverse=True,
            )
    except Exception as exc:
        print(f"Aviso: no se pudo ordenar por volumen: {exc}")

    symbols = symbols[:MAX_SYMBOLS]

    print(f"Contratos seleccionados: {len(symbols)}")
    print("Consultando precios actuales...")

    prices = get_last_prices()

    found = {
        label: {"LONG": [], "SHORT": []}
        for _, label in TIMEFRAMES
    }

    errors = 0

    for interval, label in TIMEFRAMES:
        print(f"Analizando temporalidad {label}...")

        for symbol in symbols:
            try:
                closes = candles(symbol, interval)
                value = rsi(closes, RSI_PERIOD)

                if value is None:
                    continue

                # RSI calculado con las velas de BingX.
                # El precio mostrado se obtiene por separado
                # del ticker, no del cierre de la vela.

                if value <= RSI_LIMIT:
                    found[label]["LONG"].append(
                        (value, symbol)
                    )

                elif value >= SHORT_RSI_LIMIT:
                    found[label]["SHORT"].append(
                        (value, symbol)
                    )

            except Exception as exc:
                errors += 1
                print(
                    f"Error en {symbol} {label}: {exc}"
                )

            time.sleep(0.12)

    stamp = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    sections = [
        "🔎 ESCÁNER RSI(14) — BINGX FUTUROS USDT",
        f"Actualizado: {stamp}",
        f"Contratos seleccionados: {len(symbols)}",
        f"🟢 LONG: RSI <= {RSI_LIMIT}",
        f"🔴 SHORT: RSI >= {SHORT_RSI_LIMIT}",
        "Precio: último precio negociado consultado a BingX.",
    ]

    total_long = 0
    total_short = 0

    for _, label in TIMEFRAMES:
        sections.append(f"\n📊 TEMPORALIDAD {label}")

        for direction in ("LONG", "SHORT"):
            results = found[label][direction]

            results.sort(
                key=lambda item: item[0],
                reverse=(direction == "SHORT"),
            )

            sections.append(
                f"\n{'🟢' if direction == 'LONG' else '🔴'} "
                f"{direction} — {len(results)} señales"
            )

            if direction == "LONG":
                total_long += len(results)
            else:
                total_short += len(results)

            if not results:
                sections.append(
                    "Sin señales en este momento."
                )
                continue

            for value, symbol in results:
                live_price = prices.get(symbol)

                if live_price is not None:
                    price_text = f"Último precio {live_price:g}"
                else:
                    price_text = "Último precio no disponible"

                sections.append(
                    f"{symbol} | RSI {value:.2f} | "
                    f"{price_text}"
                )

    sections.append(
        "\nNota: señales técnicas, no recomendaciones "
        "de compra o venta. El RSI no garantiza movimientos."
    )

    sections.append(
        f"Errores de consulta de velas: {errors}."
    )

    message = "\n".join(sections)

    # Dividir los mensajes para respetar el límite de Telegram.
    chunks = []
    current = ""

    for line in message.splitlines():
        if len(current) + len(line) + 1 > 3800:
            if current:
                chunks.append(current)
            current = ""

        current += line + "\n"

    if current:
        chunks.append(current)

    for chunk in chunks:
        send_telegram(chunk)

    print(
        f"Escaneo finalizado. LONG: {total_long}; "
        f"SHORT: {total_short}; errores: {errors}"
    )


if __name__ == "__main__":
    main()
