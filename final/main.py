import requests
import heapq
import math
from datetime import datetime
from collections import defaultdict
import time
import os
import itertools
from numba import njit
import numpy as np

try:
    # 🏠 【本地端模式】
    # 嘗試匯入 config.py。如果在你的電腦上，因為檔案存在，這段會成功。
    import config
    TDX_CLIENT_ID = config.TDX_CLIENT_ID
    TDX_CLIENT_SECRET = config.TDX_CLIENT_SECRET
    GOOGLE_CLIENT_ID = config.GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET = config.GOOGLE_CLIENT_SECRET

except ImportError:
    # ☁️ 【雲端模式】
    # 如果抓不到 config.py (例如在 Render 上)，就會觸發 ImportError
    # 這時程式不會當機，而是自動切換成從「環境變數」讀取密碼
    TDX_CLIENT_ID = os.environ.get('TDX_CLIENT_ID')
    TDX_CLIENT_SECRET = os.environ.get('TDX_CLIENT_SECRET')
    GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
    GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')

# ==========================================
# 全域變數：全台主要車站 ID 智慧對應表
# 由系統啟動時動態抓取填入
# ==========================================
STATION_MAP = {}
REVERSE_MAP = {}
STATION_COORDS = {} # 💡 新增：專門用來存 (經度, 緯度) 給前端地圖用
RAIL_SHAPES = {"THSR": "", "TRA": {}}
# ==========================================
# 工具函式：時間與站名轉換
# ==========================================
def time_to_min(t_str):
    try:
        h, m = map(int, t_str.split(':')[:2])
        return h * 60 + m
    except:
        return 9999

def min_to_time(m):
    """確保無論跨了多少天，時間顯示都能自動折疊回 24 小時制，並加上跨日標籤"""
    days = m // 1440
    m_mod = m % 1440
    base_time_str = f"{m_mod // 60:02d}:{m_mod % 60:02d}"
    
    # 如果大於等於 1440 分鐘，代表跨日了，自動加上標籤
    if days > 0:
        return f"{base_time_str} (+{days}天)"
    return base_time_str

def format_duration(total_mins):
    """將總分鐘數轉換為幾小時幾分的口語格式"""
    h = total_mins // 60
    m = total_mins % 60
    if h > 0:
        return f"{h}小時{m}分"
    return f"{m}分"
def get_station_name(sys_type, st_id):
    """將系統與代碼轉回站名"""
    key = f"{sys_type}_{st_id}"
    return REVERSE_MAP.get(key, key) # 如果字典裡找不到，就直接印代碼

def get_station_coords(station_key):
    """
    查詢指定車站的經緯度座標給前端地圖標記使用。
    支援輸入代碼 (例如: 'TRA_1000') 或站名 (例如: 'TRA_台北')。
    回傳格式: (經度 Lon, 緯度 Lat)，若查無資料則回傳 None。
    """
    return STATION_COORDS.get(station_key)

# ==========================================
# 工具函式：台鐵票價動態推算模型 (車種字串精準版)
# ==========================================
def estimate_tra_cost(distance_km, train_type_name):
    """
    依照台鐵官方里程分段折扣表計算票價 (image_0ca2ca.png)
    """
    # 1. 根據車種決定「基礎費率」(以50公里內為準)
    if '自強' in train_type_name or '普悠瑪' in train_type_name or '太魯閣' in train_type_name:
        base_rate = 3.39
    elif '莒光' in train_type_name:
        base_rate = 2.61
    else:  # 區間車/區間快
        base_rate = 2.18

    # 2. 根據距離套用「里程分段折扣」
    if distance_km <= 50:
        rate = base_rate
    elif distance_km <= 100:
        # 50公里內用 base_rate，50.1-100公里部分打88折
        rate = base_rate * 0.88
    elif distance_km <= 200:
        rate = base_rate * 0.83
    elif distance_km <= 300:
        rate = base_rate * 0.70
    else: # 300.1公里以上
        rate = base_rate * 0.65

    # 3. 計算價格
    final_fare = int(distance_km * rate)
    
    # 🔴 修正：台鐵最低票價為 22 元
    return max(22, final_fare)

# ==========================================
# 工具函式：乘客身分折扣計算器
# ==========================================
def get_discounted_cost(base_cost, sys_type, trans_mode, role_id):
    """
    根據不同交通工具與乘客身分，計算折扣後的票價
    角色: 1:孩童, 2:中小學生, 3:大學生, 4:老年人, 5:身心障礙, 6:一般成人
    """
    if role_id in ['1', '4', '5']:  # 法定半票身分 (孩童、敬老、愛心)
        if sys_type in ['TRA', 'THSR']: 
            return math.ceil(base_cost * 0.5)
        if sys_type == 'TRANSFER' and trans_mode == 'BUS': 
            return math.ceil(base_cost * 0.5) # 公車也有半票
            
    elif role_id == '3':  # 大學生專屬優惠
        if sys_type == 'THSR': 
            return math.ceil(base_cost * 0.75) # 高鐵大學生優惠抓平均約 75 折
            
    # 中小學生、一般成人、或搭乘計程車等無特殊單程折扣者，維持原價
    return base_cost

# ==========================================
# 工具函式：簡化台鐵冗長的車種名稱
# ==========================================
def simplify_train_name(raw_name):
    """過濾 TDX 冗長的車種名稱，轉換為簡潔的口語名稱"""
    if '3000' in raw_name or '新自強' in raw_name:
        return '自強(3000)'
    if '普悠瑪' in raw_name:
        return '普悠瑪'
    if '太魯閣' in raw_name:
        return '太魯閣'
    if '自強' in raw_name:
        return '自強號'
    if '莒光' in raw_name:
        return '莒光號'
    if '區間快' in raw_name:
        return '區間快'
    if '區間' in raw_name:
        return '區間車'
        
    # 如果有其他沒預料到的車種，直接把括號跟裡面的字砍掉
    import re
    return re.sub(r'\(.*?\)|（.*?）', '', raw_name).strip()

import math

# ==========================================
# 1. 台鐵路線歸屬與地理彎角常數
# ==========================================
LINE_GROUPS = {
    "EAST_VALLEY": {"花蓮", "吉安", "壽豐", "光復", "瑞穗", "玉里", "富里", "池上", "關山", "鹿野", "台東"}, 
    "SOUTH_LINK": {"枋寮", "加祿", "內獅", "枋山", "大武", "瀧溪", "金崙", "太麻里", "知本", "康樂", "台東"}, 
    "NORTH_LINK": {"八堵", "瑞芳", "侯硐", "雙溪", "貢寮", "福隆", "頭城", "礁溪", "宜蘭", "羅東", "冬山", "蘇澳新", "蘇澳", "東澳", "南澳", "和平", "和仁", "崇德", "新城", "花蓮"}, 
    "MOUNTAIN": {"竹南", "苗栗", "銅鑼", "三義", "泰安", "后里", "豐原", "台中", "新烏日", "彰化"} 
}

CORNER_NE = (121.944, 25.016)    # 東北角 (福隆/三貂角附近)
CORNER_SOUTH = (120.656, 22.261) # 南迴角 (枋山附近)

def find_line(st_name):
    """判斷車站所屬的台鐵路線"""
    for line, stations in LINE_GROUPS.items():
        if st_name in stations: 
            return line
    return "WEST" # 找不到的站，預設為西部幹線/海線

# ==========================================
# 2. 軌道距離計算器 (虛擬折線與動態倍率法)
# ==========================================
def calculate_haversine_distance(lon1, lat1, lon2, lat2, line1="WEST", line2="WEST"):
    def straight_dist(l1, a1, l2, a2):
        l1, a1, l2, a2 = map(math.radians, [l1, a1, l2, a2])
        dlon, dlat = l2 - l1, a2 - a1
        a = math.sin(dlat/2)**2 + math.cos(a1) * math.cos(a2) * math.sin(dlon/2)**2
        return 2 * math.asin(math.sqrt(a)) * 6371

    lines = {line1, line2}
    
    # 🔴 核心定義：廣義西部與廣義東部 (把山線與縱谷線都包進來)
    has_west = "WEST" in lines or "MOUNTAIN" in lines
    has_east = "NORTH_LINK" in lines or "EAST_VALLEY" in lines
    
    # 魔法 1：東北角折線攔截 (避開切過雪山山脈)
    if has_west and has_east and lat1 > 23.5 and lat2 > 23.5:
        base_dist = straight_dist(lon1, lat1, CORNER_NE[0], CORNER_NE[1]) + \
                    straight_dist(CORNER_NE[0], CORNER_NE[1], lon2, lat2)
                    
        # 找出位在西部的那個車站的緯度
        is_l1_west = line1 == "WEST" or line1 == "MOUNTAIN"
        west_lat = lat1 if is_l1_west else lat2
        
        # 如果西部車站是在苗栗/新竹以南 (例如台中、彰化)
        # 代表火車必須繞過整個北部海岸形成巨大 U 型彎，給予極高補償！
        if west_lat < 24.6:
            return base_dist * 1.42
            
        return base_dist * 1.15 
        
    # 魔法 2：南迴角折線攔截 (避開切過中央山脈南段)
    elif has_west and has_east and lat1 < 23.5 and lat2 < 23.5:
        base_dist = straight_dist(lon1, lat1, CORNER_SOUTH[0], CORNER_SOUTH[1]) + \
                    straight_dist(CORNER_SOUTH[0], CORNER_SOUTH[1], lon2, lat2)
        return base_dist * 1.15
        
    # 🟢 正常直線：都在同一側，沒有跨越中央山脈的問題
    else:
        base_dist = straight_dist(lon1, lat1, lon2, lat2)
        
        # 1. 特殊山區與南迴微調
        if "SOUTH_LINK" in lines: return base_dist * 1.30 
        if "MOUNTAIN" in lines: return base_dist * 1.25   
        
        # 2. 東部專屬邏輯 (利用 not has_west 確保沒跨越中央山脈)
        if "EAST_VALLEY" in lines and not has_west:
            return base_dist * 1.05
        if "NORTH_LINK" in lines and not has_west:
            return base_dist * 1.10
        
        # 3. 西部幹線「距離分級」動態補償 (西海岸弓弦效應)
        if base_dist >= 150:
            return base_dist * 1.27
        elif base_dist >= 80:
            return base_dist * 1.24
        else:
            return base_dist * 1.20

# ==========================================
# 3. 票價模型：里程分段折扣法 (主攻)
# ==========================================
# ==========================================
# 3. 票價模型：里程分段累進折扣法 (所得稅級距概念)
# ==========================================
def estimate_tra_cost(distance_km, train_type_name):
    """依照台鐵官方里程，採取「分段累進」計算法算錢"""
    if '自強' in train_type_name or '普悠瑪' in train_type_name or '太魯閣' in train_type_name:
        base_rate = 3.39
    elif '莒光' in train_type_name:
        base_rate = 2.61
    else:  
        base_rate = 2.18

    fare = 0.0
    rem_dist = distance_km

    # 第 1 級距: 0~50 公里 (原價)
    if rem_dist > 0:
        tier_dist = min(rem_dist, 50)
        fare += tier_dist * base_rate
        rem_dist -= tier_dist

    # 第 2 級距: 50~100 公里 (88折)
    if rem_dist > 0:
        tier_dist = min(rem_dist, 50)
        fare += tier_dist * (base_rate * 0.88)
        rem_dist -= tier_dist

    # 第 3 級距: 100~200 公里 (83折)
    if rem_dist > 0:
        tier_dist = min(rem_dist, 100)
        fare += tier_dist * (base_rate * 0.83)
        rem_dist -= tier_dist

    # 第 4 級距: 200~300 公里 (70折)
    if rem_dist > 0:
        tier_dist = min(rem_dist, 100)
        fare += tier_dist * (base_rate * 0.70)
        rem_dist -= tier_dist

    # 第 5 級距: 300 公里以上 (65折)
    if rem_dist > 0:
        fare += rem_dist * (base_rate * 0.65)

    return max(22, int(fare)) # 台鐵起步價 22 元

# ==========================================
# 4. 票價模型：時間換算法 (API 缺座標時的防呆備用)
# ==========================================
def estimate_tra_cost_by_time(travel_mins, train_type_name):
    """時間算錢法"""
    if '自強' in train_type_name or '普悠瑪' in train_type_name or '太魯閣' in train_type_name:
        rate_per_min = 3.15
    elif '莒光' in train_type_name:
        rate_per_min = 2.40
    else:
        rate_per_min = 1.45
    return max(22, int(travel_mins * rate_per_min))
# ==========================================
# 工具函式：高鐵官方票價查表 (標準車廂全票)
# ==========================================
THSR_STATIONS = ["南港", "台北", "板橋", "桃園", "新竹", "苗栗", "台中", "彰化", "雲林", "嘉義", "台南", "左營"]
THSR_FARE_MATRIX = [
    [0, 40, 70, 200, 330, 480, 750, 870, 970, 1120, 1390, 1530],
    [40, 0, 40, 160, 290, 430, 700, 820, 930, 1080, 1350, 1490],
    [70, 40, 0, 130, 260, 400, 670, 790, 890, 1050, 1320, 1460],
    [200, 160, 130, 0, 130, 280, 540, 670, 780, 920, 1190, 1330],
    [330, 290, 260, 130, 0, 140, 410, 540, 640, 790, 1060, 1200],
    [480, 430, 400, 280, 140, 0, 270, 400, 500, 640, 920, 1060],
    [750, 700, 670, 540, 410, 270, 0, 130, 230, 380, 650, 790],
    [870, 820, 790, 670, 540, 400, 130, 0, 110, 250, 530, 670],
    [970, 930, 890, 780, 640, 500, 230, 110, 0, 150, 420, 560],
    [1120, 1080, 1050, 920, 790, 640, 380, 250, 150, 0, 280, 410],
    [1390, 1350, 1320, 1190, 1060, 920, 650, 530, 420, 280, 0, 140],
    [1530, 1490, 1460, 1330, 1200, 1060, 790, 670, 560, 410, 140, 0]
]

def get_thsr_fare(name_from, name_to):
    """利用站名查表，回傳精準的高鐵官方票價"""
    try:
        n_from = name_from.replace("THSR_", "").strip()
        n_to = name_to.replace("THSR_", "").strip()
        idx1 = THSR_STATIONS.index(n_from)
        idx2 = THSR_STATIONS.index(n_to)
        return THSR_FARE_MATRIX[idx1][idx2]
    except ValueError:
        return 9999 # 防呆機制

# ==========================================
# 工具函式：智慧解析使用者輸入的站名
# ==========================================
def parse_station_input(user_input):
    """
    將「台北火車站」、「高鐵台中站」等自然語言轉換為系統格式 (TRA_台北, THSR_台中)
    """
    # 1. 正規化：統一把「臺」換成「台」，並去除頭尾空白
    s = user_input.replace('臺', '台').strip()
    
    if not s: return ""
    
    # 2. 判斷系統：只要有「高鐵」或「thsr」，就是高鐵，否則預設為台鐵
    is_thsr = '高鐵' in s or 'thsr' in s.lower()
    
    # 3. 拔除贅字，萃取出純站名 (例如 "台北火車站" -> "台北")
    remove_keywords = ['火車站', '高鐵站', '車站', '火車', '高鐵', '台鐵', '站']
    for keyword in remove_keywords:
        s = s.replace(keyword, '')
        
    # 4. 組裝成系統需要的內部格式
    if is_thsr:
        return f"THSR_{s}"
    else:
        return f"TRA_{s}"

# ==========================================
# 工具函式：同車次路徑智慧壓縮合併
# ==========================================
def compress_path(raw_path):
    if not raw_path: return [], 0
    compressed = []
    curr = raw_path[0].copy()
    
    for step in raw_path[1:]:
        if not step['is_transfer'] and not curr['is_transfer'] and step['train_no'] == curr['train_no']:
            curr['to_st'] = step['to_st']           # 目的地往後延
            curr['arr_time'] = step['arr_time']     # 抵達時間往後延
            # 這裡不累加 cost，因為我們等一下會用最終的首尾站重算
        else:
            compressed.append(curr)
            curr = step.copy()
    compressed.append(curr)
    
    formatted = []
    real_total_cost = 0 # 記錄精算後的全程真實總花費
    
    for leg in compressed:
        sys_from, code_from = leg['from_st'].split('_')
        sys_to, code_to = leg['to_st'].split('_')
        name_from = get_station_name(sys_from, code_from)
        name_to = get_station_name(sys_to, code_to)
        
        # 🔴 核心後處理：如果是台鐵，用合併後真正的起訖站重算一次精準票價
        if not leg['is_transfer'] and sys_from == 'TRA':
            c_from = STATION_COORDS.get(leg['from_st'])
            c_to = STATION_COORDS.get(leg['to_st'])
            if c_from and c_to:
                line1, line2 = find_line(name_from.replace("TRA_", "")), find_line(name_to.replace("TRA_", ""))
                dist_km = calculate_haversine_distance(c_from[0], c_from[1], c_to[0], c_to[1], line1, line2)
                leg['cost'] = estimate_tra_cost(dist_km, leg['train_name'])
            else:
                travel_mins = max(1, leg['arr_time'] - leg['dep_time'])
                leg['cost'] = estimate_tra_cost_by_time(travel_mins, leg['train_name'])
                
        real_total_cost += int(leg['cost'])

        if leg['is_transfer']:
            walk_m = leg['arr_time'] - leg['dep_time']
            t_mode = leg.get('trans_mode', 'WALK')
            mode_str = "👣 站內步行" if t_mode == 'WALK' else ("🚌 公車/接駁" if t_mode == 'BUS' else "🚕 計程車")
            formatted.append(f"{mode_str} 至 {name_to} (耗時 {walk_m}分, ${int(leg['cost'])})")
        else:
            t_dep, t_arr = min_to_time(leg['dep_time']), min_to_time(leg['arr_time'])
            train_name = leg.get('train_name') or leg['sys']
            formatted.append(f"🚆 搭乘 {train_name} [{leg['train_no']}] : {name_from} -> {name_to} ({t_dep} -> {t_arr}, 區間花費 ${int(leg['cost'])})")
    
    return formatted, real_total_cost

# ==========================================
# 1. TDX API 拉取模組 (新增全台車站抓取)
# ==========================================
class TDXService:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = None
        self.base_url = "https://tdx.transportdata.tw/api/basic"
        # 💡 新增：即時延誤的快取機制
        self._delay_cache = {}
        self._delay_last_update = 0  # 上次更新的時間戳記
        self._CACHE_TTL = 60         # 資料存活時間：設定 60 秒
        self._get_token()

    def _get_token(self):
        auth_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
        headers = {'content-type': 'application/x-www-form-urlencoded'}
        data = {'grant_type': 'client_credentials', 'client_id': self.client_id, 'client_secret': self.client_secret}
        response = requests.post(auth_url, headers=headers, data=data)
        if response.status_code == 200:
            self.access_token = response.json().get('access_token')
            print("✅ TDX 授權成功！")
        else:
            print("⚠️ TDX 授權失敗，請檢查金鑰。")

    def _get_headers(self):
        return {'Authorization': f'Bearer {self.access_token}', 'Accept': 'application/json'}

    def fetch_all_stations(self):
        """核心升級一：動態抓取全台灣所有台鐵與高鐵車站清單 (包含前端需要的經緯度)"""
        # 💡 把 STATION_COORDS 加進來 global 宣告
        global STATION_MAP, REVERSE_MAP, STATION_COORDS
        if not self.access_token: return
        
        headers = self._get_headers()
        print("[系統背景] 正在從 TDX 抓取全台車站清單 (包含座標資料)...")

        # 1. 抓取台鐵車站
        try:
            res_tra = requests.get(f"{self.base_url}/v3/Rail/TRA/Station", headers=headers)
            if res_tra.status_code == 200:
                for stat in res_tra.json().get('Stations', []):
                    name = stat['StationName']['Zh_tw'].replace('臺', '台')
                    st_id = stat['StationID']
                    
                    # 💡 抓取經緯度
                    lon = stat['StationPosition']['PositionLon']
                    lat = stat['StationPosition']['PositionLat']
                    
                    STATION_MAP[f"TRA_{name}"] = st_id
                    REVERSE_MAP[f"TRA_{st_id}"] = f"TRA_{name}"
                    
                    # 💡 把代碼跟站名當作 Key 都存一份座標，前端怎麼查都方便
                    STATION_COORDS[f"TRA_{st_id}"] = (lon, lat)
                    STATION_COORDS[f"TRA_{name}"] = (lon, lat)
            else:
                print(f"⚠️ 台鐵 API 異常，狀態碼: {res_tra.status_code}")
        except Exception as e: print(f"台鐵車站拉取失敗: {e}")

        # 2. 抓取高鐵車站
        try:
            res_thsr = requests.get(f"{self.base_url}/v2/Rail/THSR/Station", headers=headers)
            if res_thsr.status_code == 200:
                for stat in res_thsr.json():
                    name = stat['StationName']['Zh_tw'].replace('臺', '台')
                    st_id = stat['StationID']
                    
                    # 💡 抓取經緯度
                    lon = stat['StationPosition']['PositionLon']
                    lat = stat['StationPosition']['PositionLat']
                    
                    STATION_MAP[f"THSR_{name}"] = st_id
                    REVERSE_MAP[f"THSR_{st_id}"] = f"THSR_{name}"
                    
                    # 💡 同理，存入高鐵座標
                    STATION_COORDS[f"THSR_{st_id}"] = (lon, lat)
                    STATION_COORDS[f"THSR_{name}"] = (lon, lat)
            else:
                print(f"⚠️ 高鐵 API 異常，狀態碼: {res_thsr.status_code}")
        except Exception as e: print(f"高鐵車站拉取失敗: {e}")

        print(f"  -> 成功載入 {len(STATION_MAP)} 個車站及其座標")
    def fetch_rail_shapes(self):
        """上策火力展示：抓取 TDX 真實軌道線型 (Shape)"""
        global RAIL_SHAPES
        if not self.access_token: return
        headers = self._get_headers()
        print("[系統背景] 正在從 TDX 抓取真實軌道線型資料(Shape)...")
        
        try:
            # 抓取高鐵真實幾何軌跡
            res_thsr = requests.get(f"{self.base_url}/v2/Rail/THSR/Shape?$format=JSON", headers=headers)
            if res_thsr.status_code == 200:
                shapes = res_thsr.json()
                if shapes:
                    # 取第一筆當作高鐵主線 Geometry (LINESTRING格式)
                    RAIL_SHAPES["THSR"] = shapes[0].get('Geometry', '')
                    print("  -> 成功取得高鐵真實物理軌跡！")
        except Exception as e: 
            print(f"⚠️ 高鐵線型抓取失敗: {e}")
    def fetch_daily_all_schedules(self, target_date):
        if not self.access_token: return [], []
        headers = self._get_headers()
        tra_data, thsr_data = [], []

        print(f"[API 請求] 正在抓取 {target_date} 全台鐵路班表 (這包資料很大，請稍候)...")
        
        res_tra = requests.get(f"{self.base_url}/v3/Rail/TRA/DailyTrainTimetable/TrainDate/{target_date}", headers=headers)
        if res_tra.status_code == 200:
            tra_data = res_tra.json().get('TrainTimetables', [])
            print(f"  -> 成功取得台鐵共 {len(tra_data)} 個車次資料。")

        res_thsr = requests.get(f"{self.base_url}/v2/Rail/THSR/DailyTimetable/TrainDate/{target_date}", headers=headers)
        if res_thsr.status_code == 200:
            thsr_data = res_thsr.json()
            print(f"  -> 成功取得高鐵共 {len(thsr_data)} 個車次資料。")

        return tra_data, thsr_data

    def fetch_realtime_delay(self):
        if not self.access_token: return {}
        import time
        current_time = time.time()
        
        # 1. 檢查快取：如果距離上次抓取還不到 60 秒，直接回傳記憶體裡的舊資料！
        if current_time - self._delay_last_update < self._CACHE_TTL:
            return self._delay_cache
            
        # 🚀 神級修復：不管等一下成功還是失敗，先強制把時間戳記更新！
        # 這樣就算拿到 429，系統也會乖乖閉嘴 60 秒，不會再去砲轟 TDX
        self._delay_last_update = current_time
        
        delays = {}
        try:
            # 💡 核心修復：換成正確的 TDX v2 即時延誤 API 網址
            url = f"{self.base_url}/v2/Rail/TRA/LiveTrainDelay?$format=JSON"
            res = requests.get(url, headers=self._get_headers())
            
            if res.status_code == 200:
                data = res.json()
                delayed_count = 0
                
                # v2 的回傳結果直接是一個陣列 (List)，所以直接跑迴圈
                for t in data:
                    delay_m = t.get('DelayTime', 0)
                    delays[t['TrainNo']] = delay_m
                    if delay_m > 0:
                        delayed_count += 1

                # 💡 成功抓到新資料，更新快取與時間戳記
                self._delay_cache = delays
                self._delay_last_update = current_time
                
                if delayed_count > 0:
                    print(f"  [系統狀態] 成功連線！目前全台有 {delayed_count} 班台鐵列車發生真實誤點。")
                else:
                    print(f"  [系統狀態] 成功連線！目前全台台鐵列車皆準點行駛中！")
            else:
                print(f"⚠️ [延誤 API 錯誤] 狀態碼: {res.status_code}")
                
        except Exception as e: 
            pass # 靜默處理網路錯誤，不影響主程式運行
            
        return delays
    def calculate_taxi_fare(self, travel_time_minutes):
        """
        修正版：根據台灣計程車合理費率估算車資
        基本費：約 85 元（前 1.25 公里）
        續跳費率：每 200 公尺跳 5 元 ➡️ 換算每公里約 25 元
        延遲/停等費：時速低於 5km/h 時每分鐘約 5 元，這裡簡化為總時間的衍生常數
        假設市郊平均時速 40 km/h
        """
        # 估算行駛距離 (公里)
        estimated_distance_km = (travel_time_minutes / 60) * 40
        
        base_fare = 85
        base_distance = 1.25
        
        if estimated_distance_km <= base_distance:
            fare = base_fare
        else:
            # 超出基本里程的部分，每公里約 25 元
            fare = base_fare + int((estimated_distance_km - base_distance) * 25)
            
        # 紅綠燈停等/塞車費 (簡化模型：假設約 1/5 的時間在停等，每分鐘算 5 元)
        waiting_fee = int((travel_time_minutes / 5) * 5)
        
        return fare + waiting_fee

    def calculate_bus_fare(self, travel_time_minutes):
        """
        根據台灣公車標準費率估算票價
        基本票價：25元（前5公里），超過部分每公里1.5元
        假設平均時速 20 km/h
        """
        estimated_distance_km = (travel_time_minutes / 60) * 20
        if estimated_distance_km <= 5:
            return 25
        return 25 + int((estimated_distance_km - 5) * 1.5)

# ==========================================
# 2. 圖論網路模型建構
# ==========================================
class TransitGraph:
    def __init__(self):
        self.edges = defaultdict(list)

    def add_transit_edge(self, dep_st, arr_st, dep_t, arr_t, cost, train_no, sys_type, train_name=""):
        self.edges[dep_st].append({
            'to': arr_st, 
            'dep_time': dep_t, 
            'arr_time': arr_t, 
            'orig_dep': dep_t, 
            'orig_arr': arr_t,
            'dep_mod': dep_t % 1440,  # ⚡ 優化：預先算好餘數快取
            'arr_mod': arr_t % 1440,  # ⚡ 優化：預先算好餘數快取
            'cost': cost, 'train_no': train_no, 'type': sys_type, 
            'train_name': train_name, 
            'is_transfer': False
        })

    # 支援 trans_mode (公車/計程車) 的版本
    def add_transfer_edge(self, st_a, st_b, walk_time_min, cost=0, trans_mode="WALK"):
        transfer_data = {'walk_time': walk_time_min, 'cost': cost, 'type': 'TRANSFER', 'is_transfer': True, 'trans_mode': trans_mode}
        self.edges[st_a].append({'to': st_b, **transfer_data})
        self.edges[st_b].append({'to': st_a, **transfer_data})

    def apply_realtime_delays(self, delays):
        count = 0
        for u in self.edges:
            for edge in self.edges[u]:
                if not edge['is_transfer']:
                    edge['dep_time'] = edge.get('orig_dep', edge['dep_time'])
                    edge['arr_time'] = edge.get('orig_arr', edge['arr_time'])
                    edge['delay_mins'] = 0 
                    
                    if edge['train_no'] in delays:
                        delay_m = delays[edge['train_no']]
                        if delay_m > 0:
                            edge['arr_time'] += delay_m
                            edge['dep_time'] += delay_m 
                            edge['delay_mins'] = delay_m
                            count += 1
                            
                    # ⚡ 優化：不管有沒有延誤，都在這裡一併更新快取，免除內圈計算
                    edge['dep_mod'] = edge['dep_time'] % 1440
                    edge['arr_mod'] = edge['arr_time'] % 1440
        return count

# ==========================================
# 3. 帕雷托最佳化演算法核心 (Numba 極速外掛版)
# ==========================================
from numba import njit
import numpy as np

# 💡 將判斷邏輯抽離成「純數字」的獨立函式，餵給 Numba 吃
@njit
def is_dominated_fast(new_time, new_cost, new_transfers, new_dep, pareto_front_array):
    # 確保陣列不是空的才能比對
    if pareto_front_array.shape[0] == 0:
        return False
        
    for i in range(pareto_front_array.shape[0]):
        pt_time = pareto_front_array[i, 0]
        pt_cost = pareto_front_array[i, 1]
        pt_transfers = pareto_front_array[i, 2]
        pt_dep = pareto_front_array[i, 3]
        
        # Numba 處理這段數字比對的速度是 Python 原生的數十倍
        if pt_time <= new_time and pt_cost <= new_cost and pt_transfers <= new_transfers and pt_dep >= new_dep: 
            return True
    return False

class ParetoRouter:
    def __init__(self, graph):
        self.graph = graph

    def find_routes(self, start_id, end_id, start_time_str, max_budget, trans_pref="ALL", role_id="6", latest_time_str=""):
        start_m = time_to_min(start_time_str)
        latest_m = time_to_min(latest_time_str) if latest_time_str else 99999
        if latest_m < start_m and latest_time_str:
            latest_m += 1440 
        tie_breaker = itertools.count()

        # 🚀 A* 幾何剪枝引擎
        end_coord = STATION_COORDS.get(end_id)
        
        def get_min_time_heuristic(curr_st_id):
            if not end_coord: return 0
            curr_coord = STATION_COORDS.get(curr_st_id)
            if not curr_coord: return 0
            dist_km = math.sqrt(((curr_coord[1] - end_coord[1]) * 111)**2 + ((curr_coord[0] - end_coord[0]) * 100)**2)
            return dist_km / 5.0

        pq = [(start_m, 0, 0, start_m, next(tie_breaker), start_id, None, False, True, "", "")]
        pareto_fronts = defaultdict(list)
        pareto_fronts[start_id].append((start_m, 0, 0, start_m))
        valid_routes = []
        global_best_targets = []
        
        while pq:
            curr_time, curr_cost, curr_transfers, first_dep, _, u, path_node, has_main_train, prev_is_transfer, prev_train, prev_sys = heapq.heappop(pq)

            u_min_left = get_min_time_heuristic(u)
            is_dead_end = False
            for pt_time, pt_cost, pt_transfers, pt_dep in global_best_targets:
                if (curr_time + u_min_left) >= pt_time and curr_cost >= pt_cost and curr_transfers >= pt_transfers and first_dep <= pt_dep:
                    is_dead_end = True
                    break
            if is_dead_end: continue
            
            if u == end_id:
                real_path = []
                curr_node = path_node
                while curr_node is not None:
                    curr_node, step = curr_node
                    real_path.append(step.copy())
                real_path.reverse()
                
                valid_routes.append((curr_time, curr_cost, real_path))
                global_best_targets.append((curr_time, curr_cost, curr_transfers, first_dep))
                continue
            # ==========================================
            # 💡 [新增] 動態發車窗格：預先找出起點站的「第一班車」時間
            # ==========================================
            earliest_dep = curr_time
            if path_node is None:
                earliest_candidate = 9999999
                for e in self.graph.edges[u]:
                    if not e['is_transfer']:
                        e_dep = (curr_time // 1440 * 1440) + e['dep_mod']
                        if e_dep < curr_time: 
                            e_dep += 1440
                        if e_dep < earliest_candidate:
                            earliest_candidate = e_dep
                            
                if earliest_candidate != 9999999:
                    earliest_dep = earliest_candidate
            # ==========================================
            for edge in self.graph.edges[u]:
                is_new_transfer = 0
                if path_node is not None:
                    if edge['is_transfer'] or (not prev_is_transfer and prev_train != edge['train_no']):
                        is_new_transfer = 1
                next_transfers = curr_transfers + is_new_transfer
                v = edge['to']
                
                if edge['is_transfer']:
                    if edge['trans_mode'] != "WALK" and trans_pref != "ALL":
                        if edge['trans_mode'] != trans_pref:
                            continue 
                    
                    actual_walk_time = edge['walk_time']
                    if edge['trans_mode'] == "WALK" and role_id in ['1', '4', '5']:
                        actual_walk_time = math.ceil(edge['walk_time'] * 1.5)
                            
                    next_t = curr_time + actual_walk_time
                    actual_cost = get_discounted_cost(edge['cost'], 'TRANSFER', edge['trans_mode'], role_id)
                    next_c = curr_cost + actual_cost
                    
                    step_info = {'is_transfer': True, 'sys': 'TRANSFER', 'train_no': '', 'trans_mode': edge['trans_mode'],
                                 'from_st': u, 'to_st': v, 'dep_time': curr_time, 'arr_time': next_t, 'cost': actual_cost,'delay_mins': edge.get('delay_mins', 0) }
                    
                    next_has_main = has_main_train
                    next_prev_is_transfer = True
                    next_prev_train = ""
                    next_prev_sys = "TRANSFER"

                else:
                    edge_dep_mod = edge['dep_mod']
                    edge_arr_mod = edge['arr_mod']
                    
                    days_curr = curr_time // 1440
                    base_time = days_curr * 1440
                    
                    logical_dep = base_time + edge_dep_mod
                    if logical_dep < curr_time:
                        logical_dep += 1440 
                        
                    if logical_dep - curr_time > 1440: 
                        continue

                    logical_arr = base_time + edge_arr_mod
                    while logical_arr < logical_dep:
                        logical_arr += 1440 

                    if not has_main_train and logical_dep > latest_m:
                        continue 

                    wait_time = logical_dep - curr_time
                    
                    # 節點情境式剪枝 (Context-Aware Pruning)
                    if path_node is None:
                        # 限制只尋找起點站「第一班車」之後 4 小時 (240分鐘) 內發車的方案
                        if logical_dep > earliest_dep + 240: 
                            continue
                        # 保留原版絕對死線防呆 (最多等16小時)
                        if wait_time > 960: 
                            continue
                    else:
                        if wait_time > 240: 
                            continue
                        # 同系統轉乘的防呆緩衝 (換車至少要留時間走過去)
                        if not prev_is_transfer and prev_sys == edge['type'] and prev_train != edge['train_no']:
                            if wait_time < 5: 
                                continue

                    next_t = logical_arr
                    actual_cost = get_discounted_cost(edge['cost'], edge['type'], None, role_id)
                    next_c = curr_cost + actual_cost
                    
                    step_info = {'is_transfer': False, 'sys': edge['type'], 'train_no': edge['train_no'], 
                                 'train_name': edge.get('train_name', ''), 
                                 'from_st': u, 'to_st': v, 'dep_time': logical_dep, 'arr_time': logical_arr, 
                                 'cost': actual_cost, 'delay_mins': edge.get('delay_mins', 0) }
                    
                    next_has_main = True
                    next_prev_is_transfer = False
                    next_prev_train = edge['train_no']
                    next_prev_sys = edge['type']

                if next_c > max_budget * 1.5: continue
                
                next_dep = first_dep
                if not has_main_train and not edge['is_transfer']:
                    next_dep = logical_dep 

                v_min_left = get_min_time_heuristic(v)
                is_next_dead_end = False
                for pt_time, pt_cost, pt_transfers, pt_dep in global_best_targets:
                    if (next_t + v_min_left) >= pt_time and next_c >= pt_cost and next_transfers >= pt_transfers and next_dep <= pt_dep:
                        is_next_dead_end = True
                        break
                if is_next_dead_end: continue 

                # ==========================================
                # 🚀 核心優化：高效純 Python 帕雷托比對 (移除 Numpy 記憶體配置)
                # ==========================================
                is_dominated = False
                for pt_time, pt_cost, pt_transfers, pt_dep in pareto_fronts[v]:
                    if pt_time <= next_t and pt_cost <= next_c and pt_transfers <= next_transfers and pt_dep >= next_dep:
                        is_dominated = True
                        break
                
                if not is_dominated:
                    # 反向過濾掉被新方案支配的舊方案
                    pareto_fronts[v] = [(t, c, tr, d) for t, c, tr, d in pareto_fronts[v] 
                                        if not (next_t <= t and next_c <= c and next_transfers <= tr and next_dep >= d)]
                    
                    pareto_fronts[v].append((next_t, next_c, next_transfers, next_dep))
                    next_path_node = (path_node, step_info)
                    
                    heapq.heappush(pq, (next_t, next_c, next_transfers, next_dep, next(tie_breaker), v, next_path_node, next_has_main, next_prev_is_transfer, next_prev_train, next_prev_sys))

        final_pareto = []
        for r_time, r_cost, r_path in valid_routes:
            if not r_path: continue

            first_train_idx = -1
            for i, step in enumerate(r_path):
                if not step['is_transfer']:
                    first_train_idx = i
                    break
            
            if first_train_idx > 0:
                next_req_time = r_path[first_train_idx]['dep_time']
                for i in range(first_train_idx - 1, -1, -1):
                    duration = r_path[i]['arr_time'] - r_path[i]['dep_time']
                    r_path[i]['arr_time'] = next_req_time
                    r_path[i]['dep_time'] = next_req_time - duration
                    next_req_time = r_path[i]['dep_time']

            r_duration = r_time - r_path[0]['dep_time']
            if r_duration < 0: r_duration += 1440  
            
            is_dom = False
            for ft, fc, fd, fp in final_pareto:
                if ft <= r_time and fc <= r_cost and fd <= r_duration:
                    is_dom = True
                    break
            
            if not is_dom:
                final_pareto = [(ft, fc, fd, fp) for ft, fc, fd, fp in final_pareto 
                                if not (r_time <= ft and r_cost <= fc and r_duration <= fd)]
                final_pareto.append((r_time, r_cost, r_duration, r_path))

        return [(ft, fc, fp) for ft, fc, fd, fp in final_pareto]
# ==========================================
# 4. 系統控制器
# ==========================================
class SmartTransferSystem:
    def __init__(self, client_id, client_secret):
        self.tdx = TDXService(client_id, client_secret)
        self.graph = TransitGraph()

    def daily_initialization(self):
        # 💡 新增：啟動時先抓取全台車站，建立對應表
        self.tdx.fetch_all_stations()
        
        # 💡 [上策] 新增：抓取真實地理軌道線型
        self.tdx.fetch_rail_shapes()
        
        
        print("\n[系統背景] 執行每日全台靜態班表預載解析...")
        today = datetime.now().strftime('%Y-%m-%d')
        tra_data, thsr_data = self.tdx.fetch_daily_all_schedules(today)
        
        edge_count = 0
        for train in tra_data:
            t_info = train['TrainInfo']
            stops = train['StopTimes']
            raw_train_name = t_info.get('TrainTypeName', {}).get('Zh_tw', '台鐵')
            train_type_name = simplify_train_name(raw_train_name)

            for i in range(len(stops) - 1):
                dep_info = stops[i]
                arr_info = stops[i+1] # 🔴 只看下一站，不再跑內層迴圈！
                
                dep_id_num = dep_info['StationID']
                arr_id_num = arr_info['StationID']
                dep_st = f"TRA_{dep_id_num}"
                arr_st = f"TRA_{arr_id_num}"
                
                dep_m = time_to_min(dep_info['DepartureTime'])
                arr_m = time_to_min(arr_info['ArrivalTime'])
                
                if arr_m < dep_m: arr_m += 1440 
                travel_mins = max(1, arr_m - dep_m)

                if travel_mins > 240: continue

                coord_from = STATION_COORDS.get(f"TRA_{dep_id_num}")
                coord_to = STATION_COORDS.get(f"TRA_{arr_id_num}")
                
                # 🔴 為了避免 O(N) 累加 22 元起步價的災難，我們在這裡「不套用」起步價與打折
                # 而是算出一個「純粹的每站微小距離成本」，等演算法找出路線後再重新精算總價！
                if coord_from and coord_to:
                    name_from = REVERSE_MAP.get(f"TRA_{dep_id_num}", "").replace("TRA_", "")
                    name_to = REVERSE_MAP.get(f"TRA_{arr_id_num}", "").replace("TRA_", "")
                    line1 = find_line(name_from)
                    line2 = find_line(name_to)
                    dist_km = calculate_haversine_distance(coord_from[0], coord_from[1], coord_to[0], coord_to[1], line1, line2)
                    
                    if '自強' in train_type_name or '普悠瑪' in train_type_name or '太魯閣' in train_type_name:
                        proxy_cost = dist_km * 3.39
                    elif '莒光' in train_type_name:
                        proxy_cost = dist_km * 2.61
                    else:  
                        proxy_cost = dist_km * 2.18
                else:
                    proxy_cost = travel_mins * 2.0 

                edge_count += 1
                self.graph.add_transit_edge(dep_st, arr_st, dep_m, arr_m, proxy_cost, t_info['TrainNo'], "TRA", train_type_name)
        # 處理高鐵
        for train in thsr_data:
            t_no = train['DailyTrainInfo']['TrainNo']
            stops = train['StopTimes']
            rate_per_min = 14.5 

            for i in range(len(stops) - 1):
                dep_st = f"THSR_{stops[i]['StationID']}"
                arr_st = f"THSR_{stops[i+1]['StationID']}"
                dep_m = time_to_min(stops[i]['DepartureTime'])
                arr_m = time_to_min(stops[i+1]['ArrivalTime'])
                if arr_m < dep_m: arr_m += 1440
                # 🔴 神級修復：用真實站名去查表，取代原本的時間推算法
                name_from = REVERSE_MAP.get(dep_st, "").replace("THSR_", "")
                name_to = REVERSE_MAP.get(arr_st, "").replace("THSR_", "")
                est_cost = get_thsr_fare(name_from, name_to)
                
                edge_count += 1
                self.graph.add_transit_edge(dep_st, arr_st, dep_m, arr_m, est_cost, t_no, "THSR", "高鐵")

        # 共構站邊界 (動態對照表)
        # 共構站邊界 (站內步行)
        # ==========================================
        # 建立站際轉乘邊界 (Edges)
        # ==========================================
        try:
            # 1. 共構站邊界 (站內步行，距離短、免費)
            # 這裡對應的都是站內走路 10~20 分鐘可以到的
            self.graph.add_transfer_edge(f"TRA_{STATION_MAP['TRA_台北']}", f"THSR_{STATION_MAP['THSR_台北']}", walk_time_min=10, cost=0, trans_mode="WALK")
            self.graph.add_transfer_edge(f"TRA_{STATION_MAP['TRA_南港']}", f"THSR_{STATION_MAP['THSR_南港']}", walk_time_min=10, cost=0, trans_mode="WALK")
            self.graph.add_transfer_edge(f"TRA_{STATION_MAP['TRA_板橋']}", f"THSR_{STATION_MAP['THSR_板橋']}", walk_time_min=10, cost=0, trans_mode="WALK")
            self.graph.add_transfer_edge(f"TRA_{STATION_MAP['TRA_新烏日']}", f"THSR_{STATION_MAP['THSR_台中']}", walk_time_min=10, cost=0, trans_mode="WALK")
            self.graph.add_transfer_edge(f"TRA_{STATION_MAP['TRA_沙崙']}", f"THSR_{STATION_MAP['THSR_台南']}", walk_time_min=15, cost=0, trans_mode="WALK")
            self.graph.add_transfer_edge(f"TRA_{STATION_MAP['TRA_六家']}", f"THSR_{STATION_MAP['THSR_新竹']}", walk_time_min=10, cost=0, trans_mode="WALK")
            self.graph.add_transfer_edge(f"TRA_{STATION_MAP['TRA_豐富']}", f"THSR_{STATION_MAP['THSR_苗栗']}", walk_time_min=10, cost=0, trans_mode="WALK")
            self.graph.add_transfer_edge(f"TRA_{STATION_MAP['TRA_新左營']}", f"THSR_{STATION_MAP['THSR_左營']}", walk_time_min=10, cost=0, trans_mode="WALK")

            # 2. 非共構站接駁清單 (需要搭公車或計程車)
            # 格式：(台鐵站名, 高鐵站名, 公車耗時, 計程車耗時)
            # 價格改為公式計算，而非硬編碼
            non_co_stations = [
                ('TRA_嘉義', 'THSR_嘉義', 40, 26),
                ('TRA_桃園', 'THSR_桃園', 40, 20), 
                ('TRA_員林', 'THSR_彰化', 45, 20),
                ('TRA_田中', 'THSR_彰化', 12,  8),
                ('TRA_斗六', 'THSR_雲林', 45, 25),
            ]

            for tra_name, thsr_name, bus_time, taxi_time in non_co_stations:
                if tra_name in STATION_MAP and thsr_name in STATION_MAP:
                    tra_id = f"TRA_{STATION_MAP[tra_name]}"
                    thsr_id = f"THSR_{STATION_MAP[thsr_name]}"
                    
                    bus_cost = self.tdx.calculate_bus_fare(bus_time)
                    taxi_cost = self.tdx.calculate_taxi_fare(taxi_time)
                    
                    # 選項 A: 慢但便宜的接駁車
                    self.graph.add_transfer_edge(tra_id, thsr_id, walk_time_min=bus_time, cost=bus_cost, trans_mode="BUS")
                    # 選項 B: 快但昂貴的計程車
                    self.graph.add_transfer_edge(tra_id, thsr_id, walk_time_min=taxi_time, cost=taxi_cost, trans_mode="TAXI")

        except Exception as e:
            print(f"⚠️ 轉乘站點建立失敗，可能是名稱對應錯誤或 API 抓取遺漏: {e}")

        print(f"[系統背景] 預載解析完成！記憶體中已建立包含 {edge_count} 段站間連接的全台巨大圖論網路！")

    # 💡 核心修復：讓系統同時支援接收「站名(TRA_台北)」與「代碼(TRA_1000)」
    # 💡 核心修復：讓系統同時支援接收「站名」與「代碼」，並在輸出給前端前進行「票價精算與取整數」
    def handle_user_query(self, start_input, end_input, start_time, budget, trans_pref="ALL", role_id="6", latest_time=""):
        
        # 內部輔助函式：判斷傳入的是代碼還是站名
        def get_valid_id(query_str):
            if query_str in REVERSE_MAP: 
                return query_str 
            if query_str in STATION_MAP:
                sys_type = query_str.split('_')[0]
                return f"{sys_type}_{STATION_MAP[query_str]}" 
            return None

        start_id_str = get_valid_id(start_input)
        end_id_str = get_valid_id(end_input)

        if not start_id_str:
            return f"錯誤：找不到起點 '{start_input}'，請確認是否打錯字或代碼錯誤。"
        if not end_id_str:
            return f"錯誤：找不到終點 '{end_input}'，請確認是否打錯字或代碼錯誤。"

        start_cal_time = time.time()
        
        delays = self.tdx.fetch_realtime_delay()
        update_count = self.graph.apply_realtime_delays(delays)
        print(f"  -> 搜尋當下即時更新了 {update_count} 筆台鐵延誤資訊。")

        router = ParetoRouter(self.graph)
        results = router.find_routes(start_id_str, end_id_str, start_time, budget, trans_pref, role_id, latest_time)
        
        # ==========================================
        # 🔴 Web 前端資料清洗與票價精算 (Post-Processing)
        # 恢復 r_path 讓前端地圖能畫出沿途曲線，但把精算後的票價集中塞回第一站
        # ==========================================
        processed_results = []
        for r_time, r_cost, r_path in results:
            if not r_path: continue
            
            real_total_cost = 0
            
            # 用雙指標掃描 r_path，找出同一班直達車的「起點」與「終點」
            i = 0
            while i < len(r_path):
                start_step = r_path[i]
                
                if start_step['is_transfer']:
                    # 轉乘、走路、公車：直接取整數
                    start_step['cost'] = int(start_step['cost'])
                    real_total_cost += start_step['cost']
                    i += 1
                    continue
                
                # 往後找同一班車的最後一站
                j = i
                while j + 1 < len(r_path) and not r_path[j+1]['is_transfer'] and r_path[j+1]['train_no'] == start_step['train_no']:
                    j += 1
                
                end_step = r_path[j]
                sys_from = start_step['from_st'].split('_')[0]
                
                # 計算這段直達車的「真實總價」(起點到終點算一次里程打折)
                leg_cost = 0
                if sys_from == 'TRA':
                    name_from = REVERSE_MAP.get(start_step['from_st'], "").replace("TRA_", "")
                    name_to = REVERSE_MAP.get(end_step['to_st'], "").replace("TRA_", "")
                    c_from = STATION_COORDS.get(start_step['from_st'])
                    c_to = STATION_COORDS.get(end_step['to_st'])
                    
                    if c_from and c_to:
                        line1, line2 = find_line(name_from), find_line(name_to)
                        dist_km = calculate_haversine_distance(c_from[0], c_from[1], c_to[0], c_to[1], line1, line2)
                        leg_cost = estimate_tra_cost(dist_km, start_step['train_name'])
                    else:
                        travel_mins = max(1, end_step['arr_time'] - start_step['dep_time'])
                        leg_cost = estimate_tra_cost_by_time(travel_mins, start_step['train_name'])
                else:
                    # 🔴 神級修復：高鐵不再「直接取第一站整數」，而是抓出整條路線的首尾站去查表！
                    name_from = REVERSE_MAP.get(start_step['from_st'], "").replace("THSR_", "")
                    name_to = REVERSE_MAP.get(end_step['to_st'], "").replace("THSR_", "")
                    leg_cost = get_thsr_fare(name_from, name_to)
                
                # 🔴 魔法核心：把精算後的總價，全部算在這班車的「第一站」，後面的停靠站價格設為 0
                # 這樣前端 UI 加總時價格會完美精準，而且地圖也能拿到每一站的座標畫出完美曲線！
                start_step['cost'] = leg_cost
                for k in range(i + 1, j + 1):
                    r_path[k]['cost'] = 0
                    
                real_total_cost += leg_cost
                i = j + 1
                
            # 最終結帳嚴格審查
            if real_total_cost > budget:
                continue
                
            # 將修復好價格、保留完整路徑的 r_path 送給前端
            processed_results.append((r_time, real_total_cost, r_path))
            
        end_cal_time = time.time()
        print(f"  -> 圖論演算法遍歷運算耗時: {end_cal_time - start_cal_time:.4f} 秒")

        return processed_results

# ==========================================
# 主程式執行切入點 (CLI 互動版)
# ==========================================
if __name__ == "__main__":
    
    system = SmartTransferSystem(TDX_CLIENT_ID, TDX_CLIENT_SECRET)
    
    # 系統啟動，只會跑一次這段超久的預載
    system.daily_initialization()

    print("\n" + "="*50)
    print("智能轉乘系統啟動完畢，進入互動查詢模式")
    print("="*50)

    # 核心升級二：無限互動迴圈
    while True:
        print("\n--- 請輸入查詢條件 (輸入 q 可退出系統) ---")
        
        # 💡 提示文字變得更口語化了
        raw_start = input("出發站 (例如：台北火車站、左營高鐵站): ").strip()
        if raw_start.lower() == 'q': break
        
        raw_end = input("目的地 (例如：台南火車站、高鐵台中站): ").strip()
        if raw_end.lower() == 'q': break
        
        time_str = input("最快出發時間 (格式 13:45): ").strip()
        if time_str.lower() == 'q': break

        # 💡 新增：最晚出發時間的輸入
        latest_time_str = input("最晚出發時間 (格式 14:30，直接 Enter 代表不限制): ").strip()
        if latest_time_str.lower() == 'q': break
        
        # ==========================================
        # 🛡️ 💡 [新增] 時間邏輯防呆檢查
        # ==========================================
        if latest_time_str:
            start_m_check = time_to_min(time_str)
            latest_m_check = time_to_min(latest_time_str)
            
            # 如果最晚時間 < 最快時間
            if latest_m_check < start_m_check:
                # 判斷是否為「合理的跨夜搜尋」 (例如晚上 21:00 後出發，找隔天凌晨的車)
                if start_m_check >= 1260:  # 1260 = 21:00
                    pass # 允許這種跨夜設定
                else:
                    print(f"⚠️ 邏輯錯誤：最晚出發時間 ({latest_time_str}) 不能早於最快出發時間 ({time_str})！請重新輸入。")
                    continue # 直接中斷這回合，讓使用者重新輸入
        # ==========================================
        
        try:
            budget_limit = int(input("預算上限 (輸入數字，如 1500): ").strip())
        except ValueError:
            print("⚠️ 預算請輸入純數字！重新開始。")
            continue
        # 💡 新增：乘客身分選單
        print("\n   [乘客身分]")
        print("   1: 孩童 (12歲以下)")
        print("   2: 中小學生")
        print("   3: 大學生 (享高鐵專屬折扣)")
        print("   4: 老年人 (65歲以上)")
        print("   5: 身心障礙")
        print("   6: 一般成人 (無特殊優惠)")
        role_id = input("請選擇您的身分 (1-6) [預設6]: ").strip()
        if role_id not in ['1', '2', '3', '4', '5', '6']: 
            role_id = '6'


        # 💡 新增：讓使用者選擇非共構站的轉乘偏好
        pref_input = input("非共構站轉乘偏好 (1: 皆可/系統自動最佳化, 2: 僅大眾運輸, 3: 僅計程車) [預設1]: ").strip()
        trans_pref = "ALL"
        if pref_input == "2": trans_pref = "BUS"
        elif pref_input == "3": trans_pref = "TAXI"

        # 新增：讓使用者選擇排序規則
        sort_input = input("排序方式 (1: 價格優先, 2: 最早抵達優先, 3: 轉乘次數優先, 4: 總耗時最短優先) [預設1]: ").strip()
        sort_rule = "cost"
        if sort_input == "2":
            sort_rule = "time"
        elif sort_input == "3":
            sort_rule = "transfers"
        elif sort_input == "4":
            sort_rule = "duration"

        start_st = parse_station_input(raw_start)
        end_st = parse_station_input(raw_end)

        print(f"\n[系統解讀] 您的起點為 '{start_st}'，終點為 '{end_st}'")
        print("[系統運算中...]")
        
        # 💡 記得把 trans_pref 傳進去
        results = system.handle_user_query(start_st, end_st, time_str, budget_limit, trans_pref, role_id, latest_time_str)
        # 錯誤處理 (例如打錯車站名字)
        if isinstance(results, str):
            print(results)
            continue

        # 輸出結果
        if not results:
            print("❌ 找不到符合預算或時間的路線組合，請嘗試增加預算或調整出發時間。")
            continue

        def route_arrival_time(route):
            # 因為我們等一下會導入絕對時間軸，這裡可以直接回傳最終抵達時間
            arrival_time, _, _ = route
            return arrival_time 

        def route_duration(route):
            arrival_time, _, route_path = route
            # 用絕對時間相減，不會再出現負數
            return arrival_time - route_path[0]['dep_time']

        # 💡 [修改] 重新定義轉乘次數：將公車與計程車納入「搭乘段數」計算，步行則略過
        def route_transfers(route):
            _, _, route_path = route
            leg_count = 0  # 記錄總共搭了幾段交通工具
            prev_train = None
            
            for step in route_path:
                if not step['is_transfer']: 
                    # 情況 A：如果是搭火車/高鐵
                    if step['train_no'] != prev_train:
                        leg_count += 1
                        prev_train = step['train_no']
                else:
                    # 情況 B：遇到轉乘步驟 (步行、公車、計程車)
                    prev_train = None # 換車了，重置前一班車次紀錄
                    
                    # 💡 如果是搭公車或計程車，就算作獨立的一段「交通工具」
                    if step.get('trans_mode') in ['BUS', 'TAXI']:
                        leg_count += 1
                        
            # 轉乘次數 = 總搭乘段數 - 1
            return max(0, leg_count - 1)

        # 2. 執行正確的演算法排序
        if sort_rule == "time":
            sorted_results = sorted(results, key=route_arrival_time)
        elif sort_rule == "transfers":
            # 💡 [修改] 這裡改用剛寫好的 route_transfers 來精準計算車次轉乘
            sorted_results = sorted(results, key=route_transfers)
        elif sort_rule == "duration":
            sorted_results = sorted(results, key=route_duration)
        else:
            sorted_results = sorted(results, key=lambda x: x[1]) # 預設價格優先

        # 3. 準備並印出正確的「排序方式」名稱 (用字典對應，絕對不會印錯)
        sort_name_map = {
            "cost": "價格優先",
            "time": "最早抵達優先",
            "transfers": "轉乘次數優先",
            "duration": "總耗時最短優先"
        }
        
        print(f"\n[系統輸出] 為您找到 {len(results)} 種符合預算的最佳化路線方案：")
        print(f"[排序方式] {sort_name_map.get(sort_rule, '價格優先')}")

        # ==========================================
        # 💡 決策分析基準點：無條件尋找全場「總耗時最久」的方案
        # ==========================================
        baseline_cost = 0
        baseline_duration_mins = -1

        for r in results:
            r_arr = r[0]
            r_dep = r[2][0]['dep_time']
            r_dur = r_arr - r_dep
            if r_dur < 0: r_dur += 1440
            
            # 鎖定耗時最久的方案當作比較基準
            if r_dur > baseline_duration_mins:
                baseline_duration_mins = r_dur
                baseline_cost = r[1]

        # 開始印出每一筆結果
        # 開始印出每一筆結果
        for i, (total_time, proxy_cost, path) in enumerate(sorted_results): # 改叫 proxy_cost 避免搞混
            
            actual_start_m = path[0]['dep_time']
            duration_mins = total_time - actual_start_m
            if duration_mins < 0: 
                duration_mins += 1440 
            duration_str = format_duration(duration_mins)

            print(f"\n   推薦方案 {i + 1}")
            
            # 🔴 改用解包的方式，接住重算出來的真實票價 (real_total_cost)
            compressed_steps, real_total_cost = compress_path(path)
            
            transfer_count = route_transfers((total_time, proxy_cost, path))
            
            # 🔴 這裡顯示的總花費，改用 real_total_cost
            print(f"   總花費約: ${real_total_cost} | 預計抵達: {min_to_time(total_time)} (總搭乘耗時: {duration_str}) | 轉乘次數: {transfer_count}次")
            
            arr_time_of_day = total_time % 1440
            if arr_time_of_day >= 1380 or arr_time_of_day <= 300:
                print(f"   [深夜抵達警告] 抵達時可能已無接駁大眾運輸，請確認後續行程")
            
            print(f"   [路徑明細]")
            for step in compressed_steps:
                print(f"      {step}")
        print("\n" + "=" * 60)
