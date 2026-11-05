import os
import sys
import time
import requests

try:
    from tools_settings import get_setting
except Exception:
    def get_setting(key, default=None):
        return os.getenv(key, default)


# ---------------------------------------------------------------------------
# Bluesky (AT Protocol) — simplest of the five: app-password auth, direct
# REST calls, no OAuth dance. Create an app password at
# https://bsky.app/settings/app-passwords (never use your real password).
# ---------------------------------------------------------------------------
def post_bluesky(text: str) -> str:
    identifier = get_setting("BLUESKY_IDENTIFIER")  # handle or email
    app_password = get_setting("BLUESKY_APP_PASSWORD")
    if not identifier or not app_password:
        raise RuntimeError(
            "Missing BLUESKY_IDENTIFIER / BLUESKY_APP_PASSWORD in .env. Create an app password at "
            "https://bsky.app/settings/app-passwords (do not use your real account password)."
        )

    session_res = requests.post(
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        json={"identifier": identifier, "password": app_password},
        timeout=15,
    )
    if session_res.status_code >= 300:
        raise RuntimeError(f"Bluesky login failed ({session_res.status_code}): {session_res.text[:300]}")
    session = session_res.json()
    access_jwt = session["accessJwt"]
    did = session["did"]

    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
    }
    post_res = requests.post(
        "https://bsky.social/xrpc/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {access_jwt}"},
        json={"repo": did, "collection": "app.bsky.feed.post", "record": record},
        timeout=15,
    )
    if post_res.status_code >= 300:
        raise RuntimeError(f"Bluesky post failed ({post_res.status_code}): {post_res.text[:300]}")
    uri = post_res.json().get("uri", "")
    return f"Posted to Bluesky. URI: {uri}"


# ---------------------------------------------------------------------------
# X / Twitter — OAuth1.0a user-context credentials from a Twitter Developer
# app with read+write permission (developer.twitter.com).
# ---------------------------------------------------------------------------
def post_x(text: str) -> str:
    try:
        from requests_oauthlib import OAuth1
    except ImportError as e:
        raise ImportError(
            f"'requests-oauthlib' is not installed for THIS interpreter ({sys.executable}): {e}\n"
            f"Install with:\n    {sys.executable} -m pip install requests-oauthlib"
        )

    api_key = get_setting("X_API_KEY")
    api_secret = get_setting("X_API_SECRET")
    access_token = get_setting("X_ACCESS_TOKEN")
    access_secret = get_setting("X_ACCESS_TOKEN_SECRET")
    if not all([api_key, api_secret, access_token, access_secret]):
        raise RuntimeError(
            "Missing X credentials. Set X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET "
            "in .env — from a Twitter Developer app with OAuth1.0a user-context + read/write permission."
        )

    auth = OAuth1(api_key, api_secret, access_token, access_secret)
    res = requests.post("https://api.twitter.com/2/tweets", auth=auth, json={"text": text}, timeout=15)
    if res.status_code >= 300:
        raise RuntimeError(f"X API error ({res.status_code}): {res.text[:300]}")
    data = res.json()
    tweet_id = data.get("data", {}).get("id")
    return f"Posted to X. Tweet ID: {tweet_id}"


# ---------------------------------------------------------------------------
# YouTube — Google OAuth2 with a local-server consent flow (standard,
# well-supported pattern for desktop apps — no public redirect URI needed).
# Requires a Google Cloud project with the YouTube Data API v3 enabled and
# an OAuth 'Desktop app' client JSON downloaded from Credentials.
# ---------------------------------------------------------------------------
def post_youtube_video(file_path: str, title: str, description: str = "", privacy: str = "private") -> str:
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as e:
        raise ImportError(
            f"YouTube posting packages are not installed for THIS interpreter ({sys.executable}): {e}\n"
            f"Install with:\n    {sys.executable} -m pip install google-api-python-client google-auth-oauthlib"
        )

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Video file not found: {file_path}")

    client_secrets_file = get_setting("YOUTUBE_CLIENT_SECRETS_FILE")
    if not client_secrets_file or not os.path.exists(client_secrets_file):
        raise RuntimeError(
            "Missing YOUTUBE_CLIENT_SECRETS_FILE in .env — path to an OAuth 'Desktop app' client JSON "
            "downloaded from Google Cloud Console (APIs & Services > Credentials)."
        )

    token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "youtube_token.json")
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, scopes)
        creds = flow.run_local_server(port=0)  # opens your browser once for consent
        with open(token_file, "w") as f:
            f.write(creds.to_json())

    youtube = build("youtube", "v3", credentials=creds)
    body = {"snippet": {"title": title, "description": description}, "status": {"privacyStatus": privacy}}
    media = MediaFileUpload(file_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    video_id = response.get("id")
    return f"Uploaded to YouTube ({privacy}). https://youtu.be/{video_id}"


# ---------------------------------------------------------------------------
# Instagram — Graph API. Requires a Facebook Developer app with Instagram
# Graph API access, linked to an Instagram Business/Creator account, and a
# long-lived access token. Image must already be hosted at a public URL —
# the Graph API does not accept direct file uploads.
# ---------------------------------------------------------------------------
def post_instagram_image(image_url: str, caption: str = "") -> str:
    access_token = get_setting("INSTAGRAM_ACCESS_TOKEN")
    ig_user_id = get_setting("INSTAGRAM_USER_ID")
    if not access_token or not ig_user_id:
        raise RuntimeError(
            "Missing INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_USER_ID in .env — requires a Facebook Developer "
            "app with Instagram Graph API access and a long-lived token for a linked Business/Creator account."
        )
    if not image_url:
        raise RuntimeError("Instagram posting needs a publicly hosted image URL (direct file upload isn't supported by the Graph API).")

    container_res = requests.post(
        f"https://graph.facebook.com/v19.0/{ig_user_id}/media",
        data={"image_url": image_url, "caption": caption, "access_token": access_token},
        timeout=20,
    )
    if container_res.status_code >= 300:
        raise RuntimeError(f"Instagram container error ({container_res.status_code}): {container_res.text[:300]}")
    creation_id = container_res.json().get("id")

    publish_res = requests.post(
        f"https://graph.facebook.com/v19.0/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=20,
    )
    if publish_res.status_code >= 300:
        raise RuntimeError(f"Instagram publish error ({publish_res.status_code}): {publish_res.text[:300]}")
    media_id = publish_res.json().get("id")
    return f"Posted to Instagram. Media ID: {media_id}"


# ---------------------------------------------------------------------------
# TikTok — Content Posting API. IMPORTANT: the OAuth2 user-consent flow
# itself is NOT implemented here. It requires an approved TikTok developer
# app and a registered redirect URI, which needs TikTok's own app-review
# process — not something achievable from a local script. This function
# only performs the actual upload/post call, assuming you already have a
# valid access token from TikTok's developer console (developers.tiktok.com).
# ---------------------------------------------------------------------------
def post_tiktok_video(file_path: str, caption: str = "") -> str:
    access_token = get_setting("TIKTOK_ACCESS_TOKEN")
    if not access_token:
        raise RuntimeError(
            "Missing TIKTOK_ACCESS_TOKEN in .env. TikTok requires an approved developer app and a user "
            "OAuth token obtained via TikTok's own authorization flow (developers.tiktok.com) — that "
            "consent flow isn't something this agent can complete for you. Generate a token there and "
            "set it here; this function only performs the upload once you have one."
        )
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Video file not found: {file_path}")

    file_size = os.path.getsize(file_path)
    init_res = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={
            "post_info": {"title": caption, "privacy_level": "SELF_ONLY"},
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": file_size,
                "total_chunk_count": 1,
            },
        },
        timeout=20,
    )
    if init_res.status_code >= 300:
        raise RuntimeError(f"TikTok init error ({init_res.status_code}): {init_res.text[:300]}")
    data = init_res.json().get("data", {})
    upload_url = data.get("upload_url")
    publish_id = data.get("publish_id")
    if not upload_url:
        raise RuntimeError(f"TikTok did not return an upload URL: {init_res.text[:300]}")

    with open(file_path, "rb") as f:
        video_bytes = f.read()
    upload_res = requests.put(
        upload_url,
        headers={"Content-Type": "video/mp4",
                 "Content-Range": f"bytes 0-{len(video_bytes) - 1}/{len(video_bytes)}"},
        data=video_bytes,
        timeout=120,
    )
    if upload_res.status_code >= 300:
        raise RuntimeError(f"TikTok upload error ({upload_res.status_code}): {upload_res.text[:300]}")

    return f"Video uploaded to TikTok (publish_id: {publish_id}). It may take a moment to appear/process."
