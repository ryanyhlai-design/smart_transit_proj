from flask import Flask, url_for, session, redirect, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from authlib.integrations.flask_client import OAuth
from flask_cors import CORS
from datetime import datetime, timezone
import os
LIVEBOARD_CACHE = {}

try:
    # 🏠 【本地端模式】
    import config
    TDX_CLIENT_ID = config.TDX_CLIENT_ID
    TDX_CLIENT_SECRET = config.TDX_CLIENT_SECRET
    GOOGLE_CLIENT_ID = config.GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET = config.GOOGLE_CLIENT_SECRET
    BASE_URL = 'http://localhost:5000'

except ImportError:
    # ☁️ 【雲端模式】
    TDX_CLIENT_ID = os.environ.get('TDX_CLIENT_ID')
    TDX_CLIENT_SECRET = os.environ.get('TDX_CLIENT_SECRET')
    GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
    GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')
    BASE_URL = 'https://smart-transit-system-q08n.onrender.com'

# 💡 注意！一定要在設定好密碼之後，才匯入 main！
import main
from main import SmartTransferSystem

# ==========================================
# 🌐 1. Flask 網頁實體與環境設定
# ==========================================
# 允許本地端 HTTP 測試 OAuth
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
os.environ['AUTHLIB_INSECURE_TRANSPORT'] = '1'

app = Flask(__name__)
# 啟用 CORS 以支援地圖與前端的跨域請求
CORS(app)

app.secret_key = 'smart_transfer_system_super_secret_key' 

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# 👇 補上這行，叫 Flask 絕對不可以偷改我們的順序！
app.config['JSON_SORT_KEYS'] = False
# 👇 補上這行新版 Flask (2.2+) 專屬的設定！
app.json.sort_keys = False
# ==========================================
# 💾 2. 資料庫初始化與模型定義
# ==========================================
db = SQLAlchemy()
db.init_app(app) 

# 💡 站名翻譯小幫手：將系統代碼轉換為漂亮的使用者介面文字
def format_station_name(station_code):
    if station_code.startswith('TRA_'):
        name = station_code.replace('TRA_', '')
        return f"台鐵 {name}站"
    elif station_code.startswith('THSR_'):
        name = station_code.replace('THSR_', '')
        return f"高鐵 {name}站"
    return station_code

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False) 
    name = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class FavoriteRoute(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_email = db.Column(db.String(120), db.ForeignKey('user.email'), nullable=False) 
    start_station = db.Column(db.String(50), nullable=False) 
    end_station = db.Column(db.String(50), nullable=False)   
    alias = db.Column(db.String(50)) 
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# 💡 定義歷史查詢紀錄模型
class SearchHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_email = db.Column(db.String(120), db.ForeignKey('user.email'), nullable=False)
    start_station = db.Column(db.String(50), nullable=False)
    end_station = db.Column(db.String(50), nullable=False)
    search_time = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()

# ==========================================
# 🧠 3. 全域啟動演算法大腦 
# ==========================================
print("⏳ 正在喚醒全台智慧轉乘大腦，載入 TDX 資料庫中...")
# ✅ 正確寫法：直接拿我們在最上面設定好的變數來用
transfer_brain = SmartTransferSystem(TDX_CLIENT_ID, TDX_CLIENT_SECRET)
# 補上這一行，讓系統去抓 TDX 車站跟時刻表！
transfer_brain.daily_initialization()
print("✅ 大腦載入完成！隨時可以開始進行最快速路徑與 Pareto 計算。")
import requests

# 💡 記錄最後一次更新的日期，用於跨日判斷
LAST_UPDATE_DATE = datetime.now().date()

def refresh_tdx_data_engine():
    """治本神技：自動換發新憑證，並重新抓取今日最新時刻表"""
    global LAST_UPDATE_DATE
    print("🔄 系統正在自動換發 TDX 憑證並更新今日時刻表...")
    
    auth_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    auth_data = {
        'content-type': 'application/x-www-form-urlencoded',
        'grant_type': 'client_credentials',
        'client_id': TDX_CLIENT_ID,
        'client_secret': TDX_CLIENT_SECRET
    }
    
    try:
        res = requests.post(auth_url, data=auth_data)
        if res.status_code == 200:
            new_token = res.json().get('access_token')
            
            # 1. 暴力更新底層大腦的 Token
            if hasattr(transfer_brain, 'tdx'):
                transfer_brain.tdx.access_token = new_token 
                
            # 2. 重新執行每日初始化 (抓取今天最新的火車班次！)
            transfer_brain.daily_initialization()
            LAST_UPDATE_DATE = datetime.now().date()
            print("✅ 憑證與時刻表更新完成！")
            return True
        else:
            print("❌ TDX 憑證換發失敗:", res.text)
    except Exception as e:
        print("❌ 系統更新異常:", str(e))
    return False

# ==========================================
# 🔑 4. Google OAuth 2.0 登入元件設定
# ==========================================
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,          
    client_secret=GOOGLE_CLIENT_SECRET,  
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# ==========================================
# 🏠 5. 網頁前端路由控制 
# ==========================================
@app.route('/smart-transfer-system/')
def homepage():
    # 修正：無論登入狀態，一律導向根目錄以載入前端介面
    return redirect('/')

# 提供前端靜態檔案的路由，讓地圖介面能正常載入
@app.route('/')
def root():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'index.html')

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static'), filename)

@app.route('/smart-transfer-system/login')
def login():
    redirect_uri = f"{BASE_URL}/smart-transfer-system/login/callback" 
    return google.authorize_redirect(redirect_uri)

@app.route('/smart-transfer-system/login/callback')
def auth():
    token = google.authorize_access_token()
    user_info = token.get('userinfo')
    
    if user_info:
        session['user'] = user_info
        existing_user = User.query.filter_by(email=user_info['email']).first()
        if not existing_user:
            new_user = User(email=user_info['email'], name=user_info['name'])
            db.session.add(new_user)
            db.session.commit()
            print(f"🆕 新使用者資料庫同步註冊成功: {user_info['email']}")
            
    # 修正：登入成功後回到前端首頁
    return redirect('/')

@app.route('/smart-transfer-system/logout')
def logout():
    session.pop('user', None) 
    # 修正：登出後回到前端首頁
    return redirect('/')

# 讓前端檢查目前是否為登入狀態
@app.route('/smart-transfer-system/api/user/status')
def get_user_status():
    if 'user' in session:
        # 💡 確保回傳 status 為 logged_in，並把 user 資料包進去
        return jsonify({
            "status": "logged_in", 
            "user": session['user']
        })
    else:
        return jsonify({"status": "logged_out"}), 401

# 功能 A：呼叫 Dijkstra 演算法並自動記錄歷史
@app.route('/smart-transfer-system/api/search')
def api_search():
    global LAST_UPDATE_DATE
    
    if datetime.now().date() > LAST_UPDATE_DATE:
        print("📅 偵測到跨日！準備載入今日最新火車時刻表...")
        refresh_tdx_data_engine()

    start_st = request.args.get('start')
    end_st = request.args.get('end')
    time_str = request.args.get('time')
    latest_time_str = request.args.get('latest_time', '') 
    budget_str = request.args.get('budget')
    trans_pref = request.args.get('pref')
    role_id = request.args.get('role')
    
    if not start_st or not end_st or not time_str or not budget_str or not trans_pref or not role_id:
        return jsonify({"status": "error", "message": "後端防線攔截：參數不完整！"}), 400
        
    try:
        budget = int(budget_str)
    except ValueError:
        return jsonify({"status": "error", "message": "後端防線攔截：預算格式錯誤"}), 400
        
    if 'user' in session:
        user_email = session['user']['email']
        existing_history = SearchHistory.query.filter_by(user_email=user_email, start_station=start_st, end_station=end_st).first()
        if existing_history:
            existing_history.search_time = datetime.now(timezone.utc)
        else:
            new_history = SearchHistory(user_email=user_email, start_station=start_st, end_station=end_st, search_time = datetime.now(timezone.utc))
            db.session.add(new_history)
        db.session.commit()

    try:
        # 🚀 修正：拔除核彈重試機制，直接呼叫大腦。有錯就誠實噴錯給前端！
        results = transfer_brain.handle_user_query(
            start_st, end_st, time_str, budget, trans_pref, role_id, latest_time_str
        )

        processed_results = []
        for r_time, r_cost, r_path in results:
            actual_start_m = r_path[0]['dep_time']
            duration_mins = r_time - actual_start_m
            if duration_mins < 0: 
                duration_mins += 1440 

            processed_results.append({
                "absolute_arrival_time": r_time,  
                "arrival_time_str": main.min_to_time(r_time),
                "duration_str": main.format_duration(duration_mins), 
                "total_time": r_time,
                "total_price": r_cost,
                "raw_steps": r_path  
            })
        return jsonify({"status": "success", "data": processed_results})
    except Exception as e:
        # 這樣如果演算法出錯，你才看得到真實 Bug 是什麼，而不會去洗爆 TDX
        import traceback
        traceback.print_exc() # 在終端機印出詳細紅字錯誤
        return jsonify({"status": "error", "message": f"演算法計算失敗: {str(e)}"})

# 功能 B：手動新增常用路徑到資料庫 (💡 增加 3 筆上限)
@app.route('/smart-transfer-system/api/favorite/add', methods=['POST'])
def add_favorite():
    if 'user' not in session:
        return jsonify({"status": "error", "message": "請先登入帳號"}), 401
        
    user_email = session['user']['email']
    
    # 💡 攔截點：檢查目前最愛數量是否已達上限 (3筆)
    current_fav_count = FavoriteRoute.query.filter_by(user_email=user_email).count()
    if current_fav_count >= 3:
        return jsonify({"status": "error", "message": "常用路徑最多只能設定 3 筆喔！"})
        
    data = request.get_json()
    start_st = data.get('start')
    end_st = data.get('end')
    # 💡 呼叫小幫手，把 TRA_台北 翻譯成 台鐵台北站
    nice_start = format_station_name(start_st)
    nice_end = format_station_name(end_st)
    
    # 💡 使用翻譯後的名字來當作預設暱稱
    alias = data.get('alias', f"{nice_start} ➔ {nice_end}")
    
    duplicate = FavoriteRoute.query.filter_by(
        user_email=user_email, start_station=start_st, end_station=end_st
    ).first()
    
    if duplicate:
        return jsonify({"status": "error", "message": "此路線已在收藏清單中"})
        
    new_fav = FavoriteRoute(user_email=user_email, start_station=start_st, end_station=end_st, alias=alias)
    db.session.add(new_fav)
    db.session.commit()
    
    return jsonify({"status": "success", "message": "成功加入常用路徑！"})

# 功能 C：單純讀取常用路徑清單
@app.route('/smart-transfer-system/api/favorite/list')
def list_favorites():
    if 'user' not in session:
        return jsonify({"status": "error", "message": "請先登入"}), 401
        
    user_email = session['user']['email']
    fav_routes = FavoriteRoute.query.filter_by(user_email=user_email).all()
    
    result_list = [{"id": r.id, "start": r.start_station, "end": r.end_station, "alias": r.alias} for r in fav_routes]
    return jsonify({"status": "success", "data": result_list})

# 功能 D：取得智慧建議 (交由前端自動控制預設顯示 5 筆，後端一次給齊 15 筆)
@app.route('/smart-transfer-system/api/suggestions')
def get_suggestions():
    if 'user' not in session:
        return jsonify({"status": "error", "message": "請先登入"}), 401
    
    user_email = session['user']['email']
    
    # 1. 撈出「我的最愛」
    favorites = FavoriteRoute.query.filter_by(user_email=user_email).all()
    fav_list = [{"type": "favorite", "start": f.start_station, "end": f.end_station, "alias": f.alias} for f in favorites]
    
    # 2. 💡 拔掉 remaining_slots 限制！直接撈出最近的 15 筆歷史紀錄
    # 這樣前端的 hiddenCount 才會大於 0，按鈕才會出現！
    histories = SearchHistory.query.filter_by(user_email=user_email).order_by(SearchHistory.search_time.desc()).limit(15).all()
    
    hist_list = []
    fav_pairs = {(f.start_station, f.end_station) for f in favorites}
    seen_history_pairs = set()
    
    for h in histories:
        current_pair = (h.start_station, h.end_station)
        
        # 過濾掉已經在最愛，或是已經重複出現的歷史紀錄
        if current_pair not in fav_pairs and current_pair not in seen_history_pairs:
            seen_history_pairs.add(current_pair)
            
            nice_start = format_station_name(h.start_station)
            nice_end = format_station_name(h.end_station)
            
            hist_list.append({
                "type": "history", 
                "start": h.start_station, 
                "end": h.end_station, 
                "alias": f"{nice_start} ➔ {nice_end}"  
            })
            
    # 直接把所有乾淨的紀錄合併傳給前端
    return jsonify({"status": "success", "data": fav_list + hist_list})

# 功能 E：移除指定的常用路徑
@app.route('/smart-transfer-system/api/favorite/remove', methods=['POST'])
def remove_favorite():
    if 'user' not in session:
        return jsonify({"status": "error", "message": "請先登入帳號"}), 401
        
    data = request.get_json()
    start_st = data.get('start')
    end_st = data.get('end')
    user_email = session['user']['email']
    
    route_to_delete = FavoriteRoute.query.filter_by(
        user_email=user_email, 
        start_station=start_st, 
        end_station=end_st
    ).first()
    
    if not route_to_delete:
        return jsonify({"status": "error", "message": "您的最愛清單中沒有這條路徑喔！"})
        
    db.session.delete(route_to_delete)
    db.session.commit()
    
    return jsonify({"status": "success", "message": "已成功移除最愛路徑！"})

# 功能 F：一鍵清除所有歷史查詢紀錄 (保留最愛路徑)
@app.route('/smart-transfer-system/api/history/clear', methods=['POST'])
def clear_history():
    if 'user' not in session:
        return jsonify({"status": "error", "message": "請先登入帳號"}), 401
        
    user_email = session['user']['email']
    SearchHistory.query.filter_by(user_email=user_email).delete()
    db.session.commit()
    
    return jsonify({"status": "success", "message": "歷史紀錄已全部清除乾淨！"})

# 🤫 隱藏版功能：查看所有註冊使用者
@app.route('/smart-transfer-system/api/admin/users')
def list_all_users():
    all_users = User.query.all()
    user_list = [
        {"id": u.id, "name": u.name, "email": u.email, "join_time": u.created_at} 
        for u in all_users
    ]
    return jsonify({"status": "success", "total_users": len(user_list), "data": user_list})

# ==========================================
# 📍 從 web_app.py 補入的「前端地圖與車站資料」專用 API
# ==========================================

# 🚀 終極修復：把抓好的高鐵真實幾何形狀 (WKT) 丟給前端
@app.route('/smart-transfer-system/api/shapes')
def api_shapes():
    import main
    # 這裡的 main.RAIL_SHAPES 裡面裝著你在 daily_initialization 抓到的高鐵軌跡
    return jsonify(main.RAIL_SHAPES)


# 提供地圖所需座標 (將 Tuple 轉為前端看得懂的 Object)
@app.route('/smart-transfer-system/api/coords')
def api_coords():
    import main
    formatted_coords = {}
    for key, coords in main.STATION_COORDS.items():
        lon, lat = coords
        formatted_coords[key] = {"lat": lat, "lng": lon}
    return jsonify(formatted_coords)

# 提供下拉選單與側邊欄的全台車站與支線清單
@app.route('/smart-transfer-system/api/stations')
def api_stations():
    from main import STATION_MAP 
    
    lines = {
        "高鐵": [], "西部幹線-縱貫線(北段)": [], "西部幹線-台中線/山線": [],
        "西部幹線-海岸線/海線": [], "西部幹線-縱貫線(南段)": [], 
        "沙崙線": [], "六家線": [], "宜蘭線": [], "北迴線": [], 
        "台東線/花東線": [], "屏東線": [], "南迴線": [],
        "平溪線": [], "內灣線": [], "集集線": [], "深澳線": [], "其他": []
    }
    stations_list = []
    
    # 💡 優化 1：把「鳳鳴」排進正確的地理順序 (夾在鶯歌跟桃園中間)
    line_dict = {
        "西部幹線-縱貫線(北段)": ["基隆", "三坑", "八堵", "七堵", "百福", "五堵", "汐止", "汐科", "南港", "松山", "台北", "萬華", "板橋", "浮洲", "樹林", "南樹林", "山佳", "鶯歌", "鳳鳴", "桃園", "內壢", "中壢", "埔心", "楊梅", "富岡", "新富", "北湖", "湖口", "新豐", "竹北", "北新竹", "新竹", "三姓橋", "香山", "崎頂", "竹南"],
        "西部幹線-海岸線/海線": ["談文", "大山", "後龍", "龍港", "白沙屯", "新埔", "通霄", "苑裡", "日南", "大甲", "台中港", "清水", "沙鹿", "龍井", "大肚", "追分"],
        "西部幹線-台中線/山線": ["造橋", "豐富", "苗栗", "南勢", "銅鑼", "三義", "泰安", "后里", "豐原", "栗林", "潭子", "頭家厝", "松竹", "太原", "精武", "台中", "五權", "大慶", "烏日", "新烏日", "成功", "彰化"],
        "西部幹線-縱貫線(南段)": ["花壇", "大村", "員林", "永靖", "社頭", "田中", "二水", "林內", "石榴", "斗六", "斗南", "石龜", "大林", "民雄", "嘉北", "嘉義", "水上", "南靖", "後壁", "新營", "柳營", "林鳳營", "隆田", "拔林", "善化", "南科", "新市", "永康", "大橋", "台南", "保安", "仁德", "中洲", "大湖", "路竹", "岡山", "橋頭", "楠梓", "新左營", "左營", "內惟", "美術館", "鼓山", "三塊厝", "高雄"],
        "屏東線": ["民族", "科工館", "正義", "鳳山", "後庄", "九曲堂", "六塊厝", "屏東", "歸來", "麟洛", "西勢", "竹田", "潮州", "崁頂", "南州", "鎮安", "林邊", "佳冬", "東海", "枋寮"],
        "南迴線": ["加祿", "內獅", "枋山", "大武", "瀧溪", "金崙", "太麻里", "知本", "康樂"],
        "台東線/花東線": ["吉安", "志學", "平和", "壽豐", "豐田", "林榮新光", "南平", "鳳林", "萬榮", "光復", "大富", "富源", "瑞穗", "三民", "玉里", "東里", "東竹", "富里", "池上", "海端", "關山", "瑞和", "瑞源", "鹿野", "山里", "台東"],
        "北迴線": ["永樂", "東澳", "南澳", "武塔", "漢本", "和平", "和仁", "崇德", "新城", "景美", "北埔", "花蓮"],
        "宜蘭線": ["暖暖", "四腳亭", "瑞芳", "猴硐", "三貂嶺", "牡丹", "雙溪", "貢寮", "福隆", "石城", "大里", "大溪", "龜山", "外澳", "頭城", "頂埔", "礁溪", "四城", "宜蘭", "二結", "中里", "羅東", "冬山", "新馬", "蘇澳新", "蘇澳"],
        "平溪線": ["大華", "十分", "望古", "嶺腳", "平溪", "菁桐"],
        "深澳線": ["海科館", "八斗子"],
        "內灣線": ["千甲", "新莊", "竹中", "上員", "榮華", "竹東", "橫山", "九讚頭", "合興", "富貴", "內灣"],
        "集集線": ["源泉", "濁水", "龍泉", "集集", "水里", "車埕"],
        "沙崙線": ["長榮大學", "沙崙"],
        "六家線": ["六家"]
    }
    
    # 💡 優化 2：建立幽靈車站黑名單
    blacklist = {"台北-環島", "樹林調車場", "枋野", "南方小站", "潮州基地", "潮州機廠", "大武營"}

    st_lookup = {}
    for key, st_id in STATION_MAP.items():
        try:
            sys_type, name = key.split('_')
            
            # 🚨 攔截點：如果是黑名單內的車站，直接跳過，不存入任何清單！
            if name in blacklist:
                continue
                
            st_data = {"id": key, "name_zh": name, "code": f"{sys_type}_{st_id}"}
            
            # 因為被黑名單攔截了，所以這些站根本不會進到 stations_list
            # 前端的「隨機骰子」自然就永遠不會抽到它們了！
            stations_list.append(st_data)
            
            if sys_type == 'THSR':
                lines["高鐵"].append(st_data)
            else:
                st_lookup[name] = st_data
                
        except ValueError:
            continue

    assigned_names = set()
    for line_name, st_names in line_dict.items():
        for name in st_names:
            if name in st_lookup:
                lines[line_name].append(st_lookup[name])
                assigned_names.add(name)

    for name, st_data in st_lookup.items():
        if st_data["id"].startswith("TRA_") and name not in assigned_names:
            lines["其他"].append(st_data)

    # 💡 優化 3：自動刪除「空」的路線分類
    # 這樣一來，如果「其他」裡面沒半個車站，這個分類就會在前端直接消失，不會留下空標題
    lines = {k: v for k, v in lines.items() if len(v) > 0}

    return jsonify({"lines": lines, "stations": stations_list})

# 取得台鐵車站即時電子看板
@app.route('/smart-transfer-system/api/liveboard')
def api_liveboard():
    station_name = request.args.get('station', '民雄')
    import time
    current_time = time.time()
    if station_name in LIVEBOARD_CACHE:
        cached_data, last_update = LIVEBOARD_CACHE[station_name]
        # 如果 30 秒內查過同一個車站，直接退回記憶體裡的舊資料，不煩 TDX！
        if current_time - last_update < 30:  
            if cached_data == "BLOCKED":
                # 如果快取是封鎖狀態，回傳 error 給前端
                return jsonify({"status": "error", "message": "系統冷卻中，請稍後再試"})
            # 如果是正常陣列，回傳 success
            return jsonify({"status": "success", "data": cached_data})

    st_id = main.STATION_MAP.get(f"TRA_{station_name}", "3190") 
    url = f"{transfer_brain.tdx.base_url}/v2/Rail/TRA/LiveBoard/Station/{st_id}?$format=JSON"
    
    try:
        headers = transfer_brain.tdx._get_headers()
        res = requests.get(url, headers=headers)
        
        # 💡 【治本防護 3】攔截 401 錯誤！過期就換鑰匙再敲一次門
        if res.status_code == 401:
            print("⚠️ LiveBoard 發現 Token 過期，即將重新換發並重試...")
            if refresh_tdx_data_engine():
                headers = transfer_brain.tdx._get_headers() # 拿剛換好的新鑰匙
                res = requests.get(url, headers=headers)    # 再敲一次門！
        
        if res.status_code == 200:
            data = res.json()
            # 💡 成功拿到資料，把它寫進快取字典裡！
            LIVEBOARD_CACHE[station_name] = (data, current_time)
            return jsonify({"status": "success", "data": data})
        elif res.status_code == 429:
            print(f"⚠️ LiveBoard 遇到 429，強制進入 30 秒冷卻！")
            LIVEBOARD_CACHE[station_name] = ("BLOCKED", current_time)
            return jsonify({"status": "error", "message": "TDX 請求太頻繁，請稍後再試"})
        else:
            return jsonify({"status": "error", "message": f"TDX API 錯誤: {res.status_code}"})
            
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
# ==========================================
# 🏁 7. 啟動執行區塊 
# ==========================================
if __name__ == '__main__':
    print("🌐 系統已就緒！請打開瀏覽器輸入: http://localhost:5000/ 或 http://localhost:5000/smart-transfer-system/")
    app.run(port=5000, debug=True, use_reloader=False)   