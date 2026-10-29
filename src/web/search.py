import re
from pathlib import Path
from urllib.parse import urlparse

import requests

from http_utils.http_client import HttpClient
from utils.utils import read_cookies

_OG_IMAGE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)',
    re.I,
)
_OG_IMAGE_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
    re.I,
)
_MAX_AVATAR_BYTES = 512_000


def _avatar_image_ok(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return False
    return (
        host == "tiktok.com"
        or host.endswith(".tiktok.com")
        or host == "tiktokcdn.com"
        or host.endswith(".tiktokcdn.com")
    )


def avatar_file(avatars_dir, username):
    root = Path(avatars_dir).resolve()
    path = (root / username).resolve()
    if not path.is_relative_to(root):
        return None
    return path if path.is_file() and path.stat().st_size > 0 else None


def cache_profile_avatar(username, avatars_dir, image_url=None):
    """Download a profile image once. Stale files stay until deleted."""
    root = Path(avatars_dir).resolve()
    dest = (root / username).resolve()
    if not dest.is_relative_to(root):
        return None
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    client = HttpClient(cookies=read_cookies()).req
    url = image_url
    if not url:
        page = client.get(f"https://www.tiktok.com/@{username}", timeout=10)
        page.raise_for_status()
        match = _OG_IMAGE.search(page.text) or _OG_IMAGE_REV.search(page.text)
        if not match:
            return None
        url = match.group(1)
    if not _avatar_image_ok(url):
        return None
    # Cookie-less: session_ss is a name/value dict and would otherwise go to any host.
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
    body = response.content
    if (
        not content_type.startswith("image/")
        or not body
        or len(body) > _MAX_AVATAR_BYTES
    ):
        return None
    dest.write_bytes(body)
    return dest


def search_profiles(query):
    """Best-effort public profile search using TikTok's ordinary web API."""
    client = HttpClient(cookies=read_cookies()).req
    response = client.get(
        "https://www.tiktok.com/api/search/user/full/",
        params={
            "aid": "1988",
            "app_name": "tiktok_web",
            "device_platform": "web_pc",
            "keyword": query,
            "count": 10,
            "cursor": 0,
            "from_page": "search",
        },
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("status_code", 0) not in (0, "0"):
        raise ValueError("TikTok profile search returned an unexpected response")

    items = data.get("user_list") or (data.get("data") or {}).get("user_list") or []
    results = []
    for item in items:
        info = item.get("user_info") or item.get("user") or item
        username = info.get("unique_id") or info.get("uniqueId")
        if not username:
            continue
        avatar = info.get("avatar_larger") or info.get("avatar_medium") or {}
        avatar_urls = avatar.get("url_list", []) if isinstance(avatar, dict) else []
        live_info = info.get("live_info")
        results.append(
            {
                "username": username,
                "display_name": info.get("nickname") or username,
                "avatar_url": avatar_urls[0] if avatar_urls else None,
                "is_live": bool(
                    info.get("is_live")
                    or (isinstance(live_info, dict) and live_info.get("is_live"))
                ),
            }
        )
    return results
