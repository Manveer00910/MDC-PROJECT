from datetime import datetime, timezone, timedelta
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import time
import os
import ssl

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder='.', static_url_path='')

# Representative city coordinates for each target region. These are city-level
# weather grid points, not river-gauge locations or full-state averages.
REGIONS = [
    {"state": "Jammu & Kashmir", "location": "Srinagar", "latitude": 34.0837, "longitude": 74.7973},
    {"state": "Himachal Pradesh", "location": "Shimla", "latitude": 31.1048, "longitude": 77.1734},
    {"state": "Uttarakhand", "location": "Dehradun", "latitude": 30.3165, "longitude": 78.0322},
    {"state": "Sikkim", "location": "Gangtok", "latitude": 27.3389, "longitude": 88.6065},
    {"state": "Arunachal Pradesh", "location": "Itanagar", "latitude": 27.0844, "longitude": 93.6053},
    {"state": "Assam", "location": "Guwahati", "latitude": 26.1445, "longitude": 91.7362},
]

CACHE_TTL_SECONDS = 60
_cache = {"payload": None, "fetched_at": 0}
_cache_lock = Lock()


def fetch_location_weather(region):
    params = urlencode({
        "latitude": region["latitude"],
        "longitude": region["longitude"],
        "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m",
        "hourly": "precipitation,precipitation_probability,temperature_2m",
        "forecast_hours": 24,
        "timezone": "Asia/Kolkata",
    })
    url = "https://api.open-meteo.com/v1/forecast?" + params
    req = Request(url, headers={"User-Agent": "HydraBytes-MDC-Prototype/1.0"})
    with urlopen(req, context=SSL_CONTEXT, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Weather API returned HTTP {response.status}")
        raw = json.loads(response.read().decode("utf-8"))

    current = raw.get("current") or {}
    hourly = raw.get("hourly") or {}
    times = hourly.get("time") or []
    precipitation = hourly.get("precipitation") or []
    probabilities = hourly.get("precipitation_probability") or []
    forecasts = []
    for i, timestamp in enumerate(times[:24]):
        forecasts.append({
            "time": timestamp,
            "precipitation_mm": precipitation[i] if i < len(precipitation) else None,
            "precipitation_probability_pct": probabilities[i] if i < len(probabilities) else None,
        })

    valid_rain = [v for v in precipitation[:24] if isinstance(v, (int, float))]
    valid_prob = [v for v in probabilities[:24] if isinstance(v, (int, float))]
    rain_24h = round(sum(valid_rain), 1) if valid_rain else None
    peak_hourly_rain = round(max(valid_rain), 1) if valid_rain else None
    peak_probability = max(valid_prob) if valid_prob else None
    # Transparent weather-only trigger, not a validated flood prediction.
    if rain_24h is None:
        weather_trigger = "Unavailable"
    elif rain_24h >= 100 or (peak_hourly_rain is not None and peak_hourly_rain >= 35):
        weather_trigger = "Elevated"
    elif rain_24h >= 50 or (peak_hourly_rain is not None and peak_hourly_rain >= 20) or (peak_probability is not None and peak_probability >= 80):
        weather_trigger = "Watch"
    else:
        weather_trigger = "No rainfall trigger"

    return {
        "state": region["state"],
        "location": region["location"],
        "source": "Open-Meteo Forecast API",
        "latitude": region["latitude"],
        "longitude": region["longitude"],
        "current_conditions": {
            "time": current.get("time"),
            "temperature_c": current.get("temperature_2m"),
            "precipitation_mm": current.get("precipitation"),
            "relative_humidity_pct": current.get("relative_humidity_2m"),
            "wind_speed_kmh": current.get("wind_speed_10m"),
            "data_type": "Current weather-model estimate; not a direct station observation",
        } if current else None,
        "forecast": forecasts,
        "forecast_summary": {
            "next_24h_precipitation_mm": rain_24h,
            "max_hourly_precipitation_mm": peak_hourly_rain,
            "max_precipitation_probability_pct": peak_probability,
            "weather_trigger": weather_trigger,
            "trigger_method": "Illustrative thresholds only; not calibrated or validated for flood prediction",
        },
        "river_level_m": None,
        "river_level_status": "Unavailable — no verified official gauge feed configured",
    }


def build_weather_payload():
    # Fetch regions concurrently so one slow city does not delay all the others.
    results_by_location = {}
    failures = []
    with ThreadPoolExecutor(max_workers=min(6, len(REGIONS))) as pool:
        future_to_region = {pool.submit(fetch_location_weather, region): region for region in REGIONS}
        for future in as_completed(future_to_region):
            region = future_to_region[future]
            try:
                results_by_location[region["location"]] = future.result()
            except Exception as exc:
                failures.append({"location": region["location"], "error": str(exc)})
                results_by_location[region["location"]] = {
                    "state": region["state"],
                    "location": region["location"],
                    "source": "Open-Meteo Forecast API",
                    "current_conditions": None,
                    "forecast": [],
                    "forecast_summary": {"next_24h_precipitation_mm": None, "max_hourly_precipitation_mm": None, "max_precipitation_probability_pct": None, "weather_trigger": "Unavailable"},
                    "river_level_m": None,
                    "river_level_status": "Unavailable — no verified official gauge feed configured",
                    "error": f"Weather API request failed: {exc}",
                }
    # Keep the table order stable even though requests finish at different times.
    regions = [results_by_location[region["location"]] for region in REGIONS]
    return {
        "regions": regions,
        "source": "Open-Meteo Forecast API (https://open-meteo.com/)",
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "next_refresh_after_utc": (datetime.now(timezone.utc) + timedelta(seconds=CACHE_TTL_SECONDS)).isoformat(timespec="seconds"),
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "partial_failures": failures,
        "data_notice": "Weather values and forecasts are model-derived for representative city coordinates. The weather trigger is a transparent illustrative rainfall threshold, not a calibrated or validated flood model. River levels remain unavailable unless a verified official gauge feed is configured. This prototype does not issue official flood warnings.",
    }


def get_weather_payload(force=False):
    with _cache_lock:
        now = time.time()
        if not force and _cache["payload"] is not None and now - _cache["fetched_at"] < CACHE_TTL_SECONDS:
            return _cache["payload"]
        payload = build_weather_payload()
        # Cache successful/partial responses for at most 60 seconds. If every location
        # fails, retry after 5 minutes rather than repeatedly hammering the API.
        all_failed = bool(payload.get("partial_failures")) and all(
            region.get("current_conditions") is None for region in payload.get("regions", [])
        )
        _cache["payload"] = payload
        _cache["fetched_at"] = now - CACHE_TTL_SECONDS + 300 if all_failed else now
        return payload


@app.get('/')
def home():
    return send_from_directory('.', 'index.html')


@app.get('/api/health')
def health():
    return jsonify({"status": "ok", "service": "HydraBytes", "cache_ttl_seconds": CACHE_TTL_SECONDS})


@app.get('/api/regions')
def regions():
    force_refresh = request.args.get('refresh') == '1'
    payload = get_weather_payload(force=force_refresh)
    # A full API failure is surfaced as a clear error rather than fabricated data.
    if payload.get('partial_failures') and all(r.get('current_conditions') is None for r in payload['regions']):
        return jsonify(payload), 503
    return jsonify(payload)


@app.get('/api/risk-indicators')
def risk_indicators():
    """Return transparent rainfall-trigger indicators derived from live forecasts."""
    payload = get_weather_payload(force=request.args.get('refresh') == '1')
    rows = []
    for region in payload.get('regions', []):
        summary = region.get('forecast_summary') or {}
        rows.append({
            "state": region.get('state'),
            "location": region.get('location'),
            "next_24h_precipitation_mm": summary.get('next_24h_precipitation_mm'),
            "max_hourly_precipitation_mm": summary.get('max_hourly_precipitation_mm'),
            "max_precipitation_probability_pct": summary.get('max_precipitation_probability_pct'),
            "weather_trigger": summary.get('weather_trigger', 'Unavailable'),
            "river_level_m": region.get('river_level_m'),
            "river_level_status": region.get('river_level_status'),
            "notice": "Weather-only screening indicator; not a validated flood prediction or official warning."
        })
    return jsonify({"source": payload.get("source"), "retrieved_at_utc": payload.get("retrieved_at_utc"), "regions": rows,
                    "notice": "Use official IMD/CWC and local disaster-management advisories for decisions."})


@app.post('/api/predict')
def predict():
    """Educational demo only. This is not a validated operational flood model."""
    data = request.get_json(silent=True) or {}
    try:
        rainfall = float(data.get('rainfall_mm', 0))
        water_level = float(data.get('water_level_m', 0))
        soil = str(data.get('soil_saturation', 'medium')).lower()
        slope = str(data.get('slope', 'medium')).lower()
    except (TypeError, ValueError):
        return jsonify({"error": "Please provide valid numeric rainfall and water-level values."}), 400

    # Transparent placeholder scoring for UI continuity; synthetic/demo logic only.
    score = min(100, max(0, rainfall * 1.2 + water_level * 8 + (20 if soil in ('high', 'saturated') else 10 if soil == 'medium' else 0) + (15 if slope in ('steep', 'high') else 5 if slope == 'medium' else 0)))
    if score >= 75:
        level, recommendation = 'CRITICAL', 'Demo result only: seek official local guidance; this is not an emergency warning.'
    elif score >= 50:
        level, recommendation = 'HIGH', 'Demo result only: check official weather and disaster-management updates.'
    elif score >= 25:
        level, recommendation = 'MODERATE', 'Demo result only: continue monitoring verified local information.'
    else:
        level, recommendation = 'LOW', 'Demo result only: do not interpret this as proof of safety.'
    return jsonify({"risk_level": level, "risk_score": round(score), "demo_confidence": 0.0, "recommendation": recommendation, "model_notice": "Manual-input illustrative rule-based demo; not a validated flood prediction model. Use /api/risk-indicators for forecast-derived rainfall screening."})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5001, debug=False)
