#!/usr/bin/env python3
"""
Hormuz Crisis AIS Tracker — Browser-based data collection.
Uses a local HTTP bridge: browser fetches from MarineTraffic (bypasses Cloudflare),
serves results on localhost for the tracker script.

Run: python3 hormuz-tracker.py [--watch] [--report] [--csv]
Requires: openclaw browser with active MarineTraffic session
"""

import json, os, sys, time, datetime, subprocess, argparse
from pathlib import Path

STATE_DIR = os.path.dirname(os.path.abspath(__file__))
METRICS_FILE = os.path.join(STATE_DIR, "hormuz-metrics.jsonl")
TILES_Z8 = [
    (8, 84, 54), (8, 85, 54), (8, 86, 54),
    (8, 84, 55), (8, 85, 55), (8, 86, 55),
    (8, 83, 54), (8, 83, 55),
    (8, 87, 54), (8, 87, 55),
]

# --- Zone definitions ---
def classify_zone(lat, lon):
    if 25.5 <= lat <= 26.7 and 55.8 <= lon <= 57.2:
        return "strait"
    if 24.8 <= lat <= 25.6 and 56.0 <= lon <= 56.9:
        return "fujairah"
    if lat >= 24.0 and lon < 55.8:
        return "inside_gulf"
    if 23.0 <= lat < 25.5 and 56.5 <= lon <= 60.5:
        return "gulf_oman"
    if lat < 23.0 and 57.0 <= lon <= 65.0:
        return "arabian_sea"
    return "other"


def fetch_all_tiles_via_browser():
    """Use openclaw browser tool to fetch all tiles from MarineTraffic."""
    # Build a JS snippet that fetches all tiles and stores results
    tile_urls = [f"https://www.marinetraffic.com/getData/get_data_json_4/z:{z}/X:{x}/Y:{y}/station:0"
                 for z, x, y in TILES_Z8]

    # We'll fetch them sequentially in the browser and write results to a temp file
    all_vessels = {}
    for i, (z, x, y) in enumerate(TILES_Z8):
        url = f"https://www.marinetraffic.com/getData/get_data_json_4/z:{z}/X:{x}/Y:{y}/station:0"
        # Use openclaw CLI to evaluate in browser
        js = f"fetch('{url}', {{headers: {{'X-Requested-With': 'XMLHttpRequest'}}}}).then(r => r.json()).then(d => JSON.stringify(d))"

        result = subprocess.run(
            ["python3", os.path.join(STATE_DIR, "browser-fetch.py"), url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                data = json.loads(result.stdout.strip())
                if "data" in data and "rows" in data["data"]:
                    for v in data["data"]["rows"]:
                        ship_id = v.get("SHIP_ID")
                        if ship_id:
                            all_vessels[ship_id] = v
            except json.JSONDecodeError:
                print(f"  [WARN] Tile z:{z}/X:{x}/Y:{y} bad JSON", file=sys.stderr)
        else:
            print(f"  [WARN] Tile z:{z}/X:{x}/Y:{y} failed", file=sys.stderr)
        time.sleep(0.5)

    print(f"  Got {len(all_vessels)} unique vessels", file=sys.stderr)
    return list(all_vessels.values())


def fetch_all_tiles_from_file():
    """Load vessel data from a JSON dump file (created by browser-dump.js or manual export)."""
    dump_file = os.path.join(STATE_DIR, "vessel-dump.json")
    if os.path.exists(dump_file):
        age = time.time() - os.path.getmtime(dump_file)
        if age < 1800:  # <30 min old
            data = json.load(open(dump_file))
            print(f"  Loaded {len(data)} vessels from dump (age: {int(age)}s)", file=sys.stderr)
            return data
    return None


def is_tanker(vessel):
    st = int(vessel.get("SHIPTYPE", 0) or 0)
    gt = int(vessel.get("GT_SHIPTYPE", 0) or 0)
    if 80 <= st <= 89:
        return True
    if st in (7, 8, 9) or gt in (7, 8, 9):
        return True
    return False


def is_lng_gas_carrier(vessel):
    """Identify LNG/LPG/Gas carriers by GT_SHIPTYPE, name, or destination."""
    gt = str(vessel.get("GT_SHIPTYPE", ""))
    name = str(vessel.get("SHIPNAME", "")).upper()
    # GT_SHIPTYPE: 75=LNG Tanker, 18=LNG (MT convention varies), 19=LPG, 195=Gas
    if gt in ("75", "18", "19", "195"):
        return True
    if any(x in name for x in ["LNG", "GAS", "METHANE", "ARCTIC"]):
        return True
    return False


def is_qatar_related(vessel):
    """Vessel heading to/from Qatar LNG terminals."""
    dest = str(vessel.get("DESTINATION", "")).upper()
    return any(x in dest for x in ["QATAR", "RAS LAFFAN", "MESAIEED", "DOHA", "QALHAT"])


def compute_metrics(vessels):
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    metrics = {
        "timestamp": ts,
        "total_vessels": len(vessels),
        "total_tankers": 0,
        "fujairah_anchored": 0, "fujairah_moving": 0,
        "inside_gulf_stuck": 0, "inside_gulf_moving": 0,
        "strait_transiting": 0, "strait_slow": 0,
        "gulf_oman_queue": 0, "gulf_oman_moving": 0,
        "arabian_sea_queue": 0,
        "iran_flagged": 0, "fujairah_destination": 0,
        "avg_strait_speed": 0.0, "zero_speed_tankers": 0,
        "zones": {"strait": 0, "fujairah": 0, "inside_gulf": 0, "gulf_oman": 0, "arabian_sea": 0, "other": 0},
        # LNG/Gas specific
        "lng_gas_total": 0, "lng_gas_stuck": 0, "lng_gas_transiting": 0,
        "lng_gas_anchored_fujairah": 0, "lng_gas_inside_gulf": 0,
        "lng_gas_queue_oman": 0, "qatar_bound": 0, "qatar_bound_stuck": 0,
    }

    strait_speeds = []
    sample_vessels = {"fujairah": [], "strait": [], "inside_gulf": []}

    for v in vessels:
        lat = float(v.get("LAT", 0) or 0)
        lon = float(v.get("LON", 0) or 0)
        speed_raw = float(v.get("SPEED", 0) or 0)
        flag = v.get("FLAG", "")
        dest = (v.get("DESTINATION") or "").upper().strip()
        name = v.get("SHIPNAME", "?")

        speed_kn = speed_raw / 10.0

        zone = classify_zone(lat, lon)
        metrics["zones"][zone] = metrics["zones"].get(zone, 0) + 1

        tanker = is_tanker(v)
        if tanker:
            metrics["total_tankers"] += 1

        if flag == "IR":
            metrics["iran_flagged"] += 1
        if "FUJAIRAH" in dest:
            metrics["fujairah_destination"] += 1
        if speed_kn < 0.1 and tanker:
            metrics["zero_speed_tankers"] += 1

        # LNG/Gas carrier tracking
        lng = is_lng_gas_carrier(v)
        if lng:
            metrics["lng_gas_total"] += 1
            if speed_kn < 2.0:
                metrics["lng_gas_stuck"] += 1
            if zone == "strait" and speed_kn > 3.0:
                metrics["lng_gas_transiting"] += 1
            elif zone == "fujairah" and speed_kn < 1.5:
                metrics["lng_gas_anchored_fujairah"] += 1
            elif zone == "inside_gulf" and speed_kn < 2.0:
                metrics["lng_gas_inside_gulf"] += 1
            elif zone == "gulf_oman" and speed_kn < 2.0:
                metrics["lng_gas_queue_oman"] += 1

        if is_qatar_related(v):
            metrics["qatar_bound"] += 1
            if speed_kn < 2.0:
                metrics["qatar_bound_stuck"] += 1

        if not tanker:
            continue

        if zone == "fujairah":
            if speed_kn < 1.5:
                metrics["fujairah_anchored"] += 1
            else:
                metrics["fujairah_moving"] += 1
            if len(sample_vessels["fujairah"]) < 5:
                sample_vessels["fujairah"].append(f"{name}({flag},{speed_kn:.1f}kn)")

        elif zone == "strait":
            strait_speeds.append(speed_kn)
            if speed_kn > 3.0:
                metrics["strait_transiting"] += 1
            else:
                metrics["strait_slow"] += 1
            if len(sample_vessels["strait"]) < 5:
                sample_vessels["strait"].append(f"{name}({flag},{speed_kn:.1f}kn)")

        elif zone == "inside_gulf":
            if speed_kn > 2.0:
                metrics["inside_gulf_moving"] += 1
            else:
                metrics["inside_gulf_stuck"] += 1
            if len(sample_vessels["inside_gulf"]) < 5:
                sample_vessels["inside_gulf"].append(f"{name}({flag},{speed_kn:.1f}kn)")

        elif zone == "gulf_oman":
            if speed_kn < 2.0:
                metrics["gulf_oman_queue"] += 1
            else:
                metrics["gulf_oman_moving"] += 1

        elif zone == "arabian_sea":
            metrics["arabian_sea_queue"] += 1

    if strait_speeds:
        metrics["avg_strait_speed"] = round(sum(strait_speeds) / len(strait_speeds), 2)

    metrics["sample_vessels"] = sample_vessels

    severity = (
        metrics["fujairah_anchored"] * 2 +
        metrics["inside_gulf_stuck"] * 1.5 +
        metrics["gulf_oman_queue"] * 1 +
        metrics["arabian_sea_queue"] * 0.5 +
        max(0, 50 - metrics["strait_transiting"]) * 3 +
        metrics["zero_speed_tankers"] * 0.5 +
        metrics["fujairah_destination"] * 0.5 -
        metrics["strait_transiting"] * 2
    )
    metrics["crisis_severity"] = round(max(0, severity), 1)
    return metrics


def append_metrics(metrics):
    with open(METRICS_FILE, "a") as f:
        f.write(json.dumps(metrics) + "\n")


def load_history(n=48):
    if not os.path.exists(METRICS_FILE):
        return []
    lines = open(METRICS_FILE).readlines()
    return [json.loads(l) for l in lines[-n:]]


def print_snapshot(m):
    print(f"\n{'='*60}")
    print(f"  HORMUZ CRISIS TRACKER — {m['timestamp'][:19]}Z")
    print(f"{'='*60}")
    print(f"  Total vessels: {m['total_vessels']}  |  Tankers: {m['total_tankers']}")
    print(f"  Crisis Severity Score: {m['crisis_severity']}")
    print()
    print(f"  📊 ZONE BREAKDOWN (tankers)")
    print(f"  {'─'*45}")
    print(f"  🚢 Fujairah Anchored:    {m['fujairah_anchored']:>4}  (parking = blockade)")
    print(f"  🚢 Fujairah Moving:      {m['fujairah_moving']:>4}")
    print(f"  ⛔ Inside Gulf Stuck:     {m['inside_gulf_stuck']:>4}  (can't exit)")
    print(f"  🏃 Inside Gulf Moving:    {m['inside_gulf_moving']:>4}")
    print(f"  ✅ Strait Transiting:     {m['strait_transiting']:>4}  (passing through)")
    print(f"  🐢 Strait Slow/Stopped:   {m['strait_slow']:>4}")
    print(f"  ⏳ Gulf of Oman Queue:    {m['gulf_oman_queue']:>4}  (waiting outside)")
    print(f"  🏃 Gulf of Oman Moving:   {m['gulf_oman_moving']:>4}")
    print(f"  🌊 Arabian Sea Queue:     {m['arabian_sea_queue']:>4}")
    print()
    print(f"  🔍 INDICATORS")
    print(f"  {'─'*45}")
    print(f"  Avg Strait Speed:         {m['avg_strait_speed']:>5.1f} kn")
    print(f"  Zero Speed Tankers:       {m['zero_speed_tankers']:>4}")
    print(f"  Fujairah-bound:           {m['fujairah_destination']:>4}")
    print(f"  Iran-flagged:             {m['iran_flagged']:>4}")
    print()
    samples = m.get("sample_vessels", {})
    for zone in ["fujairah", "strait", "inside_gulf"]:
        if samples.get(zone):
            print(f"  Sample {zone}: {', '.join(samples[zone])}")
    print()


def print_trend(history):
    if len(history) < 2:
        print("  Need 2+ snapshots for trend.")
        return
    latest = history[-1]
    prev = history[-2]
    print(f"\n  📈 TREND (vs previous)")
    print(f"  {'─'*45}")

    def delta(key, good_up=False):
        d = latest.get(key, 0) - prev.get(key, 0)
        if isinstance(d, float):
            arrow = ("🟢↑" if good_up else "🔴↑") if d > 0 else ("🔴↓" if good_up else "🟢↓") if d < 0 else "  ─"
            return f"{arrow} {d:+.1f}"
        arrow = ("🟢↑" if good_up else "🔴↑") if d > 0 else ("🔴↓" if good_up else "🟢↓") if d < 0 else "  ─"
        return f"{arrow} {d:+d}"

    print(f"  Fujairah Anchored:  {delta('fujairah_anchored')}")
    print(f"  Inside Gulf Stuck:  {delta('inside_gulf_stuck')}")
    print(f"  Strait Transiting:  {delta('strait_transiting', good_up=True)}")
    print(f"  GoO Queue:          {delta('gulf_oman_queue')}")
    print(f"  Avg Strait Speed:   {delta('avg_strait_speed', good_up=True)}")
    print(f"  Severity:           {delta('crisis_severity')}")
    print()


def export_csv(history):
    if not history:
        print("No data.")
        return
    keys = ["timestamp", "total_tankers", "fujairah_anchored", "inside_gulf_stuck",
            "strait_transiting", "strait_slow", "gulf_oman_queue", "arabian_sea_queue",
            "avg_strait_speed", "zero_speed_tankers", "fujairah_destination",
            "iran_flagged", "crisis_severity"]
    print(",".join(keys))
    for m in history:
        print(",".join(str(m.get(k, "")) for k in keys))


def collect_via_browser_eval():
    """Collect all tile data by evaluating fetch() inside the openclaw browser."""
    all_vessels = {}
    for z, x, y in TILES_Z8:
        url = f"https://www.marinetraffic.com/getData/get_data_json_4/z:{z}/X:{x}/Y:{y}/station:0"
        js_code = f"fetch('{url}', {{headers: {{'X-Requested-With': 'XMLHttpRequest'}}}}).then(r=>r.text()).then(t=>window.__mt_tile_{x}_{y}=t)"

        # Write JS to a temp file, use openclaw browser to evaluate
        # This is called from the parent (openclaw agent) context
        # For standalone use, read from vessel-dump.json instead
        print(f"  [INFO] Standalone mode: looking for vessel-dump.json", file=sys.stderr)
        return None

    return list(all_vessels.values()) if all_vessels else None


def main():
    parser = argparse.ArgumentParser(description="Hormuz Crisis AIS Tracker")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--csv", action="store_true")
    parser.add_argument("--dump", type=str, help="Load vessels from JSON file")
    args = parser.parse_args()

    if args.csv:
        export_csv(load_history(999))
        return
    if args.report:
        history = load_history(48)
        if history:
            print_snapshot(history[-1])
            print_trend(history)
        else:
            print("No data yet.")
        return

    # Try to load from dump file
    vessels = None
    if args.dump:
        vessels = json.load(open(args.dump))
        print(f"  Loaded {len(vessels)} vessels from {args.dump}", file=sys.stderr)
    else:
        vessels = fetch_all_tiles_from_file()

    if not vessels:
        print("No vessel data. Use --dump <file> or create vessel-dump.json via browser.", file=sys.stderr)
        print("To create dump, run in openclaw:\n  python3 browser-dump.py", file=sys.stderr)
        return

    m = compute_metrics(vessels)
    append_metrics(m)
    print_snapshot(m)
    history = load_history(48)
    if len(history) >= 2:
        print_trend(history)


if __name__ == "__main__":
    main()
