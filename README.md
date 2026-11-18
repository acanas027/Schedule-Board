# Schedule Board

A digital version of the line schedule whiteboard. Planners write what runs on each line at the start of the shift, leads update each recipe's status from the floor, and the office watches the board update live.

## Pages

| Page | Who | What it does |
|---|---|---|
| **Board** | Everyone, no sign-in | Live board laid out like the whiteboard: Lines 1–19, Cole Slaw, Cook 1, Cook 2, Vortex, daily goals, notes, and a "Latest from the floor" feed. Refreshes every 15 s. |
| **Update** | Leads (name + PIN) | Set recipe status (No material / Ready / Next / Running / Finished), add a reason and comment (💬), mark a line down, and add recipes that weren't planned. |
| **Plan** | Planners (name + PIN) | Type the recipes for each line (one per row, `x6` for batches), set lines down / priority / hand pack, and write the board notes. *Carry over unfinished* copies everything that didn't finish on the last board. |
| **History** | Everyone | Finish rate by shift and by line, reasons recipes didn't finish, run time, and the full activity log. Each can be downloaded as a CSV. |

**Office TV:** open the app URL with `?tv=1` at the end (for example `https://yourapp.streamlit.app/?tv=1`). This hides the menus and switches to the current shift on its own.

## Status rules
- Choosing **No material** fills in the reason as "No material". If the recipe later moves to another status, that reason is cleared. The log still keeps the history.
- **Running** records `started_at` and **Finished** records `finished_at`. History uses these two times to show run minutes.
- Every change is written to `log_YYYY_MM` with who made it and when.

## Setup (about 15 minutes)

1. **Google Sheet:** create a blank sheet named something like *Schedule Board*. The app creates its own tabs the first time it runs.
2. **Service account:** you can reuse the one from the staffing app. Share the sheet with its `client_email` as **Editor**.
3. **Secrets:** copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill it in. On Streamlit Cloud, paste the same contents into *App settings → Secrets*.
4. **Leads:** after the first run, fill in the `leads` tab:

   | name | pin | role | lines | active |
   |---|---|---|---|---|
   | Maria G | 4821 | lead | 7, 8, 9 | TRUE |
   | Andres | 1907 | planner | ALL | TRUE |

   - `role`: `lead` can update status. `planner` or `admin` can also use the Plan page and always sees every line.
   - `lines`: line numbers, `Cole Slaw`, `Cook 1`, `Cook 2`, `Vortex`, or `ALL`, separated by commas.
   - Format the **pin** column as *Plain text* so leading zeros are kept.
   - PINs are stored in plain text, so share the sheet only with the people who manage it.

Without secrets the app runs in **demo mode**, using today's whiteboard data held in memory (Demo Lead `1111`, Demo Planner `2222`).

## Sheet tabs (created automatically)
- `schedule_YYYY_MM`: one row per recipe per board. Removed recipes are marked `deleted=TRUE` instead of being erased. Using one tab per month keeps reads fast.
- `log_YYYY_MM`: every status change, note, add and remove.
- `lineday`: line flags (down, priority, hand pack) for each date and shift.
- `boards`: notes for each date and shift.
- `goals`: the daily goals panel. You can edit it from the Plan page.
- `leads`: names, PINs, roles and lines.

Don't reorder the columns in these tabs. The app writes to them by position.

## Settings (top of `app.py`)
`LINES`, `STATUSES`, `REASONS`, `SHIFTS`, `SHIFT_START` (the hour each shift starts; default 1st = 5, 2nd = 15), `REFRESH_SECONDS`.

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```
