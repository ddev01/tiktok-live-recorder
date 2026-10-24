from http_utils.http_client import HttpClient
from utils.utils import read_cookies


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
