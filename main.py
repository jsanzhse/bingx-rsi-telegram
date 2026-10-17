
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

TIMEFRAMES = [
    ("1H", "1h"),
    ("4H", "4h"),
    ("1D", "1d"),
    ("1W", "1w"),
]

RSI_LONG = 30
RSI_SHORT = 70

SESSION = requests.Session()


# ============================================================
# SOLICITUDES A BINGX
# ============================================================

def bingx_get(endpoint, params=None):
    """Consulta un endpoint publico de BingX."""

    url = BINGX_BASE_URL + endpoint

    response = SESSION.get(
        url,
        params=params or {},
        timeout=20
    )

    response.raise_for_status()
    result = response.json()

    if isinstance(result, dict):
        code = result.get("code")

        if code is not None and str(code) != "0":
            raise RuntimeError(
                f"BingX devolvio codigo {code}: "
                f"{result.get('msg', 'Error desconocido')}"
            )

        return result.get("data", [])

    return result


# ============================================================
# OBTENER MERCADOS Y PRECIOS
# ============================================================

def get_market_tickers():
    """Obtiene los tickers de futuros perpetuos disponibles."""

    data = bingx_get("/openApi/swap/v2/quote/ticker")

    if isinstance(data, dict):
        data = [data]

    if not isinstance(data, list):
        raise RuntimeError("BingX no devolvio una lista de tickers.")

    return data


def get_top_symbols():
    """
    Selecciona hasta 80 contratos USDT.
    Prioriza los de mayor volumen negociado.
    """

    tickers = get_market_tickers()
    markets = {}

    for ticker in tickers:
        symbol = str(ticker.get("symbol", "")).upper()

        # Solo contratos cotizados en USDT.
        if not symbol.endswith("-USDT"):
            continue

        # Excluir contratos sin precio valido.
        try:
            price = float(ticker.get("lastPrice", 0))
        except (TypeError, ValueError):
            continue

        if price <= 0:
            continue

        # Preferir volumen cotizado en USDT cuando exista.
        volume = 0.0

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
# NOMBRES CORTOS PARA MOSTRAR EN TELEGRAM
# ============================================================

def short_symbol(symbol):
    """
    Acorta los nombres especiales solo para mostrarlos.
    El simbolo original se conserva para consultar BingX.
    """

    base = (
        symbol[:-5]
        if symbol.endswith("-USDT")
        else symbol
    )

    # Acciones e indices tokenizados.
    if base.startswith("NCSI") and base.endswith("2USD"):
        return base[4:-4]

    # Materias primas: Brent y WTI, entre otras.
    if base.startswith("NCC010IL") and base.endswith("2USD"):
        return base[len("NCC010IL"):-4]

    # Pares especiales de divisas.
    if base.startswith("NCFX") and base.endswith("2GBP"):
        return base[4:-4] + "GBP"

    # Acciones tokenizadas identificadas anteriormente.
    if base.startswith("NCSK") and base.endswith("2USD"):
        return base[4:-4]

    return base


# ============================================================
# CALCULO DEL RSI
# ============================================================

def calculate_rsi(closes, period=14):
    """Calcula el RSI usando el suavizado de Wilder."""

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

    return 100.0 - (100.0 / (1.0 + relative_strength))


def get_rsi(symbol, interval):
    """Descarga velas y calcula el RSI de un contrato."""

    data = bingx_get(
        "/openApi/swap/v3/quote/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": CANDLE_LIMIT,
        }
    )

    if not isinstance(data, list) or not data:
        raise RuntimeError("BingX no devolvio velas.")

    closes = []

    for candle in data:
        try:
            # En la respuesta habitual de BingX,
            # el cierre esta en la posicion 4.
            close = float(candle[4])
            if close > 0:
                closes.append(close)
        except (TypeError, ValueError, IndexError):
            continue

    return calculate_rsi(closes, RSI_PERIOD)


# ============================================================
# FORMATO DE PRECIOS
# ============================================================

def format_price(price):
    """Evita decimales innecesarios en precios altos."""

    if price >= 1000:
        return f"{price:,.2f}"

    if price >= 1:
        return f"{price:,.4f}".rstrip("0").rstrip(".")

    return f"{price:.8f}".rstrip("0").rstrip(".")


# ============================================================
# ESCANER RSI
# ============================================================

def scan_markets():
    """Analiza los mercados en las cuatro temporalidades."""

    markets = get_top_symbols()

    if not markets:
        raise RuntimeError(
            "No se encontraron contratos USDT para analizar."
        )

    print(f"Mercados seleccionados: {len(markets)}")

    results = {
        "1H": {"LONG": [], "SHORT": []},
        "4H": {"LONG": [], "SHORT": []},
        "1D": {"LONG": [], "SHORT": []},
        "1W": {"LONG": [], "SHORT": []},
    }

    errors = 0
    checked = 0
    total = len(markets) * len(TIMEFRAMES)

    for market in markets:
        symbol = market["symbol"]
        price = market["price"]
        display_name = short_symbol(symbol)

        for timeframe_label, interval in TIMEFRAMES:
            checked += 1

            try:
                rsi = get_rsi(symbol, interval)

                if rsi is None:
                    print(
                        f"Sin suficientes velas: "
                        f"{symbol} {timeframe_label}"
                    )
                    continue

                item = {
                    "name": display_name,
                    "symbol": symbol,
                    "price": price,
                    "rsi": rsi,
                }

                if rsi <= RSI_LONG:
                    results[timeframe_label]["LONG"].append(item)

                elif rsi >= RSI_SHORT:
                    results[timeframe_label]["SHORT"].append(item)

            except Exception as exc:
                errors += 1
                print(
                    f"Error en {symbol} {timeframe_label}: {exc}"
                )

            # Pausa breve para espaciar las solicitudes.
            time.sleep(0.05)

        if checked % 20 == 0 or checked == total:
            print(f"Progreso: {checked}/{total}")

    # Ordenar de menor a mayor RSI dentro de cada grupo.
    for timeframe_label in results:
        results[timeframe_label]["LONG"].sort(
            key=lambda item: item["rsi"]
        )

        results[timeframe_label]["SHORT"].sort(
            key=lambda item: item["rsi"]
        )

    return results, len(markets), errors


# ============================================================
# CONSTRUIR INFORME PARA TELEGRAM
# ============================================================

def build_report(results, market_count, errors):
    now = datetime.now(ZoneInfo("America/Bogota"))
    timestamp = now.strftime("%d/%m/%Y %H:%M:%S")

    lines = [
        "📊 ESCANER RSI - BINGX",
        f"🕒 Hora Colombia: {timestamp}",
        f"📈 Mercados analizados: {market_count}",
        f"📐 RSI: periodo {RSI_PERIOD}",
        f"🟢 LONG: RSI <= {RSI_LONG}",
        f"🔴 SHORT: RSI >= {RSI_SHORT}",
        "",
        "Los resultados son señales del RSI, no ordenes.",
    ]

    total_signals = 0

    for timeframe_label, _ in TIMEFRAMES:
        lines.append("")
        lines.append(f"━━━━━━━━ {timeframe_label} ━━━━━━━━")

        for direction in ("LONG", "SHORT"):
            signals = results[timeframe_label][direction]

            if direction == "LONG":
                lines.append(f"🟢 LONG (RSI <= {RSI_LONG})")
            else:
                lines.append(f"🔴 SHORT (RSI >= {RSI_SHORT})")

            if not signals:
                lines.append("  Sin señales en este momento.")
                continue

            total_signals += len(signals)

            for item in signals:
                lines.append(
                    f"  {item['name']} | "
                    f"RSI: {item['rsi']:.2f} | "
                    f"Precio: {format_price(item['price'])} USDT"
                )

    lines.extend([
        "",
        f"🔎 Total de señales: {total_signals}",
        f"⚠️ Consultas con error: {errors}",
        "",
        "Aviso: verifica el grafico antes de operar.",
        "El RSI por si solo no confirma una entrada.",
    ])

    return "\n".join(lines)


# ============================================================
# ENVIAR MENSAJES A TELEGRAM
# ============================================================

def split_message(message, max_length=3500):
    """Divide informes largos sin cortar las lineas."""

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
    """Envia el informe y verifica la respuesta de Telegram."""

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Faltan los secretos TELEGRAM_BOT_TOKEN "
            "o TELEGRAM_CHAT_ID en GitHub."
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
            timeout=20,
        )

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(
                "Telegram no acepto el mensaje: "
                f"{result.get('description', 'Error desconocido')}"
            )

        print(
            f"Telegram confirmo el mensaje "
            f"{index}/{len(chunks)}."
        )

    print("Reporte enviado a Telegram correctamente.")


# ============================================================
# EJECUCION PRINCIPAL
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

    send_telegram(report)

    print("Proceso finalizado correctamente.")


if __name__ == "__main__":
    main()
    
