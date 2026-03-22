#!/usr/bin/env python3
"""
Daily Briefing Generator
========================
Pulls data from WHOOP, OpenWeatherMap, NewsAPI, Yahoo Finance,
sunrise-sunset.org, and ZenQuotes, then assembles a beautiful
HTML email and sends it to you each morning.

Usage:
    python generate_briefing.py                  # Generate & email
    python generate_briefing.py --preview        # Generate & open in browser
    python generate_briefing.py --location 40.71 -74.00 "New York City"
"""

import os
import sys
import json
import smtplib
import argparse
import webbrowser
import tempfile
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yfinance as yf
from dotenv import load_dotenv

import platform

def fmt_date(dt, fmt):
    """Cross-platform strftime: converts %-d / %-I (Unix) to %#d / %#I (Windows)."""
    if platform.system() == "Windows":
        fmt = fmt.replace("%-", "%#")
    return dt.strftime(fmt)

# ─── Load environment ───
load_dotenv()

WHOOP_CLIENT_ID = os.getenv("WHOOP_CLIENT_ID", "")
WHOOP_CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET", "")
WHOOP_REFRESH_TOKEN = os.getenv("WHOOP_REFRESH_TOKEN", "")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
EMAIL_SENDER = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT", "")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
DEFAULT_LAT = float(os.getenv("DEFAULT_LAT", "40.7128"))
DEFAULT_LON = float(os.getenv("DEFAULT_LON", "-74.0060"))
DEFAULT_CITY = os.getenv("DEFAULT_CITY", "New York City")

TOKEN_FILE = Path(__file__).parent / ".whoop_tokens.json"


# ═══════════════════════════════════════════════
#  DATA FETCHERS
# ═══════════════════════════════════════════════

def get_whoop_tokens():
    """Refresh WHOOP access token using the refresh token."""
    # Load saved tokens if available
    saved_refresh = WHOOP_REFRESH_TOKEN
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE) as f:
            saved = json.load(f)
            saved_refresh = saved.get("refresh_token", WHOOP_REFRESH_TOKEN)

    try:
        resp = requests.post(
            "https://api.prod.whoop.com/oauth/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": WHOOP_CLIENT_ID,
                "client_secret": WHOOP_CLIENT_SECRET,
                "refresh_token": saved_refresh,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        tokens = resp.json()

        # Save new tokens for next time
        with open(TOKEN_FILE, "w") as f:
            json.dump({
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token", saved_refresh),
            }, f)

        return tokens["access_token"]
    except Exception as e:
        print(f"⚠ WHOOP token refresh failed: {e}")
        return None


def fetch_whoop_data():
    """Fetch recovery, sleep, and cycle data from WHOOP API v2."""
    access_token = get_whoop_tokens()
    if not access_token:
        return None

    headers = {"Authorization": f"Bearer {access_token}"}
    base = "https://api.prod.whoop.com/developer/v2"

    data = {}
    try:
        # Recovery (most recent)
        r = requests.get(f"{base}/recovery", headers=headers,
                         params={"limit": 1}, timeout=15)
        r.raise_for_status()
        records = r.json().get("records", [])
        if records:
            rec = records[0]
            score = rec.get("score", {})
            data["recovery_pct"] = round(score.get("recovery_score", 0))
            data["hrv"] = round(score.get("hrv_rmssd_milli", 0))
            data["rhr"] = round(score.get("resting_heart_rate", 0))

        # Sleep (most recent)
        r = requests.get(f"{base}/activity/sleep", headers=headers,
                         params={"limit": 1}, timeout=15)
        r.raise_for_status()
        records = r.json().get("records", [])
        if records:
            slp = records[0]
            score = slp.get("score", {})

            # Total sleep in ms → hours and minutes
            total_ms = score.get("stage_summary", {}).get("total_in_bed_time_milli", 0)
            total_sleep_ms = score.get("stage_summary", {}).get("total_light_sleep_time_milli", 0) + \
                             score.get("stage_summary", {}).get("total_slow_wave_sleep_time_milli", 0) + \
                             score.get("stage_summary", {}).get("total_rem_sleep_time_milli", 0)

            hours = int(total_sleep_ms // 3_600_000)
            mins = int((total_sleep_ms % 3_600_000) // 60_000)
            data["sleep_duration"] = f"{hours}h {mins}m"
            data["sleep_duration_mins"] = hours * 60 + mins

            # Sleep performance
            data["sleep_performance"] = round(score.get("sleep_performance_percentage", 0))

            # Sleep stages as percentages
            if total_sleep_ms > 0:
                stages = score.get("stage_summary", {})
                awake_ms = stages.get("total_awake_time_milli", 0)
                light_ms = stages.get("total_light_sleep_time_milli", 0)
                deep_ms = stages.get("total_slow_wave_sleep_time_milli", 0)
                rem_ms = stages.get("total_rem_sleep_time_milli", 0)
                total = awake_ms + light_ms + deep_ms + rem_ms
                if total > 0:
                    data["stage_awake"] = round(awake_ms / total * 100)
                    data["stage_light"] = round(light_ms / total * 100)
                    data["stage_deep"] = round(deep_ms / total * 100)
                    data["stage_rem"] = round(rem_ms / total * 100)

        # Cycle (for strain)
        r = requests.get(f"{base}/cycle", headers=headers,
                         params={"limit": 1}, timeout=15)
        r.raise_for_status()
        records = r.json().get("records", [])
        if records:
            cycle_score = records[0].get("score", {})
            data["prev_strain"] = round(cycle_score.get("strain", 0), 1)

    except Exception as e:
        print(f"⚠ WHOOP data fetch error: {e}")

    return data if data else None


def fetch_weather(lat, lon):
    """Fetch current weather from OpenWeatherMap. Tries 3.0 first, falls back to free 2.5."""
    if not OPENWEATHER_API_KEY:
        print("⚠ No OpenWeather API key set")
        return None

    try:
        # Try One Call 3.0 first
        r = requests.get(
            "https://api.openweathermap.org/data/3.0/onecall",
            params={
                "lat": lat, "lon": lon,
                "appid": OPENWEATHER_API_KEY,
                "units": "imperial",
                "exclude": "minutely,hourly,alerts",
            },
            timeout=15,
        )

        if r.status_code == 401 or r.status_code == 403:
            # Fall back to free 2.5 API
            print("  ↳ 3.0 not available, using free 2.5 API...")
            return _fetch_weather_25(lat, lon)

        r.raise_for_status()
        d = r.json()

        current = d.get("current", {})
        daily = d.get("daily", [{}])[0]
        weather_desc = current.get("weather", [{}])[0].get("main", "Clear")

        feels = round(current.get("feels_like", 0))
        temp = round(current.get("temp", 0))
        rain_chance = round(daily.get("pop", 0) * 100)
        wind = round(current.get("wind_speed", 0))

        advisory_parts = _build_advisory(temp, feels, rain_chance, wind)

        return {
            "temp": temp,
            "feels_like": feels,
            "condition": weather_desc,
            "high": round(daily.get("temp", {}).get("max", 0)),
            "low": round(daily.get("temp", {}).get("min", 0)),
            "humidity": current.get("humidity", 0),
            "wind": wind,
            "uvi": round(daily.get("uvi", 0)),
            "rain_chance": rain_chance,
            "advisory": " ".join(advisory_parts) if advisory_parts else "Comfortable conditions today.",
        }
    except Exception as e:
        print(f"⚠ Weather 3.0 error: {e}, trying 2.5...")
        try:
            return _fetch_weather_25(lat, lon)
        except Exception as e2:
            print(f"⚠ Weather 2.5 also failed: {e2}")
            return None


def _fetch_weather_25(lat, lon):
    """Fallback: use free OpenWeatherMap 2.5 API (current weather + forecast)."""
    # Current weather
    r = requests.get(
        "https://api.openweathermap.org/data/2.5/weather",
        params={
            "lat": lat, "lon": lon,
            "appid": OPENWEATHER_API_KEY,
            "units": "imperial",
        },
        timeout=15,
    )
    r.raise_for_status()
    current = r.json()

    # 5-day forecast for high/low and rain chance
    r2 = requests.get(
        "https://api.openweathermap.org/data/2.5/forecast",
        params={
            "lat": lat, "lon": lon,
            "appid": OPENWEATHER_API_KEY,
            "units": "imperial",
            "cnt": 8,  # Next 24 hours (3-hour intervals)
        },
        timeout=15,
    )
    r2.raise_for_status()
    forecast = r2.json()

    # Calculate high/low from forecast
    temps = [item["main"]["temp"] for item in forecast.get("list", [])]
    rain_probs = [item.get("pop", 0) for item in forecast.get("list", [])]

    temp = round(current["main"]["temp"])
    feels = round(current["main"]["feels_like"])
    wind = round(current.get("wind", {}).get("speed", 0))
    rain_chance = round(max(rain_probs, default=0) * 100)

    advisory_parts = _build_advisory(temp, feels, rain_chance, wind)

    return {
        "temp": temp,
        "feels_like": feels,
        "condition": current.get("weather", [{}])[0].get("main", "Clear"),
        "high": round(max(temps, default=temp)),
        "low": round(min(temps, default=temp)),
        "humidity": current["main"].get("humidity", 0),
        "wind": wind,
        "uvi": 0,  # Not available in free 2.5 API
        "rain_chance": rain_chance,
        "advisory": " ".join(advisory_parts) if advisory_parts else "Comfortable conditions today.",
    }


def _build_advisory(temp, feels, rain_chance, wind):
    """Build weather advisory text."""
    parts = []
    if feels < temp - 5:
        parts.append(f"🧥 Wind chill makes it feel {temp - feels}° colder than actual temp.")
    elif feels > temp + 5:
        parts.append(f"🥵 Humidity makes it feel {feels - temp}° warmer.")
    if rain_chance > 40:
        parts.append(f"☂️ {rain_chance}% chance of rain — bring an umbrella.")
    elif rain_chance <= 20:
        parts.append("No umbrella needed today.")
    if wind > 15:
        parts.append(f"💨 Gusty winds at {wind} mph.")
    return parts


def fetch_news():
    """Fetch top 3 business headlines + 1 VC/startup headline from NewsAPI."""
    if not NEWSAPI_KEY:
        print("⚠ No NewsAPI key set")
        return None

    headers = {"X-Api-Key": NEWSAPI_KEY}
    articles = []

    try:
        # Top business news from quality sources
        r = requests.get(
            "https://newsapi.org/v2/top-headlines",
            params={
                "category": "business",
                "language": "en",
                "pageSize": 10,
                "country": "us",
            },
            headers=headers,
            timeout=15,
        )
        r.raise_for_status()
        biz_articles = r.json().get("articles", [])

        # Take top 3 business articles
        for a in biz_articles[:3]:
            articles.append({
                "source": a.get("source", {}).get("name", "Unknown"),
                "headline": a.get("title", ""),
                "summary": a.get("description", ""),
                "url": a.get("url", ""),
                "is_vc": False,
            })

        # VC / Startup news
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": "venture capital OR startup funding OR Series A OR Series B OR seed round",
                "domains": "techcrunch.com,theinformation.com,pitchbook.com,crunchbase.com,fortune.com",
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 5,
            },
            headers=headers,
            timeout=15,
        )
        r.raise_for_status()
        vc_articles = r.json().get("articles", [])

        if vc_articles:
            a = vc_articles[0]
            articles.append({
                "source": a.get("source", {}).get("name", "Unknown") + " · Venture Capital",
                "headline": a.get("title", ""),
                "summary": a.get("description", ""),
                "url": a.get("url", ""),
                "is_vc": True,
            })

    except Exception as e:
        print(f"⚠ News fetch error: {e}")

    return articles if articles else None


def fetch_ai_pulse():
    """Fetch the top AI/tech development of the day."""
    if not NEWSAPI_KEY:
        return None

    try:
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": "artificial intelligence OR AI model OR machine learning OR LLM",
                "domains": "theverge.com,arstechnica.com,technologyreview.com,wired.com,techcrunch.com",
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 3,
            },
            headers={"X-Api-Key": NEWSAPI_KEY},
            timeout=15,
        )
        r.raise_for_status()
        articles = r.json().get("articles", [])

        if articles:
            a = articles[0]
            return {
                "source": a.get("source", {}).get("name", "Unknown"),
                "headline": a.get("title", ""),
                "summary": a.get("description", ""),
                "url": a.get("url", ""),
            }
    except Exception as e:
        print(f"⚠ AI pulse fetch error: {e}")

    return None


def fetch_markets():
    """Fetch market data using yfinance."""
    tickers = {
        "S&P 500": "^GSPC",
        "Nasdaq": "^IXIC",
        "Dow Jones": "^DJI",
        "10Y Treasury": "^TNX",
        "Bitcoin": "BTC-USD",
        "Gold": "GC=F",
    }

    results = []
    for name, symbol in tickers.items():
        try:
            tk = yf.Ticker(symbol)
            hist = tk.history(period="2d")
            if len(hist) >= 2:
                prev_close = hist["Close"].iloc[-2]
                last_close = hist["Close"].iloc[-1]
                change_pct = ((last_close - prev_close) / prev_close) * 100

                # Format price
                if name == "10Y Treasury":
                    price_str = f"{last_close:.2f}%"
                elif name == "Bitcoin":
                    price_str = f"${last_close:,.0f}"
                elif name == "Gold":
                    price_str = f"${last_close:,.0f}"
                else:
                    price_str = f"{last_close:,.0f}"

                results.append({
                    "name": name,
                    "price": price_str,
                    "change_pct": change_pct,
                    "direction": "up" if change_pct >= 0 else "down",
                    "change_display": f"{'▲' if change_pct >= 0 else '▼'} {abs(change_pct):.2f}%",
                })
            elif len(hist) == 1:
                last_close = hist["Close"].iloc[-1]
                if name == "10Y Treasury":
                    price_str = f"{last_close:.2f}%"
                elif name in ("Bitcoin", "Gold"):
                    price_str = f"${last_close:,.0f}"
                else:
                    price_str = f"{last_close:,.0f}"
                results.append({
                    "name": name, "price": price_str,
                    "change_pct": 0, "direction": "up",
                    "change_display": "— N/A",
                })
        except Exception as e:
            print(f"⚠ Market fetch error for {name}: {e}")

    return results if results else None


def fetch_sun_times(lat, lon):
    """Fetch sunrise/sunset from sunrise-sunset.org API."""
    try:
        r = requests.get(
            "https://api.sunrise-sunset.org/json",
            params={"lat": lat, "lng": lon, "formatted": 0, "date": "today"},
            timeout=15,
        )
        r.raise_for_status()
        results = r.json().get("results", {})

        def utc_to_local_str(iso_str):
            """Convert UTC ISO string to local 12-hour time string."""
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            try:
                from zoneinfo import ZoneInfo
                local_dt = dt.astimezone(ZoneInfo("America/New_York"))
            except ImportError:
                # Fallback: use system local time
                local_dt = dt.astimezone()
            return fmt_date(local_dt, "%-I:%M %p")

        sunrise_str = results.get("sunrise", "")
        sunset_str = results.get("sunset", "")

        if not sunrise_str or not sunset_str:
            return None

        sunrise = utc_to_local_str(sunrise_str)
        sunset = utc_to_local_str(sunset_str)

        # Golden hour is ~30 min before sunset to sunset
        sunset_dt = datetime.fromisoformat(sunset_str.replace("Z", "+00:00"))
        try:
            from zoneinfo import ZoneInfo
            sunset_local = sunset_dt.astimezone(ZoneInfo("America/New_York"))
        except ImportError:
            sunset_local = sunset_dt.astimezone()

        golden_start = fmt_date(sunset_local - timedelta(minutes=32), "%-I:%M")
        golden_end = fmt_date(sunset_local, "%-I:%M %p")

        return {
            "sunrise": sunrise,
            "sunset": sunset,
            "golden_hour": f"{golden_start} – {golden_end}",
        }
    except Exception as e:
        print(f"⚠ Sun times fetch error: {e}")
        return None


def fetch_quote():
    """Fetch a daily quote from ZenQuotes API."""
    try:
        r = requests.get("https://zenquotes.io/api/today", timeout=10)
        r.raise_for_status()
        data = r.json()
        if data and isinstance(data, list):
            return {
                "text": data[0].get("q", ""),
                "author": data[0].get("a", "Unknown"),
            }
    except Exception as e:
        print(f"⚠ Quote fetch error: {e}")

    return {"text": "The best time to plant a tree was 20 years ago. The second best time is now.", "author": "Chinese Proverb"}


# ═══════════════════════════════════════════════
#  WHOOP DATA INTERPRETATION
# ═══════════════════════════════════════════════

def interpret_recovery(pct):
    """Return recovery zone status and advice."""
    if pct >= 67:
        return {
            "status": "Peak — You're Primed",
            "color": "green",
            "advice": "Your body is well-recovered. Today is a great day for a high-strain workout or competitive effort.",
            "strain_low": 14.0,
            "strain_high": 18.0,
        }
    elif pct >= 34:
        return {
            "status": "Steady — Maintaining",
            "color": "amber",
            "advice": "Your body is maintaining. Moderate strain is fine today — listen to how you feel and don't push to max effort.",
            "strain_low": 8.0,
            "strain_high": 14.0,
        }
    else:
        return {
            "status": "Rest — Recovery Day",
            "color": "red",
            "advice": "Your body needs rest. Focus on active recovery — light walking, stretching, or mobility work. Prioritize sleep tonight.",
            "strain_low": 0.0,
            "strain_high": 8.0,
        }


# ═══════════════════════════════════════════════
#  HTML TEMPLATE
# ═══════════════════════════════════════════════

def ring_dashoffset(pct):
    """Calculate SVG stroke-dashoffset for a percentage (circle r=60, circumference=377)."""
    return round(377 * (1 - pct / 100))


def build_html(whoop, weather, news, markets, ai_pulse, sun, quote, city, lat, lon):
    """Assemble the full HTML briefing."""
    now = datetime.now()
    date_str = fmt_date(now, "%A · %B %-d, %Y")
    hour = now.hour
    greeting = "Good Morning" if hour < 12 else "Good Afternoon" if hour < 17 else "Good Evening"

    # ── WHOOP section ──
    if whoop:
        recovery_pct = whoop.get("recovery_pct", 50)
        sleep_perf = whoop.get("sleep_performance", 80)
        rec_info = interpret_recovery(recovery_pct)
        rec_color_var = {"green": "var(--accent-green)", "amber": "var(--accent-amber)", "red": "var(--accent-red)"}[rec_info["color"]]
        rec_color_dim = {"green": "var(--accent-green-dim)", "amber": "var(--accent-amber-dim)", "red": "var(--accent-red-dim)"}[rec_info["color"]]

        whoop_html = f"""
    <div class="section-label">Sleep &amp; Recovery</div>
    <div class="recovery-hero">
      <div class="recovery-rings">
        <div class="recovery-ring">
          <svg viewBox="0 0 130 130">
            <circle class="recovery-ring-bg" cx="65" cy="65" r="60"/>
            <circle class="recovery-ring-fill" style="stroke:{rec_color_var};stroke-dashoffset:{ring_dashoffset(recovery_pct)}" cx="65" cy="65" r="60"/>
          </svg>
          <div class="recovery-ring-label">
            <span class="recovery-ring-value" style="color:{rec_color_var}">{recovery_pct}%</span>
            <span class="recovery-ring-unit">Recovery</span>
          </div>
        </div>
        <div class="recovery-ring">
          <svg viewBox="0 0 130 130">
            <circle class="recovery-ring-bg" cx="65" cy="65" r="60"/>
            <circle class="recovery-ring-fill" style="stroke:var(--accent-blue);stroke-dashoffset:{ring_dashoffset(sleep_perf)}" cx="65" cy="65" r="60"/>
          </svg>
          <div class="recovery-ring-label">
            <span class="recovery-ring-value" style="color:var(--accent-blue)">{sleep_perf}%</span>
            <span class="recovery-ring-unit">Sleep Perf</span>
          </div>
        </div>
      </div>
      <div class="recovery-details">
        <div class="recovery-status">{rec_info["status"]}</div>
        <div class="recovery-advice">{rec_info["advice"]} You slept {sleep_perf}% of what your body needed.</div>
        <div class="strain-target" style="background:{rec_color_dim};color:{rec_color_var}">⎯ Strain Target: {rec_info["strain_low"]:.1f}–{rec_info["strain_high"]:.1f}</div>
      </div>
    </div>
    <div class="stat-row">
      <div class="stat-card">
        <div class="stat-value">{whoop.get("sleep_duration", "—")}</div>
        <div class="stat-label">Sleep</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{whoop.get("hrv", "—")}</div>
        <div class="stat-label">HRV (ms)</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{whoop.get("rhr", "—")}</div>
        <div class="stat-label">RHR (bpm)</div>
      </div>
    </div>
    <div class="sleep-stages">
      <div class="sleep-stages-title">Sleep Stages</div>
      <div class="stages-bar">
        <div class="stage-awake" style="flex:{whoop.get('stage_awake', 8)}"></div>
        <div class="stage-light" style="flex:{whoop.get('stage_light', 42)}"></div>
        <div class="stage-deep" style="flex:{whoop.get('stage_deep', 22)}"></div>
        <div class="stage-rem" style="flex:{whoop.get('stage_rem', 28)}"></div>
      </div>
      <div class="stages-legend">
        <div class="legend-item"><div class="legend-dot" style="background:var(--accent-amber)"></div> Awake {whoop.get('stage_awake', 8)}%</div>
        <div class="legend-item"><div class="legend-dot" style="background:var(--accent-blue)"></div> Light {whoop.get('stage_light', 42)}%</div>
        <div class="legend-item"><div class="legend-dot" style="background:var(--accent-purple)"></div> Deep {whoop.get('stage_deep', 22)}%</div>
        <div class="legend-item"><div class="legend-dot" style="background:var(--accent-green)"></div> REM {whoop.get('stage_rem', 28)}%</div>
      </div>
    </div>"""
    else:
        whoop_html = """
    <div class="section-label">Sleep &amp; Recovery</div>
    <div class="weather-advisory" style="background:var(--accent-amber-dim);border-left-color:var(--accent-amber);color:var(--accent-amber);">
      ⚠ WHOOP data unavailable. Check your API connection in .env
    </div>"""

    # ── Weather section ──
    if weather:
        weather_html = f"""
    <div class="section-label">Weather — {city}</div>
    <div class="weather-card">
      <div class="weather-main">
        <div><span class="weather-temp">{weather["temp"]}<span class="weather-temp-unit">°F</span></span></div>
        <div class="weather-desc">
          <div class="weather-condition">{weather["condition"]}</div>
          <div class="weather-feels">Feels like {weather["feels_like"]}°F · High {weather["high"]}° / Low {weather["low"]}°</div>
        </div>
      </div>
      <div class="weather-details">
        <div class="weather-detail"><div class="weather-detail-value">{weather["humidity"]}%</div><div class="weather-detail-label">Humidity</div></div>
        <div class="weather-detail"><div class="weather-detail-value">{weather["wind"]} mph</div><div class="weather-detail-label">Wind</div></div>
        <div class="weather-detail"><div class="weather-detail-value">{weather["uvi"]}</div><div class="weather-detail-label">UV Index</div></div>
        <div class="weather-detail"><div class="weather-detail-value">{weather["rain_chance"]}%</div><div class="weather-detail-label">Rain</div></div>
      </div>
      <div class="weather-advisory">{weather["advisory"]}</div>
    </div>"""
    else:
        weather_html = ""

    # ── News section ──
    news_html = ""
    if news:
        news_html = '\n    <div class="section-label">Top Business Stories</div>\n'
        for i, article in enumerate(news):
            source_style = ' style="color: var(--accent-green);"' if article.get("is_vc") else ""
            news_html += f"""
    <div class="news-item" style="--i:{i}">
      <div class="news-source"{source_style}>{article["source"]}</div>
      <div class="news-headline">{article["headline"]}</div>
      <div class="news-summary">{article["summary"] or ""}</div>
    </div>\n"""

    # ── Markets section ──
    markets_html = ""
    if markets:
        markets_html = '\n    <div class="section-label">Market Snapshot — Yesterday\'s Close</div>\n    <div class="markets-grid">\n'
        for i, m in enumerate(markets):
            markets_html += f"""      <div class="market-card" style="--i:{i}">
        <div class="market-name">{m["name"]}</div>
        <div class="market-price">{m["price"]}</div>
        <div class="market-change market-{m["direction"]}">{m["change_display"]}</div>
      </div>\n"""
        markets_html += "    </div>\n"

    # ── AI Pulse section ──
    if ai_pulse:
        ai_html = f"""
    <div class="section-label">AI / Tech Pulse</div>
    <div class="ai-pulse-card">
      <div class="ai-pulse-source">{ai_pulse["source"]}</div>
      <div class="ai-pulse-headline">{ai_pulse["headline"]}</div>
      <div class="ai-pulse-summary">{ai_pulse["summary"] or ""}</div>
    </div>"""
    else:
        ai_html = ""

    # ── Sun times section ──
    if sun:
        sun_html = f"""
    <div class="section-label">Daylight</div>
    <div class="sun-card">
      <div class="sun-item">
        <div class="sun-icon">🌅</div>
        <div>
          <div class="sun-time">{sun["sunrise"]}</div>
          <div class="sun-label">Sunrise</div>
        </div>
      </div>
      <div class="sun-divider"></div>
      <div class="sun-golden">
        <div class="sun-golden-value">{sun["golden_hour"]}</div>
        <div class="sun-golden-label">Golden Hour</div>
      </div>
      <div class="sun-divider"></div>
      <div class="sun-item">
        <div class="sun-icon">🌇</div>
        <div>
          <div class="sun-time">{sun["sunset"]}</div>
          <div class="sun-label">Sunset</div>
        </div>
      </div>
    </div>"""
    else:
        sun_html = ""

    # ── Quote section ──
    quote_html = f"""
    <div class="section-label">Daily Quote</div>
    <div class="quote-card">
      <div class="quote-mark">&ldquo;</div>
      <div class="quote-text">{quote["text"]}</div>
      <div class="quote-attr">— {quote["author"]}</div>
    </div>"""

    # ── Assemble full HTML ──
    # Read CSS from the template file
    css = get_css()

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Briefing — {date_str}</title>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;1,9..40,300;1,9..40,400&display=swap" rel="stylesheet">
<style>
{css}
</style>
</head>
<body>
<div class="container">

  <header class="header">
    <div class="header-date">{date_str}</div>
    <h1 class="header-title">{greeting}</h1>
    <p class="header-subtitle">Your daily briefing — 📍 {city}</p>
  </header>

  {whoop_html}

  <div class="divider"></div>

  {weather_html}

  <div class="divider"></div>

  {news_html}

  <div class="divider"></div>

  {markets_html}

  <div class="divider"></div>

  {ai_html}

  {sun_html}

  <div class="divider"></div>

  {quote_html}

  <footer class="footer">
    <p class="footer-text">
      Generated at {fmt_date(now, "%-I:%M %p")} · Data from WHOOP, OpenWeather, NewsAPI &amp; more
    </p>
  </footer>

</div>
</body>
</html>"""

    return html


def get_css():
    """Return the full CSS for the briefing."""
    return """:root {
    --bg: #0C0E12;
    --surface: #14171E;
    --surface-raised: #1A1E28;
    --border: #252A36;
    --text-primary: #E8EAF0;
    --text-secondary: #8A90A0;
    --text-muted: #555B6E;
    --accent-green: #44D7A8;
    --accent-green-dim: rgba(68,215,168,0.12);
    --accent-amber: #F5A623;
    --accent-amber-dim: rgba(245,166,35,0.12);
    --accent-red: #EF5350;
    --accent-red-dim: rgba(239,83,80,0.12);
    --accent-blue: #42A5F5;
    --accent-blue-dim: rgba(66,165,245,0.12);
    --accent-purple: #AB7AFF;
    --accent-purple-dim: rgba(171,122,255,0.12);
    --serif: 'Instrument Serif', Georgia, serif;
    --sans: 'DM Sans', -apple-system, sans-serif;
    --radius: 16px;
    --radius-sm: 10px;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text-primary); font-family: var(--sans); font-size: 15px; line-height: 1.6; -webkit-font-smoothing: antialiased; }
  .container { max-width: 680px; margin: 0 auto; padding: 48px 24px 80px; }
  .header { text-align: center; margin-bottom: 56px; }
  .header-date { font-size: 12px; font-weight: 500; letter-spacing: 2.5px; text-transform: uppercase; color: var(--accent-green); margin-bottom: 16px; }
  .header-title { font-family: var(--serif); font-size: 52px; font-weight: 400; line-height: 1.1; color: var(--text-primary); margin-bottom: 12px; }
  .header-subtitle { font-size: 15px; color: var(--text-secondary); font-weight: 300; }
  .divider { height: 1px; background: linear-gradient(90deg, transparent, var(--border), transparent); margin: 40px 0; }
  .section-label { font-size: 11px; font-weight: 600; letter-spacing: 2px; text-transform: uppercase; color: var(--text-muted); margin-bottom: 20px; display: flex; align-items: center; gap: 10px; }
  .section-label::after { content: ''; flex: 1; height: 1px; background: var(--border); }
  .recovery-hero { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 36px; margin-bottom: 20px; display: flex; align-items: center; gap: 32px; }
  .recovery-rings { display: flex; gap: 20px; flex-shrink: 0; }
  .recovery-ring { position: relative; width: 110px; height: 110px; flex-shrink: 0; }
  .recovery-ring svg { width: 100%; height: 100%; transform: rotate(-90deg); }
  .recovery-ring-bg { fill: none; stroke: var(--surface-raised); stroke-width: 8; }
  .recovery-ring-fill { fill: none; stroke: var(--accent-green); stroke-width: 8; stroke-linecap: round; stroke-dasharray: 377; stroke-dashoffset: 49; }
  .recovery-ring-label { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
  .recovery-ring-value { font-family: var(--serif); font-size: 34px; color: var(--accent-green); line-height: 1; }
  .recovery-ring-unit { font-size: 11px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 1.5px; margin-top: 4px; }
  .recovery-details { flex: 1; }
  .recovery-status { font-family: var(--serif); font-size: 26px; color: var(--text-primary); margin-bottom: 6px; }
  .recovery-advice { font-size: 14px; color: var(--text-secondary); line-height: 1.65; margin-bottom: 16px; }
  .strain-target { display: inline-flex; align-items: center; gap: 8px; padding: 8px 14px; border-radius: 20px; background: var(--accent-green-dim); font-size: 13px; font-weight: 500; color: var(--accent-green); }
  .stat-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px; }
  .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 18px; text-align: center; }
  .stat-value { font-family: var(--serif); font-size: 28px; color: var(--text-primary); line-height: 1.2; }
  .stat-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1.5px; margin-top: 4px; }
  .sleep-stages { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 22px 24px; margin-bottom: 40px; }
  .sleep-stages-title { font-size: 13px; font-weight: 500; color: var(--text-secondary); margin-bottom: 14px; }
  .stages-bar { display: flex; height: 14px; border-radius: 7px; overflow: hidden; margin-bottom: 14px; }
  .stage-awake { background: var(--accent-amber); }
  .stage-light { background: var(--accent-blue); }
  .stage-deep { background: var(--accent-purple); }
  .stage-rem { background: var(--accent-green); }
  .stages-legend { display: flex; gap: 20px; flex-wrap: wrap; }
  .legend-item { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-secondary); }
  .legend-dot { width: 8px; height: 8px; border-radius: 50%; }
  .weather-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 32px 36px; margin-bottom: 20px; }
  .weather-main { display: flex; align-items: center; gap: 28px; margin-bottom: 24px; }
  .weather-temp { font-family: var(--serif); font-size: 64px; line-height: 1; color: var(--text-primary); }
  .weather-temp-unit { font-size: 28px; color: var(--text-muted); vertical-align: super; }
  .weather-desc { flex: 1; }
  .weather-condition { font-size: 18px; font-weight: 500; color: var(--text-primary); margin-bottom: 4px; }
  .weather-feels { font-size: 14px; color: var(--text-secondary); }
  .weather-details { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
  .weather-detail { text-align: center; padding: 12px 0; border-radius: var(--radius-sm); background: var(--surface-raised); }
  .weather-detail-value { font-size: 16px; font-weight: 500; color: var(--text-primary); }
  .weather-detail-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; margin-top: 2px; }
  .weather-advisory { margin-top: 16px; padding: 14px 18px; border-radius: var(--radius-sm); background: var(--accent-blue-dim); border-left: 3px solid var(--accent-blue); font-size: 13px; color: var(--accent-blue); line-height: 1.5; }
  .news-item { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 24px 28px; margin-bottom: 12px; }
  .news-source { font-size: 11px; font-weight: 600; letter-spacing: 1.5px; text-transform: uppercase; color: var(--accent-amber); margin-bottom: 8px; }
  .news-headline { font-family: var(--serif); font-size: 20px; line-height: 1.35; color: var(--text-primary); margin-bottom: 8px; }
  .news-summary { font-size: 14px; color: var(--text-secondary); line-height: 1.65; }
  .markets-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 40px; }
  .market-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 20px; }
  .market-name { font-size: 12px; font-weight: 500; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
  .market-price { font-family: var(--serif); font-size: 22px; color: var(--text-primary); margin-bottom: 4px; }
  .market-change { font-size: 13px; font-weight: 500; }
  .market-up { color: var(--accent-green); }
  .market-down { color: var(--accent-red); }
  .ai-pulse-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 24px 28px; margin-bottom: 40px; }
  .ai-pulse-source { font-size: 11px; font-weight: 600; letter-spacing: 1.5px; text-transform: uppercase; color: var(--accent-purple); margin-bottom: 8px; }
  .ai-pulse-headline { font-family: var(--serif); font-size: 20px; line-height: 1.35; color: var(--text-primary); margin-bottom: 8px; }
  .ai-pulse-summary { font-size: 14px; color: var(--text-secondary); line-height: 1.65; margin-bottom: 14px; }
  .ai-pulse-why { padding: 12px 16px; border-radius: var(--radius-sm); background: var(--accent-purple-dim); border-left: 3px solid var(--accent-purple); font-size: 13px; color: var(--accent-purple); line-height: 1.5; }
  .sun-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 22px 28px; margin-bottom: 40px; display: flex; align-items: center; justify-content: space-between; }
  .sun-item { display: flex; align-items: center; gap: 12px; }
  .sun-icon { font-size: 28px; line-height: 1; }
  .sun-time { font-family: var(--serif); font-size: 22px; color: var(--text-primary); line-height: 1.2; }
  .sun-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1.5px; }
  .sun-divider { width: 1px; height: 40px; background: var(--border); }
  .sun-golden { text-align: center; }
  .sun-golden-value { font-family: var(--serif); font-size: 18px; color: var(--accent-amber); line-height: 1.2; }
  .sun-golden-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1.5px; }
  .quote-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 36px 40px; text-align: center; margin-bottom: 40px; }
  .quote-mark { font-family: var(--serif); font-size: 72px; line-height: 0.5; color: var(--accent-green); opacity: 0.3; margin-bottom: 12px; }
  .quote-text { font-family: var(--serif); font-style: italic; font-size: 22px; line-height: 1.55; color: var(--text-primary); margin-bottom: 16px; }
  .quote-attr { font-size: 13px; color: var(--text-muted); letter-spacing: 0.5px; }
  .footer { text-align: center; padding-top: 32px; border-top: 1px solid var(--border); }
  .footer-text { font-size: 12px; color: var(--text-muted); line-height: 1.7; }
  @media (max-width: 600px) {
    .header-title { font-size: 38px; }
    .recovery-hero { flex-direction: column; text-align: center; padding: 28px 24px; gap: 24px; }
    .recovery-rings { justify-content: center; }
    .stat-row { grid-template-columns: repeat(3, 1fr); gap: 8px; }
    .stat-card { padding: 14px 8px; }
    .stat-value { font-size: 22px; }
    .weather-details { grid-template-columns: repeat(2, 1fr); }
    .markets-grid { grid-template-columns: repeat(2, 1fr); }
    .sun-card { flex-wrap: wrap; gap: 16px; justify-content: center; }
    .sun-divider { width: 40px; height: 1px; }
    .weather-temp { font-size: 48px; }
    .quote-text { font-size: 18px; }
  }"""


# ═══════════════════════════════════════════════
#  EMAIL DELIVERY
# ═══════════════════════════════════════════════

def send_email(html_content, date_str):
    """Send the briefing as an HTML email."""
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT]):
        print("⚠ Email credentials not configured — skipping send")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"☀️ Your Daily Briefing — {date_str}"
    msg["From"] = f"Daily Briefing <{EMAIL_SENDER}>"
    msg["To"] = EMAIL_RECIPIENT

    # Plain text fallback
    text_part = MIMEText("Your daily briefing is ready. View this email in an HTML-capable client.", "plain")
    html_part = MIMEText(html_content, "html")

    msg.attach(text_part)
    msg.attach(html_part)

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
        print(f"✅ Briefing emailed to {EMAIL_RECIPIENT}")
        return True
    except Exception as e:
        print(f"❌ Email send failed: {e}")
        return False


# ═══════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Generate your daily briefing")
    parser.add_argument("--preview", action="store_true", help="Open in browser instead of emailing")
    parser.add_argument("--location", nargs=3, metavar=("LAT", "LON", "CITY"),
                        help="Override location (lat lon city_name)")
    parser.add_argument("--output", type=str, help="Save HTML to file path")
    args = parser.parse_args()

    # Location
    lat = float(args.location[0]) if args.location else DEFAULT_LAT
    lon = float(args.location[1]) if args.location else DEFAULT_LON
    city = args.location[2] if args.location else DEFAULT_CITY

    print(f"📍 Location: {city} ({lat}, {lon})")
    print("─" * 50)

    # Fetch all data
    print("🟢 Fetching WHOOP data...")
    whoop = fetch_whoop_data()

    print("🌤  Fetching weather...")
    weather = fetch_weather(lat, lon)

    print("📰 Fetching news...")
    news = fetch_news()

    print("🤖 Fetching AI pulse...")
    ai_pulse = fetch_ai_pulse()

    print("📈 Fetching markets...")
    markets = fetch_markets()

    print("🌅 Fetching sun times...")
    sun = fetch_sun_times(lat, lon)

    print("💬 Fetching daily quote...")
    quote = fetch_quote()

    print("─" * 50)
    print("🔨 Building HTML...")

    html = build_html(whoop, weather, news, markets, ai_pulse, sun, quote, city, lat, lon)

    date_str = fmt_date(datetime.now(), "%B %-d, %Y")

    if args.output:
        Path(args.output).write_text(html)
        print(f"💾 Saved to {args.output}")

    if args.preview:
        # Open in browser
        tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w")
        tmp.write(html)
        tmp.close()
        webbrowser.open(f"file://{tmp.name}")
        print(f"🌐 Opened in browser: {tmp.name}")
    else:
        # Send email
        send_email(html, date_str)

        # Also save a copy locally
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(exist_ok=True)
        filename = f"briefing-{datetime.now().strftime('%Y-%m-%d')}.html"
        (output_dir / filename).write_text(html)
        print(f"💾 Saved copy to output/{filename}")


if __name__ == "__main__":
    main()
