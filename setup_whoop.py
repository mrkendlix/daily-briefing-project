#!/usr/bin/env python3
"""
WHOOP OAuth Setup Helper
=========================
This script walks you through the WHOOP OAuth flow to get your
initial refresh token. You only need to run this once.

After running, it will save your tokens and update your .env file.
"""

import os
import json
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("WHOOP_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET", "")
REDIRECT_URI = "http://localhost:8080/callback"
TOKEN_FILE = Path(__file__).parent / ".whoop_tokens.json"

# The scopes we need for the daily briefing
SCOPES = "read:recovery read:sleep read:cycles read:workout read:profile offline"

AUTH_CODE = None


class CallbackHandler(BaseHTTPRequestHandler):
    """Handle the OAuth callback."""

    def do_GET(self):
        global AUTH_CODE
        query = parse_qs(urlparse(self.path).query)

        if "code" in query:
            AUTH_CODE = query["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
            <html><body style="font-family: -apple-system, sans-serif; display: flex;
            align-items: center; justify-content: center; height: 100vh; margin: 0;
            background: #0C0E12; color: #44D7A8;">
            <div style="text-align: center;">
            <h1>&#10003; WHOOP Connected!</h1>
            <p style="color: #8A90A0;">You can close this window and return to the terminal.</p>
            </div></body></html>
            """)
        else:
            error = query.get("error", ["Unknown error"])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<html><body><h1>Error: {error}</h1></body></html>".encode())

    def log_message(self, format, *args):
        pass  # Suppress server logs


def main():
    print("=" * 55)
    print("  WHOOP OAuth Setup Helper")
    print("=" * 55)
    print()

    if not CLIENT_ID or not CLIENT_SECRET:
        print("❌ WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET must be set in .env")
        print()
        print("To get these:")
        print("  1. Go to https://developer.whoop.com")
        print("  2. Sign in with your WHOOP account")
        print("  3. Create a Team (any name)")
        print("  4. Create an App with these settings:")
        print(f"     - Redirect URI: {REDIRECT_URI}")
        print("     - Scopes: read:recovery, read:sleep, read:cycles,")
        print("               read:workout, read:profile, offline")
        print("  5. Copy Client ID and Client Secret into your .env file")
        print("  6. Run this script again")
        return

    # Build the authorization URL
    auth_url = (
        f"https://api.prod.whoop.com/oauth/oauth2/auth"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={SCOPES.replace(' ', '%20')}"
        f"&state=dailybriefing"
    )

    print("Opening your browser to authorize with WHOOP...")
    print(f"If it doesn't open, visit: {auth_url}")
    print()
    webbrowser.open(auth_url)

    # Start local server to catch the callback
    print("Waiting for authorization callback on localhost:8080...")
    server = HTTPServer(("localhost", 8080), CallbackHandler)
    server.handle_request()

    if not AUTH_CODE:
        print("❌ No authorization code received. Please try again.")
        return

    print(f"✅ Authorization code received!")
    print("Exchanging for access token...")

    # Exchange code for tokens
    try:
        resp = requests.post(
            "https://api.prod.whoop.com/oauth/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": AUTH_CODE,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        tokens = resp.json()

        access_token = tokens["access_token"]
        refresh_token = tokens.get("refresh_token", "")

        # Save tokens
        with open(TOKEN_FILE, "w") as f:
            json.dump({
                "access_token": access_token,
                "refresh_token": refresh_token,
            }, f, indent=2)

        print()
        print("=" * 55)
        print("  ✅ WHOOP SETUP COMPLETE!")
        print("=" * 55)
        print()
        print(f"Tokens saved to: {TOKEN_FILE}")
        print()
        print("Add this to your .env file:")
        print(f"  WHOOP_REFRESH_TOKEN={refresh_token}")
        print()
        print("You're all set! Run generate_briefing.py to test.")

        # Quick test — fetch profile
        test = requests.get(
            "https://api.prod.whoop.com/developer/v2/user/profile/basic",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if test.ok:
            profile = test.json()
            name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
            print(f"\n👋 Connected as: {name}")

    except Exception as e:
        print(f"❌ Token exchange failed: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"   Response: {e.response.text}")


if __name__ == "__main__":
    main()
