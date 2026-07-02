#!/usr/bin/env python3
"""
Mumbai Water Index — daily fetch & compute
==========================================
Pulls BMC lake storage + IMD rainfall + a 7-day forecast, computes the index,
and injects a ready-to-render DATA object into the Story template.

Run it:
    python fetch_compute.py                 # live fetch (needs internet + ANTHROPIC_API_KEY)
    python fetch_compute.py --mock          # offline dry run using the reference-day numbers
    python fetch_compute.py --date 2026-07-01 --mock

Outputs (next to this file):
    out/<date>.html   -> the template with today's numbers baked in (screenshot this)
    out/<date>.json   -> the raw DATA object, for your records
    history.db        -> SQLite log; this is the dataset only you own — it grows every day

Design notes
------------
* Live fetching leans on an LLM extraction step (your strength) rather than brittle CSS
  selectors, so a layout change on BMC/IMD doesn't silently break the numbers.
* Historical comparisons (last year, 5-yr average) come from baselines.csv, which you
  backfill/maintain. Once history.db has a few seasons in it, you can regenerate that file
  from your own log.
* Nothing posts automatically. The script prints validation warnings so you can eyeball
  the frames before they go out — keep that human gate until the sources prove stable.
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sqlite3
import sys

# ----------------------------------------------------------------------------
# CONFIG  — everything you'd tune lives here
# ----------------------------------------------------------------------------
HANDLE = "allthingscities_mumbai"

LAKES = ["Upper Vaitarna", "Modak Sagar", "Tansa", "Middle Vaitarna",
         "Bhatsa", "Vihar", "Tulsi"]

TOTAL_CAPACITY_ML = 1_447_363     # usable capacity of the 7 lakes combined (ML)
DAILY_SUPPLY_ML   = 3_850         # BMC's daily supply to the city (ML) — for days-of-supply

# Index weights (must sum to 1.0). This blend IS your methodology — publish it, tune it.
WEIGHTS = {"relative": 0.55, "momentum": 0.25, "reserve": 0.20}
RESERVE_FULL_DAYS = 200           # days-of-supply that scores a perfect 10 on the reserve sub-score

# IMD "heavy rain" is ~64.5 mm/day, "moderate" 15.6–64.5. Used for forecast buckets.
HEAVY_MM, MODERATE_MM = 64.5, 15.6

MUMBAI_LAT, MUMBAI_LON = 19.076, 72.877

# Sources (live mode). Swap the lake URL for whichever page parses most reliably for you.
LAKE_URL      = "https://www.mcgm.gov.in/irj/portal/anonymous/qlhydrlc"
IMD_PRESS_PDF = "https://mausam.imd.gov.in/mumbai/mcdata/press.pdf"
FORECAST_URL  = ("https://api.open-meteo.com/v1/forecast"
                 f"?latitude={MUMBAI_LAT}&longitude={MUMBAI_LON}"
                 "&daily=precipitation_sum&forecast_days=8&timezone=Asia%2FKolkata")

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH        = os.path.join(HERE, "history.db")
BASELINES_CSV  = os.path.join(HERE, "baselines.csv")
TEMPLATE_PATH  = os.path.join(HERE, "template.html")
OUT_DIR        = os.path.join(HERE, "out")

DATA_START = "/* MWI-DATA:START"
DATA_END   = "/* MWI-DATA:END */"


# ----------------------------------------------------------------------------
# MOCK payloads — the reference-day numbers, so --mock runs with no network
# ----------------------------------------------------------------------------
MOCK_LAKES = {
    "total_ml": 103_871, "total_pct": 7.18,
    "lakes": [
        {"name": "Upper Vaitarna",  "pct": 0.00,  "rain_today_mm": 11,  "rain_season_mm": 470},
        {"name": "Modak Sagar",     "pct": 20.00, "rain_today_mm": 193, "rain_season_mm": 560},
        {"name": "Tansa",           "pct": 2.72,  "rain_today_mm": 180, "rain_season_mm": 540},
        {"name": "Middle Vaitarna", "pct": 11.71, "rain_today_mm": 59,  "rain_season_mm": 500},
        {"name": "Bhatsa",          "pct": 4.90,  "rain_today_mm": 120, "rain_season_mm": 520},
        {"name": "Vihar",           "pct": 50.77, "rain_today_mm": 83,  "rain_season_mm": 540},
        {"name": "Tulsi",           "pct": 28.00, "rain_today_mm": 200, "rain_season_mm": 540},
    ],
}
MOCK_CITY = {"city_today_mm": 62, "city_season_mm": 544.0}
MOCK_FORECAST_PRECIP = [95, 90, 100, 70, 45, 40, 30, 25]  # today + next 7 days (mm)


# ----------------------------------------------------------------------------
# FETCHERS (live)  — kept small; each returns a plain dict or raises
# ----------------------------------------------------------------------------
def _get(url, as_bytes=False, timeout=30):
    import requests
    headers = {"User-Agent": "Mozilla/5.0 (MumbaiWaterIndex/1.0)"}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.content if as_bytes else r.text


def _llm_extract(raw_text, instruction, schema_hint):
    """Ask Claude to pull structured JSON out of messy page/PDF text."""
    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    prompt = (
        f"{instruction}\n\n"
        f"Return ONLY valid JSON, no prose, no markdown fences, matching this shape:\n"
        f"{schema_hint}\n\n"
        f"If a value is genuinely absent, use null.\n\n"
        f"---- SOURCE TEXT ----\n{raw_text[:12000]}"
    )
    msg = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)


def fetch_lakes_live():
    raw = _get(LAKE_URL)
    schema = ('{"total_ml": number, "total_pct": number, "lakes": '
              '[{"name": string, "pct": number, "rain_today_mm": number, "rain_season_mm": number}]}')
    instr = ("From this BMC Hydraulic Engineer's Department water-stock page, extract the seven "
             "Mumbai lakes (" + ", ".join(LAKES) + "). For each lake give its current % of live "
             "storage capacity, today's rainfall (mm) and season-to-date rainfall since 01 Jun (mm). "
             "Also give the combined total live storage in million litres and its % of total capacity.")
    return _llm_extract(raw, instr, schema)


def fetch_city_rain_live():
    pdf_bytes = _get(IMD_PRESS_PDF, as_bytes=True)
    import pdfplumber, io
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    schema = '{"city_today_mm": number, "city_season_mm": number}'
    instr = ("From this IMD Mumbai press bulletin, extract the Santacruz observatory rainfall: "
             "the last 24-hour total (mm) as city_today_mm, and the seasonal total since 01 Jun "
             "(mm) as city_season_mm.")
    return _llm_extract(text, instr, schema)


def fetch_forecast_precip_live():
    data = json.loads(_get(FORECAST_URL))
    return data["daily"]["precipitation_sum"]  # list, today first


# ----------------------------------------------------------------------------
# HISTORY  (SQLite)  — this log is the moat
# ----------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS daily(
        date TEXT PRIMARY KEY, total_pct REAL, total_ml REAL,
        catchment_mm REAL, city_mm REAL, per_lake TEXT)""")
    return conn


def upsert_today(conn, date, total_pct, total_ml, catchment_mm, city_mm, per_lake):
    conn.execute("REPLACE INTO daily VALUES (?,?,?,?,?,?)",
                 (date, total_pct, total_ml, catchment_mm, city_mm, json.dumps(per_lake)))
    conn.commit()


def get_row(conn, date):
    r = conn.execute("SELECT total_pct,total_ml,per_lake FROM daily WHERE date=?", (date,)).fetchone()
    return None if not r else {"total_pct": r[0], "total_ml": r[1], "per_lake": json.loads(r[2])}


def load_baseline(mmdd):
    """baselines.csv: mmdd,avg5yr_pct,last_year_pct,catch_season_normal_mm,city_season_normal_mm"""
    if not os.path.exists(BASELINES_CSV):
        return {}
    with open(BASELINES_CSV, newline="") as f:
        for row in csv.DictReader(f):
            if row["mmdd"] == mmdd:
                return {k: (float(v) if v not in ("", None) else None) for k, v in row.items() if k != "mmdd"}
    return {}


# ----------------------------------------------------------------------------
# COMPUTE
# ----------------------------------------------------------------------------
def clamp(x, lo=0.0, hi=10.0):
    return max(lo, min(hi, x))


def compute_index(total_pct, total_ml, avg5yr_pct, change_pts, catchment_mm):
    relative = clamp(5 * (total_pct / avg5yr_pct)) if avg5yr_pct else 5.0
    momentum = clamp(5 + change_pts * 2 + min(catchment_mm, 100) / 50)
    days = total_ml / DAILY_SUPPLY_ML
    reserve = clamp(days / RESERVE_FULL_DAYS * 10)
    score = (WEIGHTS["relative"] * relative +
             WEIGHTS["momentum"] * momentum +
             WEIGHTS["reserve"]  * reserve)
    return round(score, 1), {"relative": relative, "momentum": momentum,
                             "reserve": reserve, "days_of_supply": round(days)}


def build_status(sub):
    """Turn sub-scores into the verdict text on frame 1."""
    m, rel, res = sub["momentum"], sub["relative"], sub["reserve"]
    if   m >= 5.5: tag, status = "IMPROVING", "good"
    elif m >= 4.5: tag, status = "HOLDING STEADY", "warn"
    else:          tag, status = "DECLINING", "bad"

    if   res < 3:   sub_line = "but reserves are still low for the date"
    elif rel >= 6:  sub_line = "and ahead of the seasonal norm"
    elif rel >= 4:  sub_line = "and close to the seasonal norm"
    else:           sub_line = "and below normal for the date"

    lead = {"IMPROVING": "Catchment rains are lifting stocks",
            "HOLDING STEADY": "Stocks are broadly flat today",
            "DECLINING": "Stocks slipped today"}[tag]
    tail = ("<b>but reserves stay tight for now.</b>" if res < 3
            else "<b>and the trend is in the city's favour.</b>")
    return tag, status, sub_line, f"{lead} — {tail}"


def bucket_forecast(precip_next7, start_date):
    days = []
    for i, p in enumerate(precip_next7[:7], start=1):
        d = start_date + dt.timedelta(days=i)
        intensity = "heavy" if p >= HEAVY_MM else "moderate" if p >= MODERATE_MM else "light"
        lo, hi = round(p * 0.8), round(p * 1.2)
        days.append({"d": d.strftime("%a").upper()[:3], "dd": d.strftime("%d %b"),
                     "intensity": intensity, "mm": f"{lo}\u2013{hi}"})
    verdict = ("FAVOURABLE" if sum(precip_next7[1:4]) / 3 >= HEAVY_MM
               else "STEADY" if sum(precip_next7[1:4]) / 3 >= MODERATE_MM else "WATCH")
    return verdict, days


# ----------------------------------------------------------------------------
# ASSEMBLE  -> the DATA dict, matching the template's schema exactly
# ----------------------------------------------------------------------------
def assemble(date, lakes, city, precip, conn):
    mmdd = date.strftime("%m-%d")
    iso  = date.strftime("%Y-%m-%d")
    base = load_baseline(mmdd)
    avg5yr   = base.get("avg5yr_pct")
    last_year = base.get("last_year_pct")

    total_pct = lakes["total_pct"]
    total_ml  = lakes["total_ml"]
    catchment_mm = round(sum(l["rain_today_mm"] for l in lakes["lakes"]) / len(lakes["lakes"]))
    catch_season = round(sum(l.get("rain_season_mm") or 0 for l in lakes["lakes"]) / len(lakes["lakes"]), 1)
    city_mm = city["city_today_mm"]

    # day-over-day from history (before we overwrite today's row)
    prev = get_row(conn, (date - dt.timedelta(days=1)).strftime("%Y-%m-%d"))
    if prev:
        change_ml  = round(total_ml - prev["total_ml"])
        change_pts = round(total_pct - prev["total_pct"], 2)
    else:
        change_ml, change_pts = 0, 0.0
    change_dir = "up" if change_ml >= 0 else "down"

    # per-lake trend vs yesterday
    prev_lake = {l["name"]: l["pct"] for l in prev["per_lake"]} if prev else {}
    lakes_out = []
    for l in lakes["lakes"]:
        y = prev_lake.get(l["name"])
        trend = "up" if (y is None or l["pct"] >= y) else "down"
        lakes_out.append({"name": l["name"], "pct": round(l["pct"], 2), "trend": trend})

    score, sub = compute_index(total_pct, total_ml, avg5yr, change_pts, catchment_mm)
    tag, status, sub_line, takeaway = build_status(sub)

    catch_normal = round(catch_season / base["catch_season_normal_mm"] * 100) if base.get("catch_season_normal_mm") else None
    city_normal  = round(city["city_season_mm"] / base["city_season_normal_mm"] * 100) if base.get("city_season_normal_mm") else None

    fc_verdict, fc_days = bucket_forecast(precip, date)

    data = {
        "meta": {"handle": HANDLE, "dateLine": date.strftime("%a %d %b %Y") + " \u00b7 6:00 AM"},
        "index": {"score": score, "status": status, "tag": tag, "sub": sub_line, "takeaway": takeaway},
        "stock": {"pctCapacity": round(total_pct, 2), "liveStorageML": round(total_ml),
                  "changeML": abs(change_ml), "changePct": abs(change_pts), "changeDir": change_dir},
        "vsLastYear": {"pct": round(last_year, 2) if last_year else None,
                       "deltaPts": round(abs(total_pct - last_year), 2) if last_year else None,
                       "dir": "up" if (last_year is not None and total_pct >= last_year) else "down"},
        "vs5yr": {"pct": round(avg5yr, 2) if avg5yr else None,
                  "deltaPts": round(abs(total_pct - avg5yr), 2) if avg5yr else None,
                  "dir": "up" if (avg5yr is not None and total_pct >= avg5yr) else "down"},
        "rain": {"catchmentMM": catchment_mm, "cityMM": city_mm,
                 "verdict": ("Catchment is out-raining the city \u2014 <b>inflows should keep rising.</b>"
                             if catchment_mm > city_mm else
                             "The city is seeing more rain than the catchments today."),
                 "catchmentSeasonMM": catch_season, "catchmentPctNormal": catch_normal,
                 "citySeasonMM": round(city["city_season_mm"], 1), "cityPctNormal": city_normal},
        "lakes": lakes_out, "lakesTotalPct": round(total_pct, 2),
        "outlook": {"verdict": fc_verdict, "days": fc_days},
    }

    upsert_today(conn, iso, total_pct, total_ml, catchment_mm, city_mm, lakes_out)
    data["_debug"] = {"subscores": sub}
    return data


# ----------------------------------------------------------------------------
# VALIDATE + INJECT
# ----------------------------------------------------------------------------
def validate(data):
    w = []
    for l in data["lakes"]:
        if not (0 <= l["pct"] <= 100):
            w.append(f"{l['name']} pct out of range: {l['pct']}")
    if not (0 <= data["stock"]["pctCapacity"] <= 100):
        w.append(f"total pct out of range: {data['stock']['pctCapacity']}")
    if data["vs5yr"]["pct"] is None:
        w.append("no 5-yr baseline for this date (add a row to baselines.csv)")
    if data["vsLastYear"]["pct"] is None:
        w.append("no last-year baseline for this date (add a row to baselines.csv)")
    if data["rain"]["catchmentMM"] > 500:
        w.append(f"catchment rainfall looks high: {data['rain']['catchmentMM']} mm — double-check")
    if len(data["outlook"]["days"]) != 7:
        w.append(f"forecast has {len(data['outlook']['days'])} days, expected 7")
    return w


def inject(data, out_html):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        html = f.read()
    payload = {k: v for k, v in data.items() if not k.startswith("_")}
    block = ("/* MWI-DATA:START — generated by fetch_compute.py, do not hand-edit */\n"
             "const DATA = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n"
             + DATA_END)
    start = html.index(DATA_START)
    end   = html.index(DATA_END) + len(DATA_END)
    html  = html[:start] + block + html[end:]
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="run offline with the reference-day numbers")
    ap.add_argument("--date", help="YYYY-MM-DD (defaults to today)")
    args = ap.parse_args()

    date = (dt.datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
            else dt.date.today())
    os.makedirs(OUT_DIR, exist_ok=True)

    if args.mock:
        lakes, city, precip = MOCK_LAKES, MOCK_CITY, MOCK_FORECAST_PRECIP
    else:
        print("Fetching lake storage (BMC)…");  lakes  = fetch_lakes_live()
        print("Fetching city rainfall (IMD)…"); city   = fetch_city_rain_live()
        print("Fetching forecast (Open-Meteo)…"); precip = fetch_forecast_precip_live()

    conn = db()
    data = assemble(date, lakes, city, precip, conn)

    iso = date.strftime("%Y-%m-%d")
    with open(os.path.join(OUT_DIR, f"{iso}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    out_html = os.path.join(OUT_DIR, f"{iso}.html")
    inject(data, out_html)

    print(f"\nMumbai Water Index — {iso}")
    print(f"  score {data['index']['score']}/10  ({data['index']['tag']})  "
          f"sub-scores {data['_debug']['subscores']}")
    print(f"  stock {data['stock']['pctCapacity']}%  "
          f"day-change {'+' if data['stock']['changeDir']=='up' else '-'}{data['stock']['changeML']} ML")
    print(f"  wrote {out_html}")

    warnings = validate(data)
    if warnings:
        print("\n  ⚠ CHECK BEFORE POSTING:")
        for x in warnings:
            print("   -", x)
    else:
        print("\n  ✓ no validation warnings")


if __name__ == "__main__":
    main()
