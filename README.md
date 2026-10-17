# Deptos monitor

Monitor personal de alquileres argentinos: guardá búsquedas, recopilá avisos de Argenprop, Zonaprop, Mercado Libre y RE/MAX cada seis horas, deduplicá la misma propiedad entre portales y recibí un resumen por WhatsApp.

## Arquitectura

- `web/`: frontend estático para Cloudflare Pages o Vercel, con Supabase Auth (Google y email/contraseña).
- `api/preview.py`: función de Vercel que arma una consulta por portal desde localidad y filtros, ejecuta la búsqueda en vivo y devuelve los avisos agrupados.
- `supabase/schema.sql`: búsquedas, avisos agrupados, fuentes y snapshots históricos con RLS.
- `depto_monitor/`: job Python que procesa búsquedas, valida destinos, extrae JSON-LD público, conserva historial y llama al sidecar de WhatsApp Web.
- `.github/workflows/depto-monitor.yml`: ejecución programada cada seis horas.

El formulario construye las consultas a Argenprop, Zonaprop, Mercado Libre y RE/MAX desde localidad, operación, presupuesto, ambientes, metros, expensas, mascotas, cochera y un polígono dibujado en el mapa. No hace falta conocer las URLs de los portales. Los filtros y fuentes generadas quedan persistidos para la corrida programada.

## Configuración inicial

1. Creá un proyecto Supabase, ejecutá `supabase/schema.sql` en SQL Editor y habilitá Email + Google en Auth.
2. Completá `web/config.js` con la URL y publishable key de Supabase. No publiques la service role key.
3. Para buscar en vivo, importá el repositorio en Vercel con el directorio raíz del repositorio. Vercel sirve la web y crea `POST /api/preview` automáticamente; no requiere secretos para probar filtros.
4. Copiá `.env.example` a `.env` para probar localmente. En GitHub, definí los secretos indicados en el workflow.
5. Configurá el sidecar existente en Railway. Se usa `POST /send` con `WAWEB_SHARED_SECRET`, igual que `ticket_splitter`.
6. Creá búsquedas en el frontend y dispará **Deptos monitor** una vez. La primera corrida carga el baseline; las posteriores envían novedades.

```bash
python -m pip install --requirement requirements.txt
python -m unittest discover -s tests
python -m depto_monitor.run
```

## Límites deliberados

- El job respeta HTTPS, allowlists por portal, límites de tamaño, timeouts y no sigue redirects.
- El extractor inicial usa JSON-LD público de páginas de resultados. Cada portal puede cambiar su markup; los fallos de una fuente no interrumpen las demás.
- No descarga imágenes ni intenta eludir bloqueos, CAPTCHAs o restricciones de los sitios.
- La búsqueda en vivo construye internamente las fuentes HTTPS de los portales permitidos, busca en paralelo y muestra hasta 24 avisos únicos.
- La deduplicación agrupa por dirección/título normalizado, superficie y ambientes. El criterio es deliberadamente agresivo.

## Scraper anterior

El prototipo anterior para Recoleta y zona norte sigue en `miranda_scraper/`.
