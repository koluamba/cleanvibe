"""
Ежедневный сбор данных для мини-приложения CleanVibe.

Источники:
  • WB (API аналитики, воронка продаж по дням): переходы в карточку, корзины, заказы, сумма
  • Instagram, TikTok — через Apify
  • YouTube — официальный YouTube Data API
  • VK — официальный VK API (поле "vk" у аккаунтов в config.json)

Секреты (GitHub → Settings → Secrets → Actions):
  WB_TOKEN, APIFY_TOKEN, YOUTUBE_API_KEY, VK_TOKEN (необязательно)

Результат — data.json рядом с index.html. В нём же хранится история (поле "raw"),
поэтому файл коммитится в репозиторий после каждого запуска.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "data.json")
with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

MSK = dt.timezone(dt.timedelta(hours=3))           # WB считает дни по Москве
TODAY = dt.datetime.now(MSK).date()
SNAP = TODAY - dt.timedelta(days=1)                # данные «по вчерашний день»
START = dt.date.fromisoformat(CFG["startDate"])
WB_BACKFILL_MAX = 7                                # сколько дней WB докачивать за один запуск
WB_PAUSE = 25                                      # пауза между запросами к WB, сек (лимиты)

ERRORS = []


def log(*a):
    print(*a, flush=True)


def fail(where, e):
    msg = f"{where}: {e}"
    ERRORS.append(msg)
    log("⚠️ ", msg)


def http(method, url, body=None, headers=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode()
        return json.loads(txt) if txt else None


def first_line(s, n=80):
    s = (s or "").strip().split("\n")[0].strip()
    return (s[: n - 1] + "…") if len(s) > n else (s or "Без названия")


def d(s):
    """ISO-строка/unix → date (или None)."""
    if s is None or s == "":
        return None
    try:
        if isinstance(s, (int, float)):
            return dt.datetime.fromtimestamp(s, MSK).date()
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(MSK).date()
    except Exception:
        try:
            return dt.date.fromisoformat(str(s)[:10])
        except Exception:
            return None


# ───────────────────────── Apify ─────────────────────────
def apify(actor, inp):
    tok = os.environ.get("APIFY_TOKEN")
    if not tok:
        log("APIFY_TOKEN не задан — пропускаю", actor)
        return []
    base = "https://api.apify.com/v2"
    run = http("POST", f"{base}/acts/{actor}/runs?token={tok}", inp)["data"]
    rid = run["id"]
    log(f"Apify {actor}: запуск {rid}")
    for _ in range(40):                                   # до ~40 минут
        run = http("GET", f"{base}/actor-runs/{rid}?token={tok}&waitForFinish=60", timeout=90)["data"]
        if run["status"] in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
    if run["status"] != "SUCCEEDED":
        raise RuntimeError(f"запуск завершился со статусом {run['status']}")
    items = http("GET", f"{base}/datasets/{run['defaultDatasetId']}/items?token={tok}&clean=true&format=json")
    log(f"Apify {actor}: {len(items)} записей")
    return items or []


def collect_instagram():
    users = [a["instagram"] for a in CFG["accounts"] if a.get("instagram")]
    if not users:
        return []
    items = apify("apify~instagram-scraper", {
        "directUrls": [f"https://www.instagram.com/{u}/" for u in users],
        "resultsType": "posts",
        "resultsLimit": 100,
        "onlyPostsNewerThan": CFG["startDate"],
        "addParentData": False,
    })
    out = []
    for x in items:
        if x.get("error"):
            continue
        views = x.get("videoPlayCount") or x.get("videoViewCount")
        if views is None:            # фото/карусель без видео
            continue
        pub = d(x.get("timestamp"))
        if pub and pub < START:
            continue
        out.append(dict(platform="inst", id="ig_" + str(x.get("shortCode") or x.get("id")),
                        url=x.get("url"), title=first_line(x.get("caption")),
                        published=str(pub or SNAP), account=x.get("ownerUsername") or "",
                        views=int(views)))
    return out


def collect_tiktok():
    users = [a["tiktok"] for a in CFG["accounts"] if a.get("tiktok")]
    if not users:
        return []
    items = apify("clockworks~tiktok-scraper", {
        "profiles": users,
        "resultsPerPage": 100,
        "oldestPostDate": CFG["startDate"],
        "profileSorting": "latest",
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": False,
        "shouldDownloadSubtitles": False,
        "shouldDownloadSlideshowImages": False,
    })
    out = []
    for x in items:
        if x.get("errorCode") or x.get("playCount") is None:
            continue
        pub = d(x.get("createTimeISO") or x.get("createTime"))
        if pub and pub < START:
            continue
        out.append(dict(platform="tiktok", id="tt_" + str(x.get("id")),
                        url=x.get("webVideoUrl"), title=first_line(x.get("text")),
                        published=str(pub or SNAP),
                        account=(x.get("authorMeta") or {}).get("name", ""),
                        views=int(x["playCount"])))
    return out


# ───────────────────────── YouTube ─────────────────────────
def collect_youtube():
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        log("YOUTUBE_API_KEY не задан — пропускаю YouTube")
        return []
    api = "https://www.googleapis.com/youtube/v3"
    ids, owner = [], {}
    for a in CFG["accounts"]:
        h = (a.get("youtube") or "").strip()
        if not h:
            continue
        ch = http("GET", f"{api}/channels?part=contentDetails&forHandle={urllib.parse.quote(h)}&key={key}")
        if not ch.get("items"):
            fail("YouTube", f"канал {h} не найден")
            continue
        uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        token, done = "", False
        for _ in range(6):                                  # до 300 последних роликов
            pl = http("GET", f"{api}/playlistItems?part=contentDetails&maxResults=50"
                             f"&playlistId={uploads}&key={key}" + (f"&pageToken={token}" if token else ""))
            for it in pl.get("items", []):
                pub = d(it["contentDetails"].get("videoPublishedAt"))
                if pub and pub < START:
                    done = True
                    continue
                vid = it["contentDetails"]["videoId"]
                ids.append(vid)
                owner[vid] = h.lstrip("@")
            token = pl.get("nextPageToken")
            if done or not token:
                break
    out = []
    for i in range(0, len(ids), 50):
        v = http("GET", f"{api}/videos?part=statistics,snippet&id={','.join(ids[i:i + 50])}&key={key}")
        for x in v.get("items", []):
            out.append(dict(platform="youtube", id="yt_" + x["id"], url=f"https://youtu.be/{x['id']}",
                            title=first_line(x["snippet"].get("title")),
                            published=str(d(x["snippet"].get("publishedAt")) or SNAP),
                            account=owner.get(x["id"], ""),
                            views=int(x.get("statistics", {}).get("viewCount", 0))))
    return out


# ───────────────────────── VK ─────────────────────────
def collect_vk():
    owners = [a["vk"] for a in CFG["accounts"] if a.get("vk")]
    tok = os.environ.get("VK_TOKEN")
    if not owners:
        return []
    if not tok:
        log("VK_TOKEN не задан — пропускаю VK")
        return []
    api = "https://api.vk.com/method"
    out = []
    for name in owners:
        name = name.rstrip("/").split("/")[-1]
        r = http("GET", f"{api}/utils.resolveScreenName?screen_name={name}&access_token={tok}&v=5.199")
        obj = (r or {}).get("response") or {}
        if not obj:
            fail("VK", f"{name} не найден")
            continue
        oid = obj["object_id"] if obj.get("type") == "user" else -obj["object_id"]
        res = http("GET", f"{api}/video.get?owner_id={oid}&count=200&access_token={tok}&v=5.199")
        if (res or {}).get("error"):
            fail("VK", f"{name}: {res['error'].get('error_msg')}")
            continue
        for x in ((res or {}).get("response") or {}).get("items", []):
            pub = d(x.get("date"))
            if pub and pub < START:
                continue
            out.append(dict(platform="vk", id=f"vk_{oid}_{x['id']}",
                            url=f"https://vk.com/video{oid}_{x['id']}", title=first_line(x.get("title")),
                            published=str(pub or SNAP), account=name, views=int(x.get("views", 0))))
    return out


# ───────────────────────── WB ─────────────────────────
def wb_funnel_day(day):
    tok = os.environ["WB_TOKEN"]
    url = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
    res, off = [], 0
    while True:
        j = http("POST", url, {"selectedPeriod": {"start": day, "end": day}, "limit": 1000, "offset": off},
                 {"Authorization": tok})
        p = j["data"]["products"]
        res += p
        if len(p) < 1000:
            return res
        off += 1000
        time.sleep(WB_PAUSE)


def collect_wb(raw):
    if not os.environ.get("WB_TOKEN"):
        log("WB_TOKEN не задан — пропускаю WB")
        return
    funnel = raw.setdefault("funnel", {})
    products = raw.setdefault("products", {})
    brand = CFG.get("brand", "").lower()
    only = {int(x) for x in CFG.get("nmIds") or []}
    days, x = [], START
    while x <= SNAP:
        days.append(str(x))
        x += dt.timedelta(days=1)
    # недостающие дни + два последних (WB досчитывает данные с задержкой)
    todo = [s for s in days if s not in funnel]
    for s in days[-2:]:
        if s not in todo:
            todo.append(s)
    todo = sorted(todo)[-WB_BACKFILL_MAX:] if len(todo) > WB_BACKFILL_MAX else sorted(todo)
    for i, day in enumerate(todo):
        if i:
            time.sleep(WB_PAUSE)
        try:
            items = wb_funnel_day(day)
        except urllib.error.HTTPError as e:
            fail("WB", f"{day}: HTTP {e.code} {e.read()[:200]!r}")
            if e.code == 429:
                break                      # лимит — докачаем в следующий запуск
            continue
        row = {}
        for it in items:
            p = it["product"]
            if brand and (p.get("brandName") or "").lower() != brand:
                continue
            if only and p["nmId"] not in only:
                continue
            s = it["statistic"]["selected"]
            row[str(p["nmId"])] = [s.get("openCount", 0), s.get("cartCount", 0),
                                   s.get("orderCount", 0), s.get("orderSum", 0)]
            products[str(p["nmId"])] = {"title": p.get("title", ""), "vendorCode": p.get("vendorCode", "")}
        funnel[day] = row
        log(f"WB {day}: {len(row)} карточек")


# ───────────────────────── Сборка data.json ─────────────────────────
def build(raw):
    last = max(SNAP, START)
    dates, x = [], START
    while x <= last:
        dates.append(str(x))
        x += dt.timedelta(days=1)
    idx = {s: i for i, s in enumerate(dates)}

    videos = []
    for vid, v in raw.get("videos", {}).items():
        daily = [0] * len(dates)
        prev = None
        for s in sorted(v["snap"]):
            views = v["snap"][s]
            if prev is None:
                # первый замер: всё, что набрано до него, относим ко дню публикации
                at = max(v["published"], dates[0])
                if at not in idx:
                    at = s
                inc = views
            else:
                at, inc = s, max(0, views - prev)
            if at in idx:
                daily[idx[at]] += inc
            prev = views
        videos.append(dict(id=vid, platform=v["platform"], title=v["title"], url=v["url"],
                           account=v.get("account", ""), published=v["published"], daily=daily))

    skus = []
    funnel = raw.get("funnel", {})
    for nm, meta in raw.get("products", {}).items():
        daily = []
        for s in dates:
            o, c, n, rub = (funnel.get(s, {}).get(nm) or [0, 0, 0, 0])
            daily.append({"orders": n, "revenue": rub, "clicks": o, "carts": c})
        if any(x["orders"] or x["clicks"] for x in daily):
            skus.append(dict(sku=nm, name=meta["title"], article=nm, vendor=meta["vendorCode"], daily=daily))

    return {
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "startDate": CFG["startDate"],
        "dates": dates,
        "videos": videos,
        "skus": skus,
        "errors": ERRORS,
        "raw": raw,
    }


def main():
    raw = {}
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, encoding="utf-8") as f:
                raw = json.load(f).get("raw", {})
        except Exception as e:
            fail("data.json", e)
    vids = raw.setdefault("videos", {})

    for name, fn in [("Instagram", collect_instagram), ("TikTok", collect_tiktok),
                     ("YouTube", collect_youtube), ("VK", collect_vk)]:
        try:
            got = fn()
        except Exception as e:
            fail(name, e)
            continue
        log(f"{name}: {len(got)} роликов с {CFG['startDate']}")
        for v in got:
            rec = vids.setdefault(v["id"], {"snap": {}})
            rec.update({k: v[k] for k in ("platform", "url", "title", "published", "account")})
            rec["snap"][str(SNAP)] = max(v["views"], rec["snap"].get(str(SNAP), 0))

    try:
        collect_wb(raw)
    except Exception as e:
        fail("WB", e)

    out = build(raw)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log(f"Готово: {len(out['videos'])} роликов, {len(out['skus'])} товаров, ошибок: {len(ERRORS)}")


if __name__ == "__main__":
    main()
    sys.exit(0)
