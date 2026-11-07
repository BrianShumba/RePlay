import sys
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = "https://api.openf1.org/v1"
OUT_ROOT = Path("data")

# Rate limit: 3 req/sec and 30 req/min. We'll use 0.35s between requests
# to stay under 3/sec (3 per 1.05s max), and a minute-window counter.
MIN_GAP = 0.35
_req_times = []

def _wait_for_slot():
    now = time.time()
    # enforce min gap
    if _req_times:
        elapsed = now - _req_times[-1]
        if elapsed < MIN_GAP:
            time.sleep(MIN_GAP - elapsed)
            now = time.time()
    # enforce 30/min
    cutoff = now - 60
    _req_times[:] = [t for t in _req_times if t > cutoff]
    if len(_req_times) >= 30:
        sleep_for = 60 - (now - _req_times[0]) + 0.1
        print(f"  [rate] 30/min reached, sleeping {sleep_for:.1f}s")
        time.sleep(sleep_for)
        now = time.time()
        _req_times[:] = [t for t in _req_times if t > now - 60]
    _req_times.append(now)

def api_get(path, params=None):
    """GET with retry on 429 and 5xx. Returns (data, row_count)."""
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(5):
        _wait_for_slot()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "f1-replay-fetch/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                count = len(data) if isinstance(data, list) else 1
                print(f"  GET {path} {params or ''} -> {count} rows")
                return data, count
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"  GET {path} {params or ''} -> 404 (no rows)")
                return [], 0
            if e.code == 429 or e.code >= 500:
                wait = 2 ** attempt
                print(f"  GET {path} -> HTTP {e.code}, retry in {wait}s (attempt {attempt+1}/5)")
                time.sleep(wait)
                continue
            print(f"  GET {path} -> HTTP {e.code} (giving up)")
            return [], 0
        except Exception as e:
            wait = 2 ** attempt
            print(f"  GET {path} -> {e}, retry in {wait}s (attempt {attempt+1}/5)")
            time.sleep(wait)
    print(f"  GET {path} -> exhausted retries")
    return [], 0

def fetch_all(endpoint, session_key):
    data, _ = api_get(f"/{endpoint}", {"session_key": session_key})
    if data:
        print(f"  sample {endpoint}: {json.dumps(data[0], default=str)[:300]}")
    return data

def downsample_location(samples, start_dt):
    """Keep ~1 sample/second. Returns list of [t_sec, x, y]."""
    if not samples:
        return []
    # sort by date
    samples.sort(key=lambda s: s.get("date", ""))
    kept = []
    last_sec = None
    for s in samples:
        try:
            dt = datetime.fromisoformat(s["date"].replace("Z", "+00:00"))
            t = (dt - start_dt).total_seconds()
        except Exception:
            continue
        sec = int(t)
        if sec != last_sec:
            kept.append([round(t, 2), s.get("x"), s.get("y")])
            last_sec = sec
    return kept

def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/fetch_session.py <session_key>")
        sys.exit(1)
    session_key = sys.argv[1]
    out_dir = OUT_ROOT / session_key
    loc_dir = out_dir / "location"
    loc_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== session_key={session_key} ===")
    print("Fetching session + meeting info...")
    sessions = fetch_all("sessions", session_key)
    if not sessions:
        print("FATAL: no session info returned")
        sys.exit(1)
    sess = sessions[0]
    meeting_key = sess.get("meeting_key")
    meetings = fetch_all("meetings", None) if not meeting_key else []
    if meeting_key:
        # meetings endpoint doesn't take session_key; filter manually after fetching by year
        year = None
        try:
            year = datetime.fromisoformat(sess["date_start"].replace("Z","+00:00")).year
        except Exception:
            pass
        if year:
            meetings, _ = api_get("/meetings", {"year": year})
            meetings = [m for m in meetings if m.get("meeting_key") == meeting_key]
    meeting = meetings[0] if meetings else {}

    # write meta
    meta = {
        "session_key": session_key,
        "session_name": sess.get("session_name"),
        "date_start": sess.get("date_start"),
        "date_end": sess.get("date_end"),
        "year": meeting.get("year"),
        "meeting_name": meeting.get("meeting_name") or meeting.get("circuit_short_name"),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"meta.json written")

    # determine start time for seconds-from-start
    try:
        start_dt = datetime.fromisoformat(sess["date_start"].replace("Z", "+00:00"))
    except Exception:
        print("WARN: could not parse session date_start, will use earliest location date")
        start_dt = None

    drivers = fetch_all("drivers", session_key)
    (out_dir / "drivers.json").write_text(json.dumps(drivers, indent=2))
    print(f"drivers.json written ({len(drivers)} drivers)")

    laps = fetch_all("laps", session_key)
    (out_dir / "laps.json").write_text(json.dumps(laps, indent=2))
    print(f"laps.json written ({len(laps)} rows)")

    positions = fetch_all("position", session_key)
    (out_dir / "positions.json").write_text(json.dumps(positions, indent=2))
    print(f"positions.json written ({len(positions)} rows)")

    stints = fetch_all("stints", session_key)
    (out_dir / "stints.json").write_text(json.dumps(stints, indent=2))
    print(f"stints.json written ({len(stints)} rows)")

    # Location: per driver, in time windows
    print("Fetching location (per driver, windowed)...")
    if start_dt is None and laps:
        try:
            start_dt = datetime.fromisoformat(laps[0]["date_start"].replace("Z", "+00:00"))
            print(f"Using earliest lap date as start: {start_dt}")
        except Exception:
            pass
    if start_dt is None:
        print("WARN: no start time available, location timestamps will be raw")
        start_dt = datetime(1970, 1, 1, tzinfo=timezone.utc)

    # estimate session end
    end_dt = None
    try:
        end_dt = datetime.fromisoformat(sess["date_end"].replace("Z", "+00:00"))
    except Exception:
        if laps:
            try:
                end_dt = datetime.fromisoformat(laps[-1]["date_start"].replace("Z", "+00:00")) + timedelta(minutes=5)
            except Exception:
                pass
    if end_dt is None:
        end_dt = start_dt + timedelta(hours=3)
    print(f"Location window: {start_dt} -> {end_dt}")

    driver_nums = [d["driver_number"] for d in drivers if "driver_number" in d]
    print(f"Fetching location for {len(driver_nums)} drivers")

    # 2-minute windows
    window = timedelta(minutes=2)
    for dn in driver_nums:
        all_samples = []
        cur = start_dt
        while cur < end_dt:
            nxt = min(cur + window, end_dt)
            date_gt = cur.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00"
            date_lt = nxt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00"
            rows, count = api_get("/location", {
                "session_key": session_key,
                "driver_number": dn,
                "date>": date_gt,
                "date<": date_lt,
            })
            if rows:
                all_samples.extend(rows)
            cur = nxt
        if all_samples:
            down = downsample_location(all_samples, start_dt)
            (loc_dir / f"{dn}.json").write_text(json.dumps(down))
            print(f"  driver {dn}: {len(all_samples)} raw -> {len(down)} kept")
        else:
            print(f"  driver {dn}: no location data")
        # save incrementally in case of later failure
        time.sleep(MIN_GAP)

    # update sessions index
    idx_path = OUT_ROOT / "sessions.json"
    idx = []
    if idx_path.exists():
        try:
            idx = json.loads(idx_path.read_text())
        except Exception:
            idx = []
    # remove existing entry for this session_key
    idx = [e for e in idx if str(e.get("session_key")) != str(session_key)]
    idx.append({
        "session_key": session_key,
        "year": meta.get("year"),
        "meeting_name": meta.get("meeting_name"),
        "session_name": meta.get("session_name"),
        "date_start": meta.get("date_start"),
    })
    idx_path.write_text(json.dumps(idx, indent=2))
    print(f"sessions.json updated ({len(idx)} entries)")
    print("=== DONE ===")

if __name__ == "__main__":
    main()
