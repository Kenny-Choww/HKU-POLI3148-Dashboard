from __future__ import annotations

import argparse
import csv
import io
import json
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.request import Request, urlopen
import re
from plotly.offline.offline import get_plotlyjs


STUDENT_NAME = "Kenny CHOW"
COURSE_CODE = "POLI 3148"
LATEST_YEAR = 2024
BASE_YEAR = 2004
DIVERGENCE_THRESHOLD = 0.15

DEFAULT_SOURCE = "https://drive.google.com/file/d/1rM81vVULuFOmv_d81GUv_eKdt8eRyAs_/view?usp=drive_link"
FALLBACK_LOCAL_SOURCE = Path(__file__).with_name("V-Dem-CY-Full+Others-v15.csv")
DEFAULT_OUTPUT = Path(__file__).with_name("index.html")

REGIME_MAP = {
    0: "Closed autocracy",
    1: "Electoral autocracy",
    2: "Electoral democracy",
    3: "Liberal democracy",
}

REGION6_MAP = {
    1: "Eastern Europe & Central Asia",
    2: "Latin America & Caribbean",
    3: "Middle East & North Africa",
    4: "Sub-Saharan Africa",
    5: "Western Europe & North America",
    6: "Asia-Pacific",
}

INDICATOR_LABELS = {
    "v2x_polyarchy": "Electoral Democracy Index",
    "v2x_libdem": "Liberal Democracy Index",
    "v2x_freexp_altinf": "Freedom of Expression & Alternative Information",
    "v2x_rule": "Rule of Law Index",
}

USE_COLS = [
    "country_name",
    "country_text_id",
    "year",
    "project",
    "v2x_polyarchy",
    "v2x_libdem",
    "v2x_freexp_altinf",
    "v2x_rule",
    "v2x_regime",
    "e_regionpol_6C",
]


def format_signed(value: float) -> str:
    return f"{value:+.3f}"


def google_drive_file_id(source: str) -> str | None:
    parsed = urlparse(source)
    if parsed.netloc not in {"drive.google.com", "www.drive.google.com", "drive.usercontent.google.com"}:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "file" and parts[1] == "d":
        return parts[2]

    query_id = parse_qs(parsed.query).get("id")
    if query_id:
        return query_id[0]
    return None


def google_drive_candidate_urls(source: str) -> list[str]:
    file_id = google_drive_file_id(source)
    if not file_id:
        return [source]
    return [
        f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
        f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t",
        f"https://drive.google.com/uc?export=download&id={file_id}",
        source,
    ]


def is_probably_html(data: bytes, content_type: str, final_url: str) -> bool:
    if "text/html" in content_type.lower():
        return True
    final_path = urlparse(final_url).path.lower()
    if final_path.endswith((".html", ".htm", "/")):
        return True
    sample = data[:512].lstrip().lower()
    return sample.startswith(b"<!doctype html") or sample.startswith(b"<html")


def detect_remote_file_type(data: bytes, content_type: str, final_url: str, content_disposition: str) -> str | None:
    combined_meta = " ".join([content_type.lower(), final_url.lower(), content_disposition.lower()])
    if data[:4] == b"PK\x03\x04" or ".zip" in combined_meta or "application/zip" in combined_meta:
        return "zip"
    if ".csv" in combined_meta or "text/csv" in combined_meta or "application/csv" in combined_meta:
        return "csv"
    sample = data[:4096].decode("utf-8", errors="ignore")
    lines = sample.splitlines()
    first_line = lines[0] if lines else ""
    if "," in first_line and any(name in first_line for name in ("country_name", "year", "v2x_polyarchy")):
        return "csv"
    return None


def extract_google_drive_followup_url(html: str, current_url: str) -> str | None:
    patterns = [
        r'"downloadUrl":"([^"]+)"',
        r'href="([^"]*(?:uc\?export=download|drive\.usercontent\.google\.com/download)[^"]*)"',
        r'action="([^"]*(?:uc|download)[^"]*)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            candidate = (
                match.group(1)
                .replace("\\u003d", "=")
                .replace("\\u0026", "&")
                .replace("\\/", "/")
            )
            return urljoin(current_url, unquote(candidate))

    file_id = google_drive_file_id(current_url)
    confirm_match = re.search(r"confirm=([0-9A-Za-z_-]+)", html)
    if file_id and confirm_match:
        confirm = confirm_match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}&confirm={confirm}"

    if file_id and "download_warning" in html:
        return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"

    return None


def fetch_remote_dataset_bytes(source: str) -> tuple[bytes, str]:
    seen: set[str] = set()
    queue = list(google_drive_candidate_urls(source))
    last_error: Exception | None = None

    while queue:
        request_url = queue.pop(0)
        if request_url in seen:
            continue
        seen.add(request_url)

        try:
            print(f"Downloading CSV data from Google Drive (Lecture_4/Data_Raw/VDem): {request_url}")
            request = Request(request_url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request) as response:
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type") or ""
                content_disposition = response.headers.get("Content-Disposition") or ""
                data = response.read()

            print(f"Finished download attempt: {final_url}")
            print(f"Received {len(data):,} bytes")

        except Exception as exc:
            print(f"Google Drive download failed: {exc}")
            last_error = exc
            continue

        file_type = detect_remote_file_type(data, content_type, final_url, content_disposition)
        if file_type in {"csv", "zip"}:
            print(f"Loaded remote dataset successfully as {file_type.upper()}")
            return data, file_type

        if is_probably_html(data, content_type, final_url):
            print("Google Drive returned HTML instead of the CSV/ZIP file, trying follow-up link...")
            html = data.decode("utf-8", errors="ignore")
            followup_url = extract_google_drive_followup_url(html, final_url)
            if followup_url and followup_url not in seen:
                queue.insert(0, followup_url)
                continue
            last_error = ValueError("The online source returned an HTML page instead of a CSV/ZIP dataset.")
            continue

        print("Remote response was not recognized as CSV or ZIP.")
        last_error = ValueError("The online source did not look like a CSV or ZIP dataset.")

    if last_error is not None:
        raise last_error
    raise ValueError("Could not load the online dataset.")


@contextmanager
def open_csv_text(source: str | Path):
    source_str = str(source)
    parsed = urlparse(source_str)

    if parsed.scheme in {"http", "https"}:
        data, file_type = fetch_remote_dataset_bytes(source_str)
        if file_type == "zip":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                csv_files = [name for name in zf.namelist() if name.lower().endswith(".csv")]
                if not csv_files:
                    raise FileNotFoundError(f"No CSV file found inside online ZIP source: {source_str}")
                with zf.open(csv_files[0], "r") as raw:
                    wrapper = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                    try:
                        yield wrapper
                    finally:
                        wrapper.detach()
        else:
            text_stream = io.StringIO(data.decode("utf-8-sig"))
            yield text_stream
        return

    source_path = Path(source_str)
    if source_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(source_path) as zf:
            csv_files = [name for name in zf.namelist() if name.lower().endswith(".csv")]
            if not csv_files:
                raise FileNotFoundError(f"No CSV file found inside {source_path}")
            with zf.open(csv_files[0], "r") as raw:
                wrapper = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                try:
                    yield wrapper
                finally:
                    wrapper.detach()
    else:
        with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
            yield handle


def load_data(source: str | Path) -> list[dict]:
    data: list[dict] = []
    with open_csv_text(source) as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                if int(row["project"]) != 0:
                    continue
                year = int(row["year"])
                if year < 1970 or year > LATEST_YEAR:
                    continue
                iso3 = (row.get("country_text_id") or "").strip()
                if len(iso3) != 3:
                    continue
                region_code = int(float(row["e_regionpol_6C"]))
                region_label = REGION6_MAP.get(region_code)
                if region_label is None:
                    continue
                record = {
                    "country_name": row["country_name"].strip(),
                    "country_text_id": iso3,
                    "year": year,
                    "region6": region_label,
                    "v2x_polyarchy": round(float(row["v2x_polyarchy"]), 3),
                    "v2x_libdem": round(float(row["v2x_libdem"]), 3),
                    "v2x_freexp_altinf": round(float(row["v2x_freexp_altinf"]), 3),
                    "v2x_rule": round(float(row["v2x_rule"]), 3),
                    "v2x_regime": int(round(float(row["v2x_regime"]))),
                }
            except (TypeError, ValueError, KeyError):
                continue
            data.append(record)
    return data



def index_by_year(data: list[dict]) -> dict[int, list[dict]]:
    by_year: dict[int, list[dict]] = defaultdict(list)
    for row in data:
        by_year[row["year"]].append(row)
    return dict(by_year)



def get_country_record(data: list[dict], country_name: str, year: int) -> dict | None:
    for row in data:
        if row["country_name"] == country_name and row["year"] == year:
            return row
    return None



def summarize(data: list[dict]) -> dict:
    by_year = index_by_year(data)
    latest = by_year[LATEST_YEAR]
    base_map = {row["country_text_id"]: row for row in by_year[BASE_YEAR]}

    changes: list[dict] = []
    gaps: list[dict] = []
    regime_counts: dict[str, int] = defaultdict(int)

    region_change_accumulator: dict[str, list[float]] = defaultdict(list)

    for row in latest:
        regime_counts[str(row["v2x_regime"])] += 1
        gaps.append(
            {
                "country_name": row["country_name"],
                "country_text_id": row["country_text_id"],
                "gap": round(row["v2x_polyarchy"] - row["v2x_rule"], 3),
                "v2x_polyarchy": row["v2x_polyarchy"],
                "v2x_rule": row["v2x_rule"],
                "v2x_freexp_altinf": row["v2x_freexp_altinf"],
                "v2x_libdem": row["v2x_libdem"],
            }
        )
        base = base_map.get(row["country_text_id"])
        if not base:
            continue
        change_record = {
            "country_name": row["country_name"],
            "country_text_id": row["country_text_id"],
            "region6": row["region6"],
            "polyarchy_change": round(row["v2x_polyarchy"] - base["v2x_polyarchy"], 3),
            "libdem_change": round(row["v2x_libdem"] - base["v2x_libdem"], 3),
            "freexp_change": round(row["v2x_freexp_altinf"] - base["v2x_freexp_altinf"], 3),
            "rule_change": round(row["v2x_rule"] - base["v2x_rule"], 3),
            "polyarchy_2024": row["v2x_polyarchy"],
            "polyarchy_2004": base["v2x_polyarchy"],
            "rule_2024": row["v2x_rule"],
            "rule_2004": base["v2x_rule"],
            "freexp_2024": row["v2x_freexp_altinf"],
            "freexp_2004": base["v2x_freexp_altinf"],
            "libdem_2024": row["v2x_libdem"],
            "libdem_2004": base["v2x_libdem"],
        }
        changes.append(change_record)
        region_change_accumulator[row["region6"]].append(change_record["polyarchy_change"])

    top_gainers = sorted(changes, key=lambda x: x["polyarchy_change"], reverse=True)[:10]
    top_decliners = sorted(changes, key=lambda x: x["polyarchy_change"])[:10]
    gap_high = sorted(gaps, key=lambda x: x["gap"], reverse=True)[:10]
    gap_low = sorted(gaps, key=lambda x: x["gap"])[:10]

    regional_average_changes = [
        {
            "region": region,
            "avg_polyarchy_change": round(sum(values) / len(values), 3),
            "country_count": len(values),
        }
        for region, values in region_change_accumulator.items()
    ]
    regional_average_changes.sort(key=lambda x: x["avg_polyarchy_change"])

    threshold = DIVERGENCE_THRESHOLD
    polyarchy_above_rule = sum(1 for row in latest if row["v2x_polyarchy"] - row["v2x_rule"] > threshold)
    rule_above_polyarchy = sum(1 for row in latest if row["v2x_rule"] - row["v2x_polyarchy"] > threshold)

    broad_backslide_count = sum(
        1
        for row in top_decliners
        if row["libdem_change"] < 0 and row["freexp_change"] < 0 and row["rule_change"] < 0
    )
    broad_improve_count = sum(
        1
        for row in top_gainers
        if row["libdem_change"] > 0 and row["freexp_change"] > 0 and row["rule_change"] > 0
    )

    top_polyarchy = sorted(latest, key=lambda x: x["v2x_polyarchy"], reverse=True)[:10]
    bottom_polyarchy = sorted(latest, key=lambda x: x["v2x_polyarchy"])[:10]

    def region_series(indicator: str, start_year: int = 1970) -> list[dict]:
        series_rows: list[dict] = []
        years = sorted({row["year"] for row in data if row["year"] >= start_year})
        for year in years:
            grouped: dict[str, list[float]] = defaultdict(list)
            for row in by_year.get(year, []):
                grouped[row["region6"]].append(row[indicator])
            for region, values in grouped.items():
                series_rows.append(
                    {
                        "year": year,
                        "region": region,
                        "indicator": indicator,
                        "mean": round(sum(values) / len(values), 3),
                    }
                )
        return series_rows

    tunisia_2004 = get_country_record(data, "Tunisia", BASE_YEAR)
    tunisia_2014 = get_country_record(data, "Tunisia", 2014)
    tunisia_2024 = get_country_record(data, "Tunisia", LATEST_YEAR)
    bhutan_2004 = get_country_record(data, "Bhutan", BASE_YEAR)
    bhutan_2024 = get_country_record(data, "Bhutan", LATEST_YEAR)
    maldives_2004 = get_country_record(data, "Maldives", BASE_YEAR)
    maldives_2024 = get_country_record(data, "Maldives", LATEST_YEAR)
    nicaragua_change = next((row for row in changes if row["country_name"] == "Nicaragua"), None)
    hungary_change = next((row for row in changes if row["country_name"] == "Hungary"), None)

    summary = {
        "latestYear": LATEST_YEAR,
        "baseYear": BASE_YEAR,
        "countries2024": len({row["country_text_id"] for row in latest}),
        "regimeCounts": dict(regime_counts),
        "topGainers": top_gainers,
        "topDecliners": top_decliners,
        "gapHigh": gap_high,
        "gapLow": gap_low,
        "topPolyarchy2024": [
            {
                "country_name": row["country_name"],
                "v2x_polyarchy": row["v2x_polyarchy"],
                "v2x_libdem": row["v2x_libdem"],
            }
            for row in top_polyarchy
        ],
        "bottomPolyarchy2024": [
            {
                "country_name": row["country_name"],
                "v2x_polyarchy": row["v2x_polyarchy"],
                "v2x_libdem": row["v2x_libdem"],
            }
            for row in bottom_polyarchy
        ],
        "regionalAverageChanges": regional_average_changes,
        "divergence": {
            "threshold": threshold,
            "polyarchyAboveRuleCount": polyarchy_above_rule,
            "ruleAbovePolyarchyCount": rule_above_polyarchy,
        },
        "broadChangeCounts": {
            "topDeclinersBroadBackslide": broad_backslide_count,
            "topGainersBroadImprovement": broad_improve_count,
        },
        "regionalSeries": {
            indicator: region_series(indicator)
            for indicator in ["v2x_polyarchy", "v2x_libdem", "v2x_freexp_altinf", "v2x_rule"]
        },
        "caseHighlights": {
            "tunisia": {
                "change_2004_2024": round(tunisia_2024["v2x_polyarchy"] - tunisia_2004["v2x_polyarchy"], 3),
                "peak_2014": tunisia_2014["v2x_polyarchy"],
                "score_2024": tunisia_2024["v2x_polyarchy"],
            }
            if tunisia_2004 and tunisia_2014 and tunisia_2024
            else None,
            "bhutan": {
                "polyarchy_change": round(bhutan_2024["v2x_polyarchy"] - bhutan_2004["v2x_polyarchy"], 3),
                "rule_change": round(bhutan_2024["v2x_rule"] - bhutan_2004["v2x_rule"], 3),
                "rule_2004": bhutan_2004["v2x_rule"],
            }
            if bhutan_2004 and bhutan_2024
            else None,
            "maldives": {
                "polyarchy_change": round(maldives_2024["v2x_polyarchy"] - maldives_2004["v2x_polyarchy"], 3),
                "rule_change": round(maldives_2024["v2x_rule"] - maldives_2004["v2x_rule"], 3),
                "freexp_change": round(
                    maldives_2024["v2x_freexp_altinf"] - maldives_2004["v2x_freexp_altinf"], 3
                ),
            }
            if maldives_2004 and maldives_2024
            else None,
            "nicaragua": nicaragua_change,
            "hungary": hungary_change,
        },
    }
    return summary



def build_insight_cards(summary: dict) -> str:
    regions = summary["regionalAverageChanges"]
    worst_region = regions[0]
    best_region = regions[-1]
    divergence = summary["divergence"]
    cases = summary["caseHighlights"]
    broad = summary["broadChangeCounts"]

    cards = [
        {
            "title": "1) Democratic change is geographically uneven",
            "body": (
                f"At the regional level, the mean 2004–2024 change in electoral democracy is most negative in "
                f"<strong>{escape(worst_region['region'])}</strong> ({format_signed(worst_region['avg_polyarchy_change'])}), "
                f"while <strong>{escape(best_region['region'])}</strong> records the strongest average result "
                f"({format_signed(best_region['avg_polyarchy_change'])}). The dashboard therefore supports a "
                f"politics story about uneven geography, not one synchronized global wave."
            ),
        },
        {
            "title": "2) Democratic improvements do not all look the same",
            "body": (
                f"<strong>Bhutan</strong> posts the largest gain in electoral democracy "
                f"({format_signed(cases['bhutan']['polyarchy_change'])}), but its rule-of-law score moves only "
                f"{format_signed(cases['bhutan']['rule_change'])} because it already starts high in 2004 "
                f"({cases['bhutan']['rule_2004']:.3f}). By contrast, <strong>Maldives</strong> combines electoral gains "
                f"({format_signed(cases['maldives']['polyarchy_change'])}) with large improvements in rule of law "
                f"({format_signed(cases['maldives']['rule_change'])}) and freedom of expression "
                f"({format_signed(cases['maldives']['freexp_change'])}). That means democratization can come from "
                f"different institutional starting points."
            ),
        },
        {
            "title": "3) Backsliding is usually broad, not confined to elections",
            "body": (
                f"Among the ten biggest decliners in electoral democracy, <strong>{broad['topDeclinersBroadBackslide']}</strong> also lose "
                f"simultaneously on liberal democracy, freedom of expression, and rule of law. <strong>Nicaragua</strong> is the sharpest case: "
                f"its freedom-of-expression score falls {format_signed(cases['nicaragua']['freexp_change'])}, even steeper than its electoral "
                f"democracy decline ({format_signed(cases['nicaragua']['polyarchy_change'])}). <strong>Hungary</strong> shows a slower but broader "
                f"erosion across electoral democracy ({format_signed(cases['hungary']['polyarchy_change'])}), liberal checks "
                f"({format_signed(cases['hungary']['libdem_change'])}), and expression ({format_signed(cases['hungary']['freexp_change'])})."
            ),
        },
        {
            "title": "4) A 20-year bar chart can hide recent reversals",
            "body": (
                f"<strong>Tunisia</strong> is still a net gainer relative to 2004 "
                f"({format_signed(cases['tunisia']['change_2004_2024'])}), but its 2024 electoral-democracy score "
                f"({cases['tunisia']['score_2024']:.3f}) is far below its 2014 peak ({cases['tunisia']['peak_2014']:.3f}). "
                f"That is why the gains-and-declines bar should be read together with the country trajectory chart: "
                f"long-run improvement and recent backsliding can both be true in the same case."
            ),
        },
    ]

    return "\n".join(
        f'''<div class="insight"><h3>{card['title']}</h3><p>{card['body']}</p></div>''' for card in cards
    )



def build_html(data: list[dict], summary: dict) -> str:
    plotly_js = get_plotlyjs()
    countries_2024 = summary["countries2024"]
    regime_counts = summary["regimeCounts"]
    liberal_democracies = regime_counts.get("3", 0)
    electoral_democracies = regime_counts.get("2", 0)
    autocracies = regime_counts.get("0", 0) + regime_counts.get("1", 0)
    divergence = summary["divergence"]
    insight_cards_html = build_insight_cards(summary)

    replacements = {
        "__PLOTLY_JS__": plotly_js,
        "__STUDENT_NAME__": escape(STUDENT_NAME),
        "__COURSE_CODE__": escape(COURSE_CODE),
        "__COUNTRIES_2024__": str(countries_2024),
        "__LIBERAL_DEMOCRACIES__": str(liberal_democracies),
        "__ELECTORAL_DEMOCRACIES__": str(electoral_democracies),
        "__AUTOCRACIES__": str(autocracies),
        "__DIVERGENCE_THRESHOLD__": f"{divergence['threshold']:.2f}",
        "__POLYARCHY_ABOVE_RULE__": str(divergence["polyarchyAboveRuleCount"]),
        "__RULE_ABOVE_POLYARCHY__": str(divergence["ruleAbovePolyarchyCount"]),
        "__DASHBOARD_DATA__": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        "__SUMMARY__": json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
        "__INSIGHT_CARDS__": insight_cards_html,
    }

    template = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Democratic Quality and Regime Change: V-Dem Dashboard</title>
<script>__PLOTLY_JS__</script>
<style>
  :root {
    --bg: #f6f8fb;
    --card: #ffffff;
    --text: #1f2937;
    --muted: #6b7280;
    --accent: #1d4ed8;
    --border: #dbe2ea;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: Arial, Helvetica, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.55;
  }
  .container {
    max-width: 1280px;
    margin: 0 auto;
    padding: 18px 18px 40px;
  }
  .hero {
    background: linear-gradient(135deg, #eff6ff, #ffffff);
    border: 1px solid var(--border);
    border-radius: 18px;
    padding: 18px 20px;
    margin-bottom: 16px;
  }
  .hero-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.4fr) minmax(320px, 1fr);
    gap: 16px;
    align-items: start;
  }
  .eyebrow {
    margin: 0 0 6px;
    font-size: 0.82rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--accent);
  }
  h1 { margin: 0 0 8px; font-size: 1.75rem; line-height: 1.15; }
  h2 { margin: 0 0 10px; font-size: 1.25rem; }
  h3 { margin: 0 0 10px; font-size: 1.03rem; }
  p { margin: 6px 0; }
  .lead {
    margin: 0;
    max-width: 70ch;
    color: #334155;
    font-size: 0.98rem;
  }
  .meta { color: var(--muted); font-size: 0.92rem; }
  .compact-meta { margin-top: 8px; }
  .grid {
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 16px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 16px;
    box-shadow: 0 4px 14px rgba(15, 23, 42, 0.04);
  }
  .span-12 { grid-column: span 12; }
  .span-6 { grid-column: span 6; }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: end;
    margin: 10px 0 8px;
  }
  .control { min-width: 170px; }
  .control label {
    display: block;
    font-size: 0.88rem;
    color: var(--muted);
    margin-bottom: 5px;
  }
  select, input[type="range"], input[type="text"] { width: 100%; }
  select, input[type="text"] {
    padding: 8px 10px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: #fff;
  }
  button {
    padding: 9px 14px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: #fff;
    color: var(--text);
    cursor: pointer;
  }
  button.primary {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent);
  }
  .plot { width: 100%; min-height: 360px; }
  .small-plot { width: 100%; min-height: 315px; }
  #scatterChart.small-plot { min-height: 420px; }
  #changeChart.small-plot { min-height: 420px; }
  #trajectoryChart.plot { min-height: 390px; }
  .insight-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 14px;
  }
  .insight {
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 14px;
    background: #fbfdff;
  }
  .kpis {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
  }
  .kpi {
    background: #fff;
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 11px 12px;
  }
  .kpi-value { font-size: 1.45rem; font-weight: 700; line-height: 1.1; }
  .kpi-label { color: var(--muted); font-size: 0.88rem; }
  .dropdown { min-width: 320px; flex: 1 1 420px; position: relative; }
  .dropdown details { position: relative; }
  .dropdown summary {
    list-style: none;
    padding: 8px 10px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: #fff;
    cursor: pointer;
    user-select: none;
  }
  .dropdown summary::-webkit-details-marker { display: none; }
  .dropdown-panel {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    right: 0;
    z-index: 50;
    padding: 12px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: #fff;
    box-shadow: 0 10px 24px rgba(15, 23, 42, 0.08);
  }
  .option-list {
    max-height: 260px;
    overflow-y: auto;
    margin-top: 10px;
    padding: 8px;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: #fbfdff;
  }
  .option-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 2px;
    font-size: 0.95rem;
  }
  .option-item input { width: auto; }
  .dropdown-hint { margin-top: 8px; font-size: 0.85rem; color: var(--muted); }
  .section-note { margin-top: 6px; }
  .research-details summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    cursor: pointer;
    list-style: none;
  }
  .research-details summary::-webkit-details-marker { display: none; }
  .summary-label {
    flex: 0 0 auto;
    color: var(--muted);
    font-size: 0.88rem;
  }
  .research-body {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
  }
  ul.clean { margin: 8px 0 0 18px; }
  footer { margin-top: 20px; color: var(--muted); font-size: 0.92rem; }
  a { color: var(--accent); }
  @media (max-width: 1024px) {
    .hero-grid { grid-template-columns: 1fr; }
  }
  @media (max-width: 900px) {
    .span-6 { grid-column: span 12; }
    .kpis { grid-template-columns: repeat(2, 1fr); }
  }
  @media (max-width: 640px) {
    .container { padding: 14px 14px 32px; }
    .hero { padding: 16px; }
    h1 { font-size: 1.5rem; }
    .plot { min-height: 320px; }
    .small-plot { min-height: 300px; }
    .kpis { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="container">
  <section class="hero">
    <div class="hero-grid">
      <div class="hero-copy">
        <p class="eyebrow">POLI 3148 dashboard</p>
        <h1>Democratic Quality and Regime Change, 1970–2024</h1>
        <p class="lead"><strong>Main Question:</strong> How has democratic quality changed across countries since 1970, and where do electoral competition, civil liberties, and institutional constraints move together—or diverge?</p>
        <p class="meta compact-meta"><strong>Name:</strong> __STUDENT_NAME__ &nbsp;|&nbsp; <strong>Course:</strong> __COURSE_CODE__ &nbsp;|&nbsp; <strong>Dataset:</strong> V-Dem v15 country-year data</p>
      </div>
      <div class="hero-metrics">
        <div class="kpis">
          <div class="kpi"><div class="kpi-value">__COUNTRIES_2024__</div><div class="kpi-label">Countries in 2024</div></div>
          <div class="kpi"><div class="kpi-value">__LIBERAL_DEMOCRACIES__</div><div class="kpi-label">Liberal democracies (2024)</div></div>
          <div class="kpi"><div class="kpi-value">__ELECTORAL_DEMOCRACIES__</div><div class="kpi-label">Electoral democracies (2024)</div></div>
          <div class="kpi"><div class="kpi-value">__AUTOCRACIES__</div><div class="kpi-label">Autocracies (2024)</div></div>
        </div>
      </div>
    </div>
  </section>

  <div class="grid">
    <section class="card span-12">
      <h2>Global comparison</h2>
      <div class="controls">
        <div class="control" style="min-width:250px; flex:1 1 260px;">
          <label for="yearSlider">Year: <span id="yearLabel"></span></label>
          <input id="yearSlider" type="range" min="1970" max="2024" step="1" value="2024" />
        </div>
        <div class="control">
          <label for="mapIndicator">Map indicator</label>
          <select id="mapIndicator">
            <option value="v2x_polyarchy">Electoral Democracy Index</option>
            <option value="v2x_libdem">Liberal Democracy Index</option>
            <option value="v2x_freexp_altinf">Freedom of Expression</option>
            <option value="v2x_rule">Rule of Law</option>
          </select>
        </div>
        <div class="control">
          <label>&nbsp;</label>
          <button id="playButton" class="primary" type="button">Play timeline</button>
        </div>
        <div class="control">
          <label>&nbsp;</label>
          <button id="resetButton" type="button">Reset to 2024</button>
        </div>
      </div>
      <div id="mapChart" class="plot"></div>
    </section>

    <section class="card span-6">
      <h2>Rule of law vs. freedom of expression</h2>
      <p>Each point is a country in the selected year. Regime type is from V-Dem, and bubble size shows the Electoral Democracy Index. In 2024, using a __DIVERGENCE_THRESHOLD__-point threshold, <strong>__POLYARCHY_ABOVE_RULE__</strong> countries score higher on electoral democracy than rule of law, while <strong>__RULE_ABOVE_POLYARCHY__</strong> show the reverse.</p>
      <div id="scatterChart" class="small-plot"></div>
    </section>

    <section class="card span-6">
      <h2>Largest democratic gains and declines since 2004</h2>
      <p>This plot highlights the biggest changes in <strong>Electoral Democracy Index</strong> between 2004 and 2024. The 2004 baseline gives a full 20-year window, but it should not be read as a claim of uninterrupted linear change: some countries improve over the whole period while still backsliding recently.</p>
      <div id="changeChart" class="small-plot"></div>
    </section>

    <section class="card span-12">
      <h2>Trajectory explorer</h2>
      <div class="controls">
        <div class="control">
          <label for="trajectoryIndicator">Indicator</label>
          <select id="trajectoryIndicator">
            <option value="v2x_polyarchy">Electoral Democracy Index</option>
            <option value="v2x_libdem">Liberal Democracy Index</option>
            <option value="v2x_freexp_altinf">Freedom of Expression</option>
            <option value="v2x_rule">Rule of Law</option>
          </select>
        </div>
        <div class="control">
          <label for="trajectoryLevel">Group by</label>
          <select id="trajectoryLevel">
            <option value="country">Country level</option>
            <option value="region">Regional average</option>
          </select>
        </div>
        <div class="dropdown">
          <label for="seriesDropdown">Select series</label>
          <details id="seriesDropdown">
            <summary><span id="seriesDropdownLabel">Choose countries</span></summary>
            <div class="dropdown-panel">
              <input id="seriesSearch" type="text" placeholder="Type to filter the list" />
              <div id="seriesOptions" class="option-list"></div>
              <div class="dropdown-hint">You can compare several lines at once. Switch <strong>Group by</strong> to compare regional averages instead of individual countries.</div>
            </div>
          </details>
        </div>
      </div>
      <div id="trajectoryChart" class="plot"></div>
      <p class="meta" id="trajectoryNote">Country mode uses raw country-year data from 1970–2024. Regional mode uses processed, unweighted means for each V-Dem six-region category over the same period.</p>
    </section>

    <section class="card span-12">
      <h2>Analytical insights</h2>
      <div class="insight-grid">
        __INSIGHT_CARDS__
      </div>
    </section>

    <section class="card span-12">
      <details class="research-details">
        <summary>
          <h2>Variable guide</h2>
          <span class="summary-label">Show / hide</span>
        </summary>
        <div class="research-body">
          <p>This dataset is the subset of filter: project = 0 <a href="https://www.v-dem.net/documents/6/vparty_codebook_v2.pdf" target="_blank">(meaning of the contemporary V-Dem project)</a>, and years from 1970–2024.</p>
          <p>The main indicators used in the dashboard are:</p>
          <ul class="clean">
            <li><strong>v2x_polyarchy</strong> is the headline measure of electoral democracy.</li>
            <li><strong>v2x_libdem</strong> captures whether democracy is backed by liberal checks and protections.</li>
            <li><strong>v2x_freexp_altinf</strong> reflects the information environment and freedom of expression.</li>
            <li><strong>v2x_rule</strong> shows whether the rule of law keeps power constrained and predictable.</li>
          </ul>
        </div>
      </details>
    </section>
  </div>

  <footer>
    <p><strong>Data sources:</strong> V-Dem Institute, <em>V-Dem Dataset v15</em>. Main data page: <a href="https://www.v-dem.net/data/the-v-dem-dataset/">https://www.v-dem.net/data/the-v-dem-dataset/</a>. File used here: <code>V-Dem-CY-Full+Others-v15.csv</code>.</p>
    <p><strong>Method note:</strong> the dashboard uses contemporary country-year observations only (<code>project = 0</code>) and keeps years 1970–2024. When the trajectory explorer is set to regional mode, it uses unweighted country means within V-Dem's six-region classification, so it summarizes regional direction rather than population-weighted democratic experience.</p>
    <p><strong>How to read the dashboard:</strong> start with the map for global distribution, use the scatter for dimensional divergence in one year, use the gains-and-declines bar for endpoints, and then use the trajectory explorer to test whether the pattern is concentrated in one country or reflects a broader regional drift.</p>
  </footer>
</div>

<script>
const dashboardData = __DASHBOARD_DATA__;
const summary = __SUMMARY__;
const regimeMap = {0:'Closed autocracy',1:'Electoral autocracy',2:'Electoral democracy',3:'Liberal democracy'};
const indicatorLabels = {
  'v2x_polyarchy':'Electoral Democracy Index',
  'v2x_libdem':'Liberal Democracy Index',
  'v2x_freexp_altinf':'Freedom of Expression & Alternative Information',
  'v2x_rule':'Rule of Law Index'
};

const years = [...new Set(dashboardData.map(d => d.year))].sort((a, b) => a - b);
const countries = [...new Set(dashboardData.map(d => d.country_name))].sort((a, b) => a.localeCompare(b));
const regions = [...new Set(dashboardData.map(d => d.region6))].sort((a, b) => a.localeCompare(b));
const byYear = new Map();
const byCountry = new Map();
for (const d of dashboardData) {
  d.regime_label = regimeMap[Math.round(d.v2x_regime)] || 'Unknown';
  if (!byYear.has(d.year)) byYear.set(d.year, []);
  byYear.get(d.year).push(d);
  if (!byCountry.has(d.country_name)) byCountry.set(d.country_name, []);
  byCountry.get(d.country_name).push(d);
}
for (const arr of byCountry.values()) arr.sort((a, b) => a.year - b.year);

const yearSlider = document.getElementById('yearSlider');
const yearLabel = document.getElementById('yearLabel');
const mapIndicator = document.getElementById('mapIndicator');
const trajectoryIndicator = document.getElementById('trajectoryIndicator');
const trajectoryLevel = document.getElementById('trajectoryLevel');
const seriesDropdown = document.getElementById('seriesDropdown');
const seriesDropdownLabel = document.getElementById('seriesDropdownLabel');
const seriesSearch = document.getElementById('seriesSearch');
const seriesOptions = document.getElementById('seriesOptions');
const trajectoryNote = document.getElementById('trajectoryNote');
const playButton = document.getElementById('playButton');
const resetButton = document.getElementById('resetButton');

const defaultCountries = ['Denmark', 'Mexico', 'India', 'Hungary', 'Nicaragua', 'Nepal', 'Tunisia', 'China'];
const defaultRegions = [...regions];
const selectedSeries = {
  country: new Set(defaultCountries.filter(country => countries.includes(country))),
  region: new Set(defaultRegions)
};
let autoplayHandle = null;

function currentYear() {
  return Number(yearSlider.value);
}

function activeLevel() {
  return trajectoryLevel.value;
}

function currentSeriesNames() {
  return activeLevel() === 'country' ? countries : regions;
}

function defaultSelection(level) {
  return level === 'country' ? [...selectedSeries.country].length ? [...selectedSeries.country] : defaultCountries : defaultRegions;
}

function selectedNames(level = activeLevel()) {
  const chosen = [...selectedSeries[level]];
  return chosen.length ? chosen : defaultSelection(level);
}

function updateSeriesLabel() {
  const level = activeLevel();
  const chosen = selectedNames(level);
  if (!chosen.length) {
    seriesDropdownLabel.textContent = level === 'country' ? 'Choose countries' : 'Choose regions';
    return;
  }
  if (level === 'region' && chosen.length === regions.length) {
    seriesDropdownLabel.textContent = 'All ' + regions.length + ' regions selected';
    return;
  }
  if (chosen.length <= 2) {
    seriesDropdownLabel.textContent = chosen.join(', ');
    return;
  }
  seriesDropdownLabel.textContent = chosen.length + ' ' + (level === 'country' ? 'countries' : 'regions') + ' selected';
}

function renderSeriesOptions(filterText = '') {
  const level = activeLevel();
  const query = filterText.trim().toLowerCase();
  const names = currentSeriesNames();
  seriesOptions.innerHTML = '';
  for (const name of names) {
    if (query && !name.toLowerCase().includes(query)) continue;
    const label = document.createElement('label');
    label.className = 'option-item';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.value = name;
    checkbox.checked = selectedSeries[level].has(name);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) {
        selectedSeries[level].add(name);
      } else {
        selectedSeries[level].delete(name);
      }
      updateSeriesLabel();
      drawTrajectory();
    });
    const span = document.createElement('span');
    span.textContent = name;
    label.appendChild(checkbox);
    label.appendChild(span);
    seriesOptions.appendChild(label);
  }
  updateSeriesLabel();
}

renderSeriesOptions();

function stopPlayback() {
  if (autoplayHandle) {
    clearInterval(autoplayHandle);
    autoplayHandle = null;
  }
  playButton.textContent = 'Play timeline';
}

function startPlayback() {
  if (autoplayHandle) return;
  if (Number(yearSlider.value) >= years[years.length - 1]) {
    yearSlider.value = String(years[0]);
    drawMap();
    drawScatter();
  }
  playButton.textContent = 'Pause timeline';
  autoplayHandle = setInterval(() => {
    const nextYear = Number(yearSlider.value) + 1;
    if (nextYear > years[years.length - 1]) {
      stopPlayback();
      return;
    }
    yearSlider.value = String(nextYear);
    drawMap();
    drawScatter();
  }, 850);
}

function drawMap() {
  const y = currentYear();
  yearLabel.textContent = y;
  const indicator = mapIndicator.value;
  const rows = byYear.get(y) || [];

  Plotly.react('mapChart', [{
    type: 'choropleth',
    locationmode: 'ISO-3',
    locations: rows.map(d => d.country_text_id),
    z: rows.map(d => d[indicator]),
    text: rows.map(d => d.country_name),
    customdata: rows.map(d => [
      d.v2x_polyarchy,
      d.v2x_libdem,
      d.v2x_freexp_altinf,
      d.v2x_rule,
      d.regime_label
    ]),
    colorscale: [
      [0.0, '#edf8e9'],
      [0.2, '#c7e9c0'],
      [0.4, '#a1d99b'],
      [0.6, '#74c476'],
      [0.8, '#31a354'],
      [1.0, '#006d2c']
    ],
    zmin: 0,
    zmax: 1,
    colorbar: {
      tickformat: '.1f',
      len: 0.82,
      thickness: 16
    },
    hovertemplate:
      '<b>%{text}</b><br>' +
      indicatorLabels[indicator] + ': %{z:.3f}<br>' +
      'Electoral democracy: %{customdata[0]:.3f}<br>' +
      'Liberal democracy: %{customdata[1]:.3f}<br>' +
      'Freedom of expression: %{customdata[2]:.3f}<br>' +
      'Rule of law: %{customdata[3]:.3f}<br>' +
      'Regime: %{customdata[4]}<extra></extra>'
  }], {
    title: {text: indicatorLabels[indicator] + ' in ' + y},
    height: 460,
    margin: { l: 0, r: 10, t: 46, b: 0 },
    geo: {
      domain: { x: [0, 0.93], y: [0, 1] },
      projection: { type: 'natural earth', scale: 1.18 },
      showframe: false,
      showcoastlines: false,
      showocean: true,
      oceancolor: '#dbeafe',
      bgcolor: '#ffffff',
      lataxis: { range: [-58, 85] }
    }
  }, {
    responsive: true,
    displayModeBar: false
  });
}

function drawScatter() {
  const y = currentYear();
  const rows = byYear.get(y) || [];
  const regimes = ['Closed autocracy', 'Electoral autocracy', 'Electoral democracy', 'Liberal democracy'];
  const traces = regimes.map(reg => {
    const subset = rows.filter(d => d.regime_label === reg);
    return {
      type: 'scatter',
      mode: 'markers',
      name: reg,
      x: subset.map(d => d.v2x_rule),
      y: subset.map(d => d.v2x_freexp_altinf),
      text: subset.map(d => d.country_name),
      customdata: subset.map(d => [d.v2x_polyarchy, d.v2x_libdem]),
      marker: {size: subset.map(d => 8 + d.v2x_polyarchy * 18), opacity: 0.75},
      hovertemplate:
        '<b>%{text}</b><br>' +
        'Rule of law: %{x:.3f}<br>' +
        'Freedom of expression: %{y:.3f}<br>' +
        'Electoral democracy: %{customdata[0]:.3f}<br>' +
        'Liberal democracy: %{customdata[1]:.3f}<extra></extra>'
    };
  });
  Plotly.react('scatterChart', traces, {
    height: 420,
    xaxis: {
      title: {text: 'Rule of Law Index'},
      range: [0, 1],
      automargin: true,
      title_standoff: 14
    },
    yaxis: {
      title: {text: 'Freedom of Expression'},
      range: [0, 1],
      automargin: true,
      title_standoff: 14
    },
    margin: {l: 92, r: 16, t: 10, b: 120},
    legend: {orientation: 'h', x: 0, y: -0.3, xanchor: 'left', yanchor: 'top'}
  }, {responsive: true, displayModeBar: false});
}

function drawTrajectory() {
  const indicator = trajectoryIndicator.value;
  const level = activeLevel();
  const selected = selectedNames(level);
  let traces = [];
  let title = '';

  if (level === 'country') {
    const countriesToShow = selected.length ? selected : defaultCountries;
    traces = countriesToShow.map(country => {
      const rows = byCountry.get(country) || [];
      return {
        type: 'scatter',
        mode: 'lines',
        name: country,
        x: rows.map(d => d.year),
        y: rows.map(d => d[indicator]),
        hovertemplate: '<b>' + country + '</b><br>Year: %{x}<br>' + indicatorLabels[indicator] + ': %{y:.3f}<extra></extra>'
      };
    });
    title = indicatorLabels[indicator] + ' over time by country';
    trajectoryNote.textContent = 'Country mode uses raw country-year data from 1970–2024. This is best for checking whether a large endpoint change happened steadily, abruptly, or reversed later.';
  } else {
    const series = summary.regionalSeries[indicator] || [];
    const regionsToShow = selected.length ? selected : regions;
    traces = regionsToShow.map(region => {
      const rows = series.filter(d => d.region === region).sort((a, b) => a.year - b.year);
      return {
        type: 'scatter',
        mode: 'lines',
        name: region,
        x: rows.map(d => d.year),
        y: rows.map(d => d.mean),
        hovertemplate: '<b>' + region + '</b><br>Year: %{x}<br>Regional mean: %{y:.3f}<extra></extra>'
      };
    });
    title = 'Regional mean ' + indicatorLabels[indicator] + ' over time';
    trajectoryNote.textContent = 'Regional mode uses processed, unweighted means for the V-Dem six-region categories across 1970–2024. This is useful for spotting broad drift rather than standout country cases.';
  }

  Plotly.react('trajectoryChart', traces, {
    title: {text: title},
    xaxis: {title: {text: 'Years'}, automargin: true, title_standoff: 10},
    yaxis: {title: {text: indicatorLabels[indicator]}, range: [0, 1], automargin: true, title_standoff: 10},
    margin: {l: 72, r: 10, t: 46, b: 68},
    legend: {orientation: 'h', y: -0.25}
  }, {responsive: true, displayModeBar: false});
}

function drawChangeBar() {
  const combined = [...summary.topDecliners, ...summary.topGainers].sort((a, b) => a.polyarchy_change - b.polyarchy_change);
  Plotly.react('changeChart', [{
    type: 'bar',
    orientation: 'h',
    y: combined.map(d => d.country_name),
    x: combined.map(d => d.polyarchy_change),
    marker: {color: combined.map(d => d.polyarchy_change >= 0 ? '#2563eb' : '#dc2626')},
    hovertemplate:
      '<b>%{y}</b><br>' +
      'Electoral democracy change: %{x:.3f}<br>' +
      'Liberal democracy change: %{customdata[0]:.3f}<br>' +
      'Freedom of expression change: %{customdata[1]:.3f}<br>' +
      'Rule of law change: %{customdata[2]:.3f}<extra></extra>',
    customdata: combined.map(d => [d.libdem_change, d.freexp_change, d.rule_change])
  }], {
    height: 420,
    title: {text: 'Biggest changes in Electoral Democracy Index, 2004–2024'},
    xaxis: {title: {text: 'Change in Electoral Democracy Index'}, automargin: true, title_standoff: 10},
    yaxis: {title: {text: 'Country'}, automargin: true, title_standoff: 10},
    margin: {l: 120, r: 10, t: 46, b: 60}
  }, {responsive: true, displayModeBar: false});
}


yearSlider.addEventListener('input', () => {
  stopPlayback();
  drawMap();
  drawScatter();
});
mapIndicator.addEventListener('change', drawMap);
trajectoryIndicator.addEventListener('change', drawTrajectory);
trajectoryLevel.addEventListener('change', () => {
  seriesSearch.value = '';
  renderSeriesOptions();
  drawTrajectory();
});
seriesSearch.addEventListener('input', () => {
  renderSeriesOptions(seriesSearch.value);
});
seriesDropdown.addEventListener('click', event => {
  event.stopPropagation();
});
document.addEventListener('click', event => {
  if (!seriesDropdown.contains(event.target)) {
    seriesDropdown.removeAttribute('open');
  }
});
playButton.addEventListener('click', () => {
  if (autoplayHandle) {
    stopPlayback();
  } else {
    startPlayback();
  }
});
resetButton.addEventListener('click', () => {
  stopPlayback();
  yearSlider.value = String(summary.latestYear);
  drawMap();
  drawScatter();
});

drawMap();
drawScatter();
drawChangeBar();
drawTrajectory();
</script>
</body>
</html>'''

    for key, value in replacements.items():
        template = template.replace(key, value)
    return template



def resolve_data_source(user_source: str | None) -> str | Path:
    if user_source:
        return user_source
    return DEFAULT_SOURCE


def load_data_with_fallback(user_source: str | None) -> tuple[list[dict], str]:
    primary_source = resolve_data_source(user_source)
    fallback_source = FALLBACK_LOCAL_SOURCE

    try:
        return load_data(primary_source), f"Loaded data from {primary_source}"
    except Exception as primary_exc:
        if user_source:
            raise
        if not fallback_source.exists():
            raise RuntimeError(
                f"Could not load the online source ({primary_source}) and the fallback local file "
                f"was not found at {fallback_source}."
            ) from primary_exc
        return load_data(fallback_source), f"Loaded data from local fallback {fallback_source} after online source failed: {primary_exc}"



def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a self-contained V-Dem dashboard HTML file.")
    parser.add_argument("source", nargs="?", default=None, help="Optional local CSV/ZIP path or direct URL to the V-Dem dataset")
    parser.add_argument("output", nargs="?", default=str(DEFAULT_OUTPUT), help="Path to the output HTML file")
    args = parser.parse_args()

    output_path = Path(args.output)

    data, source_message = load_data_with_fallback(args.source)
    summary = summarize(data)
    html = build_html(data, summary)
    output_path.write_text(html, encoding="utf-8")
    print(source_message)
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
