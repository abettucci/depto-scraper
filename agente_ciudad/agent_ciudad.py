"""
Reporte diario automático - Proyecto Ciudad (DIGIVENTURES)
Sin LLM — corre 100% local. Solo necesita: ASANA_TOKEN y SLACK_WEBHOOK_CIUDAD.
"""

import os
import re
import sys
import json
import requests
from datetime import date, datetime, timedelta

# Fix encoding para consola Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ASANA_TOKEN = os.environ["ASANA_TOKEN"]
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_CIUDAD", "")

PROYECTOS = {
    "Iniciativas Ciudad 2026": "1214150794003544",
    "Ciudad MC - Perf. Eng":   "1208260426826276",
}

CRONOGRAMA_PATH = os.path.join(os.path.dirname(__file__), "cronograma_ciudad.json")

# ── Asana ─────────────────────────────────────────────────────────────────────

def get_tasks(project_id: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}"}
    url = f"https://app.asana.com/api/1.0/projects/{project_id}/tasks"
    params = {
        "opt_fields": "name,completed,due_on,completed_at,memberships.section.name,num_subtasks",
        "limit": 100,
    }
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    tasks = resp.json()["data"]

    for task in tasks:
        task["subtareas_pendientes"] = []
        if task.get("num_subtasks", 0) > 0 and not task.get("completed"):
            try:
                sub_url = f"https://app.asana.com/api/1.0/tasks/{task['gid']}/subtasks"
                sub_resp = requests.get(
                    sub_url,
                    headers=headers,
                    params={"opt_fields": "name,completed", "limit": 50},
                    timeout=10,
                )
                sub_resp.raise_for_status()
                task["subtareas_pendientes"] = [
                    s["name"].strip()
                    for s in sub_resp.json()["data"]
                    if not s.get("completed")
                ]
            except Exception:
                pass

    return tasks

# ── Helpers ───────────────────────────────────────────────────────────────────

def detectar_marca(nombre: str) -> str:
    n = nombre.upper()
    if "BUEPP" in n:
        return "BUEPP"
    if "CIUDAD" in n:
        return "CIUDAD"
    return "OTRO"

def limpiar_nombre(nombre: str) -> str:
    limpio = re.sub(r"(?i)^[\s\-]*(ciudad|buepp)[\s\-]+", "", nombre).strip()
    return limpio or nombre.strip()

def formatear_subtareas(subtareas: list[str], linea_tarea: str) -> str:
    resumen = []
    for prefijo in ("Piezas", "Bases"):
        primera = next((s for s in subtareas if s.lower().startswith(prefijo.lower())), None)
        if primera:
            resumen.append(f"       ↳ {primera}")
    if resumen:
        return f"{linea_tarea}\n" + "\n".join(resumen)
    return linea_tarea

def semana_label(lunes: date) -> str:
    domingo = lunes + timedelta(days=6)
    return f"{lunes.strftime('%d/%m')} – {domingo.strftime('%d/%m')}"

# ── Clasificación por semana ───────────────────────────────────────────────────

def clasificar_tareas(tasks: list[dict], hoy: date) -> dict:
    lunes_actual   = hoy - timedelta(days=hoy.weekday())
    domingo_actual = lunes_actual + timedelta(days=6)
    lunes_sig      = lunes_actual + timedelta(days=7)
    domingo_sig    = lunes_sig + timedelta(days=6)

    def bucket():
        return {"CIUDAD": [], "BUEPP": [], "OTRO": []}

    semanas = {
        "actual":    {"lunes": lunes_actual,   "tareas": bucket(), "completadas": bucket()},
        "siguiente": {"lunes": lunes_sig,       "tareas": bucket(), "completadas": bucket()},
    }

    for t in tasks:
        nombre    = t["name"]
        seccion   = (t.get("memberships") or [{}])[0].get("section", {}).get("name", "—")
        due_raw   = t.get("due_on")
        completado = t.get("completed", False)
        marca     = detectar_marca(nombre)
        subtareas = t.get("subtareas_pendientes", [])

        etiqueta_base = f"• {limpiar_nombre(nombre)} · _{seccion}_"

        if completado:
            comp_at = t.get("completed_at", "")
            if comp_at:
                comp_date = datetime.fromisoformat(comp_at.replace("Z", "+00:00")).date()
                if lunes_actual <= comp_date <= domingo_actual:
                    semanas["actual"]["completadas"][marca].append(f"• {limpiar_nombre(nombre)}")
                elif lunes_sig <= comp_date <= domingo_sig:
                    semanas["siguiente"]["completadas"][marca].append(f"• {limpiar_nombre(nombre)}")
            continue

        if not due_raw:
            continue

        due = date.fromisoformat(due_raw)

        if due < hoy:
            atraso = (hoy - due).days
            linea = formatear_subtareas(subtareas, f"{etiqueta_base} · *{atraso}d atraso*")
            semanas["actual"]["tareas"][marca].append(linea)
        elif due <= domingo_actual:
            sufijo = "*vence HOY*" if due == hoy else due.strftime("%d/%m")
            linea = formatear_subtareas(subtareas, f"{etiqueta_base} · {sufijo}")
            semanas["actual"]["tareas"][marca].append(linea)
        elif due <= domingo_sig:
            linea = formatear_subtareas(subtareas, f"{etiqueta_base} · {due.strftime('%d/%m')}")
            semanas["siguiente"]["tareas"][marca].append(linea)

    return semanas

# ── Semáforo ──────────────────────────────────────────────────────────────────

def estado_semaforo(semanas: dict, hoy: date) -> str:
    lunes_actual = hoy - timedelta(days=hoy.weekday())
    domingo_actual = lunes_actual + timedelta(days=6)

    vencidas = 0
    hoy_count = 0
    resto = 0

    for t_str in semanas["actual"]["tareas"].values():
        for linea in t_str:
            if "atraso" in linea:
                vencidas += 1
            elif "HOY" in linea:
                hoy_count += 1
            else:
                resto += 1

    if vencidas:
        return "🔴 Rojo"
    if hoy_count:
        return "🟡 Amarillo"
    return "🟢 Verde"

# ── Cronograma ────────────────────────────────────────────────────────────────

def verificar_cronograma(hoy: date) -> list[str]:
    alertas = []
    try:
        with open(CRONOGRAMA_PATH, encoding="utf-8") as f:
            cron = json.load(f)
        for h in cron.get("hitos", []):
            fecha = date.fromisoformat(h["fecha_esperada"])
            atraso = (hoy - fecha).days
            if atraso > 0:
                alertas.append(f"• *{h['nombre']}* — {atraso}d de atraso")
            elif atraso >= -7:
                alertas.append(f"• *{h['nombre']}* — vence {fecha.strftime('%d/%m')} ({abs(atraso)}d)")
    except FileNotFoundError:
        alertas.append("• cronograma_ciudad.json no encontrado")
    return alertas

# ── Armar reporte ─────────────────────────────────────────────────────────────

MARCAS = [("CIUDAD", "🔵"), ("BUEPP", "🟣"), ("OTRO", "⚪")]

def bloque_semana(semana: dict, titulo: str) -> list[str]:
    label = semana_label(semana["lunes"])
    tareas = semana["tareas"]
    completadas = semana["completadas"]

    hay_algo = any(tareas[m] or completadas[m] for m, _ in MARCAS)
    if not hay_algo:
        return []

    lineas = [f"*{titulo} — {label}*", "─────────────────────────────"]

    for marca, emoji in MARCAS:
        t = tareas[marca]
        c = completadas[marca]
        if not t and not c:
            continue
        lineas.append(f"  *{emoji} {marca}*")
        if t:
            lineas += [f"    {l}" for l in t]
        if c:
            lineas.append("    ✅ _Completadas_")
            lineas += [f"    {l}" for l in c]

    return lineas

def armar_reporte(clasificaciones: dict, alertas_cron: list[str], hoy: date) -> str:
    # Acumular todos los proyectos en una sola estructura de semanas
    def bucket():
        return {"CIUDAD": [], "BUEPP": [], "OTRO": []}

    lunes_actual = hoy - timedelta(days=hoy.weekday())
    lunes_sig    = lunes_actual + timedelta(days=7)

    acum = {
        "actual":    {"lunes": lunes_actual, "tareas": bucket(), "completadas": bucket()},
        "siguiente": {"lunes": lunes_sig,    "tareas": bucket(), "completadas": bucket()},
    }

    for semanas in clasificaciones.values():
        for sem_key in ("actual", "siguiente"):
            if sem_key not in semanas:
                continue
            for marca in ("CIUDAD", "BUEPP", "OTRO"):
                acum[sem_key]["tareas"][marca]      += semanas[sem_key]["tareas"][marca]
                acum[sem_key]["completadas"][marca] += semanas[sem_key]["completadas"][marca]

    estado = estado_semaforo(acum, hoy)
    total_actual = sum(len(v) for v in acum["actual"]["tareas"].values())

    lineas = [
        f"*🏙️ REPORTE DIARIO — PROYECTO CIUDAD*",
        f"📅 {hoy.strftime('%d/%m/%Y')}  |  {estado}  |  {total_actual} tarea{'s' if total_actual != 1 else ''} esta semana",
        "",
    ]

    if alertas_cron:
        lineas.append("*📋 Cronograma*")
        lineas += alertas_cron
        lineas.append("")

    b_actual = bloque_semana(acum["actual"], "📍 Semana actual")
    b_sig    = bloque_semana(acum["siguiente"], "📆 Semana siguiente")

    if b_actual:
        lineas += b_actual
        lineas.append("")

    if b_sig:
        lineas += b_sig
        lineas.append("")

    if not b_actual and not b_sig:
        lineas.append("_Todo en orden. Sin tareas para las próximas dos semanas._")

    return "\n".join(lineas)

# ── Slack ─────────────────────────────────────────────────────────────────────

def enviar_slack(texto: str):
    if not SLACK_WEBHOOK:
        print("[SLACK] SLACK_WEBHOOK_CIUDAD no configurada — imprimiendo en consola.\n")
        return
    resp = requests.post(SLACK_WEBHOOK, json={"text": texto}, timeout=10)
    resp.raise_for_status()
    print("[SLACK] Enviado.")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    hoy = date.today()
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Generando reporte Ciudad...")

    clasificaciones = {}
    for nombre, pid in PROYECTOS.items():
        try:
            tasks = get_tasks(pid)
            clasificaciones[nombre] = clasificar_tareas(tasks, hoy)
        except Exception as e:
            print(f"  Error al obtener {nombre}: {e}")
            clasificaciones[nombre] = {}

    alertas_cron = verificar_cronograma(hoy)
    reporte = armar_reporte(clasificaciones, alertas_cron, hoy)

    print(reporte)
    enviar_slack(reporte)

if __name__ == "__main__":
    main()
