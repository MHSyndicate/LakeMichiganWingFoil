#!/usr/bin/env python3
"""
Lake Michigan wing-foil conditions generator.

What it does:
  1. Pulls a live forecast for each spot from api.weather.gov (NWS).
     The NWS API returns structured JSON and is not subject to the stale
     HTML-mirror caching we ran into, so no DWML workaround is needed.
  2. Buckets the hourly grid into the next few local (CDT) days and
     summarises wind direction / speed / gust / wave height / storms.
  3. Applies the per-spot sailable-direction map and the two rider
     profiles, with a few routing rules (Gillson->Greenwood, etc.).
  4. Writes report.md and a styled, self-contained index.html.

Run live:   python generate.py
Run a demo: python generate.py --demo     (uses baked-in sample data,
                                            no network — good for testing layout)

This is a v1: the data fetch is real, and the logic is a faithful first
cut of the rules we worked out by hand. Sanity-check the first few real
runs against what you actually see on the water and tune the SPOTS map
and the thresholds below.
"""

import sys
import re
import math
import datetime as dt
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
DEMO = "--demo" in sys.argv

# 16-point compass, N first, 22.5 deg steps.
DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def deg_to_compass(deg):
    return DIRS[int((deg % 360) / 22.5 + 0.5) % 16]


# --------------------------------------------------------------------------
# Spot definitions. `sail` is the set of compass directions (onshore + side)
# that work at that spot. `inland` spots are flat, short-fetch, and not hurt
# by offshore wind. Tune lat/lon and the sail sets as you dial spots in.
# --------------------------------------------------------------------------
SPOTS = [
    dict(key="gillson", name="Gillson Beach, Wilmette IL", lat=42.0772, lon=-87.6829,
         faces="ENE/NE", inland=False,
         sail={"N", "NE", "ENE", "E", "NW", "NNW"},
         source="Wilmette Buoy 45174 + LMZ741",
         live="https://iiseagrant.org/wilmettebuoy/"),
    dict(key="greenwood", name="Greenwood Beach, Evanston IL", lat=42.0460, lon=-87.6730,
         faces="E", inland=False,
         sail={"SW", "S", "SSE", "SE", "E", "NE", "N", "NW"},
         source="Wilmette Buoy 45174 + LMZ741",
         live="https://www.glerl.noaa.gov/metdata/chi/"),
    dict(key="montrose", name="Montrose Beach, Chicago IL", lat=41.9640, lon=-87.6300,
         faces="SE/S", inland=False,
         sail={"N", "NE", "ENE", "E", "SE"},
         source="Harrison-Dever CHII2 + LMZ741/742",
         live="https://www.glerl.noaa.gov/metdata/chi/"),
    dict(key="waukegan", name="Waukegan Beach, Waukegan IL", lat=42.3636, lon=-87.8120,
         faces="E", inland=False,
         sail={"N", "NE", "ENE", "E", "SE"},
         source="IISG Waukegan Buoy 45186 + LMZ740",
         live="https://www.ndbc.noaa.gov/station_page.php?station=45186"),
    dict(key="miller", name="Miller Beach, Gary IN", lat=41.6160, lon=-87.2650,
         faces="N", inland=False,
         sail={"W", "WNW", "NW"},  # direct N is poor here
         source="Burns Harbor BHRI3 + LMZ744",
         live="https://www.ndbc.noaa.gov/station_page.php?station=bhri3"),
    dict(key="wolf", name="Wolf Lake, Hammond IN", lat=41.6670, lon=-87.5130,
         faces="inland", inland=True,
         sail=set(DIRS), best={"SW", "SSW"},
         source="NWS land point-forecast"),
    dict(key="andrea", name="Lake Andrea, Pleasant Prairie WI", lat=42.5530, lon=-87.9330,
         faces="inland", inland=True,
         sail=set(DIRS),
         source="Kenosha Airport KENW",
         live="https://forecast.weather.gov/data/obhistory/KENW.html"),
    dict(key="silver", name="Silver Beach, St. Joseph MI", lat=42.1080, lon=-86.4930,
         faces="W/NW", inland=False,
         sail={"W", "WSW", "SW", "WNW", "NW", "N", "S"},
         source="LMZ043 + St. Joseph buoy"),
    dict(key="pere", name="Pere Marquette, Muskegon MI", lat=43.2270, lon=-86.3380,
         faces="W/WSW", inland=False,
         sail={"W", "WSW", "SW", "NW", "N", "S"},
         source="LMZ847 + Muskegon buoy"),
    dict(key="nusail", name="Northwestern Sailing Center, Evanston IL",
         lat=42.0536, lon=-87.6716, faces="E", inland=False, exp=False,
         sail={"S", "SE", "E", "ENE", "NE"},
         source="Wilmette Buoy 45174 + LMZ741",
         live="https://www.glerl.noaa.gov/metdata/chi/",
         webcam="https://www.youtube.com/live/-c9WI2Owp0I"),
]

# Profile thresholds (knots).
EP_LO, EP_HI = 14, 28      # Early Progressor
EXP_LO, EXP_HI = 14, 39    # Experienced
WAVE_FLAG_FT = 2           # Experienced wave go-flag when waves exceed this
MIN_HOURS = 2              # sustained wind must hold at/above the floor this many
                           # daytime hours for a green; brief peaks don't count
N_DAYS = 4                 # forecast horizon

# Early Progressor section shows ONLY these spots, in this order: inland lakes
# first (safer for progressing), then managed / east-facing launches.
EP_SPOTS = ["andrea", "wolf", "waukegan", "greenwood", "nusail"]
BY_KEY = {s["key"]: s for s in SPOTS}

KMH_TO_KT = 0.539957
M_TO_FT = 3.28084
UA = {"User-Agent": "lake-mi-wingfoil/1.0 (personal conditions page)",
      "Accept": "application/geo+json"}


# --------------------------------------------------------------------------
# Fetch + parse
# --------------------------------------------------------------------------
def _http_json(url):
    import requests
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def _parse_duration(d):
    """ISO-8601 duration -> hours (handles P#DT#H, PT#H, PT#M forms)."""
    m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", d)
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    mins = int(m.group(3) or 0)
    return max(1, days * 24 + hours + (1 if mins else 0))


def _expand(values, scale=1.0):
    """NWS grid 'values' (validTime '<iso>/<dur>') -> {utc_hour: number}."""
    out = {}
    for v in values or []:
        val = v.get("value")
        if val is None:
            continue
        start_s, dur_s = v["validTime"].split("/")
        start = dt.datetime.fromisoformat(start_s)
        for h in range(_parse_duration(dur_s)):
            out[start + dt.timedelta(hours=h)] = val * scale
    return out


def fetch_spot(spot):
    """Return per-day summaries for a spot from the live NWS grid."""
    pt = _http_json(f"https://api.weather.gov/points/{spot['lat']},{spot['lon']}")
    grid = _http_json(pt["properties"]["forecastGridData"])["properties"]

    wdir = _expand(grid.get("windDirection", {}).get("values"))
    wspd = _expand(grid.get("windSpeed", {}).get("values"), KMH_TO_KT)
    wgst = _expand(grid.get("windGust", {}).get("values"), KMH_TO_KT)
    wave = _expand(grid.get("waveHeight", {}).get("values"), M_TO_FT)

    storms = set()
    for v in grid.get("weather", {}).get("values", []):
        types = " ".join(str(w.get("weather")) for w in (v.get("value") or []))
        if "thunderstorm" in types.lower():
            start_s, dur_s = v["validTime"].split("/")
            start = dt.datetime.fromisoformat(start_s)
            for h in range(_parse_duration(dur_s)):
                storms.add((start + dt.timedelta(hours=h)).astimezone(CENTRAL).date())

    return summarise(wdir, wspd, wgst, wave, storms)


def summarise(wdir, wspd, wgst, wave, storm_days):
    """Bucket hourly series into the next N local days (daytime 8am-7pm)."""
    today = dt.datetime.now(CENTRAL).date()
    days = []
    for i in range(N_DAYS):
        day = today + dt.timedelta(days=i)
        dirs, spds, gsts, wvs = [], [], [], []
        for utc, val in wspd.items():
            loc = utc.astimezone(CENTRAL)
            if loc.date() == day and 8 <= loc.hour <= 19:
                spds.append(val)
                if utc in wdir:
                    dirs.append(wdir[utc])
                if utc in wgst:
                    gsts.append(wgst[utc])
                if utc in wave:
                    wvs.append(wave[utc])
        if not spds:
            days.append(dict(date=day, calm=True))
            continue
        # circular mean of direction
        if dirs:
            sx = sum(math.sin(math.radians(d)) for d in dirs)
            cx = sum(math.cos(math.radians(d)) for d in dirs)
            mean_dir = deg_to_compass(math.degrees(math.atan2(sx, cx)))
        else:
            mean_dir = "—"
        days.append(dict(
            date=day, calm=False, dir=mean_dir,
            wmin=round(min(spds)), wmax=round(max(spds)),
            spds=[round(v) for v in spds],
            gust=round(max(gsts)) if gsts else None,
            wave=round(max(wvs), 1) if wvs else None,
            storm=day in storm_days,
        ))
    return days


# --------------------------------------------------------------------------
# Decision logic
# --------------------------------------------------------------------------
def dir_ok(spot, d):
    """Sailable if d is in the set, or sits between two sailable directions."""
    if d in spot["sail"]:
        return True
    i = DIRS.index(d)
    return DIRS[(i - 1) % 16] in spot["sail"] and DIRS[(i + 1) % 16] in spot["sail"]


def steady_hours(s, floor):
    """How many daytime hours sit at or above the sailable floor."""
    spds = s.get("spds")
    if spds is None:  # demo/fallback rows without an hourly series
        return MIN_HOURS if s.get("wmax", 0) >= floor else 0
    return sum(1 for v in spds if v >= floor)


def assess(spot, s, profile):
    """Return (status, note). status in {'go','warn','no'}."""
    if s.get("calm"):
        return ("no", "too light")
    d = s["dir"]
    if not spot["inland"] and not dir_ok(spot, d):
        return ("no", f"{d} is offshore/cross here")
    if profile == "EP":
        if not spot["inland"] and s.get("wave") and s["wave"] > 2:
            return ("no", "too wavy to progress")
        if steady_hours(s, EP_LO) < MIN_HOURS:
            return ("no", "wind only briefly in range")
        if s["wmin"] > EP_HI:
            return ("no", "too strong")
        return ("go", f"{s['wmin']}-{s['wmax']} kt, flat")
    else:  # Experienced
        if steady_hours(s, EXP_LO) < MIN_HOURS:
            return ("no", "wind only briefly in range")
        if s["wmin"] > EXP_HI:
            return ("no", "overpowered")
        status = "warn" if s["storm"] else "go"
        note = f"{s['wmin']}-{s['wmax']} kt"
        if s.get("gust"):
            note += f" G{s['gust']}"
        if s.get("wave") and s["wave"] > WAVE_FLAG_FT:
            note += f", ~{s['wave']:g} ft wave day"
        if s["storm"]:
            note += " — storm hours, check timing"
        return (status, note)


def routing_note(spot, s):
    """A few of the hand rules, surfaced as advice when they apply."""
    if spot["key"] == "gillson" and not s.get("calm"):
        if s["dir"] == "SE":
            return "SE — sail Greenwood instead"
        if s["dir"] in {"N", "NNE", "NE", "ENE"} and ((s.get("gust") or 0) > 22 or (s.get("wave") or 0) > 3):
            return "running big — move to Greenwood"
    if spot["key"] == "miller" and not s.get("calm") and s["dir"] == "N":
        return "direct N is poor here"
    if spot["key"] == "wolf" and not s.get("calm") and s["dir"] in spot.get("best", set()):
        return "SW/SSW — prime here"
    return ""


GLYPH = {"go": "🟢", "warn": "⚠️", "no": "⚪"}


def spot_label(spot):
    """First-column label: name links to the live reading (or webcam) when present."""
    url = spot.get("live") or spot.get("webcam")
    name = f"[{spot['name']}]({url})" if url else spot["name"]
    return f"{name} ({spot['faces']})"


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------
def build_report(data):
    """data: {spot_key: days[]}. Returns markdown string."""
    issued = dt.datetime.now(CENTRAL).strftime("%a, %b %-d %Y, %-I:%M %p CDT")
    dates = [data[SPOTS[0]["key"]][i]["date"] for i in range(N_DAYS)]
    headers = []
    for i, d in enumerate(dates):
        label = "Today" if i == 0 else d.strftime("%a")
        headers.append(f"{label} {d.strftime('%-m/%-d')}")

    # Find the next Experienced window (soonest 'go', prefer bigger wave / wind).
    best = None
    for i in range(N_DAYS):
        for spot in SPOTS:
            if not spot.get("exp", True):
                continue
            s = data[spot["key"]][i]
            st, note = assess(spot, s, "EXP")
            rn = routing_note(spot, s)
            sends_away = rn and ("Greenwood" in rn or "instead" in rn)
            if st == "go" and not sends_away:
                score = (-i, (s.get("wave") or 0), s.get("wmax", 0))
                if best is None or score > best[0]:
                    best = (score, spot, s, i, note)
    # Early Progressor window — only the EP spots, in priority order
    ep_hit = None
    for i in range(N_DAYS):
        for key in EP_SPOTS:
            spot = BY_KEY[key]
            st, _ = assess(spot, data[spot["key"]][i], "EP")
            if st == "go":
                ep_hit = (spot, data[spot["key"]][i], i)
                break
        if ep_hit:
            break

    L = []
    L.append("# Lake Michigan Wing Foil Conditions & Recommendations")
    L.append("")
    L.append(f"**Issued:** {issued}  ")
    L.append(f"**Window:** {dates[0].strftime('%a %b %-d')} -> "
             f"{dates[-1].strftime('%a %b %-d, %Y')}  ")
    L.append("**Profiles:** Early Progressor (14-28 kt, inland-first, flat) · "
             "Experienced (14-39 kt, waves >2 ft, no storm hours)")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Summary")
    L.append("")
    if best:
        _, sp, s, i, note = best
        when = "today" if i == 0 else dates[i].strftime("%A")
        L.append(f"Next window: **{when} at {sp['name']}** — {note}.")
    else:
        L.append("No clearly sailable window for the Experienced profile in the next "
                 f"{N_DAYS} days.")
    if ep_hit:
        sp, s, i = ep_hit
        when = "today" if i == 0 else dates[i].strftime("%A")
        L.append(f" Early Progressor: **{when} at {sp['name']}** "
                 f"({s['wmin']}-{s['wmax']} kt, flat).")
    else:
        L.append(" Early Progressor: no suitable day in range "
                 "(inland stays under 16 kt or it's too wavy).")
    L.append("")
    L.append("**Color key:** 🟢 sailable · ⚠️ sailable with a caveat (storm hours) · "
             "⚪ not sailable. Times are CDT.")
    L.append("")
    L.append("---")
    L.append("")

    # Experienced grid
    L.append("## Experienced — next 4 days")
    L.append("")
    L.append("| Spot (faces) | " + " | ".join(headers) + " |")
    L.append("|" + "---|" * (N_DAYS + 1))
    for spot in SPOTS:
        if not spot.get("exp", True):
            continue
        cells = []
        for i in range(N_DAYS):
            s = data[spot["key"]][i]
            st, note = assess(spot, s, "EXP")
            rn = routing_note(spot, s)
            txt = f"{GLYPH[st]} {note}"
            if rn:
                txt += f" ({rn})"
            cells.append(txt)
        L.append(f"| {spot_label(spot)} | " + " | ".join(cells) + " |")
    L.append("")

    # Early Progressor grid — only the EP spots, in the specified order
    L.append("## Early Progressor — next 4 days")
    L.append("")
    L.append("| Spot (faces) | " + " | ".join(headers) + " |")
    L.append("|" + "---|" * (N_DAYS + 1))
    for key in EP_SPOTS:
        spot = BY_KEY[key]
        cells = []
        for i in range(N_DAYS):
            s = data[spot["key"]][i]
            st, note = assess(spot, s, "EP")
            cells.append(f"{GLYPH[st]} {note}")
        L.append(f"| {spot_label(spot)} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("---")
    L.append("")

    # Wind sources
    L.append("## Wind Sources")
    L.append("")
    L.append("| Spot | Sailable directions | Source | Live |")
    L.append("|---|---|---|---|")
    for spot in SPOTS:
        order = [d for d in DIRS if d in spot["sail"]]
        links = []
        if spot.get("live"):
            links.append(f"[live reading]({spot['live']})")
        if spot.get("webcam"):
            links.append(f"[webcam]({spot['webcam']})")
        live = " · ".join(links) if links else "—"
        L.append(f"| {spot['name']} | {', '.join(order)} | {spot['source']} | {live} |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Notes")
    L.append("")
    L.append("- Forecasts are live from the NWS API (api.weather.gov) per spot; "
             "wave height shows where NWS publishes it for that point.")
    L.append("- Routing rules applied: Gillson -> Greenwood on SE or when N/NE runs "
             "big; Miller wants W/WNW/NW (direct N is poor); Wolf Lake is prime on "
             "SW/SSW.")
    L.append("- Re-check a live buoy (CHII2, Wilmette 45174, Burns Harbor meter, "
             "KENW) before you commit — this is a forecast, not an observation.")
    L.append("")
    return "\n".join(L), best, dates


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
CSS = """
:root{
  --bg:#eef3f5;--surface:#fff;--ink:#13242e;--muted:#62737e;--line:#dde6ea;
  --lake:#0c6b77;--lake-2:#083e46;--lake-3:#0a525c;
  --go:#137a47;--go-bg:#e7f4ec;--go-edge:#1e9e63;
  --no-ink:#7c8a93;--no-bg:#f4f7f8;--warn-bg:#fdf3e3;--storm:#b9740c;
  --fd:'Space Grotesk',ui-sans-serif,system-ui,sans-serif;
  --fb:'Inter',ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--fb);
  font-size:16px;line-height:1.55}
a{color:var(--lake)}
.band{background:linear-gradient(150deg,var(--lake-2),var(--lake-3) 60%,var(--lake));color:#eaf6f7}
.band .inner{max-width:980px;margin:0 auto;padding:2.4rem 1.25rem 2rem}
.eyebrow{font-family:var(--fd);text-transform:uppercase;letter-spacing:.18em;
  font-size:.7rem;font-weight:600;color:#8fd0d6;margin:0 0 .6rem}
h1{font-family:var(--fd);font-weight:600;letter-spacing:-.01em;
  font-size:clamp(1.7rem,4.5vw,2.5rem);line-height:1.08;margin:0 0 1rem;color:#fff}
.meta{display:flex;flex-direction:column;gap:.15rem;font-size:.9rem;color:#cfe7ea}
.meta strong{color:#fff;font-weight:600}
.window{max-width:980px;margin:-1.3rem auto 0;padding:0 1.25rem}
.window-card{background:var(--surface);border:1px solid var(--line);
  border-left:5px solid var(--go-edge);border-radius:12px;
  box-shadow:0 10px 30px -18px rgba(8,62,70,.45);padding:1.1rem 1.25rem}
.window-card .eyebrow{color:var(--go)}
.window-card .big{font-family:var(--fd);font-weight:600;font-size:1.18rem;
  margin:.1rem 0 .35rem;color:var(--ink)}
.window-card .det{font-size:.93rem;color:var(--muted)}
main{max-width:980px;margin:0 auto;padding:1.6rem 1.25rem 3rem}
h2{font-family:var(--fd);font-weight:600;font-size:1.28rem;color:var(--lake-2);
  margin:2.4rem 0 .8rem;padding-top:1.4rem;border-top:1px solid var(--line)}
h2:first-of-type{border-top:none;padding-top:0;margin-top:1rem}
h2::before{content:"";display:block;width:30px;height:3px;border-radius:2px;
  background:var(--lake);margin-bottom:.7rem}
p{margin:.7rem 0}
hr{border:none;border-top:1px solid var(--line);margin:2rem 0}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;margin:1rem 0}
table{border-collapse:collapse;width:100%;min-width:640px;font-size:.88rem}
th{background:#e9f1f2;color:var(--lake-2);text-align:left;font-family:var(--fd);
  font-weight:600;font-size:.72rem;letter-spacing:.04em;text-transform:uppercase;
  padding:.55rem .7rem;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:.55rem .7rem;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
td.go-cell{background:var(--go-bg)}
td.warn-cell{background:var(--warn-bg)}
td.no-cell{background:var(--no-bg);color:var(--no-ink)}
td:first-child{font-weight:600;white-space:nowrap}
.i-go{color:var(--go-edge)}.i-no{color:#aab6bc}.i-warn{color:var(--storm)}
footer{max-width:980px;margin:0 auto;padding:1.5rem 1.25rem 3rem;color:var(--muted);
  font-size:.82rem;border-top:1px solid var(--line)}
@media(max-width:640px){body{font-size:15px}.band .inner,main,.window{padding-left:1rem;padding-right:1rem}}
"""


def to_html(md_text, best, dates):
    import markdown
    from bs4 import BeautifulSoup

    header, rest = md_text.split("\n---\n", 1)
    hlines = [l for l in header.splitlines() if l.strip()]
    title = hlines[0].lstrip("# ").strip()

    def inline(s):
        return re.sub(r"^<p>|</p>$", "", markdown.markdown(s)).strip()
    meta_html = "\n".join(f'<div>{inline(l)}</div>' for l in hlines[1:])

    # blank line before any table that follows a non-table line
    out, prev = [], ""
    for line in rest.split("\n"):
        if line.lstrip().startswith("|") and prev.strip() and not prev.lstrip().startswith("|"):
            out.append("")
        out.append(line)
        prev = line
    body = markdown.markdown("\n".join(out), extensions=["tables", "sane_lists"])
    soup = BeautifulSoup(body, "html.parser")

    for table in soup.find_all("table"):
        w = soup.new_tag("div"); w["class"] = "table-wrap"
        table.insert_before(w); w.append(table.extract())
    for td in soup.find_all("td"):
        t = td.get_text()
        if "🟢" in t:
            td["class"] = td.get("class", []) + ["go-cell"]
        elif "⚠️" in t:
            td["class"] = td.get("class", []) + ["warn-cell"]
        elif "⚪" in t:
            td["class"] = td.get("class", []) + ["no-cell"]
    for a in soup.find_all("a"):
        if a.get("href", "").startswith("http"):
            a["target"] = "_blank"
            a["rel"] = "noopener"
    body = str(soup)
    for g, c in {"🟢": "i-go", "⚪": "i-no", "⚠️": "i-warn"}.items():
        body = body.replace(g, f'<span class="{c}">{g}</span>')

    if best:
        _, sp, s, i, note = best
        when = "Today" if i == 0 else dates[i].strftime("%A, %b %-d")
        card = (f'<div class="window"><div class="window-card">'
                f'<p class="eyebrow">Next window</p>'
                f'<div class="big">{when} — {sp["name"]}</div>'
                f'<div class="det">{note}</div></div></div>')
    else:
        card = (f'<div class="window"><div class="window-card" style="border-left-color:#aab6bc">'
                f'<p class="eyebrow" style="color:#7c8a93">Next window</p>'
                f'<div class="big">Nothing clearly sailable in the next {N_DAYS} days</div>'
                f'<div class="det">Light, flat, or wrong-direction across the spots.</div>'
                f'</div></div>')

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>
<div class="band"><div class="inner">
<p class="eyebrow">Lake Michigan &middot; Wing Foil</p>
<h1>{title}</h1><div class="meta">{meta_html}</div></div></div>
{card}
<main>{body}</main>
<footer>Auto-generated from NWS forecasts. Re-check a live buoy before you commit.</footer>
</body></html>"""


# --------------------------------------------------------------------------
# Demo data (no network) so you can preview the layout
# --------------------------------------------------------------------------
def demo_data():
    today = dt.datetime.now(CENTRAL).date()
    d = [today + dt.timedelta(days=i) for i in range(N_DAYS)]
    # light, flat day
    light = lambda day: dict(date=day, calm=False, dir="E", wmin=4, wmax=9,
                             spds=[4, 5, 6, 7, 8, 9, 8, 7, 6, 5, 5, 4],
                             gust=12, wave=1, storm=False)
    # the false-positive case: range 4-15, gust 24, but only one hour near 15
    spiky = lambda day: dict(date=day, calm=False, dir="NE", wmin=4, wmax=15,
                             spds=[4, 5, 6, 7, 8, 6, 9, 15, 11, 7, 5, 4],
                             gust=24, wave=2, storm=False)
    # solid, steady sailable day: 13-17 holds for hours
    solid = lambda day, inland: dict(date=day, calm=False, dir="NNE", wmin=13, wmax=17,
                                     spds=[12, 14, 15, 16, 17, 16, 15, 15, 14, 14, 13, 12],
                                     gust=25, wave=None if inland else 4, storm=False)
    out = {}
    for spot in SPOTS:
        out[spot["key"]] = [
            light(d[0]),
            spiky(d[1]),
            solid(d[2], spot["inland"]),
            light(d[3]),
        ]
    return out


def main():
    if DEMO:
        data = demo_data()
    else:
        data = {}
        for spot in SPOTS:
            try:
                data[spot["key"]] = fetch_spot(spot)
            except Exception as e:
                print(f"  ! {spot['key']} fetch failed: {e}", file=sys.stderr)
                today = dt.datetime.now(CENTRAL).date()
                data[spot["key"]] = [dict(date=today + dt.timedelta(days=i), calm=True)
                                     for i in range(N_DAYS)]

    md, best, dates = build_report(data)
    html = to_html(md, best, dates)
    open("report.md", "w", encoding="utf-8").write(md)
    open("index.html", "w", encoding="utf-8").write(html)
    print(f"Wrote report.md and index.html ({len(html)} bytes)"
          + ("  [demo]" if DEMO else ""))


if __name__ == "__main__":
    main()
