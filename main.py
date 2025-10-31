import os, re, json, time, pathlib, random, hashlib
from typing import Optional, Tuple
import feedparser
import requests
import tweepy
from tweepy import Client
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

# ========================
# إعدادات عامّة قابلة للتعديل
# ========================
SITE_URL    = os.environ.get("SITE_URL", "https://loadingapk.online")
RSS         = os.environ.get("BLOG_RSS_URL", "https://loadingapk.online/feeds/posts/default?alt=rss")
YOUTUBE_URL = "https://www.youtube.com/@-Muhamedloading"
HASHTAGS    = "#لودينغ #مقالات #أبحاث #تاريخ #تقنية"
RESURFACE_EVERY_HOURS = int(os.environ.get("RESURFACE_EVERY_HOURS", "72"))
MAX_NEW_PER_RUN       = int(os.environ.get("MAX_NEW_PER_RUN", "3"))
HTTP_TIMEOUT          = 12

STATE_JSON      = pathlib.Path("posts.json")           # أرشيف التغريدات المنشورة
RESURFACE_FILE  = pathlib.Path("last_resurface.txt")   # آخر وقت إحياء

# ========================
# مفاتيح X (تويتر)
# ========================
API_KEY            = os.environ["TW_API_KEY"]
API_KEY_SECRET     = os.environ["TW_API_KEY_SECRET"]
ACCESS_TOKEN       = os.environ["TW_ACCESS_TOKEN"]
ACCESS_TOKEN_SECRET= os.environ["TW_ACCESS_TOKEN_SECRET"]
BEARER_TOKEN       = os.environ["TW_BEARER_TOKEN"]

# عميل v2 للنشر
client_v2 = Client(
    bearer_token=BEARER_TOKEN,
    consumer_key=API_KEY,
    consumer_secret=API_KEY_SECRET,
    access_token=ACCESS_TOKEN,
    access_token_secret=ACCESS_TOKEN_SECRET,
    wait_on_rate_limit=True
)

# عميل v1.1 لرفع الوسائط (صور)
auth_v1 = tweepy.OAuth1UserHandler(API_KEY, API_KEY_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET)
api_v1  = tweepy.API(auth_v1, wait_on_rate_limit=True)

# ========================
# أدوات مساعدة
# ========================
BAD_PHRASES = [
    r'المصدر\s*[:\-–]?\s*pexels', r'pexels', r'pixabay', r'unsplash',
    r'Image\s*\(forced.*?\)', r'\bsource\b.*', r'حقوق.*?الصورة', r'صورة\s+من'
]
BAD_RE = re.compile("|".join(BAD_PHRASES), re.IGNORECASE)
IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

def http_get(url: str) -> Optional[requests.Response]:
    try:
        return requests.get(url, headers={"User-Agent":"Mozilla/5.0 (LoadingAPKBot/1.0)"}, timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        return None

def clean_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(BAD_RE, " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def shorten(s: str, n: int) -> str:
    return s if len(s) <= n else s[:max(0, n-1)].rstrip() + "…"

def to_question(title: str, summary: str) -> str:
    starters = ["هل يمكن أن", "إلى أي حد يمكن أن", "ما الذي يجعل", "كيف تغيّر", "متى يصبح", "لماذا قد يكون", "هل فعلاً"]
    start = random.choice(starters)
    base = title
    if len(base) < 40 and summary:
        base = f"{title}: {summary}"
    base = re.sub(r"[\.!\u061F]+$", "", base).strip()
    return shorten(f"{start} {base}؟", 140)

def compose_tweet(title: str, summary: str, url: str) -> str:
    """
    تغريدة متعددة الأسطر (≥3):
    1) سؤال تشويقي
    2) وسوم تتضمن #لودينغ
    3) رابط المقال
    4) رابط اليوتيوب (يحذف إذا تجاوز 280)
    """
    q = to_question(title, summary)
    line1 = q
    line2 = HASHTAGS
    line3 = f"🔗 اقرأ من الموقع: {url}"
    line4 = f"🎬 يوتيوب: {YOUTUBE_URL}"

    body4 = "\n".join([line1, line2, line3, line4])
    if len(body4) <= 280:
        return body4

    body3 = "\n".join([line1, line2, line3])
    if len(body3) <= 280:
        return body3

    for qlen in (120,110,100,90,80,70,60):
        body_try = "\n".join([shorten(line1, qlen), line2, line3])
        if len(body_try) <= 280:
            return body_try

    mini_tags = "#لودينغ #مقالات"
    body_mini = "\n".join([shorten(line1, 60), mini_tags, line3])
    if len(body_mini) <= 280:
        return body_mini

    return f"{shorten(q, 60)}\n#لودينغ\n{line3}"

def fetch_entries():
    feed = feedparser.parse(RSS)
    entries = []
    for e in feed.entries:
        title = (e.get("title") or "").strip()
        link  = (e.get("link")  or "").strip()
        summary = clean_html(
            e.get("summary", "") or
            (e.get("content", [{}])[0].get("value") if e.get("content") else "")
        )
        entries.append({"title": title, "link": link, "summary": summary, "raw": e})
    return entries

# --------- استخراج صورة ---------
def extract_og_image(page_url: str) -> Optional[str]:
    """يحاول جلب og:image من صفحة المقال."""
    r = http_get(page_url)
    if not r or not r.ok:
        return None
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name":"og:image"})
        if tag and tag.get("content"):
            og = tag["content"].strip()
            if og.startswith("//"): og = "https:" + og
            if og.startswith(("http://","https://")):
                return og
    except Exception:
        return None
    return None

def find_image_url(entry) -> Optional[str]:
    """تفضيل og:image، ثم media:content/thumbnail، ثم أول <img> داخل المحتوى."""
    # 1) og:image من صفحة المقال
    page_url = entry["link"]
    og = extract_og_image(page_url)
    if og: return og

    raw = entry["raw"]

    # 2) media:content / media:thumbnail
    for key in ("media_content", "media_thumbnail"):
        if raw.get(key):
            try:
                url = raw[key][0].get("url")
                if url and url.startswith(("http://","https://")):
                    return url
            except Exception:
                pass

    # 3) أول <img> داخل summary/content
    html_blob = raw.get("content", [{}])[0].get("value") if raw.get("content") else raw.get("summary", "")
    if html_blob:
        m = IMG_RE.search(html_blob)
        if m:
            url = m.group(1)
            if url.startswith("//"): url = "https:" + url
            if url.startswith(("http://","https://")) and not url.startswith("data:"):
                return url
    return None

def download_image(url: str, max_bytes: int = 5*1024*1024) -> Optional[str]:
    try:
        with requests.get(url, headers={"User-Agent":"Mozilla/5.0 (LoadingAPKBot/1.0)"}, stream=True, timeout=HTTP_TIMEOUT) as r:
            r.raise_for_status()
            ctype = r.headers.get("Content-Type","").lower()
            # الامتداد الافتراضي
            ext = ".jpg"
            if "png" in ctype: ext = ".png"
            elif "webp" in ctype: ext = ".webp"

            path = f"/tmp/ldg_{int(time.time())}{ext}"
            size = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(8192):
                    if not chunk: continue
                    size += len(chunk)
                    if size > max_bytes:
                        f.close()
                        try: os.remove(path)
                        except: pass
                        return None
                    f.write(chunk)
            return path
    except Exception:
        return None

def upload_media(img_path: str) -> Optional[int]:
    try:
        media = api_v1.media_upload(filename=img_path)
        return media.media_id
    except Exception:
        return None

# --------- حالة/أرشيف ---------
def load_state():
    if STATE_JSON.exists():
        try:
            return json.loads(STATE_JSON.read_text(encoding="utf-8") or "[]")
        except Exception:
            return []
    return []

def save_state(items):
    STATE_JSON.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

def load_last_resurface() -> int:
    try:
        return int(RESURFACE_FILE.read_text().strip())
    except Exception:
        return 0

def save_last_resurface(ts: int):
    RESURFACE_FILE.write_text(str(ts))

# ========================
# النشر الجديد + الإحياء
# ========================
def post_new_articles(limit: int = MAX_NEW_PER_RUN) -> int:
    entries = fetch_entries()
    if not entries:
        print("[RSS] لا توجد عناصر.")
        return 0

    state = load_state()
    posted_ids = {x["pid"] for x in state}

    published = 0
    for item in entries[:10]:  # الأحدث أولاً
        pid = sha1(item["link"])
        if pid in posted_ids:
            continue

        tweet_text = compose_tweet(item["title"], item["summary"], item["link"])

        media_ids = None
        img_url = find_image_url(item)
        if img_url:
            img_path = download_image(img_url)
            if img_path:
                mid = upload_media(img_path)
                if mid: media_ids = [mid]

        if media_ids:
            resp = client_v2.create_tweet(text=tweet_text, media_ids=media_ids)
        else:
            resp = client_v2.create_tweet(text=tweet_text)

        tid = resp.data["id"]
        print("[NEW] تم النشر:", tid, "→", item["link"])

        state.append({
            "pid": pid,
            "title": item["title"],
            "link": item["link"],
            "tweet_id": tid,
            "posted_at": int(time.time())
        })
        save_state(state)

        published += 1
        if published >= limit:
            break

    if published == 0:
        print("[NEW] لا جديد للنشر.")
    return published

def maybe_resurface() -> Optional[str]:
    """
    اقتباس (Quote Tweet) من أرشيف قديم كل RESURFACE_EVERY_HOURS.
    يعمل فقط إن توفرت ملفات الحالة من تشغيل سابق (عبر artifacts).
    """
    last = load_last_resurface()
    now = int(time.time())
    if now - last < RESURFACE_EVERY_HOURS * 3600:
        print("[RESURFACE] لم يحن الوقت بعد.")
        return None

    state = load_state()
    if len(state) < 2:
        print("[RESURFACE] الأرشيف صغير.")
        save_last_resurface(now)
        return None

    cand = random.choice(state[:-1])  # استبعد الأحدث
    quote_text = random.choice([
        "تذكير مهم من أرشيفنا 📚",
        "عودة لواحدة من قراءاتنا المفضلة 🔁",
        "هل فاتتك هذه؟ 👇"
    ])
    resp = client_v2.create_tweet(text=quote_text, quote_tweet_id=cand["tweet_id"])
    print("[RESURFACE] اقتباس:", resp.data["id"], "←", cand["tweet_id"])
    save_last_resurface(now)
    return str(resp.data["id"])

# ========================
# التشغيل (مرّة واحدة)
# ========================
def main():
    posted = post_new_articles()
    maybe_resurface()
    print(f"[DONE] نشر جديد: {posted}")

if __name__ == "__main__":
    main()
