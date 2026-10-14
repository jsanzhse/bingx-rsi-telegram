# Escáner RSI de BingX → Telegram

Este proyecto escanea contratos perpetuos USDT de BingX y envía a Telegram los que tengan RSI(14) menor de 30 en 1H, 4H, 1D y 1W. Ordena cada grupo desde el RSI más bajo.

## Importante
- Solo lee datos públicos y envía mensajes. No opera ni necesita claves de BingX.
- Por prudencia, el ajuste inicial revisa los 80 contratos USDT con mayor volumen disponible. Puedes aumentar `MAX_SYMBOLS` en el archivo `.github/workflows/scan.yml`, pero revisar todos los contratos puede tardar más o activar límites de la API.
- GitHub Actions programa el escaneo aproximadamente cada 30 minutos; los trabajos programados pueden retrasarse.
- Las velas más recientes pueden estar abiertas; para una lectura estrictamente basada solo en velas cerradas, conviene validar el último dato según el comportamiento de la API de BingX.
- No publiques el token del bot en el repositorio ni lo pegues en chats.

## Configuración rápida
1. Crea un repositorio nuevo en GitHub.
2. Sube todos los archivos de esta carpeta, manteniendo `.github/workflows/scan.yml` en esa ruta exacta.
3. En el repositorio, entra en **Settings → Secrets and variables → Actions → New repository secret**.
4. Crea el secreto `TELEGRAM_BOT_TOKEN` con el token de BotFather.
5. Crea el secreto `TELEGRAM_CHAT_ID` con el identificador del chat donde iniciaste el bot.
6. Abre la pestaña **Actions**, selecciona **Escaner RSI BingX Telegram** y pulsa **Run workflow** para probarlo.
7. Revisa el registro de ejecución si falla. No compartas capturas que muestren secretos.

## Cómo averiguar el chat ID
Después de enviar `/start` a tu bot, puedes consultar `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` sustituyendo `<TU_TOKEN>` por tu token en tu propio navegador. Busca el valor `chat` → `id`. Esa URL contiene el token: no la compartas y luego cierra la pestaña. Si el bot ya recibió actualizaciones antiguas, envía un mensaje nuevo al bot y vuelve a consultar.

## Ajustes
- Umbral: variable `RSI_LIMIT` en el workflow.
- Número de contratos: variable `MAX_SYMBOLS` en el workflow.
- Frecuencia: línea `cron` del workflow.
