import os
import json
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from concurrent.futures import ThreadPoolExecutor

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# 서비스 키 세팅
KHOA_SERVICE_KEY = "8irtL4rkM7JdnNoeZZEOg=="
KMA_RAW_SERVICE_KEY = "NnwfsETyAYJ%2BZPmMISPD6Vnc63I22ZUSIXLnOaETFkmk1zvUMboPx3u54B8O5V%2F4WUS23Zlljnl3NVjuqrqXKg%3D%3D"

KST = ZoneInfo("Asia/Seoul")


def convert_grid(lat, lon):
    """위경도(lat, lon) 좌표를 기상청 투영 격자(nx, ny) 좌표로 변환"""
    RE = 6371.00877
    GRID = 5.0
    SLAT1 = 30.0
    SLAT2 = 60.0
    OLON = 126.0
    OLAT = 38.0
    XO = 43
    YO = 136

    DEGRAD = math.pi / 180.0

    re = RE / GRID
    slat1 = SLAT1 * DEGRAD
    slat2 = SLAT2 * DEGRAD
    olon = OLON * DEGRAD
    olat = OLAT * DEGRAD

    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
    sf = (math.pow(sf, sn) * math.cos(slat1)) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5)
    ro = (re * sf) / math.pow(ro, sn)

    ra = math.tan(math.pi * 0.25 + (lat) * DEGRAD * 0.5)
    ra = (re * sf) / math.pow(ra, sn)
    theta = lon * DEGRAD - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn

    nx = int(math.floor(ra * math.sin(theta) + XO + 0.5))
    ny = int(math.floor(ro - ra * math.cos(theta) + YO + 0.5))
    return nx, ny


def classify_region(cur_x, cur_y):
    """위경도 좌표를 기반으로 권역 분류"""
    if 33.0 <= cur_y < 34.8:
        if 126.0 <= cur_x <= 127.8:
            return "제주·남서해"
        elif (128.0 <= cur_x <= 129.0) and (33.5 <= cur_y < 34.5):
            return "남해 동부 연안"
    elif 34.8 <= cur_y < 36.0:
        if 126.0 <= cur_x <= 126.2 or (126.0 <= cur_x <= 127.2 and cur_y < 35.8):
            return "전북·전남 서해"
        elif 128.5 <= cur_x <= 129.6:
            return "경남·남해 연안"
    elif 36.0 <= cur_y < 37.2:
        if 126.0 <= cur_x <= 126.0:
            return "충남·서해 연안"
        elif 128.8 <= cur_x <= 129.5:
            return "경북·동해 연안"
    elif 37.2 <= cur_y <= 38.2:
        if 126.0 <= cur_x <= 126.8:
            return "인천·경기 연안"
        elif 128.4 <= cur_x <= 129.2:
            return "강원·동해 연안"
    return None


def calculate_centroid(geometry):
    """GeoJSON Geometry 기반 중심점 좌표 계산"""
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates", [])

    if geom_type == "Point" and len(coords) >= 2:
        return float(coords[1]), float(coords[0])
    elif geom_type == "Polygon" and coords and len(coords[0]) > 0:
        poly_coords = coords[0]
        if len(poly_coords) > 1 and poly_coords[0] == poly_coords[-1]:
            poly_coords = poly_coords[:-1]

        avg_lon = sum(float(c[0]) for c in poly_coords) / len(poly_coords)
        avg_lat = sum(float(c[1]) for c in poly_coords) / len(poly_coords)
        return avg_lat, avg_lon
    return None, None


def fetch_kma_realtime_wind(nx, ny):
    """기상청 초단기실황 API를 통해 실시간 풍속(WSD, m/s) 수집"""
    now = datetime.now(KST)
    if now.minute < 40:
        now = now - timedelta(hours=1)

    base_date = now.strftime("%Y%m%d")
    base_time = now.strftime("%H00")

    url = (
        f"https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst"
        f"?serviceKey={KMA_RAW_SERVICE_KEY}"
        f"&pageNo=1&numOfRows=10&dataType=JSON"
        f"&base_date={base_date}&base_time={base_time}"
        f"&nx={nx}&ny={ny}"
    )

    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            res_json = res.json()
            items = res_json.get("response", {}).get("body", {}).get("items", {}).get("item", [])
            for item in items:
                if item.get("category") == "WSD":
                    return float(item.get("obsrValue", 0.0))
    except Exception:
        pass

    return 0.0


def calculate_tidal_force(current_speed_cms):
    """조류 에너지 밀도 계산"""
    if current_speed_cms is None or current_speed_cms <= 0:
        return 0.0
    speed_ms = float(current_speed_cms) * 0.01
    seawater_density = 1025.0
    return round(0.5 * seawater_density * (speed_ms ** 3), 3)


def convert_current_to_uv(speed_cms, direction_deg):
    """조류 속도(cm/s)와 방향(도)을 u, v 성분(m/s)으로 변환"""
    # 결측치(None) 처리: 방향이나 속도 값이 없으면 None 반환
    if speed_cms is None or direction_deg is None:
        return None, None
    
    speed_ms = float(speed_cms) / 100.0
    theta = math.radians(float(direction_deg))
    
    u = speed_ms * math.sin(theta)
    v = speed_ms * math.cos(theta)
    
    return round(u, 3), round(v, 3)


def fetch_khoa_current_tile(tile_info, target_date, target_hour, wind_cache=None):
    """격자 타일별 해류 데이터 수집 및 기상청 바람 데이터 매핑"""
    cur_x, cur_y, region_name, step_deg = tile_info
    next_x = min(cur_x + step_deg, 132.0)
    next_y = min(cur_y + step_deg, 38.5)

    if wind_cache is None:
        wind_cache = {}

    url = "https://khoa.go.kr/oceandata/api/tidalCurrentAreaGeoJson/search.do"
    params = {
        "ServiceKey": KHOA_SERVICE_KEY,
        "Date": target_date,
        "Hour": target_hour,
        "Minute": "00",
        "MinX": f"{cur_x:.2f}",
        "MaxX": f"{next_x:.2f}",
        "MinY": f"{cur_y:.2f}",
        "MaxY": f"{next_y:.2f}",
        "Scale": "500000",
        "ResultType": "json"
    }

    try:
        res = requests.get(url, params=params, timeout=10)
        if res.status_code == 200:
            features = res.json().get("features", [])

            center_lat = (cur_y + next_y) / 2.0
            center_lon = (cur_x + next_x) / 2.0
            nx, ny = convert_grid(center_lat, center_lon)

            grid_key = (nx, ny)
            if grid_key in wind_cache:
                wind_ms = wind_cache[grid_key]
            else:
                wind_ms = fetch_kma_realtime_wind(nx, ny)
                wind_cache[grid_key] = wind_ms

            wind_drift_cms = wind_ms * 0.03 * 100.0

            for feature in features:
                geometry = feature.get("geometry", {})
                lat, lon = calculate_centroid(geometry)

                feature.setdefault("properties", {})
                if lat is not None and lon is not None:
                    feature["properties"]["lat"] = round(lat, 5)
                    feature["properties"]["lon"] = round(lon, 5)

                feature["properties"]["region"] = region_name
                feature["properties"]["nx"] = nx
                feature["properties"]["ny"] = ny
                feature["properties"]["wind_speed_ms"] = round(wind_ms, 1)
                feature["properties"]["wind_drift_cms"] = round(wind_drift_cms, 1)

                # 결측치가 있을 수 있으므로 .get() 사용 후 형변환 (기본값 제외)
                raw_speed = feature["properties"].get("current_speed")
                raw_direct = feature["properties"].get("current_direct")
                
                speed_val = float(raw_speed) if raw_speed is not None else None
                direct_val = float(raw_direct) if raw_direct is not None else None
                
                current_u, current_v = convert_current_to_uv(speed_val, direct_val)
                
                feature["properties"]["current_u"] = current_u
                feature["properties"]["current_v"] = current_v
                # 원본 정밀도 보존을 위해 current_speed, current_direct 필드 덮어쓰기 로직 제거

                # [임시 계산] 현재 total_current_speed 값은 방향을 고려하지 않은 스칼라 합산으로, 기존 호환성을 위한 임시 값입니다.
                # 실제 이동 시뮬레이션에서는 사용하지 않습니다.
                # 향후 조류 u/v와 바람 u/v를 이용한 벡터 합산 방식으로 교체해야 합니다.
                # 향후 목표 방식:
                # total_u = current_u + wind_u * wind_drag_coefficient
                # total_v = current_v + wind_v * wind_drag_coefficient
                if speed_val is not None:
                    total_current_speed = round(speed_val + wind_drift_cms, 1)
                    feature["properties"]["total_current_speed"] = total_current_speed
                    feature["properties"]["tidal_force"] = calculate_tidal_force(total_current_speed)
                else:
                    feature["properties"]["total_current_speed"] = None
                    feature["properties"]["tidal_force"] = None

            return features
    except Exception:
        pass

    return []


def get_high_resolution_national_ocean_data():
    """전국 해양 및 기상 바람 데이터 획득 메인 로직"""
    now = datetime.now(KST)
    target_date = now.strftime("%Y%m%d")
    target_hour = now.strftime("%H")

    print("==================================================")
    print("🚀 [GitHub Collector] 실시간 수집 프로세스 실행 중...")
    print(f"📌 [시작] 기준 시각: {now.strftime('%Y-%m-%d %H:%M:%S (KST)')}")
    print("==================================================")

    # 1. 격자 타일 생성
    print("\n[Step 1/3] 🗺️ 해역 타일 영역 생성 중...")
    valid_tiles = []
    cur_y = 33.0
    while cur_y < 38.5:
        cur_x = 124.0
        while cur_x < 132.0:
            region_name = classify_region(cur_x, cur_y)
            if region_name:
                valid_tiles.append((cur_x, cur_y, region_name, 0.5))
            cur_x += 0.5
        cur_y += 0.5

    print(f"  👉 총 {len(valid_tiles)}개 해역 타일 설정 완료")

    # 2. 병렬 데이터 수집 실행
    print("\n[Step 2/3] 🌊 해류(KHOA) & 🌀 바람(KMA HTTPS) 병렬 수집 중...")
    all_features = []
    wind_cache = {}
    completed_count = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(fetch_khoa_current_tile, tile, target_date, target_hour, wind_cache)
            for tile in valid_tiles
        ]
        for future in futures:
            features = future.result()
            completed_count += 1
            if features:
                all_features.extend(features)
            
            if completed_count % 5 == 0 or completed_count == len(valid_tiles):
                print(f"  ⚡ 진행 현황: {completed_count}/{len(valid_tiles)} 타일 완료 ({int(completed_count / len(valid_tiles) * 100)}%)")

    print(f"  👉 총 {len(all_features)}개의 GeoJSON Feature 생성 완료")

    # 3. 데이터 결합
    print("\n[Step 3/3] 📦 데이터 통합 및 JSON 패키징...")
    combined_geojson = {
        "type": "FeatureCollection",
        "features": all_features
    }

    return {
        "metadata": {
            "query_time": now.strftime("%Y-%m-%d %H:00 (KST)"),
            "region": "대한민국 연안 및 지정 구역",
            "scale": "500000",
            "version": "v2.1.1_VECTOR_FIXES",
            "applied_formula": "u = speed * sin(theta), v = speed * cos(theta)"
        },
        "ocean_current_geojson": combined_geojson
    }

def upload_to_drive(file_path):
    """구글 드라이브 API를 통해 개인 계정(OAuth)으로 업로드"""
    print("\n==================================================")
    print("📤 [Google Drive] OAuth 업로드 진행 중...")
    
    # GitHub Secrets에서 가져올 정보들
    client_id = os.environ.get("OAUTH_CLIENT_ID")
    client_secret = os.environ.get("OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("OAUTH_REFRESH_TOKEN")
    folder_id = os.environ.get("DRIVE_FOLDER_ID")

    if not all([client_id, client_secret, refresh_token, folder_id]):
        print("⚠️ [경고] OAuth 또는 드라이브 폴더 환경변수가 설정되지 않아 업로드를 건너뜁니다.")
        return

    try:
        # Refresh Token을 이용해 내 개인 계정 권한 객체 생성
        creds = Credentials(
            None,  # access_token은 자동으로 갱신되므로 None 처리
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token"
        )

        service = build('drive', 'v3', credentials=creds)

        # 시간별로 데이터를 누적하고 싶다면 고유한 파일 이름(타임스탬프) 생성
        now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        file_name = f"ocean_data_{now_str}.json"

        file_metadata = {
            'name': file_name,
            'parents': [folder_id]
        }
        
        media = MediaFileUpload(file_path, mimetype='application/json')

        # 파일 생성 (내 개인 계정 용량을 쓰므로 용량 초과 에러가 발생하지 않습니다!)
        new_file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()

        print(f"✅ [성공] 내 드라이브에 파일 업로드 완료! (파일명: {file_name}, ID: {new_file.get('id')})")

    except Exception as e:
        print(f"❌ [오류] 구글 드라이브 업로드 실패: {e}")

if __name__ == "__main__":
    # 1. 데이터 수집 실행
    collected_data = get_high_resolution_national_ocean_data()
    
    # 2. 수집된 데이터를 로컬에 임시 JSON 파일로 저장
    temp_file_name = "temp_ocean_data.json"
    with open(temp_file_name, "w", encoding="utf-8") as f:
        json.dump(collected_data, f, ensure_ascii=False, indent=4)
    
    print(f"💾 [로컬] 임시 파일 저장 완료: {temp_file_name}")
    
    # 3. 구글 드라이브로 업로드 실행
    upload_to_drive(temp_file_name)
    
    # 4. 업로드가 끝난 뒤 로컬에 남은 임시 파일 삭제 (선택사항)
    if os.path.exists(temp_file_name):
        os.remove(temp_file_name)