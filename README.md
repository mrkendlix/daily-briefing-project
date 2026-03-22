# ☀️ Daily Briefing Generator

A personalized morning briefing delivered to your inbox every day. Pulls together your WHOOP recovery & sleep data, local weather, top business news, venture capital headlines, market data, AI/tech developments, sunrise/sunset times, and a daily quote — all rendered in a beautiful dark-themed HTML email.

## What's Inside

| Section | Source | API Key Needed? |
|---|---|---|
| Sleep & Recovery | WHOOP Developer API | Yes (free) |
| Weather | OpenWeatherMap One Call 3.0 | Yes (free tier) |
| Top Business News (3) | NewsAPI.org | Yes (free tier) |
| VC / Startup News (1) | NewsAPI.org | Same key |
| Market Snapshot (6) | Yahoo Finance via yfinance | No |
| AI / Tech Pulse (1) | NewsAPI.org | Same key |
| Sunrise / Sunset | sunrise-sunset.org | No |
| Daily Quote | ZenQuotes | No |

---

## Setup Guide (15 minutes)

### Step 1: Clone & Install

```bash
git clone <your-repo-url> daily-briefing
cd daily-briefing
pip install -r requirements.txt
cp .env.example .env
```

### Step 2: WHOOP Developer App

1. Go to [developer.whoop.com](https://developer.whoop.com) and sign in with your WHOOP account
2. Click **Dashboard** in the top navigation
3. Create a **Team** (any name — e.g., "Personal")
4. Click **Create App** and fill in:
   - **App Name**: Daily Briefing
   - **Description**: Personal morning briefing
   - **Redirect URI**: `http://localhost:8080/callback`
   - **Scopes**: Check all of these:
     - `read:recovery`
     - `read:sleep`
     - `read:cycles`
     - `read:workout`
     - `read:profile`
     - `offline` (critical — this enables token refresh)
5. Copy your **Client ID** and **Client Secret** into `.env`:
   ```
   WHOOP_CLIENT_ID=your_id_here
   WHOOP_CLIENT_SECRET=your_secret_here
   ```
6. Run the setup helper:
   ```bash
   python setup_whoop.py
   ```
   This will open your browser, ask you to authorize, and save your tokens automatically.

### Step 3: OpenWeatherMap API Key

1. Go to [openweathermap.org](https://openweathermap.org/api)
2. Sign up for a free account
3. Subscribe to the **One Call API 3.0** (free for 1,000 calls/day)
4. Copy your API key into `.env`:
   ```
   OPENWEATHER_API_KEY=your_key_here
   ```

### Step 4: NewsAPI Key

1. Go to [newsapi.org/register](https://newsapi.org/register)
2. Sign up for the free Developer plan
3. Copy your API key into `.env`:
   ```
   NEWSAPI_KEY=your_key_here
   ```

### Step 5: Email Setup (Gmail)

1. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
2. Generate an App Password for "Mail"
3. Add to `.env`:
   ```
   EMAIL_SENDER=your.email@gmail.com
   EMAIL_PASSWORD=your_16_char_app_password
   EMAIL_RECIPIENT=your.email@gmail.com
   ```

> **Note**: If you use a non-Gmail provider, update `SMTP_SERVER` and `SMTP_PORT` in `.env`.

### Step 6: Test It

```bash
# Preview in browser (no email sent)
python generate_briefing.py --preview

# Send yourself a real email
python generate_briefing.py

# Override location (e.g., if traveling)
python generate_briefing.py --preview --location 37.7749 -122.4194 "San Francisco"
```

---

## Automate It (GitHub Actions)

The easiest way to run this daily is GitHub Actions — it's free for public repos (2,000 min/month for private).

1. Push this project to a GitHub repo
2. Go to **Settings → Secrets and variables → Actions**
3. Add these **Repository Secrets**:
   - `WHOOP_CLIENT_ID`
   - `WHOOP_CLIENT_SECRET`
   - `WHOOP_REFRESH_TOKEN`
   - `OPENWEATHER_API_KEY`
   - `NEWSAPI_KEY`
   - `EMAIL_SENDER`
   - `EMAIL_PASSWORD`
   - `EMAIL_RECIPIENT`
4. Optionally add **Repository Variables** for location:
   - `DEFAULT_LAT` (e.g., `40.7128`)
   - `DEFAULT_LON` (e.g., `-74.0060`)
   - `DEFAULT_CITY` (e.g., `New York City`)
5. The workflow runs at **6:30 AM ET** daily. Adjust the cron in `.github/workflows/daily-briefing.yml`
6. Test by going to **Actions → Daily Briefing → Run workflow**

---

## iPhone Location Integration

To make the weather dynamic based on where you actually are:

### Option A: Apple Shortcuts (Recommended)
Create a Shortcut that runs at 6:00 AM:
1. **Get Current Location**
2. **Get Details of Location** → Latitude, Longitude, City
3. **Run SSH Script** or **Make Web Request** to trigger the briefing with your coordinates

### Option B: Static Location
Just set `DEFAULT_LAT`, `DEFAULT_LON`, and `DEFAULT_CITY` in `.env` — this is the simplest approach and works great if you're usually in the same city.

---

## Project Structure

```
daily-briefing/
├── generate_briefing.py      # Main script — fetches data, builds HTML, sends email
├── setup_whoop.py             # One-time WHOOP OAuth setup helper
├── requirements.txt           # Python dependencies
├── .env.example               # Environment variable template
├── .env                       # Your actual config (git-ignored)
├── .whoop_tokens.json         # Saved WHOOP tokens (git-ignored)
├── .github/
│   └── workflows/
│       └── daily-briefing.yml # GitHub Actions schedule
└── output/                    # Local copies of generated briefings
```

---

## Troubleshooting

**WHOOP data shows as unavailable**
- Run `python setup_whoop.py` again to re-authorize
- Make sure the `offline` scope is enabled on your WHOOP app
- Check that `.whoop_tokens.json` exists and contains a refresh token

**Weather not loading**
- Verify your OpenWeatherMap API key is for One Call 3.0 (not 2.5)
- New API keys can take up to 2 hours to activate

**News missing**
- NewsAPI free tier only works from localhost/server, not from a browser
- Free tier has a ~1 hour delay on articles

**Email not sending**
- Gmail requires an App Password, not your regular password
- Make sure 2-factor auth is enabled on your Google account first
