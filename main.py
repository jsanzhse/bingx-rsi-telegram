
import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# ============================================================
# CONFIGURACION
# ============================================================

BINGX_BASE_URL = "https://open-api.bingx.com"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

RSI_PERIOD = 14
MAX_SYMBOLS = 80
CANDLE_LIMIT = 100

# BingX: temporalidades y valores de intervalo
TIMEFRAMES = [
    ("1H", "1h"),
    ("4H", "4h"),
    ("1D", "1d"),
    ("1W", "1w"),
]

RSI_LONG = 30
RSI_SHORT = 70

# Pausa entre consultas de velas para reducir errores por limite.
KLINE_REQUEST_DELAY = 1.1

SESSION = requests.Session()


# ============================================================
# CONSULTAS A BINGX
# ============================================================

def bingx_get(endpoint, params=None, retries=3):
    """Consulta un endpoint publico de BingX con reintentos."""

    url = BINGX_BASE_URL + endpoint
    last_error = None

    for attempt in range(retries):
        try:
            response = SESSION.get(
                url,
                params=params or {},
                timeout=25
            )

            response.raise_for_status()
            result = response.json()

            if not isinstance(result, dict):
                return result

            code = result.get("code")

            if code is not None and str(code) != "0":
                message = result.get("msg", "Error desconocido")
                raise RuntimeError(
                    f"BingX codigo {code}: {message}"
                )

            if "data" not in result:
                raise RuntimeError(
                    f"BingX no devolvio el campo data: {result}"
                )

            return result["data"]

        except Exception as exc:
            last_error = exc

            if attempt < retries - 1:
                wait_seconds = 2 ** (attempt + 1)
                print(
                    f"Consulta fallida ({attempt + 1}/{retries}) "
                    f"en {endpoint}: {exc}. "
                    f"Reintentando en {wait_seconds} s."
                )
                time.sleep(wait_seconds)

    raise RuntimeError(
        f"Fallo despues de {retries} intentos: {last_error}"
    )


# ============================================================
# MERCADOS Y PRECIOS
# ============================================================

def get_market_tickers():
    data = bingx_get("/openApi/swap/v2/quote/ticker")

    if isinstance(data, dict):
        # Si BingX devuelve un unico ticker, lo convertimos en lista.
        if "symbol" in data:
            return [data]

        # Compatibilidad con respuestas envueltas.
        for key in ("list", "rows", "tickers", "data"):
            if isinstance(data.get(key), list):
                return data[key]

    if isinstance(data, list):
        return data

    raise RuntimeError(
        f"Formato de tickers no reconocido: {type(data).__name__}"
    )


def get_top_symbols():
    """Selecciona los contratos USDT con mayor volumen disponible."""

    tickers = get_market_tickers()
    markets = {}

    for ticker in tickers:
        if not isinstance(ticker, dict):
            continue

        symbol = str(ticker.get("symbol", "")).upper()

        if not symbol.endswith("-USDT"):
            continue

        try:
            price = float(ticker.get("lastPrice", 0) or 0)
        except (TypeError, ValueError):
            continue

        if price <= 0:
            continue

        volume = 0.0

        # Preferir volumen cotizado, si está disponible.
        for field in ("quoteVolume", "turnover", "volume"):
            try:
                value = float(ticker.get(field, 0) or 0)

                if value > 0:
                    volume = value
                    break
            except (TypeError, ValueError):
                continue

        markets[symbol] = {
            "symbol": symbol,
            "price": price,
            "volume": volume,
        }

    ordered = sorted(
        markets.values(),
        key=lambda item: item["volume"],
        reverse=True
    )

    return ordered[:MAX_SYMBOLS]


# ============================================================
# NOMBRES CORTOS PARA TELEGRAM
# ============================================================

def short_symbol(symbol):
    """Acorta nombres especiales solo para mostrarlos."""

    base = (
        symbol[:-5]
        if symbol.endswith("-USDT")
        else symbol
    )

    # Acciones tokenizadas: NCSKTSLA2USD -> TSLA
    if base.startswith("NCSK") and base.endswith("2USD"):
        return base[4:-4]

    # Indices tokenizados: NCSINASDAQ1002USD -> NASDAQ100
    if base.startswith("NCSI") and base.endswith("2USD"):
        return base[4:-4]

    # Materias primas: NCCO1OILBRENT2USD -> BRENT
    if base.startswith("NCCO") and base.endswith("2USD"):
        name = base[4:-4]

        if name.startswith("1OIL"):
            name = name[4:]
        elif name.startswith("010IL"):
            name = name[5:]

        return name

    # Pares especiales de divisas.
    if base.startswith("NCFX") and base.endswith("2GBP"):
        return base[4:-4] + "GBP"

    return base


# ============================================================
# CALCULO DEL RSI
# ============================================================

def calculate_rsi(closes, period=14):
    """Calcula RSI usando el suavizado de Wilder."""

    if len(closes) <= period:
        return None

    changes = [
        closes[i] - closes[i - 1]
        for i in range(1, len(closes))
    ]

    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0

    if avg_loss == 0:
        return 100.0

    if avg_gain == 0:
        return 0.0

    relative_strength = avg_gain / avg_loss

    return 100.0 - (
        100.0 / (1.0 + relative_strength)
    )


# ============================================================
# LECTURA DE VELAS
# ============================================================

def extract_candles(data):
    """Acepta respuestas con velas en listas o diccionarios."""

    if isinstance(data, dict):
        for key in ("klines", "list", "rows", "data"):
            if isinstance(data.get(key), list):
                return data[key]

        # Algunos formatos pueden devolver una sola vela.
        if any(
            key in data
            for key in ("close", "c", "closePrice")
        ):
            return [data]

        raise RuntimeError(
            "Respuesta de velas sin una lista reconocible. "
            f"Campos recibidos: {list(data.keys())}"
        )

    if isinstance(data, list):
        return data

    raise RuntimeError(
        "Formato de velas inesperado: "
        f"{type(data).__name__}"
    )


def extract_close(candle):
    """Extrae el cierre sin asumir un único formato."""

    if isinstance(candle, dict):
        for key in ("close", "c", "closePrice"):
            if candle.get(key) is not None:
                return float(candle[key])

        raise ValueError(
            "No se encontro el precio de cierre. "
            f"Campos: {list(candle.keys())}"
        )

    if isinstance(candle, (list, tuple)):
        if len(candle) > 4:
            return float(candle[4])

        raise ValueError(
            f"La vela tiene {len(candle)} campos; "
            "se esperaban al menos 5."
        )

    raise ValueError(
        f"Tipo de vela no reconocido: {type(candle).__name__}"
    )


def get_rsi(symbol, interval):
    """Consulta las velas de BingX y calcula su RSI."""

    data = bingx_get(
        "/openApi/swap/v3/quote/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": CANDLE_LIMIT,
        }
    )

    candles = extract_candles(data)

    if not candles:
        raise RuntimeError(
            f"BingX devolvio cero velas para {symbol} {interval}"
        )

    closes = []

    for candle in candles:
        close = extract_close(candle)

        if close > 0:
            closes.append(close)

    if len(closes) <= RSI_PERIOD:
        raise RuntimeError(
            f"Solo hay {len(closes)} cierres validos "
            f"para {symbol} {interval}"
        )

    return calculate_rsi(closes, RSI_PERIOD)


# ============================================================
# FORMATO DE PRECIOS
# ============================================================

def format_price(price):
    if price >= 1000:
        return f"{price:,.2f}"

    if price >= 1:
        return f"{price:,.4f}".rstrip("0").rstrip(".")

    return f"{price:.8f}".rstrip("0").rstrip(".")


# ============================================================
# ESCANEO DE MERCADOS
# ============================================================

def scan_markets():
    markets = get_top_symbols()

    if not markets:
        raise RuntimeError(
            "No se encontraron contratos USDT para analizar."
        )

    print(f"Mercados seleccionados: {len(markets)}")

    results = {
        label: {"LONG": [], "SHORT": []}
        for label, _ in TIMEFRAMES
    }

    errors = 0
    checked = 0
    total = len(markets) * len(TIMEFRAMES)

    for market in markets:
        symbol = market["symbol"]
        display_name = short_symbol(symbol)

        for timeframe_label, interval in TIMEFRAMES:
            checked += 1

            try:
                rsi = get_rsi(symbol, interval)

                if rsi is None:
                    print(
                        f"RSI no disponible: {symbol} "
                        f"{timeframe_label}"
                    )
                    continue

                item = {
                    "name": display_name,
                    "symbol": symbol,
                    "price": market["price"],
                    "rsi": rsi,
                }

                if rsi <= RSI_LONG:
                    results[timeframe_label]["LONG"].append(item)

                elif rsi >= RSI_SHORT:
                    results[timeframe_label]["SHORT"].append(item)

            except Exception as exc:
                errors += 1
                print(
                    f"Error en {symbol} {timeframe_label}: "
                    f"{type(exc).__name__}: {exc}"
                )

            # Respetar el limite de solicitudes de velas.
            time.sleep(KLINE_REQUEST_DELAY)

            if checked % 20 == 0 or checked == total:
                print(f"Progreso: {checked}/{total}")

    for timeframe_label, _ in TIMEFRAMES:
        results[timeframe_label]["LONG"].sort(
            key=lambda item: item["rsi"]
        )

        results[timeframe_label]["SHORT"].sort(
            key=lambda item: item["rsi"]
        )

    return results, len(markets), errors


# ============================================================
# CONSTRUIR INFORME
# ============================================================

def build_report(results, market_count, errors):
    now = datetime.now(ZoneInfo("America/Bogota"))
    timestamp = now.strftime("%d/%m/%Y %H:%M:%S")

    lines = [
        "📊 ESCANER RSI - BINGX",
        f"🕒 Hora Colombia: {timestamp}",
        f"📈 Mercados seleccionados: {market_count}",
        f"📐 RSI: periodo {RSI_PERIOD}",
        f"🟢 LONG: RSI <= {RSI_LONG}",
        f"🔴 SHORT: RSI >= {RSI_SHORT}",
        "",
        "Las señales del RSI no son ordenes de trading.",
    ]

    total_signals = 0

    for timeframe_label, _ in TIMEFRAMES:
        lines.extend([
            "",
            f"━━━━━━━━ {timeframe_label} ━━━━━━━━",
            f"🟢 LONG (RSI <= {RSI_LONG})",
        ])

        long_signals = results[timeframe_label]["LONG"]

        if not long_signals:
            lines.append("  Sin señales detectadas.")
        else:
            for item in long_signals:
                total_signals += 1
                lines.append(
                    f"  {item['name']} | RSI: {item['rsi']:.2f} | "
                    f"Precio: {format_price(item['price'])} USDT"
                )

        lines.append(f"🔴 SHORT (RSI >= {RSI_SHORT})")

        short_signals = results[timeframe_label]["SHORT"]

        if not short_signals:
            lines.append("  Sin señales detectadas.")
        else:
            for item in short_signals:
                total_signals += 1
                lines.append(
                    f"  {item['name']} | RSI: {item['rsi']:.2f} | "
                    f"Precio: {format_price(item['price'])} USDT"
                )

    lines.extend([
        "",
        f"🔎 Total de señales: {total_signals}",
        f"⚠️ Consultas con error: {errors}",
        "",
        "Verifica el grafico antes de operar.",
        "El RSI por si solo no confirma una entrada.",
    ])

    return "\n".join(lines)


# ============================================================
# ENVIO A TELEGRAM
# ============================================================

def split_message(message, max_length=3500):
    """Divide mensajes largos para respetar el limite de Telegram."""

    chunks = []
    current = ""

    for line in message.splitlines():
        candidate = current + ("\n" if current else "") + line

        if len(candidate) > max_length and current:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID "
            "en los secretos de GitHub."
        )

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    chunks = split_message(message)

    print("Preparando envio a Telegram...")

    for index, chunk in enumerate(chunks, start=1):
        response = SESSION.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
            },
            timeout=25,
        )

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(
                "Telegram rechazo el mensaje: "
                f"{result.get('description', 'Error desconocido')}"
            )

        print(
            f"Telegram confirmo el mensaje "
            f"{index}/{len(chunks)}."
        )

    print("Reporte enviado a Telegram correctamente.")


# ============================================================
# INICIO DEL PROGRAMA
# ============================================================

def main():
    print("=" * 50)
    print("INICIANDO ESCANER RSI DE BINGX")
    print("=" * 50)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Los secretos de Telegram no estan configurados."
        )

    print("Los dos secretos de Telegram estan presentes.")

    results, market_count, errors = scan_markets()

    report = build_report(
        results,
        market_count,
        errors
    )

    print("\n" + report + "\n")

    # Evitar presentar un informe de cero señales como confiable
    # si absolutamente todas las consultas fallaron.
    if errors == market_count * len(TIMEFRAMES):
        raise RuntimeError(
            "Todas las consultas de RSI fallaron. "
            "No se enviara un informe de señales como si fuera valido. "
            "Revisa los errores anteriores."
        )

    send_telegram(report)

    print("Proceso finalizado correctamente.")


if __name__ == "__main__":
    main()
