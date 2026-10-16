# Scraper de departamentos en venta/alquiler → Slack

Corre todos los días, junta avisos de varios portales, evalúa el precio por m² contra un
modelo propio (se afina solo con el historial) y manda un resumen a Slack.

- **Recoleta (venta):** Miranda Bosch, Zonaprop y RE/MAX → modelo de precios con 🟢/🟡/🔴 y 🔥.
- **Zona norte (venta y alquiler):** RE/MAX y Zonaprop, solo edificios con gimnasio
  (proxy de "complejo con amenities"). Sin modelo de precios todavía; se muestra precio, USD/m² y amenities.
- El historial se guarda en `data/history.xlsx` (hojas `listings` y `norte`) y el workflow lo commitea.

## Puesta en marcha (GitHub Actions)
1. Subí este repo a tu cuenta (privado).
2. Creá un **webhook entrante de Slack** y cargalo en el repo:
   Settings → Secrets and variables → Actions → **Secrets** → `SLACK_WEBHOOK_URL`.
3. Actions → *Real State Scraper Daily* → **Run workflow** para probarlo. Después corre solo, lun-sáb 8:00 (ART).
4. Si no necesitás `agente_ciudad`, borrá `.github/workflows/agente_ciudad.yml` (falla sin `ASANA_TOKEN`).

## Correrlo local
```bash
cd miranda_scraper
pip install -r requirements.txt
cp .env.example .env        # y completar SLACK_WEBHOOK_URL
python main.py --dry-run    # corre todo sin mandar a Slack
python main.py              # corre y manda a Slack
```

## Adaptarlo a otra zona (lo más importante)
Hoy está armado para **Recoleta** y **San Isidro**. Para otra zona:

| Qué | Dónde |
|---|---|
| Búsqueda de Miranda Bosch (inmobiliaria de Recoleta; borrarla si no aplica) | `scraper.py` → `BASE_URL`, y `main.py` |
| Búsqueda de Zonaprop | `zonaprop_scraper.py` → `BASE_URL`, `NORTE_VENTA_URL`, `NORTE_ALQUILER_URL` |
| Búsqueda de RE/MAX (ubicaciones) | `remax_scraper.py` → `BASE_PARAMS["locations"]`, `NORTE_LOCATIONS` |
| Topes de precio, m² y alquiler | `common.py` (`MAX_PRICE_USD`, `MIN_M2`, `MAX_M2`, `RENT_MAX_*`) |
| Zonas y precios de referencia del modelo | `price_model.py` → `ZONES`, `ZONE_LABELS` |

Para sacar la URL/ubicación de cada portal: hacé la búsqueda en el navegador con los filtros
que quieras y copiá la URL (RE/MAX trae los IDs de ubicación en `locations=`).

**Ojo con `data/history.xlsx`:** viene con el historial de Recoleta. El modelo de precios usa
esas estadísticas por zona; si buscás en otra zona, **borrá o renombrá el archivo** para que
arranque de cero, si no te va a comparar contra precios de otro barrio.

## Zonaprop y las IPs de GitHub
Zonaprop suele bloquear las IPs de datacenter de los runners de GitHub: el síntoma es
"Zonaprop: 0 listings" en la alerta de Slack. Solución gratuita: correrlo desde tu PC.
1. Registrá tu PC como *self-hosted runner* (Settings → Actions → Runners → New self-hosted runner).
2. Creá la **variable** de repo `RUNNER_LABEL` = `self-hosted`. Para volver a GitHub, borrala.
En Windows necesitás Git for Windows (el workflow usa bash).

## Alertas y avisos
- Si una fuente falla o viene vacía, el mensaje de Slack abre con "⚠️ Fuentes con problemas".
- Cada fuente es independiente: si una cae, el resto sigue.

## Nota
Scrapear portales inmobiliarios puede ir contra sus términos de uso. Usalo con criterio y
sin sobrecargar los sitios (los delays entre pedidos están a propósito).
