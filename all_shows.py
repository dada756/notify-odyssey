import requests
from curl_cffi import requests as cffi_requests
import time
import json
import os
import re
import subprocess
import random
import threading
import sys
import signal
import logging
from datetime import datetime

# --- LOGGING CONFIGURATION ---
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s"
)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
DATES = ["20260813"]
VENUE_CODE = "ALUC"
STATE_FILE = "sniped_state_11.json"
MAX_RUNTIME_SECONDS = (5 * 3600) + (55 * 60) # 5 hours 55 mins

# --- NEW: SHOWTIME CONSTRAINTS ---
TARGET_ATTRIBUTE = "DOLBY CINEMA"
TARGET_TIME_START = "06:00 AM"
TARGET_TIME_END = "11:59 PM"

# --- PLAN A: ROW TO TICKET CATEGORY MAP ---
ROW_CATEGORY_MAP = {
    "A": "0005",
    "B": "0006", "C": "0006", "D": "0006", "E": "0006", "F": "0006", "G": "0006", "H": "0006", "I": "0006",
    "J": "0007",
    "K": "0008", "L": "0008", "M": "0008", "N": "0008", "O": "0008", "P": "0008", "Q": "0008", "R": "0008", "S": "0008"
}

# --- AUTO-LOCK / SNIPER SECRETS & CONFIG ---
EMAIL = os.environ.get("BMS_EMAIL") 
PHONE = os.environ.get("BMS_PHONE")
TOPIC = os.environ.get("NTFY_TOPIC")

DESIRED_SEATS = {
    "O": ["32", "33", "34"]
}

# Track WARP State natively (Thread-Safe)
USE_WARP = False
warp_lock = threading.Lock()
last_warp_toggle = 0

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
    "User-Agent": "Mozilla/5.0 (Linux; Android 5.0; SM-G900P Build/LRX21T) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/54.0.2110.1046 Mobile Safari/537.36",
    "X-App-Version": "18.2.1",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive"
}

POST_HEADERS = {
    "Host": "services-in.bookmyshow.com",
    "X-Timeout": "10",
    "X-Latitude": "17.385044",
    "X-Subregion-Code": "HYD",
    "X-App-Code": "MOBAND2",
    "User-Agent": "Mozilla/5.0 (Linux; Android 5.0; SM-G900P Build/LRX21T) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/54.0.2110.1046 Mobile Safari/537.36",
    "X-App-Version": "18.2.1",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept-Encoding": "gzip, deflate"
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
    logger.debug("[GIT] Executing Git pull...")
    subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, check=False)
    subprocess.run(["git", "reset", "--hard", "origin/main"], capture_output=True, check=False)

def quiet_git_push():
    logger.debug("[GIT] Executing Git push...")
    res = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True, check=False)
    return res.returncode == 0

def load_state():
    logger.info("[GIT] Loading initial state from repository...")
    quiet_git_pull()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: 
                return set(json.load(f))
        except json.JSONDecodeError: 
            logger.warning("Failed to decode local state JSON. Returning empty set.")
    return set()

def save_state(sniped_set):
    logger.info("\n[GIT] State mutated. Saving sniped seat state to Git...")
    for attempt in range(3):
        quiet_git_pull()
        with open(STATE_FILE, "w") as f:
            json.dump(list(sniped_set), f, indent=2)
            
        subprocess.run(["git", "add", STATE_FILE], capture_output=True, check=False)
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        
        if STATE_FILE in status.stdout:
            subprocess.run(["git", "commit", "-m", "Update sniped seats state"], capture_output=True, check=False)
            if quiet_git_push(): 
                logger.info("[STATE] ✅ State successfully saved and pushed to remote.")
                return
            logger.warning(f"Git push failed on attempt {attempt+1}/3. Retrying...")
            time.sleep(2)
        else:
            return
    logger.error("❌ Failed to push state updates after 3 attempts.")

def trigger_ntfy(message, attach_url=None):
    logger.info(f"🔔 ALERTING VIA NTFY:\n{message}")
    headers = {"Priority": "urgent"}
    if attach_url:
        headers["Attach"] = attach_url
        
    for i in range(1):
        try:
            resp = requests.post(f"https://ntfy.sh/{TOPIC}", data=message.encode('utf-8'), headers=headers, timeout=10)
            if resp.status_code == 200:
                logger.info(f"✅ Ntfy ping sent! Status: {resp.status_code}")
        except Exception as e:
            logger.error(f"❌ Ntfy ping failed: {e}")

def toggle_warp():
    global USE_WARP, last_warp_toggle
    with warp_lock:
        # If another thread already restarted WARP in the last 10 seconds, do nothing and return
        if time.time() - last_warp_toggle < 10:
            return 
            
        logger.info("    -> 🔌 Restarting Cloudflare WARP to bypass WAF block...")
        subprocess.run(["warp-cli", "--accept-tos", "disconnect"], capture_output=True, check=False)
        time.sleep(2)
        subprocess.run(["warp-cli", "--accept-tos", "connect"], capture_output=True, check=False)
        time.sleep(5)
        
        USE_WARP = True
        last_warp_toggle = time.time()

def make_bms_request(method, url, max_retries=25, **kwargs):
    for attempt in range(1, max_retries + 1):
        current_proxies = PROXIES if USE_WARP else None
        try:
            if method.upper() == 'GET':
                resp = cffi_requests.get(url, proxies=current_proxies, impersonate="chrome", timeout=15, **kwargs)
            else:
                resp = cffi_requests.post(url, proxies=current_proxies, impersonate="chrome", timeout=15, **kwargs)
            
            if resp.status_code in [429, 403]:
                logger.warning(f"    -> 🚧 WAF Block (HTTP {resp.status_code}) on {method} request. (Attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    toggle_warp()
                    continue
            return resp
        except Exception as e:
            logger.error(f"🌐 Request error on {method} (Attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries: 
                time.sleep(3)
                continue
    return None

# =======================================================
# PHASE 1: SHOWTIME MONITORING (USING VENUE API)
# =======================================================
def find_target_session():
    try:
        # Convert strings like "12:00 PM" into datetime.time objects for mathematical comparison
        target_time_start_obj = datetime.strptime(TARGET_TIME_START, "%I:%M %p").time()
        target_time_end_obj = datetime.strptime(TARGET_TIME_END, "%I:%M %p").time()
    except Exception as e:
        logger.error(f"❌ Time parsing error in config (TARGET_TIME_START/END). Error: {e}")
        return []

    valid_shows = []
    
    for date_code in DATES:
        url = f"https://in.bookmyshow.com/api/v3/mobile/showtimes/byvenue?appCode=MOBAND2&venueCode={VENUE_CODE}&dateCode={date_code}"
        resp = make_bms_request('GET', url, headers=GET_HEADERS)
        if not resp or resp.status_code != 200: 
            continue
            
        fallback_logged = False
            
        try:
            data = resp.json()
            show_details_list = data.get("ShowDetails", [])
            
            for show_detail in show_details_list:
                for event in show_detail.get("Event", []):
                    # Check both parent and child level for the matching Movie Code
                    parent_code = event.get("EventCode", "")
                    for child in event.get("ChildEvents", []):
                        # Extract the event code dynamically (prioritize child code, fallback to parent)
                        current_event_code = child.get("EventCode", parent_code)
                        
                        for show in child.get("ShowTimes", []):
                            # --- NEW: Constraint Check 0: Strict Date Verification ---
                            s_date_code = show.get("ShowDateCode", "")
                            if s_date_code != date_code:
                                if not fallback_logged:
                                    logger.info(f"    -> ⚠️ API fallback detected! Requested {date_code} but received {s_date_code}. Ignoring fallback shows.")
                                    fallback_logged = True
                                continue
                            
                            s_attr = show.get("Attributes", "")
                            
                            # 1. Constraint Check: Screen Attribute
                            if TARGET_ATTRIBUTE.lower() not in s_attr.lower():
                                continue
                                
                            s_time_str = show.get("ShowTime", "")
                            try:
                                s_time_obj = datetime.strptime(s_time_str, "%I:%M %p").time()
                            except:
                                continue
                            
                            # 2. Constraint Check: Time Range
                            if target_time_start_obj <= s_time_obj <= target_time_end_obj:
                                valid_shows.append({
                                    "sessionId": show.get("SessionId"),
                                    "eventCode": current_event_code,
                                    "dateCode": show.get("ShowDateCode"),
                                    "time": s_time_str,
                                    "attribute": s_attr,
                                    "datetime_obj": s_time_obj,
                                    "screen": show.get("ScreenName", "Unknown")
                                })
        except Exception as e:
            logger.error(f"    -> ❌ Error parsing venue JSON for {date_code}: {e}")
            pass
            
    if valid_shows:
        valid_shows.sort(key=lambda x: x["datetime_obj"])
        return valid_shows[:5]  # <-- Returns up to 5 shows
        
    return []

# =======================================================
# PHASE 2 & 3: THREAD WORKER / LAYOUT PARSING
# =======================================================
def fetch_seat_layout(session_id):
    url = "https://services-in.bookmyshow.com/doTrans.aspx"
    payload = f"strParam4=&strParam5=Y&strParam6=&strParam7=N&strParam1={session_id}&strParam2=WEB&strParam3=&strVenueCode={VENUE_CODE}&lngTransactionIdentifier=0&strAppCode=MOBAND2&strFormat=json&strCommand=GETSEATLAYOUT"
    resp = make_bms_request('POST', url, headers=POST_HEADERS, data=payload)
    if not resp or resp.status_code != 200: 
        return ""
    try: 
        return resp.json().get("BookMyShow", {}).get("strData", "")
    except Exception: 
        return ""

def parse_layout(str_data):
    if not str_data: return {}, {}, {}, 0
    parts = str_data.split("||")
    header_data = parts[0]
    
    categories = {}
    for cat in header_data.split("|"):
        c_parts = cat.split(":")
        if len(c_parts) >= 4:
            categories[c_parts[1]] = {"cat_code": c_parts[2], "area_id": c_parts[3]}
            
    rows_data = parts[1] if len(parts) > 1 else parts[0]
    rows = rows_data.split("|")
    
    available_seats_by_row = {}
    seat_metadata = {} 
    
    for row in rows:
        if not row or ":" not in row: continue
        elements = row.split(":")
        row_index = elements[0]
        row_letter = elements[1]
        seats = elements[2:]
        
        available_in_row = []
        for seat in seats:
            match = re.search(r"([A-Z])[14](\d+)\+(\d+)", seat)
            if match:
                block_code = match.group(1)
                backend_seat = match.group(2)
                seat_num = match.group(3)
                
                available_in_row.append(seat_num)
                seat_metadata[f"{row_letter}_{seat_num}"] = {
                    "block_code": block_code,
                    "row_index": row_index,
                    "backend_seat": backend_seat
                }
                
        if available_in_row:
            available_seats_by_row[row_letter] = available_in_row

    total_available_seats = sum(len(seats) for seats in available_seats_by_row.values())
    return available_seats_by_row, categories, seat_metadata, total_available_seats

def monitor_and_snipe_worker(session, sniped_memory, state_lock, start_time):
    s_id = session["sessionId"]
    logger.info(f"    -> [THREAD] 🚀 Started monitoring Session {s_id} ({session['time']})")
    
    # Calculate the total number of target seats for a single session
    total_desired_seats = sum(len(seats) for seats in DESIRED_SEATS.values())
    
    while (time.time() - start_time) < MAX_RUNTIME_SECONDS:
        # --- NEW: EARLY EXIT CHECK ---
        sniped_count = 0
        with state_lock:
            for target_row, target_seat_list in DESIRED_SEATS.items():
                for target_seat in target_seat_list:
                    if f"{s_id}_{target_row}_{target_seat}" in sniped_memory:
                        sniped_count += 1
                        
        if sniped_count >= total_desired_seats:
            logger.info(f"    -> [THREAD] 🎯 All target seats sniped for Session {s_id}! Thread exiting.")
            break
        # -----------------------------

        str_data = fetch_seat_layout(s_id)
        if not str_data:
            time.sleep(1)
            continue
            
        current_seats, categories_map, seat_metadata, total_available = parse_layout(str_data)
        
        for target_row, target_seat_list in DESIRED_SEATS.items():
            if target_row not in current_seats:
                continue
                
            available_in_row = current_seats[target_row]
            
            for target_seat in target_seat_list:
                seat_memory_key = f"{s_id}_{target_row}_{target_seat}"
                
                with state_lock:
                    already_sniped = seat_memory_key in sniped_memory
                
                if target_seat in available_in_row and not already_sniped:
                    meta = seat_metadata.get(f"{target_row}_{target_seat}")
                    success = execute_snipe(session, target_row, target_seat, meta, categories_map)
                    
                    if success:
                        with state_lock:
                            sniped_memory.add(seat_memory_key)
                        logger.info(f"✅ Successfully sniped and memorized: {seat_memory_key}")
                        time.sleep(1)
                        
        time.sleep(1)

# =======================================================
# PHASE 3 (cont): AUTO-LOCK / PAYMENT SNIPER 
# =======================================================
def lock_seat(session_id, row_index, backend_seat, cat_code, area_id, ticket_category, event_code):
    logger.info(f"    -> 🔒 [SNIPER] Request 1: Attempting to lock internal Row {row_index} Seat {backend_seat} ({cat_code})...")
    url = "https://in.bookmyshow.com/api/v2/mobile/booking/movies"
    
    payload = {
        "appCode": "MOBAND2",
        "venueCode": VENUE_CODE,
        "sessionId": str(session_id),
        "ticketCategory": ticket_category, 
        "numberOfTickets": "1",
        "selectedSeats": f"|1|{cat_code}|{area_id}|{row_index}|{backend_seat}|",
        "email": EMAIL,
        "eventCode": event_code,
        "version": "18234",
        "platform": "ANDROID",
        "phone": PHONE,
        "bmsId": "1.42419972.1785913202920",
        "seatLayoutType": "Y",
        "offerData": {}
    }

    headers = {
        "Host": "in.bookmyshow.com",
        "X-Latitude": "17.385044",
        "X-Subregion-Code": "HYD",
        "X-App-Code": "MOBAND2",
        "X-Phone": PHONE,
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 10; Android SDK built for x86_64 Build/QSR1.211112.011)",
        "X-Longitude": "78.48667",
        "X-Platform": "AND",
        "X-Region-Code": "HYD",
        "X-Platform-Code": "ANDROID",
        "X-Email": EMAIL,
        "Content-Type": "text/plain; charset=utf-8",
        "Accept-Encoding": "gzip, deflate, br"
    }

    data_str = json.dumps(payload, separators=(',', ':'))
    resp = make_bms_request('POST', url, headers=headers, data=data_str.encode('utf-8'))
    
    if resp and resp.status_code == 200:
        try:
            r_json = resp.json()
            t_id = r_json.get("transactionId")
            t_uid = r_json.get("transactionUID")
            if t_id and t_uid:
                logger.info(f"    -> 🟢 [SNIPER] SUCCESS! Seat locked! TransID: {t_id}")
                return t_id, t_uid
        except Exception as e:
            logger.error(f"    -> ❌ [SNIPER] Error parsing Request 1: {e}")
    
    logger.error("    -> 🔴 [SNIPER] FAILED to lock seat.")
    return None, None

def initiate_payment(trans_id, trans_uid):
    logger.info(f"    -> 💸 [SNIPER] Request 2: Initiating payment intent for {trans_id}...")
    url = "https://services-in.bookmyshow.com/doTrans.aspx"
    
    rand_hex = "".join(random.choices("0123456789abcdef", k=7))
    boundary = f"----geckoformboundary4549d0c459b45033a86405c7a{rand_hex}"

    headers = {
        "Host": "services-in.bookmyshow.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Referer": "https://in.bookmyshow.com/",
        "X-Region-Code": "HYD",
        "X-Region-Slug": "hyderabad",
        "X-Latitude": "17.385044",
        "X-Longitude": "78.486671",
        "X-Deemed-Email": EMAIL,
        "X-Deemed-Mobile": PHONE,
        "X-Phone": PHONE,
        "X-Mobile": PHONE,
        "X-Email": EMAIL,
        "X-Transaction-Uid": trans_uid,
        "X-App-Code": "WEB",
        "X-Platform-Code": "WEB",
        "X-Platform": "WEB",
        "Origin": "https://in.bookmyshow.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site"
    }

    form_fields = [
        f"--{boundary}", 'Content-Disposition: form-data; name="strAppCode"', '', 'WEB',
        f"--{boundary}", 'Content-Disposition: form-data; name="lngTransactionIdentifier"', '', trans_id,
        f"--{boundary}", 'Content-Disposition: form-data; name="strCommand"', '', 'SETPAYMENT',
        f"--{boundary}", 'Content-Disposition: form-data; name="strVenueCode"', '', VENUE_CODE,
        f"--{boundary}", 'Content-Disposition: form-data; name="strParam1"', '', "'|TYPE=UPI|UPITYPE=QRCODE|IMAGEURL=''|PROCESSTYPE=REQUEST|LSID=|MEMBERID=|CLIENTID=movies|",
        f"--{boundary}", 'Content-Disposition: form-data; name="strParam2"', '', '|ETICKET=Y|MTICKET=Y|',
        f"--{boundary}", 'Content-Disposition: form-data; name="strParam3"', '', EMAIL,
        f"--{boundary}", 'Content-Disposition: form-data; name="strParam4"', '', PHONE,
        f"--{boundary}", 'Content-Disposition: form-data; name="strFormat"', '', 'json',
        f"--{boundary}--", ''
    ]
    
    payload_str = '\r\n'.join(form_fields)
    resp = make_bms_request('POST', url, headers=headers, data=payload_str.encode('utf-8'))
    
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            bms = data.get("BookMyShow", {})
            if bms.get("blnSuccess") == "true":
                str_data = bms.get("strData", [])
                if len(str_data) > 0:
                    upi_url = str_data[0].get("BMSUPIQRPAYURL")
                    logger.info(f"    -> 🟢 [SNIPER] SUCCESS! Payment Intent Generated!")
                    return upi_url
        except Exception as e:
            logger.error(f"    -> ❌ [SNIPER] Error parsing Request 2: {e}")
            
    logger.error("    -> 🔴 [SNIPER] FAILED to generate payment intent.")
    return None

def execute_snipe(session, row, seat_num, meta, categories):
    cat_info = categories.get(meta["block_code"])
    if not cat_info: return False
    
    c_code, a_id = cat_info["cat_code"], cat_info["area_id"]
    
    ticket_category = ROW_CATEGORY_MAP.get(row, "0008")
    
    logger.info(f"    -> 🎯 [SNIPER] MATCH FOUND! Auto-locking Row {row}, Seat {seat_num} (Internal Cat: {c_code}, Area: {a_id}, Ticket Cat: {ticket_category})")
    
    # 1. Lock 
    t_id, t_uid = lock_seat(session["sessionId"], meta["row_index"], meta["backend_seat"], c_code, a_id, ticket_category, session["eventCode"])
    if not t_id: return False
    
    # 2. Pay
    upi_intent = initiate_payment(t_id, t_uid)
    if not upi_intent: return False
    
    # 3. Format Notification & Trigger Push
    qr_image_url = f"https://in.bookmyshow.com/secure/barcode/?IsImage=Y&strBarcodeType=qrcode&strBarcodeTxt={upi_intent}&intHeight=300&intWidth=300"
    hum_date = humanize_date(session["dateCode"])
    msg = f"Row {row} Seat {seat_num} is locked and awaiting payment.\n\n{VENUE_CODE} {session['eventCode']} {hum_date} {session['time']} {session['attribute']}"
    
    trigger_ntfy(msg, attach_url=qr_image_url)
    return True

# =======================================================
# MAIN LOOP STATE MACHINE
# =======================================================
def gha_sigterm_handler(signum, frame):
    logger.warning("\n⚠️ SIGTERM received (GitHub Actions cancellation). Forcing early shutdown...")
    sys.exit(0) # Triggers the 'finally' block

def main():
    start_time = time.time()
    signal.signal(signal.SIGTERM, gha_sigterm_handler)
    
    logger.info("==================================================")
    logger.info("🚀 STARTING TARGETED SEAT SNIPER (MULTI-THREADED)")
    logger.info("==================================================\n")

    logger.info("[GIT] Loading initial state from GitHub repository...")
    sniped_seats_memory = load_state()
    state_lock = threading.Lock()
    logger.info(f"[STATE] Loaded existing state for {len(sniped_seats_memory)} previously sniped seats.\n")

    # --- PHASE 1: Wait for Showtimes to list ---
    target_sessions = []
    logger.info("    -> [PHASE 1] Scanning Venue API for target showtimes...")
    
    while (time.time() - start_time) < MAX_RUNTIME_SECONDS:
        target_sessions = find_target_session()
        
        if not target_sessions:
            logger.info("    -> ⏳ No matching showtimes exist yet. Sleeping 23 seconds...")
            time.sleep(23)
        else:
            logger.info(f"\n    -> 🎉 MATCH FOUND! Detected {len(target_sessions)} matching shows.")
            break

    if not target_sessions:
        logger.info("🏁 Max runtime reached before shows were listed. Shutting down.")
        return

    # --- PROXY WARMUP: Activate WARP exactly before threading ---
    global USE_WARP
    if not USE_WARP:
        logger.info("    -> 🛡️ Pre-warming WARP Proxy before spawning threads...")
        with warp_lock:
            subprocess.run(["warp-cli", "--accept-tos", "connect"], capture_output=True, check=False)
            time.sleep(5)
            USE_WARP = True
            global last_warp_toggle
            last_warp_toggle = time.time()
        
    # --- PHASE 2 & 3: Parallel Threading ---
    logger.info(f"    -> 🚀 Spawning {len(target_sessions)} parallel threads...")
    threads = []
    for session in target_sessions:
        t = threading.Thread(
            target=monitor_and_snipe_worker, 
            args=(session, sniped_seats_memory, state_lock, start_time)
        )
        t.daemon = True
        t.start()
        threads.append(t)

    # Block main thread, waiting for all threads to finish their assigned tasks
    try:
        for t in threads:
            while t.is_alive():
                t.join(1) # Check thread status every 1 second
    finally:
        # --- PHASE 4: Deferred Cleanup & Git Commit ---
        logger.info("\n🏁 Executing deferred state commit to Git...")
        save_state(sniped_seats_memory)
        logger.info("🏁 Script shutting down gracefully.")
        
if __name__ == "__main__":
    main()
