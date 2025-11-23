from flask import Flask, render_template, jsonify, request, redirect, url_for, Response
import requests, time, hmac, hashlib, json, sqlite3, os, threading, csv, io
from datetime import datetime, timedelta

app = Flask(__name__)
start_time = time.time()

# ------------- Config -------------
BASE_URL = 'https://openapi.tuyain.com'
is_on_render = 'RENDER' in os.environ
DB_PATH = '/var/data/energy_monitor.db' if is_on_render else 'energy_monitor.db'

# ------------- DB init -------------
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    
    # Devices table
    conn.execute('''CREATE TABLE IF NOT EXISTS devices
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     device_id TEXT UNIQUE NOT NULL,
                     device_name TEXT NOT NULL,
                     access_id TEXT NOT NULL,
                     access_secret TEXT NOT NULL,
                     created_at INTEGER)''')
    
    # Readings table with device_id reference
    conn.execute('''CREATE TABLE IF NOT EXISTS readings
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     device_id TEXT NOT NULL,
                     ts INTEGER NOT NULL,
                     voltage REAL,
                     current REAL,
                     power REAL,
                     FOREIGN KEY (device_id) REFERENCES devices(device_id),
                     UNIQUE(device_id, ts))''')
    
    # Schedules table for device automation
    conn.execute('''CREATE TABLE IF NOT EXISTS schedules
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     device_id TEXT NOT NULL,
                     schedule_name TEXT NOT NULL,
                     start_time TEXT NOT NULL,
                     end_time TEXT NOT NULL,
                     days TEXT NOT NULL,
                     enabled INTEGER DEFAULT 1,
                     created_at INTEGER,
                     FOREIGN KEY (device_id) REFERENCES devices(device_id))''')
    
    conn.commit()
    
    # Verify tables were created
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print(f"[DB] Database initialized. Tables: {[t[0] for t in tables]}")
    
    conn.close()

init_db()

# ------------- Tuya Helpers -------------
def sign(method, path, access_id, access_secret, body='', token=''):
    t = str(int(time.time() * 1000))
    msg = access_id + (token or '') + t + f"{method}\n{hashlib.sha256(body.encode()).hexdigest()}\n\n{path}"
    return t, hmac.new(access_secret.encode(), msg.encode(), hashlib.sha256).hexdigest().upper()

def get_token(access_id, access_secret):
    try:
        t, sig = sign('GET', '/v1.0/token?grant_type=1', access_id, access_secret)
        r = requests.get(
            BASE_URL + '/v1.0/token?grant_type=1',
            headers={'client_id': access_id, 'sign': sig, 't': t, 'sign_method': 'HMAC-SHA256'},
            timeout=10
        )
        r.raise_for_status()
        result = r.json()
        
        # Check if result contains the token
        if 'result' in result and 'access_token' in result['result']:
            return result['result']['access_token']
        else:
            print(f"Token API response missing result: {result}")
            return None
    except Exception as e:
        print(f"Error getting token: {e}")
        return None

def get_device_info(device_id, access_id, access_secret):
    """Get device info including online status"""
    try:
        token = get_token(access_id, access_secret)
        if not token:
            return None
            
        t, sig = sign('GET', f'/v1.0/devices/{device_id}', access_id, access_secret, '', token)
        headers = {
            'client_id': access_id,
            'access_token': token,
            'sign': sig,
            't': t,
            'sign_method': 'HMAC-SHA256'
        }
        r = requests.get(BASE_URL + f'/v1.0/devices/{device_id}', headers=headers, timeout=10)
        r.raise_for_status()
        
        result = r.json()
        
        # Check if result exists
        if 'result' in result:
            return result['result']
        else:
            print(f"Device info API response missing result for {device_id}: {result}")
            return None
            
    except Exception as e:
        print(f"Error getting device info for {device_id}: {e}")
        return None

def get_device_data(device_id, access_id, access_secret):
    # First check if device is online
    device_info = get_device_info(device_id, access_id, access_secret)
    
    # If device_info is None or doesn't have online status, assume offline
    if not device_info:
        print(f"[ERROR] Could not get device info for {device_id}")
        return 0, 0, 0, False, False
    
    is_online = device_info.get('online', False)
    
    # If device is offline, return zeros and False
    if not is_online:
        print(f"[INFO] Device {device_id} is OFFLINE")
        return 0, 0, 0, False, False
    
    # Device is online, get status
    try:
        token = get_token(access_id, access_secret)
        if not token:
            return 0, 0, 0, False, False
            
        t, sig = sign('GET', f'/v1.0/devices/{device_id}/status', access_id, access_secret, '', token)
        headers = {
            'client_id': access_id,
            'access_token': token,
            'sign': sig,
            't': t,
            'sign_method': 'HMAC-SHA256'
        }
        r = requests.get(BASE_URL + f'/v1.0/devices/{device_id}/status', headers=headers, timeout=10)
        r.raise_for_status()
        
        response_data = r.json()
        
        # Check if result exists
        if 'result' not in response_data:
            print(f"Status API response missing result for {device_id}: {response_data}")
            return 0, 0, 0, False, False
        
        data = response_data['result']
        
        switch = next((d['value'] for d in data if d['code'] == 'switch_1'), False)
        voltage = next((d['value'] / 10 for d in data if d['code'] == 'cur_voltage'), 0)
        current = next((d['value'] / 1000 for d in data if d['code'] == 'cur_current'), 0)
        power = next((d['value'] / 10 for d in data if d['code'] == 'cur_power'), 0)
        
        return voltage, current, power, switch, is_online
        
    except Exception as e:
        print(f"Error getting device status for {device_id}: {e}")
        return 0, 0, 0, False, False

# ------------- Background Data Collector -------------
# Global variable to track last execution time for each schedule
schedule_execution_tracker = {}

def check_and_execute_schedules():
    """Check schedules and execute device control"""
    from datetime import datetime
    
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        
        # Get current time
        now = datetime.now()
        current_time = now.strftime('%H:%M')
        current_day = now.strftime('%A').lower()
        
        print(f"[SCHEDULE] Checking schedules at {current_time} on {current_day}")
        
        # Get all enabled schedules
        schedules = conn.execute('''
            SELECT s.id, s.device_id, s.schedule_name, s.start_time, s.end_time, s.days, d.access_id, d.access_secret, d.device_name
            FROM schedules s
            JOIN devices d ON s.device_id = d.device_id
            WHERE s.enabled = 1
        ''').fetchall()
        
        conn.close()
        
        if not schedules:
            return
        
        print(f"[SCHEDULE] Found {len(schedules)} enabled schedule(s)")
        
        for schedule_id, device_id, schedule_name, start_time, end_time, days, access_id, access_secret, device_name in schedules:
            # Parse days (stored as comma-separated: "monday,tuesday,wednesday")
            schedule_days = [day.strip().lower() for day in days.split(',')]
            
            # Check if today is a scheduled day
            if current_day not in schedule_days:
                continue
            
            print(f"[SCHEDULE] '{schedule_name}' for {device_name}: ON={start_time}, OFF={end_time}")
            
            # Create unique tracker keys for ON and OFF
            tracker_key_on = f"{schedule_id}_ON_{current_day}"
            tracker_key_off = f"{schedule_id}_OFF_{current_day}"
            
            # Check if we should turn ON (at start_time)
            if current_time == start_time:
                # Check if we already executed this today
                if tracker_key_on in schedule_execution_tracker:
                    last_exec = schedule_execution_tracker[tracker_key_on]
                    if last_exec == current_time:
                        continue  # Already executed this minute
                
                print(f"[SCHEDULE] ⚡ EXECUTING: Turning ON '{device_name}' (Schedule: {schedule_name})")
                try:
                    # Turn device ON
                    token = get_token(access_id, access_secret)
                    if token:
                        body = json.dumps({"commands": [{"code": "switch_1", "value": True}]})
                        t, sig = sign('POST', f'/v1.0/devices/{device_id}/commands', access_id, access_secret, body, token)
                        headers = {
                            'client_id': access_id,
                            'access_token': token,
                            'sign': sig,
                            't': t,
                            'sign_method': 'HMAC-SHA256',
                            'Content-Type': 'application/json'
                        }
                        response = requests.post(BASE_URL + f'/v1.0/devices/{device_id}/commands', headers=headers, data=body, timeout=10)
                        
                        if response.status_code == 200:
                            print(f"[SCHEDULE] ✅ SUCCESS: Turned ON '{device_name}'")
                            schedule_execution_tracker[tracker_key_on] = current_time
                        else:
                            print(f"[SCHEDULE] ❌ FAILED: {response.text}")
                    else:
                        print(f"[SCHEDULE] ❌ FAILED: Could not get API token")
                except Exception as e:
                    print(f"[SCHEDULE] ❌ ERROR turning ON '{device_name}': {e}")
            
            # Check if we should turn OFF (at end_time)
            elif current_time == end_time:
                # Check if we already executed this today
                if tracker_key_off in schedule_execution_tracker:
                    last_exec = schedule_execution_tracker[tracker_key_off]
                    if last_exec == current_time:
                        continue  # Already executed this minute
                
                print(f"[SCHEDULE] ⚡ EXECUTING: Turning OFF '{device_name}' (Schedule: {schedule_name})")
                try:
                    # Turn device OFF
                    token = get_token(access_id, access_secret)
                    if token:
                        body = json.dumps({"commands": [{"code": "switch_1", "value": False}]})
                        t, sig = sign('POST', f'/v1.0/devices/{device_id}/commands', access_id, access_secret, body, token)
                        headers = {
                            'client_id': access_id,
                            'access_token': token,
                            'sign': sig,
                            't': t,
                            'sign_method': 'HMAC-SHA256',
                            'Content-Type': 'application/json'
                        }
                        response = requests.post(BASE_URL + f'/v1.0/devices/{device_id}/commands', headers=headers, data=body, timeout=10)
                        
                        if response.status_code == 200:
                            print(f"[SCHEDULE] ✅ SUCCESS: Turned OFF '{device_name}'")
                            schedule_execution_tracker[tracker_key_off] = current_time
                        else:
                            print(f"[SCHEDULE] ❌ FAILED: {response.text}")
                    else:
                        print(f"[SCHEDULE] ❌ FAILED: Could not get API token")
                except Exception as e:
                    print(f"[SCHEDULE] ❌ ERROR turning OFF '{device_name}': {e}")
    
    except Exception as e:
        print(f"[SCHEDULE] ❌ CRITICAL ERROR in schedule checker: {e}")
        import traceback
        traceback.print_exc()

def collect_data_periodically():
    print("=" * 60)
    print("🚀 Starting background data collection and schedule checker...")
    print("=" * 60)
    
    last_schedule_check_minute = -1  # Track last minute we checked schedules
    
    while True:
        try:
            from datetime import datetime
            now = datetime.now()
            current_minute = now.minute
            
            # Check schedules every minute (only once per minute)
            if current_minute != last_schedule_check_minute:
                check_and_execute_schedules()
                last_schedule_check_minute = current_minute
            
            conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            
            # Fetch devices inside the try block to get fresh list each time
            try:
                devices = conn.execute('SELECT device_id, access_id, access_secret FROM devices').fetchall()
            except Exception as e:
                print(f"Error fetching devices list: {e}")
                conn.close()
                time.sleep(60)  # Increased to 60 seconds to reduce API calls
                continue
            
            for device_id, access_id, access_secret in devices:
                try:
                    # Check if device still exists before processing
                    device_exists = conn.execute(
                        'SELECT 1 FROM devices WHERE device_id = ?', 
                        (device_id,)
                    ).fetchone()
                    
                    if not device_exists:
                        print(f"[BG] Device {device_id} no longer exists, skipping")
                        continue
                    
                    voltage, current, power, switch, is_online = get_device_data(device_id, access_id, access_secret)
                    
                    # Log data for all online devices, regardless of switch state
                    if is_online:
                        ts = int(time.time())
                        
                        # Double-check device still exists before inserting
                        if conn.execute('SELECT 1 FROM devices WHERE device_id = ?', (device_id,)).fetchone():
                            conn.execute(
                                'INSERT OR IGNORE INTO readings (device_id, ts, voltage, current, power) VALUES (?,?,?,?,?)',
                                (device_id, ts, voltage, current, power)
                            )
                            conn.commit()
                            
                            if switch and power > 0:
                                print(f"[BG] Device {device_id}: {power}W, {voltage}V, {current}A (ON)")
                            else:
                                print(f"[BG] Device {device_id}: {voltage}V (OFF - no power draw)")
                        else:
                            print(f"[BG] Device {device_id} was deleted, skipping insert")
                    else:
                        print(f"[BG] Device {device_id}: OFFLINE")
                        
                except sqlite3.IntegrityError as e:
                    print(f"Database integrity error for device {device_id}: {e} (device may have been deleted)")
                except Exception as e:
                    print(f"Error collecting data for device {device_id}:", e)
            
            conn.close()
            
        except sqlite3.OperationalError as e:
            print(f"Database operational error in background collector: {e}")
        except Exception as e:
            print("Error in background collector:", e)
        
        time.sleep(15)

# ------------- Bangladesh Billing Helper -------------
def calc_bill_bd(kwh: float) -> float:
    if kwh <= 50:
        return kwh * 3.75
    elif kwh <= 75:
        return (50 * 3.75) + (kwh - 50) * 4.19
    elif kwh <= 200:
        return (50 * 3.75) + (25 * 4.19) + (kwh - 75) * 5.72
    elif kwh <= 300:
        return (50 * 3.75) + (25 * 4.19) + (125 * 5.72) + (kwh - 200) * 6.00
    elif kwh <= 400:
        return (50 * 3.75) + (25 * 4.19) + (125 * 5.72) + (100 * 6.00) + (kwh - 300) * 6.34
    else:
        return (50 * 3.75) + (25 * 4.19) + (125 * 5.72) + (100 * 6.00) + (100 * 6.34) + (kwh - 400) * 9.94

# ------------- Device Management APIs -------------
@app.route('/')
def home():
    return render_template('home.html')
# (the manual route)
@app.route('/manual')
def user_manual():
    return render_template('manual.html')

@app.route('/api/devices', methods=['GET'])
def get_devices():
    conn = sqlite3.connect(DB_PATH)
    devices = conn.execute('SELECT id, device_id, device_name, created_at FROM devices ORDER BY id').fetchall()
    conn.close()
    
    result = []
    for idx, (id, device_id, device_name, created_at) in enumerate(devices, 1):
        result.append({
            'serial': idx,
            'id': id,
            'device_id': device_id,
            'device_name': device_name,
            'created_at': created_at
        })
    
    return jsonify(result)

@app.route('/api/devices/status', methods=['GET'])
def get_all_devices_status():
    """Get real-time status for all devices"""
    conn = sqlite3.connect(DB_PATH)
    
    try:
        devices = conn.execute('SELECT device_id, access_id, access_secret FROM devices').fetchall()
    except Exception as e:
        print(f"Error fetching devices: {e}")
        conn.close()
        return jsonify({})
    
    conn.close()
    
    status_map = {}
    for device_id, access_id, access_secret in devices:
        try:
            voltage, current, power, switch, is_online = get_device_data(device_id, access_id, access_secret)
            
            if not is_online:
                status_map[device_id] = 'disconnected'
            elif switch:
                status_map[device_id] = 'on'
            else:
                status_map[device_id] = 'off'
                
        except Exception as e:
            print(f"Error getting status for {device_id}:", e)
            status_map[device_id] = 'disconnected'
    
    return jsonify(status_map)

@app.route('/api/devices', methods=['POST'])
def add_device():
    data = request.json
    device_id = data.get('device_id')
    device_name = data.get('device_name')
    access_id = data.get('access_id')
    access_secret = data.get('access_secret')
    
    if not all([device_id, device_name, access_id, access_secret]):
        return jsonify({'error': 'All fields are required'}), 400
    
    try:
        # Test connection
        token = get_token(access_id, access_secret)
        if not token:
            return jsonify({'error': 'Invalid credentials or API connection failed'}), 400
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            'INSERT INTO devices (device_id, device_name, access_id, access_secret, created_at) VALUES (?,?,?,?,?)',
            (device_id, device_name, access_id, access_secret, int(time.time()))
        )
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Device added successfully'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Device already exists'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/devices/<device_id>', methods=['DELETE'])
def delete_device(device_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        
        # Check if device exists
        device = conn.execute('SELECT device_name FROM devices WHERE device_id = ?', (device_id,)).fetchone()
        
        if not device:
            conn.close()
            return jsonify({'error': 'Device not found'}), 404
        
        # Delete readings first (foreign key constraint)
        conn.execute('DELETE FROM readings WHERE device_id = ?', (device_id,))
        
        # Delete device
        conn.execute('DELETE FROM devices WHERE device_id = ?', (device_id,))
        
        conn.commit()
        conn.close()
        
        print(f"[DELETE] Device {device_id} deleted successfully")
        
        return jsonify({'success': True, 'message': 'Device deleted successfully'})
    except Exception as e:
        print(f"[DELETE ERROR] Failed to delete device {device_id}:", e)
        return jsonify({'error': str(e)}), 500

# ------------- Device-Specific Dashboard APIs -------------
@app.route('/dashboard/<device_id>')
def device_dashboard(device_id):
    conn = sqlite3.connect(DB_PATH)
    device = conn.execute('SELECT device_name FROM devices WHERE device_id = ?', (device_id,)).fetchone()
    conn.close()
    
    if not device:
        return "Device not found", 404
    
    return render_template('dashboard.html', device_id=device_id, device_name=device[0])

@app.route('/api/device/<device_id>/live')
def api_device_live(device_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        device = conn.execute('SELECT access_id, access_secret FROM devices WHERE device_id = ?', (device_id,)).fetchone()
        conn.close()
        
        if not device:
            return jsonify({'error': 'Device not found'}), 404
        
        access_id, access_secret = device
        voltage, current, power, switch, is_online = get_device_data(device_id, access_id, access_secret)
        
        return jsonify({
            'switch': switch,
            'power': power,
            'voltage': voltage,
            'current': current,
            'is_online': is_online
        })
    except Exception as e:
        print(f"Error in live API: {e}")
        return jsonify({'error': str(e), 'is_online': False}), 500

@app.route('/api/device/<device_id>/summary')
def api_device_summary(device_id):
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = int(today_start.timestamp())
    
    conn = sqlite3.connect(DB_PATH)
    
    energy_ws = conn.execute(
        "SELECT SUM(power) * 15 FROM readings WHERE device_id = ? AND ts >= ?",
        (device_id, start_ts)
    ).fetchone()[0] or 0
    energy_kwh = energy_ws / 3600 / 1000
    
    runtime_seconds = conn.execute(
        "SELECT COUNT(*) * 15 FROM readings WHERE device_id = ? AND ts >= ? AND power > 10",
        (device_id, start_ts)
    ).fetchone()[0] or 0
    
    conn.close()
    
    return jsonify({
        'today_energy_kwh': round(energy_kwh, 2),
        'daily_runtime_seconds': runtime_seconds
    })

@app.route('/api/device/<device_id>/history_range_hourly')
def api_device_history_range_hourly(device_id):
    start_date_str = request.args.get('start')
    end_date_str = request.args.get('end')
    
    try:
        start_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
        end_dt = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid date format"}), 400
    
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''
        SELECT 
            date(datetime(ts, 'unixepoch')) AS day,
            strftime('%H', datetime(ts, 'unixepoch')) AS hour,
            AVG(power) AS avg_power
        FROM readings
        WHERE device_id = ? AND ts >= ? AND ts < ?
        GROUP BY day, hour
        ORDER BY day, hour
    ''', (device_id, int(start_dt.timestamp()), int(end_dt.timestamp()))).fetchall()
    conn.close()
    
    grouped = {}
    for day, hour_str, avg_power in rows:
        if day not in grouped:
            grouped[day] = []
        grouped[day].append({'hour': int(hour_str), 'avg_power': avg_power})
    
    return jsonify(grouped)

@app.route('/api/device/<device_id>/history_range_daily')
def api_device_history_range_daily(device_id):
    start_date_str = request.args.get('start')
    end_date_str = request.args.get('end')
    
    try:
        start_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
        end_dt = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid date format"}), 400
    
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''
        SELECT 
            date(datetime(ts, 'unixepoch')) AS day,
            SUM(power) * 15.0 / 3600.0 / 1000.0 AS kwh
        FROM readings
        WHERE device_id = ? AND ts >= ? AND ts < ?
        GROUP BY day
        ORDER BY day
    ''', (device_id, int(start_dt.timestamp()), int(end_dt.timestamp()))).fetchall()
    conn.close()
    
    result = [{'date': day, 'kwh': kwh or 0.0} for day, kwh in rows]
    return jsonify(result)

@app.route('/api/device/<device_id>/monthly_bill')
def api_device_monthly_bill(device_id):
    month_str = request.args.get('month')
    
    if month_str:
        try:
            year, month = map(int, month_str.split('-'))
            month_start = datetime(year, month, 1)
        except Exception:
            return jsonify({"error": "Invalid month format"}), 400
    else:
        now = datetime.now()
        month_start = datetime(now.year, now.month, 1)
    
    if month_start.month == 12:
        next_month = datetime(month_start.year + 1, 1, 1)
    else:
        next_month = datetime(month_start.year, month_start.month + 1, 1)
    
    conn = sqlite3.connect(DB_PATH)
    energy_ws = conn.execute(
        "SELECT SUM(power) * 15 FROM readings WHERE device_id = ? AND ts >= ? AND ts < ?",
        (device_id, int(month_start.timestamp()), int(next_month.timestamp()))
    ).fetchone()[0] or 0
    conn.close()
    
    total_kwh = energy_ws / 3600 / 1000
    total_bill = calc_bill_bd(total_kwh)
    
    return jsonify({
        'month': month_start.strftime('%Y-%m'),
        'total_kwh': round(total_kwh, 2),
        'total_bill': round(total_bill, 2)
    })

@app.route('/api/device/<device_id>/switch', methods=['POST'])
def device_switch(device_id):
    on = request.json.get('on', False)
    
    try:
        conn = sqlite3.connect(DB_PATH)
        device = conn.execute('SELECT access_id, access_secret FROM devices WHERE device_id = ?', (device_id,)).fetchone()
        conn.close()
        
        if not device:
            return jsonify({'error': 'Device not found'}), 404
        
        access_id, access_secret = device
        
        # Check if device is online first
        try:
            device_info = get_device_info(device_id, access_id, access_secret)
            
            if not device_info:
                return jsonify({'success': False, 'error': 'Cannot reach device'}), 400
                
            is_online = device_info.get('online', False)
            
            if not is_online:
                return jsonify({'success': False, 'error': 'Device is offline'}), 400
        except:
            return jsonify({'success': False, 'error': 'Cannot reach device'}), 400
        
        token = get_token(access_id, access_secret)
        if not token:
            return jsonify({'success': False, 'error': 'Authentication failed'}), 400
        
        body = json.dumps({"commands": [{"code": "switch_1", "value": on}]})
        t, sig = sign('POST', f'/v1.0/devices/{device_id}/commands', access_id, access_secret, body, token)
        
        headers = {
            'client_id': access_id,
            'access_token': token,
            'sign': sig,
            't': t,
            'sign_method': 'HMAC-SHA256',
            'Content-Type': 'application/json'
        }
        
        r = requests.post(BASE_URL + f'/v1.0/devices/{device_id}/commands', headers=headers, data=body, timeout=10)
        r.raise_for_status()
        
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error in switch API: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/system')
def api_system():
    uptime_seconds = time.time() - start_time
    return jsonify({'uptime_seconds': uptime_seconds})

@app.route('/api/device/<device_id>/export/csv')
def export_device_csv(device_id):
    """Export device data to CSV"""
    try:
        # Check if device exists
        conn = sqlite3.connect(DB_PATH)
        device = conn.execute('SELECT device_name FROM devices WHERE device_id = ?', (device_id,)).fetchone()
        
        if not device:
            conn.close()
            return jsonify({'error': 'Device not found'}), 404
        
        device_name = device[0]
        
        # Get date range from query params (optional)
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        if start_date and end_date:
            try:
                start_dt = datetime.strptime(start_date, '%Y-%m-%d')
                end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
                start_ts = int(start_dt.timestamp())
                end_ts = int(end_dt.timestamp())
                
                rows = conn.execute('''
                    SELECT ts, voltage, current, power 
                    FROM readings 
                    WHERE device_id = ? AND ts >= ? AND ts < ?
                    ORDER BY ts
                ''', (device_id, start_ts, end_ts)).fetchall()
            except ValueError:
                conn.close()
                return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
        else:
            # Export all data if no date range specified
            rows = conn.execute('''
                SELECT ts, voltage, current, power 
                FROM readings 
                WHERE device_id = ?
                ORDER BY ts
            ''', (device_id,)).fetchall()
        
        conn.close()
        
        # Create CSV in memory
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow(['Timestamp', 'Date Time', 'Voltage (V)', 'Current (A)', 'Power (W)'])
        
        # Write data rows
        for ts, voltage, current, power in rows:
            dt = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
            writer.writerow([ts, dt, voltage, current, power])
        
        # Prepare response
        output.seek(0)
        
        # Generate filename
        if start_date and end_date:
            filename = f"{device_name}_{device_id}_{start_date}_to_{end_date}.csv"
        else:
            filename = f"{device_name}_{device_id}_all_data.csv"
        
        filename = filename.replace(' ', '_')
        
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
        
    except Exception as e:
        print(f"Export error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/device/<device_id>/export/stats')
def export_device_stats(device_id):
    """Get export statistics for a device"""
    try:
        conn = sqlite3.connect(DB_PATH)
        
        # Get total records
        total_records = conn.execute(
            'SELECT COUNT(*) FROM readings WHERE device_id = ?',
            (device_id,)
        ).fetchone()[0]
        
        # Get date range
        date_range = conn.execute('''
            SELECT 
                MIN(ts) as first_reading,
                MAX(ts) as last_reading
            FROM readings 
            WHERE device_id = ?
        ''', (device_id,)).fetchone()
        
        conn.close()
        
        first_reading = None
        last_reading = None
        
        if date_range[0] and date_range[1]:
            first_reading = datetime.fromtimestamp(date_range[0]).strftime('%Y-%m-%d %H:%M:%S')
            last_reading = datetime.fromtimestamp(date_range[1]).strftime('%Y-%m-%d %H:%M:%S')
        
        return jsonify({
            'total_records': total_records,
            'first_reading': first_reading,
            'last_reading': last_reading
        })
        
    except Exception as e:
        print(f"Stats error: {e}")
        return jsonify({'error': str(e)}), 500

# ------------- Schedule Management APIs -------------
@app.route('/api/device/<device_id>/schedules', methods=['GET'])
def get_device_schedules(device_id):
    """Get all schedules for a device"""
    try:
        conn = sqlite3.connect(DB_PATH)
        
        # First check if device exists
        device = conn.execute('SELECT 1 FROM devices WHERE device_id = ?', (device_id,)).fetchone()
        if not device:
            conn.close()
            return jsonify({'error': 'Device not found'}), 404
        
        schedules = conn.execute('''
            SELECT id, schedule_name, start_time, end_time, days, enabled, created_at
            FROM schedules
            WHERE device_id = ?
            ORDER BY created_at DESC
        ''', (device_id,)).fetchall()
        conn.close()
        
        result = []
        for schedule in schedules:
            result.append({
                'id': schedule[0],
                'schedule_name': schedule[1],
                'start_time': schedule[2],
                'end_time': schedule[3],
                'days': schedule[4],
                'enabled': schedule[5] == 1,
                'created_at': schedule[6]
            })
        
        print(f"[API] Loaded {len(result)} schedule(s) for device {device_id}")
        return jsonify(result)
    except Exception as e:
        print(f"[API ERROR] Error fetching schedules for {device_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/device/<device_id>/schedules', methods=['POST'])
def create_schedule(device_id):
    """Create a new schedule"""
    try:
        data = request.json
        schedule_name = data.get('schedule_name')
        start_time = data.get('start_time')
        end_time = data.get('end_time')
        days = data.get('days')  # Comma-separated string
        
        if not all([schedule_name, start_time, end_time, days]):
            return jsonify({'error': 'All fields are required'}), 400
        
        conn = sqlite3.connect(DB_PATH)
        
        # Check if device exists
        device = conn.execute('SELECT 1 FROM devices WHERE device_id = ?', (device_id,)).fetchone()
        if not device:
            conn.close()
            return jsonify({'error': 'Device not found'}), 404
        
        # Insert schedule
        conn.execute('''
            INSERT INTO schedules (device_id, schedule_name, start_time, end_time, days, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        ''', (device_id, schedule_name, start_time, end_time, days, int(time.time())))
        
        conn.commit()
        schedule_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        
        return jsonify({'success': True, 'schedule_id': schedule_id, 'message': 'Schedule created successfully'})
    except Exception as e:
        print(f"Error creating schedule: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/schedules/<int:schedule_id>', methods=['PUT'])
def update_schedule(schedule_id):
    """Update schedule (enable/disable or edit)"""
    try:
        data = request.json
        conn = sqlite3.connect(DB_PATH)
        
        # If just toggling enabled status
        if 'enabled' in data and len(data) == 1:
            conn.execute('UPDATE schedules SET enabled = ? WHERE id = ?', 
                        (1 if data['enabled'] else 0, schedule_id))
        else:
            # Full update
            conn.execute('''
                UPDATE schedules 
                SET schedule_name = ?, start_time = ?, end_time = ?, days = ?
                WHERE id = ?
            ''', (data['schedule_name'], data['start_time'], data['end_time'], 
                  data['days'], schedule_id))
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Schedule updated successfully'})
    except Exception as e:
        print(f"Error updating schedule: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/schedules/<int:schedule_id>', methods=['DELETE'])
def delete_schedule(schedule_id):
    """Delete a schedule"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('DELETE FROM schedules WHERE id = ?', (schedule_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Schedule deleted successfully'})
    except Exception as e:
        print(f"Error deleting schedule: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/schedules/test', methods=['GET'])
def test_schedules():
    """Test endpoint to check schedule status and trigger manual check"""
    try:
        from datetime import datetime
        now = datetime.now()
        
        conn = sqlite3.connect(DB_PATH)
        schedules = conn.execute('''
            SELECT s.id, s.device_id, s.schedule_name, s.start_time, s.end_time, s.days, s.enabled, d.device_name
            FROM schedules s
            JOIN devices d ON s.device_id = d.device_id
        ''').fetchall()
        conn.close()
        
        result = {
            'current_time': now.strftime('%H:%M'),
            'current_day': now.strftime('%A').lower(),
            'total_schedules': len(schedules),
            'enabled_schedules': len([s for s in schedules if s[6] == 1]),
            'schedules': []
        }
        
        for s in schedules:
            schedule_days = [day.strip().lower() for day in s[5].split(',')]
            is_today = result['current_day'] in schedule_days
            
            result['schedules'].append({
                'id': s[0],
                'device_name': s[7],
                'schedule_name': s[2],
                'start_time': s[3],
                'end_time': s[4],
                'days': s[5],
                'enabled': s[6] == 1,
                'runs_today': is_today,
                'next_action': 'ON' if result['current_time'] < s[3] else 'OFF' if result['current_time'] < s[4] else 'Done for today'
            })
        
        # Trigger a manual schedule check
        check_and_execute_schedules()
        
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run()
