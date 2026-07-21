import requests
from curl_cffi import requests as cffi_requests
import time
import json
import os
import subprocess
from datetime import datetime

# --- CONFIGURATION ---
DATES = ["20260725","20260726"]
VENUE_CODE = "PRHN"
EVENT_CODE = "ET00506460"
STATE_FILE = "odyssey_shows_state.json"  # NEW STATE FILE
MAX_RUNTIME_SECONDS = (5 * 3600) + (55 * 60) # 5 hours 55 mins

# Track WARP State natively
USE_WARP = False

# Cloudflare WARP local proxy
PROXIES = {
    "http": "socks5://127.0.0.1:40000",
    "https": "socks5://127.0.0.1:40000"
}

GET_HEADERS = {
    "Host": "in.bookmyshow.com",
    "Content-Type": "application/json",
    "X-Latitude": "17.385044",
    "X-Subregion-Code": "HYD",
    "X-App-Code": "MOBAND2",
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 10; Android SDK built for x86_64 Build/QSR1.211112.011)",
    "X-App-Version": "18.2.3",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive"
}

def humanize_date(date_str):
    dt = datetime.strptime(date_str, "%Y%m%d")
    day = dt.day

    if 11 <= (day % 100) <= 13:
        suffix = 'th'
    else:
        suffix = ['th', 'st', 'nd', 'rd', 'th'][min(day % 10, 4)]
        
    month_name = dt.strftime("%B")
    return f"{day}{suffix} {month_name}"

def quiet_git_pull():
    subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, check=False)
    subprocess.run(["git", "reset", "--hard", "origin/main"], capture_output=True, check=False)

def quiet_git_push():
    res = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True, check=False)
    return res.returncode == 0

def read_local_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"[STATE] ⚠️ JSON Error reading state: {e}")
            return {}
    return {}

def load_state():
    quiet_git_pull()
    return read_local_state()

def save_state(deltas, commit_msg="Update discovered shows state"):
    for attempt in range(3):
        quiet_git_pull()
        latest_state = read_local_state()
        
        for s_id, s_data in deltas.items():
            latest_state[s_id] = s_data
            
        with open(STATE_FILE, "w") as f:
            json.dump(latest_state, f, indent=2)
            
        subprocess.run(["git", "add", STATE_FILE], capture_output=True, check=False)
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        
        if STATE_FILE in status.stdout:
            print(f"[GIT] Committing changes to {STATE_FILE} (Attempt {attempt+1})...")
            subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True, check=False)
            
            if quiet_git_push():
                print(f"[GIT] Successfully pushed merged state to repository.")
                return latest_state
            else:
                print(f"[GIT] Push attempt {attempt+1} failed. Retrying merge...")
                time.sleep(2)
        else:
            print("[GIT] Merged state is identical to remote. Nothing to push.")
            return latest_state
            
    print("[GIT] ❌ Failed to push after 3 attempts.")
    return latest_state

def trigger_ntfy(message):
    print(f"\n[!] ALERTING VIA NTFY:\n{message}")
    for i in range(1):
        try:
            resp = requests.post(
                "https://ntfy.sh/odssy_stlyt",
                data=message.encode('utf-8'),
                headers={"Priority": "urgent"},
                timeout=10
            )
            print(f"    -> Ntfy ping {i+1}/1 sent! Status: {resp.status_code}")
        except Exception as e:
            print(f"    -> Ntfy ping {i+1} failed: {e}")

def toggle_warp():
    global USE_WARP
    if USE_WARP:
        print("    -> 🚨 [IP ROTATION] WARP is currently ON. Disconnecting WARP (Switching to Runner IP)...")
        subprocess.run(["warp-cli", "--accept-tos", "disconnect"], capture_output=True, check=False)
        USE_WARP = False
    else:
        print("    -> 🚨 [IP ROTATION] WARP is currently OFF. Connecting to WARP (Switching to Cloudflare Proxy)...")
        subprocess.run(["warp-cli", "--accept-tos", "connect"], capture_output=True, check=False)
        time.sleep(5)
        USE_WARP = True

def make_bms_request(method, url, max_retries=3, **kwargs):
    for attempt in range(1, max_retries + 1):
        current_proxies = PROXIES if USE_WARP else None
        try:
            if method.upper() == 'GET':
                resp = cffi_requests.get(url, proxies=current_proxies, impersonate="chrome", timeout=15, **kwargs)
            else:
                resp = cffi_requests.post(url, proxies=current_proxies, impersonate="chrome", timeout=15, **kwargs)
            
            print(f"    -> Status: {resp.status_code} (Using WARP: {USE_WARP})")
            
            if resp.status_code in [429,403]:
                print(f"    -> ⚠️ Rate limited ({resp.status_code}) on attempt {attempt}/{max_retries}.")
                if attempt < max_retries:
                    toggle_warp()
                    print("    -> Retrying request...")
                    continue 
                else:
                    print("    -> ❌ Max retries reached for this request.")
            return resp
        except Exception as e:
            print(f"    -> ⚠️ Network exception on attempt {attempt}: {e}")
            if attempt < max_retries:
                time.sleep(3)
                continue
    return None

def fetch_sessions():
    sessions = []
    for date_code in DATES:
        time.sleep(6) # Built-in delay between checking different dates to avoid IP blocks
        print(f"\n[NETWORK] Fetching sessions for Date: {date_code}...")
        url = f"https://in.bookmyshow.com/api/movies-data/seatlayout/v1/primary?eventCode={EVENT_CODE}&dateCode={date_code}&regionCode=HYD&venueCode={VENUE_CODE}"
        
        resp = make_bms_request('GET', url, headers=GET_HEADERS)
        if not resp or resp.status_code != 200:
            print(f"    -> Failed fetching {date_code}. Skipping...")
            continue
            
        try:
            data = resp.json()
            shows = data.get("data", {}).get("showTimes", [])
            print(f"    -> Found {len(shows)} total shows. Filtering for PCX HDR by BARCO...")
            
            pcx_count = 0
            for show in shows:
                if show.get("attributes") == "PCX HDR by BARCO":
                    sessions.append({
                        "sessionId": show["sessionId"],
                        "dateCode": show["showDateCode"],
                        "time": show["showTime"]
                    })
                    pcx_count += 1
            print(f"    -> Filtered {pcx_count} matching sessions for {date_code}.")
            
        except Exception as e:
            print(f"    -> JSON Parse error for {date_code}: {e}")
            
    return sessions

def main():
    start_time = time.time()
    
    print("==================================================")
    print("🚀 STARTING SHOWTIME DISCOVERY MONITOR")
    print("==================================================")

    print("\n[GIT] Loading initial state from repository...")
    state = load_state()
    is_first_run = len(state) == 0
    
    if is_first_run:
        print("[STATE] Empty state found. Baseline will be initialized on first scan without alerting...")
    else:
        print(f"[STATE] Loaded {len(state)} previously discovered shows from memory.")

    cycle_count = 1
    
    while (time.time() - start_time) < MAX_RUNTIME_SECONDS:
        print(f"\n==================================================")
        print(f"🔄 STARTING POLLING CYCLE {cycle_count}")
        print(f"==================================================")
        
        # 1. Fetch current live sessions from BMS
        target_sessions = fetch_sessions()
        deltas = {}
        
        if not target_sessions:
            print("    -> No PCX HDR by BARCO sessions found yet.")
            
        # 2. Compare against our Git memory
        for session in target_sessions:
            s_id = session["sessionId"]
            s_date = session["dateCode"]
            s_time = session["time"]
            
            # If we find an ID we haven't seen before
            if s_id not in state:
                print(f"    -> 🟢 DETECTED NEW SHOW: {s_id} for {s_date} at {s_time}!")
                
                # Format exactly as requested
                human_date = humanize_date(s_date)
                msg = f"New showtime added for #TheOdyssey at PCX HDR by BARCO.\n\n{human_date}, {s_time}"
                
                # Only alert if it's NOT the first initialization run 
                # (to avoid getting spammed with existing shows when you first boot up)
                if not is_first_run:
                    trigger_ntfy(msg)
                
                # Save it to our state so we don't alert again in the next cycle
                state[s_id] = {"date": s_date, "time": s_time}
                deltas[s_id] = state[s_id]

        # 3. If new shows were found, save to GitHub to act as persistent memory
        if deltas:
            print(f"\n[STATE] Cycle finished. {len(deltas)} new show(s) detected, committing to Git...")
            state = save_state(deltas, f"Added {len(deltas)} new shows at cycle {cycle_count}")
        else:
            print("\n[STATE] Cycle finished. No new shows detected.")
            
        if is_first_run:
            is_first_run = False
            print("[STATE] First run baseline successfully established. Alerts are now armed.")
            
        cycle_count += 1
        
        # 4. The requested 20-second wait before the next loop iteration begins
        print("\n⏳ Sleeping for 20 seconds before the next check...")
        time.sleep(20)
        
    print("\n🏁 Time limit reached (5h 55m). Gracefully shutting down.")

if __name__ == "__main__":
    main()
