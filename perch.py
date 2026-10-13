#!/usr/bin/env python3
"""Perch: compare retirement scenarios side by side. Owl does the planning; this serves the page.

Scenarios live outside the repo, in ~/.perch-scenarios (or $PERCH_DATA): one folder per
household, holding household.json and one file per scenario. The first run fills it with two
made-up sample households.

    python3 perch.py              serve on http://127.0.0.1:8790
    python3 perch.py init         create the data folder from the samples, if it is missing

Set PERCH_HOST=0.0.0.0 to open it from other devices on your network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import date
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
SOLVE_RICH = ROOT / "engine" / "solve.py"
BUILD_HFP = ROOT / "engine" / "build_hfp.py"
RULES = ROOT / "engine" / "rules.json"
PAGE = ROOT / "web" / "index.html"
SAMPLES = ROOT / "samples"
DATA = Path(os.environ.get("PERCH_DATA", Path.home() / ".perch-scenarios")).expanduser()
HOST = os.environ.get("PERCH_HOST", "127.0.0.1")
PORT = int(os.environ.get("PERCH_PORT", "8790"))
ID_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
OWL = Path(os.environ.get("OWL_HOME", ROOT / "vendor" / "Owl"))
PYTHON = OWL / ".venv" / "bin" / "python"

# One folder per household: its people in household.json, and one JSON file per scenario.
# A scenario's id is "<household>/<scenario>".
HOUSE_FILE = "household.json"
# A new scenario starts on 1 January of next year.
NEXT_YEAR = date.today().year + 1

# What the page shows and edits, section by section. The household's numbers live in the
# scenario and household files, not here. "{name}" in a label is each person's name; a field
# marked perPerson holds one value per person.
SECTIONS = [
    {"name": "Scenario", "kind": "Choice", "fields": [
        {"key": "name", "label": "Name", "type": "text", "help": "What this scenario is called in the A and B lists."},
        {"key": "description", "label": "What is different about it", "type": "text", "help": "A short note for yourself. Owl does not read it."},
    ]},
    {"name": "Accounts", "kind": "Fact", "coupleNote": "Owl needs one owner per account, so joint money is split evenly inside the tool. Taxes are joint and the survivor inherits everything, so the split barely matters.", "fields": [
        {"key": "jointValue", "couple": True, "label": "Joint taxable · value", "type": "money", "help": "Brokerage and bank money owned together, on 1 January of the first year. Growth is taxed when sold."},
        {"key": "jointCost", "couple": True, "label": "Joint taxable · cost", "type": "money", "help": "What was originally paid for the joint holdings. Only the part above this is taxed as a gain when sold."},
        {"key": "ownValue", "perPerson": True, "label": "{name} own taxable · value", "type": "money", "help": "Brokerage and bank money in this person's name only."},
        {"key": "ownCost", "perPerson": True, "label": "{name} own taxable · cost", "type": "money", "help": "What was originally paid for this person's own taxable holdings."},
        {"key": "ownNote", "perPerson": True, "label": "{name} own taxable · what it holds", "type": "text", "help": "A note for yourself, for example which accounts. Owl does not read it."},
        {"key": "pretax", "perPerson": True, "label": "{name} pre-tax", "type": "money", "help": "401k, traditional IRA and similar. Taxed as income when taken out; required withdrawals start in your 70s."},
        {"key": "roth", "perPerson": True, "label": "{name} Roth", "type": "money", "help": "Roth IRA and Roth 401k. Never taxed again when taken out."},
    ]},
    {"name": "Social Security benefit", "kind": "Fact", "note": "Copy from each person's SSA statement at ssa.gov. Until then these are estimates.", "fields": [
        {"key": "ssMonthly", "perPerson": True, "label": "{name} · monthly at full retirement age", "type": "money", "help": "The monthly amount at full retirement age (about 67), from the SSA statement. Owl adjusts it for the actual claiming age."},
        {"key": "ssSource", "perPerson": True, "label": "{name} · where the number comes from", "type": "text", "help": "For example 'Estimate' or 'SSA statement 2026'. Owl does not read it."},
    ]},
    {"name": "Social Security claiming", "kind": "Choice", "fields": [
        {"key": "chooseSsAges", "label": "Let Owl choose the claiming ages", "type": "bool", "help": "Owl tries every claiming age for each person and keeps the best. Slower to solve."},
        {"key": "ssAge", "showIf": {"chooseSsAges": False}, "perPerson": True, "label": "{name} claims at, if not left to Owl", "type": "number", "step": "0.01", "help": "The age Social Security starts, 62 to 70. Later means a bigger check for life. Ignored when Owl chooses."},
    ]},
    {"name": "Work", "kind": "Fact and choice", "note": "Pay and savings are per year in today's dollars and grow with prices. While anyone in the household works, the employer's health plan is assumed to cover everyone, so marketplace insurance starts the year after the last one stops.", "fields": [
        {"key": "workUntil", "perPerson": True, "label": "{name} works through", "type": "year", "optional": True, "help": "The last calendar year of work. Leave blank if this person has stopped before the plan starts."},
        {"key": "wages", "perPerson": True, "label": "{name} · taxable pay per year", "type": "money", "help": "Pay as on the W-2, box 1: salary, bonus, and vested stock, after the pre-tax 401k and HSA paycheck savings. Owl taxes it as wages. Used only in the years worked."},
        {"key": "payrollTax", "perPerson": True, "label": "{name} · payroll taxes per year", "type": "money", "help": "Social Security and Medicare taxes taken from pay (W-2 boxes 4 and 6), plus state disability insurance (box 14). Owl does not charge these, so they are taken from cash here."},
        {"key": "pretaxSave", "perPerson": True, "label": "{name} · into the pre-tax 401k per year", "type": "money", "help": "Paycheck savings into the traditional 401k (W-2 box 12, code D). Put the employer's match in the next line."},
        {"key": "matchSave", "perPerson": True, "label": "{name} · employer match per year", "type": "money", "help": "What the employer adds to the pre-tax 401k. It is not on the W-2 and is not taxed until withdrawn."},
        {"key": "rothSave", "perPerson": True, "label": "{name} · into Roth 401k per year", "type": "money", "help": "Roth 401k paycheck savings (box 12, code AA), plus after-tax 401k money moved into Roth inside the plan (the mega backdoor). Both come out of take-home pay."},
        {"key": "hsaSave", "perPerson": True, "label": "{name} · into the HSA per year", "type": "money", "help": "Paycheck HSA savings, yours and the employer's (box 12, code W)."},
    ]},
    {"name": "Real estate", "kind": "Fact and choice", "list": "properties", "note": "A sale adds the proceeds to the taxable accounts; Owl taxes the gain. The mortgage is paid off from the sale."},
    {"name": "Children and college", "kind": "Fact and choice", "list": "children", "note": "College cost is in today's dollars per year.", "fields": [
        {"key": "collegeAbove", "label": "College costs grow faster than prices by, % a year", "type": "number", "step": "0.1", "help": "Each year's college cost rises this much faster than prices, counted from the plan start. 0 means it grows with prices."},
    ]},
    {"name": "Spending", "kind": "Choice", "fields": [
        {"key": "netSpending", "label": "Base per year", "type": "money", "help": "What you spend each year after taxes, in today's dollars. Owl raises it with inflation. The mortgage, college and the bands are on top of this."},
        {"list": "spendingBands", "label": "Extra spending bands"},
        {"key": "cushion", "perPerson": True, "label": "Keep at least this in {name}'s taxable account", "type": "money", "help": "Owl will not let this person's taxable account drop below this amount, in today's dollars, from the second year on. A safety buffer; it can cost some tax."},
    ]},
    {"name": "Market", "kind": "Assumption", "note": "Cash is assumed to earn inflation, as in a money market fund.", "fields": [
        {"key": "useHistoricalRates", "label": "Use the average of real returns 1928–2025", "type": "bool", "help": "Use the average of real market returns from 1928 to 2025 instead of the four numbers below."},
        {"key": "stockReturn", "showIf": {"useHistoricalRates": False}, "label": "Stocks per year, %", "type": "number", "step": "0.1", "help": "Average yearly stock return, before inflation. Ignored when using history."},
        {"key": "bondReturn", "showIf": {"useHistoricalRates": False}, "label": "Corporate bonds per year, %", "type": "number", "step": "0.1", "help": "Average yearly corporate bond return, before inflation. Ignored when using history."},
        {"key": "noteReturn", "showIf": {"useHistoricalRates": False}, "label": "Treasuries per year, %", "type": "number", "step": "0.1", "help": "Average yearly Treasury return, before inflation. Ignored when using history."},
        {"key": "inflation", "showIf": {"useHistoricalRates": False}, "label": "Inflation per year, %", "type": "number", "step": "0.1", "help": "Average yearly inflation. Ignored when using history."},
        {"key": "dividendPercent", "label": "Dividend yield, %", "type": "number", "step": "0.1", "help": "Yearly dividends from the stocks, taxed each year even if not spent."},
        {"key": "stocksNow", "label": "Stocks today, %", "type": "number", "help": "Share of investments in stocks at the start. Whatever stocks, bonds and cash leave goes to Treasuries."},
        {"key": "bondsNow", "label": "Corporate bonds today, %", "type": "number", "help": "Share in corporate bonds at the start."},
        {"key": "cashNow", "label": "Cash today, %", "type": "number", "help": "Share in cash at the start."},
        {"key": "stocksLater", "label": "Stocks at the end of the plan, %", "type": "number", "help": "Share in stocks at the end. Owl shifts gradually from today's mix to this one."},
        {"key": "bondsLater", "label": "Corporate bonds at the end, %", "type": "number", "help": "Share in corporate bonds at the end."},
        {"key": "cashLater", "label": "Cash at the end, %", "type": "number", "help": "Share in cash at the end."},
    ]},
    {"name": "Health", "kind": "Assumption", "note": "", "fields": [
        {"key": "healthSticker", "label": "Marketplace insurance price per year, before 65", "type": "money", "help": "The yearly marketplace (ACA) price for health insurance before subsidies, for the years before Medicare. Owl works out the subsidy."},
        {"key": "healthAbove", "label": "Health costs grow faster than prices by, % a year", "type": "number", "step": "0.1", "help": "The marketplace insurance price, Medicare premiums, and spending bands set to grow like health costs rise this much faster than prices each year. 0 means they grow with prices."},
    ]},
    {"name": "Plan settings", "kind": "Assumption", "note": "", "fields": [
        {"key": "planTo", "perPerson": True, "label": "Plan through {name}'s age", "type": "number", "help": "The age this person's plan runs to. Owl assumes they live exactly this long. Pick an age you'd be unlucky to outlive."},
        {"key": "survivorPercent", "couple": True, "label": "After the first death, the survivor spends this % of the base", "type": "number", "help": "After the first death, the survivor spends this percent of the couple's base amount."},
        {"key": "objective", "label": "What Owl makes as large as possible", "type": "select", "options": [["maxBequest", "What is left at the end"], ["maxSpending", "Yearly spending"]], "help": "What is left at the end: spend exactly the base and bands, and leave as much as possible. Yearly spending: spend as much as possible every year and leave at least the minimum below."},
        {"key": "bequestFloor", "showIf": {"objective": "maxSpending"}, "label": "Least to leave at the end, if maximizing spending", "type": "money", "help": "Used only when maximizing spending: the least that must be left at the end, in today's dollars."},
        {"key": "allowRoth", "label": "Let Owl move pre-tax money into Roth", "type": "bool", "help": "Lets Owl move money from pre-tax into Roth when the tax now is cheaper than the tax later. Owl picks the amounts and years."},
        {"key": "withdrawalOrder", "label": "Which account to spend from", "type": "select", "options": [["optimal", "Owl decides each year"], ["taxable_first", "Taxable, then pre-tax, then Roth"]], "help": "Owl decides: each year Owl picks which accounts to take money from, to pay the least tax. Fixed order: a common rule, taxable first, then pre-tax, then Roth. The fixed order is much slower to solve."},
        {"key": "spendingProfile", "label": "Spending shape over the years", "type": "select", "options": [["flat", "Flat"], ["smile", "Smile"]], "help": "Flat: the same base every year, plus inflation. Smile: a dip in the middle years and a rise late for health care. The first year is the base, so with smile the later years are mostly lower and less is spent overall. Bands come on top either way."},
        {"key": "smileDip", "showIf": {"spendingProfile": "smile"}, "label": "Smile: middle-years dip, %", "type": "number", "help": "Used only with the smile shape. How far spending drops in the middle years. Owl's default is 15."},
        {"key": "smileIncrease", "showIf": {"spendingProfile": "smile"}, "label": "Smile: rise by the end, %", "type": "number", "help": "Used only with the smile shape. How much higher spending is at the end than at the start. Owl's default is 12."},
        {"key": "smileDelay", "showIf": {"spendingProfile": "smile"}, "label": "Smile: flat years before the curve", "type": "number", "help": "Used only with the smile shape. Years of flat spending before the smile starts. Owl's default is 0."},
        {"key": "spendingSlack", "label": "Let yearly spending move up or down by up to, %", "type": "number", "help": "Lets Owl move each year's spending up or down by this much if it saves tax. 0 keeps it exact. When leaving the most is the goal, Owl will use this to spend less."},
        {"key": "heirsRate", "label": "Heirs' income tax on pre-tax money they inherit, %", "type": "number", "help": "The children pay income tax on pre-tax accounts they inherit. Used in the outcome summary and by Owl when it values what is left."},
        {"key": "taxCutsEnd", "label": "Year today's lower tax rates are assumed to end", "type": "year", "help": "Owl assumes the current tax rates expire this year and older, higher rates return. Push it out to assume they stay."},
    ]},
]

# Fields of one item in each list. Amounts are today's dollars.
LISTS = {
    "properties": {"title": "Property", "add": "Add property", "remove": "Remove property", "fields": [
        {"key": "name", "label": "Name", "type": "text", "help": "Any name. Shown in milestones."},
        {"key": "use", "label": "Use", "type": "select", "options": [["live", "Live in"], ["rental", "Rental or share"], ["other", "Other"]], "help": "Live in: a home. Up to $500k of the gain on a sale is tax-free. Rental or other: the whole gain is taxed."},
        {"key": "value", "label": "Value today", "type": "money", "help": "What it would sell for today."},
        {"key": "cost", "label": "Cost (for tax on a sale)", "type": "money", "help": "What was paid for it plus improvements."},
        {"key": "mortgage", "label": "Mortgage left", "type": "money", "help": "What is still owed at the start of the plan. 0 for none."},
        {"key": "rate", "label": "Mortgage rate, %", "type": "number", "step": "0.001", "help": "The mortgage interest rate."},
        {"key": "years", "label": "Mortgage years left", "type": "number", "help": "Whole years of payments left."},
        {"key": "sellYear", "label": "Sell in year (blank to keep)", "type": "year", "optional": True, "help": "The year it is sold. Leave blank to keep it; then it counts in what is left at the end."},
        {"key": "income", "label": "Yearly income (rent, distributions)", "type": "money", "help": "Net yearly income it pays you, in today's dollars, until it is sold. Taxed as ordinary income."},
        {"key": "growth", "label": "Value grows faster than prices by, %", "type": "number", "step": "0.1", "help": "0 means the value keeps up with prices only."},
    ]},
    "children": {"title": "Child", "add": "Add child", "remove": "Remove child", "fields": [
        {"key": "name", "label": "Name", "type": "text", "help": "Shown in milestones."},
        {"key": "born", "label": "Born", "type": "year", "help": "Year of birth."},
        {"key": "collegeStart", "label": "College starts", "type": "year", "help": "First year of college bills."},
        {"key": "years", "label": "Years of college", "type": "number", "help": "How many years of bills. 0 for none."},
        {"key": "cost", "label": "Cost per year, today's dollars", "type": "money", "help": "Tuition, housing and the rest, per year, before any grants."},
    ]},
    "spendingBands": {"title": "Band", "add": "Add band", "remove": "Remove band", "fields": [
        {"key": "name", "label": "Band", "type": "text"},
        {"key": "amount", "label": "Extra per year", "type": "money"},
        {"key": "from", "label": "From", "type": "year"},
        {"key": "to", "label": "Through", "type": "year"},
        {"key": "grows", "label": "Grows like", "type": "select", "options": [["prices", "Prices"], ["health", "Health costs"]]},
        {"key": "kind", "label": "Kind", "type": "select", "options": [["other", "Other"], ["health", "Health"], ["gift", "Gift"]]},
    ]},
}

FIELDS = [f for section in SECTIONS for f in section.get("fields", []) if "key" in f]
INPUT_KEYS = [f["key"] for f in FIELDS if f["key"] not in ("name", "description")] + list(LISTS)


def slug(text: str) -> str:
    raw = re.sub(r"[^a-z0-9]+", "-", (text or "scenario").lower()).strip("-")
    return (raw or "scenario")[:40]


def new_id(household: str, name: str) -> str:
    base = slug(name)
    candidate = base
    n = 2
    while (DATA / household / f"{candidate}.json").exists() or f"{candidate}.json" == HOUSE_FILE:
        candidate = f"{base}-{n}"
        n += 1
    return f"{household}/{candidate}"


STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_STATES: list | None = None


def states() -> list:
    """The states Owl has tax tables for, as [code, name], sorted by name. Asked of Owl once."""
    global _STATES
    if _STATES is None:
        try:
            out = subprocess.run([str(PYTHON), "-c", "from owlplanner.tax_state import valid_states; print(' '.join(valid_states()))"],
                                 capture_output=True, text=True, timeout=60)
            codes = out.stdout.split() if out.returncode == 0 else []
        except OSError:
            codes = []
        _STATES = sorted(([c, STATE_NAMES.get(c, c)] for c in (codes or STATE_NAMES)), key=lambda x: x[1])
    return _STATES


def state_of(raw, fallback: str) -> str:
    code = str(raw or "").strip().upper()
    return code if code in {c for c, _ in states()} else fallback


def check_names(names: list) -> None:
    """Each name becomes a sheet of the workbook Owl reads, so it must be one a sheet can have."""
    seen = set()
    for name in names:
        if len(name) > 31 or re.search(r"[\[\]:*?/\\]", name):
            raise ValueError(f"{name!r}: names can have at most 31 characters and none of [ ] : * ? / \\")
        if name.lower() in seen or name.lower() in ("debts", "fixed assets"):
            raise ValueError(f"{name!r}: each person needs a different name, and not Debts or Fixed Assets.")
        seen.add(name.lower())


def new_household(body: dict) -> dict:
    """Make a household folder with one scenario to start from. The facts start empty; the
    assumptions (market, health prices, plan settings) come from the sample of the same size."""
    people = [p for p in (body.get("people") or []) if isinstance(p, dict) and str(p.get("name") or "").strip()][:2]
    if not people:
        raise ValueError("A household needs at least one person.")
    check_names([str(p["name"]).strip() for p in people])
    name = str(body.get("name") or "").strip() or " & ".join(str(p["name"]).strip() for p in people)
    base, n = slug(name), 2
    ident = base
    house = {
        "name": name,
        "status": "married" if len(people) == 2 else "single",
        "state": state_of(body.get("state"), states()[0][0]),
        "people": [{"name": str(p["name"]).strip(), "born": int(num(p.get("born"), NEXT_YEAR - 50)),
                    "sex": "M" if p.get("sex") == "M" else "F"} for p in people],
    }
    sample = next(f for d in sorted(SAMPLES.iterdir()) if d.is_dir() and len(json.loads((d / HOUSE_FILE).read_text())["people"]) == len(people)
                  for f in scenario_files(d))
    start = json.loads(sample.read_text())
    for section in SECTIONS:
        if "Fact" in section["kind"]:
            for field in section.get("fields", []):
                if "key" in field:
                    start[field["key"]] = [value_of(field, None)] * len(people) if field.get("perPerson") else value_of(field, None)
            if section.get("list"):
                start[section["list"]] = []
    start.update({"name": "Starting point", "description": "Fill in your accounts, Social Security, work and spending, then clone this to try other choices.",
                  "planStart": NEXT_YEAR, "spendingBands": [], "netSpending": 0, "ssSource": ["Estimate"] * len(people)})
    start.pop("result", None)
    # Claimed by making the folder, so two households made at once never share one.
    DATA.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            (DATA / ident).mkdir()
            break
        except FileExistsError:
            ident, n = f"{base}-{n}", n + 1
    (DATA / ident / HOUSE_FILE).write_text(json.dumps(house, indent=2) + "\n")
    doc = clean(dict(start, household=ident), f"{ident}/starting-point", len(people))
    (DATA / ident / "starting-point.json").write_text(json.dumps({k: v for k, v in doc.items() if k not in ("id", "household")}, indent=2) + "\n")
    return dict(house, id=ident, scenario=doc["id"])


def scenario_file(ident: str) -> Path | None:
    """The file behind "<household>/<scenario>", or None for an id that is not well formed."""
    parts = (ident or "").split("/")
    if len(parts) != 2 or not all(ID_OK.match(p) for p in parts) or f"{parts[1]}.json" == HOUSE_FILE:
        return None
    return DATA / parts[0] / f"{parts[1]}.json"


def households() -> list[Path]:
    return sorted(d for d in DATA.iterdir() if (d / HOUSE_FILE).is_file()) if DATA.exists() else []


def scenario_files(household: Path) -> list[Path]:
    return sorted(f for f in household.glob("*.json") if f.name != HOUSE_FILE)


def num(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def flag(value) -> bool:
    return value is True or value == "true" or value == 1


def k(dollars) -> str:
    return f"{num(dollars) / 1000:.3f}"


def mix(stocks, bonds, cash) -> str:
    notes = max(0.0, 100.0 - num(stocks) - num(bonds) - num(cash))
    return f"[ {num(stocks):.1f}, {num(bonds):.1f}, {notes:.1f}, {num(cash):.1f},]"


# Each household folder holds its scenarios' inputs; what Owl worked out from them is kept apart in
# its cache folder, so clearing the cache never touches an input:
#   <household>/cache/solves/<scenario>.json      the plan with average markets (View)
#   <household>/cache/risk/<scenario>/<run>.json  Risk Analysis runs
def cache_dir(household: str) -> Path:
    return DATA / household / "cache"


def solve_file(ident: str) -> Path:
    household, name = ident.split("/")
    return cache_dir(household) / "solves" / f"{name}.json"


def risk_dir(ident: str) -> Path:
    household, name = ident.split("/")
    return cache_dir(household) / "risk" / name


def read_doc(path: Path) -> dict:
    doc = json.loads(path.read_text())
    # The folder says whose scenario it is.
    doc["household"] = path.parent.name
    doc["id"] = f"{path.parent.name}/{path.stem}"
    doc.pop("result", None)
    cached = solve_file(doc["id"])
    if cached.exists():
        try:
            doc["result"] = json.loads(cached.read_text())
        except ValueError:
            pass
    return doc


def write_doc(ident: str, doc: dict) -> None:
    """The inputs to the scenario's file; Owl's plan, if there is one, to the cache."""
    stored = {k: v for k, v in doc.items() if k not in ("id", "household", "result", "stale")}
    scenario_file(ident).write_text(json.dumps(stored, indent=2) + "\n")
    cached = solve_file(ident)
    if doc.get("result"):
        cached.parent.mkdir(parents=True, exist_ok=True)
        write_json(cached, doc["result"])
    elif cached.exists():
        cached.unlink()


def read_household(ident: str) -> dict:
    path = DATA / (ident or "") / HOUSE_FILE
    if not ID_OK.match(ident or "") or not path.exists():
        raise RuntimeError(f"No household {ident!r}")
    house = json.loads(path.read_text())
    house["id"] = ident
    return house


def with_household(doc: dict) -> dict:
    """The scenario plus who it is for, as the case writers need it."""
    if "people" in doc:
        return doc
    house = read_household(doc.get("household") or "")
    return dict(doc, people=house["people"], status=house.get("status", "married"), state=house.get("state", "CA"))


def value_of(field: dict, raw):
    kind = field["type"]
    if kind == "bool":
        return flag(raw)
    if kind == "text":
        return str(raw or "").strip()
    if kind == "select":
        allowed = [opt[0] for opt in field["options"]]
        return raw if raw in allowed else allowed[0]
    if field.get("optional") and (raw is None or raw == ""):
        return None
    if kind == "year":
        return int(num(raw))
    return num(raw)


def clean(body: dict, ident: str, people: int) -> dict:
    doc = {
        "id": ident,
        "name": str(body.get("name") or ident).strip() or ident,
        "description": str(body.get("description") or "").strip(),
        "household": str(body.get("household") or ""),
        "planStart": int(num(body.get("planStart"), NEXT_YEAR)),
    }
    for field in FIELDS:
        key = field["key"]
        if key in ("name", "description"):
            continue
        if field.get("perPerson"):
            given = body.get(key) if isinstance(body.get(key), list) else []
            doc[key] = [value_of(field, given[i] if i < len(given) else None) for i in range(people)]
        else:
            doc[key] = value_of(field, body.get(key))
    for name, spec in LISTS.items():
        rows = body.get(name) if isinstance(body.get(name), list) else []
        doc[name] = [{f["key"]: value_of(f, row.get(f["key"])) for f in spec["fields"]} for row in rows if isinstance(row, dict)]
    return doc


def inputs_of(doc: dict) -> dict:
    return {key: doc.get(key) for key in INPUT_KEYS}


def write_toml(path: Path, spec: dict, stem: str = "scenario") -> None:
    spec = with_household(spec)
    people = spec["people"]
    count = len(people)
    # A later start year and a price level let the yearly re-planning reuse this case.
    start = int(spec.get("startYear") or spec.get("planStart") or NEXT_YEAR)
    price = num(spec.get("priceFactor"), 1) or 1.0

    def each(values, fmt):
        return "[ " + "".join(fmt(v) + ", " for v in values[:count]) + "]"

    joint_value = num(spec.get("jointValue")) / count
    joint_cost = num(spec.get("jointCost")) / count
    # The yearly re-planning hands over each person's balances directly.
    taxable = spec.get("taxableNow") or [joint_value + num(v) for v in spec["ownValue"]]
    basis = spec.get("basisNow") or [joint_cost + num(v) for v in spec["ownCost"]]
    pretax = spec.get("pretaxNow") or spec["pretax"]
    roth = spec.get("rothNow") or spec["roth"]
    now = mix(spec["stocksNow"], spec["bondsNow"], spec["cashNow"])
    later = mix(spec["stocksLater"], spec["bondsLater"], spec["cashLater"])
    both = f"[ {now}, {later},]"
    if flag(spec.get("useHistoricalRates")):
        rates = 'method = "historical_average"\nfrom = 1928\nto = 2025\n'
    else:
        rates = (
            'method = "user"\nvalues = [ '
            f'{num(spec["stockReturn"]):.2f}, {num(spec["bondReturn"]):.2f}, '
            f'{num(spec["noteReturn"]):.2f}, {num(spec["inflation"]):.2f},]\n'
        )
    roth_line = "" if flag(spec.get("allowRoth")) else "maxRothConversion = 0\n"
    ages = "optimize" if flag(spec.get("chooseSsAges")) else "fixed"
    objective = spec.get("objective") or "maxBequest"
    # JSON's string escapes are valid in TOML, so quotes, backslashes and newlines stay intact.
    # Characters are kept as they are (TOML rejects JSON's escapes for emoji).
    blurb = json.dumps(str(spec.get("description") or ""), ensure_ascii=False)[1:-1]
    quoted = lambda v: json.dumps(str(v), ensure_ascii=False)
    workbook = "scenario.xlsx" if stem == "scenario" else f"HFP_{stem}.xlsx"
    text = f"""case_name = "{stem}"
description = "{blurb}"

[basic_info]
status = "{spec.get("status", "married") if count > 1 else "single"}"
names = {each([p["name"] for p in people], quoted)}
sexes = {each([p.get("sex", "F") for p in people], quoted)}
date_of_birth = {each([f'{int(p["born"])}-01-01' for p in people], quoted)}
life_expectancy = {each(spec["planTo"], lambda v: str(int(num(v))))}
start_date = "{start}-01-01"
state = "{spec.get("state", "CA")}"

[savings_assets]
taxable_savings_balances = {each(taxable, k)}
taxable_cost_basis = {each(basis, k)}
tax_deferred_savings_balances = {each(pretax, k)}
tax_free_savings_balances = {each(roth, k)}
hsa_savings_balances = {each([0] * count, k)}
beneficiary_fractions = [ 1.0, 1.0, 1.0, 1.0,]
spousal_surplus_deposit_fraction = 0.5

[household_financial_profile]
HFP_file_name = "{workbook}"

[fixed_income]
pension_monthly_amounts = {each([0] * count, k)}
pension_ages = {each([65] * count, lambda v: f"{v:.1f}")}
pension_indexed = {each([0] * count, lambda v: "false")}
pension_survivor_fraction = {each([0] * count, lambda v: "0.0")}
social_security_pia_amounts = {each(spec["ssMonthly"], lambda v: f"{num(v) * price:.2f}")}
social_security_ages = {each(spec["ssAge"], lambda v: f"{num(v):.2f}")}

[rates_selection]
heirs_rate_on_tax_deferred_estate = {num(spec.get("heirsRate"), 30):.1f}
dividend_rate = {num(spec["dividendPercent"]):.2f}
obbba_expiration_year = {int(num(spec.get("taxCutsEnd"), 2032))}
{rates}
[asset_allocation]
interpolation_method = "s-curve"
interpolation_center = {max(0.0, 15.0 - (start - int(spec.get("planStart") or NEXT_YEAR))):.1f}
interpolation_width = 5.0
type = "individual"
generic = {each([both] * count, str)}

[optimization_parameters]
spending_profile = "{spec.get("spendingProfile") or "flat"}"
smile_dip = {int(num(spec.get("smileDip"), 15))}
smile_increase = {int(num(spec.get("smileIncrease"), 12))}
smile_delay = {int(num(spec.get("smileDelay"), 0))}
surviving_spouse_spending_percent = {int(num(spec["survivorPercent"]))}
objective = "{objective}"
medical_inflation_excess = {num(spec.get("healthAbove"), 0):.2f}

[aca_settings]
slcsp_annual = {num(spec["healthSticker"]) * price / 1000:.3f}
aca_start_year = {max([start] + [int(y) + 1 for y in spec.get("workUntil") or [] if y])}

[solver_options]
units = "k"
netSpending = {num(spec["netSpending"]) * price / 1000:.3f}
bequest = {num(spec["bequestFloor"]) * price / 1000:.3f}
noRothConversions = "none"
{roth_line}solver = "HiGHS"
withMedicare = "loop"
withACA = "loop"
withLTCG = "loop"
withNIIT = "loop"
withSSTaxability = "loop"
withSSAges = "{ages}"
withdrawalOrder = "{spec.get("withdrawalOrder") or "optimal"}"
spendingSlack = {num(spec.get("spendingSlack")):.0f}
minTaxableBalance = {each(spec.get("cushion") or [0] * count, lambda v: f"{num(v) * price / 1000:.3f}")}

[results]
default_plots = "today"
worksheet_real_dollars = true
"""
    path.write_text(text)


def solve(doc: dict) -> dict:
    if not PYTHON.exists():
        raise RuntimeError(f"Owl is not installed at {OWL}")
    spec = with_household(doc)
    with tempfile.TemporaryDirectory(prefix="perch-") as folder:
        folder = Path(folder)
        write_toml(folder / "scenario.toml", spec)
        spec_path = folder / "spec.json"
        spec_path.write_text(json.dumps(spec))
        built = subprocess.run(
            [str(PYTHON), str(BUILD_HFP), str(spec_path), str(folder / "scenario.xlsx")],
            capture_output=True,
            text=True,
        )
        if built.returncode != 0:
            raise RuntimeError((built.stderr or "workbook failed")[-2000:])
        result_path = folder / "result.json"
        env = dict(os.environ, OWL_START_YEAR=str(int(spec.get("planStart") or NEXT_YEAR)))
        proc = subprocess.run(
            [str(PYTHON), str(SOLVE_RICH), str(folder / "scenario.toml"), str(result_path)],
            cwd=SOLVE_RICH.parent,
            capture_output=True,
            text=True,
            timeout=900,
            env=env,
        )
        if proc.returncode != 0 or not result_path.exists():
            raise RuntimeError((proc.stderr or proc.stdout or "Owl failed")[-2000:])
        return json.loads(result_path.read_text())


# ---------- risk: one plan across many market paths ----------
# Each path is one Owl solve that knows that path's returns in advance: real history starting in
# each possible year, or random five-year blocks of history. Social Security ages are held at the
# ones Owl chose for the plan, since choosing them again makes every solve minutes long.
# Market kinds: real history from each start year, Owl's historical bootstrap, or Owl's GARCH
# model fitted to that history, with one seed per path. Plain names and one-line explanations for the page.
RISK_MODELS = {
    "history": ("Real history", "Each path replays the real markets that followed one past year: one path for every start year history allows."),
    "random": ("Random history", "Owl's historical bootstrap: each path strings together stretches of real years, picked at random from all of history, so good and bad years come in a new order. Stretches average five years, as Owl's documentation recommends."),
    "garch_dcc": ("Calm and stormy spells", "Volatile years cluster together, and assets move more alike in a storm."),
}
RISK_KINDS = tuple(RISK_MODELS)
RANDOM_PATHS = 100
RISK_WORKERS = max(2, (os.cpu_count() or 4) - 2)
JOBS: dict = {}
JOBS_LOCK = threading.Lock()
# Risk result files are read, changed and written back by several jobs; one lock keeps those whole.
RISK_FILE_LOCK = threading.Lock()


def write_json(file: Path, data) -> None:
    """Written whole or not at all, so a page loading it never reads half a file."""
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_name(file.name + ".tmp")
    temp.write_text(json.dumps(data))
    os.replace(temp, file)


def full_rows(timeline: list) -> list:
    """Every number Owl gives for each year of a path, kept so the page can draw the path in full.
    The per-person to-do details are left out; they are only shown for the plan itself."""
    return [{k: v for k, v in r.items() if k != "people"} for r in timeline]


def solve_path(args: list, env: dict) -> subprocess.CompletedProcess:
    """One Risk solve. One that runs past the time limit counts as unsolved, not as a failed run."""
    try:
        return subprocess.run(args, cwd=SOLVE_RICH.parent, capture_output=True, text=True, timeout=900, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, "", "Owl took too long on this path.")


def run_id(data: dict) -> str | None:
    """Which run a results file holds; older files have only the minute they ran."""
    return data.get("runId") or data.get("ranAt")


def set_runout(file: Path, value: int, found: dict, run: str | None) -> None:
    """Keep a path's run-out year with its run, if that run is still the one in the file."""
    with RISK_FILE_LOCK:
        if not file.exists():
            return
        data = json.loads(file.read_text())
        if run is not None and run_id(data) != run:
            return
        for p in data["paths"]:
            if p["value"] == value:
                p["runout"] = found
        write_json(file, data)
_HISTORY: list | None = None


def history_rows() -> list:
    """Owl's market history: year, stocks, bonds, notes, inflation, in percent."""
    global _HISTORY
    if _HISTORY is None:
        out = subprocess.run([str(PYTHON), "-c", "from owlplanner.rates import df; import json; "
                              "print(json.dumps(df[['year','S&P 500','Bonds Baa','T-Notes','Inflation']].values.tolist()))"],
                             capture_output=True, text=True, timeout=60, check=True)
        _HISTORY = json.loads(out.stdout)
    return _HISTORY


def market_path(kind: str, value: int, years: int) -> list:
    """The yearly returns of real history from a start year, as Owl's data has them."""
    return [r[1:] for r in history_rows() if value <= r[0] < value + years]


def fingerprint(doc: dict) -> str:
    """Changes whenever anything Owl would see changes: the inputs, the people, or the plan it chose."""
    spec = with_household(doc)
    seen = {"inputs": inputs_of(doc), "people": spec["people"], "state": spec.get("state"),
            "ssAges": (doc.get("result") or {}).get("ssAges")}
    return hashlib.sha256(json.dumps(seen, sort_keys=True).encode()).hexdigest()[:16]


def risk_file(ident: str, kind: str, life: bool = False, spend: bool = False) -> Path:
    return risk_dir(ident) / f"{kind}{'-life' if life else ''}{'-spend' if spend else ''}.json"


def path_setup(kind: str, value: int, years: int, life: bool) -> tuple:
    """What one Risk path hands Owl, and its label: the same for the run and for later looks at it."""
    first, last = int(history_rows()[0][0]), int(history_rows()[-1][0])
    if kind == "history":
        market = {"rates": market_path("history", value, years)}
        label = f"Markets of {value}–{value + years - 1}"
    elif kind == "random":
        # Owl's own historical bootstrap, with the settings its documentation recommends.
        market = {"model": {"method": "historical_bootstrap", "seed": value, "from": first, "to": last,
                            "bootstrap_type": "stationary", "block_size": 5}}
        label = f"Path {value}"
    else:
        market = {"model": {"method": kind, "seed": value, "from": first, "to": last}}
        label = f"Path {value}"
    if life:
        market["lifespanSeed"] = 100003 * value + 17
    market["maxTime"] = PATH_SOLVE_SECONDS
    return market, label


def risk_case(doc: dict, folder: Path) -> dict:
    """Write the scenario's case into folder, Social Security held at the plan's ages; return its spec."""
    result = doc.get("result") or {}
    spec = with_household(doc)
    if spec.get("chooseSsAges") and result.get("ssAges"):
        spec = dict(spec, chooseSsAges=False, ssAge=result["ssAges"])
    write_toml(folder / "scenario.toml", spec)
    (folder / "spec.json").write_text(json.dumps(spec))
    built = subprocess.run([str(PYTHON), str(BUILD_HFP), str(folder / "spec.json"), str(folder / "scenario.xlsx")], capture_output=True, text=True)
    if built.returncode != 0:
        raise RuntimeError((built.stderr or "workbook failed")[-2000:])
    return spec


def run_risk(ident: str, kind: str, life: bool, spend: bool, job: dict) -> dict:
    doc = read_doc(scenario_file(ident))
    result = doc.get("result") or {}
    years = len(result.get("timeline") or [])
    if not years:
        raise RuntimeError("Solve the scenario first.")
    spec = with_household(doc)
    if spec.get("chooseSsAges") and result.get("ssAges"):
        spec = dict(spec, chooseSsAges=False, ssAge=result["ssAges"])
    first, last = int(history_rows()[0][0]), int(history_rows()[-1][0])
    values = list(range(first, last - years + 2)) if kind == "history" else list(range(1, RANDOM_PATHS + 1))
    job["total"] = len(values)
    env = dict(os.environ, OWL_START_YEAR=str(int(spec.get("planStart") or NEXT_YEAR)), OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    with tempfile.TemporaryDirectory(prefix="perch-risk-") as folder:
        folder = Path(folder)
        write_toml(folder / "scenario.toml", spec)
        (folder / "spec.json").write_text(json.dumps(spec))
        built = subprocess.run([str(PYTHON), str(BUILD_HFP), str(folder / "spec.json"), str(folder / "scenario.xlsx")], capture_output=True, text=True)
        if built.returncode != 0:
            raise RuntimeError((built.stderr or "workbook failed")[-2000:])

        def one(value: int) -> dict:
            over, out = folder / f"path-{value}-in.json", folder / f"path-{value}.json"
            market, label = path_setup(kind, value, years, life)
            if spend:
                # The most this path can spend every year, on top of the spending bands.
                market["objective"] = "maxSpending"
            over.write_text(json.dumps(market))
            proc = solve_path([str(PYTHON), str(SOLVE_RICH), str(folder / "scenario.toml"), str(out), str(over)], env)
            with JOBS_LOCK:
                job["done"] += 1
            # The market this path lived through, kept whether or not a plan pays for everything in it.
            returns_file = Path(str(out) + ".returns.json")
            returns = json.loads(returns_file.read_text()) if returns_file.exists() else None
            if proc.returncode != 0 or not out.exists():
                text = proc.stderr + proc.stdout
                return {"value": value, "label": label, "solved": False, "short": "No plan satisfies" in text, "returns": returns,
                        "error": "" if "No plan satisfies" in text else text[-600:]}
            data = json.loads(out.read_text())
            return {"value": value, "label": label, "solved": True, "bequest": data["bequest"], "spending": data.get("spending"), "returns": returns,
                    "lifespans": data.get("lifespans"), "rows": full_rows(data["timeline"]),
                    "convergence": data.get("convergence"), "residual": data.get("residual"),
                    "timeline": [{"year": r["year"], "worth": round(r["taxable"] + r["pretax"] + r["roth"] + r["realEstate"] - r["debt"]),
                                  "tax": r["tax"], "conv": r["rothMoved"]} for r in data["timeline"]]}

        with ThreadPoolExecutor(RISK_WORKERS) as pool:
            paths = list(pool.map(one, values))
    return {"kind": kind, "life": life, "spend": spend, "netSpending": doc.get("netSpending"), "fingerprint": fingerprint(doc), "ranAt": time.strftime("%Y-%m-%d %H:%M"),
            "runId": f"{time.time():.6f}",
            "history": [first, last], "years": years, "ssAges": result.get("ssAges"), "paths": paths}


def find_runout(ident: str, kind: str, life: bool, value: int) -> dict:
    """For a path that ran short: the last year the plan still pays for everything, and the plan to then.

    Owl returns no plan when the money runs out, so the same path is solved again ending earlier
    and earlier, halving the gap each time, until the last year that works is found."""
    doc = read_doc(scenario_file(ident))
    timeline = (doc.get("result") or {}).get("timeline") or []
    if not timeline:
        raise RuntimeError("Solve the scenario first.")
    years, start, end = len(timeline), timeline[0]["year"], timeline[-1]["year"]
    market, label = path_setup(kind, value, years, life)
    env = dict(os.environ, OWL_START_YEAR=str(start), OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    with tempfile.TemporaryDirectory(prefix="perch-runout-") as folder:
        folder = Path(folder)
        risk_case(doc, folder)

        def attempt(last_year: int):
            over, out = folder / f"h-{last_year}-in.json", folder / f"h-{last_year}.json"
            over.write_text(json.dumps(dict(market, horizonYear=last_year)))
            solve_path([str(PYTHON), str(SOLVE_RICH), str(folder / "scenario.toml"), str(out), str(over)], env)
            return json.loads(out.read_text()) if out.exists() else None

        # Owl cannot plan a horizon that ends before anyone reaches Medicare age, so the search starts
        # there; if even that plan runs short, the money runs out before then.
        medicare = max(start, min(int(p["born"]) + 65 for p in with_household(doc)["people"]))
        best = attempt(medicare) if medicare < end else None
        if best is None:
            return {"value": value, "label": label, "lastYear": None, "before": medicare}
        # With drawn lifespans a path can outlive the plan's own ages; any year past everyone's
        # drawn end is the full path, which runs short.
        if life:
            end = max(end, max(int(p["born"]) for p in with_household(doc)["people"]) + 121)
        lo, hi = medicare, end   # the plan to `lo` works; the full plan does not
        while hi - lo > 1:
            mid = (lo + hi) // 2
            got = attempt(mid)
            if got:
                lo, best = mid, got
            else:
                hi = mid
        if best["timeline"][-1]["year"] != lo:
            best = attempt(lo)
    if not best:
        return {"value": value, "label": label, "lastYear": None, "before": lo + 1}
    rows = [{"year": r["year"], "worth": round(r["taxable"] + r["pretax"] + r["roth"] + r["realEstate"] - r["debt"]),
             "liquid": round(r["taxable"] + r["pretax"] + r["roth"])} for r in best["timeline"]]
    return {"value": value, "label": label, "lastYear": lo, "convergence": best.get("convergence"), "timeline": rows,
            "rows": full_rows(best["timeline"])}


def start_runout_all(ident: str, kind: str, life: bool) -> dict:
    """Find when the money runs out on every path of a run that ran short, several at a time."""
    key = (ident, kind, life, "runout")
    with JOBS_LOCK:
        if key in JOBS and JOBS[key].get("running"):
            return JOBS[key]
        if JOBS.get((ident, kind, life, False), {}).get("running"):
            return {"running": False, "error": "Wait for the run to finish."}
        job = JOBS[key] = {"running": True, "done": 0, "total": 0, "error": "", "began": time.time()}
    file = risk_file(ident, kind, life, False)

    def work():
        try:
            with RISK_FILE_LOCK:
                data = json.loads(file.read_text())
            run = run_id(data)
            todo = [p["value"] for p in data["paths"] if not p["solved"] and p.get("short") and not p.get("runout")]
            job["total"] = len(todo)

            def one(value):
                found = find_runout(ident, kind, life, value)
                with JOBS_LOCK:
                    job["done"] += 1
                # Saved as each one finishes, so the page fills in while the rest are running.
                set_runout(file, value, found, run)
                return found

            with ThreadPoolExecutor(RISK_WORKERS) as pool:
                list(pool.map(one, todo))
            if todo:
                # Per round of paths running side by side: each path's search takes about as long.
                record_timing("runout", time.time() - job["began"], -(-len(todo) // RISK_WORKERS))
        except Exception as exc:  # the page shows it
            job["error"] = str(exc)[-1000:]
        job["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return job


def start_risk(ident: str, kind: str, life: bool, spend: bool) -> dict:
    key = (ident, kind, life, spend)
    with JOBS_LOCK:
        if key in JOBS and JOBS[key].get("running"):
            return JOBS[key]
        if not spend and JOBS.get((ident, kind, life, "runout"), {}).get("running"):
            return {"running": False, "error": "Wait for the search for when the money runs out to finish."}
        job = JOBS[key] = {"running": True, "done": 0, "total": 0, "error": "", "began": time.time()}

    def work():
        try:
            data = run_risk(ident, kind, life, spend, job)
            record_timing("paths", time.time() - job["began"], len(data["paths"]))
            with RISK_FILE_LOCK:
                write_json(risk_file(ident, kind, life, spend), data)
        except Exception as exc:  # the page shows it
            job["error"] = str(exc)[-1000:]
        job["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return job


def risk_state(ident: str, kind: str, life: bool, spend: bool) -> dict:
    file = risk_file(ident, kind, life, spend)
    data = json.loads(file.read_text()) if file.exists() else None
    if data and kind == "history":
        # Runs from before paths kept their returns: real history's are known from Owl's data.
        start = int(((read_doc(scenario_file(ident)).get("result") or {}).get("timeline") or [{"year": NEXT_YEAR}])[0]["year"])
        for p in data["paths"]:
            if not p.get("returns"):
                p["returns"] = [[start + k] + list(row) for k, row in enumerate(market_path("history", p["value"], data["years"]))]
    current = fingerprint(read_doc(scenario_file(ident)))
    return {"kind": kind, "life": life, "spend": spend, "job": JOBS.get((ident, kind, life, spend)), "result": data,
            "runoutJob": JOBS.get((ident, kind, life, "runout")),
            "stale": bool(data) and data.get("fingerprint") != current, "workers": RISK_WORKERS}


# ---------- this year's Roth conversion: what committing to an amount costs ----------
# For each start year of real history, Owl first plans with hindsight and finds that path's own
# best conversion for the first year. Then each path is solved again with the first year's
# conversion locked at each amount on a grid, everything after free to adapt. The cost of an
# amount on a path is how much less is left at the end than with the hindsight choice.
REGRET_GRID = 9
PATH_SOLVE_SECONDS = 20  # per solve in Risk runs; normal paths take a few seconds


def regret_file(ident: str) -> Path:
    return risk_dir(ident) / "regret.json"


def run_regret(ident: str, job: dict) -> dict:
    doc = read_doc(scenario_file(ident))
    result = doc.get("result") or {}
    timeline = result.get("timeline") or []
    if not timeline:
        raise RuntimeError("Solve the scenario first.")
    years = len(timeline)
    first_moves = [p.get("convert", 0) for p in timeline[0].get("people") or []]
    if not first_moves:
        raise RuntimeError("Solve the scenario again: this one was solved before Perch kept each person's moves.")
    person = max(range(len(first_moves)), key=lambda i: first_moves[i])
    spec = with_household(doc)
    if spec.get("chooseSsAges") and result.get("ssAges"):
        spec = dict(spec, chooseSsAges=False, ssAge=result["ssAges"])
    first, last = int(history_rows()[0][0]), int(history_rows()[-1][0])
    starts = list(range(first, last - years + 2))
    env = dict(os.environ, OWL_START_YEAR=str(int(spec.get("planStart") or NEXT_YEAR)), OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    with tempfile.TemporaryDirectory(prefix="perch-regret-") as folder:
        folder = Path(folder)
        write_toml(folder / "scenario.toml", spec)
        (folder / "spec.json").write_text(json.dumps(spec))
        built = subprocess.run([str(PYTHON), str(BUILD_HFP), str(folder / "spec.json"), str(folder / "scenario.xlsx")], capture_output=True, text=True)
        if built.returncode != 0:
            raise RuntimeError((built.stderr or "workbook failed")[-2000:])

        def one(task: tuple) -> dict:
            start, locked = task
            tag = f"{start}-{'free' if locked is None else int(locked)}"
            over, out = folder / f"r-{tag}-in.json", folder / f"r-{tag}.json"
            fixed = [None] * len(first_moves)
            if locked is not None:
                fixed[person] = float(locked)
            over.write_text(json.dumps({"rates": market_path("history", start, years), "fixedRoth": fixed, "maxTime": PATH_SOLVE_SECONDS}))
            proc = solve_path([str(PYTHON), str(SOLVE_RICH), str(folder / "scenario.toml"), str(out), str(over)], env)
            with JOBS_LOCK:
                job["done"] += 1
            if proc.returncode != 0 or not out.exists():
                return {"start": start, "locked": locked, "solved": False, "short": "No plan satisfies" in proc.stderr + proc.stdout}
            data = json.loads(out.read_text())
            row = data["timeline"][0]
            return {"start": start, "locked": locked, "solved": True, "left": data["bequest"], "convergence": data.get("convergence"),
                    "first": (row.get("people") or [{}] * (person + 1))[person].get("convert", 0)}

        # Phase one: hindsight, to see where each path's own best first-year conversion falls.
        job["total"] = len(starts) * (REGRET_GRID + 1)
        with ThreadPoolExecutor(RISK_WORKERS) as pool:
            free = list(pool.map(one, [(s, None) for s in starts]))
        best = sorted(r["first"] for r in free if r["solved"])
        if not best:
            raise RuntimeError("No path could be solved.")
        # The grid spans the paths' own choices and the plan's, with some room either side.
        low = max(0.0, min(best[0], first_moves[person]) - 40_000)
        high = max(best[-1], first_moves[person]) + 40_000
        grid = sorted({round(low + (high - low) * k / (REGRET_GRID - 1), -3) for k in range(REGRET_GRID)} | {round(first_moves[person], -3)})
        job["total"] = len(starts) * (len(grid) + 1)
        with ThreadPoolExecutor(RISK_WORKERS) as pool:
            locked = list(pool.map(one, [(s, g) for s in starts for g in grid]))
    names = [p["name"] for p in spec["people"]]
    return {"fingerprint": fingerprint(doc), "ranAt": time.strftime("%Y-%m-%d %H:%M"), "person": person, "name": names[person],
            "planFirst": first_moves[person], "grid": grid, "free": free, "locked": locked}


def start_regret(ident: str) -> dict:
    key = (ident, "regret")
    with JOBS_LOCK:
        if key in JOBS and JOBS[key].get("running"):
            return JOBS[key]
        job = JOBS[key] = {"running": True, "done": 0, "total": 0, "error": "", "began": time.time()}

    def work():
        try:
            data = run_regret(ident, job)
            record_timing("regret-perch", time.time() - job["began"], len(data["free"]) + len(data["locked"]))
            write_json(regret_file(ident), data)
        except Exception as exc:  # the page shows it
            job["error"] = str(exc)[-1000:]
        job["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return job


def regret_state(ident: str) -> dict:
    file = regret_file(ident)
    data = json.loads(file.read_text()) if file.exists() else None
    current = fingerprint(read_doc(scenario_file(ident)))
    return {"job": JOBS.get((ident, "regret")), "result": data, "stale": bool(data) and data.get("fingerprint") != current,
            "workers": RISK_WORKERS}


# ---------- Owl's own tools, run unchanged ----------
OWL_TOOLS = ("frontier", "regret")
OWL_TOOLS_SCRIPT = ROOT / "engine" / "owl_tools.py"


def owl_tool_file(ident: str, tool: str) -> Path:
    return risk_dir(ident) / f"owl-{tool}.json"


def run_owl_tool(ident: str, tool: str, job: dict) -> dict:
    doc = read_doc(scenario_file(ident))
    result = doc.get("result") or {}
    if not result.get("timeline"):
        raise RuntimeError("Solve the scenario first.")
    spec = with_household(doc)
    if spec.get("chooseSsAges") and result.get("ssAges"):
        spec = dict(spec, chooseSsAges=False, ssAge=result["ssAges"])
    extra = []
    if tool == "regret":
        moves = [p.get("convert", 0) for p in result["timeline"][0].get("people") or []]
        if not moves:
            raise RuntimeError("Solve the scenario again first.")
        extra = [str(max(range(len(moves)), key=lambda i: moves[i]))]
    env = dict(os.environ, OWL_START_YEAR=str(int(spec.get("planStart") or NEXT_YEAR)))
    with tempfile.TemporaryDirectory(prefix="perch-owl-") as folder:
        folder = Path(folder)
        write_toml(folder / "scenario.toml", spec)
        (folder / "spec.json").write_text(json.dumps(spec))
        built = subprocess.run([str(PYTHON), str(BUILD_HFP), str(folder / "spec.json"), str(folder / "scenario.xlsx")], capture_output=True, text=True)
        if built.returncode != 0:
            raise RuntimeError((built.stderr or "workbook failed")[-2000:])
        progress, out = folder / "progress.json", folder / "result.json"
        # Its messages go to a file: a pipe nobody reads until the end fills up and stops the tool.
        log = open(folder / "stderr.txt", "w")
        proc = subprocess.Popen([str(PYTHON), str(OWL_TOOLS_SCRIPT), str(folder / "scenario.toml"), str(out), str(progress), tool] + extra,
                                cwd=OWL_TOOLS_SCRIPT.parent, stdout=subprocess.DEVNULL, stderr=log, text=True, env=env)
        while proc.poll() is None:
            time.sleep(1)
            try:
                p = json.loads(progress.read_text())
                job["phase"], job["done"], job["total"] = p.get("phase", ""), p.get("done", 0), p.get("total", 0)
            except (OSError, ValueError):
                pass
        log.close()
        err = (folder / "stderr.txt").read_text(errors="replace")
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(err[-1500:] or "Owl's tool failed")
        data = json.loads(out.read_text())
    data.update({"fingerprint": fingerprint(doc), "ranAt": time.strftime("%Y-%m-%d %H:%M"),
                 "names": [p["name"] for p in spec["people"]], "netSpending": doc.get("netSpending")})
    if extra:
        data["person"] = int(extra[0])
    return data


def start_owl_tool(ident: str, tool: str) -> dict:
    key = (ident, "owl", tool)
    with JOBS_LOCK:
        if key in JOBS and JOBS[key].get("running"):
            return JOBS[key]
        job = JOBS[key] = {"running": True, "done": 0, "total": 0, "phase": "", "error": "", "began": time.time()}

    def work():
        try:
            data = run_owl_tool(ident, tool, job)
            write_json(owl_tool_file(ident, tool), data)
            record_timing(f"owl-{tool}", time.time() - job["began"], 1)
        except Exception as exc:  # the page shows it
            job["error"] = str(exc)[-1000:]
        job["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return job


def owl_tool_state(ident: str, tool: str) -> dict:
    file = owl_tool_file(ident, tool)
    data = json.loads(file.read_text()) if file.exists() else None
    current = fingerprint(read_doc(scenario_file(ident)))
    return {"job": JOBS.get((ident, "owl", tool)), "result": data, "stale": bool(data) and data.get("fingerprint") != current}


# ---------- how long an analysis takes on this computer ----------
# Seconds per run (Owl's tools) or per path (Perch's runs), from the last runs here; until a run
# has happened, from runs measured while building Perch.
TIMINGS_DEFAULT = {"paths": 1.6, "regret-perch": 0.95, "owl-frontier": 25.0, "owl-regret": 870.0, "runout": 15.0}


def timings_file() -> Path:
    return DATA / "timings.json"


def timings() -> dict:
    try:
        return dict(TIMINGS_DEFAULT, **json.loads(timings_file().read_text()))
    except (OSError, ValueError):
        return dict(TIMINGS_DEFAULT)


def record_timing(kind: str, seconds: float, units: int) -> None:
    if units <= 0:
        return
    with JOBS_LOCK:
        known = timings()
        known[kind] = round(seconds / units, 3)
        try:
            timings_file().write_text(json.dumps({k: v for k, v in known.items()}))
        except OSError:
            pass


def export_owl(ident: str) -> tuple[str, bytes]:
    """The scenario as the two files Owl's own app opens, in a zip: its case and its workbook."""
    import io
    import zipfile
    doc = read_doc(scenario_file(ident))
    spec = with_household(doc)
    stem = slug(doc.get("name") or ident.split("/")[1])
    start = int(spec.get("planStart") or NEXT_YEAR)
    with tempfile.TemporaryDirectory(prefix="perch-export-") as folder:
        folder = Path(folder)
        write_toml(folder / f"Case_{stem}.toml", spec, stem)
        (folder / "spec.json").write_text(json.dumps(spec))
        built = subprocess.run([str(PYTHON), str(BUILD_HFP), str(folder / "spec.json"), str(folder / f"HFP_{stem}.xlsx")], capture_output=True, text=True)
        if built.returncode != 0:
            raise RuntimeError((built.stderr or "workbook failed")[-2000:])
        note = (f"{doc.get('name')}, exported from Perch.\n\n"
                f"In Owl's app, open Case_{stem}.toml on the Create Case page, then load HFP_{stem}.xlsx on the "
                f"Household Financial Profile page.\n\n"
                f"The plan starts on 1 January {start}. Owl's app always starts a plan on 1 January of the current "
                f"year, so until then it will run this plan one year early: the balances are treated as this "
                f"year's, and the workbook's rows begin in {start}.\n\n"
                f"Amounts in the workbook are in each year's own dollars, as Owl expects.\n")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
            zipped.write(folder / f"Case_{stem}.toml", f"Case_{stem}.toml")
            zipped.write(folder / f"HFP_{stem}.xlsx", f"HFP_{stem}.xlsx")
            zipped.writestr("README.txt", note)
    return f"{stem}-for-owl.zip", buffer.getvalue()


class BadRequest(Exception):
    """Input the server cannot use; answered with 400."""


class Handler(BaseHTTPRequestHandler):
    # A scenario's routes are matched by position, "/api/scenarios/<household>/<scenario>/<rest>",
    # so a scenario or household named like a route (export, risk, owl) still loads.
    @staticmethod
    def _scenario(path: str) -> tuple:
        if not path.startswith("/api/scenarios/"):
            return None, None
        parts = path[len("/api/scenarios/"):].split("/")
        return "/".join(parts[:2]), parts[2:]

    # Bad input gets a 400 and anything else a 500, rather than a dropped connection.
    def do_GET(self):
        self._guard(self._get)

    def do_POST(self):
        self._guard(self._post)

    def do_PUT(self):
        self._guard(self._put)

    def do_DELETE(self):
        self._guard(self._delete)

    def _guard(self, handle) -> None:
        try:
            handle()
        except BadRequest as exc:
            self._send(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._send(500, {"error": str(exc)[-1000:]})

    def _get(self):
        path = urlparse(self.path).path
        ident, rest = self._scenario(path)
        if path == "/api/config":
            self._send(200, {"sections": SECTIONS, "lists": LISTS, "rules": json.loads(RULES.read_text()), "states": states(),
                                 "history": [int(history_rows()[0][0]), int(history_rows()[-1][0])],
                                 "riskModels": [[k, v[0], v[1]] for k, v in RISK_MODELS.items()],
                                 "timings": timings(), "workers": RISK_WORKERS, "randomPaths": RANDOM_PATHS, "regretGrid": REGRET_GRID, "pathSolveSeconds": PATH_SOLVE_SECONDS})
            return
        if path == "/api/households":
            rows = []
            for folder in households():
                try:
                    rows.append({"id": folder.name, "name": json.loads((folder / HOUSE_FILE).read_text()).get("name") or folder.name})
                except json.JSONDecodeError:
                    continue
            self._send(200, rows)
            return
        if path.startswith("/api/households/"):
            try:
                self._send(200, read_household(path.rsplit("/", 1)[-1]))
            except RuntimeError:
                self._send(404, {"error": "not found"})
            return
        if path == "/api/scenarios":
            self._send(200, self._list())
            return
        if rest == ["regret"]:
            if scenario_file(ident) is None or not scenario_file(ident).exists():
                self._send(404, {"error": "not found"})
                return
            self._send(200, regret_state(ident))
            return
        if rest and len(rest) == 2 and rest[0] == "owl":
            tool = rest[1]
            if scenario_file(ident) is None or not scenario_file(ident).exists() or tool not in OWL_TOOLS:
                self._send(404, {"error": "not found"})
                return
            self._send(200, owl_tool_state(ident, tool))
            return
        if rest and len(rest) == 2 and rest[0] == "risk":
            kind = rest[1]
            if scenario_file(ident) is None or not scenario_file(ident).exists() or kind not in RISK_KINDS:
                self._send(404, {"error": "not found"})
                return
            query = urlparse(self.path).query
            self._send(200, risk_state(ident, kind, "life=1" in query, "spend=1" in query))
            return
        if rest == ["export"]:
            if scenario_file(ident) is None or not scenario_file(ident).exists():
                self._send(404, {"error": "not found"})
                return
            try:
                name, raw = export_owl(ident)
            except RuntimeError as exc:
                self._send(500, {"error": str(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if rest == []:
            file = scenario_file(ident)
            if file is None or not file.exists():
                self._send(404, {"error": "not found"})
                return
            self._send(200, read_doc(file))
            return
        if path in ("/", "/index.html"):
            self._file_response(PAGE, "text/html; charset=utf-8")
            return
        self._send(404, {"error": "not found"})

    def _post(self):
        path = urlparse(self.path).path
        ident, rest = self._scenario(path)
        if path == "/api/households":
            try:
                self._send(201, new_household(self._body()))
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            return
        if path.startswith("/api/households/") and path.endswith("/clear-cache"):
            household = path[len("/api/households/"):-len("/clear-cache")]
            try:
                read_household(household)
            except RuntimeError:
                self._send(404, {"error": "not found"})
                return
            with JOBS_LOCK:
                busy = any(job.get("running") and str(key[0]).startswith(household + "/") for key, job in JOBS.items())
            if busy:
                self._send(409, {"error": "Wait for the runs in progress to finish."})
                return
            shutil.rmtree(cache_dir(household), ignore_errors=True)
            self._send(200, {"cleared": household})
            return
        if path == "/api/scenarios":
            body = self._body()
            source = body.get("copyFrom")
            if source:
                file = scenario_file(str(source))
                if file is None or not file.exists():
                    self._send(404, {"error": "not found"})
                    return
                copied = read_doc(file)
                copied.pop("id", None)
                copied["name"] = body.get("name") or ((copied.get("name") or "Scenario") + " copy")
                if "description" in body:
                    copied["description"] = str(body.get("description") or "")
                result = copied.pop("result", None)
                body = copied
            else:
                result = None
            household = str(body.get("household") or "")
            try:
                people = len(read_household(household)["people"])
            except RuntimeError as exc:
                self._send(400, {"error": str(exc)})
                return
            ident = new_id(household, body.get("name") or "scenario")
            doc = clean(body, ident, people)
            if result:
                # A copy has the same inputs, so it has the same plan until it is edited.
                doc["result"] = result
            self._write(ident, doc)
            self._send(201, doc)
            return
        if rest == ["regret"]:
            if scenario_file(ident) is None or not scenario_file(ident).exists():
                self._send(404, {"error": "not found"})
                return
            start_regret(ident)
            self._send(202, regret_state(ident))
            return
        if rest and len(rest) == 2 and rest[0] == "owl":
            tool = rest[1]
            if scenario_file(ident) is None or not scenario_file(ident).exists() or tool not in OWL_TOOLS:
                self._send(404, {"error": "not found"})
                return
            start_owl_tool(ident, tool)
            self._send(202, owl_tool_state(ident, tool))
            return
        if rest and len(rest) == 3 and rest[0] == "risk" and rest[2] == "runout-all":
            kind = rest[1]
            life = "life=1" in urlparse(self.path).query
            if scenario_file(ident) is None or not scenario_file(ident).exists() or kind not in RISK_KINDS or not risk_file(ident, kind, life, False).exists():
                self._send(404, {"error": "not found"})
                return
            start_runout_all(ident, kind, life)
            self._send(202, risk_state(ident, kind, life, False))
            return
        if rest and len(rest) == 3 and rest[0] == "risk" and rest[2] == "runout":
            kind = rest[1]
            query = urlparse(self.path).query
            life = "life=1" in query
            try:
                value = int(next((q.split("=")[1] for q in query.split("&") if q.startswith("value=")), "0"))
            except ValueError:
                raise BadRequest("value must be a whole number")
            if scenario_file(ident) is None or not scenario_file(ident).exists() or kind not in RISK_KINDS:
                self._send(404, {"error": "not found"})
                return
            file = risk_file(ident, kind, life, False)
            run = run_id(json.loads(file.read_text())) if file.exists() else None
            try:
                found = find_runout(ident, kind, life, value)
            except Exception as exc:
                self._send(500, {"error": str(exc)[-1000:]})
                return
            # Kept with the run, so it shows next time without solving again.
            set_runout(file, value, found, run)
            self._send(200, found)
            return
        if rest and len(rest) == 2 and rest[0] == "risk":
            kind = rest[1]
            life, spend = "life=1" in urlparse(self.path).query, "spend=1" in urlparse(self.path).query
            if scenario_file(ident) is None or not scenario_file(ident).exists() or kind not in RISK_KINDS:
                self._send(404, {"error": "not found"})
                return
            if life and kind == "history":
                self._send(400, {"error": "Real history is too short for drawn lifespans; pick a market model."})
                return
            start_risk(ident, kind, life, spend)
            self._send(202, risk_state(ident, kind, life, spend))
            return
        if rest == ["solve"]:
            file = scenario_file(ident)
            if file is None or not file.exists():
                self._send(404, {"error": "not found"})
                return
            doc = read_doc(file)
            solved_for = with_household(doc)
            began = time.monotonic()
            try:
                result = solve(doc)
            except Exception as exc:
                self._send(500, {"error": str(exc)})
                return
            result["seconds"] = round(time.monotonic() - began, 1)
            # The scenario may have been edited while Owl was solving; keep the plan only if not.
            current = read_doc(file)
            if inputs_of(current) == inputs_of(doc) and with_household(current).get("people") == solved_for["people"] \
                    and with_household(current).get("state") == solved_for["state"]:
                current["result"] = result
                self._write(ident, current)
                self._send(200, current)
            else:
                self._send(409, {"error": "edited while solving", "doc": current})
            return
        self._send(404, {"error": "not found"})

    def _put(self):
        path = urlparse(self.path).path
        if path.startswith("/api/households/"):
            ident = path.rsplit("/", 1)[-1]
            try:
                house = read_household(ident)
            except RuntimeError:
                self._send(404, {"error": "not found"})
                return
            body = self._body()
            people = body.get("people") if isinstance(body.get("people"), list) else []
            if len(people) != len(house["people"]):
                self._send(400, {"error": "The number of people cannot change here."})
                return
            renamed = [str(p.get("name") or "").strip() for p in people]
            try:
                check_names([name or old["name"] for name, old in zip(renamed, house["people"])])
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            house["people"] = [
                {"name": name or old["name"], "born": int(num(p.get("born"), old["born"])),
                 "sex": p.get("sex") if p.get("sex") in ("F", "M") else old.get("sex", "F")}
                for name, p, old in zip(renamed, people, house["people"])
            ]
            # A household named after its people follows their new names.
            was = " & ".join(old["name"] for old in json.loads((DATA / ident / HOUSE_FILE).read_text())["people"])
            house["name"] = str(body.get("name") or "").strip() or (" & ".join(p["name"] for p in house["people"]) if house["name"] == was else house["name"])
            house["state"] = state_of(body.get("state"), house.get("state", ""))
            house.pop("id", None)
            (DATA / ident / HOUSE_FILE).write_text(json.dumps(house, indent=2) + "\n")
            # Every plan for this household now needs a fresh solve.
            for file in scenario_files(DATA / ident):
                doc = read_doc(file)
                if "result" in doc:
                    doc.pop("result")
                    self._write(doc["id"], doc)
            house["id"] = ident
            self._send(200, house)
            return
        if not path.startswith("/api/scenarios/"):
            self._send(404, {"error": "not found"})
            return
        ident = path[len("/api/scenarios/"):]
        file = scenario_file(ident)
        if file is None or not file.exists():
            self._send(404, {"error": "not found"})
            return
        previous = read_doc(file)
        body = dict(self._body(), household=previous.get("household"), planStart=previous.get("planStart"))
        doc = clean(body, ident, len(read_household(previous["household"])["people"]))
        if inputs_of(previous) == inputs_of(doc) and "result" in previous:
            doc["result"] = previous["result"]
        self._write(ident, doc)
        self._send(200, doc)

    def _delete(self):
        path = urlparse(self.path).path
        ident = path[len("/api/scenarios/"):] if path.startswith("/api/scenarios/") else ""
        file = scenario_file(ident)
        if file is None or not file.exists():
            self._send(404, {"error": "not found"})
            return
        file.unlink()
        solve_file(ident).unlink(missing_ok=True)
        shutil.rmtree(risk_dir(ident), ignore_errors=True)
        self._send(200, {"deleted": ident})

    def _list(self) -> list:
        rows = []
        for folder in households():
            for file in scenario_files(folder):
                try:
                    doc = json.loads(file.read_text())
                except json.JSONDecodeError:
                    continue
                ident = f"{folder.name}/{file.stem}"
                rows.append({"id": ident, "name": doc.get("name") or file.stem, "household": folder.name, "solved": solve_file(ident).exists()})
        return rows

    def _write(self, ident: str, doc: dict) -> None:
        # The id and household come from where the file is, so they are not stored in it.
        write_doc(ident, doc)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode())
        except ValueError:
            raise BadRequest("The request body is not valid JSON.")
        if not isinstance(body, dict):
            raise BadRequest("The request body must be a JSON object.")
        return body

    def _send(self, code: int, payload) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _file_response(self, path: Path, content_type: str) -> None:
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}")


def init_data() -> bool:
    """Create the data folder from the samples. Never touches a folder that already exists."""
    if DATA.exists():
        return False
    shutil.copytree(SAMPLES, DATA)
    DATA.chmod(0o700)
    return True


def move_to_cache() -> None:
    """Data from before the cache folders: plans inside scenario files, Risk runs in .risk, timings
    in .timings.json. Moved where they now live; the inputs stay as they were."""
    old = DATA / ".timings.json"
    if old.exists() and not timings_file().exists():
        old.rename(timings_file())
    for folder in households():
        files = scenario_files(folder)
        for file in files:
            raw = json.loads(file.read_text())
            if "result" in raw:
                if raw["result"]:
                    write_json(solve_file(f"{folder.name}/{file.stem}"), raw.pop("result"))
                raw.pop("result", None)
                raw.pop("stale", None)
                file.write_text(json.dumps(raw, indent=2) + "\n")
        risk = folder / ".risk"
        if risk.is_dir():
            names = sorted((f.stem for f in files), key=len, reverse=True)
            for run in list(risk.glob("*.json")):
                # "<scenario>-<run>.json", the longest scenario name that fits; a scenario name may
                # itself contain dashes.
                name = next((n for n in names if run.name.startswith(n + "-")), None)
                if name is None:
                    continue  # from a scenario since deleted: left in .risk
                target = risk_dir(f"{folder.name}/{name}") / run.name[len(name) + 1:]
                target.parent.mkdir(parents=True, exist_ok=True)
                run.rename(target)
            if not any(risk.iterdir()):
                risk.rmdir()


def main() -> None:
    made = init_data()
    move_to_cache()
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        print(f"{'Created' if made else 'Already there:'} {DATA}")
        return
    if made:
        print(f"Created {DATA} with two sample households.")
    if not PYTHON.exists():
        print(f"Owl is not set up at {OWL}. Run ./setup.sh first.", file=sys.stderr)
    print(f"Perch on http://{'localhost' if HOST in ('127.0.0.1', '0.0.0.0') else HOST}:{PORT}  ·  data in {DATA}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
