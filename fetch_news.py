#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
短线热门股 Skill - 新闻热点爬虫
抓取最近 3～5 天国内/国际财经热点，多源并行，结果按时间与来源权重排序。
数据来源见 .cursor/skills/short-term-hot-stock-discovery/SKILL.md
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# 请求超时（秒），单源过久则跳过
FETCH_TIMEOUT = 12
# 最近 N 天
DAYS_RANGE = 5
# 最大并行源数
MAX_WORKERS = 10
# 通用请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    date: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _parse_cn_date(s: str) -> datetime | None:
    """解析常见中文日期格式，返回 naive UTC+8 datetime。"""
    if not s or not s.strip():
        return None
    s = s.strip().replace(" ", "").replace("年", "-").replace("月", "-").replace("日", " ").strip()
    # 仅日期
    for fmt in ("%Y-%m-%d", "%Y-%m-%d%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s[: max(len(s), 19)], fmt)
            if dt.year < 2000:
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    # 相对时间
    if "分钟" in s or "小时" in s:
        return datetime.now()
    return None


def _in_range(dt: datetime | None, cutoff: datetime) -> bool:
    if dt is None:
        return True
    if dt.tzinfo:
        cutoff = cutoff.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt >= cutoff


def fetch_cls(session: requests.Session) -> list[NewsItem]:
    """财联社 - 快讯列表页或 API。"""
    out: list[NewsItem] = []
    try:
        url = "https://www.cls.cn/telegraph"
        r = session.get(url, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        text = r.text
        # 页面常通过 JS 注入数据，尝试从 script 里抽电报列表
        m = re.search(r'"telegraphs":\s*(\[.*?\])\s*[,}]', text, re.DOTALL)
        if m:
            raw = json.loads(m.group(1))
            for i, item in enumerate(raw[:40]):
                if not isinstance(item, dict):
                    continue
                title = (item.get("title") or item.get("content") or "")[:200]
                if not title:
                    continue
                ctime = item.get("ctime") or item.get("create_time")
                if ctime:
                    try:
                        dt = datetime.fromtimestamp(int(ctime), tz=timezone(timedelta(hours=8)))
                        date_str = dt.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        date_str = ""
                else:
                    date_str = ""
                out.append(
                    NewsItem(
                        title=title,
                        url=urljoin(url, item.get("url") or ""),
                        source="财联社",
                        date=date_str,
                        summary=title[:80] + ("..." if len(title) > 80 else ""),
                    )
                )
        if not out:
            soup = BeautifulSoup(text, "lxml")
            for a in soup.select("a[href*='telegraph'], a[href*='detail']")[:25]:
                t = (a.get_text() or "").strip()
                if len(t) > 5 and "财联社" not in t:
                    href = a.get("href") or ""
                    out.append(
                        NewsItem(
                            title=t[:200],
                            url=urljoin(url, href),
                            source="财联社",
                            date="",
                            summary=t[:80],
                        )
                    )
    except Exception as e:
        sys.stderr.write(f"[财联社] 获取失败: {e}\n")
    return out


def fetch_eastmoney(session: requests.Session) -> list[NewsItem]:
    """东方财富 - 滚动/快讯。"""
    out: list[NewsItem] = []
    try:
        url = "https://np-eastmoney.eastmoney.com/api/content/roll/get"
        params = {"client": "web", "biz": "web_roll_news", "page_size": 30}
        r = session.get(url, params=params, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            return out
        items = data.get("data", {}).get("list") or data.get("list") or []
        for item in items[:30]:
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or item.get("Title") or "").strip()
            if not title:
                continue
            digest = (item.get("digest") or item.get("showTime") or "").strip()
            show_time = item.get("showTime") or item.get("show_time") or ""
            out.append(
                NewsItem(
                    title=title[:200],
                    url=(item.get("url") or item.get("url_w") or "").strip() or url,
                    source="东方财富",
                    date=str(show_time)[:19] if show_time else "",
                    summary=(digest or title)[:80],
                )
            )
    except Exception as e:
        sys.stderr.write(f"[东方财富] API 失败: {e}\n")
    if not out:
        try:
            url = "https://roll.eastmoney.com/stock,finance,money,forex,fund_3.html"
            r = session.get(url, timeout=FETCH_TIMEOUT)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a[href*='eastmoney.com'][href*='.html']")[:25]:
                t = (a.get_text() or "").strip()
                if 5 < len(t) < 120:
                    out.append(
                        NewsItem(
                            title=t[:200],
                            url=a.get("href", ""),
                            source="东方财富",
                            date="",
                            summary=t[:80],
                        )
                    )
        except Exception as e2:
            sys.stderr.write(f"[东方财富] 列表页失败: {e2}\n")
    return out


def fetch_sina(session: requests.Session) -> list[NewsItem]:
    """新浪财经 - 滚动/财经。"""
    out: list[NewsItem] = []
    try:
        url = "https://feed.mix.sina.com.cn/api/roll/get"
        params = {"pageid": "153", "lid": "2509", "k": "", "num": 25, "page": 1}
        r = session.get(url, params=params, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        items = (data.get("result", {}) or {}).get("data", {}).get("feed") or []
        for item in items[:25]:
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            if not title:
                continue
            out.append(
                NewsItem(
                    title=title[:200],
                    url=(item.get("url") or item.get("link") or "").strip(),
                    source="新浪财经",
                    date=(item.get("ctime") or "")[:19],
                    summary=(item.get("summary") or title)[:80],
                )
            )
    except Exception as e:
        sys.stderr.write(f"[新浪财经] 获取失败: {e}\n")
    return out


def fetch_xinhua(session: requests.Session) -> list[NewsItem]:
    """新华网财经。"""
    out: list[NewsItem] = []
    try:
        url = "http://www.news.cn/fortune/"
        r = session.get(url, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a[href*='news.cn'][href*='.html']")[:25]:
            t = (a.get_text() or "").strip()
            if 8 < len(t) < 100:
                out.append(
                    NewsItem(
                        title=t[:200],
                        url=urljoin(url, a.get("href", "")),
                        source="新华网财经",
                        date="",
                        summary=t[:80],
                    )
                )
    except Exception as e:
        sys.stderr.write(f"[新华网财经] 获取失败: {e}\n")
    return out


def fetch_people(session: requests.Session) -> list[NewsItem]:
    """人民网财经。"""
    out: list[NewsItem] = []
    try:
        url = "http://finance.people.com.cn/"
        r = session.get(url, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a[href*='people.com.cn'][href*='.html']")[:25]:
            t = (a.get_text() or "").strip()
            if 8 < len(t) < 100:
                out.append(
                    NewsItem(
                        title=t[:200],
                        url=urljoin(url, a.get("href", "")),
                        source="人民网财经",
                        date="",
                        summary=t[:80],
                    )
                )
    except Exception as e:
        sys.stderr.write(f"[人民网财经] 获取失败: {e}\n")
    return out


def fetch_stcn(session: requests.Session) -> list[NewsItem]:
    """证券时报。"""
    out: list[NewsItem] = []
    try:
        url = "https://www.stcn.com/"
        r = session.get(url, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a[href*='stcn.com'][href*='.html']")[:25]:
            t = (a.get_text() or "").strip()
            if 8 < len(t) < 100:
                out.append(
                    NewsItem(
                        title=t[:200],
                        url=urljoin(url, a.get("href", "")),
                        source="证券时报",
                        date="",
                        summary=t[:80],
                    )
                )
    except Exception as e:
        sys.stderr.write(f"[证券时报] 获取失败: {e}\n")
    return out


def fetch_yicai(session: requests.Session) -> list[NewsItem]:
    """第一财经。"""
    out: list[NewsItem] = []
    try:
        url = "https://www.yicai.com/news/"
        r = session.get(url, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a[href*='yicai.com'][href*='/news/']")[:25]:
            t = (a.get_text() or "").strip()
            if 8 < len(t) < 100:
                out.append(
                    NewsItem(
                        title=t[:200],
                        url=urljoin(url, a.get("href", "")),
                        source="第一财经",
                        date="",
                        summary=t[:80],
                    )
                )
    except Exception as e:
        sys.stderr.write(f"[第一财经] 获取失败: {e}\n")
    return out


def fetch_10jqka(session: requests.Session) -> list[NewsItem]:
    """同花顺要闻。"""
    out: list[NewsItem] = []
    try:
        url = "https://news.10jqka.com.cn/today/"
        r = session.get(url, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a[href*='10jqka.com.cn'][href*='.shtml']")[:25]:
            t = (a.get_text() or "").strip()
            if 8 < len(t) < 100:
                out.append(
                    NewsItem(
                        title=t[:200],
                        url=urljoin(url, a.get("href", "")),
                        source="同花顺要闻",
                        date="",
                        summary=t[:80],
                    )
                )
    except Exception as e:
        sys.stderr.write(f"[同花顺要闻] 获取失败: {e}\n")
    return out


def fetch_jin10(session: requests.Session) -> list[NewsItem]:
    """金十数据 - 快讯/财经。"""
    out: list[NewsItem] = []
    try:
        url = "https://www.jin10.com/"
        r = session.get(url, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a[href*='jin10.com'][href*='.html']")[:20]:
            t = (a.get_text() or "").strip()
            if 5 < len(t) < 120:
                out.append(
                    NewsItem(
                        title=t[:200],
                        url=urljoin(url, a.get("href", "")),
                        source="金十数据",
                        date="",
                        summary=t[:80],
                    )
                )
    except Exception as e:
        sys.stderr.write(f"[金十数据] 获取失败: {e}\n")
    return out


# 来源权重（央媒/头部优先，用于排序）
SOURCE_WEIGHT = {
    "新华网财经": 3,
    "人民网财经": 3,
    "证券时报": 2,
    "第一财经": 2,
    "财联社": 2,
    "东方财富": 2,
    "新浪财经": 1,
    "同花顺要闻": 1,
    "金十数据": 2,
}


def fetch_all(days: int = DAYS_RANGE) -> list[NewsItem]:
    """并行抓取所有源，过滤最近 days 天，按时间+来源权重排序。"""
    cutoff = datetime.now() - timedelta(days=days)
    sources = [
        fetch_cls,
        fetch_eastmoney,
        fetch_sina,
        fetch_xinhua,
        fetch_people,
        fetch_stcn,
        fetch_yicai,
        fetch_10jqka,
        fetch_jin10,
    ]
    all_items: list[NewsItem] = []
    session = _session()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(f, session): f.__name__ for f in sources}
        for fut in as_completed(futures):
            try:
                items = fut.result()
                for it in items:
                    dt = _parse_cn_date(it.date) if it.date else None
                    if _in_range(dt, cutoff):
                        all_items.append(it)
            except Exception as e:
                sys.stderr.write(f"[{futures.get(fut, '?')}] 异常: {e}\n")

    # 去重（同标题或同 URL 保留一条）
    seen_url: set[str] = set()
    seen_title: set[str] = set()
    unique: list[NewsItem] = []
    for it in all_items:
        u = (it.url or "").strip()
        t = (it.title or "").strip()
        if u and u in seen_url:
            continue
        if t and t in seen_title:
            continue
        if u:
            seen_url.add(u)
        if t:
            seen_title.add(t)
        unique.append(it)

    # 排序：先按日期（有日期的靠前），再按来源权重，再按标题稳定序
    def key(x: NewsItem) -> tuple:
        dt = _parse_cn_date(x.date) if x.date else datetime.min
        ts = dt.timestamp() if dt else 0
        w = SOURCE_WEIGHT.get(x.source, 0)
        return (-ts, -w, x.title or "")

    unique.sort(key=key)
    return unique[:35]


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else DAYS_RANGE
    items = fetch_all(days=days)
    out = [x.to_dict() for x in items]
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
