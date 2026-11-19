#!/usr/bin/env python3
"""One-time helper: mint YOUTUBE_REFRESH_TOKEN (and discover YOUTUBE_CHANNEL_ID).

Google retired the copy-paste "oob" redirect in 2022, so the consent code now has
to come back to a redirect URI. This script uses ``http://localhost:8080/``
*without* running a listener: the browser lands on a dead page, but the code sits
in the address bar and you paste the whole URL back here. That keeps the flow
working when the browser is on your laptop and Django is on a headless server.

Standard library only — no pip install needed. Run it from the project root:

    python3 deploy/mint_youtube_token.py

Reads YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET from .env, and offers to write
YOUTUBE_REFRESH_TOKEN and YOUTUBE_CHANNEL_ID back into it.

``SCOPES`` asks for one scope, ``youtube.force-ssl``, which covers everything the
platform does on the channel: upload recordings, read unlisted videos, and create
and fill playlists. It must match ``SCOPES`` in ``apps/livesessions/youtube.py`` —
a refresh asking for a scope the token was not granted fails with
``invalid_scope``.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
REDIRECT_URI = "http://localhost:8080/"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def read_env():
    """Return the ``KEY=value`` pairs in .env as a dict, ignoring comments; exit if .env is missing."""
    values = {}
    if not os.path.exists(ENV_PATH):
        sys.exit(f"No .env found at {ENV_PATH}")
    with open(ENV_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def write_env_value(key, value):
    """Replace ``key``'s line in .env, preserving everything else verbatim."""
    with open(ENV_PATH, encoding="utf-8") as fh:
        lines = fh.readlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            break
    else:
        lines.append(f"{key}={value}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def post_form(url, fields):
    """POST *fields* form-encoded to *url* and return the JSON reply; exit with Google's error on HTTP failure."""
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        sys.exit(f"\nGoogle rejected the request ({exc.code}):\n{detail}\n")


def get_json(url, token):
    """GET *url* with a bearer *token* and return the JSON reply, or ``None`` (after printing why) on HTTP failure."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"  (channel lookup failed: {exc.code} {exc.read().decode(errors='replace')[:200]})")
        return None


def main():
    """Walk the operator through consent, exchange the code and optionally write the results to .env.

    The consent URL sends ``access_type=offline`` (what makes Google return a
    refresh token at all) and ``prompt=consent`` (forces one even if the account
    has consented before).
    """
    env = read_env()
    client_id = env.get("YOUTUBE_CLIENT_ID", "")
    client_secret = env.get("YOUTUBE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        sys.exit("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env first.")

    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    })

    print("=" * 78)
    print("STEP 1 — open this URL in the browser signed in as the CHANNEL OWNER")
    print("=" * 78)
    print()
    print(auth_url)
    print()
    print("Approve the YouTube permission. If you see 'Google hasn't verified")
    print("this app', click Advanced -> Go to <app> (unsafe) — that warning is")
    print("expected for your own unpublished project.")
    print()
    print("=" * 78)
    print("STEP 2 — the browser will fail to load localhost:8080. That is correct.")
    print("=" * 78)
    print("Copy the FULL URL out of the address bar and paste it below.")
    print()

    pasted = input("Redirected URL (or just the code): ").strip()
    if not pasted:
        sys.exit("Nothing pasted.")

    if pasted.startswith("http"):
        query = urllib.parse.urlparse(pasted).query
        code = urllib.parse.parse_qs(query).get("code", [""])[0]
    else:
        code = pasted
    if not code:
        sys.exit("Could not find a 'code' parameter in that URL.")

    print("\nExchanging the code for a refresh token...")
    tokens = post_form("https://oauth2.googleapis.com/token", {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    })

    refresh = tokens.get("refresh_token")
    access = tokens.get("access_token")
    if not refresh:
        sys.exit("No refresh_token returned. Re-run and make sure you approved the "
                 "consent screen fresh (the URL already sends prompt=consent).")

    print("\nGranted scopes:", tokens.get("scope", "(none reported)"))
    print("Refresh token :", refresh[:12] + "..." + refresh[-6:])

    channel_id = ""
    if access:
        data = get_json(
            "https://www.googleapis.com/youtube/v3/channels?part=id,snippet&mine=true", access)
        items = (data or {}).get("items") or []
        if items:
            channel_id = items[0]["id"]
            print("Channel       :", items[0]["snippet"]["title"], f"({channel_id})")

    print()
    if input("Write these into .env now? [y/N] ").strip().lower() == "y":
        write_env_value("YOUTUBE_REFRESH_TOKEN", refresh)
        print("  wrote YOUTUBE_REFRESH_TOKEN")
        if channel_id:
            write_env_value("YOUTUBE_CHANNEL_ID", channel_id)
            print("  wrote YOUTUBE_CHANNEL_ID")
        print("\nDone. Restart the app to pick the new values up.")
    else:
        print("\nNothing written. Set these manually:")
        print(f"  YOUTUBE_REFRESH_TOKEN={refresh}")
        if channel_id:
            print(f"  YOUTUBE_CHANNEL_ID={channel_id}")


if __name__ == "__main__":
    main()
