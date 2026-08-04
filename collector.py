import os
import json
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from concurrent.futures import ThreadPoolExecutor

from google.oauth2.service_account import Credentials
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

                raw_current_speed = float(feature["properties"].get("current_speed", 0.0))
                total_current_speed = round(raw_current_speed + wind_drift_cms, 1)
                feature["properties"]["total_current_speed"] = total_current_speed
                feature["properties"]["tidal_force"] = calculate_tidal_force(total_current_speed)

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
            "version": "v2.0_HTTPS_FIXED",
            "applied_formula": "total_current_speed (cm/s) = current_current_speed + (wind_speed_ms * 0.03 * 100)"
        },
        "ocean_current_geojson": combined_geojson
    }


from datetime import datetime

def upload_to_drive(file_path):
    """구글 드라이브 API를 통해 시간별 고유 파일명으로 업로드 (용량 에러 우회)"""
    print("\n==================================================")
    print("📤 [Google Drive] API 업로드 진행 중...")
    
    service_account_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    folder_id = os.environ.get("DRIVE_FOLDER_ID")

    if not service_account_str or not folder_id:
        print("⚠️ [경고] 구글 서비스 계정 환경변수가 설정되지 않아 로컬 파일 생성만 완료합니다.")
        return

    try:
        service_account_info = json.loads(service_account_str)
        creds = Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/drive.file']
        )
        service = build('drive', 'v3', credentials=creds)

        # 1. 현재 시간 기준으로 고유한 파일 이름 생성 (예: ocean_data_2026-08-04_13-25-00.json)
        now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        unique_file_name = f"ocean_data_{now_str}.json"

        # 2. 로컬에 있는 기존 수집 파일을 새로운 이름으로 복사하거나, 업로드할 때 이름 변경 적용
        # (만약 기존에 'ocean_data_latest.json'으로 저장하고 있었다면, 업로드 시 이름을 unique_file_name으로 지정합니다)
        file_metadata = {
            'name': unique_file_name,
            'parents': [folder_id]
        }
        
        media = MediaFileUpload(file_path, mimetype='application/json')

        # 3. 매번 다른 이름으로 생성(create)하므로 서비스 계정 용량 에러가 발생하지 않습니다!
        new_file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        print(f"✅ [성공] 시간별 데이터 파일 업로드 완료! (파일명: {unique_file_name}, ID: {new_file.get('id')})")

    except Exception as e:
        print(f"❌ [오류] 구글 드라이브 업로드 실패: {e}")


if __name__ == "__main__":
    start_time = time.time()
    ocean_data = get_high_resolution_national_ocean_data()

    # 작업 공간 내 JSON 파일로 일단 저장
    output_filename = "./ocean_data_latest.json"
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(ocean_data, f, ensure_ascii=False, indent=2)
    print(f"💾 [완료] 로컬 작업 디렉터리 파일 생성: {output_filename}")

    # 구글 드라이브 업로드 실행
    upload_to_drive(output_filename)

    elapsed = round(time.time() - start_time, 2)
    print(f"⏱️ 총 작업 소요 시간: {elapsed}초")
    print("==================================================")