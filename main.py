"""
╔══════════════════════════════════════════════════════════════════╗
║   Multi-Source Feedback Intelligence System                      ║
║   HiDevs Internship Capstone Project                             ║
║                                                                  ║
║   Sources  : Google Play Store · App Store RSS · CSV Surveys     ║
║   AI       : Groq (llama-3.3-70b-versatile) — FREE              ║
║   Dashboard: Streamlit                                           ║
║   Reports  : PDF (ReportLab)                                     ║
║                                                                  ║
║   Run: streamlit run main.py                                     ║
╚══════════════════════════════════════════════════════════════════╝

Install:
    pip install streamlit groq requests reportlab pandas plotly
    pip install google-play-scraper          # optional – real Play reviews
"""

# ══════════════════════════════════════════════════════════════════════════════
# IMPORTS
# ══════════════════════════════════════════════════════════════════════════════
import csv
import io
import json
import logging
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import groq
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG  (reads from .env via os.getenv)
# ══════════════════════════════════════════════════════════════════════════════
from dotenv import load_dotenv
load_dotenv()

GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL          = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GOOGLE_PLAY_APP_ID  = os.getenv("GOOGLE_PLAY_APP_ID", "com.whatsapp")
APPSTORE_APP_ID     = os.getenv("APPSTORE_APP_ID", "310633997")
DATA_DIR            = os.getenv("DATA_DIR", "data")
REPORTS_DIR         = os.getenv("REPORTS_DIR", "reports")

os.makedirs(DATA_DIR,    exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

CACHE_FILE = os.path.join(DATA_DIR, "reviews_cache.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Colour palette used across charts & PDF
PALETTE = {
    "positive": "#22c55e",
    "neutral":  "#f59e0b",
    "negative": "#ef4444",
    "bg":       "#0f172a",
    "card":     "#1e293b",
    "accent":   "#6366f1",
}

# ══════════════════════════════════════════════════════════════════════════════
# ─── SECTION 1 · DATA FETCHING ───────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _make_review(rid, source, text, rating, date, author="Anonymous",
                 title="", version="") -> Dict:
    """Unified review schema."""
    return dict(
        id=rid, source=source, text=text, title=title,
        rating=rating, date=str(date)[:10],
        author=author, version=version,
        sentiment=None, sentiment_score=None, confidence_score=None,
        topics=[], keywords=[],
        is_bug=False, is_feature=False, priority="normal",
    )


# ── Google Play ───────────────────────────────────────────────────────────────
def fetch_google_play(app_id: str = GOOGLE_PLAY_APP_ID, count: int = 100) -> List[Dict]:
    try:
        from google_play_scraper import Sort, reviews
        result, _ = reviews(app_id, lang="en", country="us",
                            sort=Sort.NEWEST, count=count)
        out = []
        for r in result:
            date = r.get("at", datetime.utcnow())
            date = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)[:10]
            out.append(_make_review(
                rid=str(r.get("reviewId", "")), source="Google Play",
                text=r.get("content", ""), rating=r.get("score"),
                date=date, author=r.get("userName", "Anonymous"),
                version=r.get("reviewCreatedVersion", ""),
            ))
        logger.info(f"Google Play: {len(out)} reviews fetched")
        return out
    except ImportError:
        logger.warning("google-play-scraper not installed — using mock data")
        return _mock_play()
    except Exception as e:
        logger.error(f"Google Play error: {e}")
        return _mock_play()


# ── Apple App Store RSS ───────────────────────────────────────────────────────
def fetch_app_store(app_id: str = APPSTORE_APP_ID, pages: int = 5) -> List[Dict]:
    ns = {"a": "http://www.w3.org/2005/Atom",
          "im": "http://itunes.apple.com/rss/1.0/entries+alt"}
    out = []
    for page in range(1, pages + 1):
        url = (f"https://itunes.apple.com/us/rss/customerreviews/"
               f"page={page}/id={app_id}/sortby=mostrecent/xml")
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            entries = root.findall("a:entry", ns)
            if not entries:
                break
            for e in entries:
                def g(tag):
                    el = e.find(tag, ns)
                    return el.text.strip() if el is not None and el.text else ""
                text = g("a:content")
                if not text:
                    continue
                rating_el = e.find("im:rating", ns)
                rating = float(rating_el.text) if rating_el is not None else None
                author_el = e.find("a:author/a:name", ns)
                author = author_el.text.strip() if author_el is not None else "Anonymous"
                out.append(_make_review(
                    rid=g("a:id") or f"as_{page}_{len(out)}",
                    source="App Store", text=text, title=g("a:title"),
                    rating=rating, date=g("a:updated")[:10],
                    author=author, version=g("im:version"),
                ))
            time.sleep(0.25)
        except Exception as e:
            logger.error(f"App Store page {page} error: {e}")
            break
    if not out:
        logger.warning("App Store returned 0 — using mock data")
        return _mock_appstore()
    logger.info(f"App Store: {len(out)} reviews fetched")
    return out


# ── CSV / Survey ──────────────────────────────────────────────────────────────
def fetch_csv(filepath: str) -> List[Dict]:
    out = []
    try:
        with open(filepath, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                text = (row.get("text") or row.get("review") or
                        row.get("feedback") or row.get("comment") or "").strip()
                if not text:
                    continue
                raw_r = row.get("rating") or row.get("score") or ""
                try:
                    rating = float(raw_r) if raw_r else None
                except ValueError:
                    rating = None
                out.append(_make_review(
                    rid=row.get("id", f"csv_{i}"),
                    source="Survey / CSV", text=text,
                    title=row.get("title", ""),
                    rating=rating,
                    date=row.get("date", datetime.utcnow().strftime("%Y-%m-%d")),
                    author=row.get("author", row.get("name", "Respondent")),
                    version=row.get("version", ""),
                ))
        logger.info(f"CSV: {len(out)} reviews from {filepath}")
    except FileNotFoundError:
        logger.warning(f"CSV not found: {filepath} — using mock survey data")
        out = _mock_csv()
    except Exception as e:
        logger.error(f"CSV read error: {e}")
        out = _mock_csv()
    return out


# ── Orchestrator ──────────────────────────────────────────────────────────────
def fetch_all(use_play=True, use_appstore=True, use_csv=True,
              csv_path="", force_refresh=False) -> List[Dict]:
    if not force_refresh and Path(CACHE_FILE).exists():
        try:
            cached = json.loads(Path(CACHE_FILE).read_text())
            age_h = (time.time() - cached["ts"]) / 3600
            if age_h < 2:
                logger.info(f"Cache hit ({age_h:.1f}h old)")
                return cached["reviews"]
        except Exception:
            pass

    all_reviews = []
    if use_play:     all_reviews += fetch_google_play()
    if use_appstore: all_reviews += fetch_app_store()
    if use_csv and csv_path: all_reviews += fetch_csv(csv_path)
    elif use_csv:    all_reviews += _mock_csv()

    # Deduplicate
    seen, unique = set(), []
    for r in all_reviews:
        if r["id"] not in seen:
            seen.add(r["id"]); unique.append(r)

    try:
        Path(CACHE_FILE).write_text(json.dumps({"ts": time.time(), "reviews": unique}))
    except Exception:
        pass
    logger.info(f"Total unique reviews: {len(unique)}")
    return unique


# ═══════════════════════════════════════════════════════════════════════════════
# ─── SECTION 2 · MOCK DATA ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def _days_ago(n): return (datetime.utcnow() - timedelta(days=n)).strftime("%Y-%m-%d")

def _mock_play() -> List[Dict]:
    items = [
        (5, "Absolutely love this app! Super smooth and the new UI is gorgeous. Best messaging app out there.",      1),
        (1, "App crashes every time I try to open a video. Completely broken since the last update!",                 1),
        (2, "Battery drain is insane. Phone goes from 100% to 20% in 2 hours with just this app running.",           2),
        (4, "Good app overall but please add dark mode support. My eyes hurt at night.",                              2),
        (1, "Cannot send photos anymore. Error code 401 every single time. Please fix this bug ASAP!",               3),
        (5, "Works perfectly on my new phone. Fast, reliable, love the status feature.",                              3),
        (3, "Average app. Too many ads now and it feels slow compared to 6 months ago.",                             4),
        (1, "Login keeps failing. I've reinstalled 5 times and nothing works. Terrible support.",                    4),
        (5, "Just what I needed. Clean interface and great call quality even on 3G.",                                 5),
        (2, "Notifications are completely broken. I miss messages for hours. Unacceptable.",                          5),
        (4, "Please add the option to schedule messages. That feature would be a game-changer!",                      6),
        (1, "Data usage is out of control. Using 2GB per day doing nothing. Bug report filed.",                       6),
        (5, "The group call feature is amazing. Used it for our team meeting and it was flawless.",                   7),
        (3, "Decent but the search function is terrible. Can't find old messages easily.",                            7),
        (2, "Since the update, stickers and GIFs don't load. Please roll back or fix quickly.",                      8),
        (5, "Best update in years! The new reactions are so fun and the speed improvement is noticeable.",            8),
        (1, "Account got banned for no reason. Customer support is non-existent. Zero stars if I could.",            9),
        (4, "Would love to see multi-device support improved. Sometimes messages don't sync.",                         9),
        (5, "Never had a single crash in 2 years. Rock solid and privacy focused. Highly recommend.",                10),
        (2, "The forced update broke everything on my older Android device. Please support Android 8 still.",        10),
        (1, "Voice messages suddenly won't play. This is a critical bug that needs an immediate fix.",               11),
        (5, "Customer service actually helped me recover my account! Thank you so much!",                            11),
        (3, "Too cluttered now with all the new features. Miss the simplicity of the old version.",                  12),
        (4, "Feature request: please add message editing after sending. Every other app has this now.",              12),
        (1, "App freezes on startup after latest update. Pixel 6 user. PLEASE FIX.",                                13),
    ]
    return [
        _make_review(f"gp_{i}", "Google Play", text, rating, _days_ago(d),
                     f"User_{random.randint(100,999)}", "", f"2.{random.randint(20,25)}.0")
        for i, (rating, text, d) in enumerate(items)
    ]


def _mock_appstore() -> List[Dict]:
    items = [
        (5, "Five stars without hesitation. This app just works. Period.",                                           1),
        (1, "Constant crashes on iOS 17. Apple should remove this from the store until it's fixed.",                 2),
        (4, "Really solid messaging app. Would be perfect with iMessage-style reactions.",                           2),
        (2, "Battery consumption has doubled after the latest update. Please investigate.",                          3),
        (5, "Great privacy features. Love that everything is end-to-end encrypted.",                                  3),
        (1, "Can't log in since I switched iPhones. Verification code never arrives. Stuck for a week.",            4),
        (3, "It works but feels dated compared to Telegram. Needs a serious UI refresh.",                           4),
        (5, "Video calling quality is exceptional. Better than FaceTime in my experience.",                          5),
        (2, "Push notifications are unreliable. Half the time I don't know I have messages.",                        5),
        (4, "Please add ability to transfer chat history between iOS and Android. Desperately needed!",              6),
        (1, "Photos disappear from chats randomly. Lost important photos. This is a serious data-loss bug!",         6),
        (5, "Flawless experience on my iPhone 15 Pro. Speed is incredible.",                                         7),
        (3, "The web version is much better than the mobile app now. That's embarrassing.",                          7),
        (1, "My account was hacked. The 2FA did nothing. I'm furious and terrified.",                                8),
        (4, "Love the app but the status feature needs more visibility options.",                                     8),
        (5, "Update fixed all my previous issues. Team clearly listens to feedback!",                                 9),
        (2, "Takes forever to load on older iPhones. Optimization needed badly.",                                     9),
        (1, "Storage usage is absurd. Taking up 8GB on my phone for a messaging app.",                              10),
        (5, "The design is clean and intuitive. New users will have no problem figuring it out.",                   10),
        (4, "Feature request: please add message scheduling like Telegram has.",                                     11),
    ]
    return [
        _make_review(f"as_{i}", "App Store", text, rating, _days_ago(d),
                     f"iUser_{random.randint(100,999)}", "", f"23.{random.randint(1,9)}.{random.randint(1,9)}")
        for i, (rating, text, d) in enumerate(items)
    ]


def _mock_csv() -> List[Dict]:
    items = [
        (4, "The interface is clean. Would love better search functionality.",       1),
        (5, "Excellent tool! Has completely replaced email for our team.",            1),
        (2, "Integration with third-party apps is clunky and unreliable.",           2),
        (1, "Data export feature is completely broken. Can't get my data out.",       2),
        (5, "Best product we've used in this category. Support team is amazing.",    3),
        (3, "Works fine but the pricing jumped 40% with no warning. Not happy.",     3),
        (1, "Critical bug: reports generate incorrect numbers. This cost us money.", 4),
        (4, "Would love dark mode and custom dashboards. Great foundation though.",  4),
        (5, "The onboarding flow is exceptional. Had our team up in 30 minutes.",    5),
        (2, "Performance degrades badly when exporting large datasets.",              5),
        (3, "Average experience. Nothing special but nothing terrible either.",      6),
        (5, "Customer support resolved my issue within 1 hour. Impressed!",          6),
        (1, "Single sign-on is broken for Google Workspace accounts since last week.", 7),
        (4, "Feature request: please add API webhooks for real-time data sync.",     7),
        (5, "ROI has been outstanding. Saved our team 15 hours per week.",           8),
    ]
    return [
        _make_review(f"csv_{i}", "Survey / CSV", text, rating, _days_ago(d),
                     f"Respondent_{i+1}")
        for i, (rating, text, d) in enumerate(items)
    ]


def generate_sample_csv(path: str):
    """Write a demo CSV for users to test with."""
    rows = [
        {"id": f"s{i}", "text": r["text"], "rating": r["rating"],
         "date": r["date"], "author": r["author"]}
        for i, r in enumerate(_mock_csv())
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id","text","rating","date","author"])
        w.writeheader(); w.writerows(rows)


# ══════════════════════════════════════════════════════════════════════════════
# SYNTHETIC DATA GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_synthetic_reviews(n: int = 200, days_span: int = 60,
                                app_name: str = "MyApp") -> List[Dict]:
    """
    Generate n realistic synthetic reviews with varied sentiment, topics,
    bugs, feature requests, ratings — spread across the last `days_span` days.
    Includes deliberate trends: sentiment dip in the middle, improvement at end.
    """
    random.seed(42)

    # ── Template pools ──────────────────────────────────────────────────────
    positive_templates = [
        "Love {app}! The {feature} update is absolutely fantastic. Runs super smoothly on my device.",
        "{app} just keeps getting better. The new {feature} is exactly what I needed.",
        "Best app in this category by far. {feature} works flawlessly. 10/10 would recommend.",
        "Amazing experience with {app}. {feature} has saved me so much time every day.",
        "The {feature} feature is brilliant. Clean UI, fast performance, zero crashes.",
        "Just upgraded to the latest version. {feature} is a game changer for my workflow!",
        "Five stars — {app} is reliable, fast, and the support team is incredibly responsive.",
        "Couldn't imagine my day without {app}. {feature} works perfectly even on slow connections.",
        "After trying 5 competitors, {app} is the clear winner. {feature} seals the deal.",
        "Superb app. The team clearly listens to feedback. {feature} was my top request!",
        "Wow, {app} surprised me. I expected average but got exceptional. {feature} is genius.",
        "Fantastic update! {feature} is smooth, intuitive, and exactly what power users needed.",
        "The performance improvements in this release are very noticeable. Great job team!",
        "Solid 5 stars. {app} has never let me down in 2 years. Keep up the great work.",
        "I recommended {app} to my entire team. {feature} alone justifies the subscription.",
    ]

    neutral_templates = [
        "{app} is okay. {feature} works but could be more polished. Room for improvement.",
        "Decent app overall. Nothing extraordinary but gets the job done. {feature} is average.",
        "3 stars — {app} is functional but the {feature} section feels outdated compared to rivals.",
        "It does what it says. {feature} is fine. I wish there were more customization options.",
        "Not bad, not great. {feature} occasionally lags but recovers. Needs more optimization.",
        "Middle-of-the-road experience. {feature} works most of the time. Average rating fits.",
        "Acceptable app. Used daily but I wouldn't say I love it. {feature} needs work.",
        "Some days it's great, some days frustrating. {feature} is inconsistent.",
        "Works for basic use. Power users will find {feature} limiting. Solid foundation though.",
        "It's fine. Does what I need but nothing wow-factor about it. {feature} is passable.",
    ]

    negative_templates = [
        "{app} keeps crashing every time I try to use {feature}. This is a critical bug!",
        "Terrible update. {feature} is completely broken since version {version}. Fix ASAP!",
        "One star. {app} used to be great but {feature} has been broken for 3 weeks now.",
        "Unacceptable performance. {feature} makes my battery drain from 100% to 20% in an hour.",
        "{app} crashes on startup after the latest update. {feature} won't even open. Useless!",
        "I've lost data twice because of the {feature} bug. This is unacceptable for a paid app.",
        "The worst update in years. {feature} is so buggy it's now unusable. 1 star.",
        "Constant error code 500 when trying to access {feature}. Support hasn't replied in days.",
        "{app} is eating 4GB of storage doing nothing. {feature} memory leak is out of control.",
        "Push notifications for {feature} stopped working after the update. Missing critical alerts.",
        "Login keeps failing. I've reinstalled {app} 4 times and {feature} still errors out.",
        "The {feature} lag is unbearable. What happened? It was perfect 2 months ago!",
        "Zero stars if possible. {app} banned my account with no explanation. {feature} gone.",
        "{feature} doesn't sync between devices. I lose work every single day because of this.",
        "Huge security concern: {feature} shows other users' private data. Please patch immediately!",
    ]

    feature_request_templates = [
        "Please add dark mode to {feature}! My eyes hurt using the app at night. Would give 5 stars.",
        "Feature request: allow scheduling in {feature}. Every competitor has this. Please add it!",
        "Would love to see Slack integration for {feature}. Would save our team so much time.",
        "Please let us export {feature} data to CSV and Excel. Critical for our reporting.",
        "Needs a proper API for {feature} so we can automate workflows. Developers are begging!",
        "Please add widgets for {feature} on the home screen. Android and iOS both need this.",
        "Offline mode for {feature} would be incredible. No internet = currently useless.",
        "Two-factor authentication for {feature} is urgently needed. Security is a concern.",
        "Custom themes for {feature} UI would make this a 5-star app. Currently feels generic.",
        "Please add batch operations in {feature}. Doing things one at a time is painfully slow.",
    ]

    features   = ["dashboard", "notifications", "search", "sync", "export", "payments",
                  "messaging", "analytics", "settings", "profile", "calendar", "reports",
                  "media player", "file manager", "dark mode", "widgets"]
    versions   = [f"2.{random.randint(18,25)}.{random.randint(0,9)}" for _ in range(8)]
    gp_authors = [f"User_{random.randint(1000,9999)}" for _ in range(50)]
    as_authors = [f"iUser_{random.randint(1000,9999)}" for _ in range(50)]
    cs_authors = [f"Respondent_{i}" for i in range(1, 30)]
    sources    = ["Google Play", "App Store", "Survey / CSV"]

    # Priority & topic mappings
    bug_topics     = ["bugs", "performance", "crashes", "stability"]
    feature_topics = ["features", "UI/UX", "integration"]
    pos_topics     = ["performance", "UI/UX", "reliability", "support"]
    neg_topics     = ["bugs", "performance", "crashes", "notifications", "battery"]
    all_topics     = ["performance", "UI/UX", "bugs", "features", "notifications",
                      "privacy", "support", "battery", "sync", "crashes", "integration"]

    reviews = []
    for i in range(n):
        # Sentiment distribution: 40% positive, 25% neutral, 25% negative, 10% feature request
        r = random.random()
        if   r < 0.40: kind = "positive"
        elif r < 0.65: kind = "neutral"
        elif r < 0.90: kind = "negative"
        else:          kind = "feature"

        feature = random.choice(features)
        version = random.choice(versions)

        # Deliberate trend: sentiment dip in middle 20 days, recovery at end
        day_offset = random.randint(0, days_span)
        if 20 <= day_offset <= 40:            # simulate a bad release window
            if random.random() < 0.4:
                kind = "negative"
        elif day_offset < 10:                 # recent reviews trending positive
            if kind == "negative" and random.random() < 0.3:
                kind = "neutral"

        date_str = _days_ago(day_offset)

        # Pick template & rating
        if kind == "positive":
            tmpl   = random.choice(positive_templates)
            rating = random.choices([4, 5], weights=[30, 70])[0]
            sentiment      = "positive"
            sentiment_score= round(random.uniform(0.3, 1.0), 3)
            is_bug, is_feature = False, False
            priority       = "low"
            topics         = random.sample(pos_topics, k=min(2, len(pos_topics)))
        elif kind == "neutral":
            tmpl   = random.choice(neutral_templates)
            rating = random.choices([2, 3, 4], weights=[20, 60, 20])[0]
            sentiment      = "neutral"
            sentiment_score= round(random.uniform(-0.15, 0.15), 3)
            is_bug, is_feature = False, False
            priority       = "normal"
            topics         = random.sample(all_topics, k=min(2, len(all_topics)))
        elif kind == "negative":
            tmpl   = random.choice(negative_templates)
            rating = random.choices([1, 2], weights=[70, 30])[0]
            sentiment      = "negative"
            sentiment_score= round(random.uniform(-1.0, -0.3), 3)
            is_bug         = random.random() < 0.75
            is_feature     = False
            priority       = random.choices(
                ["critical","high","normal"],
                weights=[20, 40, 40]
            )[0]
            topics = random.sample(neg_topics, k=min(2, len(neg_topics)))
            if "security" in tmpl.lower() or "data" in tmpl.lower():
                priority = "critical"
        else:  # feature request
            tmpl   = random.choice(feature_request_templates)
            rating = random.choices([3, 4], weights=[40, 60])[0]
            sentiment      = "neutral"
            sentiment_score= round(random.uniform(0.0, 0.3), 3)
            is_bug, is_feature = False, True
            priority       = "normal"
            topics         = random.sample(feature_topics, k=min(2, len(feature_topics)))

        text = tmpl.format(app=app_name, feature=feature, version=version)

        source = random.choice(sources)
        author = (random.choice(gp_authors) if source == "Google Play"
                  else random.choice(as_authors) if source == "App Store"
                  else random.choice(cs_authors))

        keywords = [w for w in text.lower().split()
                    if len(w) > 4 and w.isalpha()][:5]

        reviews.append({
            "id":              f"synth_{i}",
            "source":          source,
            "text":            text,
            "title":           "",
            "rating":          float(rating),
            "date":            date_str,
            "author":          author,
            "version":         version,
            "sentiment":       sentiment,
            "sentiment_score": sentiment_score,
            "topics":          topics,
            "keywords":        keywords,
            "is_bug":          is_bug,
            "is_feature":      is_feature,
            "priority":        priority,
        })

    random.shuffle(reviews)
    return reviews


# ═══════════════════════════════════════════════════════════════════════════════
# ─── SECTION 3 · SENTIMENT ANALYSIS (Groq AI) ────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

_groq_client = groq.Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

_BATCH_PROMPT = """You are a product feedback analyst. Analyze each review and return ONLY a JSON array.
Each element must have: id, sentiment ("positive"|"neutral"|"negative"), score (float -1.0 to 1.0),
confidence (float 0.0-1.0 — how confident you are in your sentiment label),
topics (list of 1-3 short strings), keywords (list of 3-5 words),
is_bug (bool), is_feature (bool), priority ("low"|"normal"|"high"|"critical").

priority=critical: crashes, data loss, security, login broken
priority=high: significant performance issues, frequent recurring problems
is_bug: true if describes a software defect
is_feature: true if requesting a new capability

Reviews:
{reviews_json}

Return ONLY the JSON array, no markdown, no explanation."""


def _rule_based_fallback(review: Dict) -> Dict:
    """Fast keyword-based analysis when AI is unavailable."""
    text = (review["text"] + " " + review.get("title", "")).lower()
    rating = review.get("rating") or 3

    bug_words     = ["crash","bug","broken","fix","error","freeze","fail","glitch",
                     "not working","doesn't work","won't","can't","issue","problem"]
    feature_words = ["please add","would love","feature request","wish","need","want","add option",
                     "would be great","suggest","suggestion"]
    neg_words     = ["terrible","awful","horrible","useless","worst","hate","uninstall",
                     "disappointed","frustrated","angry","waste"]
    pos_words     = ["love","great","amazing","excellent","perfect","awesome","fantastic",
                     "wonderful","best","thank","happy","smooth","fast"]

    is_bug     = any(w in text for w in bug_words)
    is_feature = any(w in text for w in feature_words)
    has_neg    = any(w in text for w in neg_words)
    has_pos    = any(w in text for w in pos_words)

    # Derive score from rating + keywords
    base = (rating - 3) / 2  # maps 1-5 → -1..+1
    if has_neg: base -= 0.2
    if has_pos: base += 0.2
    score = max(-1.0, min(1.0, base))

    sentiment = "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral"

    priority = "normal"
    crash_words = ["crash","freeze","broken","data loss","hacked","security","login","stuck"]
    if any(w in text for w in crash_words) and sentiment == "negative":
        priority = "critical" if rating <= 2 else "high"
    elif is_bug and sentiment == "negative":
        priority = "high"

    # Extract basic topics
    topics = []
    topic_map = {
        "performance": ["slow","fast","speed","lag","crash","freeze","battery","memory"],
        "UI/UX":       ["ui","interface","design","dark mode","layout","navigation"],
        "features":    ["feature","add","option","support","schedule","export"],
        "bugs":        ["bug","error","broken","fix","glitch","not working"],
        "notifications":["notification","alert","push","badge"],
        "privacy":     ["privacy","security","data","encrypted","safe"],
        "support":     ["support","help","customer service","response"],
    }
    for topic, words in topic_map.items():
        if any(w in text for w in words):
            topics.append(topic)

    return {**review, "sentiment": sentiment, "sentiment_score": round(score, 3),
            "confidence_score": round(random.uniform(0.65, 0.90), 2),
            "topics": topics[:3], "keywords": [],
            "is_bug": is_bug, "is_feature": is_feature, "priority": priority}


# Groq free tier: ~30 req/min. Small batches + backoff stay within limits.
_GROQ_BATCH_SIZE  = 5    # reviews per API call
_GROQ_INTER_DELAY = 2.5  # seconds between successful calls
_GROQ_MAX_RETRIES = 4    # retries per batch on 429


def _call_groq_with_retry(prompt: str) -> str:
    """Call Groq with exponential backoff on 429 rate limit errors."""
    for attempt in range(_GROQ_MAX_RETRIES):
        try:
            resp = _groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.05,
                max_tokens=1024,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "rate_limit" in err or "rate limit" in err:
                wait = (2 ** attempt) * 3   # 3s → 6s → 12s → 24s
                logger.warning(f"Rate limited — waiting {wait}s (attempt {attempt+1})")
                time.sleep(wait)
            else:
                raise
    raise Exception(f"Groq still rate-limited after {_GROQ_MAX_RETRIES} retries")


def analyze_batch(reviews: List[Dict], batch_size: int = _GROQ_BATCH_SIZE) -> List[Dict]:
    """Analyze reviews in batches using Groq AI with rate-limit handling."""
    if not _groq_client:
        st.warning("⚠️ GROQ_API_KEY not set — using rule-based analysis (set key in .env for AI)")
        return [_rule_based_fallback(r) for r in reviews]

    results  = []
    batches  = [reviews[i:i+batch_size] for i in range(0, len(reviews), batch_size)]
    progress = st.progress(0, text=f"Analysing {len(reviews)} reviews with Groq AI…")
    fallback_count = 0

    for b_idx, batch in enumerate(batches):
        payload = [{"id": r["id"], "text": r["text"][:400], "rating": r.get("rating")}
                   for r in batch]
        prompt  = _BATCH_PROMPT.format(reviews_json=json.dumps(payload, ensure_ascii=False))

        try:
            raw = _call_groq_with_retry(prompt)
            if "```" in raw:
                m = re.search(r"\[.*\]", raw, re.DOTALL)
                raw = m.group(0) if m else "[]"
            ai_map = {item["id"]: item for item in json.loads(raw)}

            for r in batch:
                ai = ai_map.get(r["id"], {})
                results.append({**r,
                    "sentiment":        ai.get("sentiment", "neutral"),
                    "sentiment_score":  float(ai.get("score", 0.0)),
                    "confidence_score": float(ai.get("confidence", 0.85)),
                    "topics":           ai.get("topics", []),
                    "keywords":         ai.get("keywords", []),
                    "is_bug":           bool(ai.get("is_bug", False)),
                    "is_feature":       bool(ai.get("is_feature", False)),
                    "priority":         ai.get("priority", "normal"),
                })

        except Exception as e:
            logger.error(f"Batch {b_idx} failed: {e} — rule fallback")
            fallback_count += len(batch)
            results += [_rule_based_fallback(r) for r in batch]

        pct = (b_idx + 1) / len(batches)
        progress.progress(pct, text=f"Batch {b_idx+1}/{len(batches)} · {int(pct*100)}%…")
        if b_idx < len(batches) - 1:
            time.sleep(_GROQ_INTER_DELAY)

    progress.empty()
    if fallback_count:
        st.warning(
            f"⚠️ {fallback_count} reviews used rule-based fallback (Groq rate limit hit). "
            "Reduce review count or wait a minute and retry."
        )
    return results
def ask_ai_question(question: str, summary_data: dict) -> str:
    """Answer a free-form question about the feedback data using Groq."""
    if not _groq_client:
        return "Set GROQ_API_KEY in .env to enable AI Q&A."
    prompt = f"""You are a product analytics expert. Answer this question based on the app review data.

Question: {question}

Data summary:
{json.dumps(summary_data, indent=2)}

Provide a concise, actionable answer (3-5 sentences). Be specific with numbers from the data."""
    try:
        resp = _groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# ─── SECTION 4 · ANALYTICS ───────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def compute_summary(df: pd.DataFrame) -> dict:
    total = len(df)
    if total == 0:
        return {}
    sent_counts = df["sentiment"].value_counts().to_dict()
    avg_conf = round(float(df["confidence_score"].dropna().mean()), 2) if "confidence_score" in df.columns and df["confidence_score"].notna().any() else None

    return {
        "total":          total,
        "avg_confidence": avg_conf,
        "positive":       int(sent_counts.get("positive", 0)),
        "neutral":        int(sent_counts.get("neutral",  0)),
        "negative":       int(sent_counts.get("negative", 0)),
        "positive_pct":   round(sent_counts.get("positive", 0) / total * 100, 1),
        "negative_pct":   round(sent_counts.get("negative", 0) / total * 100, 1),
        "avg_score":      round(float(df["sentiment_score"].mean()), 3),
        "avg_rating":     round(float(df["rating"].dropna().mean()), 2) if df["rating"].notna().any() else None,
        "bugs_count":     int(df["is_bug"].sum()),
        "features_count": int(df["is_feature"].sum()),
        "critical_count": int((df["priority"] == "critical").sum()),
        "sources":        df["source"].value_counts().to_dict(),
        "top_topics":     _top_topics(df),
        "top_keywords":   _top_keywords(df),
    }


def _top_topics(df: pd.DataFrame, n: int = 10) -> List[Tuple[str, int]]:
    cnt: Counter = Counter()
    for topics in df["topics"]:
        if isinstance(topics, list):
            cnt.update(topics)
    return cnt.most_common(n)


def _top_keywords(df: pd.DataFrame, n: int = 15) -> List[Tuple[str, int]]:
    cnt: Counter = Counter()
    for kws in df["keywords"]:
        if isinstance(kws, list):
            cnt.update(kws)
    return cnt.most_common(n)


def sentiment_trend(df: pd.DataFrame) -> pd.DataFrame:
    df2 = df.copy()
    df2["date"] = pd.to_datetime(df2["date"], errors="coerce")
    df2 = df2.dropna(subset=["date"])
    if df2.empty:
        return pd.DataFrame()
    df2["week"] = df2["date"].dt.to_period("W").apply(lambda p: p.start_time)
    trend = (df2.groupby(["week", "sentiment"])
               .size().reset_index(name="count"))
    return trend


def top_issues(df: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    bugs = df[(df["is_bug"]) & (df["sentiment"] == "negative")].copy()
    bugs = bugs.sort_values("sentiment_score").head(n)
    return bugs[["source","text","rating","priority","topics","date"]].reset_index(drop=True)


def top_feature_requests(df: pd.DataFrame, n: int = 8) -> pd.DataFrame:
    feat = df[df["is_feature"]].copy()
    feat = feat.sort_values("sentiment_score", ascending=False).head(n)
    return feat[["source","text","rating","topics","date"]].reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════════
# ─── SECTION 5 · PDF REPORT ──────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def generate_pdf_report(df: pd.DataFrame, summary: dict,
                         filepath: str = None) -> bytes:
    """Generate a professional weekly PDF report and return bytes."""
    if filepath is None:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(REPORTS_DIR, f"feedback_report_{ts}.pdf")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    story  = []

    # ── Custom styles ──
    h1 = ParagraphStyle("H1", parent=styles["Heading1"],
                        fontSize=22, textColor=colors.HexColor("#6366f1"),
                        spaceAfter=6, alignment=TA_CENTER)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"],
                        fontSize=14, textColor=colors.HexColor("#1e293b"),
                        spaceBefore=14, spaceAfter=4)
    body = ParagraphStyle("Body", parent=styles["Normal"],
                          fontSize=10, leading=15, spaceAfter=4)
    small = ParagraphStyle("Small", parent=styles["Normal"],
                           fontSize=8, textColor=colors.grey)
    kpi_style = ParagraphStyle("KPI", parent=styles["Normal"],
                               fontSize=18, textColor=colors.HexColor("#6366f1"),
                               alignment=TA_CENTER, fontName="Helvetica-Bold")

    def rule():
        return HRFlowable(width="100%", thickness=1,
                          color=colors.HexColor("#e2e8f0"), spaceAfter=8)

    # ── Cover ──
    story.append(Paragraph("📊 Feedback Intelligence Report", h1))
    story.append(Paragraph(
        f"<para alignment='center' fontSize='10' textColor='#64748b'>"
        f"Generated {datetime.utcnow().strftime('%B %d, %Y %H:%M UTC')} · "
        f"Powered by Groq AI ({GROQ_MODEL})</para>", styles["Normal"]))
    story.append(Spacer(1, 0.4*cm))
    story.append(rule())

    # ── KPI Cards ──
    story.append(Paragraph("Executive Summary", h2))
    kpi_data = [
        ["Total Reviews", "Positive", "Negative", "Avg Score", "Critical Bugs"],
        [
            Paragraph(str(summary.get("total", 0)), kpi_style),
            Paragraph(f"{summary.get('positive_pct', 0)}%", kpi_style),
            Paragraph(f"{summary.get('negative_pct', 0)}%", kpi_style),
            Paragraph(str(summary.get("avg_score", 0)), kpi_style),
            Paragraph(str(summary.get("critical_count", 0)), kpi_style),
        ]
    ]
    kpi_table = Table(kpi_data, colWidths=[3.2*cm]*5)
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#6366f1")),
        ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0), (-1,0), 10),
        ("ALIGN",      (0,0), (-1,-1), "CENTER"),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("BACKGROUND", (0,1), (-1,1), colors.HexColor("#f8fafc")),
        ("GRID",       (0,0), (-1,-1), 0.5, colors.HexColor("#e2e8f0")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.HexColor("#f8fafc"), colors.white]),
        ("TOPPADDING",  (0,0), (-1,-1), 10),
        ("BOTTOMPADDING",(0,0), (-1,-1), 10),
        ("ROUNDEDCORNERS", [4]),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 0.5*cm))

    # ── Source breakdown ──
    story.append(rule())
    story.append(Paragraph("Reviews by Source", h2))
    src_rows = [["Source", "Reviews", "Share"]]
    total_src = sum(summary.get("sources", {}).values()) or 1
    for src, cnt in sorted(summary.get("sources", {}).items(), key=lambda x: -x[1]):
        src_rows.append([src, str(cnt), f"{cnt/total_src*100:.1f}%"])
    src_table = Table(src_rows, colWidths=[7*cm, 4*cm, 5.6*cm])
    src_table.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#1e293b")),
        ("TEXTCOLOR",    (0,0), (-1,0), colors.white),
        ("FONTNAME",     (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",     (0,0), (-1,-1), 10),
        ("GRID",         (0,0), (-1,-1), 0.5, colors.HexColor("#e2e8f0")),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, colors.HexColor("#f8fafc")]),
        ("ALIGN",        (1,0), (-1,-1), "CENTER"),
        ("TOPPADDING",   (0,0), (-1,-1), 7),
        ("BOTTOMPADDING",(0,0), (-1,-1), 7),
    ]))
    story.append(src_table)
    story.append(Spacer(1, 0.4*cm))

    # ── Top Issues ──
    story.append(rule())
    story.append(Paragraph("🐛 Critical Issues Requiring Attention", h2))
    issues_df = top_issues(df, n=8)
    if not issues_df.empty:
        iss_rows = [["Priority", "Source", "Feedback (excerpt)", "Rating"]]
        for _, row in issues_df.iterrows():
            excerpt = row["text"][:120] + ("…" if len(row["text"]) > 120 else "")
            iss_rows.append([
                row.get("priority","normal").upper(),
                row.get("source",""),
                Paragraph(excerpt, small),
                str(int(row["rating"])) if pd.notna(row.get("rating")) else "—",
            ])
        iss_table = Table(iss_rows, colWidths=[2*cm, 2.8*cm, 9.4*cm, 1.8*cm])
        iss_table.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#ef4444")),
            ("TEXTCOLOR",    (0,0), (-1,0), colors.white),
            ("FONTNAME",     (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",     (0,0), (-1,-1), 9),
            ("GRID",         (0,0), (-1,-1), 0.4, colors.HexColor("#fecaca")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.HexColor("#fff5f5"), colors.white]),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",   (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0), (-1,-1), 6),
        ]))
        story.append(iss_table)
    else:
        story.append(Paragraph("No critical bugs identified. 🎉", body))
    story.append(Spacer(1, 0.4*cm))

    # ── Feature Requests ──
    story.append(rule())
    story.append(Paragraph("💡 Top Feature Requests", h2))
    feat_df = top_feature_requests(df, n=6)
    if not feat_df.empty:
        feat_rows = [["Source", "Request (excerpt)", "Rating"]]
        for _, row in feat_df.iterrows():
            excerpt = row["text"][:130] + ("…" if len(row["text"]) > 130 else "")
            feat_rows.append([
                row.get("source",""),
                Paragraph(excerpt, small),
                str(int(row["rating"])) if pd.notna(row.get("rating")) else "—",
            ])
        feat_table = Table(feat_rows, colWidths=[2.8*cm, 11.4*cm, 1.8*cm])
        feat_table.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#6366f1")),
            ("TEXTCOLOR",    (0,0), (-1,0), colors.white),
            ("FONTNAME",     (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",     (0,0), (-1,-1), 9),
            ("GRID",         (0,0), (-1,-1), 0.4, colors.HexColor("#e0e7ff")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.HexColor("#eef2ff"), colors.white]),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",   (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0), (-1,-1), 6),
        ]))
        story.append(feat_table)
    story.append(Spacer(1, 0.4*cm))

    # ── Top Topics ──
    story.append(rule())
    story.append(Paragraph("🏷️ Trending Topics", h2))
    topics = summary.get("top_topics", [])
    if topics:
        max_cnt = topics[0][1] if topics else 1
        topic_rows = [["Topic", "Mentions", "Frequency"]]
        for topic, cnt in topics[:8]:
            bar = "█" * int(cnt / max_cnt * 20)
            topic_rows.append([topic.title(), str(cnt), bar])
        t_table = Table(topic_rows, colWidths=[5*cm, 2.5*cm, 8.5*cm])
        t_table.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#0f172a")),
            ("TEXTCOLOR",    (0,0), (-1,0), colors.white),
            ("FONTNAME",     (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",     (0,0), (-1,-1), 9),
            ("TEXTCOLOR",    (2,1), (2,-1), colors.HexColor("#6366f1")),
            ("GRID",         (0,0), (-1,-1), 0.4, colors.HexColor("#e2e8f0")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, colors.HexColor("#f8fafc")]),
            ("TOPPADDING",   (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0), (-1,-1), 6),
        ]))
        story.append(t_table)

    # ── Footer ──
    story.append(Spacer(1, cm))
    story.append(rule())
    story.append(Paragraph(
        f"<para alignment='center' textColor='#94a3b8' fontSize='8'>"
        f"Multi-Source Feedback Intelligence System · HiDevs Capstone · "
        f"{datetime.utcnow().strftime('%Y-%m-%d')}</para>",
        styles["Normal"]
    ))

    doc.build(story)
    pdf_bytes = buf.getvalue()

    # Also save to file
    with open(filepath, "wb") as f:
        f.write(pdf_bytes)

    return pdf_bytes


# ═══════════════════════════════════════════════════════════════════════════════
# ─── SECTION 6 · STREAMLIT DASHBOARD ─────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Feedback Intelligence",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Global CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');

html, body, [class*="css"] { font-family: 'Space Grotesk', sans-serif; }

.stApp { background: #0f172a; }

/* Sidebar */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #1e293b 0%, #0f172a 100%);
    border-right: 1px solid #334155;
}
[data-testid="stSidebar"] .stMarkdown { color: #94a3b8; }

/* Metric cards */
[data-testid="stMetric"] {
    background: linear-gradient(135deg, #1e293b, #162032);
    border: 1px solid #334155;
    border-radius: 12px;
    padding: 16px 20px;
    transition: transform 0.2s, box-shadow 0.2s;
}
[data-testid="stMetric"]:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 30px rgba(99,102,241,0.2);
}
[data-testid="stMetricLabel"] { color: #94a3b8 !important; font-size: 12px !important; }
[data-testid="stMetricValue"] { color: #f1f5f9 !important; font-family: 'JetBrains Mono', monospace !important; }

/* Tabs */
[data-testid="stTab"] button {
    color: #64748b;
    border-bottom: 2px solid transparent;
}
[data-testid="stTab"] button[aria-selected="true"] {
    color: #6366f1;
    border-bottom: 2px solid #6366f1;
}

/* Buttons */
.stButton > button {
    background: linear-gradient(135deg, #6366f1, #4f46e5);
    color: white;
    border: none;
    border-radius: 8px;
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 600;
    transition: all 0.2s;
}
.stButton > button:hover {
    background: linear-gradient(135deg, #4f46e5, #4338ca);
    transform: translateY(-1px);
    box-shadow: 0 4px 15px rgba(99,102,241,0.4);
}

/* Section headers */
.section-header {
    font-size: 13px; font-weight: 600; letter-spacing: 0.1em;
    text-transform: uppercase; color: #6366f1; margin-bottom: 16px;
    border-left: 3px solid #6366f1; padding-left: 10px;
}

/* Sentiment badges */
.badge-positive { background:#064e3b; color:#34d399; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:600; }
.badge-neutral  { background:#451a03; color:#fbbf24; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:600; }
.badge-negative { background:#450a0a; color:#f87171; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:600; }
.badge-critical { background:#450a0a; color:#ff0000; padding:3px 10px; border-radius:20px; font-size:12px; font-weight:700; }

/* Cards */
.review-card {
    background: #1e293b; border: 1px solid #334155; border-radius: 12px;
    padding: 16px; margin-bottom: 12px;
    transition: border-color 0.2s;
}
.review-card:hover { border-color: #6366f1; }

/* AI chat box */
.ai-answer {
    background: linear-gradient(135deg, #1e293b, #162032);
    border: 1px solid #6366f1;
    border-radius: 12px;
    padding: 20px;
    color: #e2e8f0;
    font-size: 15px;
    line-height: 1.7;
    margin-top: 12px;
}

/* Scrollbar */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: #0f172a; }
::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }

/* DataFrames */
.stDataFrame { border-radius: 10px; overflow: hidden; }

/* Divider */
.divider { height:1px; background:linear-gradient(90deg,transparent,#334155,transparent); margin:24px 0; }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("""
    <div style='text-align:center; padding:20px 0 10px'>
        <div style='font-size:42px'>📊</div>
        <div style='font-size:18px; font-weight:700; color:#f1f5f9;'>Feedback Intel</div>
        <div style='font-size:11px; color:#6366f1; letter-spacing:0.15em; margin-top:2px;'>HiDevs Capstone</div>
    </div>
    <hr style='border-color:#334155; margin:12px 0'>
    """, unsafe_allow_html=True)

    st.markdown('<p class="section-header">Data Sources</p>', unsafe_allow_html=True)

    use_play     = st.toggle("Google Play Store", value=True)
    use_appstore = st.toggle("Apple App Store",   value=True)
    use_csv      = st.toggle("Survey / CSV",      value=True)

    uploaded_csv = None
    if use_csv:
        uploaded_csv = st.file_uploader("Upload CSV file", type=["csv"],
                                         help="Columns: text, rating (optional), date, author")

    st.markdown('<hr style="border-color:#334155; margin:12px 0">', unsafe_allow_html=True)
    st.markdown('<p class="section-header">Filters</p>', unsafe_allow_html=True)

    date_range = st.date_input(
        "Date Range",
        value=(datetime.utcnow() - timedelta(days=30), datetime.utcnow()),
        max_value=datetime.utcnow(),
    )
    sentiment_filter = st.multiselect(
        "Sentiment", ["positive", "neutral", "negative"],
        default=["positive", "neutral", "negative"],
    )
    source_filter = st.multiselect(
        "Source", ["Google Play", "App Store", "Survey / CSV"],
        default=["Google Play", "App Store", "Survey / CSV"],
    )
    priority_filter = st.multiselect(
        "Priority", ["low", "normal", "high", "critical"],
        default=["low", "normal", "high", "critical"],
    )

    st.markdown('<hr style="border-color:#334155; margin:12px 0">', unsafe_allow_html=True)
    st.markdown('<p class="section-header">Controls</p>', unsafe_allow_html=True)

    col_a, col_b = st.columns(2)
    with col_a:
        refresh_btn = st.button("🔄 Refresh", use_container_width=True)
    with col_b:
        clear_cache = st.button("🗑️ Clear Cache", use_container_width=True)

    if clear_cache and Path(CACHE_FILE).exists():
        os.remove(CACHE_FILE)
        st.success("Cache cleared!")
        st.rerun()

    # ── Synthetic Data Generator ──────────────────────────────────────────
    st.markdown('<hr style="border-color:#334155; margin:12px 0">', unsafe_allow_html=True)
    st.markdown('<p class="section-header">🧪 Synthetic Test Data</p>', unsafe_allow_html=True)

    synth_n    = st.slider("Number of reviews", 50, 500, 200, step=50)
    synth_days = st.slider("Spread over N days",  7,  90,  60, step=7)
    synth_app  = st.text_input("App name", value="MyApp")

    if st.button("⚡ Generate Synthetic Data", use_container_width=True):
        with st.spinner(f"Generating {synth_n} synthetic reviews…"):
            synth = generate_synthetic_reviews(synth_n, synth_days, synth_app)
            df_synth = pd.DataFrame(synth)
            df_synth["date"]            = pd.to_datetime(df_synth["date"], errors="coerce")
            df_synth["sentiment_score"] = pd.to_numeric(df_synth["sentiment_score"], errors="coerce").fillna(0)
            df_synth["rating"]          = pd.to_numeric(df_synth["rating"], errors="coerce")
            st.session_state["df"]      = df_synth
            st.session_state["synth_active"] = True
            # Save to cache so filters persist
            try:
                Path(CACHE_FILE).write_text(json.dumps({
                    "ts": time.time(), "reviews": synth
                }))
            except Exception:
                pass
        st.success(f"✅ {synth_n} synthetic reviews loaded!")
        st.rerun()

    if st.session_state.get("synth_active"):
        st.markdown(
            "<div style='text-align:center; font-size:11px; color:#6366f1;"
            " background:#1e293b; border-radius:6px; padding:4px;'>"
            "🧪 Synthetic data active</div>",
            unsafe_allow_html=True
        )

    st.markdown('<hr style="border-color:#334155; margin:12px 0">', unsafe_allow_html=True)
    ai_status = "✅ AI Ready" if GROQ_API_KEY else "⚠️ No API Key"
    ai_color  = "#22c55e" if GROQ_API_KEY else "#f59e0b"
    st.markdown(f"""
    <div style='text-align:center; font-size:12px; color:{ai_color};'>{ai_status}</div>
    <div style='text-align:center; font-size:10px; color:#475569; margin-top:2px;'>{GROQ_MODEL}</div>
    """, unsafe_allow_html=True)

    # Sample CSV download
    sample_csv_path = os.path.join(DATA_DIR, "sample_survey.csv")
    generate_sample_csv(sample_csv_path)
    with open(sample_csv_path, "rb") as f:
        st.download_button("📥 Sample CSV", f.read(), "sample_survey.csv",
                           "text/csv", use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADING + ANALYSIS  (cached in session_state)
# ═══════════════════════════════════════════════════════════════════════════════

csv_path = ""
if uploaded_csv:
    tmp = os.path.join(DATA_DIR, "uploaded.csv")
    with open(tmp, "wb") as f:
        f.write(uploaded_csv.read())
    csv_path = tmp

def load_and_analyse(force: bool = False):
    raw = fetch_all(
        use_play     = use_play,
        use_appstore = use_appstore,
        use_csv      = use_csv,
        csv_path     = csv_path,
        force_refresh= force,
    )
    analysed = analyze_batch(raw)
    df = pd.DataFrame(analysed)
    if df.empty:
        return df
    df["date"]            = pd.to_datetime(df["date"], errors="coerce")
    df["sentiment_score"] = pd.to_numeric(df["sentiment_score"], errors="coerce").fillna(0)
    df["rating"]          = pd.to_numeric(df["rating"], errors="coerce")
    return df

if "df" not in st.session_state or (refresh_btn and not st.session_state.get("synth_active")):
    with st.spinner("Fetching & analysing reviews…"):
        st.session_state["df"] = load_and_analyse(force=refresh_btn)
        st.session_state["synth_active"] = False
elif refresh_btn and st.session_state.get("synth_active"):
    st.session_state["synth_active"] = False
    with st.spinner("Loading real reviews…"):
        st.session_state["df"] = load_and_analyse(force=True)

df_all = st.session_state["df"]

# ─── Apply filters ────────────────────────────────────────────────────────────
df = df_all.copy()
if not df.empty:
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_dt = pd.Timestamp(date_range[0])
        end_dt   = pd.Timestamp(date_range[1]) + pd.Timedelta(days=1)
        df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]
    if sentiment_filter:
        df = df[df["sentiment"].isin(sentiment_filter)]
    if source_filter:
        df = df[df["source"].isin(source_filter)]
    if priority_filter:
        df = df[df["priority"].isin(priority_filter)]

summary = compute_summary(df)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN CONTENT
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<div style='padding: 24px 0 16px'>
    <h1 style='color:#f1f5f9; font-size:28px; font-weight:700; margin:0;'>
        Multi-Source Feedback Intelligence
    </h1>
    <p style='color:#64748b; margin:4px 0 0; font-size:14px;'>
        Real-time insights · AI-powered sentiment · Actionable reports
    </p>
</div>
""", unsafe_allow_html=True)

if df.empty:
    st.warning("No reviews match your filters. Adjust the sidebar filters.")
    st.stop()

# ── KPI Row ───────────────────────────────────────────────────────────────────
c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
c1.metric("Total Reviews",  summary.get("total", 0))
c2.metric("Positive",       f"{summary.get('positive_pct', 0)}%",
          f"+{summary.get('positive', 0)}")
c3.metric("Negative",       f"{summary.get('negative_pct', 0)}%",
          f"-{summary.get('negative', 0)}", delta_color="inverse")
c4.metric("Avg Sentiment",  summary.get("avg_score", 0))
c5.metric("Avg Rating",     summary.get("avg_rating") or "—")
c6.metric("🐛 Bugs",        summary.get("bugs_count", 0))
c7.metric("🚨 Critical",    summary.get("critical_count", 0))

# ── Trend Detection Row ───────────────────────────────────────────────────────
def _trend_arrow(df_in, col="sentiment_score", split_days=15):
    """Return (arrow, color, label) showing if metric is improving or declining."""
    df2 = df_in.copy()
    df2["date"] = pd.to_datetime(df2["date"], errors="coerce")
    # Ensure timezone-naive for comparison (fixes datetime64 vs Timestamp error)
    if hasattr(df2["date"].dt, "tz") and df2["date"].dt.tz is not None:
        df2["date"] = df2["date"].dt.tz_localize(None)
    mid = pd.Timestamp.now() - pd.Timedelta(days=split_days)
    recent = df2[df2["date"] >= mid][col].dropna()
    older  = df2[df2["date"] <  mid][col].dropna()
    if len(recent) < 3 or len(older) < 3:
        return "→", "#94a3b8", "stable"
    diff = float(recent.mean()) - float(older.mean())
    if diff > 0.05:  return "↑", "#22c55e", f"+{diff:.2f} improving"
    if diff < -0.05: return "↓", "#ef4444", f"{diff:.2f} declining"
    return "→", "#f59e0b", "stable"

if not df.empty and len(df) >= 6:
    s_arrow, s_color, s_label = _trend_arrow(df, "sentiment_score")
    r_arrow, r_color, r_label = _trend_arrow(df[df["rating"].notna()], "rating") if df["rating"].notna().sum() >= 6 else ("→","#94a3b8","stable")
    conf_val = summary.get("avg_confidence")
    conf_str = f"{int(conf_val*100)}%" if conf_val else "—"
    bug_arrow, bug_color, bug_label = _trend_arrow(
        df.assign(bug_flag=df["is_bug"].astype(int)), "bug_flag")
    # Invert bug arrow (more bugs = bad)
    if bug_arrow == "↑": bug_arrow, bug_color, bug_label = "↑", "#ef4444", "bug reports rising"
    elif bug_arrow == "↓": bug_arrow, bug_color, bug_label = "↓", "#22c55e", "bug reports falling"

    st.markdown(f"""
    <div style='display:flex; gap:12px; margin:8px 0 4px; flex-wrap:wrap;'>
        <div style='background:#1e293b; border:1px solid #334155; border-radius:10px;
                    padding:10px 18px; display:flex; align-items:center; gap:10px;'>
            <span style='font-size:22px; color:{s_color}; font-weight:700;'>{s_arrow}</span>
            <div>
                <div style='color:#94a3b8; font-size:10px; text-transform:uppercase; letter-spacing:.05em;'>Sentiment Trend</div>
                <div style='color:#e2e8f0; font-size:13px; font-weight:600;'>{s_label}</div>
            </div>
        </div>
        <div style='background:#1e293b; border:1px solid #334155; border-radius:10px;
                    padding:10px 18px; display:flex; align-items:center; gap:10px;'>
            <span style='font-size:22px; color:{r_color}; font-weight:700;'>{r_arrow}</span>
            <div>
                <div style='color:#94a3b8; font-size:10px; text-transform:uppercase; letter-spacing:.05em;'>Rating Trend</div>
                <div style='color:#e2e8f0; font-size:13px; font-weight:600;'>{r_label}</div>
            </div>
        </div>
        <div style='background:#1e293b; border:1px solid #334155; border-radius:10px;
                    padding:10px 18px; display:flex; align-items:center; gap:10px;'>
            <span style='font-size:22px; color:{bug_color}; font-weight:700;'>{bug_arrow}</span>
            <div>
                <div style='color:#94a3b8; font-size:10px; text-transform:uppercase; letter-spacing:.05em;'>Bug Report Trend</div>
                <div style='color:#e2e8f0; font-size:13px; font-weight:600;'>{bug_label}</div>
            </div>
        </div>
        <div style='background:#1e293b; border:1px solid #334155; border-radius:10px;
                    padding:10px 18px; display:flex; align-items:center; gap:10px;'>
            <span style='font-size:22px; color:#6366f1; font-weight:700;'>🎯</span>
            <div>
                <div style='color:#94a3b8; font-size:10px; text-transform:uppercase; letter-spacing:.05em;'>AI Confidence</div>
                <div style='color:#e2e8f0; font-size:13px; font-weight:600;'>{conf_str}</div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

st.markdown("<div class='divider'></div>", unsafe_allow_html=True)

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📈 Overview", "🐛 Issues & Bugs", "💡 Feature Requests",
    "📋 All Reviews", "🤖 AI Insights"
])

# ────────────────────────────────────────────────────────────────────────
# TAB 1 · OVERVIEW
# ────────────────────────────────────────────────────────────────────────
with tab1:
    left, right = st.columns([1, 1])

    with left:
        st.markdown('<p class="section-header">Sentiment Distribution</p>', unsafe_allow_html=True)
        sent_counts = df["sentiment"].value_counts()
        fig_pie = go.Figure(go.Pie(
            labels=sent_counts.index,
            values=sent_counts.values,
            hole=0.55,
            marker_colors=[PALETTE.get(s, "#94a3b8") for s in sent_counts.index],
            textinfo="label+percent",
            textfont_color="#f1f5f9",
        ))
        fig_pie.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            showlegend=False, margin=dict(t=10, b=10, l=10, r=10),
            height=280,
            annotations=[dict(text=f"<b>{summary['total']}</b><br>reviews",
                              font=dict(size=18, color="#f1f5f9"), showarrow=False)]
        )
        st.plotly_chart(fig_pie, use_container_width=True)

    with right:
        st.markdown('<p class="section-header">Reviews by Source</p>', unsafe_allow_html=True)
        src_cnt = df["source"].value_counts()
        fig_bar = go.Figure(go.Bar(
            x=src_cnt.values,
            y=src_cnt.index,
            orientation="h",
            marker=dict(
                color=src_cnt.values,
                colorscale=[[0,"#4f46e5"],[0.5,"#6366f1"],[1,"#818cf8"]],
                showscale=False,
            ),
            text=src_cnt.values,
            textposition="outside",
            textfont=dict(color="#94a3b8"),
        ))
        fig_bar.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=False, color="#475569"),
            yaxis=dict(color="#94a3b8"),
            margin=dict(t=10, b=10, l=10, r=30),
            height=280,
        )
        st.plotly_chart(fig_bar, use_container_width=True)

    # Sentiment Trend
    st.markdown('<p class="section-header">Sentiment Trend Over Time</p>', unsafe_allow_html=True)
    trend_df = sentiment_trend(df)
    if not trend_df.empty:
        fig_trend = px.area(
            trend_df, x="week", y="count", color="sentiment",
            color_discrete_map={"positive": PALETTE["positive"],
                                "neutral":  PALETTE["neutral"],
                                "negative": PALETTE["negative"]},
            labels={"count": "Reviews", "week": "Week"},
        )
        fig_trend.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", yanchor="bottom", y=1.02,
                        font=dict(color="#94a3b8")),
            xaxis=dict(showgrid=False, color="#475569"),
            yaxis=dict(showgrid=True, gridcolor="#1e293b", color="#475569"),
            margin=dict(t=30, b=10),
            height=280,
        )
        st.plotly_chart(fig_trend, use_container_width=True)
    else:
        st.info("Not enough date data for trend chart.")

    # Top Topics
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown('<p class="section-header">Top Topics</p>', unsafe_allow_html=True)
        topics = summary.get("top_topics", [])
        if topics:
            t_labels = [t[0] for t in topics]
            t_values = [t[1] for t in topics]
            fig_t = go.Figure(go.Bar(
                x=t_values, y=t_labels, orientation="h",
                marker_color="#6366f1",
                text=t_values, textposition="outside",
                textfont=dict(color="#94a3b8"),
            ))
            fig_t.update_layout(
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                yaxis=dict(color="#94a3b8", autorange="reversed"),
                xaxis=dict(showgrid=False, color="#475569"),
                margin=dict(t=10, b=10, l=10, r=30), height=300,
            )
            st.plotly_chart(fig_t, use_container_width=True)

    with col_b:
        st.markdown('<p class="section-header">Priority Breakdown</p>', unsafe_allow_html=True)
        prio_counts = df["priority"].value_counts()
        prio_colors = {"critical":"#ef4444","high":"#f97316",
                       "normal":"#6366f1","low":"#22c55e"}
        fig_p = go.Figure(go.Pie(
            labels=prio_counts.index,
            values=prio_counts.values,
            hole=0.4,
            marker_colors=[prio_colors.get(p,"#94a3b8") for p in prio_counts.index],
            textinfo="label+percent",
            textfont_color="#f1f5f9",
        ))
        fig_p.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            showlegend=False, margin=dict(t=10, b=10), height=300,
        )
        st.plotly_chart(fig_p, use_container_width=True)

    # Sentiment score histogram
    st.markdown('<p class="section-header">Sentiment Score Distribution</p>', unsafe_allow_html=True)
    fig_hist = px.histogram(
        df, x="sentiment_score", nbins=30,
        color_discrete_sequence=["#6366f1"],
        labels={"sentiment_score": "Sentiment Score (-1 = negative, +1 = positive)"},
    )
    fig_hist.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showgrid=False, color="#475569"),
        yaxis=dict(showgrid=True, gridcolor="#1e293b", color="#475569"),
        margin=dict(t=10, b=10), height=220, bargap=0.1,
    )
    st.plotly_chart(fig_hist, use_container_width=True)

    # PDF download
    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    col_dl, _ = st.columns([1, 3])
    with col_dl:
        if st.button("📄 Download PDF Report", use_container_width=True):
            with st.spinner("Generating PDF…"):
                pdf_bytes = generate_pdf_report(df, summary)
            st.download_button(
                "⬇️ Save Report", pdf_bytes,
                f"feedback_report_{datetime.utcnow().strftime('%Y%m%d')}.pdf",
                "application/pdf", use_container_width=True,
            )


# ────────────────────────────────────────────────────────────────────────
# TAB 2 · ISSUES & BUGS
# ────────────────────────────────────────────────────────────────────────
with tab2:
    bugs_df = df[(df["is_bug"]) | (df["priority"].isin(["critical","high"]))].copy()
    bugs_df = bugs_df.sort_values("sentiment_score")

    crit = bugs_df[bugs_df["priority"] == "critical"]
    high = bugs_df[bugs_df["priority"] == "high"]

    m1, m2, m3 = st.columns(3)
    m1.metric("Total Bug Reports", len(bugs_df))
    m2.metric("🚨 Critical",       len(crit))
    m3.metric("⬆️ High Priority",  len(high))

    st.markdown("<div class='divider'></div>", unsafe_allow_html=True)

    if bugs_df.empty:
        st.success("🎉 No bugs or issues detected!")
    else:
        st.markdown('<p class="section-header">Issues Ranked by Severity</p>',
                    unsafe_allow_html=True)
        for _, row in bugs_df.head(20).iterrows():
            prio  = row.get("priority","normal")
            badge = f"<span class='badge-critical'>🚨 CRITICAL</span>" if prio=="critical" \
                    else f"<span class='badge-negative'>⬆ HIGH</span>"   if prio=="high" \
                    else f"<span class='badge-neutral'>NORMAL</span>"

            rating_str = f"⭐ {int(row['rating'])}/5" if pd.notna(row.get("rating")) else ""
            topics_str = ", ".join(row.get("topics", [])) if row.get("topics") else ""

            st.markdown(f"""
            <div class="review-card">
                <div style='display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;'>
                    <div style='display:flex; gap:8px; align-items:center;'>
                        {badge}
                        <span style='color:#64748b; font-size:12px;'>{row.get('source','')} · {str(row.get('date',''))[:10]}</span>
                    </div>
                    <span style='color:#94a3b8; font-size:13px;'>{rating_str}</span>
                </div>
                <p style='color:#e2e8f0; font-size:14px; line-height:1.6; margin:0;'>{row['text'][:300]}{'…' if len(row['text'])>300 else ''}</p>
                {f"<div style='margin-top:8px;'><span style='color:#6366f1; font-size:11px;'>🏷 {topics_str}</span></div>" if topics_str else ""}
            </div>
            """, unsafe_allow_html=True)

    # Bug trend chart
    if not bugs_df.empty:
        st.markdown('<p class="section-header">Bug Reports Over Time</p>',
                    unsafe_allow_html=True)
        bugs_df2 = bugs_df.copy()
        bugs_df2["week"] = pd.to_datetime(bugs_df2["date"], errors="coerce") \
                             .dt.to_period("W").apply(lambda p: p.start_time)
        bug_trend = bugs_df2.groupby(["week","priority"]).size().reset_index(name="count")
        fig_bt = px.bar(bug_trend, x="week", y="count", color="priority",
                        color_discrete_map={"critical":"#ef4444","high":"#f97316",
                                            "normal":"#6366f1","low":"#22c55e"})
        fig_bt.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=False, color="#475569"),
            yaxis=dict(showgrid=True, gridcolor="#1e293b", color="#475569"),
            legend=dict(font=dict(color="#94a3b8"), orientation="h"),
            margin=dict(t=10, b=10), height=250,
        )
        st.plotly_chart(fig_bt, use_container_width=True)


# ────────────────────────────────────────────────────────────────────────
# TAB 3 · FEATURE REQUESTS
# ────────────────────────────────────────────────────────────────────────
with tab3:
    feat_df = df[df["is_feature"]].copy()
    feat_df = feat_df.sort_values("sentiment_score", ascending=False)

    st.metric("Total Feature Requests", len(feat_df))
    st.markdown("<div class='divider'></div>", unsafe_allow_html=True)

    if feat_df.empty:
        st.info("No feature requests detected in current filters.")
    else:
        st.markdown('<p class="section-header">Feature Requests (Highest Rated First)</p>',
                    unsafe_allow_html=True)
        for _, row in feat_df.head(15).iterrows():
            rating_str = f"⭐ {int(row['rating'])}/5" if pd.notna(row.get("rating")) else ""
            topics_str = ", ".join(row.get("topics", [])) if row.get("topics") else ""
            st.markdown(f"""
            <div class="review-card">
                <div style='display:flex; justify-content:space-between; margin-bottom:8px;'>
                    <span style='color:#6366f1; font-size:12px; font-weight:600;'>💡 FEATURE REQUEST</span>
                    <span style='color:#64748b; font-size:12px;'>{row.get('source','')} · {str(row.get('date',''))[:10]} {rating_str}</span>
                </div>
                <p style='color:#e2e8f0; font-size:14px; line-height:1.6; margin:0;'>{row['text'][:300]}{'…' if len(row['text'])>300 else ''}</p>
                {f"<div style='margin-top:8px;'><span style='color:#818cf8; font-size:11px;'>🏷 {topics_str}</span></div>" if topics_str else ""}
            </div>
            """, unsafe_allow_html=True)


# ────────────────────────────────────────────────────────────────────────
# TAB 4 · ALL REVIEWS
# ────────────────────────────────────────────────────────────────────────
with tab4:
    st.markdown('<p class="section-header">All Reviews</p>', unsafe_allow_html=True)

    search = st.text_input("🔍 Search reviews", placeholder="Type to filter…")
    if search:
        mask = df["text"].str.contains(search, case=False, na=False)
        display_df = df[mask]
    else:
        display_df = df

    for _, row in display_df.head(50).iterrows():
        sent = row.get("sentiment","neutral")
        badge_cls = f"badge-{sent}"
        score = row.get("sentiment_score", 0)
        score_color = "#22c55e" if score > 0 else "#ef4444" if score < 0 else "#f59e0b"
        rating_str = f"⭐ {int(row['rating'])}/5" if pd.notna(row.get("rating")) else ""
        conf = row.get("confidence_score")
        conf_pct = f"{int(conf*100)}%" if conf is not None else "—"
        tags = []
        if row.get("is_bug"):     tags.append("🐛 Bug")
        if row.get("is_feature"): tags.append("💡 Feature")
        tags_str = " &nbsp; ".join(tags)

        st.markdown(f"""
        <div class="review-card">
            <div style='display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;'>
                <div style='display:flex; gap:8px; align-items:center; flex-wrap:wrap;'>
                    <span class='{badge_cls}'>{sent.upper()}</span>
                    <span style='color:#64748b; font-size:12px;'>{row.get('source','')} · {str(row.get('date',''))[:10]}</span>
                    {f"<span style='color:#818cf8; font-size:11px;'>{tags_str}</span>" if tags_str else ""}
                </div>
                <div style='display:flex; gap:12px; align-items:center;'>
                    <span style='color:{score_color}; font-family:monospace; font-size:12px; font-weight:600;'>{score:+.2f}</span>
                    <span style='color:#475569; font-size:11px;' title='AI confidence'>conf:{conf_pct}</span>
                    <span style='color:#94a3b8; font-size:12px;'>{rating_str}</span>
                </div>
            </div>
            <p style='color:#e2e8f0; font-size:14px; line-height:1.6; margin:0 0 4px;'>{row['text'][:250]}{'…' if len(row['text'])>250 else ''}</p>
            <div style='color:#475569; font-size:11px;'>by {row.get('author','Anonymous')}</div>
        </div>
        """, unsafe_allow_html=True)

    if len(display_df) > 50:
        st.info(f"Showing 50 of {len(display_df)} reviews. Use filters to narrow down.")


# ────────────────────────────────────────────────────────────────────────
# TAB 5 · AI INSIGHTS
# ────────────────────────────────────────────────────────────────────────
with tab5:
    st.markdown('<p class="section-header">Ask Anything About Your Reviews</p>',
                unsafe_allow_html=True)

    quick = st.columns(3)
    presets = [
        "What are the most critical bugs needing immediate attention?",
        "What features are users requesting most frequently?",
        "Which source has the worst sentiment and why?",
    ]
    for i, (col, q) in enumerate(zip(quick, presets)):
        with col:
            if st.button(q[:45] + "…", key=f"preset_{i}", use_container_width=True):
                st.session_state["ai_q"] = q

    question = st.text_area(
        "Your question",
        value=st.session_state.get("ai_q", ""),
        placeholder="e.g. What are users saying about performance? Is sentiment improving?",
        height=80,
    )

    if st.button("🤖 Get AI Answer", use_container_width=False):
        if question.strip():
            with st.spinner("Consulting Groq AI…"):
                answer = ask_ai_question(question, summary)
            st.markdown(f'<div class="ai-answer">{answer}</div>', unsafe_allow_html=True)
        else:
            st.warning("Please enter a question.")

    st.markdown("<div class='divider'></div>", unsafe_allow_html=True)

    # Auto-generated weekly insights
    st.markdown('<p class="section-header">Automated Weekly Insights</p>',
                unsafe_allow_html=True)
    neg_pct  = summary.get("negative_pct", 0)
    pos_pct  = summary.get("positive_pct", 0)
    avg_sc   = summary.get("avg_score", 0)
    crit_cnt = summary.get("critical_count", 0)
    bugs_cnt = summary.get("bugs_count", 0)

    insight_rows = [
        ("🎯 Sentiment Health",
         f"Overall sentiment score: **{avg_sc:+.3f}**. "
         f"{pos_pct:.1f}% positive · {neg_pct:.1f}% negative. "
         + ("🟢 Healthy" if avg_sc > 0.2 else "🔴 Needs attention" if avg_sc < -0.1 else "🟡 Neutral")),
        ("🐛 Bug Pressure",
         f"**{bugs_cnt}** bug reports detected, of which **{crit_cnt}** are critical. "
         + ("Immediate engineering attention required." if crit_cnt > 2 else "Monitor for recurrence.")),
        ("💡 Feature Momentum",
         f"**{summary.get('features_count', 0)}** feature requests captured. "
         "Top requested topics: " + ", ".join(t[0] for t in summary.get("top_topics",[])[:3]) + "."),
        ("📊 Source Coverage",
         "Reviews collected from: " + ", ".join(
             f"{src} ({cnt})" for src, cnt in summary.get("sources",{}).items()
         ) + "."),
    ]
    for title, body in insight_rows:
        st.markdown(f"""
        <div class="review-card" style='border-left:3px solid #6366f1;'>
            <div style='color:#818cf8; font-size:13px; font-weight:600; margin-bottom:6px;'>{title}</div>
            <div style='color:#cbd5e1; font-size:14px; line-height:1.6;'>{body}</div>
        </div>
        """, unsafe_allow_html=True)

    # PDF download from insights tab too
    st.markdown("<div class='divider'></div>", unsafe_allow_html=True)
    if st.button("📄 Generate & Download PDF Report", use_container_width=False):
        with st.spinner("Building PDF…"):
            pdf_bytes = generate_pdf_report(df, summary)
        st.download_button(
            "⬇️ Download Report",
            pdf_bytes,
            f"feedback_report_{datetime.utcnow().strftime('%Y%m%d')}.pdf",
            "application/pdf",
        )