"""
Fetcher: ballpark weather at/near first pitch time.

Source: OpenWeatherMap free tier (api.openweathermap.org)
  - /data/2.5/forecast  → 3-hour intervals, up to 5 days ahead
  - /data/2.5/weather   → current conditions (fallback)

Returns temp (°F), wind speed (mph) + direction, precip chance (%),
sky condition (Clear / Cloudy / Rain / etc.), and a short display string.

Requires env var: OPENWEATHERMAP_API_KEY
"""

import logging
import math
import os
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_OWM_FORECAST = "https://api.openweathermap.org/data/2.5/forecast"
_OWM_CURRENT  = "https://api.openweathermap.org/data/2.5/weather"

# Ballpark coordinates  {team_abbr: (lat, lon)}
PARK_COORDS: dict[str, tuple[float, float]] = {
    "ARI": (33.4455, -112.0667),   # Chase Field
    "ATH": (37.7516, -122.2005),   # Oakland Coliseum (Athletics still using it in 2026)
    "ATL": (33.8908, -84.4678),    # Truist Park
    "BAL": (39.2838, -76.6216),    # Camden Yards
    "BOS": (42.3467, -71.0972),    # Fenway Park
    "CHC": (41.9484, -87.6553),    # Wrigley Field
    "CIN": (39.0979, -84.5082),    # Great American Ball Park
    "CLE": (41.4962, -81.6852),    # Progressive Field
    "COL": (39.7559, -104.9942),   # Coors Field
    "CWS": (41.8300, -87.6339),    # Guaranteed Rate Field
    "DET": (42.3390, -83.0485),    # Comerica Park
    "HOU": (29.7573, -95.3555),    # Minute Maid Park
    "KC":  (39.0517, -94.4803),    # Kauffman Stadium
    "LAA": (33.8003, -117.8827),   # Angel Stadium
    "LAD": (34.0739, -118.2400),   # Dodger Stadium
    "MIA": (25.7781, -80.2197),    # loanDepot park
    "MIL": (43.0280, -87.9712),    # American Family Field
    "MIN": (44.9817, -93.2778),    # Target Field
    "NYM": (40.7571, -73.8458),    # Citi Field
    "NYY": (40.8296, -73.9262),    # Yankee Stadium
    "OAK": (37.7516, -122.2005),   # (alias for ATH)
    "PHI": (39.9061, -75.1665),    # Citizens Bank Park
    "PIT": (40.4469, -80.0057),    # PNC Park
    "SD":  (32.7076, -117.1570),   # Petco Park
    "SEA": (47.5914, -122.3325),   # T-Mobile Park
    "SF":  (37.7786, -122.3893),   # Oracle Park
    "STL": (38.6226, -90.1928),    # Busch Stadium
    "TB":  (27.7683, -82.6534),    # Tropicana Field
    "TEX": (32.7473, -97.0824),    # Globe Life Field
    "TOR": (43.6414, -79.3894),    # Rogers Centre
    "WSH": (38.8730, -77.0074),    # Nationals Park
}

_WIND_DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _deg_to_compass(deg: float) -> str:
    idx = round(deg / 22.5) % 16
    return _WIND_DIRS[idx]


def _condition_label(weather_id: int, description: str) -> str:
    """Map OWM weather ID to a short human label."""
    if weather_id < 300:
        return "Thunderstorm"
    if weather_id < 400:
        return "Drizzle"
    if weather_id < 600:
        return "Rain"
    if weather_id < 700:
        return "Snow"
    if weather_id < 800:
        return "Fog"
    if weather_id == 800:
        return "Clear"
    if weather_id == 801:
        return "Mostly Clear"
    if weather_id == 802:
        return "Partly Cloudy"
    return "Cloudy"


def fetch_weather(home_team: str, first_pitch_utc: str | None = None) -> dict | None:
    """
    Fetch weather for a ballpark at/near first pitch time.

    Args:
        home_team:       3-letter team abbreviation (home team = park location).
        first_pitch_utc: ISO string like "2026-04-13T17:35:00Z". If None or
                         the game is within 3 hrs, uses current conditions.

    Returns:
        {
          "temp_f":      float,   # temperature in °F
          "feels_like":  float,
          "wind_mph":    float,
          "wind_dir":    str,     # compass direction e.g. "SW"
          "precip_pct":  int,     # probability of precipitation 0-100
          "condition":   str,     # "Clear" / "Rain" / etc.
          "description": str,     # raw OWM description
          "display":     str,     # short human string for UI
        }
        or None on failure.
    """
    api_key = os.getenv("OPENWEATHERMAP_API_KEY")
    if not api_key:
        logger.warning("OPENWEATHERMAP_API_KEY not set — skipping weather fetch")
        return None

    coords = PARK_COORDS.get(home_team)
    if not coords:
        logger.warning("No park coords for team %s", home_team)
        return None

    lat, lon = coords

    # --- Decide: forecast or current? ---
    use_forecast = False
    target_ts: float | None = None

    if first_pitch_utc:
        try:
            fp = datetime.fromisoformat(first_pitch_utc.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta_hrs = (fp - now).total_seconds() / 3600
            if 0 < delta_hrs <= 120:   # OWM free forecast range is 5 days
                use_forecast = True
                target_ts = fp.timestamp()
        except Exception:
            pass

    if use_forecast:
        return _from_forecast(lat, lon, api_key, target_ts)
    else:
        return _from_current(lat, lon, api_key)


def _from_current(lat: float, lon: float, api_key: str) -> dict | None:
    try:
        resp = requests.get(
            _OWM_CURRENT,
            params={"lat": lat, "lon": lon, "appid": api_key, "units": "imperial"},
            timeout=10,
        )
        resp.raise_for_status()
        d = resp.json()
    except Exception as exc:
        logger.warning("OWM current weather failed: %s", exc)
        return None

    return _parse_current_entry(d)


def _from_forecast(lat: float, lon: float, api_key: str, target_ts: float) -> dict | None:
    try:
        resp = requests.get(
            _OWM_FORECAST,
            params={"lat": lat, "lon": lon, "appid": api_key, "units": "imperial", "cnt": 40},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("OWM forecast failed: %s", exc)
        return None

    entries = data.get("list", [])
    if not entries:
        return None

    # Pick the forecast slot closest to first pitch
    best = min(entries, key=lambda e: abs(e["dt"] - target_ts))
    return _parse_forecast_entry(best)


def _parse_forecast_entry(e: dict) -> dict | None:
    try:
        main     = e.get("main", {})
        wind     = e.get("wind", {})
        weather  = (e.get("weather") or [{}])[0]
        pop      = e.get("pop", 0)   # probability of precipitation 0.0–1.0

        temp_f     = round(main.get("temp", 0), 1)
        feels_like = round(main.get("feels_like", temp_f), 1)
        wind_mph   = round(wind.get("speed", 0), 1)
        wind_deg   = wind.get("deg", 0)
        wid        = weather.get("id", 800)
        desc       = weather.get("description", "")
        precip_pct = round(pop * 100)

        condition = _condition_label(wid, desc)
        display   = _build_display(temp_f, wind_mph, wind_deg, precip_pct, condition)

        return {
            "temp_f":      temp_f,
            "feels_like":  feels_like,
            "wind_mph":    wind_mph,
            "wind_dir":    _deg_to_compass(wind_deg),
            "precip_pct":  precip_pct,
            "condition":   condition,
            "description": desc,
            "display":     display,
        }
    except Exception as exc:
        logger.warning("_parse_forecast_entry failed: %s", exc)
        return None


def _parse_current_entry(d: dict) -> dict | None:
    try:
        main     = d.get("main", {})
        wind     = d.get("wind", {})
        weather  = (d.get("weather") or [{}])[0]
        rain     = d.get("rain", {})
        precip_pct = min(100, round(rain.get("1h", 0) * 50))  # rough proxy

        temp_f     = round(main.get("temp", 0), 1)
        feels_like = round(main.get("feels_like", temp_f), 1)
        wind_mph   = round(wind.get("speed", 0), 1)
        wind_deg   = wind.get("deg", 0)
        wid        = weather.get("id", 800)
        desc       = weather.get("description", "")
        condition  = _condition_label(wid, desc)
        display    = _build_display(temp_f, wind_mph, wind_deg, precip_pct, condition)

        return {
            "temp_f":      temp_f,
            "feels_like":  feels_like,
            "wind_mph":    wind_mph,
            "wind_dir":    _deg_to_compass(wind_deg),
            "precip_pct":  precip_pct,
            "condition":   condition,
            "description": desc,
            "display":     display,
        }
    except Exception as exc:
        logger.warning("_parse_current_entry failed: %s", exc)
        return None


def _build_display(temp_f, wind_mph, wind_deg, precip_pct, condition) -> str:
    wind_dir = _deg_to_compass(wind_deg)
    parts = [f"{condition}", f"{temp_f}°F", f"💨 {wind_mph} mph {wind_dir}"]
    if precip_pct >= 10:
        parts.append(f"🌧 {precip_pct}%")
    return "  ·  ".join(parts)


if __name__ == "__main__":
    import json
    import sys
    from datetime import date, timedelta

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not os.getenv("OPENWEATHERMAP_API_KEY"):
        print("Set OPENWEATHERMAP_API_KEY first.")
        sys.exit(1)

    # Test a few parks
    tomorrow = (date.today() + timedelta(days=0)).strftime("%Y-%m-%dT18:00:00Z")
    teams = ["NYY", "LAD", "CHC", "HOU", "SEA", "MIA"]
    print(f"\nWeather near first pitch ({tomorrow[:10]}):\n")
    for team in teams:
        w = fetch_weather(team, tomorrow)
        if w:
            print(f"  {team:<4}  {w['display']}")
        else:
            print(f"  {team:<4}  —")
