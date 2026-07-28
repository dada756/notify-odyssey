import requests
from curl_cffi import requests as cffi_requests
import time
import json
import os
import subprocess
from datetime import datetime

# --- CONFIGURATION ---
DATES = ["20260730", "20260731", "20260801", "20260802"]
VENUE_CODE = "PRHN"
STATE_FILE = "odyssey_shows_state.json"
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
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive"
}

# --- VERBOSE LOGGING SYSTEM ---
def get_timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(level, message):
    """Custom logger to prepend timestamp and log level."""
    print(f"[{level}] {message}")

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

# --- GIT OPERATIONS ---
def verbose_git_pull():
    log("GIT", "Initiating git fetch and reset to sync state...")
    
    fetch_res = subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, text=True, check=False)
    log("GIT-DEBUG", f"Fetch STDOUT: {fetch_res.stdout.strip()} | STDERR: {fetch_res.stderr.strip()}")
    
    reset_res = subprocess.run(["git", "reset", "--hard", "origin/main"], capture_output=True, text=True, check=False)
    log("GIT-DEBUG", f"Reset STDOUT: {reset_res.stdout.strip()} | STDERR: {reset_res.stderr.strip()}")

def verbose_git_push():
    log("GIT", "Initiating git push to origin/main...")
    res = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True, check=False)
    log("GIT-DEBUG", f"Push STDOUT: {res.stdout.strip()} | STDERR: {res.stderr.strip()}")
    return res.returncode == 0

# --- STATE MANAGEMENT ---
def read_local_state():
    log("STATE", f"Looking for local state file: {STATE_FILE}")
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
                if "known_movies" not in state: state["known_movies"] = []
                if "known_sessions" not in state: state["known_sessions"] = {}
                log("STATE-DEBUG", f"Successfully parsed JSON. Found {len(state['known_movies'])} movies, {len(state['known_sessions'])} sessions.")
                return state
        except json.JSONDecodeError as e:
            log("ERROR", f"JSON Error reading state: {e}. Defaulting to empty state.")
            return {"known_movies": [], "known_sessions": {}}
    log("STATE", "State file not found. Initializing empty state.")
    return {"known_movies": [], "known_sessions": {}}

def load_state():
    verbose_git_pull()
    return read_local_state()

def save_state(full_new_state, commit_msg):
    log("STATE", "Attempting to save state to repository...")
    for attempt in range(3):
        log("STATE", f"Save attempt {attempt+1}/3")
        verbose_git_pull()
        
        with open(STATE_FILE, "w") as f:
            json.dump(full_new_state, f, indent=2)
        log("STATE-DEBUG", f"Written updated JSON to local {STATE_FILE}")
            
        add_res = subprocess.run(["git", "add", STATE_FILE], capture_output=True, text=True, check=False)
        log("GIT-DEBUG", f"Add STDOUT: {add_res.stdout.strip()} | STDERR: {add_res.stderr.strip()}")
        
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        log("GIT-DEBUG", f"Status Output:\n{status.stdout.strip()}")
        
        if STATE_FILE in status.stdout:
            log("GIT", f"Committing changes: '{commit_msg}'")
            commit_res = subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True, text=True, check=False)
            log("GIT-DEBUG", f"Commit STDOUT: {commit_res.stdout.strip()} | STDERR: {commit_res.stderr.strip()}")
            
            if verbose_git_push():
                log("GIT", "Successfully pushed merged state to repository.")
                return full_new_state
            else:
                log("WARN", f"Push attempt {attempt+1} failed. Retrying in 2 seconds...")
                time.sleep(2)
        else:
            log("GIT", "Merged state is identical to remote. Nothing to commit or push.")
            return full_new_state
            
    log("ERROR", "Failed to push after 3 attempts.")
    return full_new_state

# --- NOTIFICATION SYSTEM ---
def trigger_ntfy(message):
    log("NTFY", f"Alerting via ntfy. Payload:\n{message}")
    for i in range(1):
        try:
            start_req = time.time()
            resp = requests.post(
                "https://ntfy.sh/odssy_stlyt",
                data=message.encode('utf-8'),
                headers={"Priority": "urgent"},
                timeout=10
            )
            elapsed = time.time() - start_req
            log("NTFY-DEBUG", f"Ping {i+1}/1 sent. Status: {resp.status_code}, Response: {resp.text.strip()}, Time: {elapsed:.2f}s")
        except Exception as e:
            log("ERROR", f"Ntfy ping {i+1} failed: {e}")

# --- NETWORK & PROXY ROTATION ---
def toggle_warp():
    global USE_WARP
    if USE_WARP:
        log("PROXY", "WARP is ON. Disconnecting WARP (Switching to Runner IP)...")
        res = subprocess.run(["warp-cli", "--accept-tos", "disconnect"], capture_output=True, text=True, check=False)
        log("PROXY-DEBUG", f"Disconnect STDOUT: {res.stdout.strip()}")
        USE_WARP = False
    else:
        log("PROXY", "WARP is OFF. Connecting to WARP (Switching to Cloudflare Proxy)...")
        res = subprocess.run(["warp-cli", "--accept-tos", "connect"], capture_output=True, text=True, check=False)
        log("PROXY-DEBUG", f"Connect STDOUT: {res.stdout.strip()}")
        time.sleep(5)
        USE_WARP = True

def make_bms_request(method, url, max_retries=3, **kwargs):
    for attempt in range(1, max_retries + 1):
        current_proxies = PROXIES if USE_WARP else None
        proxy_display = "127.0.0.1:40000" if USE_WARP else "Direct (Runner IP)"
        
        log("NET", f"Request {attempt}/{max_retries} | Method: {method.upper()} | Proxy: {proxy_display}")
        
        try:
            if method.upper() == 'GET':
                resp = cffi_requests.get(url, proxies=current_proxies, impersonate="chrome", timeout=15, **kwargs)
            else:
                resp = cffi_requests.post(url, proxies=current_proxies, impersonate="chrome", timeout=15, **kwargs)
            
            log("NET", f"Status: {resp.status_code}")
            
            if resp.status_code in [429, 403]:
                log("WARN", f"Rate limited or forbidden (HTTP {resp.status_code}).")
                if attempt < max_retries:
                    toggle_warp()
                    log("NET", "Retrying request with new IP...")
                    continue 
                else:
                    log("ERROR", "Max retries reached for this request.")
            return resp
        except Exception as e:
            log("ERROR", f"Network exception on attempt {attempt}: {e}")
            if attempt < max_retries:
                log("NET", "Sleeping for 3 seconds before retry...")
                time.sleep(3)
                continue
    return None

# --- PARSING & SCRAPING ---
def fetch_venue_data():
    current_movies = set()
    current_sessions = {}
    
    for date_code in DATES:
        time.sleep(6) 
        
        url = f"https://in.bookmyshow.com/api/v3/mobile/showtimes/byvenue?appCode=MOBAND2&venueCode={VENUE_CODE}&dateCode={date_code}"
        
        resp = make_bms_request('GET', url, headers=GET_HEADERS)
        if not resp or resp.status_code != 200:
            log("WARN", f"Failed fetching {date_code} or non-200 status. Skipping to next date.")
            continue
            
        try:
            data = resp.json()
            show_details_list = data.get("ShowDetails", [])
            log("PARSE-DEBUG", f"Found {len(show_details_list)} item(s) in 'ShowDetails' array.")
            
            for sd_idx, show_detail in enumerate(show_details_list):
                events = show_detail.get("Event", [])                
                for event in events:
                    event_title = event.get("EventTitle", "Unknown Title")
                    current_movies.add(event_title)
                    log("PARSE-DEBUG", f"--> Event: '{event_title}'")
                    
                    child_events = event.get("ChildEvents", [])
                    
                    for child in child_events:
                        format_lang = f"{child.get('EventDimension', '')} {child.get('EventLanguage', '')}".strip()
                        showtimes = child.get("ShowTimes", [])
                        log("PARSE-DEBUG", f"    --> Format: '{format_lang}' | {len(showtimes)} Showtime(s) listed.")
                        
                        for show in showtimes:
                            s_id = show.get("SessionId")
                            s_time = show.get("ShowTime")
                            s_screen = show.get("ScreenName")
                            if s_id:
                                log("PARSE-DEBUG", f"        --> Found Session: {s_id} | {s_time} | {s_screen}")
                                current_sessions[s_id] = {
                                    "movie": event_title,
                                    "date": show.get("ShowDateCode"),
                                    "time": s_time,
                                    "screen": s_screen,
                                    "format": format_lang
                                }
        except Exception as e:
            log("ERROR", f"JSON Parse error for {date_code}: {e}")
            
    return current_movies, current_sessions

# --- MAIN LOOP ---
def main():
    start_time = time.time()
    
    print("\n" + "="*70)
    log("INFO", "🚀 STARTING ALLU CINEMAS DISCOVERY MONITOR (VERBOSE MODE)")
    print("="*70 + "\n")

    log("INIT", "Loading initial state...")
    state = load_state()
    
    known_movies_mem = set(state.get("known_movies", []))
    known_sessions_mem = state.get("known_sessions", {})
    
    is_first_run = len(known_movies_mem) == 0 and len(known_sessions_mem) == 0
    
    if is_first_run:
        log("INIT", "Empty state found. Baseline will be initialized on first scan without alerting.")
    else:
        log("INIT", f"Memory loaded successfully. Known Movies: {len(known_movies_mem)} | Known Sessions: {len(known_sessions_mem)}")
        log("INIT-DEBUG", f"Known Movies List: {list(known_movies_mem)}")

    cycle_count = 1
    
    while (time.time() - start_time) < MAX_RUNTIME_SECONDS:
        print("\n" + "="*70)
        log("INFO", f"🔄 STARTING POLLING CYCLE {cycle_count}")
        print("="*70 + "\n")
        
        # 1. Fetch current live sessions
        current_movies, current_sessions = fetch_venue_data()
        
        log("LOGIC", f"Data extraction complete. Found {len(current_movies)} unique movies and {len(current_sessions)} total sessions.")
        
        new_movies_discovered = current_movies - known_movies_mem
        new_sessions_discovered = {}
        
        # 2. Compare against our memory
        log("LOGIC", "Comparing extracted sessions against local memory state...")
        for s_id, s_data in current_sessions.items():
            if s_id not in known_sessions_mem:
                log("LOGIC-DEBUG", f"Unrecognized Session ID found: {s_id} for '{s_data['movie']}'")
                new_sessions_discovered[s_id] = s_data

        log("LOGIC", f"Comparison complete. {len(new_movies_discovered)} new movies, {len(new_sessions_discovered)} new sessions.")

        # 3. Alerting Logic
        if not is_first_run:
            for movie in new_movies_discovered:
                log("ALERT", f"🟢 DETECTED NEW MOVIE: {movie}")
                
            if new_sessions_discovered:
                sessions_by_movie = {}
                for s_id, s_data in new_sessions_discovered.items():
                    m = s_data["movie"]
                    if m not in sessions_by_movie:
                        sessions_by_movie[m] = []
                    sessions_by_movie[m].append(s_data)
                    
                for movie, sessions in sessions_by_movie.items():
                    count = len(sessions)
                    dates = sorted(list(set([humanize_date(s["date"]) for s in sessions if s.get("date")])))
                    dates_str = ", ".join(dates)
                    
                    log("ALERT", f"🟢 DETECTED {count} NEW SHOWS FOR: {movie}")
                    msg = f"{count} New showtimes added for '{movie}' at Prasads Cinemas!\n\nDates: {dates_str}"
                    trigger_ntfy(msg)

        # 4. Save to GitHub if state mutated
        if new_movies_discovered or new_sessions_discovered:
            log("STATE", f"Cycle {cycle_count} mutated state. Updating GitHub...")
            
            known_movies_mem.update(new_movies_discovered)
            known_sessions_mem.update(new_sessions_discovered)
            
            full_new_state = {
                "known_movies": list(known_movies_mem),
                "known_sessions": known_sessions_mem
            }
            
            commit_message = f"Added {len(new_movies_discovered)} movies, {len(new_sessions_discovered)} shows at cycle {cycle_count}"
            save_state(full_new_state, commit_message)
        else:
            log("STATE", "No mutations detected in this cycle. Skipping Git operations.")
            
        if is_first_run:
            is_first_run = False
            log("INIT", "First run baseline successfully established. Alerts are now ARMED for next cycle.")
            
        cycle_count += 1
        
        log("INFO", "⏳ Sleeping for 20 seconds before the next loop...")
        time.sleep(21)
        
    log("INFO", "🏁 Time limit reached (5h 55m). Gracefully shutting down to prevent runner force-kill.")

if __name__ == "__main__":
    main()
