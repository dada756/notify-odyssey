import requests
from curl_cffi import requests as cffi_requests
import time
import json
import os
import subprocess
from datetime import datetime

# --- CONFIGURATION ---
DATES = ["20260729", "20260730", "20260731", "20260801", "20260802"]
VENUE_CODE = "ALUC"
STATE_FILE = "aluc_venue_state.json"  # NEW STATE FILE
MAX_RUNTIME_SECONDS = (5 * 3600) + (55 * 60)  # 5 hours 55 mins

# Track WARP State natively
USE_WARP = False

# Cloudflare WARP local proxy
PROXIES = {
    "http": "socks5://127.0.0.1:40000",
    "https": "socks5://127.0.0.1:40000"
}

# EXACT HEADERS PROVIDED
GET_HEADERS = {
    "Host": "in.bookmyshow.com",
    "X-Latitude": "17.385044",
    "X-Subregion-Code": "HYD",
    "X-App-Code": "MOBAND2",
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 10; Android SDK built for x86_64 Build/QSR1.211112.011)",
    "X-Longitude": "78.48667",
    "X-Region-Code": "HYD",
    "X-Platform-Code": "ANDROID",
    "Accept-Encoding": "gzip, deflate, br"
}

def humanize_date(date_str):
    if not date_str or len(date_str) != 8:
        return date_str
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
                state = json.load(f)
                # Ensure structure exists
                if "known_movies" not in state: state["known_movies"] = []
                if "known_sessions" not in state: state["known_sessions"] = {}
                return state
        except json.JSONDecodeError as e:
            print(f"[STATE] ⚠️ JSON Error reading state: {e}")
            return {"known_movies": [], "known_sessions": {}}
    return {"known_movies": [], "known_sessions": {}}

def load_state():
    quiet_git_pull()
    return read_local_state()

def save_state(full_new_state, commit_msg="Update discovered shows state"):
    for attempt in range(3):
        quiet_git_pull()
        
        # Write the completely updated state object
        with open(STATE_FILE, "w") as f:
            json.dump(full_new_state, f, indent=2)
            
        subprocess.run(["git", "add", STATE_FILE], capture_output=True, check=False)
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        
        if STATE_FILE in status.stdout:
            print(f"[GIT] Committing changes to {STATE_FILE} (Attempt {attempt+1})...")
            subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True, check=False)
            
            if quiet_git_push():
                print(f"[GIT] Successfully pushed merged state to repository.")
                return full_new_state
            else:
                print(f"[GIT] Push attempt {attempt+1} failed. Retrying merge...")
                time.sleep(2)
        else:
            print("[GIT] Merged state is identical to remote. Nothing to push.")
            return full_new_state
            
    print("[GIT] ❌ Failed to push after 3 attempts.")
    return full_new_state

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
        print("    -> 🚨 [IP ROTATION] WARP is ON. Disconnecting WARP (Switching to Runner IP)...")
        subprocess.run(["warp-cli", "--accept-tos", "disconnect"], capture_output=True, check=False)
        USE_WARP = False
    else:
        print("    -> 🚨 [IP ROTATION] WARP is OFF. Connecting to WARP (Switching to Cloudflare Proxy)...")
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
            
            if resp.status_code in [429, 403]:
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

def fetch_venue_data():
    current_movies = set()
    current_sessions = {}
    
    for date_code in DATES:
        time.sleep(6) # Built-in delay between checking different dates to avoid IP blocks
        print(f"\n[NETWORK] Fetching venue schedule for Date: {date_code}...")
        
        # New API Endpoint
        url = f"https://in.bookmyshow.com/api/v3/mobile/showtimes/byvenue?appCode=MOBAND2&venueCode={VENUE_CODE}&dateCode={date_code}"
        
        resp = make_bms_request('GET', url, headers=GET_HEADERS)
        if not resp or resp.status_code != 200:
            print(f"    -> Failed fetching {date_code}. Skipping...")
            continue
            
        try:
            data = resp.json()
            show_details_list = data.get("ShowDetails", [])
            
            # Traverse nested JSON structure
            for show_detail in show_details_list:
                events = show_detail.get("Event", [])
                
                for event in events:
                    event_title = event.get("EventTitle", "Unknown Title")
                    current_movies.add(event_title)
                    
                    child_events = event.get("ChildEvents", [])
                    for child in child_events:
                        format_lang = f"{child.get('EventDimension', '')} {child.get('EventLanguage', '')}".strip()
                        showtimes = child.get("ShowTimes", [])
                        
                        for show in showtimes:
                            s_id = show.get("SessionId")
                            if s_id:
                                current_sessions[s_id] = {
                                    "movie": event_title,
                                    "date": show.get("ShowDateCode"),
                                    "time": show.get("ShowTime"),
                                    "screen": show.get("ScreenName"),
                                    "format": format_lang
                                }
        except Exception as e:
            print(f"    -> JSON Parse error for {date_code}: {e}")
            
    return current_movies, current_sessions

def main():
    start_time = time.time()
    
    print("==================================================")
    print("🚀 STARTING ALLU CINEMAS DISCOVERY MONITOR")
    print("==================================================")

    print("\n[GIT] Loading initial state from repository...")
    state = load_state()
    
    known_movies_mem = set(state.get("known_movies", []))
    known_sessions_mem = state.get("known_sessions", {})
    
    # Empty baseline check
    is_first_run = len(known_movies_mem) == 0 and len(known_sessions_mem) == 0
    
    if is_first_run:
        print("[STATE] Empty state found. Baseline will be initialized on first scan without alerting...")
    else:
        print(f"[STATE] Loaded {len(known_movies_mem)} movies and {len(known_sessions_mem)} previous sessions.")

    cycle_count = 1
    
    while (time.time() - start_time) < MAX_RUNTIME_SECONDS:
        print(f"\n==================================================")
        print(f"🔄 STARTING POLLING CYCLE {cycle_count}")
        print(f"==================================================")
        
        # 1. Fetch current live sessions
        current_movies, current_sessions = fetch_venue_data()
        
        new_movies_discovered = current_movies - known_movies_mem
        new_sessions_discovered = {}
        
        # 2. Compare against our memory
        for s_id, s_data in current_sessions.items():
            if s_id not in known_sessions_mem:
                new_sessions_discovered[s_id] = s_data

        # 3. Alerting Logic (Only if not initializing baseline)
        if not is_first_run:
            
            # Alert for Brand New Movies
            for movie in new_movies_discovered:
                print(f"    -> 🟢 DETECTED NEW MOVIE: {movie}")
                msg = f"🎬 New Movie Added at ALLU Cinemas!\n\n'{movie}' is now listed. Showtimes are opening."
                trigger_ntfy(msg)
                
            # Group New Showtimes by Movie to prevent spam
            if new_sessions_discovered:
                sessions_by_movie = {}
                for s_id, s_data in new_sessions_discovered.items():
                    m = s_data["movie"]
                    if m not in sessions_by_movie:
                        sessions_by_movie[m] = []
                    sessions_by_movie[m].append(s_data)
                    
                for movie, sessions in sessions_by_movie.items():
                    count = len(sessions)
                    # Extract unique humanized dates to make the alert readable
                    dates = sorted(list(set([humanize_date(s["date"]) for s in sessions if s.get("date")])))
                    dates_str = ", ".join(dates)
                    
                    print(f"    -> 🟢 DETECTED {count} NEW SHOWS FOR: {movie}")
                    msg = f"🎟️ {count} new showtime(s) added for '{movie}' at ALLU Cinemas!\n\nDates: {dates_str}"
                    trigger_ntfy(msg)

        # 4. Save to GitHub if anything changed
        if new_movies_discovered or new_sessions_discovered:
            print(f"\n[STATE] Cycle finished. Updating git with {len(new_movies_discovered)} new movies, {len(new_sessions_discovered)} new shows...")
            
            # Update memory variables
            known_movies_mem.update(new_movies_discovered)
            known_sessions_mem.update(new_sessions_discovered)
            
            # Craft new full state object
            full_new_state = {
                "known_movies": list(known_movies_mem),
                "known_sessions": known_sessions_mem
            }
            
            save_state(full_new_state, f"Added {len(new_movies_discovered)} movies, {len(new_sessions_discovered)} shows at cycle {cycle_count}")
        else:
            print("\n[STATE] Cycle finished. No new shows or movies detected.")
            
        if is_first_run:
            is_first_run = False
            print("[STATE] First run baseline successfully established. Alerts are now armed.")
            
        cycle_count += 1
        
        print("\n⏳ Sleeping for 20 seconds before the next check...")
        time.sleep(20)
        
    print("\n🏁 Time limit reached (5h 55m). Gracefully shutting down.")

if __name__ == "__main__":
    main()
