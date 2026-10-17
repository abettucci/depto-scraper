# Deptos monitor

Monitor personal de alquileres argentinos: guardá búsquedas, recopilá avisos de Argenprop, Zonaprop, Mercado Libre y RE/MAX cada seis horas, deduplicá la misma propiedad entre portales y recibí un resumen por WhatsApp.

## Arquitectura

- `web/`: frontend estático para Cloudflare Pages o Vercel, con Supabase Auth (Google y email/contraseña).
- `api/preview.py`: función de Vercel que sirve el frontend y deriva la búsqueda en vivo a Lambda cuando está configurada.
- `lambda_function.py` + `infra/`: Lambda Python, EventBridge cada seis horas, CloudWatch Logs, Secrets Manager y Terraform.
- `supabase/schema.sql`: búsquedas, avisos agrupados, fuentes y snapshots históricos con RLS.
- `depto_monitor/`: job Python que procesa búsquedas, valida destinos, extrae JSON-LD público, conserva historial y llama al sidecar de WhatsApp Web.
- `.github/workflows/deploy-lambda.yml`: empaquetado, Terraform y deploy de la Lambda.

El formulario construye las consultas a Argenprop, Zonaprop, Mercado Libre y RE/MAX desde localidad, operación, presupuesto, ambientes, metros, expensas, mascotas, cochera y un polígono dibujado en el mapa. No hace falta conocer las URLs de los portales. Los filtros y fuentes generadas quedan persistidos para la corrida programada.

## Configuración inicial

1. Creá un proyecto Supabase, ejecutá `supabase/schema.sql` en SQL Editor y habilitá Email + Google en Auth.
2. Completá `web/config.js` con la URL y publishable key de Supabase. No publiques la service role key.
3. Para buscar en vivo, importá el repositorio en Vercel con el directorio raíz. Vercel sirve la web y conserva `POST /api/preview` como proxy autenticado hacia Lambda.
4. En GitHub → Settings → Secrets and variables → Actions, definí: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `WHATSAPP_SIDECAR_URL`, `WAWEB_SHARED_SECRET`, `DEFAULT_WHATSAPP_CHAT_ID` y `SCRAPER_LAMBDA_SHARED_SECRET` (una cadena aleatoria de 32+ caracteres).
5. El workflow **Deploy Lambda scraper** empaqueta la función, crea la infraestructura con Terraform y guarda los secretos runtime en AWS Secrets Manager. Al terminar, copiá el `Lambda URL` del log a Vercel → Settings → Environment Variables como `SCRAPER_LAMBDA_URL`, y cargá el mismo `SCRAPER_LAMBDA_SHARED_SECRET` de GitHub en Vercel. Redeployá Vercel.
6. Configurá el sidecar existente en Railway. Se usa `POST /send` con `WAWEB_SHARED_SECRET`, igual que `ticket_splitter`.
7. Creá búsquedas en el frontend. EventBridge ejecuta el monitor cada seis horas; la primera corrida carga el baseline y las posteriores envían novedades.

```bash
python -m pip install --requirement requirements.txt
python -m unittest discover -s tests
python -m depto_monitor.run
```

## Logs y diagnóstico

- Para una búsqueda en vivo: Vercel → proyecto → **Logs** → filtro `/api/preview` muestra el proxy y la respuesta de Lambda.
- Para el scraper: AWS → CloudWatch → Log groups → `/aws/lambda/depto-scraper`. Cada portal escribe su estado, cantidad, duración y código HTTP sin registrar filtros, URLs ni secretos.
- La Function URL no tiene CORS: el navegador nunca la llama directamente. Vercel la invoca con el secreto compartido por `Authorization`.

## Límites deliberados

- El job respeta HTTPS, allowlists por portal, límites de tamaño, timeouts y no sigue redirects.
- El extractor inicial usa JSON-LD público de páginas de resultados. Cada portal puede cambiar su markup; los fallos de una fuente no interrumpen las demás.
- No descarga imágenes ni intenta eludir bloqueos, CAPTCHAs o restricciones de los sitios.
- La búsqueda en vivo construye internamente las fuentes HTTPS de los portales permitidos, busca en paralelo dentro de Lambda y muestra hasta 24 avisos únicos.
- La deduplicación agrupa por dirección/título normalizado, superficie y ambientes. El criterio es deliberadamente agresivo.

## Scraper anterior

El prototipo anterior para Recoleta y zona norte sigue en `miranda_scraper/`.
