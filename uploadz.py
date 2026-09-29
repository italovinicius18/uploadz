#!/usr/bin/env python3
"""Uploadz: publish videos to your own TikTok account through the official API.

Standard library only. Credentials come from the environment or from
~/.config/uploadz/credentials.env (TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET);
tokens are kept in ~/.config/uploadz/token.json (mode 600). Neither lives in
this repository.

    uploadz.py login                      # connect a TikTok account
    uploadz.py whoami                     # connected account + allowed privacy levels
    uploadz.py post clip.mp4 --privacy SELF_ONLY --caption "..."
    uploadz.py post clip.mp4 --inbox      # send to the TikTok inbox as a draft
    uploadz.py status <publish_id>
    uploadz.py videos                     # latest videos on the account
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://open.tiktokapis.com"
AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
REDIRECT_URI = os.environ.get(
    "UPLOADZ_REDIRECT_URI", "https://italovinicius18.github.io/uploadz/callback.html"
)
SCOPES = os.environ.get(
    "UPLOADZ_SCOPES",
    "user.info.basic,user.info.profile,user.info.stats,video.list,video.upload,video.publish",
)
CONFIG_DIR = Path(os.environ.get("UPLOADZ_HOME", Path.home() / ".config" / "uploadz"))
TOKEN_FILE = CONFIG_DIR / "token.json"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.env"

MB = 1024 * 1024
MIN_CHUNK = 5 * MB  # TikTok: chunks are 5-64 MB, the last one may reach 128 MB
MAX_SINGLE = 64 * MB
DEFAULT_CHUNK = 10 * MB
PRIVACY_LEVELS = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY")
MIME = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}


class TikTokError(RuntimeError):
    pass


# ---------------------------------------------------------------- config

def load_credentials() -> tuple[str, str]:
    env = dict(os.environ)
    if CREDENTIALS_FILE.exists():
        for line in CREDENTIALS_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip("'\""))
    key, secret = env.get("TIKTOK_CLIENT_KEY"), env.get("TIKTOK_CLIENT_SECRET")
    if not key or not secret:
        sys.exit(f"Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in {CREDENTIALS_FILE}")
    return key, secret


def save_token(tok: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    tok = dict(tok)
    tok["expires_at"] = now + int(tok.get("expires_in", 0))
    tok["refresh_expires_at"] = now + int(tok.get("refresh_expires_in", 0))
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f, indent=2)


def load_token() -> dict:
    if not TOKEN_FILE.exists():
        sys.exit("No TikTok account connected. Run: uploadz.py login")
    return json.loads(TOKEN_FILE.read_text())


# ---------------------------------------------------------------- http

def _request(method: str, url: str, *, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _json(body: bytes) -> dict:
    try:
        return json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise TikTokError(f"non-JSON response: {body[:300]!r}")


def oauth(form: dict) -> dict:
    status, body = _request(
        "POST", f"{API}/v2/oauth/token/",
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
    )
    out = _json(body)
    if status != 200 or "access_token" not in out:
        raise TikTokError(f"token request failed ({status}): {out.get('error')} "
                          f"{out.get('error_description', '')} log_id={out.get('log_id')}")
    return out


def access_token() -> str:
    tok = load_token()
    if tok.get("expires_at", 0) - 60 > time.time():
        return tok["access_token"]
    if tok.get("refresh_expires_at", 0) < time.time():
        sys.exit("TikTok session expired. Run: uploadz.py login")
    key, secret = load_credentials()
    tok = oauth({"client_key": key, "client_secret": secret,
                 "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    save_token(tok)
    return tok["access_token"]


def api(method: str, path: str, payload: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {access_token()}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=UTF-8"
        data = json.dumps(payload).encode()
    status, body = _request(method, f"{API}{path}", data=data, headers=headers)
    out = _json(body)
    err = out.get("error") or {}
    if status != 200 or err.get("code") not in (None, "ok"):
        raise TikTokError(f"{path} failed ({status}): {err.get('code')} {err.get('message', '')} "
                          f"log_id={err.get('log_id')}")
    return out.get("data") or {}


# ---------------------------------------------------------------- pure helpers

def chunk_plan(size: int, chunk: int = DEFAULT_CHUNK) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) as TikTok expects them.

    Up to 64 MB goes as one chunk. Above that the count is size // chunk and the
    last chunk absorbs the remainder, which is how the API computes it.
    """
    if size <= 0:
        raise ValueError("empty file")
    if size <= MAX_SINGLE:
        return size, 1
    chunk = max(MIN_CHUNK, min(chunk, MAX_SINGLE))
    return chunk, size // chunk


def chunk_ranges(size: int, chunk_size: int, count: int):
    for i in range(count):
        start = i * chunk_size
        end = size - 1 if i == count - 1 else start + chunk_size - 1
        yield start, end


def parse_pasted_code(text: str) -> tuple[str, str | None]:
    """Accept the callback page's "code state" line, a bare code or the full callback URL."""
    text = text.strip()
    if text.startswith("http"):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
        if "error" in q:
            raise TikTokError(f"authorization failed: {q['error'][0]} {q.get('error_description', [''])[0]}")
        return q["code"][0], (q.get("state") or [None])[0]
    parts = text.split()
    code = urllib.parse.unquote(parts[0])
    return code, (parts[1] if len(parts) > 1 else None)


def authorize_url(client_key: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_key": client_key, "scope": SCOPES, "response_type": "code",
        "redirect_uri": REDIRECT_URI, "state": state,
    })


# ---------------------------------------------------------------- commands

def cmd_login(_args) -> None:
    key, secret = load_credentials()
    state = secrets.token_urlsafe(16)
    url = authorize_url(key, state)
    print("Open this link, sign in with TikTok and authorize Uploadz:\n")
    print(url + "\n")
    print("The page you land on shows a code. Copy it and paste it here.")
    code, got_state = parse_pasted_code(input("code> "))
    if got_state and got_state != state:
        sys.exit("The code belongs to a different login attempt (state mismatch). Run login again.")
    tok = oauth({"client_key": key, "client_secret": secret, "code": code,
                 "grant_type": "authorization_code", "redirect_uri": REDIRECT_URI})
    save_token(tok)
    print(f"Connected. Scopes granted: {tok.get('scope')}")
    cmd_whoami(None)


def cmd_whoami(_args) -> None:
    user = api("GET", "/v2/user/info/?fields=open_id,display_name,username,avatar_url,"
                      "follower_count,video_count").get("user", {})
    info = api("POST", "/v2/post/publish/creator_info/query/", {})
    print(f"Account: {user.get('display_name')} (@{user.get('username') or info.get('creator_username')})")
    if "follower_count" in user:
        print(f"Followers: {user.get('follower_count')}  Videos: {user.get('video_count')}")
    print(f"Privacy levels allowed: {', '.join(info.get('privacy_level_options', []))}")
    print(f"Max video length: {info.get('max_video_post_duration_sec')} s")
    off = [n for n in ("comment", "duet", "stitch") if info.get(f"{n}_disabled")]
    if off:
        print(f"Disabled in the account settings: {', '.join(off)}")


def upload_file(upload_url: str, path: Path, chunk_size: int, count: int) -> None:
    size = path.stat().st_size
    mime = MIME.get(path.suffix.lower(), "video/mp4")
    with path.open("rb") as f:
        for i, (start, end) in enumerate(chunk_ranges(size, chunk_size, count), 1):
            f.seek(start)
            data = f.read(end - start + 1)
            status, body = _request("PUT", upload_url, data=data, headers={
                "Content-Type": mime, "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{size}",
            })
            if status not in (200, 201, 206):
                raise TikTokError(f"chunk {i}/{count} failed ({status}): {body[:300]!r}")
            print(f"  uploaded chunk {i}/{count} ({(end + 1) * 100 // size}%)")


def cmd_post(args) -> None:
    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"File not found: {path}")
    size = path.stat().st_size
    chunk_size, count = chunk_plan(size)
    source = {"source": "FILE_UPLOAD", "video_size": size,
              "chunk_size": chunk_size, "total_chunk_count": count}

    if args.inbox:
        data = api("POST", "/v2/post/publish/inbox/video/init/", {"source_info": source})
    else:
        if not args.privacy:
            sys.exit("Choose who can see the video with --privacy (run whoami for the allowed levels).")
        info = api("POST", "/v2/post/publish/creator_info/query/", {})
        allowed = info.get("privacy_level_options", [])
        if args.privacy not in allowed:
            sys.exit(f"--privacy {args.privacy} is not allowed for this account now. Allowed: {', '.join(allowed)}")
        caption = args.caption or ""
        if args.caption_file:
            caption = Path(args.caption_file).read_text().strip()
        post_info = {
            "title": caption,
            "privacy_level": args.privacy,
            "disable_comment": args.no_comment or bool(info.get("comment_disabled")),
            "disable_duet": args.no_duet or bool(info.get("duet_disabled")),
            "disable_stitch": args.no_stitch or bool(info.get("stitch_disabled")),
            "video_cover_timestamp_ms": args.cover_ms,
            "brand_content_toggle": args.branded,
            "brand_organic_toggle": args.your_brand,
            "is_aigc": args.ai_generated,
        }
        print(f"Posting to @{info.get('creator_username')} as {args.privacy}. "
              "By posting you agree to TikTok's Music Usage Confirmation.")
        data = api("POST", "/v2/post/publish/video/init/",
                   {"post_info": post_info, "source_info": source})

    publish_id = data["publish_id"]
    print(f"publish_id: {publish_id} ({size / MB:.1f} MB in {count} chunk(s))")
    upload_file(data["upload_url"], path, chunk_size, count)
    if args.no_wait:
        return
    wait_status(publish_id)


def wait_status(publish_id: str, timeout: int = 600) -> None:
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        st = api("POST", "/v2/post/publish/status/fetch/", {"publish_id": publish_id})
        status = st.get("status")
        if status != last:
            print(f"  status: {status}")
            last = status
        if status == "FAILED":
            raise TikTokError(f"TikTok rejected the post: {st.get('fail_reason')}")
        if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            ids = st.get("publicaly_available_post_id") or []
            if ids:
                print(f"  post id: {', '.join(map(str, ids))}")
            if status == "SEND_TO_USER_INBOX":
                print("  Open TikTok and finish the post from your inbox notification.")
            return
        time.sleep(5)
    print(f"Still processing. Check later with: uploadz.py status {publish_id}")


def cmd_status(args) -> None:
    print(json.dumps(api("POST", "/v2/post/publish/status/fetch/", {"publish_id": args.publish_id}), indent=2))


def cmd_videos(args) -> None:
    data = api("POST", "/v2/video/list/?fields=id,title,create_time,share_url,duration",
               {"max_count": args.count})
    for v in data.get("videos", []):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(v.get("create_time", 0)))
        print(f"{when}  {v.get('duration', '?')}s  {v.get('share_url')}  {(v.get('title') or '')[:60]}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="uploadz", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login").set_defaults(fn=cmd_login)
    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)
    s = sub.add_parser("status"); s.add_argument("publish_id"); s.set_defaults(fn=cmd_status)
    v = sub.add_parser("videos"); v.add_argument("--count", type=int, default=10); v.set_defaults(fn=cmd_videos)
    po = sub.add_parser("post")
    po.add_argument("file")
    po.add_argument("--privacy", choices=PRIVACY_LEVELS, help="no default, as TikTok requires")
    po.add_argument("--caption", help="caption with hashtags (up to 2200 characters)")
    po.add_argument("--caption-file", help="read the caption from a text file")
    po.add_argument("--inbox", action="store_true", help="upload as a draft to the TikTok inbox instead")
    po.add_argument("--no-comment", action="store_true")
    po.add_argument("--no-duet", action="store_true")
    po.add_argument("--no-stitch", action="store_true")
    po.add_argument("--cover-ms", type=int, default=1000, help="cover frame timestamp in ms")
    po.add_argument("--branded", action="store_true", help="paid partnership (branded content)")
    po.add_argument("--your-brand", action="store_true", help="promotes your own business")
    po.add_argument("--ai-generated", action="store_true", help="label as AI-generated content")
    po.add_argument("--no-wait", action="store_true", help="do not wait for TikTok to finish processing")
    po.set_defaults(fn=cmd_post)
    args = p.parse_args(argv)
    if getattr(args, "branded", False) and args.privacy == "SELF_ONLY":
        sys.exit("Branded content cannot be private (TikTok rule).")
    try:
        args.fn(args)
    except TikTokError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
