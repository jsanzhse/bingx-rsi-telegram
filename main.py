
import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# ==================================================
# CONFIGURACION
# ==================================================

BASE_URL = "https://open-api.bingx.com"

TIMEFRAMES = {
    "1H": "1h",
    "4H": "4h",
    "1D": "1d",
    "1W": "1w",
}

RSI_PERIOD = 14
LONG_LEVEL = 30
SHORT_LEVEL = 70
MAX_SYMBOLS = 80

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

session = requests.Session()
session.headers.update({"User-Agent": "BingX-RSI-Scanner/1.0"})


# ==================================================
# CONSULTAS A BINGX
# ==================================================

def get_json(path, params=None):
    url = BASE_URL + path
    response = session.get(url, params=params, timeout=20)
    response.raise_for_status()

    result = response.json()

    if str(result.get("code", "0")) != "0":
        raise RuntimeError(
            f"BingX API {result.get('code')}: "
            f"{result.get('msg', 'Error desconocido')}"
        )

    return result.get("data", [])


def get_symbols():
    data = get_json("/openApi/swap/v2/quote/contracts")

    if isinstance(data, dict):
        data = data.get("data", [])

    symbols = []

    for item in data:
        symbol = item.get("symbol", "")
        status = str(item.get("status", "1"))

        if not symbol.endswith("-USDT"):
            continue

        if status not in ("1", "TRADING", "ONLINE"):
            continue

        symbols.append(symbol)

    return symbols


def get_tickers():
    data = get_json("/openApi/swap/v2/quote/ticker")

    if isinstance(data, dict):
        data = data.get("data", [])

    tickers = {}

    for item in data:
        symbol = item.get("symbol")
        if not symbol:
            continue

        try:
            price = float(item.get("lastPrice", 0) or 0)
            volume = float(
                item.get("quoteVolume", 0)
                or item.get("volume", 0)
                or 0
            )

            if price > 0:
                tickers[symbol] = {
                    "price": price,
                    "volume": volume,
                }
        except (ValueError, TypeError):
            continue

    return tickers


def get_closes(symbol, interval):
    data = get_json(
        "/openApi/swap/v3/quote/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": 100,
        },
    )

    if isinstance(data, dict):
        data = data.get("data", [])

    candles = []

    for candle in data:
        try:
            if isinstance(candle, dict):
                close = float(candle["close"])
                timestamp = int(
                    candle.get("time", candle.get("timestamp", 0))
                )
            else:
                timestamp = int(candle[0])
                close = float(candle[4])

            candles.append((timestamp, close))

        except (ValueError, TypeError, KeyError, IndexError):
            continue

    candles.sort(key=lambda item: item[0])
    return [item[1] for item in candles]


# ==================================================
# CALCULO RSI (METODO WILDER)
# ==================================================

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None

    changes = [
        closes[i] - closes[i - 1]
        for i in range(1, len(closes))
    ]

    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]

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
        return 100.0 if avg_gain > 0 else 50.0

    relative_strength = avg_gain / avg_loss
    return 100 - (100 / (1 + relative_strength))


# ==================================================
# TELEGRAM
# ==================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Faltan los secretos TELEGRAM_BOT_TOKEN "
            "o TELEGRAM_CHAT_ID en GitHub."
        )

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    # Telegram limita el texto de cada mensaje.
    chunks = []
    while len(message) > 3900:
        split_at = message.rfind("\n", 0, 3900)
        if split_at < 1:
            split_at = 3900

        chunks.append(message[:split_at])
        message = message[split_at:].lstrip("\n")

    if message:
        chunks.append(message)

    for chunk in chunks:
        response = session.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
            },
            timeout=20,
        )
        response.raise_for_status()

        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(
                "Telegram rechazó el mensaje: "
                + str(result)
            )


# ==================================================
# FORMATO DE PRECIOS
# ==================================================

def format_price(price):
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    if price >= 0.01:
        return f"{price:.6f}"
    return f"{price:.8f}"


# ==================================================
# ESCANEO PRINCIPAL
# ==================================================

def main():
    print("Iniciando escáner RSI de BingX...")

    print("Obteniendo contratos...")
    symbols = get_symbols()

    print("Consultando precios y volumen...")
    tickers = get_tickers()

    # Priorizar contratos con mayor volumen disponible.
    symbols = [
        symbol for symbol in symbols
        if symbol in tickers
    ]

    symbols.sort(
        key=lambda symbol: tickers[symbol]["volume"],
        reverse=True,
    )

    symbols = symbols[:MAX_SYMBOLS]

    print(f"Contratos seleccionados: {len(symbols)}")

    found = {
        timeframe: {"LONG": [], "SHORT": []}
        for timeframe in TIMEFRAMES
    }

    errors = 0

    for label, interval in TIMEFRAMES.items():
        print(f"Analizando temporalidad {label}...")

        for index, symbol in enumerate(symbols, start=1):
            try:
                closes = get_closes(symbol, interval)
                value = calculate_rsi(closes, RSI_PERIOD)

                if value is None:
                    print(f"Sin suficientes velas: {symbol} {label}")
                    continue

                price = tickers[symbol]["price"]

                signal = None

                if value <= LONG_LEVEL:
                    signal = "LONG"
                elif value >= SHORT_LEVEL:
                    signal = "SHORT"

                if signal:
                    found[label][signal].append({
                        "symbol": symbol,
                        "rsi": value,
                        "price": price,
                    })

                if index % 20 == 0:
                    print(
                        f"{label}: revisados {index}/"
                        f"{len(symbols)} contratos"
                    )

            except Exception as error:
                errors += 1
                print(
                    f"Error en {symbol} {label}: {error}"
                )

            # Pausa corta para reducir solicitudes demasiado rápidas.
            time.sleep(0.05)

    stamp = datetime.now(
        ZoneInfo("America/Bogota")
    ).strftime("%Y-%m-%d %H:%M")

    lines = [
        "🔎 ESCÁNER RSI(14) — BINGX FUTUROS USDT",
        f"🕒 Hora de Bogotá: {stamp}",
        f"📊 Contratos analizados: {len(symbols)}",
        "📌 LONG: RSI ≤ 30 | SHORT: RSI ≥ 70",
        "",
        "⚠️ Alertas informativas; no son órdenes de trading.",
    ]

    total_signals = 0

    for timeframe in TIMEFRAMES:
        lines.append("")
        lines.append(f"━━━━━━━━ {timeframe} ━━━━━━━━")

        for signal, title in (
            ("LONG", "🟢 LONG — RSI bajo"),
            ("SHORT", "🔴 SHORT — RSI alto"),
        ):
            results = found[timeframe][signal]
            lines.append("")
            lines.append(f"{title}: {len(results)}")

            if not results:
                lines.append("Sin señales en este momento.")
                continue

            # Mostrar primero los RSI más extremos.
            if signal == "LONG":
                results.sort(key=lambda item: item["rsi"])
            else:
                results.sort(
                    key=lambda item: item["rsi"],
                    reverse=True,
                )

            for item in results:
                lines.append(
                    f"{item['symbol']} | "
                    f"RSI: {item['rsi']:.2f} | "
                    f"Precio: {format_price(item['price'])} USDT"
                )
                total_signals += 1

    lines.extend([
        "",
        f"📍 Total de señales encontradas: {total_signals}",
        f"⚠️ Errores de consulta: {errors}",
        "",
        "El RSI puede cambiar antes del cierre de la vela. "
        "Confirma la señal en BingX antes de operar.",
    ])

    report = "\n".join(lines)

    print(report)
    send_telegram(report)

    print("Reporte enviado a Telegram.")


if __name__ == "__main__":
    main()
    
