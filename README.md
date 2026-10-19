# Deptos monitor (local)

Monitor personal de alquileres en Argentina. Ejecuta las consultas desde tu Mac, permite probar búsquedas en el navegador, guarda filtros e historial en SQLite y puede enviar novedades por tu sidecar de WhatsApp Web.

No usa Supabase, Vercel, Cloudflare, AWS, GitHub Actions ni una base de datos remota. Es una instalación local de un único usuario.

## Requisitos

- Python 3.12 o superior.
- Acceso a Internet para consultar los portales y cargar el mapa.
- Opcional: el sidecar de WhatsApp Web que ya usás.

## Instalación

```bash
cd /Users/abettucci/depto-scraper
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --requirement requirements.txt
test -f .env || cp .env.example .env
mkdir -p logs data
```

Editá `.env` solamente si querés cambiar la ruta de la base, habilitar WhatsApp o usar la API oficial de Mercado Libre:

```dotenv
DEPTOS_DATABASE_PATH=data/depto-monitor.sqlite3
WHATSAPP_SIDECAR_URL=https://tu-sidecar.up.railway.app
WAWEB_SHARED_SECRET=tu-secreto-local
DEFAULT_WHATSAPP_CHAT_ID=54911XXXXXXXX
```

No publiques `.env` ni la base SQLite. Ambos ya están ignorados por Git.

## Usar la aplicación

Iniciá el servidor local:

```bash
cd /Users/abettucci/depto-scraper
source .venv/bin/activate
python -m depto_monitor.local_server
```

Abrí [http://127.0.0.1:8000](http://127.0.0.1:8000). La aplicación sólo escucha en tu computadora, sin login ni cuenta externa.

1. Elegí portales, localidades, precio, ambientes, metros y otros filtros.
2. Presioná **Buscar ahora** para ver los avisos y sus puntos en el mapa cuando el portal publica coordenadas.
3. Escribí un nombre y presioná **Guardar y monitorear**. Los filtros y el historial quedan en `data/depto-monitor.sqlite3`.

La búsqueda se ejecuta desde la misma Mac. Cada portal puede bloquear solicitudes, cambiar su página o no incluir todos los campos; el resultado muestra el estado por portal. El proyecto no intenta evadir bloqueos, CAPTCHAs ni restricciones de los sitios.

## Monitor y WhatsApp

Corré una vez el monitor manualmente para cargar el baseline:

```bash
cd /Users/abettucci/depto-scraper
source .venv/bin/activate
python -m depto_monitor.run
```

La primera corrida registra los avisos existentes sin notificar. Las siguientes registran avisos nuevos, cambios de precio y reapariciones. Si configuraste el sidecar y un número de chat, envían el resumen por WhatsApp.

Para ejecutarlo cada seis horas en macOS, abrí `crontab -e` y agregá:

```cron
17 */6 * * * cd /Users/abettucci/depto-scraper && /Users/abettucci/depto-scraper/.venv/bin/python -m depto_monitor.run >> /Users/abettucci/depto-scraper/logs/monitor.log 2>&1
```

Verificá el resultado con:

```bash
tail -f /Users/abettucci/depto-scraper/logs/monitor.log
```

Para detenerlo, eliminá esa línea de `crontab -e`.

## Datos y backup

- Búsquedas, avisos agrupados y snapshots: `data/depto-monitor.sqlite3`.
- Copia de seguridad manual: `cp data/depto-monitor.sqlite3 data/depto-monitor.backup.sqlite3`.
- Para empezar de cero, detené el servidor y borrá ese archivo.

## Tests

```bash
cd /Users/abettucci/depto-scraper
source .venv/bin/activate
python -m unittest discover -s tests
```
