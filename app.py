from flask import Flask, render_template, jsonify, request, session, redirect, url_for
import json, os, urllib.request, urllib.error, threading, time, random
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'wynnpalace_secret_2024')

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

DEFAULT_SETTINGS = {
    "games": {
        "dice":     { "enabled": True, "name": "Super Sicbo",      "odds": 2.0 },
        "topcard":  { "enabled": True, "name": "Top Card",          "odds": 1.9 },
        "slots":    { "enabled": True, "name": "Lucky Slots",       "jackpot_mult": 10.0, "two_match_mult": 0.5 },
        "roulette": { "enabled": True, "name": "Fireball Roulette", "odds": { "red": 2.0, "blue": 2.0, "green": 3.0, "gold": 6.0 } },
        "lottery8": { "enabled": True, "name": "8 Point Lottery",   "small_odds": 2.0, "big_odds": 2.0 }
    },
    "schedule": [],
    "admin_password": "admin123",
    "site_name": "Wynn Palace",
    "welcome_bonus": 500,
    "payment_methods": {
        "upi": { "enabled": True, "upi_id": "", "qr_url": "" },
        "bank": { "enabled": True, "account_name": "", "account_no": "", "ifsc": "", "bank_name": "" },
        "bitcoin": { "enabled": False, "address": "" }
    }
}

# ── SUPABASE ──
def supa_request(method, path, data=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=representation'
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        print(f"Supabase error {e.code}: {e.read().decode()}")
        return None

def load_settings():
    try:
        result = supa_request('GET', 'wynn_settings?key=eq.main&select=value')
        if result:
            s = json.loads(result[0]['value'])
            # Merge missing keys from DEFAULT_SETTINGS
            for key, val in DEFAULT_SETTINGS.items():
                if key not in s:
                    s[key] = val
            return s
        supa_request('POST', 'wynn_settings', {'key': 'main', 'value': json.dumps(DEFAULT_SETTINGS)})
        return DEFAULT_SETTINGS
    except Exception as e:
        print(f"Load error: {e}")
        return DEFAULT_SETTINGS

def save_settings(data):
    try:
        supa_request('DELETE', 'wynn_settings?key=eq.main')
        supa_request('POST', 'wynn_settings', {'key': 'main', 'value': json.dumps(data)})
    except Exception as e:
        print(f"Save error: {e}")

# ══════════════════════════════════════
#  8 POINT LOTTERY ENGINE
# ══════════════════════════════════════
ROUND_DURATION  = 300  # 5 minutes betting
DRAW_DURATION   = 5    # 5 sec animation
RESULT_DURATION = 10   # 10 sec result display
CYCLE_DURATION  = ROUND_DURATION + DRAW_DURATION + RESULT_DURATION  # 315 sec

round_lock = threading.Lock()

def get_round_state():
    """Calculate current round state from epoch time — universal, restart-proof."""
    now = int(time.time())
    # Use a fixed epoch start (Jan 1 2025 00:00:00 UTC)
    EPOCH_START = 1735689600
    elapsed = (now - EPOCH_START) % CYCLE_DURATION
    round_no = (now - EPOCH_START) // CYCLE_DURATION + 1

    if elapsed < ROUND_DURATION:
        return {
            'round_no': round_no,
            'status': 'betting',
            'seconds_left': ROUND_DURATION - elapsed,
            'result': None,
            'sum': None
        }
    elif elapsed < ROUND_DURATION + DRAW_DURATION:
        return {
            'round_no': round_no,
            'status': 'drawing',
            'seconds_left': ROUND_DURATION + DRAW_DURATION - elapsed,
            'result': None,
            'sum': None
        }
    else:
        return {
            'round_no': round_no,
            'status': 'result',
            'seconds_left': CYCLE_DURATION - elapsed,
            'result': None,  # fetched from Supabase
            'sum': None
        }

# In-memory bets and forced result for current round
current_bets = {}
forced_result = None
last_processed_round = 0
last_result = None
last_sum = None
round_lock = threading.Lock()

def run_lottery():
    global current_bets, forced_result, last_processed_round, last_result, last_sum
    while True:
        state = get_round_state()
        rno = state['round_no']

        # New round started — process result if not done
        if state['status'] == 'drawing' and last_processed_round < rno:
            last_processed_round = rno
            with round_lock:
                bets = dict(current_bets)
                fr = forced_result

            # Calculate result
            if fr:
                big_t = 'big' in fr
                s, _ = gen_sum_for(big_t)
                result = 'big' if big_t else 'small'
            else:
                majority = get_majority(bets)
                if majority == 'big':
                    s, _ = gen_sum_for(False)
                elif majority == 'small':
                    s, _ = gen_sum_for(True)
                else:
                    balls = [random.randint(1,8) for _ in range(8)]
                    s = sum(balls)
                result = calculate_result(s)

            with round_lock:
                last_result = result
                last_sum = s

            # Save to Supabase
            try:
                supa_request('POST', 'lottery_rounds', {
                    'round_no': rno,
                    'status': 'result',
                    'result': result,
                    'draw_time': datetime.utcnow().isoformat()
                })
            except: pass

        # Reset bets for new betting round
        if state['status'] == 'betting':
            with round_lock:
                if last_processed_round < rno - 1 or (current_bets and state['seconds_left'] > ROUND_DURATION - 2):
                    pass  # keep bets
                elif state['seconds_left'] == ROUND_DURATION:
                    current_bets = {}
                    forced_result = None

        time.sleep(1)

threading.Thread(target=run_lottery, daemon=True).start()

# ── PLAYER ROUTES ──

@app.route('/api/withdrawal', methods=['POST'])
def api_withdrawal():
    data = request.json
    try:
        supa_request('POST', 'withdrawal_requests', {
            'username':     data.get('username', 'Player'),
            'account_name': data.get('account_name'),
            'account_no':   data.get('account_no'),
            'ifsc':         data.get('ifsc'),
            'bank_name':    data.get('bank_name'),
            'amount':       int(data.get('amount', 0)),
            'status':       'pending'
        })
        return jsonify({'success': True})
    except Exception as e:
        print(f"Withdrawal error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/lottery')
def lottery():
    return render_template('lottery8.html')

@app.route('/api/settings')
def api_settings():
    s = load_settings()
    current_time = datetime.now().strftime('%H:%M')
    for rule in s.get('schedule', []):
        if rule['start'] <= current_time <= rule['end']:
            game = rule['game']
            if game in s['games']:
                s['games'][game]['odds'] = rule.get('value', s['games'][game].get('odds', 2.0))
    return jsonify({'games': s['games'], 'site_name': s.get('site_name','Wynn Palace'), 'welcome_bonus': s.get('welcome_bonus',500)})

# ── LOTTERY API ──

@app.route('/api/lottery/round')
def lottery_round():
    state = get_round_state()
    with round_lock:
        if state['status'] == 'result':
            state['result'] = last_result
            state['sum'] = last_sum
        state['server_time'] = int(time.time())
    return jsonify({'round': state})

@app.route('/api/lottery/bet', methods=['POST'])
def lottery_bet():
    data = request.json
    bet_type = data.get('bet_type')
    amount   = int(data.get('amount', 0))
    username = data.get('username', 'Player')

    valid_bets = ['big','small']
    if bet_type not in valid_bets:
        return jsonify({'error': 'Invalid bet type'}), 400
    if amount < 10:
        return jsonify({'error': 'Min bet 10 pts'}), 400

    state = get_round_state()
    if state['status'] != 'betting':
        return jsonify({'error': 'Betting is closed'}), 400

    with round_lock:
        current_bets[username] = {'bet_type': bet_type, 'amount': amount}

    try:
        rno = state['round_no']
        supa_request('POST', 'lottery_bets', {
            'round_id': rno, 'username': username,
            'bet_type': bet_type, 'amount': amount
        })
    except: pass

    return jsonify({'success': True})

@app.route('/api/lottery/force', methods=['POST'])
def lottery_force():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    result = request.json.get('result')
    valid = ['big','small',None]
    if result not in valid:
        return jsonify({'error': 'Invalid result'}), 400
    with round_lock:
        forced_result = result
    return jsonify({'success': True, 'forced': result})

@app.route('/api/lottery/pool')
def lottery_pool():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    state = get_round_state()
    with round_lock:
        bets = dict(current_bets)
        status = state['status']
        rno = state['round_no']
    totals = {}
    for b in bets.values():
        bt = b['bet_type']
        totals[bt] = totals.get(bt, 0) + b['amount']
    return jsonify({'round_no': rno, 'status': status, 'pool': totals, 'total_bets': len(bets)})

# ── ADMIN ROUTES ──

@app.route('/admin')
def admin():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    return render_template('admin.html', settings=load_settings())

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        s = load_settings()
        if request.form.get('password') == s['admin_password']:
            session['admin_logged_in'] = True
            return redirect(url_for('admin'))
        error = 'Wrong password!'
    return render_template('admin_login.html', error=error)

@app.route('/admin/withdrawals')
def admin_withdrawals():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        # Try filtered query first
        result = supa_request('GET', 'withdrawal_requests?status=eq.pending&order=created_at.desc&limit=50')
        
        # If result is None or empty, try without filter to debug
        if not result:
            print("📌 WARNING: Pending withdrawals query returned empty, trying all withdrawals...")
            result = supa_request('GET', 'withdrawal_requests?order=created_at.desc&limit=50')
            if result:
                print(f"📊 All withdrawals found: {len(result)} records")
                for r in result:
                    print(f"   - {r.get('username')}: status={r.get('status')}, amount={r.get('amount')}")
                # Filter locally
                result = [r for r in result if r.get('status') == 'pending']
                print(f"📌 After filtering for 'pending': {len(result)} records")
            else:
                print("❌ No withdrawals found at all!")
                result = []
        else:
            print(f"✅ Pending withdrawals query successful: {len(result)} records")
        
        return jsonify({'requests': result or []})
    except Exception as e:
        print(f"❌ Admin withdrawals error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/withdrawal/update', methods=['POST'])
def admin_withdrawal_update():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    try:
        # Get withdrawal request details
        wd = supa_request('GET', f"withdrawal_requests?id=eq.{data['id']}&select=*")
        if not wd:
            return jsonify({'error': 'Request not found'}), 404
        wd = wd[0]

        # If approving, deduct balance from user
        if data['status'] == 'approved' and wd['status'] == 'pending':
            user = supa_request('GET', f"users?username=eq.{wd['username']}&select=id,balance")
            if user:
                new_bal = float(user[0]['balance']) - float(wd['amount'])
                if new_bal < 0:
                    return jsonify({'error': 'User has insufficient balance'}), 400
                supa_request('PATCH', f"users?id=eq.{user[0]['id']}", {'balance': new_bal})

        supa_request('PATCH', f"withdrawal_requests?id=eq.{data['id']}", {'status': data['status']})
        return jsonify({'success': True})
    except Exception as e:
        print(f"Withdrawal update error: {e}")
        return jsonify({'error': str(e)}), 500

# ── DEPOSIT REQUESTS ──
@app.route('/admin/deposits')
def admin_deposits():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        # Try filtered query first
        result = supa_request('GET', 'deposit_requests?status=eq.pending&order=created_at.desc&limit=50')
        
        # If result is None or empty, try without filter to debug
        if not result:
            print("📌 WARNING: Pending deposits query returned empty, trying all deposits...")
            result = supa_request('GET', 'deposit_requests?order=created_at.desc&limit=50')
            if result:
                print(f"📊 All deposits found: {len(result)} records")
                for r in result:
                    print(f"   - {r.get('username')}: status={r.get('status')}, amount={r.get('amount')}")
                # Filter locally
                result = [r for r in result if r.get('status') == 'pending']
                print(f"📌 After filtering for 'pending': {len(result)} records")
            else:
                print("❌ No deposits found at all!")
                result = []
        else:
            print(f"✅ Pending deposits query successful: {len(result)} records")
        
        return jsonify({'requests': result or []})
    except Exception as e:
        print(f"❌ Admin deposits error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/deposit/add', methods=['POST'])
def admin_deposit_add():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    username = data.get('username', '').strip()
    amount = int(data.get('amount', 0))
    if not username or amount <= 0:
        return jsonify({'error': 'Invalid data'}), 400
    try:
        user = supa_request('GET', f"users?username=eq.{username}&select=id,balance")
        if not user:
            return jsonify({'error': 'User not found'}), 404
        new_bal = float(user[0]['balance']) + amount
        supa_request('PATCH', f"users?id=eq.{user[0]['id']}", {'balance': new_bal})
        # Log deposit
        supa_request('POST', 'deposit_requests', {
            'username': username,
            'amount': amount,
            'status': 'approved',
            'note': data.get('note', 'Admin manual deposit')
        })
        return jsonify({'success': True, 'new_balance': new_bal})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/users')
def admin_users():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        result = supa_request('GET', 'users?select=id,username,balance,vip_level,created_at&order=created_at.desc')
        return jsonify({'users': result or []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

# ── CHAT SYSTEM ──
@app.route('/api/chat/send', methods=['POST'])
def api_chat_send():
    if 'user' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    data = request.json
    message = data.get('message', '').strip()
    issue_type = data.get('issue_type', None)
    if not message:
        return jsonify({'error': 'Empty message'}), 400
    try:
        payload = {'username': session['user']['username'], 'message': message, 'sender': 'user'}
        if issue_type:
            payload['issue_type'] = issue_type
        supa_request('POST', 'chat_messages', payload)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chat/history')
def api_chat_history():
    if 'user' not in session:
        return jsonify({'messages': []})
    try:
        result = supa_request('GET', f"chat_messages?username=eq.{session['user']['username']}&order=created_at.asc&limit=100")
        return jsonify({'messages': result or []})
    except:
        return jsonify({'messages': []})

@app.route('/api/chat/poll')
def api_chat_poll():
    if 'user' not in session:
        return jsonify({'messages': []})
    after_id = request.args.get('after', 0)
    try:
        result = supa_request('GET', f"chat_messages?username=eq.{session['user']['username']}&id=gt.{after_id}&order=created_at.asc")
        return jsonify({'messages': result or []})
    except:
        return jsonify({'messages': []})

@app.route('/admin/chat/users')
def admin_chat_users():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        result = supa_request('GET', 'chat_messages?order=created_at.desc&limit=500')
        if not result:
            return jsonify({'users': []})
        users = {}
        for msg in result:
            u = msg['username']
            if u not in users:
                users[u] = {'username': u, 'issue_type': None, 'recent_messages': [], 'created_at': msg['created_at']}
            if msg.get('issue_type') and not users[u]['issue_type']:
                users[u]['issue_type'] = msg['issue_type']
            if len(users[u]['recent_messages']) < 3:
                users[u]['recent_messages'].insert(0, {'message': msg['message'], 'sender': msg['sender']})
        return jsonify({'users': list(users.values())})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/chat/messages')
def admin_chat_messages():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    username = request.args.get('username')
    if not username:
        return jsonify({'messages': []})
    try:
        result = supa_request('GET', f"chat_messages?username=eq.{username}&order=created_at.asc&limit=100")
        return jsonify({'messages': result or []})
    except:
        return jsonify({'messages': []})

@app.route('/admin/chat/reply', methods=['POST'])
def admin_chat_reply():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    username = data.get('username', '').strip()
    message = data.get('message', '').strip()
    if not username or not message:
        return jsonify({'error': 'Invalid data'}), 400
    try:
        supa_request('POST', 'chat_messages', {'username': username, 'message': message, 'sender': 'admin'})
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/chat/poll')
def admin_chat_poll():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    after_id = request.args.get('after', 0)
    username = request.args.get('username', '')
    try:
        if username:
            result = supa_request('GET', f"chat_messages?username=eq.{username}&id=gt.{after_id}&order=created_at.asc")
        else:
            result = supa_request('GET', f"chat_messages?sender=eq.user&id=gt.{after_id}&order=created_at.asc")
        return jsonify({'messages': result or []})
    except:
        return jsonify({'messages': []})
import hashlib
def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.json
    username = data.get('username','').strip()
    password = data.get('password','').strip()
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    if len(username) < 3:
        return jsonify({'error': 'Username min 3 chars'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password min 6 chars'}), 400
    # Check if exists
    existing = supa_request('GET', f"users?username=eq.{username}&select=id")
    if existing:
        return jsonify({'error': 'Username already taken'}), 400
    result = supa_request('POST', 'users', {
        'username': username,
        'password': hash_pw(password),
        'balance': 0,
        'vip_level': 1
    })
    if result:
        user = result[0]
        session['user'] = {'id': user['id'], 'username': user['username'], 'balance': float(user['balance']), 'vip_level': user['vip_level']}
        return jsonify({'success': True, 'user': session['user']})
    return jsonify({'error': 'Registration failed'}), 500

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json
    username = data.get('username','').strip()
    password = data.get('password','').strip()
    result = supa_request('GET', f"users?username=eq.{username}&password=eq.{hash_pw(password)}&select=*")
    if not result:
        return jsonify({'error': 'Invalid username or password'}), 401
    user = result[0]
    session['user'] = {'id': user['id'], 'username': user['username'], 'balance': float(user['balance']), 'vip_level': user['vip_level']}
    return jsonify({'success': True, 'user': session['user']})

@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.pop('user', None)
    return jsonify({'success': True})

@app.route('/api/me')
def api_me():
    if 'user' not in session:
        return jsonify({'logged_in': False})
    # Refresh balance from DB
    try:
        result = supa_request('GET', f"users?id=eq.{session['user']['id']}&select=balance,vip_level")
        if result:
            session['user']['balance'] = float(result[0]['balance'])
            session['user']['vip_level'] = result[0]['vip_level']
    except: pass
    return jsonify({'logged_in': True, 'user': session['user']})

@app.route('/api/balance/update', methods=['POST'])
def api_balance_update():
    if 'user' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    data = request.json
    new_balance = data.get('balance')
    try:
        supa_request('PATCH', f"users?id=eq.{session['user']['id']}", {'balance': new_balance})
        session['user']['balance'] = new_balance
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── BET HISTORY ──
@app.route('/api/bet/save', methods=['POST'])
def api_bet_save():
    data = request.json
    username = session.get('user', {}).get('username', 'Guest')
    try:
        supa_request('POST', 'bet_history', {
            'username':   username,
            'game_name':  data.get('game_name'),
            'bet_amount': int(data.get('bet_amount', 0)),
            'choice':     data.get('choice', ''),
            'result':     data.get('result', ''),
            'won':        bool(data.get('won', False)),
            'payout':     int(data.get('payout', 0))
        })
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/bet/history')
def api_bet_history():
    username = session.get('user', {}).get('username')
    if not username:
        return jsonify({'history': []})
    try:
        result = supa_request('GET', f"bet_history?username=eq.{username}&order=created_at.desc&limit=50")
        return jsonify({'history': result or []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/save', methods=['POST'])
def admin_save():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    s = load_settings()

    for game_id, game_data in data.get('games', {}).items():
        if game_id in s['games']:
            s['games'][game_id]['enabled'] = game_data.get('enabled', True)
            if game_id == 'dice':
                s['games'][game_id]['odds'] = float(game_data.get('odds', 2.0))
            elif game_id == 'topcard':
                s['games'][game_id]['odds'] = float(game_data.get('odds', 1.9))
            elif game_id == 'slots':
                s['games'][game_id]['jackpot_mult']  = float(game_data.get('jackpot_mult', 10.0))
                s['games'][game_id]['two_match_mult'] = float(game_data.get('two_match_mult', 0.5))
            elif game_id == 'roulette':
                for color in ['red','blue','green','gold']:
                    if color in game_data.get('odds', {}):
                        s['games'][game_id]['odds'][color] = float(game_data['odds'][color])
            elif game_id == 'lottery8':
                s['games'][game_id]['small_odds'] = float(game_data.get('small_odds', 2.0))
                s['games'][game_id]['big_odds']   = float(game_data.get('big_odds', 2.0))

    s['schedule']      = data.get('schedule', [])
    s['welcome_bonus'] = int(data.get('welcome_bonus', 500))
    if data.get('new_password'):
        s['admin_password'] = data['new_password']
    # Save payment methods
    if 'payment_methods' in data:
        s['payment_methods'] = data['payment_methods']

    save_settings(s)
    return jsonify({'success': True})

# ── PAYMENT METHODS ──
@app.route('/api/payment-methods')
def api_payment_methods():
    s = load_settings()
    return jsonify({'payment_methods': s.get('payment_methods', {})})

# ── DEPOSIT SLIP ──
@app.route('/api/deposit/submit', methods=['POST'])
def api_deposit_submit():
    if 'user' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    data = request.json
    username = session['user']['username']
    method = data.get('method', '')
    slip_url = data.get('slip_url', '')  # Base64 image or URL
    amount = data.get('amount', 0)
    try:
        # Store deposit with payment slip
        deposit_result = supa_request('POST', 'deposit_requests', {
            'username': username,
            'method': method,
            'slip_url': slip_url,
            'amount': amount,
            'status': 'pending',
            'note': f'{method} deposit request with payment slip'
        })
        
        # Notify user in chat
        supa_request('POST', 'chat_messages', {
            'username': username,
            'message': f'📎 Payment slip submitted via {method.upper()}. Awaiting confirmation.',
            'sender': 'user',
            'issue_type': 'deposit'
        })
        
        # Notify admin
        supa_request('POST', 'chat_messages', {
            'username': 'ADMIN',
            'message': f'🔔 NEW DEPOSIT: {username} deposited ${amount} via {method.upper()}. Payment slip uploaded.',
            'sender': 'system',
            'issue_type': 'deposit'
        })
        
        return jsonify({'success': True, 'deposit_id': deposit_result[0]['id'] if deposit_result else None})
    except Exception as e:
        print(f"Deposit error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/deposits/pending')
def admin_deposits_pending():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        # Only get pending deposits
        result = supa_request('GET', 'deposit_requests?status=eq.pending&order=created_at.desc&limit=50')
        return jsonify({'requests': result or []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/deposit/action', methods=['POST'])
def admin_deposit_action():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    req_id = data.get('id')
    action = data.get('action')  # 'approve' or 'reject'
    amount = data.get('amount', 0)
    try:
        # Get request
        req = supa_request('GET', f"deposit_requests?id=eq.{req_id}&select=*")
        if not req:
            return jsonify({'error': 'Request not found'}), 404
        req = req[0]
        username = req['username']

        if action == 'approve':
            # Add balance
            user = supa_request('GET', f"users?username=eq.{username}&select=id,balance")
            if user:
                new_bal = float(user[0]['balance']) + float(amount or req.get('amount', 0))
                supa_request('PATCH', f"users?id=eq.{user[0]['id']}", {'balance': new_bal})
            # Update request status
            supa_request('PATCH', f"deposit_requests?id=eq.{req_id}", {'status': 'approved', 'amount': float(amount or req.get('amount', 0))})
            # Notify user via chat
            supa_request('POST', 'chat_messages', {
                'username': username,
                'message': f'✅ Payment confirmed! ${int(float(amount or req.get("amount",0))):,} added to your account.',
                'sender': 'admin',
                'issue_type': 'deposit'
            })
        else:
            supa_request('PATCH', f"deposit_requests?id=eq.{req_id}", {'status': 'rejected'})
            # Notify user via chat
            supa_request('POST', 'chat_messages', {
                'username': username,
                'message': '❌ Payment not received. Please contact customer support if you believe this is an error.',
                'sender': 'admin',
                'issue_type': 'deposit'
            })

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/deposit/details', methods=['GET'])
def admin_deposit_details():
    """Get deposit details including payment slip image"""
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    req_id = request.args.get('id')
    if not req_id:
        return jsonify({'error': 'Missing deposit ID'}), 400
    try:
        result = supa_request('GET', f"deposit_requests?id=eq.{req_id}&select=*")
        if not result:
            return jsonify({'error': 'Deposit not found'}), 404
        deposit = result[0]
        # Return full deposit details including payment slip (base64)
        return jsonify({
            'id': deposit['id'],
            'username': deposit['username'],
            'amount': deposit['amount'],
            'method': deposit.get('method', 'upi'),
            'status': deposit['status'],
            'slip_url': deposit.get('slip_url'),  # Base64 image data
            'created_at': deposit['created_at'],
            'note': deposit.get('note', '')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

SUPER_ADMIN_PASSWORD = os.environ.get('SUPER_ADMIN_PASSWORD', 'superadmin999')

# ── MAINTENANCE MODE CHECK ──
@app.before_request
def check_maintenance():
    allowed_paths = ['/superadmin', '/superadmin/login', '/superadmin/toggle',
                     '/admin', '/admin/login', '/admin/save', '/static/']
    # Allow admin and superadmin routes always
    for path in allowed_paths:
        if request.path.startswith(path):
            return None
    # Check maintenance mode
    s = load_settings()
    if s.get('maintenance_mode', False):
        if request.path.startswith('/api/') or request.headers.get('X-Requested-With'):
            return jsonify({'error': 'Site is under maintenance', 'maintenance': True}), 503
        return render_template('maintenance.html'), 503

# ── SUPER ADMIN ROUTES ──
@app.route('/superadmin/login', methods=['GET','POST'])
def superadmin_login():
    error = ''
    if request.method == 'POST':
        if request.form.get('password') == SUPER_ADMIN_PASSWORD:
            session['superadmin_logged_in'] = True
            return redirect('/superadmin')
        error = 'Wrong password'
    return f'''<!DOCTYPE html>
<html>
<head>
<title>Super Admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:#03050a; display:flex; align-items:center; justify-content:center; min-height:100vh; font-family:sans-serif; }}
  .box {{ background:#0a0f1e; border:1px solid rgba(255,50,50,0.3); border-radius:16px; padding:32px 24px; width:90%; max-width:340px; text-align:center; }}
  h2 {{ color:#ff4444; font-size:20px; margin-bottom:6px; }}
  p {{ color:#555; font-size:12px; margin-bottom:24px; }}
  input {{ width:100%; padding:12px; background:#0f1525; border:1px solid rgba(255,50,50,0.2); border-radius:8px; color:#fff; font-size:15px; outline:none; margin-bottom:14px; text-align:center; letter-spacing:2px; }}
  button {{ width:100%; padding:12px; background:linear-gradient(135deg,#6e0c14,#d42030); border:none; border-radius:8px; color:#fff; font-size:15px; font-weight:700; cursor:pointer; }}
  .err {{ color:#e63946; font-size:12px; margin-bottom:12px; }}
</style>
</head>
<body>
<div class="box">
  <h2>⚡ SUPER ADMIN</h2>
  <p>Wynn Palace Control Panel</p>
  {"<div class='err'>"+error+"</div>" if error else ""}
  <form method="POST">
    <input type="password" name="password" placeholder="Enter super password" autofocus>
    <button type="submit">ACCESS</button>
  </form>
</div>
</body>
</html>'''

@app.route('/superadmin')
def superadmin():
    if not session.get('superadmin_logged_in'):
        return redirect('/superadmin/login')
    s = load_settings()
    maintenance = s.get('maintenance_mode', False)
    status_color = '#e63946' if maintenance else '#2ecc71'
    status_text = 'SITE DOWN (Maintenance)' if maintenance else 'SITE LIVE'
    btn_text = '▲ BRING SITE UP' if maintenance else '▼ TAKE SITE DOWN'
    btn_color = 'linear-gradient(135deg,#0c5c2a,#18b855)' if maintenance else 'linear-gradient(135deg,#6e0c14,#d42030)'
    return f'''<!DOCTYPE html>
<html>
<head>
<title>Super Admin Panel</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:#03050a; font-family:sans-serif; padding:20px; min-height:100vh; }}
  .header {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:24px; }}
  h1 {{ color:#ff4444; font-size:18px; }}
  .logout {{ background:rgba(255,255,255,0.07); border:1px solid rgba(255,255,255,0.12); border-radius:8px; color:#aaa; padding:6px 14px; font-size:12px; cursor:pointer; text-decoration:none; }}
  .status-card {{ background:#0a0f1e; border:2px solid {status_color}; border-radius:16px; padding:24px; text-align:center; margin-bottom:20px; }}
  .status-dot {{ width:16px; height:16px; border-radius:50%; background:{status_color}; display:inline-block; margin-right:8px; animation:{"pulse 1.5s infinite" if maintenance else "none"}; }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.3}} }}
  .status-text {{ color:{status_color}; font-size:20px; font-weight:700; }}
  .toggle-btn {{ width:100%; padding:16px; border:none; border-radius:12px; background:{btn_color}; color:#fff; font-size:16px; font-weight:700; cursor:pointer; margin-bottom:16px; letter-spacing:0.5px; }}
  .info {{ background:#0a0f1e; border:1px solid rgba(255,255,255,0.07); border-radius:12px; padding:16px; margin-bottom:16px; }}
  .info-row {{ display:flex; justify-content:space-between; padding:8px 0; border-bottom:1px solid rgba(255,255,255,0.05); font-size:13px; }}
  .info-row:last-child {{ border:none; }}
  .info-label {{ color:#555; }}
  .info-val {{ color:#aaa; font-weight:600; }}
  .admin-link {{ display:block; text-align:center; padding:12px; background:rgba(201,168,76,0.1); border:1px solid rgba(201,168,76,0.2); border-radius:10px; color:#c9a84c; text-decoration:none; font-size:14px; font-weight:600; }}
</style>
</head>
<body>
<div class="header">
  <h1>⚡ SUPER ADMIN</h1>
  <a href="/superadmin/logout" class="logout">Logout</a>
</div>

<div class="status-card">
  <div style="margin-bottom:10px;"><span class="status-dot"></span><span class="status-text">{status_text}</span></div>
  <div style="font-size:12px;color:#555;">wynn-palace-1.onrender.com</div>
</div>

<form action="/superadmin/toggle" method="POST">
  <button class="toggle-btn" type="submit">{btn_text}</button>
</form>

<div class="info">
  <div class="info-row"><span class="info-label">Maintenance Mode</span><span class="info-val" style="color:{'#e63946' if maintenance else '#2ecc71'};">{"ON" if maintenance else "OFF"}</span></div>
  <div class="info-row"><span class="info-label">Site URL</span><span class="info-val">wynn-palace-1.onrender.com</span></div>
  <div class="info-row"><span class="info-label">Admin Panel</span><span class="info-val">/admin</span></div>
</div>

<a href="/admin" class="admin-link">→ Go to Admin Panel</a>
</body>
</html>'''

@app.route('/superadmin/toggle', methods=['POST'])
def superadmin_toggle():
    if not session.get('superadmin_logged_in'):
        return redirect('/superadmin/login')
    s = load_settings()
    s['maintenance_mode'] = not s.get('maintenance_mode', False)
    save_settings(s)
    return redirect('/superadmin')

@app.route('/superadmin/logout')
def superadmin_logout():
    session.pop('superadmin_logged_in', None)
    return redirect('/superadmin/login')

@app.route('/api/profile/bank', methods=['GET'])
def get_bank_details():
    if 'user' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    try:
        result = supa_request('GET', f"users?id=eq.{session['user']['id']}&select=bank_details")
        if result and result[0].get('bank_details'):
            bd = result[0]['bank_details']
            # Handle both string (old) and dict (new JSONB)
            if isinstance(bd, str):
                bd = json.loads(bd)
            return jsonify({'bank_details': bd})
        return jsonify({'bank_details': None})
    except:
        return jsonify({'bank_details': None})

@app.route('/api/profile/bank', methods=['POST'])
def save_bank_details():
    if 'user' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    data = request.json
    bank_details = {
        'account_name': data.get('account_name', ''),
        'account_no': data.get('account_no', ''),
        'ifsc': data.get('ifsc', ''),
        'bank_name': data.get('bank_name', '')
    }
    try:
        supa_request('PATCH', f"users?id=eq.{session['user']['id']}", {'bank_details': bank_details})
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=False)
