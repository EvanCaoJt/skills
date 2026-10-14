#!/usr/bin/env python3
"""通过 SocialData API 抓取 X(Twitter) 推文。Python 3.10+。

  user    HANDLE [HANDLE ...]  账号推文（默认合并成一次搜索；--timeline 走单账号完整时间线）
  search  QUERY                任意 X 高级搜索语句（from: / since: / min_faves: ...）
  tweet   URL_OR_ID [...]      单条推文；--thread / --comments N / --quotes N / --full（长文、长帖全文）
  list    LIST_ID              X 列表时间线
  profile HANDLE [HANDLE ...]  账号资料（含数字 user id）

密钥：环境变量 SOCIALDATA_API_KEY，或 --env-file 指定的本地文件。
数据写到 stdout（或 --out）；日志和花费估算写到 stderr。
"""
import argparse
import csv
import html
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

try:
    from zoneinfo import ZoneInfo
except ImportError:  # 极旧的 Python
    ZoneInfo = None

BASE = os.environ.get("SOCIALDATA_BASE_URL", "https://api.socialdata.tools").rstrip("/")
PAUSE = 0.5  # 限速 120 次/分钟
DEFAULT_BUDGET = Decimal("0.20")
UNIT_PRICE = Decimal("0.0002")
PAGE_ITEMS = 20  # 官方约 20 条/页，接口无法保证页大小；见预算说明
TITLES = {"tweets": "推文", "full": "全文（长文 / 长帖）", "thread": "同一线程", "comments": "回复", "quotes": "引用本帖的推文"}
HINTS = {
    401: "密钥无效或缺失，检查 SOCIALDATA_API_KEY",
    402: "SocialData 余额不足，请先到 https://socialdata.tools 控制台充值",
    403: "该密钥无权执行此请求",
    404: "未找到：账号/推文不存在、已删除、被封禁或受保护",
    422: "参数校验失败：检查查询语法、ID 格式",
    429: "触发限速（120 次/分钟），稍后重试",
}


class Fatal(Exception):
    pass


class NotFound(Fatal):
    pass


class BudgetExceeded(Fatal):
    pass


def log(msg):
    print(f"[socialdata] {msg}", file=sys.stderr)


# ---------- 密钥与 HTTP ----------
def load_key(path):
    key = os.environ.get("SOCIALDATA_API_KEY", "").strip()
    if not key and path:
        if not os.path.isfile(path):
            raise Fatal(f"--env-file 不存在: {path}")
        try:
            with open(path, encoding="utf-8-sig") as f:
                for line in f:
                    m = re.match(r"\s*(?:export\s+)?SOCIALDATA_API_KEY\s*=\s*(.*?)\s*$", line)
                    if m:
                        key = m.group(1).strip("'\"")
        except (OSError, UnicodeError) as e:
            raise Fatal("无法读取密钥配置文件；检查权限并保存为 UTF-8") from e
    if not key:
        raise Fatal("缺少 SOCIALDATA_API_KEY：设置环境变量，或用 --env-file 指向含 SOCIALDATA_API_KEY=... 的文件")
    return key


def _message(e):
    try:
        body = json.loads(e.read().decode("utf-8", "replace"))
        return body.get("message") or body.get("error") or str(body)
    except Exception:
        return str(getattr(e, "reason", "") or "")


def _retry_after(e):
    try:
        value = float(e.headers.get("Retry-After"))
        return max(0, min(value, 60)) if math.isfinite(value) else None
    except (TypeError, ValueError, AttributeError):
        return None


class Client:
    def __init__(self, key, budget=DEFAULT_BUDGET, checkpoint=None):
        self.key, self.requests, self._last = key, 0, 0.0
        self.units = self.uncertain_units = 0
        self.budget = budget
        self.limit = int(budget / UNIT_PRICE)
        self.page_items = PAGE_ITEMS
        self.sections, self.users, self.queries = {}, [], []
        self.complete, self.reasons = True, []
        self.checkpoint = checkpoint
        if checkpoint:
            parent = os.path.dirname(os.path.abspath(checkpoint))
            os.makedirs(parent, exist_ok=True)
            # Checkpoints contain user-requested data, never request headers or keys.
            fd = os.open(checkpoint, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(json.dumps({"event": "start", "version": 1, "budget_usd": str(budget),
                                    "at": datetime.now(timezone.utc).isoformat()}) + "\n")

    @property
    def billed(self):
        return float(UNIT_PRICE * self.units)

    def partial(self, reason):
        self.complete = False
        if reason not in self.reasons:
            self.reasons.append(reason)

    def record(self, section, tweet):
        self.sections.setdefault(section, []).append(tweet)

    def journal(self, event):
        if self.checkpoint:
            def scrub(value):
                if isinstance(value, str):
                    return value.replace(self.key, "[REDACTED]")
                if isinstance(value, list):
                    return [scrub(item) for item in value]
                if isinstance(value, dict):
                    return {name: scrub(item) for name, item in value.items()}
                return value
            with open(self.checkpoint, "a", encoding="utf-8", newline="") as f:
                f.write(json.dumps(scrub(event), ensure_ascii=False) + "\n")
                f.flush()

    def reserve(self, items):
        if self.units + items > self.limit:
            raise BudgetExceeded(
                f"预算 ${self.budget} 不足以预留下一次请求约 {items} 个对象；"
                f"已累计估算 ${self.billed:.4f}。需要更多数据时显式调高 --budget-usd；"
                "超过 $0.20 还需 --yes")

    def call(self, path, params=None, body=None, *, page=False):
        if page:
            expected = self.page_items
        elif body is not None:
            expected = len(body.get("ids", body.get("usernames", [])))
        else:
            expected = 1
        expected = max(1, expected)
        url = BASE + path
        if params:
            qs = {k: v for k, v in params.items() if v not in (None, "")}
            if qs:
                url += "?" + urllib.parse.urlencode(qs, quote_via=urllib.parse.quote)
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
            "User-Agent": "socialdata-fetch-tweets/1.0",
        }
        if data:
            headers["Content-Type"] = "application/json"
        for attempt in range(4):
            self.reserve(expected)
            gap = PAUSE - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()
            self.requests += 1
            req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    response = json.loads(r.read().decode("utf-8"))
                if page:
                    objects = response.get("tweets") if isinstance(response, dict) else response
                elif body is not None:
                    objects = (response if isinstance(response, list) else
                               response.get("tweets", response.get("users")) if isinstance(response, dict) else None)
                else:
                    objects = [response]
                if (not isinstance(objects, list) or not all(isinstance(obj, dict) and
                        re.fullmatch(r"\d{1,25}", str(obj.get("id_str") or obj.get("id") or ""))
                        for obj in objects)):
                    self.units += expected
                    self.uncertain_units += expected
                    self.journal({"event": "invalid_response", "path": path, "params": params,
                                  "body": body, "response": response, "reserved_units": expected,
                                  "cumulative_estimated_units": self.units})
                    raise Fatal("接口响应结构无效，停止请求；已保留此前结果")
                charged = max(1, len(objects))  # 空结果也可能触发公平使用计费，保守估算
                self.units += charged
                if page:
                    self.page_items = max(self.page_items, charged)
                self.journal({"event": "response", "path": path, "params": params, "body": body,
                              "response": response, "estimated_units": charged,
                              "cumulative_estimated_units": self.units})
                if self.units > self.limit:
                    self.partial("接口返回页大于预留数量，估算费用超出预算；停止后续请求")
                    log(self.reasons[-1])
                return response
            except urllib.error.HTTPError as e:
                msg = str(_message(e)).replace(self.key, "[REDACTED]")
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    wait = _retry_after(e)
                    wait = 2 ** (attempt + 1) if wait is None else wait
                    log(f"HTTP {e.code}（{msg}），{wait:g}s 后重试 {attempt + 1}/3")
                    time.sleep(wait)
                    continue
                text = f"HTTP {e.code}: {msg}" + (f"——{HINTS[e.code]}" if e.code in HINTS else "")
                raise (NotFound if e.code == 404 else Fatal)(text)
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                # A response may have been billed even if it never reached this process.
                self.units += expected
                self.uncertain_units += expected
                self.journal({"event": "uncertain_request", "path": path, "params": params,
                              "body": body, "reserved_units": expected,
                              "cumulative_estimated_units": self.units})
                if attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise Fatal(f"网络或响应解析错误: {str(e).replace(self.key, '[REDACTED]')}")


# ---------- 解析工具 ----------
def tz_of(name):
    if name in ("UTC", "Etc/UTC", "Z"):
        return timezone.utc
    offset = re.fullmatch(r"(?:UTC)?([+-])(\d{2}):(\d{2})", name)
    if offset:
        hours, minutes = int(offset[2]), int(offset[3])
        if hours < 24 and minutes < 60:
            delta = timedelta(hours=hours, minutes=minutes)
            return timezone(delta if offset[1] == "+" else -delta)
        raise Fatal(f"无效时区偏移: {name}")
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except (KeyError, ValueError) as e:
            raise Fatal(f"无法加载时区 {name!r}：核对名称；缺少 IANA 数据时安装 tzdata。"
                        "也可使用 UTC 或 UTC+08:00 等固定偏移") from e
    raise Fatal("命名时区需要 Python 3.9+ 的 zoneinfo 和 IANA 时区数据")


def parse_when(s, tz):
    try:
        dt = datetime.fromisoformat(s.strip().replace(" ", "T"))
    except ValueError:
        raise Fatal(f"无法解析时间 {s!r}，示例：2026-10-06 或 2026-10-06T08:00")
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def created(t):
    s = t.get("tweet_created_at")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def tweet_id(t):
    return str(t.get("id_str") or t.get("id"))


def handle_of(s):
    m = re.search(r"(?:x|twitter)\.com/@?([A-Za-z0-9_]{1,15})(?:[/?#]|$)", s)
    h = m.group(1) if m else s.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", h):
        raise Fatal(f"账号名无效: {s!r}")
    return h


def tweet_id_of(s):
    m = re.search(r"status(?:es)?/(\d+)", s) or re.fullmatch(r"\s*(\d{5,25})\s*", s)
    if not m:
        raise Fatal(f"无法识别推文 ID/链接: {s!r}")
    return m.group(1)


def list_id_of(s):
    m = re.search(r"lists/(\d+)", s) or re.fullmatch(r"\s*(\d{5,25})\s*", s)
    if not m:
        raise Fatal(f"无法识别列表 ID/链接: {s!r}")
    return m.group(1)


# ---------- 抓取 ----------
def collect(c, path, params, limit, keep=None, stop=None, section="tweets"):
    """按页抓取，直到凑够 limit 条 / 没有下一页 / stop(t) 为真；每页约 20 条，按条计费。"""
    out, seen, cursor, scanned = [], set(), None, 0
    cap = limit * 5 + 40  # 客户端过滤很严时，避免为凑数无限翻页烧钱
    while len(out) < limit:
        data = c.call(path, {**params, "cursor": cursor}, page=True)
        page = (data.get("tweets") if isinstance(data, dict) else data) or []
        scanned += len(page)
        done = False
        for t in page:
            if tweet_id(t) in seen:
                continue
            seen.add(tweet_id(t))
            if stop and stop(t):
                done = True
                break
            if not keep or keep(t):
                out.append(t)
                c.record(section, t)
                if len(out) == limit:
                    break
        previous_cursor = cursor
        cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if len(out) >= limit or done or not page or not cursor:
            break
        if cursor == previous_cursor:
            c.partial("接口重复返回同一游标，已停止分页")
            break
        if scanned >= cap:
            reason = f"已扫描 {scanned} 条仍未凑够 {limit} 条，停止（可放宽过滤条件）"
            c.partial(reason)
            log(reason)
            break
    return out[:limit]


def search_ops(a, tz):
    ops = []
    if a.start_time:
        ops.append(f"since_time:{int(a.start_time.timestamp())}")
    if a.end_time:
        ops.append(f"until_time:{int(a.end_time.timestamp())}")
    if a.since_id:
        ops.append(f"since_id:{a.since_id}")
    if a.min_faves:
        ops.append(f"min_faves:{a.min_faves}")
    if a.lang:
        ops.append(f"lang:{a.lang}")
    if a.originals:
        ops += ["-filter:replies", "-filter:retweets"]
    elif a.no_replies:
        ops.append("-filter:replies")
    return ops


def timeline(c, handle, a, tz):
    prof = c.call(f"/twitter/user/{handle}")
    uid = str(prof.get("id_str") or prof.get("id"))
    path = f"/twitter/user/{uid}/" + ("tweets" if (a.no_replies or a.originals) else "tweets-and-replies")
    lo, hi = a.start_time, a.end_time
    sid = int(a.since_id) if a.since_id else None

    def too_old(t):  # 时间线新→旧；置顶帖可能很旧，不能据此停止
        if t.get("is_pinned"):
            return False
        d = created(t)
        return bool((lo and d and d < lo) or (sid and int(tweet_id(t)) <= sid))

    def keep(t):
        d = created(t)
        if (lo and d and d < lo) or (hi and d and d >= hi) or (sid and int(tweet_id(t)) <= sid):
            return False
        if t.get("retweeted_status") and not a.with_retweets:
            return False
        if (a.no_replies or a.originals) and t.get("in_reply_to_status_id_str"):
            return False
        if a.originals and (t.get("is_quote_status") or t.get("quoted_status")):
            return False
        if a.min_faves and (t.get("favorite_count") or 0) < a.min_faves:
            return False
        return not (a.lang and t.get("lang") != a.lang)

    return collect(c, path, {}, a.max, keep=keep, stop=too_old)


def chunks(handles, size, per_account):
    if per_account:
        return [[h] for h in handles]
    out, cur = [], []
    for h in handles:
        if cur and (len(cur) >= size or len(" OR ".join(f"from:{x}" for x in cur + [h])) > 380):
            out.append(cur)
            cur = []
        cur.append(h)
    return out + [cur] if cur else out


def cmd_search(c, a, tz):
    ops = search_ops(a, tz)
    base = f"({a.query})" if ops and " OR " in a.query else a.query  # OR 的优先级高于空格(AND)，带过滤条件时先加括号
    q = " ".join([base] + ops)
    c.queries.append(q)
    return {"tweets": collect(c, "/twitter/search", {"query": q, "type": a.type}, a.max)}, [q]


def cmd_user(c, a, tz):
    handles = list(dict.fromkeys(handle_of(h) for h in a.handles))
    out, queries = [], []
    if a.timeline:
        for h in handles:
            queries.append(f"timeline:@{h}")
            c.queries.append(queries[-1])
            try:
                out += timeline(c, h, a, tz)
            except NotFound as e:
                log(f"@{h}: {e}")
                c.partial(f"@{h}: {e}")
        return {"tweets": out}, queries
    ops = search_ops(a, tz)
    parts = chunks(handles, a.batch, a.per_account)
    for ch in parts:
        who = f"from:{ch[0]}" if len(ch) == 1 else "(" + " OR ".join(f"from:{h}" for h in ch) + ")"
        q = " ".join([who] + ops)
        queries.append(q)
        c.queries.append(q)
        out += collect(c, "/twitter/search", {"query": q, "type": "Latest"}, a.max)
    if len(parts) > 1 and not a.per_account:  # 多批合并：总体取最新 --max 条
        out = order(dedupe(out), "new")[: a.max]
    return {"tweets": out}, queries


def cmd_tweet(c, a, tz):
    ids = list(dict.fromkeys(tweet_id_of(r) for r in a.refs))
    c.queries.extend(f"tweet:{i}" for i in ids)
    main = c.sections.setdefault("tweets", [])
    if len(ids) > 1 and (a.thread or a.comments or a.quotes):
        raise Fatal("--thread / --comments / --quotes 只支持单条推文")
    if len(ids) == 1:
        main.append(c.call(f"/twitter/tweets/{ids[0]}"))
    else:
        for i in range(0, len(ids), 100):
            d = c.call("/twitter/tweets-by-ids", body={"ids": ids[i : i + 100]})
            got = d if isinstance(d, list) else d.get("tweets", [])
            main += got
    sec = {"tweets": main}
    if a.full:  # 长文 / 被截断的长帖：接口按「帖子 ID」返回 Markdown，每条 $0.0002
        sec["full"] = c.sections.setdefault("full", [])
        for i in ids:
            sec["full"].append(c.call(f"/v2/twitter/article/{i}"))
    if a.thread:
        sec["thread"] = collect(c, f"/twitter/thread/{ids[0]}", {}, a.max,
                                keep=lambda t: tweet_id(t) != ids[0], section="thread")
    if a.comments:
        sec["comments"] = collect(c, f"/twitter/tweets/{ids[0]}/comments", {}, a.comments, section="comments")
    if a.quotes:
        sec["quotes"] = collect(c, f"/twitter/tweets/{ids[0]}/quotes", {}, a.quotes, section="quotes")
    return sec, [f"tweet:{i}" for i in ids]


def cmd_list(c, a, tz):
    lid = list_id_of(a.list_id)
    c.queries.append(f"list:{lid}")
    return {"tweets": collect(c, f"/twitter/list/{lid}/tweets", {}, a.max)}, [f"list:{lid}"]


def cmd_profile(c, a, tz):
    hs = list(dict.fromkeys(handle_of(h) for h in a.handles))
    users = c.users
    c.queries.extend(f"profile:@{h}" for h in hs)
    if len(hs) == 1:
        users.append(c.call(f"/twitter/user/{hs[0]}"))
    else:
        for i in range(0, len(hs), 100):
            d = c.call("/twitter/users-by-usernames", body={"usernames": hs[i : i + 100]})
            users += d if isinstance(d, list) else d.get("users", [])
    missing = set(h.lower() for h in hs) - set((u.get("screen_name") or "").lower() for u in users)
    if missing:
        log("未返回资料的账号: " + ", ".join(sorted(missing)))
        c.partial("未返回资料的账号: " + ", ".join(sorted(missing)))
    return users, [f"profile:@{h}" for h in hs]


# ---------- 整理与输出 ----------
def dedupe(tweets):
    seen, out = set(), []
    for t in tweets:
        if tweet_id(t) not in seen:
            seen.add(tweet_id(t))
            out.append(t)
    return out


def order(tweets, how):
    if how == "likes":
        return sorted(tweets, key=lambda t: t.get("favorite_count") or 0, reverse=True)
    if how in ("new", "old"):
        return sorted(tweets, key=lambda t: int(tweet_id(t)), reverse=(how == "new"))
    return tweets


def clean_text(t):
    s = t.get("full_text") or t.get("text") or ""
    ent = t.get("entities") or {}
    for u in ent.get("urls") or []:
        if u.get("url") and u.get("expanded_url"):
            s = s.replace(u["url"], u["expanded_url"])
    for m in ent.get("media") or []:
        if m.get("url"):
            s = s.replace(m["url"], "")
    return html.unescape(s).strip()


def post_url(t):
    return f"https://x.com/{(t.get('user') or {}).get('screen_name') or 'i'}/status/{tweet_id(t)}"


def norm(t, tz):
    rt = t.get("retweeted_status")
    b = rt or t  # 转推：正文与互动数据取原帖
    u, bu = t.get("user") or {}, b.get("user") or {}
    ent = b.get("entities") or {}
    q = b.get("quoted_status")
    kind = "retweet" if rt else "quote" if (q or b.get("is_quote_status")) else "reply" if t.get("in_reply_to_status_id_str") else "tweet"
    dt = created(t)
    text = clean_text(b)
    if q:  # 引用推文末尾自带的原帖链接与下方「↳ 引用」重复，去掉
        text = re.sub(r"\s*https?://(?:www\.)?(?:x|twitter)\.com/\w+/status(?:es)?/%s\S*" % tweet_id(q), "", text).strip()
    links = [x["expanded_url"] for x in ent.get("urls") or [] if x.get("expanded_url")]
    r = {
        "id": tweet_id(t),
        "url": post_url(t),
        "created_at": t.get("tweet_created_at"),
        "time": dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if dt else "",
        "author": u.get("screen_name"),
        "author_name": u.get("name"),
        "author_followers": u.get("followers_count"),
        "type": kind,
        "text": text,
        "display_text": text,
        "raw_text": b.get("full_text") or b.get("text") or "",
        "lang": b.get("lang"),
        "reply_to": t.get("in_reply_to_screen_name"),
        "likes": b.get("favorite_count"),
        "retweets": b.get("retweet_count"),
        "replies": b.get("reply_count"),
        "quotes": b.get("quote_count"),
        "bookmarks": b.get("bookmark_count"),
        "views": b.get("views_count"),
        "links": links,
        "has_article": any(re.search(r"(?:x|twitter)\.com/.*/article(?:/|$)", link) for link in links),
        "media": [{"type": m.get("type"), "url": m.get("media_url_https")} for m in ent.get("media") or []],
        "hashtags": [x["text"] for x in ent.get("hashtags") or [] if x.get("text")],
        "cashtags": [x["text"] for x in ent.get("symbols") or [] if x.get("text")],
    }
    if "markdown" in t:  # /v2/twitter/article 返回的全文
        r["markdown"], r["markdown_source"] = t["markdown"], t.get("markdown_source")
        r["title"] = (t.get("article") or {}).get("title")
    if rt:
        r["orig_author"], r["orig_url"] = bu.get("screen_name"), post_url(rt)
    if q:
        r["quoted"] = {"url": post_url(q), "author": (q.get("user") or {}).get("screen_name"),
                       "text": clean_text(q), "raw_text": q.get("full_text") or q.get("text") or ""}
    return r


def short(n):
    if n is None:
        return "-"
    for unit, div in (("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + unit
    return str(n)


def md_item(i, r):
    who = f"@{r['author']}" + (f"（{r['author_name']}）" if r.get("author_name") else "")
    tag = {
        "retweet": f" · 转推 @{r.get('orig_author')}",
        "quote": " · 引用推文",
        "reply": f" · 回复 @{r.get('reply_to')}",
    }.get(r["type"], "")
    out = [f"**{i}. {who}** · {r['time']}{tag} · [原帖]({r['url']})", ""]
    out += [("> " + ln).rstrip() for ln in (r["text"] or "（无正文）").split("\n")]
    if r.get("quoted"):
        qt = re.sub(r"\s+", " ", r["quoted"]["text"])
        out.append(f"> ↳ 引用 @{r['quoted']['author']}：{qt}（{r['quoted']['url']}）")
    out.append("")
    if r.get("has_article"):
        out.append("含 X 长文：用 `tweet <链接> --full` 读取全文  ")
    if r["media"]:
        kinds = {}
        for m in r["media"]:
            kinds[m["type"]] = kinds.get(m["type"], 0) + 1
        out.append("媒体：" + "、".join(f"{k}×{v}" for k, v in kinds.items()) + "  ")
    out.append(
        f"赞 {short(r['likes'])} · 转发 {short(r['retweets'])} · 回复 {short(r['replies'])} · "
        f"引用 {short(r['quotes'])} · 浏览 {short(r['views'])}"
    )
    return "\n".join(out)


def md_full(i, r):
    kind = "X 长文" if r.get("markdown_source") == "article" else "长帖正文"
    title = f"《{r['title']}》" if r.get("title") else ""
    return f"### {i}. @{r['author']} · {kind}{title} · {r['time']} · [原帖]({r['url']})\n\n{r.get('markdown', '').strip()}\n"


def csv_row(row, mode):
    if mode == "raw":
        return row
    def safe(value):
        if isinstance(value, str):
            start = value.lstrip(" \t\r\n")
            if (start and start[0] in "=+-@＝＋－＠") or value.startswith(("\t", "\r", "\n")):
                return "\t" + value
        return value
    return {k: safe(v) for k, v in row.items()}


def render_tweets(sections, raw, queries, a, tz, meta):
    recs = {k: [norm(t, tz) for t in v] for k, v in sections.items()}
    if a.format in ("json", "jsonl"):
        pick = raw if a.raw else recs
        if a.format == "jsonl":
            return "".join(json.dumps({"section": k, "data": x}, ensure_ascii=False) + "\n"
                           for k, v in pick.items() for x in v)
        body = {"meta": meta}
        body.update(pick)
        return json.dumps(body, ensure_ascii=False, indent=2) + "\n"
    if a.format == "csv":
        cols = ["section", "id", "url", "time", "created_at", "author", "author_name", "type", "raw_text", "display_text", "text", "likes",
                "retweets", "replies", "quotes", "bookmarks", "views", "lang", "links", "quoted_url", "markdown"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n", quoting=csv.QUOTE_ALL)
        w.writeheader()
        for k, v in recs.items():
            for r in v:
                w.writerow(csv_row({**r, "section": k, "links": " ".join(r["links"]),
                                    "quoted_url": (r.get("quoted") or {}).get("url", "")}, a.csv_mode))
        return buf.getvalue()
    n = meta["unique_tweet_count"]
    lines = [f"# X 推文（{n} 条不同推文，{meta['record_count']} 条结果记录）", "",
             "- 完整性：" + ("请求范围内已完成" if meta["complete"] else "部分结果，任务未完成"),
             "- 查询：" + "；".join(f"`{q}`" for q in queries)]
    if meta.get("stop_reasons"):
        lines.append("- 停止原因：" + "；".join(meta["stop_reasons"]))
    if meta.get("range"):
        lines.append(f"- 时间范围（{a.tz}）：{meta['range']}")
    for k, v in recs.items():
        if not v:
            continue
        lines += ["", f"## {TITLES[k]}（{len(v)}）"] if len(recs) > 1 else []
        lines += [""] + [(md_full if k == "full" else md_item)(i, r) + "\n" for i, r in enumerate(v, 1)]
    return "\n".join(lines).rstrip() + "\n"


def render_profiles(users, queries, a, tz, meta):
    if a.format in ("json", "jsonl"):
        if a.format == "jsonl":
            return "".join(json.dumps({"section": "users", "data": u}, ensure_ascii=False) + "\n" for u in users)
        return json.dumps({"meta": meta, "users": users}, ensure_ascii=False, indent=2) + "\n"
    keys = ["screen_name", "name", "id_str", "followers_count", "friends_count", "statuses_count", "verified", "created_at", "description"]
    if a.format == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore", lineterminator="\n", quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(csv_row(u, a.csv_mode) for u in users)
        return buf.getvalue()
    lines = [f"# X 账号资料（{len(users)} 个）", "",
             "- 完整性：" + ("请求范围内已完成" if meta["complete"] else "部分结果，任务未完成"), ""]
    if meta.get("stop_reasons"):
        lines += ["- 停止原因：" + "；".join(meta["stop_reasons"]), ""]
    for u in users:
        lines.append(
            f"**@{u.get('screen_name')}**（{u.get('name')}）· user id `{u.get('id_str')}` · 粉丝 {short(u.get('followers_count'))}"
            f" · 关注 {short(u.get('friends_count'))} · 推文 {short(u.get('statuses_count'))}"
            f"{' · 已认证' if u.get('verified') else ''}{' · 受保护' if u.get('protected') else ''}"
        )
        if u.get("description"):
            lines += ["", "> " + u["description"].replace("\n", " ")]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--format", choices=["md", "json", "jsonl", "csv"], default="md", help="输出格式（默认 md）")
    common.add_argument("--out", help="写入文件，而不是 stdout")
    common.add_argument("--raw", action="store_true", help="json/jsonl 输出 API 原始对象（默认输出整理后的字段）")
    common.add_argument("--tz", default="UTC+08:00", help="时间显示与解析；默认 UTC+08:00；IANA 时区需系统数据或 tzdata")
    common.add_argument("--sort", choices=["new", "old", "likes", "keep"], help="排序；keep 保持接口顺序")
    common.add_argument("--env-file", help="含 SOCIALDATA_API_KEY=... 的本地文件；不自动查找或保存密钥")
    common.add_argument("--budget-usd", type=money, default=DEFAULT_BUDGET, help="整次任务的估算预算，默认 0.20 美元")
    common.add_argument("--yes", action="store_true", help="确认显式设置超过 0.20 美元的预算；不会取消预算上限")
    common.add_argument("--checkpoint", help="保存原始响应的 JSONL 检查点；指定 --out 时默认 <out>.checkpoint.jsonl")
    common.add_argument("--csv-mode", choices=["safe", "raw"], default="safe", help="safe 防护表格公式前缀；raw 仅供程序导入")
    cap = argparse.ArgumentParser(add_help=False)
    cap.add_argument("--max", type=int, default=50, help="最多返回条数（默认 50；每页约 20 条，每条约 $0.0002）")
    flt = argparse.ArgumentParser(add_help=False)
    flt.add_argument("--since", help="起始时间，如 2026-10-01 或 2026-10-01T08:00（按 --tz 解释）")
    flt.add_argument("--until", help="结束时间（不含）")
    flt.add_argument("--since-hours", type=float, help="只要最近 N 小时")
    flt.add_argument("--since-id", help="只要比该推文 ID 更新的（增量抓取）")
    flt.add_argument("--min-faves", type=int, help="至少 N 个赞")
    flt.add_argument("--lang", help="语言代码，如 en / zh")
    flt.add_argument("--no-replies", action="store_true", help="排除回复")
    flt.add_argument("--originals", action="store_true", help="只要原创：排除回复、转推、引用")

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("user", parents=[common, cap, flt], help="账号推文")
    s.add_argument("handles", nargs="+", help="账号名、@账号或主页链接")
    s.add_argument("--timeline", action="store_true", help="走用户时间线接口（每账号多 1 次资料查询）")
    s.add_argument("--with-retweets", action="store_true", help="时间线模式下保留纯转推")
    s.add_argument("--per-account", action="store_true", help="每个账号单独查询，--max 对每个账号生效")
    s.add_argument("--batch", type=int, default=20, help="每次搜索合并的账号数（默认 20）")
    s.set_defaults(fn=cmd_user)
    s = sub.add_parser("search", parents=[common, cap, flt], help="高级搜索")
    s.add_argument("query")
    s.add_argument("--type", choices=["Latest", "Top"], default="Latest", help="Latest 最新 / Top 热门")
    s.set_defaults(fn=cmd_search)
    s = sub.add_parser("tweet", parents=[common, cap], help="单条推文")
    s.add_argument("refs", nargs="+", help="推文链接或 ID")
    s.add_argument("--full", action="store_true", help="同时取全文：X 长文，或被截断的长帖（正文以 … 结尾）")
    s.add_argument("--thread", action="store_true", help="同时抓取所在线程")
    s.add_argument("--comments", type=int, default=0, help="同时抓取前 N 条回复")
    s.add_argument("--quotes", type=int, default=0, help="同时抓取前 N 条引用推文")
    s.set_defaults(fn=cmd_tweet)
    s = sub.add_parser("list", parents=[common, cap], help="列表时间线")
    s.add_argument("list_id", help="列表 ID 或链接")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("profile", parents=[common], help="账号资料")
    s.add_argument("handles", nargs="+")
    s.set_defaults(fn=cmd_profile)
    return p


def money(value):
    try:
        result = Decimal(value)
        if not result.is_finite() or result < UNIT_PRICE:
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("预算必须是至少 0.0002 的有限正数")


def validate_args(a):
    # Complete all user input checks before loading a key or making a paid request.
    tz = tz_of(a.tz)
    for name in ("max", "batch"):
        if hasattr(a, name) and getattr(a, name) <= 0:
            raise Fatal(f"--{name} 必须大于 0")
    for name in ("comments", "quotes", "min_faves"):
        value = getattr(a, name, None)
        if value is not None and value < 0:
            raise Fatal(f"--{name.replace('_', '-')} 不能小于 0")
    hours = getattr(a, "since_hours", None)
    if hours is not None and (not math.isfinite(hours) or hours <= 0):
        raise Fatal("--since-hours 必须是有限正数")
    if getattr(a, "since_id", None):
        a.since_id = tweet_id_of(a.since_id) if re.fullmatch(r"\d{5,25}", a.since_id) else None
        if not a.since_id:
            raise Fatal("--since-id 必须是 5–25 位数字推文 ID")
    starts = []
    if getattr(a, "since", None):
        starts.append(parse_when(a.since, tz))
    if hours is not None:
        try:
            starts.append(datetime.now(timezone.utc) - timedelta(hours=hours))
        except OverflowError as e:
            raise Fatal("--since-hours 超出支持范围") from e
    a.start_time = max(starts) if starts else None
    a.end_time = parse_when(a.until, tz) if getattr(a, "until", None) else None
    if a.start_time and a.end_time and a.start_time >= a.end_time:
        raise Fatal("起始时间必须早于结束时间")
    if hasattr(a, "handles"):
        a.handles = list(dict.fromkeys(handle_of(h) for h in a.handles))
    if a.cmd == "tweet":
        a.refs = list(dict.fromkeys(tweet_id_of(s) for s in a.refs))
        if len(a.refs) > 1 and (a.thread or a.comments or a.quotes):
            raise Fatal("--thread / --comments / --quotes 只支持单条推文")
    if a.cmd == "list":
        a.list_id = list_id_of(a.list_id)
    if a.raw and a.format not in ("json", "jsonl"):
        raise Fatal("--raw 仅支持 json/jsonl 格式")
    if a.budget_usd > DEFAULT_BUDGET and not a.yes:
        raise Fatal(f"预算 ${a.budget_usd} 超过 $0.20；告知用户估算后使用 --yes 确认")
    units = estimate_units(a)
    if units * UNIT_PRICE > a.budget_usd:
        raise Fatal(f"按目标条数及约 20 条/页估算约需 ${units * UNIT_PRICE:.4f}，"
                    f"超过预算 ${a.budget_usd}；调低条数或显式提高 --budget-usd（超过 $0.20 需 --yes）")
    try:
        base = urllib.parse.urlsplit(BASE)
    except ValueError as e:
        raise Fatal("SOCIALDATA_BASE_URL 不是有效地址") from e
    if base.scheme != "https" or not base.netloc or base.username or base.password or base.query or base.fragment:
        raise Fatal("SOCIALDATA_BASE_URL 必须是无凭据、查询参数和片段的 HTTPS 地址")
    checkpoint = a.checkpoint or (a.out + ".checkpoint.jsonl" if a.out else None)
    files = [path for path in (a.out, checkpoint,
             a.out + ".meta.json" if a.out and a.format in ("csv", "jsonl") else None) if path]
    canonical = [os.path.normcase(os.path.realpath(path)) for path in files]
    if len(set(canonical)) != len(canonical):
        raise Fatal("输出、检查点和元数据不能指向同一文件")
    protected = [os.path.normcase(os.path.realpath(path)) for path in (__file__, a.env_file) if path]
    if set(canonical) & set(protected):
        raise Fatal("输出文件不能覆盖脚本或密钥配置文件")
    # Detect unwritable output paths before any paid calls, without truncating an existing result.
    if a.out:
        target = os.path.abspath(a.out)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.isdir(target):
            raise Fatal("--out 必须指向文件")
        import tempfile
        fd, probe = tempfile.mkstemp(prefix=".socialdata-write-", dir=os.path.dirname(target))
        os.close(fd)
        os.unlink(probe)
    return tz, checkpoint


def estimate_units(a):
    pages = lambda n: ((n + PAGE_ITEMS - 1) // PAGE_ITEMS) * PAGE_ITEMS
    if a.cmd == "profile":
        return len(a.handles)
    if a.cmd in ("search", "list"):
        return pages(a.max)
    if a.cmd == "user":
        if a.timeline:
            return len(a.handles) * (1 + pages(a.max))
        return len(chunks(a.handles, a.batch, a.per_account)) * pages(a.max)
    return (len(a.refs) * (1 + int(a.full)) + (pages(a.max) if a.thread else 0)
            + pages(a.comments) + pages(a.quotes))


def write_output(text, path):
    if not path:
        sys.stdout.write(text)
        return
    import tempfile
    target = os.path.abspath(path)
    fd, temporary = tempfile.mkstemp(prefix=".socialdata-result-", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def finish(c, a, tz, result=None):
    meta = {"queries": c.queries, "estimated_cost_usd": round(c.billed, 4), "requests": c.requests,
            "budget_usd": float(c.budget), "uncertain_cost_usd": float(c.uncertain_units * UNIT_PRICE),
            "complete": c.complete, "stop_reasons": c.reasons,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if a.cmd == "profile":
        users = result if result is not None else c.users
        count = len(users)
        meta.update(count=count, record_count=count)
        text = render_profiles(users, c.queries, a, tz, meta)
    else:
        sections = result if result is not None else c.sections
        defaults = {"tweets": "keep" if a.cmd == "tweet" or (a.cmd == "search" and a.type == "Top") else "new", "thread": "old"}
        raw = {k: order(dedupe(v), a.sort or defaults.get(k)) for k, v in sections.items()}
        if a.cmd == "user" and not a.per_account and not a.timeline and "tweets" in raw:
            raw["tweets"] = raw["tweets"][:a.max]
        count = len({tweet_id(t) for v in raw.values() for t in v})
        meta.update(count=count, unique_tweet_count=count, record_count=sum(len(v) for v in raw.values()))
        dts = [d for d in (created(t) for t in raw.get("tweets", [])) if d]
        if dts:
            fmt = lambda d: d.astimezone(tz).strftime("%Y-%m-%d %H:%M")
            meta["range"] = f"{fmt(min(dts))} ~ {fmt(max(dts))}"
        text = render_tweets(raw, raw, c.queries, a, tz, meta)
    write_output(text, a.out)
    # Stream formats have no top-level metadata: keep completeness and accounting beside the file.
    if a.out and a.format in ("csv", "jsonl"):
        write_output(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", a.out + ".meta.json")
    c.journal({"event": "finish", "meta": meta})
    state = "完成" if c.complete else "未完成（已保留部分结果）"
    log(f"{state}：{count} 个不同对象 | 请求 {c.requests} 次 | 估算花费 ≈ ${c.billed:.4f}"
        + (f" | 已写入 {a.out}" if a.out else ""))
    if c.checkpoint:
        log(f"原始响应检查点：{c.checkpoint}")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    a = build_parser().parse_args(argv)
    c = None
    result = None
    exit_code = 0
    try:
        tz, checkpoint = validate_args(a)
        log(f"目标条数估算约 ${estimate_units(a) * UNIT_PRICE:.4f}；整次任务预算 ${a.budget_usd}。"
            "整页、过滤和重试可能增加消耗；不是实际账单")
        c = Client(load_key(a.env_file), a.budget_usd, checkpoint)
        result, queries = a.fn(c, a, tz)
    except (Fatal, OSError) as e:
        message = str(e).replace(c.key, "[REDACTED]") if c else str(e)
        log(f"错误：{message}")
        exit_code = 2 if isinstance(e, BudgetExceeded) else 1
        if c:
            c.partial(message)
    except KeyboardInterrupt:
        exit_code = 130
        if c:
            c.partial("用户中断")
    if c is not None:
        try:
            finish(c, a, tz, result)
        except (Fatal, OSError, ValueError, TypeError) as e:
            log(f"保存结果失败：{str(e).replace(c.key, '[REDACTED]')} | 请求 {c.requests} 次 | 估算花费 ≈ ${c.billed:.4f}")
            if c.checkpoint:
                log(f"已获取的原始响应请检查：{c.checkpoint}")
            return 1
        if not c.complete and exit_code == 0:
            exit_code = 2
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
