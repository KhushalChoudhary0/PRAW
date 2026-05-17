import os
import secrets
import asyncio
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
import praw
from dotenv import load_dotenv

# ─── Environment ─────────────────────────────────────────────────────────────
load_dotenv()

REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_REDIRECT_URI = os.getenv("REDDIT_REDIRECT_URI")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "web:stealth-scraper-v1:0.1")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY")

# Explicit MOCK_MODE toggle – "true" enables full sandbox simulation
MOCK_MODE = os.getenv("MOCK_MODE", "true").strip().lower() == "true"

if MOCK_MODE:
    print("[SANDBOX] MOCK_MODE is ON -- sandbox simulation active, no Reddit API calls will be made.")

# ─── FastAPI App ──────────────────────────────────────────────────────────────
app = FastAPI(title="Stealth Reddit OSINT Microservice")

app.add_middleware(
    SessionMiddleware,
    secret_key=APP_SECRET_KEY or "fallback-temporary-development-key",
    session_cookie="stealth_session",
    max_age=3600 * 24 * 7,
    same_site="lax",
    https_only=False,  # True in production with SSL
)

templates = Jinja2Templates(directory="templates")


# ─── Pydantic request model ──────────────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    query: str


# ─── PRAW helper ──────────────────────────────────────────────────────────────
def get_praw_client() -> praw.Reddit:
    return praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        redirect_uri=REDDIT_REDIRECT_URI,
        user_agent=REDDIT_USER_AGENT,
    )


# ─────────────────────────────────────────────────────────────────────────────
# MOCK DATA GENERATORS
# ─────────────────────────────────────────────────────────────────────────────

def _mock_lead_feed(query: str) -> list[dict]:
    """Generate 10 realistic lead-feed entries spread over the past 30 days."""
    now = datetime.now(timezone.utc)
    subreddits = [
        "r/webdev", "r/freelance", "r/smallbusiness", "r/Entrepreneur",
        "r/forhire", "r/wedding", "r/weddingplanning", "r/webdesign",
        "r/WordPress", "r/startups",
    ]
    snippets = [
        f"We're getting married in June and desperately need someone who can build a clean wedding website quickly. Budget is flexible.",
        f"Looking for a freelancer to make a simple but elegant site for our upcoming wedding. Any recommendations?",
        f"My fiancée wants a custom RSVP portal for the wedding — can anyone do this affordably?",
        f"Just started a wedding-planning side hustle. I need a 5-page site with booking and payment integration.",
        f"Does anyone know a dev who specializes in wedding vendor directories? Need one built ASAP.",
        f"Our photographer needs a portfolio site with galleries and client proofing. Who builds these?",
        f"Need a wedding invitation site with animations and countdown timer. What's the going rate?",
        f"I run a small wedding-decor shop — looking for someone to build an e-commerce site for me.",
        f"Any recommendations for someone who builds {query.lower()}-related websites? Preferably fast turnaround.",
        f"Searching for a developer experienced in wedding-industry sites. Must support mobile and have good SEO.",
    ]
    leads = []
    for i in range(10):
        post_time = now - timedelta(days=i * 3, hours=i * 2)
        leads.append({
            "title": snippets[i][:80] + ("…" if len(snippets[i]) > 80 else ""),
            "snippet": snippets[i],
            "subreddit": subreddits[i],
            "author": f"u/mock_user_{1000 + i}",
            "url": f"https://www.reddit.com/{subreddits[i]}/comments/mock{i:04d}/",
            "created_utc": post_time.isoformat(),
            "relative_time": _relative_time(post_time, now),
        })
    return leads


def _mock_timeline(query: str) -> dict:
    """Generate a multi-year market-trend timeline for product-oriented queries."""
    return {
        "years": [
            {
                "year": 2026,
                "label": "Current Year",
                "color": "emerald",
                "metrics": [
                    {"label": "Numbness / pain concerns mentioned", "value": "2,999"},
                    {"label": "Users demanding at-home solutions", "value": "900"},
                    {"label": "Explicitly asking for a product", "value": "200"},
                    {"label": "Searching for a specific massager", "value": "24"},
                ],
                "summary": "Explosive demand spike — neuropathy and plantar-fasciitis threads dominate r/ChronicPain, r/elderly, and r/massage.",
            },
            {
                "year": 2025,
                "label": "Last Year",
                "color": "sky",
                "metrics": [
                    {"label": "Foot-pain complaints raised", "value": "2,000"},
                    {"label": "Growing demand for home-care tools", "value": "620"},
                    {"label": "Product recommendation requests", "value": "145"},
                ],
                "summary": "Steady growth in home-wellness interest post-pandemic. Sub-communities around senior care tools gained 40% more subscribers.",
            },
            {
                "year": 2024,
                "label": "Two Years Ago",
                "color": "violet",
                "metrics": [
                    {"label": "Historical references to foot pain", "value": "100"},
                    {"label": "Early DIY remedy discussions", "value": "55"},
                ],
                "summary": "Early-stage market interest. Scattered conversations, mostly in r/AskDocs and r/HealthyAging.",
            },
            {
                "year": "Pre-2024",
                "label": "Older Historical Data",
                "color": "slate",
                "metrics": [
                    {"label": "Baseline keyword appearances", "value": "38"},
                ],
                "summary": "Minimal organic activity. The trend had not yet formed a recognisable pattern on Reddit. First mentions traced to 2021 in r/physicaltherapy.",
            },
        ],
        "historical_threads": [
            {
                "title": "[2026] Best foot massager for elderly parents with neuropathy?",
                "subreddit": "r/ChronicPain",
                "url": "https://www.reddit.com/r/ChronicPain/comments/mock_t1/",
                "year": 2026,
            },
            {
                "title": "[2025] My grandmother swears by heated foot massagers — worth it?",
                "subreddit": "r/elderly",
                "url": "https://www.reddit.com/r/elderly/comments/mock_t2/",
                "year": 2025,
            },
            {
                "title": "[2025] Affordable massagers for plantar fasciitis — recommendations",
                "subreddit": "r/PlantarFasciitis",
                "url": "https://www.reddit.com/r/PlantarFasciitis/comments/mock_t3/",
                "year": 2025,
            },
            {
                "title": "[2024] Has anyone tried EMS foot massagers? Skeptical but curious",
                "subreddit": "r/massage",
                "url": "https://www.reddit.com/r/massage/comments/mock_t4/",
                "year": 2024,
            },
            {
                "title": "[2022] Any evidence that foot massagers help with circulation?",
                "subreddit": "r/physicaltherapy",
                "url": "https://www.reddit.com/r/physicaltherapy/comments/mock_t5/",
                "year": 2022,
            },
        ],
    }


def _relative_time(dt: datetime, now: datetime) -> str:
    """Convert a datetime to a human-readable relative string."""
    diff = now - dt
    secs = int(diff.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs // 86400
    if days == 1:
        return "yesterday"
    return f"{days}d ago"


def _classify_query(query: str) -> str:
    """Decide whether a query is lead-oriented or product/market-oriented."""
    q = query.lower()
    product_keywords = [
        "massager", "foot", "product", "gadget", "device", "cream",
        "supplement", "pillow", "tool", "older", "elderly", "senior",
        "pain", "relief", "health", "wellness", "physical",
    ]
    if any(kw in q for kw in product_keywords):
        return "timeline"
    return "leads"


# ─────────────────────────────────────────────────────────────────────────────
# REAL PRAW ANALYSIS (used when MOCK_MODE is False)
# ─────────────────────────────────────────────────────────────────────────────

def _praw_lead_feed(query: str, refresh_token: str) -> list[dict]:
    """Search Reddit via PRAW for recent posts matching the query (past 30 days)."""
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        refresh_token=refresh_token,
        user_agent=REDDIT_USER_AGENT,
    )
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    results = []

    try:
        for submission in reddit.subreddit("all").search(query, sort="new", time_filter="month", limit=50):
            created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
            if created < cutoff:
                continue
            results.append({
                "title": submission.title[:80] + ("…" if len(submission.title) > 80 else ""),
                "snippet": (submission.selftext[:200] + "…") if submission.selftext else submission.title,
                "subreddit": f"r/{submission.subreddit.display_name}",
                "author": f"u/{submission.author.name}" if submission.author else "u/[deleted]",
                "url": f"https://www.reddit.com{submission.permalink}",
                "created_utc": created.isoformat(),
                "relative_time": _relative_time(created, now),
            })
            if len(results) >= 10:
                break
    except Exception as e:
        print(f"PRAW search error: {e}")

    return results


def _praw_timeline(query: str, refresh_token: str) -> dict:
    """Build a multi-year timeline by bucketing PRAW search results by year."""
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        refresh_token=refresh_token,
        user_agent=REDDIT_USER_AGENT,
    )
    now = datetime.now(timezone.utc)
    current_year = now.year
    buckets: dict[str, list] = {
        str(current_year): [],
        str(current_year - 1): [],
        str(current_year - 2): [],
        "older": [],
    }
    threads_for_display = []

    try:
        for submission in reddit.subreddit("all").search(query, sort="relevance", time_filter="all", limit=200):
            created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
            yr = created.year
            key = str(yr) if str(yr) in buckets else "older"
            buckets[key].append(submission)

            if len(threads_for_display) < 5:
                threads_for_display.append({
                    "title": f"[{yr}] {submission.title[:70]}",
                    "subreddit": f"r/{submission.subreddit.display_name}",
                    "url": f"https://www.reddit.com{submission.permalink}",
                    "year": yr,
                })
    except Exception as e:
        print(f"PRAW timeline error: {e}")

    color_map = {
        str(current_year): "emerald",
        str(current_year - 1): "sky",
        str(current_year - 2): "violet",
        "older": "slate",
    }
    label_map = {
        str(current_year): "Current Year",
        str(current_year - 1): "Last Year",
        str(current_year - 2): "Two Years Ago",
        "older": "Older Historical Data",
    }

    years = []
    for key in [str(current_year), str(current_year - 1), str(current_year - 2), "older"]:
        posts = buckets[key]
        year_label = key if key != "older" else f"Pre-{current_year - 2}"
        years.append({
            "year": year_label,
            "label": label_map[key],
            "color": color_map[key],
            "metrics": [
                {"label": "Total posts found", "value": f"{len(posts):,}"},
                {"label": "With substantive body text", "value": f"{sum(1 for p in posts if p.selftext and len(p.selftext) > 50):,}"},
            ],
            "summary": f"Found {len(posts)} matching submissions in this period." if posts else "No data found for this period.",
        })

    return {"years": years, "historical_threads": threads_for_display}


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request):
    username = request.session.get("username")
    error = request.session.pop("error_msg", None)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "username": username,
            "error": error,
            "mock_mode": MOCK_MODE,
        },
    )


@app.get("/login")
async def login(request: Request):
    # ── Mock Mode: instant sandbox login, no Reddit redirect ──
    if MOCK_MODE:
        request.session["username"] = "Sandbox_Tester"
        request.session["refresh_token"] = "mock_sandbox_token"
        return RedirectResponse(url="/", status_code=303)

    # ── Live Mode: standard OAuth flow ──
    oauth_state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = oauth_state

    if not all([REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_REDIRECT_URI]):
        request.session["error_msg"] = "Reddit API credentials missing. Update your .env file."
        return RedirectResponse(url="/", status_code=303)

    try:
        reddit = get_praw_client()
        auth_url = reddit.auth.url(
            scopes=["identity", "read", "history"],
            state=oauth_state,
            duration="permanent",
        )
        return RedirectResponse(url=auth_url)
    except Exception as e:
        request.session["error_msg"] = f"Reddit Auth init failed: {e}"
        return RedirectResponse(url="/", status_code=303)


@app.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
):
    if error:
        request.session["error_msg"] = f"Reddit Auth Error: {error}"
        return RedirectResponse(url="/", status_code=303)

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing authorization parameters.")

    stored_state = request.session.get("oauth_state")
    if not stored_state or state != stored_state:
        request.session.pop("oauth_state", None)
        raise HTTPException(status_code=403, detail="CSRF state mismatch.")

    request.session.pop("oauth_state", None)

    try:
        reddit = get_praw_client()
        refresh_token = reddit.auth.authorize(code)
        authorized = praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            refresh_token=refresh_token,
            user_agent=REDDIT_USER_AGENT,
        )
        username = authorized.user.me().name
        request.session["username"] = username
        request.session["refresh_token"] = refresh_token
    except Exception as e:
        request.session["error_msg"] = f"Auth callback failed: {e}"
        return RedirectResponse(url="/", status_code=303)

    return RedirectResponse(url="/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


# ─── Analysis API ─────────────────────────────────────────────────────────────
@app.post("/api/analyze")
async def analyze(request: Request, payload: AnalyzeRequest):
    """
    Core analysis endpoint.
    Accepts a query phrase and returns either a lead-feed or a multi-year
    timeline depending on query classification.
    """
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="Query cannot be empty.")

    # Simulate network latency for realistic UX in mock mode
    if MOCK_MODE:
        await asyncio.sleep(1.5)

    result_type = _classify_query(query)

    if MOCK_MODE:
        if result_type == "timeline":
            data = _mock_timeline(query)
        else:
            data = _mock_lead_feed(query)
    else:
        refresh_token = request.session.get("refresh_token")
        if not refresh_token:
            raise HTTPException(status_code=401, detail="Missing refresh token. Please re-login.")
        if result_type == "timeline":
            data = _praw_timeline(query, refresh_token)
        else:
            data = _praw_lead_feed(query, refresh_token)

    return JSONResponse(content={"type": result_type, "query": query, "data": data})
