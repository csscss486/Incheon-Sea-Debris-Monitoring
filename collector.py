import os
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from concurrent.futures import ThreadPoolExecutor

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# 1. KHOA_SERVICE_KEY 환경변수 처리 및 검증
KHOA_SERVICE_KEY = os.environ.get("KHOA_SERVICE_KEY")
if not KHOA_SERVICE_KEY:
    raise ValueError("❌ [오류] KHOA_SERVICE_KEY 환경변수가 설정되지 않았습니다. 실행 전 환경변수를 설정해주세요.")

KST = ZoneInfo("Asia/Seoul")


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
        if 126.0 <= cur_x <= 126.5:
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


def fetch_open_meteo_wind_vector(lat, lon):
    """Open-Meteo API를 통해 10m 바람 벡터(u, v) 수집 및 km/h -> m/s 단위 변환 (실패 시 None, None 반환)"""
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=wind_u_component_10m,wind_v_component_10m"
    )
    
    try:
        res = requests.get(url, timeout=10)
        if res.status_code != 200:
            return None, None

        res_json = res.json()
        current_data = res_json.get("current")
        if not current_data:
            return None, None

        u_kmh = current_data.get("wind_u_component_10m")
        v_kmh = current_data.get("wind_v_component_10m")

        if u_kmh is None or v_kmh is None:
            return None, None

        return float(u_kmh) / 3.6, float(v_kmh) / 3.6
    except Exception:
        return None, None


def calculate_tidal_force(current_speed_cms):
    """조류 에너지 밀도 계산"""
    if current_speed_cms is None or current_speed_cms <= 0:
        return 0.0
    speed_ms = float(current_speed_cms) * 0.01
    seawater_density = 1025.0
    return round(0.5 * seawater_density * (speed_ms ** 3), 3)


def convert_current_to_uv(speed_cms, direction_deg):
    """조류 속도(cm/s)와 방향(도)을 u, v 성분(m/s)으로 변환"""
    if speed_cms is None or direction_deg is None:
        return None, None
    
    speed_ms = float(speed_cms) / 100.0
    theta = math.radians(float(direction_deg))
    
    return round(speed_ms * math.sin(theta), 3), round(speed_ms * math.cos(theta), 3)


def fetch_khoa_current_tile(tile_info, target_date, target_hour, data_timestamp_str, wind_cache=None):
    """격자 타일별 해류 데이터 수집 및 API 오류 검사, Feature별 예외 처리 적용"""
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
            data = res.json()
            
            if "errorCode" in data or "resultCode" in data:
                err_code = data.get("errorCode") or data.get("resultCode")
                if str(err_code) not in ["0", "00", "NORMAL", "SUCCESS"]:
                    return [], {"tile": tile_info, "reason": f"API_INTERNAL_ERROR_{err_code}"}, 0

            raw_features = data.get("features", [])
            if not raw_features and ("result" in data and data.get("result") == "error"):
                return [], {"tile": tile_info, "reason": "API_RESULT_ERROR_FIELD"}, 0

            center_lat = (cur_y + next_y) / 2.0
            center_lon = (cur_x + next_x) / 2.0

            grid_key = (round(center_lat, 2), round(center_lon, 2))
            if grid_key in wind_cache:
                wind_u, wind_v = wind_cache[grid_key]
            else:
                wind_u, wind_v = fetch_open_meteo_wind_vector(center_lat, center_lon)
                wind_cache[grid_key] = (wind_u, wind_v)

            wind_drag_coefficient = 0.03
            if wind_u is not None and wind_v is not None:
                wind_status = "VALID"
                wind_speed_ms = math.hypot(wind_u, wind_v)
                wind_drift_cms = wind_speed_ms * wind_drag_coefficient * 100.0
            else:
                wind_status = "MISSING"
                wind_u, wind_v = None, None
                wind_speed_ms = None
                wind_drift_cms = None

            valid_features = []
            skipped_feature_count = 0

            for feature in raw_features:
                try:
                    geometry = feature.get("geometry", {})
                    lat, lon = calculate_centroid(geometry)

                    feature.setdefault("properties", {})
                    feature["properties"]["timestamp"] = data_timestamp_str

                    if lat is not None and lon is not None:
                        feature["properties"]["lat"] = round(lat, 5)
                        feature["properties"]["lon"] = round(lon, 5)

                    feature["properties"]["region"] = region_name
                    
                    feature["properties"]["wind_u"] = round(wind_u, 3) if wind_status == "VALID" else None
                    feature["properties"]["wind_v"] = round(wind_v, 3) if wind_status == "VALID" else None
                    feature["properties"]["wind_speed_ms"] = round(wind_speed_ms, 1) if wind_speed_ms is not None else None
                    feature["properties"]["wind_drift_cms"] = round(wind_drift_cms, 1) if wind_drift_cms is not None else None
                    feature["properties"]["wind_data_status"] = wind_status

                    raw_speed = feature["properties"].get("current_speed")
                    raw_direct = feature["properties"].get("current_direct")
                    
                    speed_val = float(raw_speed) if raw_speed is not None else None
                    direct_val = float(raw_direct) if raw_direct is not None else None
                    
                    current_u, current_v = convert_current_to_uv(speed_val, direct_val)
                    feature["properties"]["current_u"] = current_u
                    feature["properties"]["current_v"] = current_v
                    
                    if current_u is not None and current_v is not None:
                        if wind_status == "VALID":
                            total_u = current_u + wind_u * wind_drag_coefficient
                            total_v = current_v + wind_v * wind_drag_coefficient
                            feature["properties"]["total_vector_status"] = "VALID"
                        else:
                            total_u = current_u
                            total_v = current_v
                            feature["properties"]["total_vector_status"] = "WIND_MISSING"
                        
                        feature["properties"]["total_u"] = round(total_u, 3)
                        feature["properties"]["total_v"] = round(total_v, 3)
                        
                        total_speed_ms = math.hypot(total_u, total_v)
                        total_speed_cms = round(total_speed_ms * 100.0, 1)
                        feature["properties"]["total_current_speed"] = total_speed_cms
                        
                        total_dir_deg = round(math.degrees(math.atan2(total_u, total_v)) % 360, 1)
                        feature["properties"]["total_current_direction"] = total_dir_deg
                        
                        feature["properties"]["tidal_force"] = calculate_tidal_force(total_speed_cms)
                    else:
                        feature["properties"]["total_u"] = None
                        feature["properties"]["total_v"] = None
                        feature["properties"]["total_current_speed"] = None
                        feature["properties"]["total_current_direction"] = None
                        feature["properties"]["tidal_force"] = None
                        feature["properties"]["total_vector_status"] = "WIND_MISSING" if wind_status == "MISSING" else "VALID"

                    valid_features.append(feature)
                except Exception:
                    skipped_feature_count += 1

            return valid_features, None, skipped_feature_count
    except Exception as e:
        print(f"❌ [오류] 타일 요청 예외 발생 (Tile: {tile_info}): {e}")

    return [], {"tile": tile_info, "reason": "API_REQUEST_FAILED_OR_EXCEPTION"}, 0


def get_high_resolution_national_ocean_data():
    """전국 해양 및 기상 바람 데이터 획득 메인 로직"""
    now = datetime.now(KST)
    collected_at_str = now.isoformat()
    
    target_date = now.strftime("%Y%m%d")
    target_hour = now.strftime("%H")
    
    data_timestamp_str = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}T{target_hour}:00:00+09:00"

    print("==================================================")
    print("🚀 [GitHub Collector] 실시간 수집 프로세스 실행 중...")
    print(f"📌 [실행 시각 - collected_at]: {collected_at_str}")
    print(f"📌 [데이터 유효 시각 - timestamp]: {data_timestamp_str}")
    print("==================================================")

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

    print("\n[Step 2/3] 🌊 해류(KHOA) & 🌀 바람(Open-Meteo) 병렬 수집 중...")
    all_features = []
    failed_tiles = []
    total_skipped_features = 0
    wind_cache = {}
    completed_count = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(fetch_khoa_current_tile, tile, target_date, target_hour, data_timestamp_str, wind_cache)
            for tile in valid_tiles
        ]
        for future in futures:
            features, fail_info, skipped_count = future.result()
            completed_count += 1
            if features:
                all_features.extend(features)
            if fail_info:
                failed_tiles.append(fail_info)
            total_skipped_features += skipped_count
            
            if completed_count % 5 == 0 or completed_count == len(valid_tiles):
                print(f"  ⚡ 진행 현황: {completed_count}/{len(valid_tiles)} 타일 완료 ({int(completed_count / len(valid_tiles) * 100)}%)")

    print(f"  👉 총 {len(all_features)}개의 GeoJSON Feature 생성 완료 (실패 타일: {len(failed_tiles)}개, 제외된 피처: {total_skipped_features}개)")

    total_tiles_count = len(valid_tiles)
    failed_tiles_count = len(failed_tiles)

    if failed_tiles_count == 0:
        current_status = "SUCCESS"
    elif failed_tiles_count < total_tiles_count:
        current_status = "PARTIAL"
    else:
        current_status = "FAILED"

    if all_features:
        missing_wind_count = sum(1 for f in all_features if f.get("properties", {}).get("wind_data_status") == "MISSING")
        if missing_wind_count == 0:
            wind_status = "SUCCESS"
        elif missing_wind_count < len(all_features):
            wind_status = "PARTIAL"
        else:
            wind_status = "FAILED"
    else:
        wind_status = "FAILED"

    print("\n[Step 3/3] 📦 데이터 통합 및 JSON 패키징...")
    combined_geojson = {
        "type": "FeatureCollection",
        "features": all_features
    }

    metadata = {
        "timestamp": data_timestamp_str,
        "collected_at": collected_at_str,
        "query_time": now.strftime("%Y-%m-%d %H:00 (KST)"),
        "region": "대한민국 연안 및 지정 구역",
        "scale": "500000",
        "grid_resolution": "0.5 degree",
        "wind_source": "Open-Meteo Forecast API (10m u/v components)",
        "current_source": "KHOA tidalCurrentAreaGeoJson API",
        "wind_status": wind_status,
        "current_status": current_status,
        "skipped_features_count": total_skipped_features,
        "version": "v3.6_WIND_MISSING_FIXED",
        "applied_formula": "total_u = current_u + wind_u*0.03, total_v = current_v + wind_v*0.03 (m/s)",
        "failed_tiles": failed_tiles
    }

    return {
        "metadata": metadata,
        "ocean_current_geojson": combined_geojson
    }


def upload_to_drive(file_path):
    """구글 드라이브 API를 통해 개인 계정(OAuth)으로 업로드"""
    print("\n==================================================")
    print("📤 [Google Drive] OAuth 업로드 진행 중...")
    
    client_id = os.environ.get("OAUTH_CLIENT_ID")
    client_secret = os.environ.get("OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("OAUTH_REFRESH_TOKEN")
    folder_id = os.environ.get("DRIVE_FOLDER_ID")

    if not all([client_id, client_secret, refresh_token, folder_id]):
        print("⚠️ [경고] OAuth 또는 드라이브 폴더 환경변수가 설정되지 않아 업로드를 건너뜁니다.")
        return

    try:
        creds = Credentials(
            None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token"
        )

        service = build('drive', 'v3', credentials=creds)

        timestamp_str = collected_data.get("metadata", {}).get("timestamp", "")
        try:
            dt_obj = datetime.fromisoformat(timestamp_str)
            file_date_str = dt_obj.strftime("%Y%m%d_%H%M")
        except Exception:
            file_date_str = datetime.now(KST).strftime("%Y%m%d_%H%M")

        file_name = f"ocean_data_{file_date_str}.json"

        file_metadata = {
            'name': file_name,
            'parents': [folder_id]
        }
        
        media = MediaFileUpload(file_path, mimetype='application/json')

        new_file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()

        print(f"✅ [성공] 내 드라이브에 파일 업로드 완료! (파일명: {file_name}, ID: {new_file.get('id')})")

    except Exception as e:
        print(f"❌ [오류] 구글 드라이브 업로드 실패: {e}")


if __name__ == "__main__":
    collected_data = get_high_resolution_national_ocean_data()
    
    features_list = collected_data.get("ocean_current_geojson", {}).get("features", [])
    if features_list:
        print("\n🔍 [검증 결과] 최종 GeoJSON Feature 샘플 출력:")
        print(json.dumps(features_list[0], ensure_ascii=False, indent=4))
    else:
        print("\n⚠️ [검증 결과] 수집된 Feature가 없습니다.")

    timestamp_str = collected_data.get("metadata", {}).get("timestamp", "")
    try:
        dt_obj = datetime.fromisoformat(timestamp_str)
        file_date_str = dt_obj.strftime("%Y%m%d_%H%M")
    except Exception:
        file_date_str = datetime.now(KST).strftime("%Y%m%d_%H%M")

    temp_file_name = f"ocean_data_{file_date_str}.json"
    with open(temp_file_name, "w", encoding="utf-8") as f:
        json.dump(collected_data, f, ensure_ascii=False, indent=4)
    
    print(f"\n💾 [로컬] 임시 파일 저장 완료: {temp_file_name}")
    
    upload_to_drive(temp_file_name)
    
    if os.path.exists(temp_file_name):
        os.remove(temp_file_name)