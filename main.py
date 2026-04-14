import os
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse
import requests
from google import genai
from google.genai import types

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]

print("Gemini API key set:", bool(GEMINI_API_KEY))
print("Discord webhook set:", bool(WEBHOOK))

client = genai.Client(api_key=GEMINI_API_KEY)

prompt = """
今日の重要ニュースを、以下の3カテゴリでそれぞれ3件ずつ教えてください。

カテゴリ:
- AI
- 経済
- 政治

必ず Google Search を使って最新情報を確認してください。

各ニュースについて、必ず以下の形式で書いてください。

[AI]
タイトル: ...
要約: ...
URL: https://...

[経済]
タイトル: ...
要約: ...
URL: https://...

[政治]
タイトル: ...
要約: ...
URL: https://...

制約:
- URLは必ず https:// から始まる実在のURLにしてください
- URLはトップページではなく、そのニュース本文のページを直接指すURLにしてください
- URLに `...` やプレースホルダ、推測しただけのURLを含めないでください
- URLが確認できないニュースは出力しないでください
- 要約は2〜3文で、少し詳しめに書いてください
- 日本語で簡潔に書いてください
- 同じ話題の重複は避けてください

禁止事項:
- ゴシップ（芸能ニュース、スキャンダル、噂話など）は絶対に含めない
- スポーツニュースは絶対に含めない
- エンタメ系ニュース（映画・音楽・ドラマなど）は含めない
- これらが候補に含まれる場合は除外し、別のニュースに置き換えてください
- これらの禁止事項に違反する出力は無効とします
"""

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def normalize_url(url: str) -> str | None:
    trimmed = url.strip().strip("<>()[]{}.,\"'")
    if " " in trimmed or "..." in trimmed:
        return None

    parsed = urlparse(trimmed)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return urlunparse(parsed._replace(fragment=""))


def verify_url(url: str) -> str | None:
    try:
        response = requests.head(
            url,
            allow_redirects=True,
            timeout=10,
            headers=REQUEST_HEADERS,
        )
        if response.status_code >= 400 or not response.url:
            raise requests.HTTPError(f"HEAD status={response.status_code}")
    except Exception:
        try:
            response = requests.get(
                url,
                allow_redirects=True,
                timeout=10,
                headers=REQUEST_HEADERS,
                stream=True,
            )
            if response.status_code >= 400 or not response.url:
                return None
        except Exception:
            return None

    content_type = response.headers.get("Content-Type", "").lower()
    if content_type and "text/html" not in content_type and "application/pdf" not in content_type:
        return None

    resolved_url = normalize_url(response.url)
    if not resolved_url:
        return None

    path = urlparse(resolved_url).path.rstrip("/")
    if path in {"", "/"}:
        return None

    return resolved_url

def fetch_news_text() -> str:
    text = None

    for i in range(5):
        try:
            print(f"Gemini試行 {i+1}回目...")
            res = client.models.generate_content(
                model="gemini-3.1-flash-lite-preview",
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.3,
                )
            )

            text = res.text
            print("Gemini response取得成功")
            break

        except Exception as e:
            print("Gemini error:", e)

            if i == 4:
                raise

            wait = 5 * (i + 1)
            print(f"{wait}秒待機してリトライ...")
            time.sleep(wait)

    if not text:
        raise RuntimeError("Geminiから結果が返ってきませんでした")

    print("Gemini preview:")
    print(text[:700])
    return text


def parse_news(raw_text: str) -> dict:
    categories = {
        "AI": [],
        "経済": [],
        "政治": [],
    }

    current_category = None
    current_item = {}

    lines = raw_text.splitlines()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if line in ["[AI]", "[経済]", "[政治]"]:
            if (
                current_category
                and current_item.get("title")
                and current_item.get("summary")
                and current_item.get("url")
            ):
                categories[current_category].append(current_item)

            current_category = line.strip("[]")
            current_item = {}
            continue

        if line.startswith("タイトル:"):
            if (
                current_category
                and current_item.get("title")
                and current_item.get("summary")
                and current_item.get("url")
            ):
                categories[current_category].append(current_item)
                current_item = {}

            current_item["title"] = line.replace("タイトル:", "", 1).strip()

        elif line.startswith("要約:"):
            current_item["summary"] = line.replace("要約:", "", 1).strip()

        elif line.startswith("URL:"):
            url = line.replace("URL:", "", 1).strip()
            normalized_url = normalize_url(url)
            if normalized_url:
                current_item["url"] = normalized_url

        else:
            # 要約の続きらしき行は要約に連結
            if "summary" in current_item and "url" not in current_item:
                current_item["summary"] += " " + line

    if (
        current_category
        and current_item.get("title")
        and current_item.get("summary")
        and current_item.get("url")
    ):
        categories[current_category].append(current_item)

    return categories


def validate_news_urls(news: dict) -> dict:
    validated_news = {}
    seen_urls = set()

    for category, items in news.items():
        validated_items = []

        for item in items:
            url = item.get("url")
            if not url:
                continue

            verified_url = verify_url(url)
            if not verified_url:
                print(f"無効なURLを除外: {url}")
                continue

            if verified_url in seen_urls:
                print(f"重複URLを除外: {verified_url}")
                continue

            updated_item = item.copy()
            updated_item["url"] = verified_url
            validated_items.append(updated_item)
            seen_urls.add(verified_url)

            if len(validated_items) == 3:
                break

        validated_news[category] = validated_items

    return validated_news


def build_embed_description(items: list[dict]) -> str:
    parts = []

    for i, item in enumerate(items[:3], start=1):
        title = item.get("title", "無題")
        summary = item.get("summary", "要約なし")
        url = item.get("url", "")

        if url:
            title_line = f"**{i}. [{title}]({url})**"
        else:
            title_line = f"**{i}. {title}**"

        block = (
            f"{title_line}\n"
            f"{summary}"
        )

        parts.append(block)

    return "\n\n".join(parts)


def build_embeds(news: dict) -> list[dict]:
    timestamp = datetime.now(timezone.utc).isoformat()

    category_styles = {
        "AI": {"emoji": "🤖", "color": 0x3498DB},
        "経済": {"emoji": "💹", "color": 0x2ECC71},
        "政治": {"emoji": "🏛️", "color": 0xE74C3C},
    }

    embeds = []

    for category in ["AI", "経済", "政治"]:
        items = news.get(category, [])
        if not items:
            continue

        style = category_styles[category]
        description = build_embed_description(items)

        embed = {
            "title": f"{style['emoji']} {category}ニュース",
            "description": description[:4000],
            "color": style["color"],
            "footer": {
                "text": "Daily News Bot"
            },
            "timestamp": timestamp,
        }
        embeds.append(embed)

    return embeds


def send_to_discord(embeds: list[dict]) -> None:
    if not embeds:
        payload = {
            "content": "今日のニュースを取得できませんでした。"
        }
    else:
        payload = {
            "content": "📰 今日のニュースまとめ",
            "embeds": embeds
        }

    print("Discord に送信中...")
    r = requests.post(WEBHOOK, json=payload, timeout=30)
    print("Discord status:", r.status_code)
    print("Discord response:", r.text)
    r.raise_for_status()
    print("送信完了")


def main() -> None:
    raw_text = fetch_news_text()
    news = parse_news(raw_text)
    news = validate_news_urls(news)
    embeds = build_embeds(news)
    send_to_discord(embeds)


if __name__ == "__main__":
    main()
