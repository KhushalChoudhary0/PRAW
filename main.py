import asyncio
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import httpx

# ─── FastAPI App ──────────────────────────────────────────────────────────────
app = FastAPI(title="Stealth Reddit OSINT Microservice")

templates = Jinja2Templates(directory="templates")

# ─── Pydantic request model ──────────────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    query: str

# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

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

def _clean_keywords(query: str) -> str:
    """Basic extraction/cleaning of query string for URL usage."""
    # Remove basic punctuation and extra spaces
    import re
    cleaned = re.sub(r'[^\w\s]', '', query)
    return "%20".join(cleaned.split())

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={},
    )

# ─── Analysis API ─────────────────────────────────────────────────────────────
@app.post("/api/analyze")
async def analyze(request: Request, payload: AnalyzeRequest):
    """
    Direct HTTP scraping endpoint using Reddit's JSON feed.
    Builds both Lead Feed and Timeline synchronously from the same payload.
    """

    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="Query cannot be empty.")

    keywords = _clean_keywords(query)
    
    # Reddit search API URL
    url = f"https://www.reddit.com/search.json?q={keywords}&limit=100&sort=new"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 StealthOSINT/1.0"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            return JSONResponse(status_code=429, content={"detail": "Reddit is rate-limiting us right now. Please try again in a moment."})
        return JSONResponse(status_code=e.response.status_code, content={"detail": f"Unable to fetch live data from Reddit ({e.response.status_code})."})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": "Unable to fetch live data from Reddit right now. Please try again in a moment."})

    # Prepare datasets
    now = datetime(2026, 5, 17, tzinfo=timezone.utc) # Fixed date reference for this sandbox environment
    cutoff_30_days = now - timedelta(days=30)
    
    lead_feed = []
    
    timeline_buckets = {
        "2026": {"concerns": 0, "demand": 0, "links": []},
        "2025": {"concerns": 0, "demand": 0, "links": []},
        "2024": {"concerns": 0, "demand": 0, "links": []},
        "Older": {"concerns": 0, "demand": 0, "links": []}
    }
    
    # Keyword sets for pattern matching
    concern_words = {"pain", "numb", "problem", "issue", "hurt", "bother"}
    demand_words = {"looking for", "recommend", "massager", "buy", "hire", "service", "solution"}

    children = data.get("data", {}).get("children", [])
    
    for child in children:
        post = child.get("data", {})
        
        # Extract fields
        title = post.get("title", "")
        selftext = post.get("selftext", "")
        subreddit = post.get("subreddit_name_prefixed", "")
        created_utc = post.get("created_utc", 0)
        permalink = post.get("permalink", "")
        author = post.get("author", "[deleted]")
        
        full_text = f"{title} {selftext}".lower()
        absolute_url = f"https://www.reddit.com{permalink}"
        post_time = datetime.fromtimestamp(created_utc, tz=timezone.utc)
        
        # 1. Lead Feed logic
        if post_time >= cutoff_30_days:
            snippet = selftext[:200] + "..." if selftext else title
            lead_feed.append({
                "title": title[:80] + ("..." if len(title) > 80 else ""),
                "snippet": snippet,
                "subreddit": subreddit,
                "author": f"u/{author}",
                "url": absolute_url,
                "created_utc": post_time.isoformat(),
                "relative_time": _relative_time(post_time, now)
            })
            
        # 2. Timeline logic
        year = str(post_time.year)
        if year not in ["2026", "2025", "2024"]:
            year = "Older"
            
        # Lightweight pattern matching
        has_concern = any(word in full_text for word in concern_words)
        has_demand = any(word in full_text for word in demand_words)
        
        if has_concern:
            timeline_buckets[year]["concerns"] += 1
        if has_demand:
            timeline_buckets[year]["demand"] += 1
            
        # Store top 3 links per year block
        if len(timeline_buckets[year]["links"]) < 3:
            timeline_buckets[year]["links"].append({
                "title": f"[{post_time.year}] {title[:60]}...",
                "subreddit": subreddit,
                "url": absolute_url,
                "year": post_time.year
            })

    # Sort lead feed newest to oldest
    lead_feed.sort(key=lambda x: x["created_utc"], reverse=True)
    
    # Format timeline output
    timeline_output = []
    labels = {
        "2026": ("Current Year", "emerald"),
        "2025": ("Last Year", "sky"),
        "2024": ("Two Years Ago", "violet"),
        "Older": ("Older Historical Data", "slate")
    }
    
    for yr in ["2026", "2025", "2024", "Older"]:
        bucket = timeline_buckets[yr]
        timeline_output.append({
            "year": yr,
            "label": labels[yr][0],
            "color": labels[yr][1],
            "metrics": [
                {"label": "Concerns / Complaints", "value": bucket["concerns"]},
                {"label": "Product / Service Demand", "value": bucket["demand"]}
            ],
            "historical_threads": bucket["links"]
        })

    return JSONResponse(content={
        "query": query, 
        "data": {
            "lead_feed": lead_feed,
            "timeline": timeline_output
        }
    })
