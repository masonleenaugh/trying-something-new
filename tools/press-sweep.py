#!/usr/bin/env python3
"""
Sweep for press mentions and print a deduped, most-recent-first candidate list.

Uses two free, key-less sources because neither alone is sufficient:

  Google News RSS  broad recall, good on mainstream outlets
  GDELT DOC 2.0    thinner per-name, but indexes trade press Google misses

Measured on this subject: Google News returned 11 stories, GDELT returned 1
-- but GDELT's one (Roll Call) was absent from Google News. Union beats
either.

Neither can tell a Reuters exclusive from a site that scraped it, so this
prints CANDIDATES for a human to approve. It never edits the page.

    python3 tools/press-sweep.py
    python3 tools/press-sweep.py --json candidates.json

Note: GDELT's HTTPS endpoint frequently fails to complete a TLS handshake,
so this tries HTTPS and falls back to HTTP. The query is a public figure's
name against a public research API -- no credentials, nothing personal.
"""

import argparse, json, re, sys, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

NAME = "Mason Lynaugh"
EXTRA_QUERIES = ['"Lynaugh" "Stand With Crypto"']

UA = {"User-Agent": "Mozilla/5.0 (press-sweep)"}

# Mirrors and aggregators: real URLs, but not independent coverage.
AGGREGATORS = {
    "investing.com", "aol.com", "yahoo.com", "money.usnews.com", "msn.com",
    "biggo.com", "cryptorank.io", "tradingview.com", "listennotes.com",
    "blockonomi.com", "thecoinrepublic.com", "cryptotimes.io", "coin-turk.com",
    "wtvbam.com", "wkzo.com", "ibtimes.com", "coindesk.cc",
}
OWNED = {"standwithcrypto.org"}

# Name collisions and unrelated listings that match the string but not the person.
NOISE = ("ticketnews.com", "eclipse festival")


def get(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def google_news(query, after=None, before=None):
    """One call returns at most ~10 items, so callers window the query.

    Without after/before you only ever see the most recent handful, which is
    how a whole year of earlier coverage stays invisible.
    """
    if after:
        query += " after:%s" % after
    if before:
        query += " before:%s" % before
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query) + "&hl=en-US&gl=US&ceid=US:en")
    try:
        xml = get(url)
    except Exception as e:
        print("  ! google news: %s" % e, file=sys.stderr)
        return []
    out = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def tag(t):
            m = re.search(r"<%s[^>]*>(.*?)</%s>" % (t, t), block, re.S)
            return re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1)).strip() if m else ""
        pub = tag("pubDate")
        try:
            when = parsedate_to_datetime(pub).strftime("%Y%m%d")
        except Exception:
            when = ""
        link = tag("link")
        out.append({
            "date": when,
            "title": re.sub(r"\s+-\s+[^-]+$", "", tag("title")),  # trim " - Outlet"
            "outlet": tag("source"),
            "url": link,
            "via": "google",
        })
    return out


def gdelt(query, since):
    params = {
        "query": query, "mode": "ArtList", "maxrecords": "250",
        "format": "json", "sort": "DateDesc",
        "startdatetime": since.strftime("%Y%m%d000000"),
        "enddatetime": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
    }
    qs = urllib.parse.urlencode(params)
    for scheme in ("https", "http"):          # https often hangs; http works
        try:
            raw = get("%s://api.gdeltproject.org/api/v2/doc/doc?%s" % (scheme, qs), timeout=30)
            if raw.strip().startswith("{"):
                arts = json.loads(raw).get("articles", [])
                return [{
                    "date": a.get("seendate", "")[:8],
                    "title": a.get("title", "").strip(),
                    "outlet": a.get("domain", ""),
                    "url": a.get("url", ""),
                    "via": "gdelt",
                } for a in arts]
        except Exception as e:
            print("  ~ gdelt over %s: %s" % (scheme, e), file=sys.stderr)
    return []


def domain_of(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def norm(t):
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    return " ".join(t.split()[:8])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--since", default="2025-01-01",
                    help="ignore anything published before this date (YYYY-MM-DD)")
    args = ap.parse_args()

    since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    floor = since.strftime("%Y%m%d")

    items = []

    # walk quarterly windows so older coverage is not buried by the cap
    win = since
    now = datetime.now(timezone.utc)
    while win < now:
        nxt = win + timedelta(days=92)
        a, b = win.strftime("%Y-%m-%d"), min(nxt, now).strftime("%Y-%m-%d")
        print("google news: %s .. %s" % (a, b), file=sys.stderr)
        for q in ['"%s"' % NAME] + EXTRA_QUERIES:
            items += google_news(q, after=a, before=b)
        win = nxt

    print("google news: unwindowed", file=sys.stderr)
    items += google_news('"%s"' % NAME)
    print("gdelt: \"%s\" since %s" % (NAME, args.since), file=sys.stderr)
    items += gdelt('"%s"' % NAME, since)
    for q in EXTRA_QUERIES:
        print("gdelt: %s" % q, file=sys.stderr)
        items += gdelt(q, since)

    # Google News ignores the window, so enforce the floor here. Undated rows
    # are kept rather than silently dropped.
    before = len(items)
    items = [i for i in items if not i["date"] or i["date"] >= floor]
    if before != len(items):
        print("dropped %d published before %s" % (before - len(items), args.since), file=sys.stderr)

    items = [i for i in items
             if not any(n in (i["url"] + " " + i["title"]).lower() for n in NOISE)]

    # one bucket per story, so wire copies collapse
    groups = defaultdict(list)
    for it in items:
        if it["title"]:
            groups[norm(it["title"])].append(it)

    rows = []
    for members in groups.values():
        def rank(a):
            d = domain_of(a["url"]) or a["outlet"].lower()
            return (any(x in d for x in AGGREGATORS), any(x in d for x in OWNED))
        members.sort(key=rank)
        lead = members[0]
        d = domain_of(lead["url"]) or lead["outlet"].lower()
        rows.append({
            "date": lead["date"],
            "title": lead["title"],
            "outlet": lead["outlet"] or d,
            "url": lead["url"],
            "copies": len(members),
            "via": "+".join(sorted({m["via"] for m in members})),
            "flag": "aggregator" if any(x in d for x in AGGREGATORS)
                    else ("owned" if any(x in d for x in OWNED) else ""),
        })

    rows.sort(key=lambda r: r["date"], reverse=True)

    print("\n%-12s %-24s %-8s %s" % ("DATE", "OUTLET", "SOURCE", "TITLE"))
    print("-" * 112)
    for r in rows:
        d = r["date"]
        d = "%s-%s-%s" % (d[:4], d[4:6], d[6:8]) if len(d) == 8 else (d or "?")
        flag = "  [%s]" % r["flag"] if r["flag"] else ""
        print("%-12s %-24s %-8s %s%s" % (d, r["outlet"][:24], r["via"], r["title"][:56], flag))

    print("\n%d distinct stories from %d raw hits" % (len(rows), len(items)), file=sys.stderr)

    if args.json:
        json.dump(rows, open(args.json, "w"), indent=2)
        print("wrote %s" % args.json, file=sys.stderr)


if __name__ == "__main__":
    main()
