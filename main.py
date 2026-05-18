import os
import json
import asyncio
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import httpx
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

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
    
    # Reddit search API URL (Limit 50)
    url = f"https://www.reddit.com/search.json?q={keywords}&limit=50&sort=new"
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

    now = datetime(2026, 5, 17, tzinfo=timezone.utc) # Fixed date reference for this sandbox environment
    cutoff_30_days = now - timedelta(days=30)
    
    children = data.get("data", {}).get("children", [])
    
    # Pre-process raw posts for mapping
    raw_posts = []
    llm_payload_posts = []
    
    for idx, child in enumerate(children):
        post = child.get("data", {})
        title = post.get("title", "")
        selftext = post.get("selftext", "")
        subreddit = post.get("subreddit_name_prefixed", "")
        created_utc = post.get("created_utc", 0)
        permalink = post.get("permalink", "")
        author = post.get("author", "[deleted]")
        
        post_time = datetime.fromtimestamp(created_utc, tz=timezone.utc)
        absolute_url = f"https://www.reddit.com{permalink}"
        
        year = str(post_time.year)
        if year not in ["2026", "2025", "2024"]:
            year = "Older"

        raw_posts.append({
            "idx": idx,
            "title": title,
            "selftext": selftext,
            "subreddit": subreddit,
            "absolute_url": absolute_url,
            "author": author,
            "post_time": post_time,
            "year": year
        })
        
        llm_payload_posts.append({
            "index": idx,
            "title": title,
            "text": selftext[:500], # Limit text payload to conserve tokens
            "year": year
        })

    # Prepare default timeline structure
    timeline_buckets = {
        "2026": {"concerns": 0, "demand": 0, "links": [], "summary_snippets": []},
        "2025": {"concerns": 0, "demand": 0, "links": [], "summary_snippets": []},
        "2024": {"concerns": 0, "demand": 0, "links": [], "summary_snippets": []},
        "Older": {"concerns": 0, "demand": 0, "links": [], "summary_snippets": []}
    }

    lead_feed = []
    
    # Store top 3 links for ALL blocks regardless of method
    for post in raw_posts:
        yr = post["year"]
        if len(timeline_buckets[yr]["links"]) < 3:
            timeline_buckets[yr]["links"].append({
                "title": f"[{post['post_time'].year}] {post['title'][:60]}...",
                "subreddit": post["subreddit"],
                "url": post["absolute_url"],
                "year": post["post_time"].year
            })

    nvidia_api_key = os.getenv("NVIDIA_API_KEY", "MISSING_NVIDIA_API_KEY")
    use_fallback = False
    
    if nvidia_api_key == "MISSING_NVIDIA_API_KEY" or not children:
        use_fallback = True
    else:
        # LLM CALL
        system_prompt = f"""You are a strict Market Intelligence & Intent Extraction system. 
Analyze the provided batch of Reddit posts against the user's query context: "{query}".
Return a strictly minified JSON structure containing:
1. "live_leads": An array of integer "index" values identifying posts created within the past 30 days that show actual purchase intent, hiring intent, or an immediate addressable business problem. Filter out unrelated conversational gossip or spam.
2. "timeline": A dictionary with keys strictly "2026", "2025", "2024", and "Older". For each year, estimate the approximate macro market size using your pre-trained knowledge and extrapolate realistic historical and current macroeconomic numbers based on the trend in the sample data. Do not just count the sample size.
   - "concerns_count": Integer, estimated approximate macro total number of users venting about a relevant pain point, discomfort, or operational problem for that year (e.g. 2999 for 2026, 2000 for 2025).
   - "demands_count": Integer, estimated approximate macro total number of users explicitly asking for a product recommendation, service, or tool for that year.
   - "summary_snippets": An array of strings. Concise, bulleted semantic synthesis of exactly what the core consumer complaints or trends looked like during that specific year. Make it maximum 2 items per year.

Output only valid JSON without markdown wrapping or extra conversational text."""

        llm_payload = {
            "model": "meta/llama-3-nemotron-70b-instruct",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(llm_payload_posts)}
            ],
            "temperature": 0.2,
            "max_tokens": 1500
        }
        
        try:
            async with httpx.AsyncClient() as client:
                llm_resp = await client.post(
                    "https://integrate.api.nvidia.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {nvidia_api_key}", "Content-Type": "application/json"},
                    json=llm_payload,
                    timeout=25.0
                )
                llm_resp.raise_for_status()
                llm_data = llm_resp.json()
                
                content = llm_data["choices"][0]["message"]["content"].strip()
                if content.startswith("```json"):
                    content = content[7:-3].strip()
                elif content.startswith("```"):
                    content = content[3:-3].strip()
                    
                parsed = json.loads(content)
                
                llm_leads = set(parsed.get("live_leads", []))
                
                # Apply LLM Leads mapped to actual posts
                for post in raw_posts:
                    if post["idx"] in llm_leads and post["post_time"] >= cutoff_30_days:
                        snippet = post["selftext"][:200] + "..." if post["selftext"] else post["title"]
                        lead_feed.append({
                            "title": post["title"][:80] + ("..." if len(post["title"]) > 80 else ""),
                            "snippet": snippet,
                            "subreddit": post["subreddit"],
                            "author": f"u/{post['author']}",
                            "url": post["absolute_url"],
                            "created_utc": post["post_time"].isoformat(),
                            "relative_time": _relative_time(post["post_time"], now)
                        })
                
                # Apply LLM Timeline
                llm_timeline = parsed.get("timeline", {})
                for yr in ["2026", "2025", "2024", "Older"]:
                    if yr in llm_timeline:
                        timeline_buckets[yr]["concerns"] = llm_timeline[yr].get("concerns_count", 0)
                        timeline_buckets[yr]["demand"] = llm_timeline[yr].get("demands_count", 0)
                        timeline_buckets[yr]["summary_snippets"] = llm_timeline[yr].get("summary_snippets", [])
                        
        except Exception as e:
            # Fallback on LLM failure
            use_fallback = True

    if use_fallback:
        # Native Keyword matching
        concern_words = {"pain", "numb", "problem", "issue", "hurt", "bother"}
        demand_words = {"looking for", "recommend", "massager", "buy", "hire", "service", "solution"}
        
        for post in raw_posts:
            full_text = f"{post['title']} {post['selftext']}".lower()
            
            if post["post_time"] >= cutoff_30_days:
                snippet = post["selftext"][:200] + "..." if post["selftext"] else post["title"]
                lead_feed.append({
                    "title": post["title"][:80] + ("..." if len(post["title"]) > 80 else ""),
                    "snippet": snippet,
                    "subreddit": post["subreddit"],
                    "author": f"u/{post['author']}",
                    "url": post["absolute_url"],
                    "created_utc": post["post_time"].isoformat(),
                    "relative_time": _relative_time(post["post_time"], now)
                })
                
            yr = post["year"]
            if any(word in full_text for word in concern_words):
                timeline_buckets[yr]["concerns"] += 1
            if any(word in full_text for word in demand_words):
                timeline_buckets[yr]["demand"] += 1

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
            "summary_snippets": bucket["summary_snippets"],
            "historical_threads": bucket["links"]
        })

    return JSONResponse(content={
        "query": query, 
        "data": {
            "lead_feed": lead_feed,
            "timeline": timeline_output
        }
    })
