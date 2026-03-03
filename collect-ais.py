#!/usr/bin/env python3
"""
Automated AIS collector for Hormuz Crisis Dashboard.
Fetches vessel data via CDP WebSocket to MarineTraffic browser tab,
processes with hormuz-tracker.py, commits and pushes to GitHub.

Cron: */30 * * * * cd /tmp/hormuz-crisis && python3 collect-ais.py >> state/collect.log 2>&1
"""
import json, os, subprocess, sys, time
from datetime import datetime, timezone

REPO_DIR = "/tmp/hormuz-crisis"
DUMP_FILE = os.path.join(REPO_DIR, "vessel-dump.json")
TRACKER = os.path.join(REPO_DIR, "hormuz-tracker.py")
CDP_PORT = 18800

TILES = [(8,83,54),(8,83,55),(8,84,54),(8,84,55),(8,85,54),(8,85,55),(8,86,54),(8,86,55),(8,87,54),(8,87,55)]


def find_mt_tab():
    try:
        r = subprocess.run(["curl","-s",f"http://127.0.0.1:{CDP_PORT}/json"],
                          capture_output=True, text=True, timeout=5)
        for t in json.loads(r.stdout):
            if "marinetraffic.com" in t.get("url",""):
                return t["id"]
    except Exception as e:
        print(f"CDP error: {e}", file=sys.stderr)
    return None


def fetch_vessels(tab_id):
    fetches = ",".join(
        f"fetch('https://www.marinetraffic.com/getData/get_data_json_4/z:{z}/X:{x}/Y:{y}/station:0',"
        f"{{headers:{{'X-Requested-With':'XMLHttpRequest'}}}}).then(r=>r.json()).catch(()=>({{data:{{rows:[]}}}}))"
        for z,x,y in TILES
    )
    expr = (f"(async()=>{{const R=await Promise.all([{fetches}]);"
            "const A={};R.forEach(d=>(d?.data?.rows||[]).forEach(v=>{if(v.SHIP_ID)A[v.SHIP_ID]=v}));"
            "return JSON.stringify(Object.values(A))})()")
    
    node_code = f"""
const ws = new WebSocket('ws://127.0.0.1:{CDP_PORT}/devtools/page/{tab_id}');
ws.addEventListener('open', () => {{
    ws.send(JSON.stringify({{id:1, method:'Runtime.evaluate',
        params:{{expression:{json.dumps(expr)}, awaitPromise:true, returnByValue:true, timeout:30000}}}}));
}});
ws.addEventListener('message', (ev) => {{
    const msg = JSON.parse(ev.data);
    if (msg.id === 1) {{
        const val = msg.result?.result?.value;
        if (val) process.stdout.write(val);
        ws.close();
        setTimeout(()=>process.exit(0), 100);
    }}
}});
ws.addEventListener('error', () => process.exit(1));
setTimeout(() => process.exit(0), 35000);
"""
    
    script = "/tmp/mt-collect.js"
    with open(script, "w") as f:
        f.write(node_code)
    
    r = subprocess.run(["node", script], capture_output=True, timeout=40)
    if r.stdout:
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            # Try saving raw and loading — might be oversized but valid
            with open(DUMP_FILE, "wb") as f:
                f.write(r.stdout)
            try:
                return json.load(open(DUMP_FILE))
            except:
                pass
    print(f"Fetch failed: {r.stderr[:200] if r.stderr else 'no output'}", file=sys.stderr)
    return None


def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n=== AIS Collection {ts} ===")
    
    subprocess.run(["git","pull","--ff-only"], cwd=REPO_DIR, capture_output=True, timeout=15)
    
    tab_id = find_mt_tab()
    if not tab_id:
        print("ERROR: No MarineTraffic tab", file=sys.stderr)
        return 1
    
    vessels = fetch_vessels(tab_id)
    if not vessels or len(vessels) < 50:
        print(f"ERROR: {len(vessels) if vessels else 0} vessels", file=sys.stderr)
        return 1
    print(f"Fetched {len(vessels)} vessels")
    
    with open(DUMP_FILE, "w") as f:
        json.dump(vessels, f)
    
    r = subprocess.run([sys.executable, TRACKER, "--dump", DUMP_FILE],
                      cwd=REPO_DIR, capture_output=True, text=True, timeout=30)
    print(r.stdout)
    
    subprocess.run(["git","add","hormuz-metrics.jsonl"], cwd=REPO_DIR, capture_output=True, timeout=10)
    
    with open(os.path.join(REPO_DIR, "hormuz-metrics.jsonl")) as f:
        n = sum(1 for _ in f)
    
    r = subprocess.run(["git","commit","-m",f"AIS snapshot #{n} — {len(vessels)} vessels ({ts})"],
                      cwd=REPO_DIR, capture_output=True, text=True, timeout=10)
    if r.returncode == 0:
        push = subprocess.run(["git","push","origin","main"], cwd=REPO_DIR,
                             capture_output=True, text=True, timeout=30)
        status = "Pushed" if push.returncode == 0 else "PUSH FAILED"
        print(f"{status} snapshot #{n}")
        if push.returncode != 0:
            print(push.stderr[:200], file=sys.stderr)
    else:
        print("No changes")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
