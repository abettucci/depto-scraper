# Deptos monitor

Monitor personal de alquileres argentinos: guardá búsquedas, recopilá avisos de Argenprop, Zonaprop, Mercado Libre y RE/MAX cada seis horas, deduplicá la misma propiedad entre portales y recibí un resumen por WhatsApp.

## Arquitectura

- `web/`: frontend estático para Cloudflare Pages, con Supabase Auth (Google y email/contraseña).
- `supabase/schema.sql`: búsquedas, avisos agrupados, fuentes y snapshots históricos con RLS.
- `depto_monitor/`: job Python que procesa búsquedas, valida destinos, extrae JSON-LD público, conserva historial y llama al sidecar de WhatsApp Web.
- `.github/workflows/depto-monitor.yml`: ejecución programada cada seis horas.

Las URLs por portal se guardan junto con filtros (barrios, presupuesto, ambientes, expensas, mascotas, cochera, radio y coordenadas). El formulario admite URLs ya armadas por portal; los filtros propios quedan persistidos para extender adaptadores específicos.

## Configuración inicial

1. Creá un proyecto Supabase, ejecutá `supabase/schema.sql` en SQL Editor y habilitá Email + Google en Auth.
2. Copiá `web/config.example.js` a `web/config.js`, completá la URL y anon key, y desplegá `web/` en Cloudflare Pages. No publiques la service role key.
3. Copiá `.env.example` a `.env` para probar localmente. En GitHub, definí los secretos indicados en el workflow.
4. Configurá el sidecar existente en Railway. Se usa `POST /send` con `WAWEB_SHARED_SECRET`, igual que `ticket_splitter`.
5. Creá búsquedas en el frontend y dispará **Deptos monitor** una vez. La primera corrida carga el baseline; las posteriores envían novedades.

```bash
python -m pip install --requirement requirements.txt
python -m unittest discover -s tests
python -m depto_monitor.run
```

## Límites deliberados

- El job respeta HTTPS, allowlists por portal, límites de tamaño, timeouts y no sigue redirects.
- El extractor inicial usa JSON-LD público de páginas de resultados. Cada portal puede cambiar su markup; los fallos de una fuente no interrumpen las demás.
- No descarga imágenes ni intenta eludir bloqueos, CAPTCHAs o restricciones de los sitios.
- La deduplicación agrupa por dirección/título normalizado, superficie y ambientes. El criterio es deliberadamente agresivo.

## Scraper anterior

El prototipo anterior para Recoleta y zona norte sigue en `miranda_scraper/`.
