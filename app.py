"""
Schedule Board — live production schedule for the plant lines.

Planners write the board at shift start (what runs on each line), leads update
recipe status from the floor (PIN sign-in), and the office watches it live.

Backend: Google Sheets. With no secrets configured the app runs in DEMO mode
(in-memory data seeded from the whiteboard) so it can be tried right away.
"""
from __future__ import annotations

import html
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Schedule Board", page_icon="📋", layout="wide")

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
REASONS = ["Ran out of time", "Mechanical issues", "Rework other product", "No material", "Short staffed"]

SHIFTS = ["1st", "2nd"]                 # in start-time order
SHIFT_START = {"1st": 5, "2nd": 15}     # hour (24h) each shift starts
PLANNER_ROLES = ("planner", "admin")
REFRESH_SECONDS = 15                    # board auto-refresh
CACHE_SECONDS = 10                      # shared read cache (protects Sheets quota)
FLAG_KEYS = ("down", "priority")

# Sheet tabs (schedule_YYYY_MM and log_YYYY_MM are created per month)
SCHED_COLS = ["id", "date", "shift", "line", "seq", "recipe", "qty", "status", "reason", "comment",
              "deleted", "created_by", "created_at", "updated_by", "updated_at", "started_at", "finished_at",
              "carried", "carried_from"]
LOG_COLS = ["ts", "date", "shift", "line", "item_id", "recipe", "action", "from_status", "to_status",
            "reason", "comment", "user"]
LINEDAY_COLS = ["key", "date", "shift", "line", "down", "priority", "updated_by", "updated_at"]
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
    return f"{d:%a %b} {d.day} · {shift} shift"


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
    for tok in re.split(r"[,;/]+", spec):
        t = tok.strip().upper()
        if not t:
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

    def ensure(self, name, header, seed=None):
        if name in self.ws:
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
        [line_key(d, shift, ln), d.isoformat(), shift, ln, ln in ("L17", "L18"), ln == "L9", "Demo Planner", start]
        for ln in ("L9", "L17", "L18")])
    s.ensure("boards", BOARD_COLS, seed=[[board_key(d, shift), d.isoformat(), shift, DEMO_NOTES, "Demo Planner", start]])
    return s


@st.cache_resource(show_spinner="Connecting to Google Sheets…")
def store():
    try:
        sa = dict(st.secrets["gcp_service_account"])
        sheet_id = st.secrets["sheet_id"]
    except Exception:
        return demo_store()
    return SheetStore(sa, sheet_id)


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
        out[r["line"]] = {k: truthy(r[k]) for k in FLAG_KEYS}
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
    return f"Shift ended {t:%-I:%M %p} by {row.get('closed_by', '')} — {row.get('carried_count') or 0} carried to next shift"


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
        flash("That recipe was removed or changed by someone else — board refreshed.", "warn")
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


def save_flags(d: date, shift: str, desired: dict, current: dict, user: str) -> None:
    rows = []
    for line in LINE_IDS:
        want, have = desired.get(line, {}), current.get(line, {})
        if any(bool(want.get(k)) != bool(have.get(k)) for k in FLAG_KEYS):
            rows.append({"key": line_key(d, shift, line), "date": d.isoformat(), "shift": shift, "line": line,
                         **{k: bool(want.get(k)) for k in FLAG_KEYS}, "updated_by": user, "updated_at": stamp()})
    upsert("lineday", LINEDAY_COLS, rows)


def safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # Sheets/API errors shouldn't crash the floor tablet
        flash(f"Couldn't save — {e}", "err")
        return None


# ── Board rendering (HTML) ─────────────────────────────────────────────────────
BOARD_CSS = """<style>
.sb{font-family:"Source Sans Pro","Source Sans 3",system-ui,sans-serif;color:#1f2a37}
.sb-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;margin-bottom:10px}
.sb-title{font-size:22px;font-weight:800;letter-spacing:-.01em}
.sb-title span{font-weight:600;color:#5f6b7a;font-size:15px;margin-left:6px}
.kpis{display:flex;gap:6px;flex-wrap:wrap}
.kpi{border-radius:999px;padding:2px 10px;font-size:13px;font-weight:700;border:1.5px solid;white-space:nowrap}
.live{margin-left:auto;font-size:12px;color:#5f6b7a;white-space:nowrap}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#188038;margin-right:5px;animation:sbp 2s infinite}
@keyframes sbp{50%{opacity:.25}}
.sb-main{display:grid;grid-template-columns:minmax(0,1fr) 210px;gap:10px;align-items:start}
.sb-grid{display:grid;grid-template-columns:repeat(10,minmax(0,1fr));gap:8px}
.sb-side{display:flex;flex-direction:column;gap:8px}
.card{background:#fff;border:1px solid #d6dbe2;border-radius:8px;padding:6px 6px 8px;min-height:170px;box-shadow:0 1px 2px rgba(16,24,40,.05)}
.sb-side .card{min-height:0}
.card.prio{border:2px solid #c62828}
.card.down{background:repeating-linear-gradient(135deg,#f6f7f9 0 10px,#eceff3 10px 20px)}
.ch{display:flex;justify-content:space-between;align-items:center;gap:4px;margin-bottom:4px}
.lbl{color:#c62828;border:1.5px solid #c62828;border-radius:3px;font-weight:800;font-size:12px;padding:1px 6px;text-transform:uppercase;background:#fff;white-space:nowrap}
.cnt{font-size:11px;color:#5f6b7a;font-weight:700}
.badges{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:2px}
.badge{font-size:9.5px;font-weight:800;border-radius:3px;padding:1px 5px;letter-spacing:.03em;color:#fff}
.badge.prio{background:#c62828}
.downx{font-size:20px;font-weight:900;color:#9aa3ae;text-align:center;padding:16px 0 6px}
.item{display:flex;justify-content:space-between;align-items:center;gap:4px;border:1.5px solid;border-radius:6px;padding:3px 6px;margin-top:4px;font-weight:700;font-size:14px;line-height:1.25}
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
@media (max-width:1200px){
 .sb-main{grid-template-columns:1fr}
 .sb-grid,.sb-side{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
 .sb-foot{grid-template-columns:1fr}.card{min-height:0}.live{margin-left:0}}
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
    out = [f'<div class="{cls}"><div class="ch"><span class="lbl">{e(LINE_LABEL[line])}</span>'
           f'{f"<span class=cnt>{done}/{n}</span>" if n else ""}</div>']
    badges = '<span class="badge prio">★ PRIORITY</span>' if f.get("priority") else ""
    if badges:
        out.append(f'<div class="badges">{badges}</div>')
    if f.get("down"):
        out.append('<div class="downx">✕ DOWN</div>')
    for it in li.to_dict("records"):
        status = it["status"] if it["status"] in STATUS_STYLE else "Ready"
        bg, bd, fg, tag = STATUS_STYLE[status]
        qty = f' <span class="qty">×{e(it["qty"])}</span>' if it["qty"] else ""
        out.append(f'<div class="item s-{status.lower().replace(" ", "-")}" style="background:{bg};border-color:{bd};color:{fg}">'
                   f'<span class="rec">{e(it["recipe"])}{qty}</span><span class="tag">{tag}{" ↪" if truthy(it["carried"]) else ""}</span></div>')
        sub = ([f"<b>{e(it['reason'])}</b>"] if it["reason"] else []) + ([e(it["comment"])] if it["comment"] else [])
        if sub:
            out.append(f'<div class="cmt">{" · ".join(sub)}</div>')
    if not n and not f.get("down"):
        out.append('<div class="empty">—</div>')
    out.append("</div>")
    return "".join(out)


def render_board(d: date, shift: str, items: pd.DataFrame, flags: dict, notes: str, goals: pd.DataFrame, tv: bool,
                 closed: str = "") -> str:
    e = html.escape
    st_counts = items["status"].value_counts() if not items.empty else pd.Series(dtype=int)
    total = len(items)
    kpis = [("Finished", f"✅ {int(st_counts.get('Finished', 0))}/{total} done"),
            ("Running", f"🟢 {int(st_counts.get('Running', 0))} running"),
            ("Next", f"🟡 {int(st_counts.get('Next', 0))} next"),
            ("Not ready", f"🔴 {int(st_counts.get('Not ready', 0))} not ready")]
    downs = sum(1 for f in flags.values() if f.get("down"))
    kpi_html = "".join(f'<span class="kpi" style="background:{STATUS_STYLE[s][0]};border-color:{STATUS_STYLE[s][1]};'
                       f'color:{STATUS_STYLE[s][2]}">{t}</span>' for s, t in kpis)
    if downs:
        kpi_html += f'<span class="kpi" style="background:#f1f3f6;border-color:#9aa3ae;color:#4b5563">✕ {downs} line{"s" if downs > 1 else ""} down</span>'
    if closed:
        kpi_html += f'<span class="kpi" style="background:#1f2a37;border-color:#1f2a37;color:#fff">↪ {e(closed)}</span>'

    goal_rows = "".join(f'<div class="goal"><span>{e(str(g["label"]))}</span><b>{e(str(g["value"]))}</b></div>'
                        for g in goals.to_dict("records")) if not goals.empty else '<div class="empty">—</div>'
    side = "".join(_card(ln, items, flags) for ln in SIDE_LINES) + \
        f'<div class="panel"><div class="ph">Daily goals</div>{goal_rows}</div>'

    feed_items = items[(items["updated_at"] != items["created_at"])] if not items.empty else items
    feed_items = feed_items.sort_values("updated_at", ascending=False).head(10) if not feed_items.empty else feed_items
    feed = "".join(
        f'<div><time>{e(r["updated_at"][11:16])}</time><b>{e(LINE_LABEL.get(r["line"], r["line"]))}</b> · '
        f'{e(r["recipe"])} → {ICON.get(r["status"], "")} {e(r["status"])}'
        f'{" — " + e(r["comment"]) if r["comment"] else ""} <span class="who">({e(r["updated_by"])})</span></div>'
        for r in feed_items.to_dict("records")) or '<div class="empty" style="padding:6px 0">No updates yet</div>'
    legend = "".join(f'<span style="background:{STATUS_STYLE[s][0]};border-color:{STATUS_STYLE[s][1]};'
                     f'color:{STATUS_STYLE[s][2]}">{s}</span>' for s in STATUSES)

    return (f'{BOARD_CSS}<div class="sb{" tv" if tv else ""}">'
            f'<div class="sb-head"><div class="sb-title">Schedule Board<span>{e(slot_label(d, shift))}</span></div>'
            f'<div class="kpis">{kpi_html}</div>'
            f'<div class="live"><span class="dot"></span>Live · updated {now().strftime("%-I:%M:%S %p")}</div></div>'
            f'<div class="sb-main"><div class="sb-grid">{"".join(_card(ln, items, flags) for ln in GRID_LINES)}</div>'
            f'<div class="sb-side">{side}</div></div>'
            f'<div class="sb-foot"><div class="panel"><div class="ph">Notes</div><div class="notes">{e(notes) or "—"}</div>'
            f'<div class="legend">{legend}</div></div>'
            f'<div class="panel feed"><div class="ph">Latest from the floor</div>{feed}</div></div></div>')


# ── Shared UI pieces ───────────────────────────────────────────────────────────
def board_picker(key: str) -> tuple[date, str]:
    cd, cs = current_board()
    c1, c2, _ = st.columns([1.3, 1, 4])
    d = c1.date_input("Date", cd, key=f"{key}_date", format="MM/DD/YYYY")
    shift = c2.selectbox("Shift", SHIFTS, index=SHIFTS.index(cs), key=f"{key}_shift")
    return d, shift


def login_form(core: dict) -> None:
    leads = core["leads"]
    if not leads.empty:
        leads = leads[leads["active"].map(lambda x: str(x).strip() == "" or truthy(x))]
    names = sorted(leads["name"].astype(str).str.strip().unique().tolist()) if not leads.empty else []
    if not names:
        st.info("No leads set up yet. Add people to the **leads** tab of the Google Sheet: "
                "`name`, `pin`, `role` (lead / planner), `lines` (e.g. `1, 2, Cole Slaw` or `ALL`), `active` (TRUE).")
        return
    with st.form("login", border=True):
        st.markdown("**Sign in to update the board**")
        name = st.selectbox("Name", names, index=None, placeholder="Pick your name")
        pin = st.text_input("PIN", type="password", max_chars=12)
        if st.form_submit_button("Sign in", type="primary"):
            row = leads[leads["name"].astype(str).str.strip() == name] if name else leads.iloc[0:0]
            if not row.empty and pin_ok(pin, row.iloc[0]["pin"]):
                r = row.iloc[0]
                role = str(r["role"]).strip().lower() or "lead"
                st.session_state["user"] = {"name": name, "role": role,
                                            "lines": list(LINE_IDS) if role in PLANNER_ROLES else allowed_lines(r["lines"])}
                st.rerun()
            st.error("Wrong name or PIN.")


def need_user(core: dict, roles=None) -> dict | None:
    u = st.session_state.get("user")
    if not u:
        login_form(core)
        return None
    if roles and u["role"] not in roles:
        st.warning("Only planners can set up the board. Use **Update** to change recipe status.")
        return None
    return u


# ── Pages ──────────────────────────────────────────────────────────────────────
@st.fragment(run_every=REFRESH_SECONDS)
def live_board(d: date | None, shift: str | None, tv: bool) -> None:
    if d is None:
        d, shift = current_board()  # TV follows the clock
    core, sched = load_core(), load_sched(mk(d))
    st.html(render_board(d, shift, board_items(sched, d, shift), line_flags(core["lineday"], d, shift),
                         board_notes(core["boards"], d, shift), core["goals"], tv,
                         closed_text(board_row(core["boards"], d, shift))))


def board_page() -> None:
    if is_tv():
        st.html(TV_CSS)
        live_board(None, None, True)
        return
    d, shift = board_picker("board")
    live_board(d, shift, False)
    st.caption(f"Refreshes every {REFRESH_SECONDS}s. For the office TV, open the app link with `?tv=1` at the end — "
               "it hides the menus and follows the current shift.")


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
        flash(f"{item['recipe']} → {new}")


def status_key(it: dict) -> str:
    return f"st_{it['id']}_{it['updated_at']}_{st.session_state.get('nonce_' + it['id'], 0)}"


def reset_status_widget(item_id: str) -> None:
    """Give the status buttons a fresh key so an unsaved pick snaps back to the saved status."""
    st.session_state["nonce_" + item_id] = st.session_state.get("nonce_" + item_id, 0) + 1


def cancel_notready() -> None:
    p = st.session_state.pop("_notready", None)
    if p:
        reset_status_widget(p["item"]["id"])


@st.dialog("Why is it not ready?", on_dismiss=cancel_notready)
def notready_dialog() -> None:
    p = st.session_state.get("_notready")
    if not p:
        return
    it = p["item"]
    st.markdown(f"**{LINE_LABEL.get(it['line'], it['line'])} · {it['recipe']}**")
    reason = st.selectbox("Reason (required)", REASONS, index=None, placeholder="Pick a reason")
    comment = st.text_area("Comment (optional)", it["comment"], height=80)
    c1, c2 = st.columns(2)
    if c1.button("Save", type="primary", width="stretch"):
        if not reason:
            st.error("Pick a reason first.")
            return
        st.session_state.pop("_notready", None)
        if safe(update_item, it, {"status": "Not ready", "reason": reason, "comment": comment.strip()},
                p["user"], "status"):
            flash(f"{it['recipe']} → Not ready ({reason})")
        st.rerun()
    if c2.button("Cancel", width="stretch"):
        cancel_notready()
        st.rerun()


def on_toggle_down(d: date, shift: str, line: str, key: str, flags: dict, user: str) -> None:
    desired = {ln: dict(v) for ln, v in flags.items()}
    desired.setdefault(line, {})["down"] = bool(st.session_state.get(key))
    safe(save_flags, d, shift, desired, flags, user)
    refresh()


def item_row(it: dict, user: str) -> None:
    c1, c2, c3 = st.columns([2.2, 6, 2], vertical_alignment="center")
    c1.markdown(f"**{it['recipe']}**" + (f" ×{it['qty']}" if it["qty"] else "") + (" ↪" if truthy(it["carried"]) else ""))
    sub = " · ".join(x for x in (it["reason"], it["comment"]) if x)
    if sub:
        c1.caption(sub)
    key = status_key(it)
    pending = st.session_state.get("_notready")
    if key in st.session_state and st.session_state[key] != it["status"] and not (pending and pending["key"] == key):
        reset_status_widget(it["id"])  # an unsaved pick (e.g. cancelled Not ready) snaps back
        key = status_key(it)
    c2.segmented_control("Status", STATUSES, default=it["status"] if it["status"] in STATUSES else None,
                         format_func=lambda s: f"{ICON[s]} {s}", key=key, label_visibility="collapsed",
                         on_change=on_status, args=(it, key, user))
    with c3.popover("💬 Comments", help="Reason and comment — carried to the next shift"):
        with st.form(f"note_{it['id']}_{it['updated_at']}", border=False):
            st.markdown(f"**Comments · {it['recipe']}**")
            reason = st.selectbox("Reason", REASONS, index=REASONS.index(it["reason"]) if it["reason"] in REASONS else None,
                                  placeholder="Pick a reason")
            comment = st.text_area("Comment", it["comment"], height=80, placeholder="What happened, what's needed…")
            if st.form_submit_button("Save", type="primary"):
                if it["status"] == "Not ready" and not reason:
                    st.error("Not ready needs a reason.")
                    return
                if safe(update_item, it, {"reason": reason or "", "comment": comment.strip()}, user, "note"):
                    flash(f"Comments saved on {it['recipe']}")
                st.rerun()


@st.dialog("End shift & carry over", width="large")
def carryover_dialog(d: date, shift: str, user: str) -> None:
    items = board_items(load_sched(mk(d)), d, shift)
    todo = items[(items["status"] != "Finished") & (~items["carried"].map(truthy))] if not items.empty else items
    nd, ns = next_slot(d, shift)
    if todo.empty:
        st.success("Everything on this board is finished or already carried over.")
        return
    st.markdown(f"These **{len(todo)}** recipes didn't finish. They'll move to the top of **{slot_label(nd, ns)}** "
                "with their status, reason and comment. Finished recipes stay on this shift's record.")
    with st.form("carryover_form", border=False):
        for line in LINE_IDS:
            li = todo[todo["line"] == line]
            if li.empty:
                continue
            st.markdown(f"**{LINE_LABEL[line]}**")
            for it in li.to_dict("records"):
                c1, c2, c3 = st.columns([1.6, 2, 3], vertical_alignment="center")
                c1.markdown(f"{ICON.get(it['status'], '')} {it['recipe']}" + (f" ×{it['qty']}" if it["qty"] else ""))
                c2.selectbox("Reason", REASONS, key=f"co_r_{it['id']}", label_visibility="collapsed",
                             index=REASONS.index(it["reason"]) if it["reason"] in REASONS else None,
                             placeholder="Reason (required)")
                c3.text_input("Comment", it["comment"], key=f"co_c_{it['id']}", label_visibility="collapsed",
                              placeholder="Comment (optional)")
        go = st.form_submit_button(f"↪ Carry over to {slot_label(nd, ns)}", type="primary")
    if go:
        ss = st.session_state
        missing = [f"{LINE_LABEL[r['line']]} · {r['recipe']}" for r in todo.to_dict("records") if not ss.get(f"co_r_{r['id']}")]
        if missing:
            st.error(f"Pick a reason for every recipe — missing {len(missing)}: " + ", ".join(missing[:8])
                     + ("…" if len(missing) > 8 else ""))
            return
        answers = {r["id"]: (ss[f"co_r_{r['id']}"], ss.get(f"co_c_{r['id']}", "").strip()) for r in todo.to_dict("records")}
        res = safe(carry_over, d, shift, todo, answers, user)
        if res:
            n, rd, rs = res
            flash(f"Shift ended — {n} recipes carried to {slot_label(rd, rs)}")
        st.rerun()


def lead_page() -> None:
    st.title("✏️ Update the floor")
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
    with_items = [ln for ln in mine if not items.empty and (items["line"] == ln).any()]
    default = mine if len(mine) <= 6 else (with_items or mine)
    c1, c2, c3 = st.columns([4, 1, 1.6], vertical_alignment="bottom")
    show = c1.multiselect("Lines", mine, default=default, format_func=LINE_LABEL.get, key=f"lead_lines_{u['name']}")
    if c2.button("🔄 Refresh"):
        refresh()
        st.rerun()
    if c3.button("🔚 End shift & carry over", type="primary",
                 help="At the end of the shift: everything not Finished moves to the next shift's board"):
        cancel_notready()
        carryover_dialog(d, shift, u["name"])
    elif "_notready" in st.session_state:
        notready_dialog()
    if closed:
        st.info(f"↪ {closed}")
    if items.empty:
        st.info("Nothing is on the board for this shift yet. A planner sets it up on the **Plan** page — "
                "or add recipes below.")
    for line in show:
        li = items[items["line"] == line] if not items.empty else items
        f = flags.get(line, {})
        with st.container(border=True):
            h1, h2 = st.columns([4, 1], vertical_alignment="center")
            tags = " · ★ priority" if f.get("priority") else ""
            done = int((li["status"] == "Finished").sum()) if len(li) else 0
            h1.markdown(f"#### {LINE_LABEL[line]} <span style='font-size:.8rem;color:#5f6b7a'>"
                        f"{done}/{len(li)} done{tags}</span>", unsafe_allow_html=True)
            dk = f"down_{line}_{d}_{shift}_{f.get('down', False)}"
            h2.toggle("Line down", value=f.get("down", False), key=dk,
                      on_change=on_toggle_down, args=(d, shift, line, dk, flags, u["name"]))
            for it in li.to_dict("records"):
                item_row(it, u["name"])
            with st.form(f"add_{line}_{d}_{shift}", clear_on_submit=True, border=False):
                a1, a2 = st.columns([5, 1], vertical_alignment="bottom")
                txt = a1.text_input("Add recipe", placeholder="Add recipe — e.g. PO 379 or CS 131 x6",
                                    label_visibility="collapsed")
                if a2.form_submit_button("➕ Add"):
                    entries = parse_entries(txt)
                    if entries:
                        start = int(pd.to_numeric(li["seq"], errors="coerce").fillna(0).max()) + 1 if len(li) else 1
                        if safe(add_items, d, shift, line, entries, u["name"], start):
                            flash(f"Added {len(entries)} to {LINE_LABEL[line]}")
                        st.rerun()


def plan_keys(tag: str) -> list[str]:
    return [f"plan_{tag}_{ln}" for ln in LINE_IDS] + [f"plan_{tag}_{k}" for k in (*FLAG_KEYS, "notes")]


def fill_plan(tag: str, items: pd.DataFrame, flags: dict, notes: str, overwrite: bool, copy_down=True) -> None:
    ss = st.session_state
    values = {f"plan_{tag}_{ln}": entries_text(items[items["line"] == ln]) if not items.empty else "" for ln in LINE_IDS}
    for k in FLAG_KEYS:
        values[f"plan_{tag}_{k}"] = [ln for ln in LINE_IDS if flags.get(ln, {}).get(k) and (copy_down or k != "down")]
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


def plan_page() -> None:
    st.title("🗓️ Plan the board")
    core = load_core()
    u = need_user(core, roles=PLANNER_ROLES)
    if not u:
        return
    d, shift = board_picker("plan")
    tag = f"{d.isoformat()}_{shift}"
    items = board_items(load_sched(mk(d)), d, shift)
    flags = line_flags(core["lineday"], d, shift)
    notes = board_notes(core["boards"], d, shift)

    b2, b3, _ = st.columns([1.1, 0.9, 3.4])
    if b2.button("⧉ Copy last board"):
        prev = find_previous_board(d, shift)
        if prev:
            pd_, ps, pit = prev
            fill_plan(tag, pit, line_flags(core["lineday"], pd_, ps), board_notes(core["boards"], pd_, ps),
                      overwrite=True, copy_down=False)
            st.info(f"Copied {slot_label(pd_, ps)} — review and **Save board**.")
        else:
            st.warning("No earlier board found in the last two weeks.")
    if b3.button("↺ Discard edits"):
        for k in plan_keys(tag):
            st.session_state.pop(k, None)
    fill_plan(tag, items, flags, notes, overwrite=False)

    with st.form(f"plan_form_{tag}"):
        st.caption("One recipe per row (or comma-separated), in run order. Add `x6` for batches. "
                   "Recipes already on the board (including ones carried over from last shift) keep their "
                   "status when you save.")
        for group in (GRID_LINES[:10], GRID_LINES[10:], SIDE_LINES):
            for i in range(0, len(group), 5):
                cols = st.columns(5)
                for c, line in zip(cols, group[i:i + 5]):
                    c.text_area(LINE_LABEL[line], key=f"plan_{tag}_{line}", height=150)
                    li = items[items["line"] == line] if not items.empty else items
                    started = [f"{ICON[s]} {r}" for r, s in zip(li["recipe"], li["status"]) if s in ICON and s != "Ready"]
                    if started:
                        c.caption(" · ".join(started))
        f1, f2 = st.columns(2)
        f1.multiselect("Lines down (✕)", LINE_IDS, key=f"plan_{tag}_down", format_func=LINE_LABEL.get)
        f2.multiselect("Priority lines", LINE_IDS, key=f"plan_{tag}_priority", format_func=LINE_LABEL.get)
        st.text_area("Board notes (everyone sees these)", key=f"plan_{tag}_notes", height=110,
                     placeholder="High protein items, reminders…")
        submitted = st.form_submit_button("💾 Save board", type="primary")

    if submitted:
        ss = st.session_state
        plan = {ln: parse_entries(ss[f"plan_{tag}_{ln}"]) for ln in LINE_IDS}
        desired = {ln: {k: ln in ss[f"plan_{tag}_{k}"] for k in FLAG_KEYS} for ln in LINE_IDS}
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
            st.error(f"Couldn't save — {e}")
        if ok:
            refresh()
            for k in plan_keys(tag):
                st.session_state.pop(k, None)
            flash(f"Board saved — {added} added, {removed} removed")
            st.rerun()

    with st.expander("🎯 Daily goals (side panel)"):
        g = core["goals"][GOAL_COLS] if not core["goals"].empty else pd.DataFrame(DEFAULT_GOALS, columns=GOAL_COLS)
        edited = st.data_editor(g, num_rows="dynamic", hide_index=True, key="goals_editor")
        if st.button("Save goals"):
            rows = [[str(r["label"]).strip(), str(r["value"]).strip()] for r in edited.to_dict("records")
                    if str(r.get("label") or "").strip()]
            if safe(lambda: store().replace("goals", [GOAL_COLS] + rows) or True):
                flash("Goals saved")
            refresh()
            st.rerun()


def history_page() -> None:
    st.title("📈 History")
    today = now().date()
    c1, c2, c3 = st.columns([1, 1, 3])
    start = c1.date_input("From", today - timedelta(days=6), format="MM/DD/YYYY")
    end = c2.date_input("To", today, format="MM/DD/YYYY")
    sel = c3.multiselect("Lines", LINE_IDS, format_func=LINE_LABEL.get, placeholder="All lines")
    if start > end:
        st.warning("'From' is after 'To'.")
        return
    months = month_range(start, end)
    df = pd.concat([load_sched(m) for m in months], ignore_index=True)
    lg = pd.concat([load_log(m) for m in months], ignore_index=True)
    if df.empty:
        st.info("No boards in this range yet.")
        return
    lo, hi = start.isoformat(), end.isoformat()
    df = df[(df["date"] >= lo) & (df["date"] <= hi) & (~df["deleted"].map(truthy))].copy()
    if not lg.empty:
        lg = lg[(lg["date"] >= lo) & (lg["date"] <= hi)]
    if sel:
        df = df[df["line"].isin(sel)]
        lg = lg[lg["line"].isin(sel)] if not lg.empty else lg
    if df.empty:
        st.info("Nothing planned for these lines in this range.")
        return
    df["Line"] = df["line"].map(LINE_LABEL).fillna(df["line"])
    df["finished"] = df["status"].eq("Finished")
    df["carried"] = df["carried"].map(lambda x: "Yes" if truthy(x) else "")
    df["run_min"] = ((pd.to_datetime(df["finished_at"], errors="coerce") -
                      pd.to_datetime(df["started_at"], errors="coerce")).dt.total_seconds() / 60).round(0)

    planned, fin = len(df), int(df["finished"].sum())
    carried = int((df["carried"] == "Yes").sum())
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Recipes planned", planned)
    m2.metric("Finished", f"{fin}  ({fin / planned:.0%})")
    m3.metric("Not finished", planned - fin)
    m4.metric("Carried to next shift", carried)

    a, b = st.columns(2)
    with a:
        st.markdown("**Why recipes didn't finish (carry-over reasons)**")
        nf = df[~df["finished"]]
        if nf.empty:
            st.caption("Everything planned was finished. 🎉")
        else:
            st.bar_chart(nf["reason"].replace("", "No reason given").value_counts().rename("Recipes"), horizontal=True,
                         sort="-Recipes")
    with b:
        st.markdown("**Finish rate by line**")
        by_line = df.groupby("Line", sort=False).agg(planned=("id", "count"), finished=("finished", "sum"))
        by_line["Finish %"] = (by_line["finished"] / by_line["planned"] * 100).round(0)
        by_line = by_line.reindex([LINE_LABEL[i] for i in LINE_IDS if LINE_LABEL[i] in by_line.index])
        st.bar_chart(by_line["Finish %"], sort=False)

    st.markdown("**By shift**")
    g = df.groupby(["date", "shift"]).agg(Planned=("id", "count"), Finished=("finished", "sum")).reset_index()
    g["Not finished"] = g["Planned"] - g["Finished"]
    g["Finish %"] = (g["Finished"] / g["Planned"] * 100).round(0)
    st.dataframe(g.sort_values(["date", "shift"], ascending=False).rename(columns={"date": "Date", "shift": "Shift"}),
                 hide_index=True)

    st.markdown("**Recipes**")
    cols = {"date": "Date", "shift": "Shift", "Line": "Line", "seq": "#", "recipe": "Recipe", "qty": "Qty",
            "status": "Status", "reason": "Reason", "comment": "Comment", "carried": "Carried over", "started_at": "Started",
            "finished_at": "Finished", "run_min": "Run (min)", "updated_by": "Last update by"}
    out = df.sort_values(["date", "shift", "line", "seq"])[list(cols)].rename(columns=cols)
    st.dataframe(out, hide_index=True)
    st.download_button("⬇ Download recipes (CSV)", out.to_csv(index=False).encode(), f"schedule_{lo}_{hi}.csv", "text/csv")

    with st.expander("Activity log (every status change)"):
        if lg.empty:
            st.caption("No activity yet.")
        else:
            lg = lg.assign(line=lg["line"].map(LINE_LABEL).fillna(lg["line"])).sort_values("ts", ascending=False)
            st.dataframe(lg[LOG_COLS], hide_index=True)
            st.download_button("⬇ Download log (CSV)", lg[LOG_COLS].to_csv(index=False).encode(),
                               f"activity_{lo}_{hi}.csv", "text/csv")


# ── App ────────────────────────────────────────────────────────────────────────
def sidebar() -> None:
    with st.sidebar:
        if store().demo:
            st.warning("**Demo mode** — not connected to Google Sheets, data resets when the app restarts.\n\n"
                       "Try it: **Demo Lead** PIN `1111`, **Demo Planner** PIN `2222`.")
        u = st.session_state.get("user")
        if u:
            st.markdown(f"👤 **{u['name']}** · {u['role']}")
            if st.button("Sign out"):
                st.session_state.pop("user", None)
                st.rerun()


def main() -> None:
    tv = is_tv()
    try:
        store()
    except Exception as e:
        st.error(f"Can't reach the Google Sheet: {e}")
        st.stop()
    pages = [st.Page(board_page, title="Board", icon="📋", default=True),
             st.Page(lead_page, title="Update", icon="✏️"),
             st.Page(plan_page, title="Plan", icon="🗓️"),
             st.Page(history_page, title="History", icon="📈")]
    pg = st.navigation(pages, position="hidden" if tv else "sidebar")
    if not tv:
        sidebar()
    show_flash()
    pg.run()


main()
