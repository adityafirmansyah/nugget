#!/usr/bin/env python3
"""Job 3 - market sentiment research, with a citation gate.

Reliability review item 5. An LLM asked to write market commentary will
confabulate a coherent narrative connecting whatever already happened to
whatever it said. "Don't hallucinate" in a system prompt is not a control.

So the structure is: the LLM PROPOSES, a non-LLM validator COMMITS.

  * fetch_sources()  - pull real articles from reachable feeds
  * The agent reads them and returns a call plus CLAIMS, each of which must
    carry a source URL from that fetched set.
  * validate_citations() - rejects the output if any claim has no traceable
    source, or cites a URL that was not in the fetched set.
  * Only then is the call recorded - as its OWN prediction row with
    signal_source='sentiment'.

Sentiment is graded SEPARATELY, never fused into the technical score. It earns
its place in an ensemble only after it has its own independent track record.
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_market as fm          # noqa: E402
import ledger                      # noqa: E402
import predictor                   # noqa: E402
import stats                       # noqa: E402

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))
JOB = "sentiment"

# Keyless feeds. Order matters: earlier = preferred. Unreachable ones are
# skipped and reported, never silently dropped.
FEEDS = [
    ("google-news-gold",
     "https://news.google.com/rss/search?q=gold+price+XAUUSD+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("google-news-fed",
     "https://news.google.com/rss/search?q=federal+reserve+rates+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("google-news-inflation",
     "https://news.google.com/rss/search?q=inflation+CPI+treasury+yields+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("cnbc-markets", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("wsj-markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("investing-commodities", "https://www.investing.com/rss/news_11.rss"),
    ("yahoo-finance", "https://finance.yahoo.com/news/rssindex"),
]

MAX_ITEMS_PER_FEED = 8


# ------------------------------------------------------------------ fetching

def _parse_rss(xml_text: str, source: str) -> list[dict]:
    """Minimal, dependency-free RSS/Atom parse. Raises SchemaError on garbage."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise fm.SchemaError(f"{source}: unparseable XML ({e})")

    items = []
    for tag in (".//item", ".//{http://www.w3.org/2005/Atom}entry"):
        for it in root.findall(tag):
            def txt(name):
                el = it.find(name)
                if el is None:
                    el = it.find(f"{{http://www.w3.org/2005/Atom}}{name}")
                return (el.text or "").strip() if el is not None and el.text else ""

            link = txt("link")
            if not link:
                le = it.find("{http://www.w3.org/2005/Atom}link")
                if le is not None:
                    link = le.get("href", "")
            title = txt("title")
            if not title or not link:
                continue
            items.append({
                "source": source, "title": title, "link": link,
                "summary": re.sub(r"<[^>]+>", " ", txt("description") or txt("summary"))[:400],
                "published": txt("pubDate") or txt("updated"),
            })
            if len(items) >= MAX_ITEMS_PER_FEED:
                break
        if items:
            break
    return items


def fetch_sources() -> dict:
    """Pull every reachable feed. Returns items + a per-feed status map."""
    items, status = [], {}
    for name, url in FEEDS:
        host = url.split("/")[2]
        if not fm.circuit_allows(host):
            status[name] = "circuit_open"
            continue
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": fm.UA})
            with urllib.request.urlopen(req, timeout=fm.TIMEOUT_S) as r:
                body = r.read().decode("utf-8", "replace")
            got = _parse_rss(body, name)
            if not got:
                raise fm.SchemaError(f"{name}: feed parsed but contained 0 items")
            fm.circuit_record(host, True)
            items.extend(got)
            status[name] = f"ok ({len(got)})"
        except fm.SchemaError as e:
            fm.circuit_record(host, False)
            status[name] = f"SchemaError: {e}"
        except Exception as e:
            fm.circuit_record(host, False)
            status[name] = f"{type(e).__name__}: {e}"
    return {"items": items, "status": status}


# ------------------------------------------------------------ citation gate

_URL_RE = re.compile(r"https?://[^\s\)\]\>,]+")


def validate_citations(payload: dict, sources: list[dict]) -> dict:
    """Every claim must cite a URL that was actually fetched.

    Rejects the whole output if any claim lacks a traceable source, or cites a
    URL outside the fetched set. This is what makes confabulation unable to
    reach the ledger - not a prompt instruction.
    """
    valid = {s["link"] for s in sources}
    valid_norm = {u.rstrip("/").split("?")[0] for u in valid}

    claims = payload.get("claims")
    if not isinstance(claims, list) or not claims:
        return {"ok": False, "reason": "no claims supplied", "uncited": [],
                "invalid_urls": []}

    uncited, invalid = [], []
    for i, c in enumerate(claims):
        if not isinstance(c, dict):
            uncited.append(f"claim[{i}] not an object")
            continue
        text = (c.get("text") or "").strip()
        url = (c.get("source_url") or "").strip()
        if not text:
            uncited.append(f"claim[{i}] has no text")
            continue
        if not url:
            uncited.append(f"claim[{i}] has no source_url")
            continue
        norm = url.rstrip("/").split("?")[0]
        if url not in valid and norm not in valid_norm:
            invalid.append(url)

    ok = not uncited and not invalid
    return {"ok": ok, "uncited": uncited, "invalid_urls": invalid,
            "claims": len(claims), "sources_available": len(valid)}


def record_sentiment(con, payload: dict, sources: list[dict], *, horizon: str = "1h",
                     target: datetime | None = None, ref_price: float | None = None,
                     slot: datetime | None = None) -> dict:
    """Validate, then record the sentiment call as its own graded row."""
    g = validate_citations(payload, sources)
    if not g["ok"]:
        return {"action": "rejected", "grounding": g,
                "reason": "uncited or invalid sources; not recorded"}

    direction = payload.get("direction")
    if direction not in ("bullish", "bearish", "no_call"):
        return {"action": "rejected",
                "reason": f"invalid direction {direction!r}"}

    now = datetime.now(UTC)
    slot = slot or now.replace(minute=0, second=0, microsecond=0)
    target = target or (slot + timedelta(hours=1))

    pid = ledger.record_prediction(
        con, horizon=horizon,
        target_bar_utc=target.isoformat(timespec="seconds"),
        scheduled_slot_utc=slot.isoformat(timespec="seconds"),
        direction=direction,
        confidence=float(payload.get("confidence") or 0.0),
        ref_price=float(ref_price or 0.0),
        model_version="sentiment-v0.1",
        params={"method": "llm-cited"},
        features={"claims": len(payload.get("claims", []))},
        inputs={"claims": payload.get("claims"),
                "sources": [s["link"] for s in sources][:20],
                "feed_status": payload.get("feed_status")},
        signal_source="sentiment",
        noise_threshold=0.0,   # sentiment calls graded on raw direction
        notes=f"cited {len(payload.get('claims', []))} claims")
    return {"action": "recorded", "prediction_id": pid, "direction": direction,
            "grounding": g}


def run(now: datetime | None = None, *, payload: dict | None = None,
        dry_run: bool = False) -> dict:
    """Gather sources; if a payload is supplied, validate and record it."""
    now = now or datetime.now(UTC)
    slot = now.replace(minute=0, second=0, microsecond=0)
    slot_iso = slot.isoformat(timespec="seconds")
    con = ledger.connect()

    if not dry_run and not ledger.claim_slot(con, JOB, slot_iso):
        return {"action": "duplicate_slot_rejected", "slot_utc": slot_iso}

    fs = fetch_sources()
    out = {"slot_utc": slot_iso, "sources": len(fs["items"]),
           "feed_status": fs["status"]}

    if not fs["items"]:
        if not dry_run:
            ledger.record_fetch_error(con, "sentiment", "no feed reachable")
            ledger.finish_slot(con, JOB, slot_iso, "failed", "no feed reachable")
        out["action"] = "no_sources"
        return out

    # reference price for grading
    try:
        ref = fm.fetch_spot()["price"]
    except (fm.FetchError, fm.SchemaError):
        ref = None

    if payload is None:
        out["action"] = "sources_ready"
        out["evidence"] = {
            "items": fs["items"][:20],
            "feed_status": fs["status"],
            "spot": ref,
            "now_wib": now.astimezone(WIB).isoformat(timespec="seconds"),
        }
        if not dry_run:
            ledger.finish_slot(con, JOB, slot_iso, "ok", "sources_ready")
        return out

    if ref is None:
        out["action"] = "no_reference_price"
        return out

    res = record_sentiment(con, payload, fs["items"], target=slot + timedelta(hours=1),
                           ref_price=ref, slot=slot)
    out.update(res)
    if not dry_run:
        ledger.finish_slot(con, JOB, slot_iso,
                           "ok" if res["action"] == "recorded" else "failed",
                           res["action"])
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--payload", help="JSON file with the agent's proposed call")
    a = ap.parse_args()
    p = json.loads(Path(a.payload).read_text()) if a.payload else None
    print(json.dumps(run(payload=p, dry_run=a.dry_run), indent=2, default=str))