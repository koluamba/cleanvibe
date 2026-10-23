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
WB_BACKFILL_MAX = 25                               # сколько дней WB докачивать за один запуск
WB_PAUSE = 30                                      # пауза между запросами к WB, сек (лимиты)

ERRORS = []
VK_DEBUG = []

# секреты: убираем случайные пробелы/переносы строк, которые попадают при копировании
SECRETS = []
for _k in ("WB_TOKEN", "APIFY_TOKEN", "YOUTUBE_API_KEY", "VK_TOKEN"):
    if os.environ.get(_k):
        os.environ[_k] = os.environ[_k].strip()
        SECRETS.append(os.environ[_k])


def log(*a):
    print(*a, flush=True)


def fail(where, e):
    msg = f"{where}: {e}"
    for sec in SECRETS:                      # никогда не выводим токены в лог и data.json
        if sec:
            msg = msg.replace(sec, "***")
    if "header value" in msg.lower():
        msg = f"{where}: неверный формат токена (проверь секрет на лишние пробелы/переносы)"
    msg = msg[:300]
    ERRORS.append(msg)
    log("⚠️ ", msg)


def http(method, url, body=None, headers=None, timeout=120, retries=3):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt else None
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == retries:
                raise
            wait = int(e.headers.get("X-RateLimit-Retry") or e.headers.get("Retry-After") or 60)
            if wait > 1500:                      # очень долгая блокировка — докачаем в следующий запуск
                raise
            log(f"429: жду {wait} сек и повторяю")
            time.sleep(wait + 2)


def first_line(s, n=80):
    import re
    lines = [l.strip() for l in (s or "").splitlines() if l.strip()]
    good = [l for l in lines if not re.search(r"артикул|WW\d+|копир|^#", l, re.I)]
    s = (good[0] if good else "").strip()
    return (s[: n - 1] + "…") if len(s) > n else (s or "Ролик без описания")


CODE_RE = __import__("re").compile(r"WW\s*(\d{4,})", __import__("re").I)


def parse_codes(*texts):
    """Коды WW из описания ролика → [[код, подпись], ...]. Подпись — текст перед кодом."""
    import re
    out, seen = [], set()
    for t in texts:
        parts = CODE_RE.split(t or "")
        for i in range(1, len(parts), 2):
            code = "WW" + parts[i]
            if code in seen:
                continue
            seen.add(code)
            label = re.split(r"[:\n()\[\]|•,;]", parts[i - 1])[-1]
            label = re.sub(r"[^\w\s\-]", " ", label)
            label = re.sub(r"\s+", " ", label).strip(" -—–_")
            out.append([code, label[:40]])
    return out


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
    try:
        run = http("POST", f"{base}/acts/{actor}/runs?token={tok}", inp)["data"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Apify {actor}: HTTP {e.code} {e.read()[:300].decode('utf-8', 'replace')}")
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
                        codes=parse_codes(x.get("caption")), views=int(views)))
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
                        published=str(pub or SNAP), codes=parse_codes(x.get("text")),
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
                            codes=parse_codes(x["snippet"].get("title"), x["snippet"].get("description")),
                            published=str(d(x["snippet"].get("publishedAt")) or SNAP),
                            account=owner.get(x["id"], ""),
                            views=int(x.get("statistics", {}).get("viewCount", 0))))
    return out


# ───────────────────────── VK ─────────────────────────
def vk_owner(name):
    """id123 / club123 / public123 → owner_id без запроса к API."""
    import re
    name = name.rstrip("/").split("/")[-1].lstrip("@")
    m = re.fullmatch(r"(id|club|public)(\d+)", name)
    if m:
        return name, (int(m.group(2)) if m.group(1) == "id" else -int(m.group(2)))
    return name, None


def collect_vk():
    owners = [a["vk"] for a in CFG["accounts"] if a.get("vk")]
    out = []
    tok = os.environ.get("VK_TOKEN")
    if owners and tok:
        try:
            out = collect_vk_api(owners, tok)
        except Exception as e:
            fail("VK API", e)
    # автопоиск: клипы VK, в описании которых есть ваши коды WW
    codes = [c for p in CFG.get("products") or [] for c in p.get("codes") or []]
    if codes and CFG.get("vkSearch", True):
        seen = {v["id"] for v in out}
        try:
            out += [v for v in collect_vk_search(codes) if v["id"] not in seen]
        except Exception as e:
            fail("VK поиск", e)
    # клипы, добавленные ссылками вручную (config.json → "vkClips")
    links = [u for u in CFG.get("vkClips") or [] if u]
    if links:
        seen = {v["id"] for v in out}
        try:
            out += [v for v in collect_vk_clips(links) if v["id"] not in seen]
        except Exception as e:
            fail("VK клипы", e)
    return out


def collect_vk_search(codes):
    """Поиск клипов VK по кодам WW (Apify maximedupre/vk-clips, режим keywords)."""
    import re
    items = apify("maximedupre~vk-clips", {"discoveryMethod": "keywords", "keywords": codes, "maxItems": 300})
    out, wanted = [], {c.upper() for c in codes}
    for x in items:
        url = x.get("clipUrl") or x.get("url") or ""
        m = re.search(r"(?:clip|video)(-?\d+)_(\d+)", url)
        text = " ".join(str(x.get(k) or "") for k in ("title", "description", "caption"))
        cs = parse_codes(text)
        if not m or not any(c in wanted for c, _ in cs):     # только клипы с вашими кодами
            continue
        pub = d(x.get("publishedAt"))
        if pub and pub < START:
            continue
        own = x.get("owner") or {}
        views = (x.get("engagement") or {}).get("views", x.get("views")) or 0
        oid, vid = m.group(1), m.group(2)
        out.append(dict(platform="vk", id=f"vk_{oid}_{vid}", url=f"https://vk.com/clip{oid}_{vid}",
                        title=first_line(text), codes=cs, published=str(pub or SNAP),
                        account=own.get("handle") or own.get("name") or oid, views=int(views)))
    VK_DEBUG.append({"search": len(items), "matched": len(out)})
    log(f"VK поиск по кодам: найдено {len(items)}, с вашими кодами — {len(out)}")
    return out


def collect_vk_clips(links):
    """Просмотры и описание конкретных клипов по ссылкам — через Apify."""
    import re
    norm = []
    for u in links:
        m = re.search(r"(clip|video)(-?\d+_\d+)", u)
        if m:
            norm.append({"url": f"https://vk.com/{m.group(1)}{m.group(2)}"})
    items = apify("automation-lab~vk-clips-scraper", {"mode": "clips", "clipUrls": norm, "maxItems": len(norm)})
    out = []
    for x in items:
        url = x.get("clipUrl") or x.get("url") or ""
        m = re.search(r"(?:clip|video)(-?\d+)_(\d+)", url)
        if not m or x.get("views") is None:
            continue
        oid, vid = m.group(1), m.group(2)
        text = " ".join(str(x.get(k) or "") for k in ("title", "caption", "description"))
        pub = d(x.get("publishedAt"))
        if pub and pub < START:
            continue
        out.append(dict(platform="vk", id=f"vk_{oid}_{vid}", url=url, title=first_line(text),
                        codes=parse_codes(text), published=str(pub or SNAP),
                        account=x.get("authorHandle") or x.get("authorName") or (f"id{oid}" if not oid.startswith("-") else f"club{oid[1:]}"),
                        views=int(x.get("views") or 0)))
    log(f"VK клипы по ссылкам: {len(out)} из {len(links)}")
    return out


def collect_vk_api(owners, tok):
    """Официальный VK API. Нужен ключ ПОЛЬЗОВАТЕЛЯ (сервисный ключ video.get не принимает)."""
    api = "https://api.vk.com/method"
    out = []
    for raw_name in owners:
        name, oid = vk_owner(raw_name)
        if oid is None:
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
                            codes=parse_codes(x.get("title"), x.get("description")),
                            published=str(pub or SNAP), account=name, views=int(x.get("views", 0))))
    return out


def collect_vk_apify(owners):
    """Без токена VK — через Apify (тот же APIFY_TOKEN)."""
    names = [vk_owner(x)[0] for x in owners]
    # VK отдаёт видео профилей только российским IP — нужен резидентный прокси Apify (RU)
    items = apify("trev0n~vk-video-profile-scraper", {
        "screenNames": names, "includeRecentVideos": True, "maxVideos": 100,
        "proxyConfiguration": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"], "apifyProxyCountry": "RU"},
    })
    out = []
    # диагностика: что вернул скрейпер (без лишних данных), чтобы понять, если ролики не находятся
    VK_DEBUG.clear()
    for prof in items:
        rv = prof.get("recentVideos") or []
        VK_DEBUG.append({"profile": prof.get("screenName") or prof.get("profileUrl"),
                         "error": prof.get("error") or prof.get("errorMessage"),
                         "stats": {k: (prof.get("statistics") or {}).get(k) for k in ("videosCount", "clipsCount")},
                         "recent": len(rv), "keys": sorted(rv[0].keys()) if rv else [],
                         "sample": [{k: x.get(k) for k in ("title", "addedAt", "isClip", "views")} for x in rv[:3]]})
    for prof in items:
        acc = prof.get("screenName") or str(prof.get("ownerId") or prof.get("id") or "")
        for x in prof.get("recentVideos") or []:
            pub = d(x.get("addedAt"))
            if pub and pub < START:
                continue
            oid, vid = x.get("ownerId") or prof.get("ownerId"), x.get("id")
            text = " ".join(str(x.get(k) or "") for k in ("title", "description"))
            out.append(dict(platform="vk", id=f"vk_{oid}_{vid}",
                            url=x.get("url") or f"https://vk.com/video{oid}_{vid}",
                            title=first_line(x.get("title")), codes=parse_codes(text),
                            published=str(pub or SNAP), account=acc, views=int(x.get("views") or 0)))
    return out


# ───────────────────────── WB ─────────────────────────
def wb_funnel_day(day):
    return wb_funnel_day_range(day, day)


def wb_funnel_day_range(start, end):
    tok = os.environ["WB_TOKEN"]
    url = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
    res, off = [], 0
    while True:
        j = http("POST", url, {"selectedPeriod": {"start": start, "end": end}, "limit": 1000, "offset": off},
                 {"Authorization": tok})
        p = j["data"]["products"]
        res += p
        if len(p) < 1000:
            return res
        off += 1000
        time.sleep(WB_PAUSE)


def wb_subs_day(day, nm_ids):
    """Статистика по подменным артикулам: прямые запросы вида «ww1262654» в поиске WB."""
    tok = os.environ["WB_TOKEN"]
    url = "https://seller-analytics-api.wildberries.ru/api/v2/search-report/product/search-texts"
    out = []
    for i in range(0, len(nm_ids), 50):
        if i:
            time.sleep(WB_PAUSE)
        j = http("POST", url, {"currentPeriod": {"start": day, "end": day}, "nmIds": nm_ids[i:i + 50],
                               "topOrderBy": "openCard", "orderBy": {"field": "openCard", "mode": "desc"},
                               "limit": 30, "includeSubstitutedSKUs": True, "includeSearchTexts": False},
                 {"Authorization": tok})
        out += ((j or {}).get("data") or {}).get("items") or []
    return out


def cur(v):
    return (v or {}).get("current", 0) if isinstance(v, dict) else (v or 0)


def day_list(a, b):
    out, x = [], a
    while x <= b:
        out.append(str(x))
        x += dt.timedelta(days=1)
    return out


BASE_DAYS = int(CFG.get("baselineDays", 14))
BASE_START = START - dt.timedelta(days=BASE_DAYS)


def collect_wb(raw):
    if not os.environ.get("WB_TOKEN"):
        log("WB_TOKEN не задан — пропускаю WB")
        return
    funnel = raw.setdefault("funnel", {})
    subs = raw.setdefault("subs", {})
    code_nm = raw.setdefault("codeNm", {})
    products = raw.setdefault("products", {})
    brand = CFG.get("brand", "").lower()
    only = {int(x) for x in CFG.get("nmIds") or []}
    mapped = {int(n) for c in (CFG.get("products") or []) for n in (c.get("nmIds") or [])}

    # дни после старта: воронка + подменные артикулы (свежие — первыми)
    days = day_list(START, SNAP) if SNAP >= START else []
    todo = sorted([s for s in days if s not in funnel or s not in subs], reverse=True)[:WB_BACKFILL_MAX]
    calls = 0

    def pause():
        nonlocal calls
        if calls:
            time.sleep(WB_PAUSE)
        calls += 1

    # база до старта — ОДНИМ запросом за весь период (WB строго лимитирует запросы)
    base_key = f"{BASE_START}..{START - dt.timedelta(days=1)}"
    if (raw.get("baseAgg") or {}).get("period") != base_key:
        pause()
        try:
            items = wb_funnel_day_range(str(BASE_START), str(START - dt.timedelta(days=1)))
            agg = {}
            for it in items:
                p = it["product"]
                if p["nmId"] not in mapped and brand and (p.get("brandName") or "").lower() != brand:
                    continue
                st = it["statistic"]["selected"]
                agg[str(p["nmId"])] = [st.get("openCount", 0), st.get("cartCount", 0),
                                       st.get("orderCount", 0), st.get("orderSum", 0)]
            raw["baseAgg"] = {"period": base_key, "days": BASE_DAYS, "nm": agg}
            log(f"WB база {base_key}: {len(agg)} карточек")
        except urllib.error.HTTPError as e:
            fail("WB база", f"HTTP {e.code}")
            if e.code == 429:
                return

    for day in todo:
        pause()
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
            if p["nmId"] not in mapped:
                if brand and (p.get("brandName") or "").lower() != brand:
                    continue
                if only and p["nmId"] not in only:
                    continue
            st = it["statistic"]["selected"]
            row[str(p["nmId"])] = [st.get("openCount", 0), st.get("cartCount", 0),
                                   st.get("orderCount", 0), st.get("orderSum", 0)]
            products[str(p["nmId"])] = {"title": p.get("title", ""), "vendorCode": p.get("vendorCode", ""),
                                        "avgPrice": st.get("avgPrice") or products.get(str(p["nmId"]), {}).get("avgPrice", 0)}
        funnel[day] = row
        log(f"WB {day}: {len(row)} карточек")

        # подменные артикулы: переходы, корзины, заказы по каждому коду WW
        pause()
        try:
            items = wb_subs_day(day, sorted(int(n) for n in products))
        except urllib.error.HTTPError as e:
            fail("WB подменные", f"{day}: HTTP {e.code} {e.read()[:200]!r}")
            if e.code == 429:
                break
            continue
        srow = {}
        for it in items:
            m = CODE_RE.search(it.get("text") or "")
            if not m:
                continue
            code, nm = "WW" + m.group(1), str(it.get("nmId"))
            o, c, n = cur(it.get("openCard")), cur(it.get("addToCart")), cur(it.get("orders"))
            f = funnel[day].get(nm)
            price = (f[3] / f[2]) if f and f[2] else products.get(nm, {}).get("avgPrice", 0)
            a = srow.setdefault(code, [0, 0, 0, 0])
            a[0] += o; a[1] += c; a[2] += n; a[3] += round(n * (price or 0))
            if nm not in code_nm.setdefault(code, []):
                code_nm[code].append(nm)          # на какую карточку ведёт код — WB сообщает сам
        subs[day] = srow
        log(f"WB {day}: подменных кодов с активностью — {len(srow)}")


def collect_ads(raw):
    """Внутренняя реклама WB по карточкам бренда (нужна категория токена «Продвижение»)."""
    tok = os.environ.get("WB_TOKEN")
    if not tok:
        return
    ads = raw.setdefault("ads", {})
    nms = set(raw.get("products", {}))
    api = "https://advert-api.wildberries.ru"
    try:
        cnt = http("GET", f"{api}/adv/v1/promotion/count", headers={"Authorization": tok})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            log("Реклама: у токена нет категории «Продвижение» — пропускаю")
            raw["adsAccess"] = False
            return
        raise
    raw["adsAccess"] = True
    since = str(BASE_START - dt.timedelta(days=1))
    ids = []
    for g in (cnt or {}).get("adverts") or []:
        if g.get("status") not in (7, 9, 11):
            continue
        for a in g.get("advert_list") or []:
            if g.get("status") in (9, 11) or (a.get("changeTime") or "")[:10] >= since:
                ids.append(a["advertId"])
    days = day_list(BASE_START, SNAP)
    # докачиваем: всё, чего нет, + 3 последних дня (статистика досчитывается)
    need = [s for s in days if s not in ads] + days[-3:]
    if not ids or not need:
        log(f"Реклама: кампаний {len(ids)}")
        return
    begin, end = min(need), max(need)
    if (dt.date.fromisoformat(end) - dt.date.fromisoformat(begin)).days > 30:
        begin = str(dt.date.fromisoformat(end) - dt.timedelta(days=30))
    fresh = {s: {} for s in day_list(dt.date.fromisoformat(begin), dt.date.fromisoformat(end))}
    for i in range(0, len(ids), 50):
        if i:
            time.sleep(WB_PAUSE)
        q = ",".join(str(x) for x in ids[i:i + 50])
        res = http("GET", f"{api}/adv/v3/fullstats?ids={q}&beginDate={begin}&endDate={end}",
                   headers={"Authorization": tok}) or []
        for camp in res:
            for day in camp.get("days") or []:
                s = (day.get("date") or "")[:10]
                if s not in fresh:
                    continue
                for app in day.get("apps") or []:
                    for x in app.get("nms") or []:
                        nm = str(x.get("nmId"))
                        if nm not in nms:
                            continue
                        r = fresh[s].setdefault(nm, [0, 0, 0, 0, 0])
                        r[0] += x.get("views", 0); r[1] += x.get("clicks", 0); r[2] += x.get("orders", 0)
                        r[3] += float(x.get("sum", 0)); r[4] += float(x.get("sum_price", 0))
    ads.update(fresh)
    log(f"Реклама: кампаний {len(ids)}, дней {begin}…{end}")


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
        codes = v.get("codes") or []
        videos.append(dict(id=vid, platform=v["platform"], title=v["title"], url=v["url"],
                           account=v.get("account", ""), published=v["published"],
                           codes=[c for c, _ in codes], daily=daily))

    funnel = raw.get("funnel", {})

    def wb_daily(nms):
        out = []
        for s in dates:
            o = c = n = rub = 0
            for nm in nms:
                a, b, e, f = funnel.get(s, {}).get(str(nm)) or [0, 0, 0, 0]
                o, c, n, rub = o + a, c + b, n + e, rub + f
            out.append({"orders": n, "revenue": rub, "clicks": o, "carts": c})
        return out

    # все карточки бренда на WB
    cards = []
    for nm, meta in raw.get("products", {}).items():
        daily = wb_daily([nm])
        if any(x["orders"] or x["clicks"] for x in daily):
            cards.append(dict(sku=nm, name=meta["title"], article=nm, vendor=meta["vendorCode"], daily=daily))

    # товары с подменными артикулами WW (из config.json) + коды, найденные в описаниях, но не заданные
    labels, views_by_code = {}, {}
    for v in raw.get("videos", {}).values():
        for c, lab in v.get("codes") or []:
            if lab and len(lab) > len(labels.get(c, "")):
                labels[c] = lab
            labels.setdefault(c, "")
    subs = raw.get("subs", {})

    def subs_daily(codes):
        out = []
        for s in dates:
            o = c = n = rub = 0
            for code in codes:
                a, b, e, f = subs.get(s, {}).get(code) or [0, 0, 0, 0]
                o, c, n, rub = o + a, c + b, n + e, rub + f
            out.append({"orders": n, "revenue": rub, "clicks": o, "carts": c})
        return out

    ads = raw.get("ads", {})
    code_nm = raw.get("codeNm", {})
    prods = raw.get("products", {})
    base_days = [s for s in day_list(BASE_START, START - dt.timedelta(days=1)) if s in funnel]
    coef_all = float(CFG.get("coef", 1.9))

    def ads_daily(nms, days=None):
        out = []
        for s in days or dates:
            v = c = n = sp = rv = 0
            for nm in nms:
                a, b, e, f, g = ads.get(s, {}).get(str(nm)) or [0, 0, 0, 0, 0]
                v, c, n, sp, rv = v + a, c + b, n + e, sp + f, rv + g
            out.append({"views": v, "clicks": c, "orders": n, "spend": round(sp), "revenue": round(rv)})
        return out

    base_agg = raw.get("baseAgg") or {}

    def base_of(nms):
        if not nms or not (base_agg.get("nm") is not None or base_days):
            return None
        if base_agg.get("nm") is not None:
            k = base_agg["days"]
            tot = lambda i: sum((base_agg["nm"].get(str(nm)) or [0, 0, 0, 0])[i] for nm in nms)
        else:
            card = [funnel.get(s, {}) for s in base_days]
            k = len(base_days)
            tot = lambda i: sum((d.get(str(nm)) or [0, 0, 0, 0])[i] for d in card for nm in nms)
        all_base = day_list(BASE_START, START - dt.timedelta(days=1))
        ad = ads_daily(nms, all_base)
        ka = sum(1 for s in all_base if s in ads)
        return {"days": k, "clicks": tot(0) / k, "orders": tot(2) / k, "revenue": tot(3) / k,
                "adSpend": sum(x["spend"] for x in ad) / ka if ka else None,
                "adOrders": sum(x["orders"] for x in ad) / ka if ka else None,
                "adClicks": sum(x["clicks"] for x in ad) / ka if ka else None}

    skus, known = [], set()
    for prod in CFG.get("products") or []:
        codes = [x.upper().replace(" ", "") for x in prod.get("codes") or []]
        known.update(codes)
        nms = [str(n) for n in prod.get("nmIds") or []] or \
              sorted({nm for c in codes for nm in code_nm.get(c, [])})
        skus.append(dict(sku=prod["name"], name=prod["name"], codes=codes, daily=subs_daily(codes),
                         byCodeWB={c: subs_daily([c]) for c in codes},
                         coef=float(prod.get("coef", coef_all)),
                         nmIds=nms, cardNames=[prods.get(n, {}).get("vendorCode", n) for n in nms],
                         card=wb_daily(nms) if nms else None,
                         ads=ads_daily(nms) if nms and raw.get("adsAccess") else None,
                         base=base_of(nms)))
    for c in sorted(set(labels) - known):
        skus.append(dict(sku=c, name=(labels.get(c) or c).capitalize(), codes=[c], unknown=True,
                         daily=subs_daily([c]), byCodeWB={c: subs_daily([c])}))

    return {
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "startDate": CFG["startDate"],
        "dates": dates,
        "videos": videos,
        "skus": skus,
        "cards": cards,
        "coef": float(CFG.get("coef", 1.9)),
        "adsAccess": raw.get("adsAccess"),
        "errors": ERRORS,
        "vkDebug": VK_DEBUG,
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
            rec.update({k: v[k] for k in ("platform", "url", "title", "published", "account", "codes")})
            rec["snap"][str(SNAP)] = max(v["views"], rec["snap"].get(str(SNAP), 0))

    try:
        collect_wb(raw)
    except Exception as e:
        fail("WB", e)
    try:
        collect_ads(raw)
    except Exception as e:
        fail("Реклама WB", e)

    out = build(raw)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log(f"Готово: {len(out['videos'])} роликов, {len(out['skus'])} товаров, ошибок: {len(ERRORS)}")


if __name__ == "__main__":
    main()
    sys.exit(0)
