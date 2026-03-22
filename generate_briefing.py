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
    # Load saved tokens — check multiple sources in priority order
    saved_refresh = WHOOP_REFRESH_TOKEN

    # 1. Check local JSON token file (from previous local runs)
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE) as f:
            saved = json.load(f)
            saved_refresh = saved.get("refresh_token", saved_refresh)

    # 2. Check committed token file (from GitHub Actions previous runs)
    committed_token_file = Path(__file__).parent / ".whoop_refresh_token"
    if committed_token_file.exists():
        token_from_file = committed_token_file.read_text().strip()
        if token_from_file:
            saved_refresh = token_from_file

    try:
        resp = requests.post(
            "https://api.prod.whoop.com/oauth/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": WHOOP_CLIENT_ID,
                "client_secret": WHOOP_CLIENT_SECRET,
                "refresh_token": saved_refresh,
                "scope": "offline",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        tokens = resp.json()

        new_refresh = tokens.get("refresh_token", saved_refresh)

        # Save new tokens locally for next time
        with open(TOKEN_FILE, "w") as f:
            json.dump({
                "access_token": tokens["access_token"],
                "refresh_token": new_refresh,
            }, f)

        # Write new refresh token to a file for GitHub Actions to update the secret
        new_token_file = Path(__file__).parent / ".new_refresh_token"
        new_token_file.write_text(new_refresh)

        return tokens["access_token"]
    except Exception as e:
        print(f"WHOOP token refresh failed: {e}")
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
    """Assemble email-compatible HTML briefing using tables and inline styles."""
    now = datetime.now()
    date_str = fmt_date(now, "%A, %B %-d, %Y")
    hour = now.hour
    greeting = "Good Morning" if hour < 12 else "Good Afternoon" if hour < 17 else "Good Evening"

    # Colors — clean light theme
    C = {
        "bg": "#F4F5F7", "surface": "#FFFFFF", "raised": "#F0F1F3", "border": "#E2E4E9",
        "text": "#1A1D23", "text2": "#5F6577", "muted": "#9198A8",
        "green": "#0D9B6E", "green_dim": "#E8F7F1",
        "amber": "#C47E0A", "amber_dim": "#FFF6E5",
        "red": "#D93025", "red_dim": "#FDECEA",
        "blue": "#1A73E8", "blue_dim": "#E8F0FE",
        "purple": "#7B3FE4", "purple_dim": "#F3ECFD",
    }

    def section_label(text):
        return f'''<tr><td style="padding:32px 0 16px 0;font-size:11px;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:{C["muted"]};border-bottom:1px solid {C["border"]};">{text}</td></tr>'''

    def card_start(extra_style=""):
        return f'<tr><td style="padding:8px 0;"><table width="100%" cellpadding="0" cellspacing="0" style="background-color:{C["surface"]};border:1px solid {C["border"]};border-radius:12px;{extra_style}"><tr><td style="padding:24px;">'

    def card_end():
        return '</td></tr></table></td></tr>'

    def divider():
        return f'<tr><td style="padding:20px 0;"><table width="100%" cellpadding="0" cellspacing="0"><tr><td style="height:1px;background:{C["border"]};"></td></tr></table></td></tr>'

    # ── WHOOP section ──
    whoop_section = ""
    if whoop:
        recovery_pct = whoop.get("recovery_pct", 50)
        sleep_perf = whoop.get("sleep_performance", 80)
        rec_info = interpret_recovery(recovery_pct)
        rec_color = {"green": C["green"], "amber": C["amber"], "red": C["red"]}[rec_info["color"]]
        rec_bg = {"green": C["green_dim"], "amber": C["amber_dim"], "red": C["red_dim"]}[rec_info["color"]]

        whoop_section = f'''
        {section_label("SLEEP &amp; RECOVERY")}
        {card_start()}
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td width="100" style="text-align:center;vertical-align:top;padding-right:16px;">
            <div style="font-size:42px;font-weight:300;color:{rec_color};line-height:1;">{recovery_pct}%</div>
            <div style="font-size:11px;color:{C["text2"]};text-transform:uppercase;letter-spacing:1.5px;margin-top:4px;">Recovery</div>
          </td>
          <td width="100" style="text-align:center;vertical-align:top;padding-right:20px;">
            <div style="font-size:42px;font-weight:300;color:{C["blue"]};line-height:1;">{sleep_perf}%</div>
            <div style="font-size:11px;color:{C["text2"]};text-transform:uppercase;letter-spacing:1.5px;margin-top:4px;">Sleep Perf</div>
          </td>
          <td style="vertical-align:top;">
            <div style="font-size:22px;color:{C["text"]};margin-bottom:6px;">{rec_info["status"]}</div>
            <div style="font-size:14px;color:{C["text2"]};line-height:1.6;margin-bottom:12px;">{rec_info["advice"]}</div>
            <div style="display:inline-block;padding:6px 14px;border-radius:16px;background:{rec_bg};font-size:13px;color:{rec_color};">Strain Target: {rec_info["strain_low"]:.1f} - {rec_info["strain_high"]:.1f}</div>
          </td>
        </tr></table>
        {card_end()}
        <tr><td style="padding:6px 0;">
          <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td width="33%" style="padding-right:4px;">
              <table width="100%" cellpadding="0" cellspacing="0" style="background-color:{C["surface"]};border:1px solid {C["border"]};border-radius:10px;"><tr><td style="padding:16px;text-align:center;">
                <div style="font-size:24px;color:{C["text"]};">{whoop.get("sleep_duration", "-")}</div>
                <div style="font-size:10px;color:{C["muted"]};text-transform:uppercase;letter-spacing:1.5px;margin-top:4px;">Sleep</div>
              </td></tr></table>
            </td>
            <td width="33%" style="padding:0 2px;">
              <table width="100%" cellpadding="0" cellspacing="0" style="background-color:{C["surface"]};border:1px solid {C["border"]};border-radius:10px;"><tr><td style="padding:16px;text-align:center;">
                <div style="font-size:24px;color:{C["text"]};">{whoop.get("hrv", "-")}</div>
                <div style="font-size:10px;color:{C["muted"]};text-transform:uppercase;letter-spacing:1.5px;margin-top:4px;">HRV (ms)</div>
              </td></tr></table>
            </td>
            <td width="33%" style="padding-left:4px;">
              <table width="100%" cellpadding="0" cellspacing="0" style="background-color:{C["surface"]};border:1px solid {C["border"]};border-radius:10px;"><tr><td style="padding:16px;text-align:center;">
                <div style="font-size:24px;color:{C["text"]};">{whoop.get("rhr", "-")}</div>
                <div style="font-size:10px;color:{C["muted"]};text-transform:uppercase;letter-spacing:1.5px;margin-top:4px;">RHR (bpm)</div>
              </td></tr></table>
            </td>
          </tr></table>
        </td></tr>
        {card_start()}
          <div style="font-size:13px;color:{C["text2"]};margin-bottom:10px;">Sleep Stages</div>
          <table width="100%" cellpadding="0" cellspacing="0" style="border-radius:7px;overflow:hidden;"><tr>
            <td width="{whoop.get('stage_awake', 8)}%" style="height:12px;background:{C["amber"]};"></td>
            <td width="{whoop.get('stage_light', 42)}%" style="height:12px;background:{C["blue"]};"></td>
            <td width="{whoop.get('stage_deep', 22)}%" style="height:12px;background:{C["purple"]};"></td>
            <td width="{whoop.get('stage_rem', 28)}%" style="height:12px;background:{C["green"]};"></td>
          </tr></table>
          <div style="margin-top:10px;font-size:12px;color:{C["text2"]};">
            <span style="color:{C["amber"]};">&#9679;</span> Awake {whoop.get('stage_awake', 8)}%&nbsp;&nbsp;
            <span style="color:{C["blue"]};">&#9679;</span> Light {whoop.get('stage_light', 42)}%&nbsp;&nbsp;
            <span style="color:{C["purple"]};">&#9679;</span> Deep {whoop.get('stage_deep', 22)}%&nbsp;&nbsp;
            <span style="color:{C["green"]};">&#9679;</span> REM {whoop.get('stage_rem', 28)}%
          </div>
        {card_end()}'''
    else:
        whoop_section = f'''
        {section_label("SLEEP &amp; RECOVERY")}
        {card_start()}
          <div style="font-size:14px;color:{C["amber"]};padding:8px 0;">WHOOP data unavailable. Check your API connection in .env</div>
        {card_end()}'''

    # ── Weather section ──
    weather_section = ""
    if weather:
        weather_section = f'''
        {section_label("WEATHER - " + city.upper())}
        {card_start()}
          <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td style="vertical-align:top;">
              <div style="font-size:56px;color:{C["text"]};line-height:1;">{weather["temp"]}<span style="font-size:24px;color:{C["muted"]};">&deg;F</span></div>
            </td>
            <td style="vertical-align:top;padding-left:20px;">
              <div style="font-size:18px;color:{C["text"]};margin-bottom:4px;">{weather["condition"]}</div>
              <div style="font-size:14px;color:{C["text2"]};">Feels like {weather["feels_like"]}&deg;F &middot; High {weather["high"]}&deg; / Low {weather["low"]}&deg;</div>
            </td>
          </tr></table>
          <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;"><tr>
            <td width="25%" style="text-align:center;padding:10px 0;background:{C["raised"]};border-radius:8px;">
              <div style="font-size:15px;color:{C["text"]};">{weather["humidity"]}%</div>
              <div style="font-size:10px;color:{C["muted"]};text-transform:uppercase;letter-spacing:1px;">Humidity</div>
            </td>
            <td width="4"></td>
            <td width="25%" style="text-align:center;padding:10px 0;background:{C["raised"]};border-radius:8px;">
              <div style="font-size:15px;color:{C["text"]};">{weather["wind"]} mph</div>
              <div style="font-size:10px;color:{C["muted"]};text-transform:uppercase;letter-spacing:1px;">Wind</div>
            </td>
            <td width="4"></td>
            <td width="25%" style="text-align:center;padding:10px 0;background:{C["raised"]};border-radius:8px;">
              <div style="font-size:15px;color:{C["text"]};">{weather["uvi"]}</div>
              <div style="font-size:10px;color:{C["muted"]};text-transform:uppercase;letter-spacing:1px;">UV Index</div>
            </td>
            <td width="4"></td>
            <td width="25%" style="text-align:center;padding:10px 0;background:{C["raised"]};border-radius:8px;">
              <div style="font-size:15px;color:{C["text"]};">{weather["rain_chance"]}%</div>
              <div style="font-size:10px;color:{C["muted"]};text-transform:uppercase;letter-spacing:1px;">Rain</div>
            </td>
          </tr></table>
          <div style="margin-top:14px;padding:12px 16px;background:{C["blue_dim"]};border-left:3px solid {C["blue"]};border-radius:8px;font-size:13px;color:{C["blue"]};line-height:1.5;">{weather["advisory"]}</div>
        {card_end()}'''

    # ── News section ──
    news_section = ""
    if news:
        news_section = section_label("TOP BUSINESS STORIES")
        for article in news:
            source_color = C["green"] if article.get("is_vc") else C["amber"]
            news_section += f'''
            {card_start()}
              <div style="font-size:11px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:{source_color};margin-bottom:6px;">{article["source"]}</div>
              <div style="font-size:18px;color:{C["text"]};line-height:1.35;margin-bottom:6px;">{article["headline"]}</div>
              <div style="font-size:14px;color:{C["text2"]};line-height:1.6;">{article["summary"] or ""}</div>
            {card_end()}'''

    # ── Markets section ──
    markets_section = ""
    if markets:
        markets_section = section_label("MARKET SNAPSHOT")
        # Build rows of 3
        for i in range(0, len(markets), 3):
            row = markets[i:i+3]
            markets_section += '<tr><td style="padding:4px 0;"><table width="100%" cellpadding="0" cellspacing="0"><tr>'
            for j, m in enumerate(row):
                change_color = C["green"] if m["direction"] == "up" else C["red"]
                pad = 'padding-right:4px;' if j == 0 else ('padding:0 2px;' if j == 1 else 'padding-left:4px;')
                markets_section += f'''
                <td width="33%" style="{pad}">
                  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:{C["surface"]};border:1px solid {C["border"]};border-radius:10px;"><tr><td style="padding:16px;">
                    <div style="font-size:11px;color:{C["muted"]};text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">{m["name"]}</div>
                    <div style="font-size:20px;color:{C["text"]};margin-bottom:2px;">{m["price"]}</div>
                    <div style="font-size:13px;color:{change_color};">{m["change_display"]}</div>
                  </td></tr></table>
                </td>'''
            markets_section += '</tr></table></td></tr>'

    # ── AI Pulse section ──
    ai_section = ""
    if ai_pulse:
        ai_section = f'''
        {section_label("AI / TECH PULSE")}
        {card_start()}
          <div style="font-size:11px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:{C["purple"]};margin-bottom:6px;">{ai_pulse["source"]}</div>
          <div style="font-size:18px;color:{C["text"]};line-height:1.35;margin-bottom:6px;">{ai_pulse["headline"]}</div>
          <div style="font-size:14px;color:{C["text2"]};line-height:1.6;">{ai_pulse["summary"] or ""}</div>
        {card_end()}'''

    # ── Sun times section ──
    sun_section = ""
    if sun:
        sun_section = f'''
        {section_label("DAYLIGHT")}
        {card_start()}
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="text-align:center;">
            <div style="font-size:22px;color:{C["text"]};">{sun["sunrise"]}</div>
            <div style="font-size:10px;color:{C["muted"]};text-transform:uppercase;letter-spacing:1.5px;">Sunrise</div>
          </td>
          <td width="1" style="background:{C["border"]};"></td>
          <td style="text-align:center;">
            <div style="font-size:16px;color:{C["amber"]};">{sun["golden_hour"]}</div>
            <div style="font-size:10px;color:{C["muted"]};text-transform:uppercase;letter-spacing:1.5px;">Golden Hour</div>
          </td>
          <td width="1" style="background:{C["border"]};"></td>
          <td style="text-align:center;">
            <div style="font-size:22px;color:{C["text"]};">{sun["sunset"]}</div>
            <div style="font-size:10px;color:{C["muted"]};text-transform:uppercase;letter-spacing:1.5px;">Sunset</div>
          </td>
        </tr></table>
        {card_end()}'''

    # ── Quote section ──
    quote_section = f'''
    {section_label("DAILY QUOTE")}
    {card_start()}
      <div style="text-align:center;">
        <div style="font-size:48px;color:{C["green"]};opacity:0.3;line-height:0.5;margin-bottom:12px;">&ldquo;</div>
        <div style="font-size:20px;font-style:italic;color:{C["text"]};line-height:1.55;margin-bottom:14px;">{quote["text"]}</div>
        <div style="font-size:13px;color:{C["muted"]};">&mdash; {quote["author"]}</div>
      </div>
    {card_end()}'''

    gen_time = fmt_date(now, "%-I:%M %p")

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Briefing - {date_str}</title>
<!--[if mso]><style>table,td {{font-family:Arial,sans-serif !important;}}</style><![endif]-->
</head>
<body style="margin:0;padding:0;background-color:{C["bg"]};font-family:Georgia,'Times New Roman',serif;">
<table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background-color:{C["bg"]};">
<tr><td align="center" style="padding:40px 16px 60px;">
<table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="max-width:600px;">

  <!-- Header -->
  <tr><td style="text-align:center;padding-bottom:40px;">
    <div style="font-size:11px;font-weight:bold;letter-spacing:2.5px;text-transform:uppercase;color:{C["green"]};margin-bottom:14px;font-family:Arial,sans-serif;">{date_str}</div>
    <div style="font-size:44px;color:{C["text"]};line-height:1.1;margin-bottom:10px;">{greeting}</div>
    <div style="font-size:15px;color:{C["text2"]};font-family:Arial,sans-serif;">Your daily briefing &mdash; {city}</div>
  </td></tr>

  {divider()}
  {whoop_section}
  {divider()}
  {weather_section}
  {divider()}
  {news_section}
  {divider()}
  {markets_section}
  {divider()}
  {ai_section}
  {sun_section}
  {divider()}
  {quote_section}

  <!-- Footer -->
  <tr><td style="text-align:center;padding-top:24px;border-top:1px solid {C["border"]};">
    <div style="font-size:12px;color:{C["muted"]};line-height:1.7;">
      Generated at {gen_time} &middot; Data from WHOOP, OpenWeather, NewsAPI &amp; more
    </div>
  </td></tr>

</table>
</td></tr></table>
</body>
</html>'''

    return html


def get_css():
    """No longer needed — all styles are inline for email compatibility."""
    return ""


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
        Path(args.output).write_text(html, encoding="utf-8")
        print(f"Saved to {args.output}")

    if args.preview:
        # Open in browser
        tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
        tmp.write(html)
        tmp.close()
        webbrowser.open(f"file://{tmp.name}")
        print(f"Opened in browser: {tmp.name}")
    else:
        # Send email
        send_email(html, date_str)

        # Also save a copy locally
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(exist_ok=True)
        filename = f"briefing-{datetime.now().strftime('%Y-%m-%d')}.html"
        (output_dir / filename).write_text(html, encoding="utf-8")
        print(f"Saved copy to output/{filename}")


if __name__ == "__main__":
    main()
