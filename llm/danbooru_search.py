import json
import time
from curl_cffi import requests

_DANBOORU_URL = "https://danbooru.donmai.us/posts.json"
_LAST_CALL = 0


def _make_auth_params(username: str, api_key: str) -> dict:
    if not api_key or not username:
        return {}
    return {"login": username, "api_key": api_key}


def danbooru_search(query: str, max_posts: int = 3,
                    api_key: str = "", username: str = "",
                    proxy_url: str = "") -> str:
    global _LAST_CALL
    elapsed = time.time() - _LAST_CALL
    if elapsed < 0.5:
        time.sleep(0.5 - elapsed)
    _LAST_CALL = time.time()

    query = query.strip().strip(",.，。")
    if not query:
        return ""

    parts = [p.strip() for p in query.replace("，", ",").split(",") if p.strip()]
    char_tag = parts[0].replace(" ", "_") if parts else query.replace(" ", "_")

    auth_params = _make_auth_params(username, api_key)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
        "Referer": "https://danbooru.donmai.us/",
        "Accept": "application/json",
    }
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    def _do_search(tags: str) -> list:
        global _LAST_CALL
        elapsed = time.time() - _LAST_CALL
        if elapsed < 0.5:
            time.sleep(0.5 - elapsed)
        _LAST_CALL = time.time()
        params = {"tags": tags, "limit": max_posts, **auth_params}
        try:
            resp = requests.get(_DANBOORU_URL, params=params, headers=headers,
                                proxies=proxies, timeout=30,
                                impersonate="chrome")
        except Exception:
            return None
        if resp.status_code == 410:
            return None
        if resp.status_code == 403:
            return None
        if not resp.ok:
            return None
        try:
            data = resp.json()
            return data if isinstance(data, list) else None
        except Exception:
            return None

    posts = _do_search(char_tag)
    print(f"[danbooru] step1 '{char_tag}': {len(posts) if isinstance(posts, list) else posts}")
    if not posts and len(parts) >= 2:
        series_tag = parts[1].replace(" ", "_")
        compound = f"{char_tag}_({series_tag})"
        posts = _do_search(compound)
        print(f"[danbooru] step2 '{compound}': {len(posts) if isinstance(posts, list) else posts}")
    if not posts and len(parts) >= 2:
        series_tag = parts[1].replace(" ", "_")
        compound = f"{char_tag}_({series_tag}) {series_tag}"
        posts = _do_search(compound)
        print(f"[danbooru] step3 '{compound}': {len(posts) if isinstance(posts, list) else posts}")

    if posts is None:
        return "[Danbooru: API 请求失败]"
    if not posts:
        return ""

    lines = []
    for i, post in enumerate(posts[:max_posts], 1):
        char_tags = post.get("tag_string_character", "").strip()
        copy_tags = post.get("tag_string_copyright", "").strip()
        all_tags = post.get("tag_string", "").strip()
        rating = post.get("rating", "?")

        parts = []
        if char_tags:
            parts.append(f"角色: {char_tags}")
        if copy_tags:
            parts.append(f"作品: {copy_tags}")
        parts.append(f"标签: {all_tags[:300]}")
        parts.append(f"分级: {rating}")
        lines.append(f"--- 图片{i} ---\n" + "\n".join(parts))

    return "\n\n".join(lines)
