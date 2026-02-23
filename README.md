# 📊 Multi-Source Feedback Intelligence System
**HiDevs Internship Capstone · Customer Experience & Product Analytics**

---

## What It Does

Aggregates app reviews from **Google Play Store**, **Apple App Store (RSS)**, and **CSV surveys**, runs AI sentiment analysis via **Groq (free)**, and surfaces insights through a **Streamlit dashboard** with PDF export.

---

## Quick Start

```bash
# 1. Clone / download
git clone <repo-url>
cd feedback-intel

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure your API key
cp .env.example .env
# Edit .env → set GROQ_API_KEY (free at https://console.groq.com)

# 4. Run
streamlit run main.py
```

Open **http://localhost:8501** in your browser.

---

## Features vs Evaluation Criteria

| Criterion | ✅ Implemented |
|---|---|
| Multi-Source Integration | Google Play (scraper) + App Store (RSS) + CSV upload — mock fallback if unavailable |
| Sentiment Analysis | Groq AI (llama-3.3-70b) → sentiment, score, **confidence %**, rule-based fallback |
| Trend Detection | ↑↓→ arrows for sentiment, rating & bug trends with period-over-period diff |
| Issue Prioritization | `critical / high / normal / low` flags; auto-ranked bug board |
| Streamlit Dashboard | Date range · Source · Sentiment · Priority filters; 5-tab layout |
| PDF Reports | ReportLab — KPIs, bug table, feature requests, topic bars, footer |
| Code Quality | All in `main.py` with clear sections (Fetching → Analysis → Analytics → PDF → Dashboard) |
| Error Handling | 45+ try/except blocks; graceful mock-data fallback on every source |
| Documentation | This README + inline docstrings + `.env.example` |

---

## How to Get a Free Groq API Key

1. Go to **https://console.groq.com**
2. Sign up (no credit card required)
3. Click **API Keys → Create API Key**
4. Copy the key into your `.env` file

---

## Environment Variables (`.env`)

| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEY` | *(required)* | Your Groq API key |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Groq model to use |
| `GOOGLE_PLAY_APP_ID` | `com.whatsapp` | Android app package ID |
| `APPSTORE_APP_ID` | `310633997` | iOS numeric app ID |
| `DATA_DIR` | `data/` | Cache & CSV storage |
| `REPORTS_DIR` | `reports/` | PDF output folder |

---

## Dashboard Tabs

| Tab | Content |
|---|---|
| 📈 Overview | KPIs · Trend arrows · Pie chart · Source bar · Sentiment trend · Topic chart |
| 🐛 Issues & Bugs | Ranked critical/high issues · Bug trend over time |
| 💡 Feature Requests | All detected feature asks sorted by rating |
| 📋 All Reviews | Searchable full list with sentiment score + confidence % |
| 🤖 AI Insights | Free-text Q&A + auto weekly summary |

---

## Synthetic Data (for testing without APIs)

In the sidebar under **🧪 Synthetic Test Data**:
- Choose 50–500 reviews
- Set date spread (7–90 days)
- Click **⚡ Generate** — loads instantly, no API calls
- Includes deliberate sentiment dip (days 20–40) to test trend detection

---

## Real App Store Integration

**Google Play** (optional):
```bash
pip install google-play-scraper
```
Then set `GOOGLE_PLAY_APP_ID` in `.env` to your app's package name.

**Apple App Store** — works out of the box via public RSS feed. Set `APPSTORE_APP_ID` to your numeric app ID (find it in the App Store URL).

**CSV Survey Export** — upload any CSV with a `text` column via the sidebar uploader. Optional columns: `rating`, `date`, `author`.

---

## PDF Report

Click **📄 Download PDF Report** in the Overview or AI Insights tab. The report includes:
- Executive KPI table (total, positive %, negative %, avg score, critical count)
- Source breakdown
- Top 8 critical bugs with priority labels
- Top 6 feature requests
- Top 8 trending topics with frequency bars

---

## Skills Demonstrated

`API Integration` · `Web Scraping` · `Sentiment Analysis` · `Streamlit` · `PDF Generation` · `Data Aggregation` · `Trend Analysis`