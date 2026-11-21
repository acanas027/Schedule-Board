"""
Schedule Board — live production schedule for the plant lines.

Planners write the board at shift start (what runs on each line), leads update
recipe status from the floor (PIN sign-in), and the office watches it live.

Backend: Google Sheets. With no secrets configured the app runs in DEMO mode
(in-memory data seeded from the whiteboard) so it can be tried right away.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Schedule Board", page_icon="📋", layout="wide", initial_sidebar_state="collapsed")

# ── Configuration ──────────────────────────────────────────────────────────────
TZ = ZoneInfo("America/Chicago")

# (id, label, area) — area: top row, bottom row, side panel (like the whiteboard)
LINES = (
    [(f"L{i}", f"Line {i}", "top") for i in range(1, 11)]
    + [(f"L{i}", f"Line {i}", "bottom") for i in range(11, 20)]
    + [("CS", "Cole Slaw", "bottom"), ("C1", "Cook 1", "side"), ("C2", "Cook 2", "side"), ("VX", "Vortex", "side")]
)
LINE_IDS = [i for i, _, _ in LINES]
LINE_LABEL = {i: lbl for i, lbl, _ in LINES}
LINE_ORDER = {i: n for n, i in enumerate(LINE_IDS)}
GRID_LINES = [i for i, _, a in LINES if a in ("top", "bottom")]
SIDE_LINES = [i for i, _, a in LINES if a == "side"]

STATUSES = ["Not ready", "Ready", "Next", "Running", "Finished"]
ICON = {"Not ready": "🔴", "Ready": "⚪", "Next": "🟡", "Running": "🟢", "Finished": "✅"}
STATUS_STYLE = {  # background, border, text, tag
    "Not ready": ("#fde4e2", "#d93025", "#a50e0e", "NOT READY"),
    "Ready": ("#ffffff", "#c3c9d2", "#1f2a37", "READY"),
    "Next": ("#fff3cd", "#e6a700", "#7a5600", "NEXT"),
    "Running": ("#dcf5e3", "#188038", "#0d5223", "RUNNING"),
    "Finished": ("#eceff3", "#c3c9d2", "#7a8594", "DONE"),
}
REASONS = ["Ran out of time", "Temoerature problems" , "No carts", "No totes" , "No Dry Spice" , "No wet mix", "Mechanical issues", "Rework other product", "No packing material", "Short staffed", "Working on it" , "No Food"]

SHIFTS = ["1st", "2nd"]                 # in start-time order
SHIFT_START = {"1st": 6, "2nd": 16}     # hour (24h) each shift starts
CLOSE_BY = {"1st": (15, 55, 0), "2nd": (3, 0, 1)}  # shift must be ended by (hour, minute, days after shift date)
CLOSE_WARN_MINUTES = 15                 # pop-up starts this many minutes before the deadline
PLANNER_ROLES = ("planner", "admin")
SESSION_HOURS = 12                      # how long a sign-in lasts (survives refresh)
REFRESH_SECONDS = 15                    # board auto-refresh
CACHE_SECONDS = 10                      # shared read cache (protects Sheets quota)
FLAG_KEYS = ("down", "priority")
DOWN_REASONS = ["No Packing Material", "No Staff", "Mechanical Issues", "Not Scheduled" , "No Food"]

# Sheet tabs (schedule_YYYY_MM and log_YYYY_MM are created per month)
SCHED_COLS = ["id", "date", "shift", "line", "seq", "recipe", "qty", "status", "reason", "comment",
              "deleted", "created_by", "created_at", "updated_by", "updated_at", "started_at", "finished_at",
              "carried", "carried_from"]
LOG_COLS = ["ts", "date", "shift", "line", "item_id", "recipe", "action", "from_status", "to_status",
            "reason", "comment", "user"]
LINEDAY_COLS = ["key", "date", "shift", "line", "down", "priority", "updated_by", "updated_at", "down_reason"]
BOARD_COLS = ["key", "date", "shift", "notes", "updated_by", "updated_at", "closed_by", "closed_at", "carried_count"]
GOAL_COLS = ["label", "value"]
LEAD_COLS = ["name", "pin", "role", "lines", "active"]
CORE = {"lineday": LINEDAY_COLS, "boards": BOARD_COLS, "goals": GOAL_COLS, "leads": LEAD_COLS}
DEFAULT_GOALS = [["Pounds", "785-825"], ["Units", "425-500"], ["Units Per Hour", "30K-40K"], ["Cases Per Day", "60K-70K"]]


# ── Small helpers ──────────────────────────────────────────────────────────────
def now() -> datetime:
    return datetime.now(TZ)


def stamp() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def truthy(x) -> bool:
    return str(x).strip().upper() in ("TRUE", "1", "YES", "Y", "X")


def mk(d: date) -> str:
    return f"{d.year}_{d.month:02d}"


def sched_tab(d: date) -> str:
    return f"schedule_{mk(d)}"


def log_tab(d: date) -> str:
    return f"log_{mk(d)}"


def board_key(d: date, shift: str) -> str:
    return f"{d.isoformat()}|{shift}"


def line_key(d: date, shift: str, line: str) -> str:
    return f"{d.isoformat()}|{shift}|{line}"


def current_board(at: datetime | None = None) -> tuple[date, str]:
    """Date + shift happening right now (overnight hours belong to yesterday's last shift)."""
    n = at or now()
    started = [s for s in SHIFTS if n.hour >= SHIFT_START[s]]
    if started:
        return n.date(), started[-1]
    return n.date() - timedelta(days=1), SHIFTS[-1]


def prev_slot(d: date, shift: str) -> tuple[date, str]:
    i = SHIFTS.index(shift)
    return (d, SHIFTS[i - 1]) if i > 0 else (d - timedelta(days=1), SHIFTS[-1])


def next_slot(d: date, shift: str) -> tuple[date, str]:
    i = SHIFTS.index(shift)
    return (d, SHIFTS[i + 1]) if i < len(SHIFTS) - 1 else (d + timedelta(days=1), SHIFTS[0])


def slot_label(d: date, shift: str) -> str:
    if lang() == "es":
        return f"{fmt_date(d)} · {shift_name(shift)} turno"
    return f"{fmt_date(d)} · {shift} shift"


def parse_entries(text: str) -> list[tuple[str, str]]:
    """'PO 379\\nCS 131 x6, dice' -> [('PO 379',''), ('CS 131','6'), ('DICE','')]"""
    out = []
    for raw in re.split(r"[\n,;]+", text or ""):
        s = " ".join(raw.strip().split())
        if not s:
            continue
        qty = ""
        m = re.match(r"^(.*?)\s*[x×*]\s*(\d+)$", s, re.I)
        if m and m.group(1).strip():
            s, qty = m.group(1).strip(), m.group(2)
        s = re.sub(r"^([A-Z]{2})\s*(\d)", r"\1 \2", s.upper())
        out.append((s, qty))
    return out


def entries_text(items: pd.DataFrame) -> str:
    return "\n".join(f"{r} x{q}" if q else r for r, q in zip(items["recipe"], items["qty"]))


def allowed_lines(spec) -> list[str]:
    spec = str(spec or "").strip()
    if not spec or spec.upper() == "ALL":
        return list(LINE_IDS)
    out = []
    for tok in re.split(r"[,;]+", spec):
        t = tok.strip().upper()
        if not t:
            continue
        rng = (re.match(r"^(?:LINES?\s*)?(\d+)\s*(?:-|–|TO)\s*(\d+)$", t)
               or re.match(r"^(\d+)/(\d+)(?:/\d+)?$", t))  # Sheets may turn 1-9 into a date like 1/9/2026
        if rng:  # a range like 1-9 or 10-17
            lo, hi = sorted((int(rng.group(1)), int(rng.group(2))))
            out += [f"L{n}" for n in range(lo, hi + 1) if f"L{n}" in LINE_LABEL and f"L{n}" not in out]
            continue
        if t.isdigit():
            t = f"L{int(t)}"
        for i, lbl, _ in LINES:
            if t in (i.upper(), lbl.upper(), lbl.upper().replace(" ", "")) and i not in out:
                out.append(i)
    return out


def pin_ok(given, stored) -> bool:
    g, s = str(given).strip(), str(stored).strip()
    if not g or not s:
        return False
    return g == s or (g.isdigit() and s.isdigit() and int(g) == int(s))


def flash(msg: str, kind: str = "ok") -> None:
    st.session_state.setdefault("_flash", []).append((msg, kind))


def show_flash() -> None:
    for msg, kind in st.session_state.pop("_flash", []):
        st.toast(msg, icon="✅" if kind == "ok" else "⚠️")


def is_tv() -> bool:
    return str(st.query_params.get("tv", "")).lower() in ("1", "true", "yes")


# ── Language (per device: English / Español) ───────────────────────────────────
# Stored data (statuses, reasons, line names in the sheet) stays in English; only what's shown is translated.
# To change a Spanish wording, edit it here.
ES = {
    # statuses / tags
    "Not ready": "No listo", "Ready": "Listo", "Next": "Siguiente", "Running": "Corriendo", "Finished": "Terminado",
    "NOT READY": "NO LISTO", "READY": "LISTO", "NEXT": "SIGUE", "RUNNING": "ACTIVO", "DONE": "HECHO",
    # reasons
    "Ran out of time": "Se acabó el tiempo", "Mechanical issues": "Problemas mecánicos",
    "Rework other product": "Retrabajo de otro producto", "No packing material": "Sin material de empaque",
    "Short staffed": "Falta de personal", "Working on it": "Trabajando en ello", "No Food": "Sin comida",
    "No material": "Sin material", "No reason given": "Sin razón",
    "No Material": "Sin material", "No Staff": "Sin personal", "Mechanical Issues": "Problemas mecánicos",
    "Not Scheduled": "No programada",
    # default goals
    "Pounds": "Libras", "Units": "Unidades", "Units Per Hour": "Unidades por hora", "Cases Per Day": "Cajas por día",
    # board
    "Schedule Board": "Tablero de producción", "★ PRIORITY": "★ PRIORIDAD", "✕ DOWN": "✕ PARADA",
    "✅ {a}/{b} done": "✅ {a}/{b} terminadas", "🟢 {n} running": "🟢 {n} corriendo", "🟡 {n} next": "🟡 {n} siguientes",
    "🔴 {n} not ready": "🔴 {n} no listas", "✕ {n} line down": "✕ {n} línea parada", "✕ {n} lines down": "✕ {n} líneas paradas",
    "Daily goals": "Metas diarias", "No updates yet": "Sin actualizaciones", "Live · updated {t}": "En vivo · actualizado {t}",
    "Notes": "Notas", "Latest from the floor": "Últimas del piso",
    "Shift ended {t} by {who} — {n} carried to next shift": "Turno cerrado {t} por {who} — {n} pasadas al siguiente turno",
    "{slot} not ended — was due {due} ({m} min late)": "{slot} sin cerrar — vencía {due} ({m} min tarde)",
    "{slot} must be ended by {due} ({m} min left)": "{slot} debe cerrarse antes de {due} (faltan {m} min)",
    # pickers / sign-in
    "Date": "Fecha", "Shift": "Turno", "Board": "Tablero", "Update": "Actualizar", "Plan": "Planear", "History": "Historial",
    "No leads set up yet. Add people to the **leads** tab of the Google Sheet: `name`, `pin`, `role` (lead / planner), "
    "`lines` (e.g. `1, 2, Cole Slaw` or `ALL`), `active` (TRUE).":
        "Todavía no hay líderes. Agrega personas en la pestaña **leads** del Google Sheet: `name`, `pin`, `role` "
        "(lead / planner), `lines` (ej. `1, 2, Cole Slaw` o `ALL`), `active` (TRUE).",
    "**Sign in to update the board**": "**Inicia sesión para actualizar el tablero**", "Name": "Nombre",
    "Pick your name": "Elige tu nombre", "Sign in": "Entrar", "Wrong name or PIN.": "Nombre o PIN incorrecto.",
    "Your sign-in expired — sign in again.": "Tu sesión expiró — vuelve a entrar.",
    "Only planners can see the history.": "Solo los planeadores pueden ver el historial.",
    "Only planners can set up the board. Use **Update** to change recipe status.":
        "Solo los planeadores pueden armar el tablero. Usa **Actualizar** para cambiar el estado de las recetas.",
    "Signed in for about {n} more hours": "Sesión activa por unas {n} horas más",
    "Signed in for about {n} more hour": "Sesión activa por {n} hora más", "Sign out": "Salir",
    "lead": "líder", "planner": "planeador", "admin": "admin", "Language": "Idioma",
    # update page
    "✏️ Update the floor": "✏️ Actualizar el piso", "Lines": "Líneas", "🔄 Refresh": "🔄 Recargar",
    "🔚 End shift & carry over": "🔚 Cerrar turno y pasar pendientes",
    "At the end of the shift: everything not Finished moves to the next shift's board":
        "Al final del turno: todo lo que no esté Terminado pasa al tablero del siguiente turno",
    "No lines could be read for you. In the **leads** tab, column **lines** should look like `1-9`, `7, 8, 9` or `ALL` "
    "(format the column as Plain text). Then sign out and back in.":
        "No se pudieron leer tus líneas. En la pestaña **leads**, la columna **lines** debe verse como `1-9`, `7, 8, 9` "
        "o `ALL` (formato texto sin formato). Luego sal y vuelve a entrar.",
    "Nothing is on the board for this shift yet. A planner sets it up on the **Plan** page — or add recipes below.":
        "Todavía no hay nada en el tablero de este turno. Un planeador lo arma en la página **Planear** — o agrega recetas abajo.",
    "{a}/{b} done": "{a}/{b} terminadas", " · ★ priority": " · ★ prioridad", " · ✕ down: {r}": " · ✕ parada: {r}",
    "Line down": "Línea parada", "Add recipe": "Agregar receta",
    "Add recipe — e.g. PO 379 or CS 131 x6": "Agregar receta — ej. PO 379 o CS 131 x6", "➕ Add": "➕ Agregar",
    "Added {n} to {line}": "{n} agregadas a {line}", "Status": "Estado",
    "{r} → {s}": "{r} → {s}", "That recipe was removed or changed by someone else — board refreshed.":
        "Otra persona quitó o cambió esa receta — se recargó el tablero.",
    "Couldn't save — {e}": "No se pudo guardar — {e}",
    "Why is it not ready?": "¿Por qué no está lista?", "Reason (required)": "Razón (obligatoria)",
    "Pick a reason": "Elige una razón", "Comment (optional)": "Comentario (opcional)", "Save": "Guardar",
    "Pick a reason first.": "Primero elige una razón.", "Cancel": "Cancelar",
    "{r} → Not ready ({why})": "{r} → No listo ({why})",
    "Why is the line down?": "¿Por qué está parada la línea?", "{line} is down — {why}": "{line} está parada — {why}",
    "💬 Comments": "💬 Comentarios", "Reason and comment — carried to the next shift":
        "Razón y comentario — pasan al siguiente turno",
    "**Comments · {r}**": "**Comentarios · {r}**", "Reason": "Razón", "Comment": "Comentario",
    "What happened, what's needed…": "Qué pasó, qué se necesita…", "Not ready needs a reason.": "No listo necesita una razón.",
    "Comments saved on {r}": "Comentarios guardados en {r}",
    "↔ Move": "↔ Mover", "Move this recipe to another line": "Mover esta receta a otra línea",
    "**Move {r}** from {line}": "**Mover {r}** de {line}", "To line": "A la línea", "Pick a line": "Elige una línea",
    "Put it": "Ponerla", "Last in line": "Al final", "First in line": "Al principio", "Move": "Mover",
    "Pick a line.": "Elige una línea.", "{r} moved to {line}": "{r} movida a {line}",
    "End shift & carry over": "Cerrar turno y pasar pendientes",
    "Everything on this board is finished or already carried over.": "Todo en este tablero está terminado o ya se pasó.",
    "🔚 End shift": "🔚 Cerrar turno", "{slot} ended": "{slot} cerrado",
    "These **{n}** recipes didn't finish. They'll move to the top of **{slot}** with their status, reason and comment. "
    "Finished recipes stay on this shift's record.":
        "Estas **{n}** recetas no se terminaron. Pasarán arriba de **{slot}** con su estado, razón y comentario. "
        "Las terminadas se quedan en el registro de este turno.",
    "↪ Carry over to {slot}": "↪ Pasar a {slot}",
    "Pick a reason for every recipe — missing {n}: ": "Elige una razón para cada receta — faltan {n}: ",
    "Shift ended — {n} recipes carried to {slot}": "Turno cerrado — {n} recetas pasadas a {slot}",
    "⏰ End the shift": "⏰ Cierra el turno",
    "**{slot}** hasn't been ended. It was due at **{due}** — {m} min late.":
        "**{slot}** no se ha cerrado. Vencía a las **{due}** — {m} min tarde.",
    "**{slot}** must be ended by **{due}** — {m} min left.": "**{slot}** debe cerrarse antes de las **{due}** — faltan {m} min.",
    "Press **End shift & carry over** so the next shift gets its board with everything that didn't finish and why.":
        "Presiona **Cerrar turno y pasar pendientes** para que el siguiente turno reciba su tablero con todo lo que "
        "no se terminó y por qué.",
    "🔚 End shift now": "🔚 Cerrar turno ahora", "Remind me in 5 min": "Recordar en 5 min",
    # plan page
    "🗓️ Plan the board": "🗓️ Planear el tablero", "⧉ Copy last board": "⧉ Copiar último tablero",
    "Copied {slot} — review and **Save board**.": "Se copió {slot} — revisa y **Guarda el tablero**.",
    "No earlier board found in the last two weeks.": "No hay tableros anteriores en las últimas dos semanas.",
    "↺ Discard edits": "↺ Descartar cambios", "🗑️ Clear board": "🗑️ Borrar tablero",
    "Remove every recipe, line down / priority and notes from this shift's board":
        "Quitar todas las recetas, líneas paradas / prioridad y notas del tablero de este turno",
    "Clear the board?": "¿Borrar el tablero?",
    "This removes **everything** from **{slot}**: {n} recipes, lines down / priority and the notes. "
    "The board starts blank.": "Esto quita **todo** de **{slot}**: {n} recetas, líneas paradas / prioridad y las notas. "
                               "El tablero queda en blanco.",
    "The previous shift's record is kept, and every removed recipe is saved in the activity log.":
        "El registro del turno anterior se conserva y cada receta quitada queda en el registro de actividad.",
    "🗑️ Yes, clear it": "🗑️ Sí, borrar", "The board is already blank.": "El tablero ya está en blanco.",
    "{slot} cleared — {n} recipes removed": "{slot} borrado — {n} recetas quitadas",
    "One recipe per row (or comma-separated), in run order. Add `x6` for batches. To mark a line down, pick its reason "
    "in the box under it (leave it on *Running* otherwise). Recipes already on the board (including ones carried over "
    "from last shift) keep their status when you save.":
        "Una receta por renglón (o separadas por coma), en orden de corrida. Agrega `x6` para baches. Para marcar una "
        "línea parada, elige la razón en la caja de abajo (déjala en *Corriendo* si no). Las recetas que ya están en el "
        "tablero (incluyendo las del turno anterior) conservan su estado al guardar.",
    "Line down?": "¿Línea parada?", "Priority lines": "Líneas prioritarias",
    "Board notes (everyone sees these)": "Notas del tablero (todos las ven)",
    "High protein items, reminders…": "Productos altos en proteína, recordatorios…", "💾 Save board": "💾 Guardar tablero",
    "Board saved — {a} added, {r} removed": "Tablero guardado — {a} agregadas, {r} quitadas",
    "🎯 Daily goals (side panel)": "🎯 Metas diarias (panel lateral)", "label": "meta", "value": "valor",
    "Save goals": "Guardar metas", "Goals saved": "Metas guardadas",
    # history
    "📈 History": "📈 Historial", "From": "Desde", "To": "Hasta", "All lines": "Todas las líneas",
    "'From' is after 'To'.": "'Desde' es después de 'Hasta'.", "No boards in this range yet.": "Todavía no hay tableros en estas fechas.",
    "Nothing planned for these lines in this range.": "Nada planeado para estas líneas en estas fechas.",
    "Recipes planned": "Recetas planeadas", "Not finished": "No terminadas", "Carried to next shift": "Pasadas al siguiente turno",
    "Why recipes didn't finish (carry-over reasons)": "Por qué no se terminaron (razones)",
    "Everything planned was finished. 🎉": "Todo lo planeado se terminó. 🎉", "Everything planned was finished.": "Todo lo planeado se terminó.",
    "Recipes": "Recetas", "Finish rate by line": "% terminado por línea", "Finish %": "% terminado", "By shift": "Por turno",
    "Planned": "Planeadas", "Line": "Línea", "#": "#", "Recipe": "Receta", "Qty": "Cant.", "Carried over": "Pasada",
    "Started": "Inició", "Run (min)": "Duración (min)", "Last update by": "Última actualización por", "Yes": "Sí",
    "⬇ Download recipes (CSV)": "⬇ Descargar recetas (CSV)", "Activity log (every status change)": "Registro de actividad (cada cambio)",
    "No activity yet.": "Todavía no hay actividad.", "⬇ Download log (CSV)": "⬇ Descargar registro (CSV)",
    "Schedule Board — History": "Tablero de producción — Historial", "printed": "impreso",
    "Nothing to show.": "Nada que mostrar.", "🖨️ Print summary": "🖨️ Imprimir resumen",
}
_ES_DAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
_ES_MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
LANG_COOKIE = "sb_lang"


def lang() -> str:
    try:
        return st.session_state.get("lang", "en")
    except Exception:
        return "en"


def _(text: str, **kw) -> str:
    """Translate a UI string for this device (and fill in {placeholders})."""
    out = ES.get(text, text) if lang() == "es" else text
    return out.format(**kw) if kw else out


def line_name(line_id: str) -> str:
    lbl = LINE_LABEL.get(line_id, line_id)
    return lbl.replace("Line ", "Línea ") if lang() == "es" else lbl


def shift_name(shift: str) -> str:
    return {"1st": "1er", "2nd": "2do", "3rd": "3er"}.get(shift, shift) if lang() == "es" else shift


def fmt_date(d: date, weekday: bool = True) -> str:
    if lang() == "es":
        return (f"{_ES_DAYS[d.weekday()]} " if weekday else "") + f"{d.day} {_ES_MONTHS[d.month - 1]}"
    return (f"{d:%a} " if weekday else "") + f"{d:%b} {d.day}"


def init_lang() -> None:
    """Language for this device: the ?lang= link wins, then this browser's saved choice, then English."""
    ss, qp = st.session_state, st.query_params
    want = qp.get("lang")
    if want not in ("en", "es"):
        want = ss.get("lang")
    if want not in ("en", "es"):
        try:
            want = st.context.cookies.get(LANG_COOKIE)
        except Exception:
            want = None
    ss["lang"] = want if want in ("en", "es") else "en"
    if ss["lang"] == "es" and qp.get("lang") != "es":
        qp["lang"] = "es"  # keep it in the link so refresh / page changes stay in Spanish
    elif ss["lang"] == "en" and "lang" in qp:
        qp.pop("lang", None)


def on_lang_change() -> None:
    pick = st.session_state.get("_lang_pick")
    if pick in ("en", "es"):
        st.session_state["lang"] = pick
        st.session_state["_save_lang_cookie"] = pick
        if pick == "es":
            st.query_params["lang"] = "es"
        else:
            st.query_params.pop("lang", None)


def save_lang_cookie() -> None:
    """Remember the choice in this browser (1 year) so the device opens in that language next time."""
    pick = st.session_state.pop("_save_lang_cookie", None)
    if pick:
        components.html(f"<script>document.cookie='{LANG_COOKIE}={pick}; max-age=31536000; path=/; SameSite=Lax';"
                        f"try{{window.parent.document.cookie='{LANG_COOKIE}={pick}; max-age=31536000; path=/; SameSite=Lax'}}catch(e){{}}"
                        "</script>", height=0)


# ── Storage: Google Sheets, or in-memory demo ──────────────────────────────────
def _cell(v) -> str:
    return "TRUE" if v is True else "FALSE" if v is False else str(v)


class SheetStore:
    demo = False

    def __init__(self, sa_info: dict, sheet_id: str):
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_info(
            sa_info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.gs = gspread
        self.sh = gspread.authorize(creds).open_by_key(sheet_id)
        self.ws = {w.title: w for w in self.sh.worksheets()}
        self.lock = threading.Lock()
        self.checked: set[str] = set()

    def ensure(self, name, header, seed=None):
        if name in self.ws:
            if name not in self.checked:  # add columns a newer version needs to an existing tab
                self.checked.add(name)
                cur = [c.strip() for c in self.ws[name].row_values(1)]
                if cur != header and cur == header[:len(cur)]:
                    self.ws[name].update(range_name="A1", values=[header], value_input_option="RAW")
            return
        with self.lock:
            if name in self.ws:
                return
            try:
                w = self.sh.add_worksheet(title=name, rows=1000, cols=max(len(header), 10))
                w.update(range_name="A1", values=[header] + [[_cell(v) for v in r] for r in (seed or [])],
                         value_input_option="RAW")
            except self.gs.exceptions.APIError:
                w = self.sh.worksheet(name)  # created by another session meanwhile
            self.ws[name] = w

    def read(self, names):
        names = [n for n in names if n in self.ws]
        if not names:
            return {}
        res = self.sh.values_batch_get([f"'{n}'!A:Z" for n in names])
        return {n: vr.get("values", []) for n, vr in zip(names, res.get("valueRanges", []))}

    def col_values(self, name):
        return self.ws[name].col_values(1)

    def update_cells(self, name, cells):
        if cells:
            a1 = self.gs.utils.rowcol_to_a1
            self.ws[name].batch_update([{"range": a1(r, c), "values": [[_cell(v)]]} for r, c, v in cells],
                                       value_input_option="RAW")

    def update_rows(self, name, rows):
        if rows:
            self.ws[name].batch_update([{"range": f"A{r}", "values": [[_cell(v) for v in vals]]} for r, vals in rows],
                                       value_input_option="RAW")

    def append(self, name, rows):
        if rows:
            self.ws[name].append_rows([[_cell(v) for v in r] for r in rows],
                                      value_input_option="RAW", table_range="A1")

    def replace(self, name, values):
        w = self.ws[name]
        w.clear()
        w.update(range_name="A1", values=[[_cell(v) for v in r] for r in values], value_input_option="RAW")


class MemoryStore:
    demo = True
    why = ""

    def __init__(self):
        self.t: dict[str, list[list[str]]] = {}
        self.lock = threading.Lock()

    def ensure(self, name, header, seed=None):
        with self.lock:
            if name not in self.t:
                self.t[name] = [list(header)] + [[_cell(v) for v in r] for r in (seed or [])]

    def read(self, names):
        with self.lock:
            return {n: [list(r) for r in self.t[n]] for n in names if n in self.t}

    def col_values(self, name):
        with self.lock:
            return [r[0] if r else "" for r in self.t[name]]

    def _set(self, name, r, c, v):
        rows = self.t[name]
        while len(rows) < r:
            rows.append([])
        row = rows[r - 1]
        while len(row) < c:
            row.append("")
        row[c - 1] = _cell(v)

    def update_cells(self, name, cells):
        with self.lock:
            for r, c, v in cells:
                self._set(name, r, c, v)

    def update_rows(self, name, rows):
        with self.lock:
            for r, vals in rows:
                for i, v in enumerate(vals):
                    self._set(name, r, i + 1, v)

    def append(self, name, rows):
        with self.lock:
            self.t[name].extend([[_cell(v) for v in r] for r in rows])

    def replace(self, name, values):
        with self.lock:
            self.t[name] = [[_cell(v) for v in r] for r in values]


DEMO_BOARD = {  # from the whiteboard photo
    "L1": ["DI 354", "DI 153"], "L2": ["PO 379", "CS 131", "CS 04", "PO 380"],
    "L4": ["CS 38", "MA 157", "MA 129", "PO 329"],
    "L7": ["PO 294", "PO 242", "PO 244", "PO 241", "MA 173"],
    "L8": ["PO 294", "PO 302", "MA 118", "CS 117", "PO 439", "CS 118", "PO 349"],
    "L9": ["CS 38", "PA 158", "PO 297", "PO 418", "PT 209", "CH 95", "SA 40", "SA 70"],
    "L11": ["PO 253", "PO 460", "MA 157", "PO 219"], "L13": ["MA 161", "MA 157", "VE 103"],
    "L14": ["MA 137", "PO 416", "MA 120", "PT 245", "PT 197", "PO 430", "PT 469"],
    "L15": ["PO 250", "PO 256"], "L16": ["DI 117", "DI 119"], "L19": ["MO 54", "VE 149", "PR 148"],
    "CS": ["CS 131 x6", "CS 04 x17", "CS 38 x16", "DICE", "WENDY"],
    "C1": ["CI 70", "CI 65"], "C2": ["CI 39"], "VX": ["CI 86", "CI 37"],
}
DEMO_PROGRESS = {"L2": 2, "L4": 1, "L7": 3, "L8": 2, "L9": 2, "L14": 3, "CS": 1, "C1": 1, "L11": 1, "L16": 1}
DEMO_NOTES = ("High protein items: PT 468 – 2nd, PT 469 – can run\n\n"
              "We don't have time to touch product twice. We need to do it right the first time!")


def demo_store() -> MemoryStore:
    s = MemoryStore()
    d, shift = current_board()
    base = now()
    ts = lambda mins: (base - timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    start = ts(240)
    rows, minute = [], 200
    for line, recs in DEMO_BOARD.items():
        done = DEMO_PROGRESS.get(line, -1)
        for seq, (rec, qty) in enumerate(parse_entries("\n".join(recs)), 1):
            status = ("Finished" if seq <= done else "Running" if seq == done + 1 else
                      "Next" if seq == done + 2 else "Ready") if done >= 0 else "Ready"
            reason = comment = ""
            if line == "L9" and rec == "PO 418":
                status, reason, comment = "Not ready", "No material", "Waiting on material"
            if line == "L11" and rec == "PO 219":
                comment = "Out of box lids"
            upd = start
            if status != "Ready" or comment:
                minute -= 4
                upd = ts(max(minute, 2))
            rows.append([uuid.uuid4().hex[:10], d.isoformat(), shift, line, seq, rec, qty, status, reason, comment,
                         False, "Demo Planner", start, "Demo Lead" if upd != start else "Demo Planner", upd,
                         upd if status in ("Running", "Finished") else "", upd if status == "Finished" else "",
                         "", ""])
    s.ensure(sched_tab(d), SCHED_COLS, seed=rows)
    s.ensure("leads", LEAD_COLS, seed=[["Demo Lead", "1111", "lead", "ALL", True],
                                       ["Demo Planner", "2222", "planner", "ALL", True]])
    s.ensure("goals", GOAL_COLS, seed=DEFAULT_GOALS)
    s.ensure("lineday", LINEDAY_COLS, seed=[
        [line_key(d, shift, ln), d.isoformat(), shift, ln, ln in ("L17", "L18"), ln == "L9", "Demo Planner", start,
         {"L17": "Not Scheduled", "L18": "Mechanical Issues"}.get(ln, "")]
        for ln in ("L9", "L17", "L18")])
    s.ensure("boards", BOARD_COLS, seed=[[board_key(d, shift), d.isoformat(), shift, DEMO_NOTES, "Demo Planner", start]])
    return s


@st.cache_resource(show_spinner="Connecting to Google Sheets…")
def store():
    try:
        keys = list(st.secrets.keys())
    except Exception:
        keys = []
    if not keys:
        s = demo_store()
        s.why = "No secrets found."
        return s
    missing = [k for k in ("sheet_id", "gcp_service_account") if k not in keys]
    if missing:
        s = demo_store()
        s.why = (f"Secrets are missing `{'` and `'.join(missing)}` at the top level. "
                 f"Top-level keys found: `{', '.join(keys)}`.")
        return s
    return SheetStore(dict(st.secrets["gcp_service_account"]), str(st.secrets["sheet_id"]).strip())


# ── Reads (cached, shared by every viewer) ─────────────────────────────────────
def to_df(values, cols) -> pd.DataFrame:
    if not values:
        return pd.DataFrame(columns=cols + ["_row"])
    header = [str(h).strip() for h in values[0]]
    rows = []
    for i, r in enumerate(values[1:], start=2):
        r = [str(v) for v in r] + [""] * (len(header) - len(r))
        rows.append(r[:len(header)] + [i])
    df = pd.DataFrame(rows, columns=header + ["_row"])
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    return df[df[cols[0]].astype(str).str.strip() != ""].reset_index(drop=True)


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def load_core() -> dict[str, pd.DataFrame]:
    s = store()
    for name, cols in CORE.items():
        s.ensure(name, cols, seed=DEFAULT_GOALS if name == "goals" else None)
    raw = s.read(list(CORE))
    return {name: to_df(raw.get(name, []), cols) for name, cols in CORE.items()}


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def load_sched(month: str) -> pd.DataFrame:
    name = f"schedule_{month}"
    return to_df(store().read([name]).get(name, []), SCHED_COLS)


@st.cache_data(ttl=60, show_spinner=False)
def load_log(month: str) -> pd.DataFrame:
    name = f"log_{month}"
    return to_df(store().read([name]).get(name, []), LOG_COLS)


def refresh() -> None:
    load_core.clear()
    load_sched.clear()
    load_log.clear()


def board_items(sched: pd.DataFrame, d: date, shift: str) -> pd.DataFrame:
    if sched.empty:
        return sched
    m = (sched["date"] == d.isoformat()) & (sched["shift"] == shift) & (~sched["deleted"].map(truthy))
    df = sched[m].copy()
    df["_lo"] = df["line"].map(LINE_ORDER).fillna(999)
    df["_seq"] = pd.to_numeric(df["seq"], errors="coerce").fillna(999)
    return df.sort_values(["_lo", "_seq"]).reset_index(drop=True)


def line_flags(lineday: pd.DataFrame, d: date, shift: str) -> dict[str, dict]:
    out = {}
    if lineday.empty:
        return out
    df = lineday[(lineday["date"] == d.isoformat()) & (lineday["shift"] == shift)]
    for r in df.to_dict("records"):
        out[r["line"]] = {**{k: truthy(r[k]) for k in FLAG_KEYS},
                          "down_reason": str(r.get("down_reason", "")).strip() if truthy(r["down"]) else ""}
    return out


def board_row(boards: pd.DataFrame, d: date, shift: str) -> dict:
    if boards.empty:
        return {}
    m = boards[boards["key"] == board_key(d, shift)]
    return m.iloc[-1].to_dict() if not m.empty else {}


def board_notes(boards: pd.DataFrame, d: date, shift: str) -> str:
    return str(board_row(boards, d, shift).get("notes", ""))


def closed_text(row: dict) -> str:
    if not row.get("closed_at"):
        return ""
    t = datetime.strptime(row["closed_at"], "%Y-%m-%d %H:%M:%S")
    return _("Shift ended {t} by {who} — {n} carried to next shift", t=f"{t:%-I:%M %p}", who=row.get("closed_by", ""),
             n=row.get("carried_count") or 0)


def close_deadline(d: date, shift: str) -> datetime:
    h, m, off = CLOSE_BY[shift]
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ) + timedelta(days=off)


def close_reminder(core: dict) -> dict | None:
    """The oldest of the last two shifts that is near/past its end-shift deadline and not ended yet."""
    n = now()
    cd, cs = current_board()
    for d, s in (prev_slot(cd, cs), (cd, cs)):
        dl = close_deadline(d, s)
        if n < dl - timedelta(minutes=CLOSE_WARN_MINUTES) or board_row(core["boards"], d, s).get("closed_at"):
            continue
        if board_items(load_sched(mk(d)), d, s).empty:  # nothing ran (e.g. no production) — nothing to close
            continue
        mins = int(abs((n - dl).total_seconds()) // 60)
        return {"d": d, "s": s, "deadline": dl, "overdue": n >= dl, "mins": mins}
    return None


def reminder_text(r: dict) -> str:
    due = f"{r['deadline']:%-I:%M %p}"
    if r["overdue"]:
        return _("{slot} not ended — was due {due} ({m} min late)", slot=slot_label(r["d"], r["s"]), due=due, m=r["mins"])
    return _("{slot} must be ended by {due} ({m} min left)", slot=slot_label(r["d"], r["s"]), due=due, m=r["mins"])


def month_range(a: date, b: date) -> list[str]:
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f"{y}_{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ── Writes ─────────────────────────────────────────────────────────────────────
def item_log(item: dict, action: str, user: str, from_status=None, to_status=None, reason=None, comment=None) -> dict:
    return {"ts": stamp(), "date": item["date"], "shift": item["shift"], "line": item["line"],
            "item_id": item["id"], "recipe": item["recipe"], "action": action,
            "from_status": item.get("status", "") if from_status is None else from_status,
            "to_status": item.get("status", "") if to_status is None else to_status,
            "reason": item.get("reason", "") if reason is None else reason,
            "comment": item.get("comment", "") if comment is None else comment, "user": user}


def write_logs(entries: list[dict]) -> None:
    by_tab: dict[str, list] = {}
    for e in entries:
        by_tab.setdefault(log_tab(date.fromisoformat(e["date"])), []).append([e[c] for c in LOG_COLS])
    s = store()
    for tab, rows in by_tab.items():
        s.ensure(tab, LOG_COLS)
        s.append(tab, rows)


def update_item(item: dict, changes: dict, user: str, action: str) -> bool:
    s = store()
    tab = sched_tab(date.fromisoformat(item["date"]))
    ids = s.col_values(tab)
    if item["id"] not in ids:
        refresh()
        flash(_("That recipe was removed or changed by someone else — board refreshed."), "warn")
        return False
    r = ids.index(item["id"]) + 1
    t = stamp()
    ch = {**changes, "updated_by": user, "updated_at": t}
    new_status = ch.get("status")
    if new_status == "Running" and not item.get("started_at"):
        ch["started_at"] = t
    if new_status == "Finished":
        ch["finished_at"] = t
    s.update_cells(tab, [(r, SCHED_COLS.index(k) + 1, v) for k, v in ch.items()])
    write_logs([item_log(item, action, user, to_status=new_status or item["status"],
                         reason=ch.get("reason"), comment=ch.get("comment"))])
    refresh()
    return True


def move_item(item: dict, to_line: str, first: bool, target: pd.DataFrame, user: str) -> bool:
    """Move a recipe (with its status, reason and comment) to another line — first or last in that line."""
    s = store()
    tab = sched_tab(date.fromisoformat(item["date"]))
    ids = s.col_values(tab)
    if item["id"] not in ids:
        refresh()
        flash(_("That recipe was removed or changed by someone else — board refreshed."), "warn")
        return False
    col = {c: SCHED_COLS.index(c) + 1 for c in SCHED_COLS}
    cells = []
    if first:
        seq = 1
        for e in target.to_dict("records"):  # push the line's recipes down one
            if e["id"] in ids:
                cells.append((ids.index(e["id"]) + 1, col["seq"], int(e["_seq"]) + 1))
    else:
        seq = int(target["_seq"].max()) + 1 if len(target) else 1
    r, t = ids.index(item["id"]) + 1, stamp()
    cells += [(r, col["line"], to_line), (r, col["seq"], seq), (r, col["updated_by"], user), (r, col["updated_at"], t)]
    s.update_cells(tab, cells)
    write_logs([item_log(item, f"moved to {LINE_LABEL[to_line]}", user)])
    refresh()
    return True


def new_item(d: date, shift: str, line: str, seq: int, recipe: str, qty: str, user: str, t: str) -> dict:
    return {"id": uuid.uuid4().hex[:10], "date": d.isoformat(), "shift": shift, "line": line, "seq": seq,
            "recipe": recipe, "qty": qty, "status": "Ready", "reason": "", "comment": "", "deleted": False,
            "created_by": user, "created_at": t, "updated_by": user, "updated_at": t,
            "started_at": "", "finished_at": "", "carried": False, "carried_from": ""}


def add_items(d: date, shift: str, line: str, entries, user: str, start_seq: int) -> bool:
    s = store()
    tab = sched_tab(d)
    s.ensure(tab, SCHED_COLS)
    t = stamp()
    rows = [new_item(d, shift, line, start_seq + i, rec, qty, user, t) for i, (rec, qty) in enumerate(entries)]
    s.append(tab, [[r[c] for c in SCHED_COLS] for r in rows])
    write_logs([item_log(r, "added", user, from_status="") for r in rows])
    refresh()
    return True


def save_plan(d: date, shift: str, plan: dict, current: pd.DataFrame, user: str) -> tuple[int, int]:
    """Sync the planner's text per line with the sheet. Matches existing recipes so their status is kept."""
    s = store()
    tab = sched_tab(d)
    s.ensure(tab, SCHED_COLS)
    row_of = {v: i + 1 for i, v in enumerate(s.col_values(tab))}
    col = {c: SCHED_COLS.index(c) + 1 for c in SCHED_COLS}
    t = stamp()
    cells, new_rows, logs = [], [], []
    for line, entries in plan.items():
        existing = current[current["line"] == line].to_dict("records") if not current.empty else []
        used = set()
        for seq, (rec, qty) in enumerate(entries, 1):
            m = next((e for e in existing if e["id"] not in used and e["recipe"].strip().upper() == rec), None)
            if m:
                used.add(m["id"])
                r = row_of.get(m["id"])
                if r and str(m["seq"]) != str(seq):
                    cells.append((r, col["seq"], seq))
                if r and str(m["qty"]) != str(qty):
                    cells.append((r, col["qty"], qty))
            else:
                row = new_item(d, shift, line, seq, rec, qty, user, t)
                new_rows.append(row)
                logs.append(item_log(row, "planned", user, from_status=""))
        for e in existing:
            r = row_of.get(e["id"])
            if e["id"] not in used and r:
                cells += [(r, col["deleted"], True), (r, col["updated_by"], user), (r, col["updated_at"], t)]
                logs.append(item_log(e, "removed", user))
    s.update_cells(tab, cells)
    s.append(tab, [[row[c] for c in SCHED_COLS] for row in new_rows])
    write_logs(logs)
    return len(new_rows), sum(1 for e in logs if e["action"] == "removed")


def upsert(tab: str, cols: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    s = store()
    s.ensure(tab, cols)
    idx = {k: i + 1 for i, k in enumerate(s.col_values(tab))}
    cells, new = [], []
    for row in rows:
        if row["key"] in idx:  # existing row: only touch the fields given
            cells += [(idx[row["key"]], cols.index(k) + 1, v) for k, v in row.items() if k in cols and k != "key"]
        else:
            new.append([row.get(c, "") for c in cols])
    s.update_cells(tab, cells)
    s.append(tab, new)


def carry_over(d: date, shift: str, items: pd.DataFrame, answers: dict, user: str) -> tuple[int, date, str]:
    """End of shift: save reasons, copy every unfinished recipe (with its data) to the top of the next board."""
    nd, ns = next_slot(d, shift)
    s = store()
    src, dst = sched_tab(d), sched_tab(nd)
    s.ensure(src, SCHED_COLS)
    s.ensure(dst, SCHED_COLS)
    refresh()
    col = {c: SCHED_COLS.index(c) + 1 for c in SCHED_COLS}
    t = stamp()
    src_rows = {v: i + 1 for i, v in enumerate(s.col_values(src))}
    dst_rows = src_rows if dst == src else {v: i + 1 for i, v in enumerate(s.col_values(dst))}
    nxt = board_items(load_sched(mk(nd)), nd, ns)
    src_cells, dst_cells, new_rows, logs = [], [], [], []
    for line in LINE_IDS:
        li = items[items["line"] == line] if not items.empty else items
        if li.empty:
            continue
        n = len(li)
        if not nxt.empty:  # push what's already planned on the next board below the carry-over
            for e in nxt[nxt["line"] == line].to_dict("records"):
                if e["id"] in dst_rows:
                    dst_cells.append((dst_rows[e["id"]], col["seq"], int(e["_seq"]) + n))
        for seq, it in enumerate(li.to_dict("records"), 1):
            reason, comment = answers[it["id"]]
            r = src_rows.get(it["id"])
            if r:
                src_cells += [(r, col["reason"], reason), (r, col["comment"], comment), (r, col["carried"], True)]
            logs.append(item_log(it, "carried over", user, reason=reason, comment=comment))
            row = new_item(nd, ns, line, seq, it["recipe"], it["qty"], user, t)
            row.update(status=it["status"], reason=reason, comment=comment, carried_from=it["id"],
                       started_at=t if it["status"] == "Running" else "")
            new_rows.append(row)
    s.update_cells(src, src_cells)
    s.update_cells(dst, dst_cells)
    s.append(dst, [[row[c] for c in SCHED_COLS] for row in new_rows])
    write_logs(logs)
    # line flags + notes follow the board unless the next board already has its own
    core = load_core()
    flags, nflags = line_flags(core["lineday"], d, shift), line_flags(core["lineday"], nd, ns)
    desired = {**{ln: f for ln, f in flags.items()}, **nflags}
    save_flags(nd, ns, desired, nflags, user)
    notes, nrow = board_notes(core["boards"], d, shift), board_row(core["boards"], nd, ns)
    rows = [{"key": board_key(d, shift), "date": d.isoformat(), "shift": shift, "closed_by": user, "closed_at": t,
             "carried_count": int(str(board_row(core["boards"], d, shift).get("carried_count") or 0) or 0) + len(new_rows)}]
    if notes and not nrow.get("notes"):
        rows.append({"key": board_key(nd, ns), "date": nd.isoformat(), "shift": ns, "notes": notes,
                     "updated_by": user, "updated_at": t})
    upsert("boards", BOARD_COLS, rows)
    refresh()
    return len(new_rows), nd, ns


def save_flags(d: date, shift: str, desired: dict, current: dict, user: str) -> bool:
    rows = []
    for line in LINE_IDS:
        want, have = desired.get(line, {}), current.get(line, {})
        reason = (want.get("down_reason") or "") if want.get("down") else ""
        if any(bool(want.get(k)) != bool(have.get(k)) for k in FLAG_KEYS) or reason != (have.get("down_reason") or ""):
            rows.append({"key": line_key(d, shift, line), "date": d.isoformat(), "shift": shift, "line": line,
                         **{k: bool(want.get(k)) for k in FLAG_KEYS}, "down_reason": reason,
                         "updated_by": user, "updated_at": stamp()})
    upsert("lineday", LINEDAY_COLS, rows)
    return True


def clear_board(d: date, shift: str, user: str) -> int:
    """Start from scratch: remove every recipe, line down / priority and the notes from one shift's board.
    Recipes are marked deleted (not erased) and each one is logged, so nothing is lost from the record."""
    refresh()
    items = board_items(load_sched(mk(d)), d, shift)
    core = load_core()
    flags, notes = line_flags(core["lineday"], d, shift), board_notes(core["boards"], d, shift)
    s, n, t = store(), 0, stamp()
    if not items.empty:
        tab = sched_tab(d)
        row_of = {v: i + 1 for i, v in enumerate(s.col_values(tab))}
        col = {c: SCHED_COLS.index(c) + 1 for c in SCHED_COLS}
        cells, logs = [], []
        for e in items.to_dict("records"):
            r = row_of.get(e["id"])
            if r:
                cells += [(r, col["deleted"], True), (r, col["updated_by"], user), (r, col["updated_at"], t)]
                logs.append(item_log(e, "cleared (board started from scratch)", user))
        s.update_cells(tab, cells)
        write_logs(logs)
        n = len(logs)
    if any(f.get("down") or f.get("priority") for f in flags.values()):
        save_flags(d, shift, {}, flags, user)
    if notes.strip():
        upsert("boards", BOARD_COLS, [{"key": board_key(d, shift), "date": d.isoformat(), "shift": shift,
                                       "notes": "", "updated_by": user, "updated_at": t}])
    refresh()
    return n


def safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # Sheets/API errors shouldn't crash the floor tablet
        flash(_("Couldn't save — {e}", e=e), "err")
        return None


# ── Board rendering (HTML) ─────────────────────────────────────────────────────
BOARD_CSS = """<style>
.sb{font-family:"Source Sans Pro","Source Sans 3",system-ui,sans-serif;color:#1f2a37}
.sb-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;margin-bottom:10px}
.sb-title{font-size:22px;font-weight:800;letter-spacing:-.01em}
.sb-title span{font-weight:600;color:#5f6b7a;font-size:15px;margin-left:6px}
.kpis{display:flex;gap:6px;flex-wrap:wrap}
.kpi{border-radius:999px;padding:2px 10px;font-size:13px;font-weight:700;border:1.5px solid;white-space:nowrap}
.kpi.alert{background:#d93025;border-color:#d93025;color:#fff;animation:sbp 1.6s infinite}
.live{margin-left:auto;font-size:12px;color:#5f6b7a;white-space:nowrap}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#188038;margin-right:5px;animation:sbp 2s infinite}
@keyframes sbp{50%{opacity:.25}}
.sb{container-type:inline-size}
.sb-main{display:grid;grid-template-columns:1fr;gap:10px;align-items:start}
.sb-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(165px,1fr));gap:8px}
.sb-side{display:grid;grid-template-columns:repeat(auto-fill,minmax(165px,1fr));gap:8px;align-items:start}
.card{background:#fff;border:1px solid #d6dbe2;border-radius:8px;padding:6px 6px 8px;min-height:90px;box-shadow:0 1px 2px rgba(16,24,40,.05)}
.sb-side .card{min-height:0}
@container (min-width:1000px){
 .sb-main{grid-template-columns:minmax(0,1fr) 230px}
 .sb-grid{grid-template-columns:repeat(5,minmax(0,1fr))}
 .sb-side{display:flex;flex-direction:column;align-items:stretch}}
@container (min-width:1750px){
 .sb-grid{grid-template-columns:repeat(10,minmax(0,1fr))}
 .card{min-height:170px}}
.card.prio{border:2px solid #c62828}
.card.down{background:repeating-linear-gradient(135deg,#f6f7f9 0 10px,#eceff3 10px 20px)}
.ch{display:flex;justify-content:space-between;align-items:center;gap:4px;margin-bottom:4px}
.lbl{color:#c62828;border:1.5px solid #c62828;border-radius:3px;font-weight:800;font-size:12px;padding:1px 6px;text-transform:uppercase;background:#fff;white-space:nowrap}
.cnt{font-size:11px;color:#5f6b7a;font-weight:700}
.badges{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:2px}
.badge{font-size:9.5px;font-weight:800;border-radius:3px;padding:1px 5px;letter-spacing:.03em;color:#fff}
.badge.prio{background:#c62828}
.downx{font-size:20px;font-weight:900;color:#9aa3ae;text-align:center;padding:16px 0 2px}
.downr{font-size:12px;font-weight:800;color:#a50e0e;text-align:center;padding-bottom:6px}
.item{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:0 6px;border:1.5px solid;border-radius:6px;padding:3px 6px;margin-top:4px;font-weight:700;font-size:14px;line-height:1.25}
.item .rec{white-space:nowrap}
.item .tag{font-size:8.5px;font-weight:800;letter-spacing:.05em;white-space:nowrap}
.item.s-running{box-shadow:0 0 0 2px rgba(24,128,56,.28)}
.item.s-finished .rec{text-decoration:line-through}
.qty{font-weight:600;opacity:.8}
.cmt{font-size:11px;color:#5f6b7a;margin:2px 0 0 3px;line-height:1.25}
.cmt b{color:#a50e0e}
.empty{color:#b3bac4;text-align:center;padding-top:22px}
.goal{display:flex;justify-content:space-between;gap:6px;font-size:13px;padding:3px 0;border-bottom:1px solid #f0f2f5}
.goal b{color:#1a56db;white-space:nowrap}
.sb-foot{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px;margin-top:10px}
.panel{background:#fff;border:1px solid #d6dbe2;border-radius:8px;padding:8px 10px}
.ph{margin:0 0 6px;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.07em;color:#5f6b7a}
.notes{white-space:pre-wrap;font-size:14px;line-height:1.4}
.feed div{font-size:13px;padding:3px 0;border-bottom:1px solid #f0f2f5}
.feed time{color:#5f6b7a;font-variant-numeric:tabular-nums;margin-right:6px}
.feed .who{color:#8a94a3}
.legend{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.legend span{font-size:11px;font-weight:700;border:1.5px solid;border-radius:5px;padding:1px 6px}
.sb.tv .item{font-size:17px}.sb.tv .lbl{font-size:14px}.sb.tv .sb-title{font-size:28px}.sb.tv .cmt{font-size:12.5px}
@container (max-width:760px){.sb-foot{grid-template-columns:1fr}.live{margin-left:0}}
</style>"""

TV_CSS = """<style>
[data-testid="stSidebar"],[data-testid="stSidebarCollapsedControl"],[data-testid="stHeader"],
[data-testid="stToolbar"],[data-testid="stDecoration"],footer{display:none!important}
.block-container,[data-testid="stMainBlockContainer"]{padding:.6rem 1rem!important;max-width:100%!important}
</style>"""


def _card(line: str, items: pd.DataFrame, flags: dict) -> str:
    e = html.escape
    f = flags.get(line, {})
    li = items[items["line"] == line] if not items.empty else items
    cls = "card" + (" down" if f.get("down") else "") + (" prio" if f.get("priority") else "")
    n, done = len(li), int((li["status"] == "Finished").sum()) if len(li) else 0
    out = [f'<div class="{cls}"><div class="ch"><span class="lbl">{e(line_name(line))}</span>'
           f'{f"<span class=cnt>{done}/{n}</span>" if n else ""}</div>']
    badges = f'<span class="badge prio">{_("★ PRIORITY")}</span>' if f.get("priority") else ""
    if badges:
        out.append(f'<div class="badges">{badges}</div>')
    if f.get("down"):
        out.append(f'<div class="downx">{_("✕ DOWN")}</div>')
        if f.get("down_reason"):
            out.append(f'<div class="downr">{e(_(f["down_reason"]))}</div>')
    for it in li.to_dict("records"):
        status = it["status"] if it["status"] in STATUS_STYLE else "Ready"
        bg, bd, fg, tag = STATUS_STYLE[status]
        qty = f' <span class="qty">×{e(it["qty"])}</span>' if it["qty"] else ""
        out.append(f'<div class="item s-{status.lower().replace(" ", "-")}" style="background:{bg};border-color:{bd};color:{fg}">'
                   f'<span class="rec">{e(it["recipe"])}{qty}</span><span class="tag">{_(tag)}{" ↪" if truthy(it["carried"]) else ""}</span></div>')
        sub = ([f"<b>{e(_(it['reason']))}</b>"] if it["reason"] else []) + ([e(it["comment"])] if it["comment"] else [])
        if sub:
            out.append(f'<div class="cmt">{" · ".join(sub)}</div>')
    if not n and not f.get("down"):
        out.append('<div class="empty">—</div>')
    out.append("</div>")
    return "".join(out)


def render_board(d: date, shift: str, items: pd.DataFrame, flags: dict, notes: str, goals: pd.DataFrame, tv: bool,
                 closed: str = "", alert: str = "") -> str:
    e = html.escape
    st_counts = items["status"].value_counts() if not items.empty else pd.Series(dtype=int)
    total = len(items)
    kpis = [("Finished", _("✅ {a}/{b} done", a=int(st_counts.get("Finished", 0)), b=total)),
            ("Running", _("🟢 {n} running", n=int(st_counts.get("Running", 0)))),
            ("Next", _("🟡 {n} next", n=int(st_counts.get("Next", 0)))),
            ("Not ready", _("🔴 {n} not ready", n=int(st_counts.get("Not ready", 0))))]
    downs = sum(1 for f in flags.values() if f.get("down"))
    kpi_html = "".join(f'<span class="kpi" style="background:{STATUS_STYLE[s][0]};border-color:{STATUS_STYLE[s][1]};'
                       f'color:{STATUS_STYLE[s][2]}">{t}</span>' for s, t in kpis)
    if downs:
        kpi_html += (f'<span class="kpi" style="background:#f1f3f6;border-color:#9aa3ae;color:#4b5563">'
                     f'{_("✕ {n} lines down" if downs > 1 else "✕ {n} line down", n=downs)}</span>')
    if closed:
        kpi_html += f'<span class="kpi" style="background:#1f2a37;border-color:#1f2a37;color:#fff">↪ {e(closed)}</span>'
    if alert:
        kpi_html += f'<span class="kpi alert">⏰ {e(alert)}</span>'

    goal_rows = "".join(f'<div class="goal"><span>{e(_(str(g["label"])))}</span><b>{e(str(g["value"]))}</b></div>'
                        for g in goals.to_dict("records")) if not goals.empty else '<div class="empty">—</div>'
    side = "".join(_card(ln, items, flags) for ln in SIDE_LINES) + \
        f'<div class="panel"><div class="ph">{_("Daily goals")}</div>{goal_rows}</div>'

    feed_items = items[(items["updated_at"] != items["created_at"])] if not items.empty else items
    feed_items = feed_items.sort_values("updated_at", ascending=False).head(10) if not feed_items.empty else feed_items
    feed = "".join(
        f'<div><time>{e(r["updated_at"][11:16])}</time><b>{e(line_name(r["line"]))}</b> · '
        f'{e(r["recipe"])} → {ICON.get(r["status"], "")} {e(_(r["status"]))}'
        f'{" — " + e(r["comment"]) if r["comment"] else ""} <span class="who">({e(r["updated_by"])})</span></div>'
        for r in feed_items.to_dict("records")) or f'<div class="empty" style="padding:6px 0">{_("No updates yet")}</div>'
    legend = "".join(f'<span style="background:{STATUS_STYLE[s][0]};border-color:{STATUS_STYLE[s][1]};'
                     f'color:{STATUS_STYLE[s][2]}">{_(s)}</span>' for s in STATUSES)

    return (f'{BOARD_CSS}<div class="sb{" tv" if tv else ""}">'
            f'<div class="sb-head"><div class="sb-title">{_("Schedule Board")}<span>{e(slot_label(d, shift))}</span></div>'
            f'<div class="kpis">{kpi_html}</div>'
            f'<div class="live"><span class="dot"></span>{_("Live · updated {t}", t=now().strftime("%-I:%M:%S %p"))}</div></div>'
            f'<div class="sb-main"><div class="sb-grid">{"".join(_card(ln, items, flags) for ln in GRID_LINES)}</div>'
            f'<div class="sb-side">{side}</div></div>'
            f'<div class="sb-foot"><div class="panel"><div class="ph">{_("Notes")}</div><div class="notes">{e(notes) or "—"}</div>'
            f'<div class="legend">{legend}</div></div>'
            f'<div class="panel feed"><div class="ph">{_("Latest from the floor")}</div>{feed}</div></div></div>')


# ── Shared UI pieces ───────────────────────────────────────────────────────────
def board_picker(key: str) -> tuple[date, str]:
    cd, cs = current_board()
    c1, c2, _sp = st.columns([1.3, 1, 4])
    d = c1.date_input(_("Date"), cd, key=f"{key}_date", format="MM/DD/YYYY")
    prev = st.session_state.get(f"{key}_shift_val", cs)  # keep the pick when the language (and widget key) changes
    shift = c2.selectbox(_("Shift"), SHIFTS, index=SHIFTS.index(prev) if prev in SHIFTS else 0,
                         key=f"{key}_shift_{lang()}", format_func=shift_name)
    st.session_state[f"{key}_shift_val"] = shift
    return d, shift


def login_form(core: dict) -> None:
    leads = core["leads"]
    if not leads.empty:
        leads = leads[leads["active"].map(lambda x: str(x).strip() == "" or truthy(x))]
    names = sorted(leads["name"].astype(str).str.strip().unique().tolist()) if not leads.empty else []
    if not names:
        st.info(_("No leads set up yet. Add people to the **leads** tab of the Google Sheet: "
                  "`name`, `pin`, `role` (lead / planner), `lines` (e.g. `1, 2, Cole Slaw` or `ALL`), `active` (TRUE)."))
        return
    with st.form("login", border=True):
        st.markdown(_("**Sign in to update the board**"))
        name = st.selectbox(_("Name"), names, index=None, placeholder=_("Pick your name"))
        pin = st.text_input("PIN", type="password", max_chars=12)
        if st.form_submit_button(_("Sign in"), type="primary"):
            row = leads[leads["name"].astype(str).str.strip() == name] if name else leads.iloc[0:0]
            if not row.empty and pin_ok(pin, row.iloc[0]["pin"]):
                exp = int(time.time() + SESSION_HOURS * 3600)
                u = make_user(row.iloc[0], exp, make_token(name, row.iloc[0]["pin"], exp))
                st.session_state["user"] = u
                st.query_params["s"] = u["token"]
                st.rerun()
            st.error(_("Wrong name or PIN."))


# ── Staying signed in: a signed token in the URL (?s=…) survives refresh for SESSION_HOURS ──
@st.cache_resource
def session_secret() -> bytes:
    try:
        return hashlib.sha256(str(st.secrets["gcp_service_account"]["private_key"]).encode()).digest()
    except Exception:
        return os.urandom(32)  # demo mode: sign-ins last until the app restarts


def _sig(payload: str, pin) -> str:
    return hmac.new(session_secret(), f"{payload}|{str(pin).strip()}".encode(), hashlib.sha256).hexdigest()[:24]


def make_token(name: str, pin, exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"n": name, "e": exp}).encode()).decode().rstrip("=")
    return f"{payload}.{_sig(payload, pin)}"


def make_user(row, exp: int, token: str) -> dict:
    role = str(row["role"]).strip().lower() or "lead"
    return {"name": str(row["name"]).strip(), "role": role, "exp": exp, "token": token,
            "lines": list(LINE_IDS) if role in PLANNER_ROLES else allowed_lines(row["lines"])}


def user_from_token(tok: str, leads: pd.DataFrame) -> dict | None:
    """Valid only if not expired, the person is still active, and their PIN hasn't changed."""
    try:
        payload, sig = tok.rsplit(".", 1)
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if int(data["e"]) < time.time() or leads.empty:
            return None
        rows = leads[(leads["name"].astype(str).str.strip() == data["n"])
                     & leads["active"].map(lambda x: str(x).strip() == "" or truthy(x))]
        if rows.empty or not hmac.compare_digest(sig, _sig(payload, rows.iloc[0]["pin"])):
            return None
        return make_user(rows.iloc[0], int(data["e"]), tok)
    except Exception:
        return None


def keep_signed_in() -> None:
    ss, qp = st.session_state, st.query_params
    u = ss.get("user")
    if u and time.time() > u["exp"]:
        ss.pop("user", None)
        qp.pop("s", None)
        flash(_("Your sign-in expired — sign in again."), "warn")
        return
    if not u and qp.get("s"):
        u = user_from_token(qp["s"], load_core()["leads"])
        if u:
            ss["user"] = u
        else:
            qp.pop("s", None)
    if u and qp.get("s") != u["token"]:
        qp["s"] = u["token"]  # keep the token in the URL on every page so a refresh stays signed in


def need_user(core: dict, roles=None, denied: str = "Only planners can set up the board. Use **Update** to change recipe status.") -> dict | None:
    u = st.session_state.get("user")
    if not u:
        login_form(core)
        return None
    if roles and u["role"] not in roles:
        st.warning(_(denied))
        return None
    return u


# ── Pages ──────────────────────────────────────────────────────────────────────
@st.fragment(run_every=REFRESH_SECONDS)
def live_board(d: date | None, shift: str | None, tv: bool) -> None:
    if d is None:
        d, shift = current_board()  # TV follows the clock
    core, sched = load_core(), load_sched(mk(d))
    r = close_reminder(core)
    st.html(render_board(d, shift, board_items(sched, d, shift), line_flags(core["lineday"], d, shift),
                         board_notes(core["boards"], d, shift), core["goals"], tv,
                         closed_text(board_row(core["boards"], d, shift)), reminder_text(r) if r else ""))


def board_page() -> None:
    if is_tv():
        st.html(TV_CSS)
        live_board(None, None, True)
        return
    d, shift = board_picker("board")
    live_board(d, shift, False)
   


def on_status(item: dict, key: str, user: str) -> None:
    new = st.session_state.get(key)
    if not new or new == item["status"]:
        return
    if new == "Not ready":  # reason is required — ask in a dialog before saving
        st.session_state["_notready"] = {"item": item, "key": key, "user": user}
        return
    ch = {"status": new}
    if item["status"] == "Not ready":
        ch["reason"] = ""
    if safe(update_item, item, ch, user, "status"):
        flash(_("{r} → {s}", r=item["recipe"], s=_(new)))


def status_key(it: dict) -> str:
    return f"st_{it['id']}_{it['updated_at']}_{st.session_state.get('nonce_' + it['id'], 0)}_{lang()}"


def reset_status_widget(item_id: str) -> None:
    """Give the status buttons a fresh key so an unsaved pick snaps back to the saved status."""
    st.session_state["nonce_" + item_id] = st.session_state.get("nonce_" + item_id, 0) + 1


def cancel_notready() -> None:
    p = st.session_state.pop("_notready", None)
    if p:
        reset_status_widget(p["item"]["id"])


def notready_dialog() -> None:
    st.dialog(_("Why is it not ready?"), on_dismiss=cancel_notready)(_notready_body)()


def _notready_body() -> None:
    p = st.session_state.get("_notready")
    if not p:
        return
    it = p["item"]
    st.markdown(f"**{line_name(it['line'])} · {it['recipe']}**")
    reason = st.selectbox(_("Reason (required)"), REASONS, index=None, placeholder=_("Pick a reason"), format_func=_)
    comment = st.text_area(_("Comment (optional)"), it["comment"], height=80)
    c1, c2 = st.columns(2)
    if c1.button(_("Save"), type="primary", width="stretch"):
        if not reason:
            st.error(_("Pick a reason first."))
            return
        st.session_state.pop("_notready", None)
        if safe(update_item, it, {"status": "Not ready", "reason": reason, "comment": comment.strip()},
                p["user"], "status"):
            flash(_("{r} → Not ready ({why})", r=it["recipe"], why=_(reason)))
        st.rerun()
    if c2.button(_("Cancel"), width="stretch"):
        cancel_notready()
        st.rerun()


def on_toggle_down(d: date, shift: str, line: str, key: str, flags: dict, user: str) -> None:
    if st.session_state.get(key):  # turning a line down needs a reason — ask first
        st.session_state["_down_pending"] = {"d": d, "shift": shift, "line": line, "flags": flags, "user": user}
        return
    desired = {ln: dict(v) for ln, v in flags.items()}
    desired.setdefault(line, {}).update(down=False, down_reason="")
    safe(save_flags, d, shift, desired, flags, user)
    refresh()


def cancel_down() -> None:
    p = st.session_state.pop("_down_pending", None)
    if p:
        reset_status_widget(f"down_{p['line']}")


def down_dialog() -> None:
    st.dialog(_("Why is the line down?"), on_dismiss=cancel_down)(_down_body)()


def _down_body() -> None:
    p = st.session_state.get("_down_pending")
    if not p:
        return
    st.markdown(f"**{line_name(p['line'])}** · {slot_label(p['d'], p['shift'])}")
    reason = st.radio(_("Reason (required)"), DOWN_REASONS, index=None, format_func=_)
    c1, c2 = st.columns(2)
    if c1.button(_("Save"), type="primary", width="stretch"):
        if not reason:
            st.error(_("Pick a reason first."))
            return
        st.session_state.pop("_down_pending", None)
        desired = {ln: dict(v) for ln, v in p["flags"].items()}
        desired.setdefault(p["line"], {}).update(down=True, down_reason=reason)
        if safe(save_flags, p["d"], p["shift"], desired, p["flags"], p["user"]):
            flash(_("{line} is down — {why}", line=line_name(p["line"]), why=_(reason)))
        refresh()
        st.rerun()
    if c2.button(_("Cancel"), width="stretch"):
        cancel_down()
        st.rerun()


def item_row(it: dict, user: str, items: pd.DataFrame) -> None:
    c1, c3, c4 = st.columns([4, 1.5, 1.1], vertical_alignment="center")
    c1.markdown(f"**{it['recipe']}**" + (f" ×{it['qty']}" if it["qty"] else "") + (" ↪" if truthy(it["carried"]) else ""))
    sub = " · ".join(x for x in (_(it["reason"]) if it["reason"] else "", it["comment"]) if x)
    if sub:
        c1.caption(sub)
    key = status_key(it)
    pending = st.session_state.get("_notready")
    if key in st.session_state and st.session_state[key] != it["status"] and not (pending and pending["key"] == key):
        reset_status_widget(it["id"])  # an unsaved pick (e.g. cancelled Not ready) snaps back
        key = status_key(it)
    st.segmented_control(_("Status"), STATUSES, default=it["status"] if it["status"] in STATUSES else None,
                         format_func=lambda s: f"{ICON[s]} {_(s)}", key=key, label_visibility="collapsed",
                         on_change=on_status, args=(it, key, user))
    with c3.popover(_("💬 Comments"), help=_("Reason and comment — carried to the next shift")):
        with st.form(f"note_{it['id']}_{it['updated_at']}", border=False):
            st.markdown(_("**Comments · {r}**", r=it["recipe"]))
            reason = st.selectbox(_("Reason"), REASONS, index=REASONS.index(it["reason"]) if it["reason"] in REASONS else None,
                                  placeholder=_("Pick a reason"), format_func=_)
            comment = st.text_area(_("Comment"), it["comment"], height=80, placeholder=_("What happened, what's needed…"))
            if st.form_submit_button(_("Save"), type="primary"):
                if it["status"] == "Not ready" and not reason:
                    st.error(_("Not ready needs a reason."))
                    return
                if safe(update_item, it, {"reason": reason or "", "comment": comment.strip()}, user, "note"):
                    flash(_("Comments saved on {r}", r=it["recipe"]))
                st.rerun()
    with c4.popover(_("↔ Move"), help=_("Move this recipe to another line")):
        with st.form(f"move_{it['id']}_{it['updated_at']}", border=False):
            st.markdown(_("**Move {r}** from {line}", r=it["recipe"], line=line_name(it["line"])))
            to = st.selectbox(_("To line"), [ln for ln in LINE_IDS if ln != it["line"]], index=None,
                              format_func=line_name, placeholder=_("Pick a line"))
            first = st.radio(_("Put it"), ["Last in line", "First in line"], horizontal=True,
                             format_func=_) == "First in line"
            if st.form_submit_button(_("Move"), type="primary"):
                if not to:
                    st.error(_("Pick a line."))
                    return
                if safe(move_item, it, to, first, items[items["line"] == to], user):
                    flash(_("{r} moved to {line}", r=it["recipe"], line=line_name(to)))
                st.rerun()


def carryover_dialog(d: date, shift: str, user: str) -> None:
    st.dialog(_("End shift & carry over"), width="large")(_carryover_body)(d, shift, user)


def _carryover_body(d: date, shift: str, user: str) -> None:
    items = board_items(load_sched(mk(d)), d, shift)
    todo = items[(items["status"] != "Finished") & (~items["carried"].map(truthy))] if not items.empty else items
    nd, ns = next_slot(d, shift)
    if todo.empty:
        st.success(_("Everything on this board is finished or already carried over."))
        if not board_row(load_core()["boards"], d, shift).get("closed_at"):
            if st.button(_("🔚 End shift"), type="primary"):
                if safe(carry_over, d, shift, todo, {}, user):
                    flash(_("{slot} ended", slot=slot_label(d, shift)))
                st.rerun()
        return
    st.markdown(_("These **{n}** recipes didn't finish. They'll move to the top of **{slot}** with their status, reason "
                  "and comment. Finished recipes stay on this shift's record.", n=len(todo), slot=slot_label(nd, ns)))
    with st.form("carryover_form", border=False):
        for line in LINE_IDS:
            li = todo[todo["line"] == line]
            if li.empty:
                continue
            st.markdown(f"**{line_name(line)}**")
            for it in li.to_dict("records"):
                c1, c2, c3 = st.columns([1.6, 2, 3], vertical_alignment="center")
                c1.markdown(f"{ICON.get(it['status'], '')} {it['recipe']}" + (f" ×{it['qty']}" if it["qty"] else ""))
                c2.selectbox(_("Reason"), REASONS, key=f"co_r_{it['id']}", label_visibility="collapsed",
                             index=REASONS.index(it["reason"]) if it["reason"] in REASONS else None,
                             placeholder=_("Reason (required)"), format_func=_)
                c3.text_input(_("Comment"), it["comment"], key=f"co_c_{it['id']}", label_visibility="collapsed",
                              placeholder=_("Comment (optional)"))
        go = st.form_submit_button(_("↪ Carry over to {slot}", slot=slot_label(nd, ns)), type="primary")
    if go:
        ss = st.session_state
        missing = [f"{line_name(r['line'])} · {r['recipe']}" for r in todo.to_dict("records") if not ss.get(f"co_r_{r['id']}")]
        if missing:
            st.error(_("Pick a reason for every recipe — missing {n}: ", n=len(missing)) + ", ".join(missing[:8])
                     + ("…" if len(missing) > 8 else ""))
            return
        answers = {r["id"]: (ss[f"co_r_{r['id']}"], ss.get(f"co_c_{r['id']}", "").strip()) for r in todo.to_dict("records")}
        res = safe(carry_over, d, shift, todo, answers, user)
        if res:
            n, rd, rs = res
            flash(_("Shift ended — {n} recipes carried to {slot}", n=n, slot=slot_label(rd, rs)))
        st.rerun()


def snooze(r: dict, minutes: int = 5) -> None:
    st.session_state.setdefault("_close_snooze", {})[board_key(r["d"], r["s"])] = now() + timedelta(minutes=minutes)


def snoozed(r: dict) -> bool:
    until = st.session_state.get("_close_snooze", {}).get(board_key(r["d"], r["s"]))
    return bool(until and now() < until)


def dismiss_reminder() -> None:
    r = st.session_state.get("_reminder")
    if r:
        snooze(r)


def close_reminder_dialog() -> None:
    st.dialog(_("⏰ End the shift"), on_dismiss=dismiss_reminder)(_close_reminder_body)()


def _close_reminder_body() -> None:
    r = st.session_state["_reminder"]
    due = f"{r['deadline']:%-I:%M %p}"
    if r["overdue"]:
        st.error(_("**{slot}** hasn't been ended. It was due at **{due}** — {m} min late.",
                   slot=slot_label(r["d"], r["s"]), due=due, m=r["mins"]))
    else:
        st.warning(_("**{slot}** must be ended by **{due}** — {m} min left.",
                     slot=slot_label(r["d"], r["s"]), due=due, m=r["mins"]))
    st.markdown(_("Press **End shift & carry over** so the next shift gets its board with everything that didn't "
                  "finish and why."))
    c1, c2 = st.columns(2)
    if c1.button(_("🔚 End shift now"), type="primary", width="stretch"):
        snooze(r, 10)
        st.session_state["_open_carryover"] = (r["d"], r["s"])
        st.rerun()
    if c2.button(_("Remind me in 5 min"), width="stretch"):
        snooze(r)
        st.rerun()


@st.fragment(run_every=60)
def reminder_watch() -> None:
    """Pops the end-shift reminder on an idle Update page when it comes due."""
    ss = st.session_state
    if ss.get("_reminder_open") or "_notready" in ss or "_down_pending" in ss:
        return
    r = close_reminder(load_core())
    if r and not snoozed(r):
        st.rerun(scope="app")


def lead_page() -> None:
    st.title(_("✏️ Update the floor"))
    core = load_core()
    u = need_user(core)
    if not u:
        return
    d, shift = board_picker("lead")
    sched = load_sched(mk(d))
    items = board_items(sched, d, shift)
    flags = line_flags(core["lineday"], d, shift)
    closed = closed_text(board_row(core["boards"], d, shift))
    mine = u["lines"]
    if not mine:
        st.warning(_("No lines could be read for you. In the **leads** tab, column **lines** should look like "
                     "`1-9`, `7, 8, 9` or `ALL` (format the column as Plain text). Then sign out and back in."))
        return
    with_items = [ln for ln in mine if not items.empty and (items["line"] == ln).any()]
    default = mine if len(mine) <= 6 else (with_items or mine)
    c2, c3, _sp2 = st.columns([1, 1.6, 2], vertical_alignment="bottom")
    show = st.multiselect(_("Lines"), mine, default=default, format_func=line_name, key=f"lead_lines_{u['name']}_{lang()}")
    if c2.button(_("🔄 Refresh"), width="stretch"):
        refresh()
        st.rerun()
    ss = st.session_state
    ss["_reminder_open"] = False
    goto = ss.pop("_open_carryover", None)
    rem = close_reminder(core)
    if c3.button(_("🔚 End shift & carry over"), type="primary", width="stretch",
                 help=_("At the end of the shift: everything not Finished moves to the next shift's board")):
        cancel_notready()
        if rem:
            snooze(rem, 10)
        carryover_dialog(d, shift, u["name"])
    elif goto:
        cancel_notready()
        carryover_dialog(goto[0], goto[1], u["name"])
    elif "_notready" in ss:
        notready_dialog()
    elif "_down_pending" in ss:
        down_dialog()
    elif rem and not snoozed(rem):
        ss["_reminder_open"], ss["_reminder"] = True, rem
        close_reminder_dialog()
    if closed:
        st.info(f"↪ {closed}")
    if items.empty:
        st.info(_("Nothing is on the board for this shift yet. A planner sets it up on the **Plan** page — "
                  "or add recipes below."))
    for line in show:
        li = items[items["line"] == line] if not items.empty else items
        f = flags.get(line, {})
        with st.container(border=True):
            h1, h2 = st.columns([3, 1.3], vertical_alignment="center")
            tags = (_(" · ★ priority") if f.get("priority") else "") + \
                   (_(" · ✕ down: {r}", r=_(f["down_reason"])) if f.get("down") and f.get("down_reason") else "")
            done = int((li["status"] == "Finished").sum()) if len(li) else 0
            h1.markdown(f"#### {line_name(line)} <span style='font-size:.8rem;color:#5f6b7a'>"
                        f"{_('{a}/{b} done', a=done, b=len(li))}{tags}</span>", unsafe_allow_html=True)
            dk = f"down_{line}_{d}_{shift}_{f.get('down', False)}_{st.session_state.get('nonce_down_' + line, 0)}_{lang()}"
            h2.toggle(_("Line down"), value=f.get("down", False), key=dk,
                      on_change=on_toggle_down, args=(d, shift, line, dk, flags, u["name"]))
            for it in li.to_dict("records"):
                item_row(it, u["name"], items)
            with st.form(f"add_{line}_{d}_{shift}", clear_on_submit=True, border=False):
                a1, a2 = st.columns([5, 1], vertical_alignment="bottom")
                txt = a1.text_input(_("Add recipe"), placeholder=_("Add recipe — e.g. PO 379 or CS 131 x6"),
                                    label_visibility="collapsed")
                if a2.form_submit_button(_("➕ Add")):
                    entries = parse_entries(txt)
                    if entries:
                        start = int(pd.to_numeric(li["seq"], errors="coerce").fillna(0).max()) + 1 if len(li) else 1
                        if safe(add_items, d, shift, line, entries, u["name"], start):
                            flash(_("Added {n} to {line}", n=len(entries), line=line_name(line)))
                        st.rerun()
    reminder_watch()


def plan_keys(tag: str) -> list[str]:
    return ([f"plan_{tag}_{ln}" for ln in LINE_IDS] + [f"plan_{tag}_down_{ln}" for ln in LINE_IDS]
            + [f"plan_{tag}_priority", f"plan_{tag}_notes"])


def fill_plan(tag: str, items: pd.DataFrame, flags: dict, notes: str, overwrite: bool, copy_down=True) -> None:
    ss = st.session_state
    values = {f"plan_{tag}_{ln}": entries_text(items[items["line"] == ln]) if not items.empty else "" for ln in LINE_IDS}
    values[f"plan_{tag}_priority"] = [ln for ln in LINE_IDS if flags.get(ln, {}).get("priority")]
    for ln in LINE_IDS:
        f = flags.get(ln, {})
        values[f"plan_{tag}_down_{ln}"] = (f.get("down_reason") or DOWN_REASONS[0]) if copy_down and f.get("down") else None
    values[f"plan_{tag}_notes"] = notes
    for k, v in values.items():
        if overwrite or k not in ss:
            ss[k] = v


def find_previous_board(d: date, shift: str, look_back: int = 28):
    cd, cs = d, shift
    for _ in range(look_back):
        cd, cs = prev_slot(cd, cs)
        it = board_items(load_sched(mk(cd)), cd, cs)
        if not it.empty:
            return cd, cs, it
    return None


def clear_dialog(d: date, shift: str, tag: str, user: str) -> None:
    st.dialog(_("Clear the board?"))(_clear_body)(d, shift, tag, user)


def _clear_body(d: date, shift: str, tag: str, user: str) -> None:
    core = load_core()
    items = board_items(load_sched(mk(d)), d, shift)
    flags, notes = line_flags(core["lineday"], d, shift), board_notes(core["boards"], d, shift)
    if items.empty and not notes.strip() and not any(f.get("down") or f.get("priority") for f in flags.values()):
        st.info(_("The board is already blank."))
        return
    st.warning(_("This removes **everything** from **{slot}**: {n} recipes, lines down / priority and the notes. "
                 "The board starts blank.", slot=slot_label(d, shift), n=len(items)))
    st.caption(_("The previous shift's record is kept, and every removed recipe is saved in the activity log."))
    c1, c2 = st.columns(2)
    if c1.button(_("🗑️ Yes, clear it"), type="primary", width="stretch"):
        n = safe(clear_board, d, shift, user)
        if n is not None:
            for k in plan_keys(tag):
                st.session_state.pop(k, None)
            flash(_("{slot} cleared — {n} recipes removed", slot=slot_label(d, shift), n=n))
        st.rerun()
    if c2.button(_("Cancel"), width="stretch"):
        st.rerun()


def plan_page() -> None:
    st.title(_("🗓️ Plan the board"))
    core = load_core()
    u = need_user(core, roles=PLANNER_ROLES)
    if not u:
        return
    d, shift = board_picker("plan")
    tag = f"{d.isoformat()}_{shift}_{lang()}"
    items = board_items(load_sched(mk(d)), d, shift)
    flags = line_flags(core["lineday"], d, shift)
    notes = board_notes(core["boards"], d, shift)

    b2, b3, b4, _sp = st.columns([1.2, 1.1, 1.1, 2.6])
    if b2.button(_("⧉ Copy last board")):
        prev = find_previous_board(d, shift)
        if prev:
            pd_, ps, pit = prev
            fill_plan(tag, pit, line_flags(core["lineday"], pd_, ps), board_notes(core["boards"], pd_, ps),
                      overwrite=True, copy_down=False)
            st.info(_("Copied {slot} — review and **Save board**.", slot=slot_label(pd_, ps)))
        else:
            st.warning(_("No earlier board found in the last two weeks."))
    if b3.button(_("↺ Discard edits")):
        for k in plan_keys(tag):
            st.session_state.pop(k, None)
    if b4.button(_("🗑️ Clear board"), help=_("Remove every recipe, line down / priority and notes from this shift's board")):
        clear_dialog(d, shift, tag, u["name"])
    fill_plan(tag, items, flags, notes, overwrite=False)

    with st.form(f"plan_form_{tag}"):
        st.caption(_("One recipe per row (or comma-separated), in run order. Add `x6` for batches. "
                     "To mark a line down, pick its reason in the box under it (leave it on *Running* otherwise). "
                     "Recipes already on the board (including ones carried over from last shift) keep their "
                     "status when you save."))
        for group in (GRID_LINES[:10], GRID_LINES[10:], SIDE_LINES):
            for i in range(0, len(group), 5):
                cols = st.columns(5)
                for c, line in zip(cols, group[i:i + 5]):
                    c.text_area(line_name(line), key=f"plan_{tag}_{line}", height=150)
                    c.selectbox(_("Line down?"), DOWN_REASONS, key=f"plan_{tag}_down_{line}", index=None,
                                placeholder=_("Running"), label_visibility="collapsed", format_func=_)
                    li = items[items["line"] == line] if not items.empty else items
                    started = [f"{ICON[s]} {r}" for r, s in zip(li["recipe"], li["status"]) if s in ICON and s != "Ready"]
                    if started:
                        c.caption(" · ".join(started))
        st.multiselect(_("Priority lines"), LINE_IDS, key=f"plan_{tag}_priority", format_func=line_name)
        st.text_area(_("Board notes (everyone sees these)"), key=f"plan_{tag}_notes", height=110,
                     placeholder=_("High protein items, reminders…"))
        submitted = st.form_submit_button(_("💾 Save board"), type="primary")

    if submitted:
        ss = st.session_state
        plan = {ln: parse_entries(ss[f"plan_{tag}_{ln}"]) for ln in LINE_IDS}
        desired = {ln: {"down": bool(ss.get(f"plan_{tag}_down_{ln}")), "down_reason": ss.get(f"plan_{tag}_down_{ln}") or "",
                        "priority": ln in ss[f"plan_{tag}_priority"]} for ln in LINE_IDS}
        new_notes = ss[f"plan_{tag}_notes"].strip()
        try:
            added, removed = save_plan(d, shift, plan, items, u["name"])
            save_flags(d, shift, desired, flags, u["name"])
            if new_notes != notes.strip():
                upsert("boards", BOARD_COLS, [{"key": board_key(d, shift), "date": d.isoformat(), "shift": shift,
                                               "notes": new_notes, "updated_by": u["name"], "updated_at": stamp()}])
            ok = True
        except Exception as e:
            ok = False
            st.error(_("Couldn't save — {e}", e=e))
        if ok:
            refresh()
            for k in plan_keys(tag):
                st.session_state.pop(k, None)
            flash(_("Board saved — {a} added, {r} removed", a=added, r=removed))
            st.rerun()

    with st.expander(_("🎯 Daily goals (side panel)")):
        g = core["goals"][GOAL_COLS] if not core["goals"].empty else pd.DataFrame(DEFAULT_GOALS, columns=GOAL_COLS)
        edited = st.data_editor(g, num_rows="dynamic", hide_index=True, key="goals_editor")
        if st.button(_("Save goals")):
            rows = [[str(r["label"]).strip(), str(r["value"]).strip()] for r in edited.to_dict("records")
                    if str(r.get("label") or "").strip()]
            if safe(lambda: store().replace("goals", [GOAL_COLS] + rows) or True):
                flash(_("Goals saved"))
            refresh()
            st.rerun()


def history_print_html(df: pd.DataFrame, start: date, end: date, sel: list, planned: int, fin: int, carried: int) -> str:
    """A Print button that prints only the summary (numbers + both charts), not the whole page."""
    e = html.escape

    def bars(series: pd.Series, suffix: str = "", vmax: float | None = None) -> str:
        if series.empty:
            return f'<p class="muted">{_("Nothing to show.")}</p>'
        top = vmax or max(float(series.max()), 1)
        return "".join(
            f'<div class="row"><span class="lbl">{e(str(k))}</span><span class="track">'
            f'<span class="bar" style="width:{max(float(v), 0) / top * 100:.1f}%"></span></span>'
            f'<span class="val">{float(v):.0f}{suffix}</span></div>' for k, v in series.items())

    nf = df[~df["finished"]]
    reasons = nf["reason"].replace("", "No reason given").map(_).value_counts()
    by_line = df.groupby("line").agg(planned=("id", "count"), finished=("finished", "sum"))
    by_line = (by_line["finished"] / by_line["planned"] * 100).reindex([i for i in LINE_IDS if i in by_line.index])
    by_line.index = [line_name(i) for i in by_line.index]
    lines_txt = ", ".join(line_name(i) for i in sel) if sel else _("All lines")
    rng = f"{fmt_date(start, False)}, {start.year} – {fmt_date(end, False)}, {end.year}"
    report = f"""
<h1>{e(_("Schedule Board — History"))}</h1>
<p class="sub">{e(rng)} · {e(lines_txt)} · {_("printed")} {fmt_date(now().date(), False)}, {now():%-I:%M %p}</p>
<div class="kpis">
  <div><span>{_("Recipes planned")}</span><b>{planned}</b></div>
  <div><span>{_("Finished")}</span><b>{fin} ({fin / planned:.0%})</b></div>
  <div><span>{_("Not finished")}</span><b>{planned - fin}</b></div>
  <div><span>{_("Carried to next shift")}</span><b>{carried}</b></div>
</div>
<h2>{e(_("Why recipes didn't finish (carry-over reasons)"))}</h2>{bars(reasons) if not nf.empty else f'<p class="muted">{_("Everything planned was finished.")}</p>'}
<h2>{_("Finish rate by line")}</h2>{bars(by_line, "%", 100)}"""
    return f"""<!DOCTYPE html><html><head><style>
body{{margin:0;font-family:"Source Sans Pro",system-ui,sans-serif;color:#1f2a37}}
button{{font:inherit;font-size:14px;padding:6px 14px;border:1px solid #d6dbe2;border-radius:8px;background:#fff;cursor:pointer}}
button:hover{{border-color:#c62828;color:#c62828}}
#report{{display:none}}
h1{{font-size:22px;margin:0 0 2px}} h2{{font-size:15px;margin:22px 0 8px}} .sub,.muted{{color:#5f6b7a;font-size:12px;margin:0}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px}}
.kpis div{{border:1px solid #d6dbe2;border-radius:8px;padding:8px 10px}} .kpis span{{display:block;font-size:12px;color:#5f6b7a}}
.kpis b{{font-size:24px}}
.row{{display:grid;grid-template-columns:150px 1fr 48px;align-items:center;gap:8px;margin:4px 0;font-size:13px}}
.track{{background:#eef0f3;border-radius:3px;height:14px;display:block}}
.bar{{display:block;height:14px;border-radius:3px;background:#1a56db;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
.val{{text-align:right;font-variant-numeric:tabular-nums}}
@media print{{ #btn{{display:none}} #report{{display:block}} @page{{margin:14mm}} }}
</style></head><body>
<button id="btn" onclick="window.print()">{_("🖨️ Print summary")}</button>
<div id="report">{report}</div>
</body></html>"""


def history_page() -> None:
    st.title(_("📈 History"))
    if not need_user(load_core(), roles=PLANNER_ROLES, denied="Only planners can see the history."):
        return
    today = now().date()
    c1, c2, c3 = st.columns([1, 1, 3])
    start = c1.date_input(_("From"), today - timedelta(days=6), format="MM/DD/YYYY")
    end = c2.date_input(_("To"), today, format="MM/DD/YYYY")
    sel = c3.multiselect(_("Lines"), LINE_IDS, format_func=line_name, placeholder=_("All lines"))
    if start > end:
        st.warning(_("'From' is after 'To'."))
        return
    months = month_range(start, end)
    df = pd.concat([load_sched(m) for m in months], ignore_index=True)
    lg = pd.concat([load_log(m) for m in months], ignore_index=True)
    if df.empty:
        st.info(_("No boards in this range yet."))
        return
    lo, hi = start.isoformat(), end.isoformat()
    df = df[(df["date"] >= lo) & (df["date"] <= hi) & (~df["deleted"].map(truthy))].copy()
    if not lg.empty:
        lg = lg[(lg["date"] >= lo) & (lg["date"] <= hi)]
    if sel:
        df = df[df["line"].isin(sel)]
        lg = lg[lg["line"].isin(sel)] if not lg.empty else lg
    if df.empty:
        st.info(_("Nothing planned for these lines in this range."))
        return
    df["Line"] = df["line"].map(line_name)
    df["finished"] = df["status"].eq("Finished")
    df["carried"] = df["carried"].map(lambda x: _("Yes") if truthy(x) else "")
    df["run_min"] = ((pd.to_datetime(df["finished_at"], errors="coerce") -
                      pd.to_datetime(df["started_at"], errors="coerce")).dt.total_seconds() / 60).round(0)

    planned, fin = len(df), int(df["finished"].sum())
    carried = int((df["carried"] != "").sum())
    components.html(history_print_html(df, start, end, sel, planned, fin, carried), height=48)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(_("Recipes planned"), planned)
    m2.metric(_("Finished"), f"{fin}  ({fin / planned:.0%})")
    m3.metric(_("Not finished"), planned - fin)
    m4.metric(_("Carried to next shift"), carried)

    a, b = st.columns(2)
    with a:
        why_title = _("Why recipes didn't finish (carry-over reasons)")
        st.markdown(f"**{why_title}**")
        nf = df[~df["finished"]]
        if nf.empty:
            st.caption(_("Everything planned was finished. 🎉"))
        else:
            rec = _("Recipes")
            st.bar_chart(nf["reason"].replace("", "No reason given").map(_).value_counts().rename(rec), horizontal=True,
                         sort=f"-{rec}")
    with b:
        st.markdown(f"**{_('Finish rate by line')}**")
        pct = _("Finish %")
        by_line = df.groupby("Line", sort=False).agg(planned=("id", "count"), finished=("finished", "sum"))
        by_line[pct] = (by_line["finished"] / by_line["planned"] * 100).round(0)
        by_line = by_line.reindex([line_name(i) for i in LINE_IDS if line_name(i) in by_line.index])
        st.bar_chart(by_line[pct], sort=False)

    st.markdown(f"**{_('By shift')}**")
    g = df.groupby(["date", "shift"]).agg(Planned=("id", "count"), Finished=("finished", "sum")).reset_index()
    g["Not finished"] = g["Planned"] - g["Finished"]
    g["Finish %"] = (g["Finished"] / g["Planned"] * 100).round(0)
    g["shift"] = g["shift"].map(shift_name)
    g = g.sort_values(["date", "shift"], ascending=False)
    g.columns = [_(c) for c in ("Date", "Shift", "Planned", "Finished", "Not finished", "Finish %")]
    st.dataframe(g, hide_index=True)

    st.markdown(f"**{_('Recipes')}**")
    cols = {"date": "Date", "shift": "Shift", "Line": "Line", "seq": "#", "recipe": "Recipe", "qty": "Qty",
            "status": "Status", "reason": "Reason", "comment": "Comment", "carried": "Carried over", "started_at": "Started",
            "finished_at": "Finished", "run_min": "Run (min)", "updated_by": "Last update by"}
    out = df.sort_values(["date", "shift", "line", "seq"])[list(cols)].copy()
    out["shift"] = out["shift"].map(shift_name)
    out["status"] = out["status"].map(lambda v: _(v) if v else v)
    out["reason"] = out["reason"].map(lambda v: _(v) if v else v)
    out = out.rename(columns={k: _(v) for k, v in cols.items()})
    st.dataframe(out, hide_index=True)
    st.download_button(_("⬇ Download recipes (CSV)"), out.to_csv(index=False).encode(), f"schedule_{lo}_{hi}.csv", "text/csv")

    with st.expander(_("Activity log (every status change)")):
        if lg.empty:
            st.caption(_("No activity yet."))
        else:
            lg = lg.assign(line=lg["line"].map(line_name)).sort_values("ts", ascending=False)
            st.dataframe(lg[LOG_COLS], hide_index=True)
            st.download_button(_("⬇ Download log (CSV)"), lg[LOG_COLS].to_csv(index=False).encode(),
                               f"activity_{lo}_{hi}.csv", "text/csv")


# ── App ────────────────────────────────────────────────────────────────────────
def sidebar() -> None:
    with st.sidebar:
        if store().demo:
            st.warning("**Demo mode** — not connected to Google Sheets, data resets when the app restarts.\n\n"
                       f"{store().why}\n\n"
                       "Try it: **Demo Lead** PIN `1111`, **Demo Planner** PIN `2222`.")
        st.segmented_control(f"🌐 {_('Language')}", ["en", "es"], default=lang(), key="_lang_pick",
                             format_func={"en": "English", "es": "Español"}.get, on_change=on_lang_change)
        u = st.session_state.get("user")
        if u:
            left = max(0, int((u["exp"] - time.time()) // 3600))
            st.markdown(f"👤 **{u['name']}** · {_(u['role'])}")
            st.caption(_("Signed in for about {n} more hours" if left != 1 else "Signed in for about {n} more hour", n=left))
            if st.button(_("Sign out")):
                st.session_state.pop("user", None)
                st.query_params.pop("s", None)
                st.rerun()


def main() -> None:
    tv = is_tv()
    try:
        store()
    except Exception as e:
        st.error(f"Can't reach the Google Sheet: {e}")
        st.stop()
    init_lang()
    pages = [st.Page(board_page, title=_("Board"), icon="📋", default=True),
             st.Page(lead_page, title=_("Update"), icon="✏️"),
             st.Page(plan_page, title=_("Plan"), icon="🗓️"),
             st.Page(history_page, title=_("History"), icon="📈")]
    pg = st.navigation(pages, position="hidden" if tv else "top")
    if not tv:
        st.html("<style>.block-container,[data-testid='stMainBlockContainer']{padding-top:3.75rem!important}</style>")
        keep_signed_in()
        sidebar()
        save_lang_cookie()
    show_flash()
    pg.run()


main()
