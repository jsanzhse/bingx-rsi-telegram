
import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# ============================================================
# CONFIGURACIÓN
# ============================================================

BINGX_BASE_URL = "https://open-api.bingx.com"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

RSI_PERIOD = 14
CANDLE_LIMIT = 100
MAX_MARKETS = 80

OVERSOLD_LEVEL = 30
OVERBOUGHT_LEVEL = 70

# Pausa entre consultas para reducir errores por límite de solicitudes.
KLINE_REQUEST_DELAY = 1.1

TIMEFRAMES = [
    ("1H", "1h"),
    ("4H", "4h"),
    ("1D", "1d"),
    ("1W", "1w"),
]

REQUEST_TIMEOUT = 25
MAX_RETRIES = 4

session = requests.Session()


# ============================================================
# HORA Y FORMATO
# ============================================================

def bogota_time():
    return datetime.now(ZoneInfo("America/Bogota")).strftime(
        "%d/%m/%Y %H:%M:%S"
    )


def short_symbol(symbol):
    """Acorta el nombre del par para que el reporte sea más legible."""
    base = symbol[:-5] if symbol.endswith("-USDT") else symbol

    if base.startswith("NCSK") and base.endswith("2USD"):
        return base[4:-4]

    if base.startswith("NCSI") and base.endswith("2USD"):
        return base[4:-4]

    if base.startswith("NCCO") and base.endswith("2USD"):
        name = base[4:-4]

        if name.startswith("1OIL"):
            name = name[4:]
        elif name.startswith("010IL"):
            name = name[5:]

        return name

        # Pares de divisas NCFX
    if base.startswith("NCFX") and base.endswith("2USD"):
        name = base[4:-4]

        # Ejemplo: NCFXEURSGD2USD -> EURSGD
        return name

    if base.startswith("NCFX") and base.endswith("2GBP"):
        return base[4:-4] + "GBP"

    return base


def format_price(price):
    if price is None:
        return "N/D"

    try:
        value = float(price)

        if value >= 1000:
            return f"{value:,.2f}"

        if value >= 1:
            return f"{value:.4f}".rstrip("0").rstrip(".")

        return f"{value:.8f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return "N/D"


# ============================================================
# SOLICITUDES A BINGX
# ============================================================

def bingx_get(endpoint, params=None):
    """Consulta BingX con reintentos y manejo de errores."""
    url = BINGX_BASE_URL + endpoint
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(
                url,
                params=params or {},
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()
            payload = response.json()

            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"Respuesta inesperada de BingX: {type(payload).__name__}"
                )

            code = payload.get("code")

            if code not in (None, 0, "0", 200, "200"):
                message = payload.get(
                    "msg",
                    payload.get("message", "Error desconocido"),
                )
                raise RuntimeError(
                    f"BingX devolvió código {code}: {message}"
                )

            return payload

        except Exception as exc:
            last_error = exc

            if attempt < MAX_RETRIES:
                wait_seconds = min(2 ** attempt, 15)
                print(
                    f"Error BingX ({attempt}/{MAX_RETRIES}) en "
                    f"{endpoint}: {exc}. Reintento en {wait_seconds}s."
                )
                time.sleep(wait_seconds)

    raise RuntimeError(
        f"Falló la consulta a BingX después de {MAX_RETRIES} intentos: "
        f"{last_error}"
    )


# ============================================================
# MERCADOS Y VOLUMEN
# ============================================================

def extract_list(data):
    """Extrae una lista de una respuesta API con distintas estructuras."""
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("data", "result", "rows", "list", "tickers"):
            value = data.get(key)

            if isinstance(value, list):
                return value

            if isinstance(value, dict):
                nested = extract_list(value)
                if nested:
                    return nested

    return []


def get_markets():
    """
    Obtiene contratos USDT y prioriza los pares con mayor volumen.
    Devuelve una lista de pares y un diccionario de precios.
    """
    payload = bingx_get("/openApi/swap/v2/quote/ticker")
    tickers = extract_list(payload.get("data", payload))

    if not tickers:
        raise RuntimeError(
            "BingX no devolvió una lista de mercados en la respuesta de ticker."
        )

    markets = []

    for ticker in tickers:
        if not isinstance(ticker, dict):
            continue

        symbol = ticker.get("symbol", "")

        # Solo futuros cotizados en USDT.
        if not isinstance(symbol, str) or not symbol.endswith("-USDT"):
            continue

        try:
            price = float(
                ticker.get("lastPrice")
                or ticker.get("last_price")
                or ticker.get("price")
                or 0
            )

            volume = float(
                ticker.get("quoteVolume")
                or ticker.get("turnover")
                or ticker.get("quote_volume")
                or 0
            )

            if volume <= 0:
                volume = float(
                    ticker.get("volume")
                    or ticker.get("vol")
                    or 0
                )

        except (ValueError, TypeError):
            continue

        markets.append({
            "symbol": symbol,
            "price": price,
            "volume": volume,
        })

    # Evita pares duplicados.
    unique = {}

    for market in markets:
        unique[market["symbol"]] = market

    markets = list(unique.values())

    # Los 80 pares de mayor volumen primero.
    markets.sort(key=lambda item: item["volume"], reverse=True)
    markets = markets[:MAX_MARKETS]

    if not markets:
        raise RuntimeError(
            "No se encontraron contratos perpetuos USDT para analizar."
        )

    prices = {
        market["symbol"]: market["price"]
        for market in markets
    }

    print(f"Mercados seleccionados: {len(markets)}")
    return [market["symbol"] for market in markets], prices


# ============================================================
# VELAS Y CÁLCULO DEL RSI
# ============================================================

def extract_candles(data):
    """Admite velas en listas o dentro de diccionarios."""
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("data", "result", "rows", "list", "klines"):
            value = data.get(key)

            if isinstance(value, list):
                return value

            if isinstance(value, dict):
                candles = extract_candles(value)
                if candles:
                    return candles

    return []


def extract_close(candle):
    """Extrae el precio de cierre de una vela."""
    if isinstance(candle, dict):
        for key in ("close", "c", "closePrice", "close_price"):
            value = candle.get(key)

            if value is not None:
                close = float(value)

                if close > 0:
                    return close

        raise ValueError(
            "No se encontró el precio de cierre en la vela."
        )

    if isinstance(candle, (list, tuple)) and len(candle) > 4:
        close = float(candle[4])

        if close > 0:
            return close

    raise ValueError("Formato de vela no reconocido.")


def get_open_time(candle):
    """Obtiene el timestamp para ordenar las velas cronológicamente."""
    if isinstance(candle, dict):
        for key in ("time", "openTime", "open_time", "t"):
            value = candle.get(key)

            if value is not None:
                return int(value)

        raise ValueError(
            "No se encontró el timestamp de apertura de la vela."
        )

    if isinstance(candle, (list, tuple)) and len(candle) > 0:
        return int(candle[0])

    raise ValueError("No se pudo obtener la hora de la vela.")


def calculate_rsi(closes, period=14):
    """Calcula RSI usando el suavizado de Wilder."""
    if len(closes) <= period:
        raise ValueError(
            f"Se necesitan más de {period} precios de cierre."
        )

    gains = []
    losses = []

    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]

        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        average_gain = (
            average_gain * (period - 1) + gains[i]
        ) / period

        average_loss = (
            average_loss * (period - 1) + losses[i]
        ) / period

    if average_loss == 0:
        if average_gain == 0:
            return 50.0
        return 100.0

    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def get_rsi(symbol, interval):
    """Descarga velas, las ordena y calcula el RSI más reciente."""
    payload = bingx_get(
        "/openApi/swap/v3/quote/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": CANDLE_LIMIT,
        },
    )

    candles = extract_candles(payload.get("data", payload))

    if not candles:
        raise RuntimeError(
            f"BingX no devolvió velas para {symbol} {interval}."
        )

    # Orden cronológico: de la vela más antigua a la más reciente.
    candles = sorted(candles, key=get_open_time)

    closes = []

    for candle in candles:
        close = extract_close(candle)

        if close > 0:
            closes.append(close)

    if len(closes) <= RSI_PERIOD:
        raise RuntimeError(
            f"Velas insuficientes para {symbol} {interval}: "
            f"{len(closes)} cierres."
        )

    rsi = calculate_rsi(closes, RSI_PERIOD)

    print(f"RSI comprobado: {symbol} {interval} = {rsi:.2f}")
    return rsi


# ============================================================
# ESCÁNER DE SOBREVENTA Y SOBRECOMPRA
# ============================================================

def scan_markets():
    symbols, prices = get_markets()

    results = {
        label: {
            "LONG": [],
            "SHORT": [],
        }
        for label, _ in TIMEFRAMES
    }

    total_requests = 0
    failed_requests = 0

    for timeframe_label, interval in TIMEFRAMES:
        print(f"\nAnalizando temporalidad {timeframe_label}...")

        for symbol in symbols:
            total_requests += 1

            try:
                rsi = get_rsi(symbol, interval)

                signal = {
                    "symbol": symbol,
                    "rsi": rsi,
                    "price": prices.get(symbol),
                }

                if rsi <= OVERSOLD_LEVEL:
                    results[timeframe_label]["LONG"].append(signal)

                elif rsi >= OVERBOUGHT_LEVEL:
                    results[timeframe_label]["SHORT"].append(signal)

            except Exception as exc:
                failed_requests += 1
                print(
                    f"Error en {symbol} {timeframe_label}: {exc}"
                )

            # Pausa entre solicitudes de velas.
            time.sleep(KLINE_REQUEST_DELAY)

    # No enviar un reporte vacío engañoso si todas las consultas fallaron.
    successful_requests = total_requests - failed_requests

    if successful_requests == 0:
        raise RuntimeError(
            f"Fallaron las {total_requests} consultas de RSI. "
            "No se enviará un reporte que pueda inducir a error."
        )

    # ========================================================
    # ORDEN DE LAS SEÑALES
    # ========================================================

    for timeframe_label, _ in TIMEFRAMES:
        # LONG: sobreventa, de MENOR a MAYOR RSI.
        results[timeframe_label]["LONG"].sort(
            key=lambda item: item["rsi"]
        )

        # SHORT: sobrecompra, de MAYOR a MENOR RSI.
        results[timeframe_label]["SHORT"].sort(
            key=lambda item: item["rsi"],
            reverse=True,
        )

    print(
        f"\nAnálisis terminado. Consultas: {total_requests}; "
        f"correctas: {successful_requests}; errores: {failed_requests}."
    )

    return results, total_requests, failed_requests


# ============================================================
# REPORTE PARA TELEGRAM
# ============================================================

def format_signal(signal):
    symbol = short_symbol(signal["symbol"])
    rsi = signal["rsi"]
    price = format_price(signal.get("price"))

    return f"• {symbol} | RSI: {rsi:.2f} | Precio: {price}"


def build_report(results, total_requests, failed_requests):
    lines = [
        "📊 ESCÁNER RSI — BINGX",
        f"🕒 Hora Bogotá: {bogota_time()}",
        f"📈 RSI: {RSI_PERIOD} periodos",
        f"🔎 Pares analizados: hasta {MAX_MARKETS}",
        "",
        "LONG = SOBREVENTA (RSI ≤ 30)",
        "Orden: RSI menor a mayor",
        "SHORT = SOBRECOMPRA (RSI ≥ 70)",
        "Orden: RSI mayor a menor",
        "",
    ]

    for timeframe_label, _ in TIMEFRAMES:
        long_signals = results[timeframe_label]["LONG"]
        short_signals = results[timeframe_label]["SHORT"]

        lines.append("=" * 25)
        lines.append(f"⏱️ TEMPORALIDAD {timeframe_label}")
        lines.append("=" * 25)

        lines.append("")
        lines.append(
            f"🟢 LONG — SOBREVENTA ({len(long_signals)})"
        )

        if long_signals:
            for signal in long_signals:
                lines.append(format_signal(signal))
        else:
            lines.append("Sin pares en sobreventa.")

        lines.append("")
        lines.append(
            f"🔴 SHORT — SOBRECOMPRA ({len(short_signals)})"
        )

        if short_signals:
            for signal in short_signals:
                lines.append(format_signal(signal))
        else:
            lines.append("Sin pares en sobrecompra.")

        lines.append("")

    successful_requests = total_requests - failed_requests

    lines.extend([
        "-" * 25,
        f"Consultas correctas: {successful_requests}/{total_requests}",
        f"Consultas con error: {failed_requests}",
        "",
        "Aviso: el RSI es un indicador técnico, no una garantía "
        "de que el precio vaya a revertir.",
    ])

    return "\n".join(lines)


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID "
            "en los secretos del repositorio."
        )

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    # Telegram limita el tamaño de los mensajes.
    max_length = 3900
    chunks = []

    while len(message) > max_length:
        split_at = message.rfind("\n", 0, max_length)

        if split_at <= 0:
            split_at = max_length

        chunks.append(message[:split_at])
        message = message[split_at:].lstrip("\n")

    if message:
        chunks.append(message)

    for index, chunk in enumerate(chunks, start=1):
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(
                f"Telegram rechazó el mensaje {index}: {result}"
            )

        print(
            f"Telegram confirmó el mensaje {index}/{len(chunks)}."
        )

    print("Reporte enviado a Telegram correctamente.")


# ============================================================
# EJECUCIÓN PRINCIPAL
# ============================================================

def main():
    print("=" * 50)
    print("INICIANDO ESCÁNER RSI DE BINGX")
    print(f"Hora Bogotá: {bogota_time()}")
    print("=" * 50)

    results, total_requests, failed_requests = scan_markets()

    report = build_report(
        results,
        total_requests,
        failed_requests,
    )

    print("\n" + report)
    send_telegram(report)


if __name__ == "__main__":
    main()
            
