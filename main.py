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
    import re
    cleaned = re.sub(r'[^\w\s]', '', query)
    return "%20".join(cleaned.split())

def _clean_json_response(content: str) -> dict:
    """Robust JSON sanitization helper to strip markdown blocks and filler."""
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    
    if content.endswith("```"):
        content = content[:-3]
    
    # Try to extract just the JSON object if there's trailing/leading text
    start_idx = content.find("{")
    end_idx = content.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
        content = content[start_idx:end_idx+1]
        
    return json.loads(content)

# ─────────────────────────────────────────────────────────────────────────────
# ROUTING CACHE (Stage 1 Bypass)
# ─────────────────────────────────────────────────────────────────────────────
ROUTING_CACHE = {
    "massager": ["biohackers", "painmanagement", "AskOldPeople"],
    "massage": ["biohackers", "painmanagement", "AskOldPeople"],
    "foot": ["biohackers", "painmanagement", "AskOldPeople"],
    "website": ["smallbusiness", "forhire", "startups"],
    "dev": ["smallbusiness", "forhire", "startups"],
    "app": ["smallbusiness", "forhire", "startups"]
}

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
    Two-Stage AI Workflow Direct HTTP scraping endpoint.
    """
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="Query cannot be empty.")

    nvidia_api_key = os.getenv("NVIDIA_API_KEY", "MISSING_NVIDIA_API_KEY")
    query_lower = query.lower()

    # =========================================================================
    # STAGE 1: SMART INTENT ROUTER
    # =========================================================================
    target_subreddits = []
    search_keywords = _clean_keywords(query)
    
    # Check cache first
    for key, subs in ROUTING_CACHE.items():
        if key in query_lower:
            target_subreddits = subs
            break
            
    if not target_subreddits:
        if nvidia_api_key != "MISSING_NVIDIA_API_KEY":
            stage_1_prompt = f"""Act as a Reddit Data Archeologist. Analyze the user's business intent, product, or service offering: "{query}".
Output a strict JSON structure containing:
1. "keywords": A refined, clean, boolean-like query string (e.g., "foot pain OR massager OR numbness").
2. "target_subreddits": An array of exactly 3 specific, highly active subreddits where individuals experiencing this target problem or looking for this service congregate. 
CRITICAL RULE: NEVER return generic subreddits like r/all, r/AskReddit, r/Advice, r/pics, or any NSFW subreddits. If you cannot find specific niches, default to ["smallbusiness", "ConsumerReports", "entrepreneur"].
Output ONLY valid JSON. No conversational text."""
            
            payload_s1 = {
                "model": "meta/llama-3-nemotron-70b-instruct",
                "messages": [{"role": "system", "content": stage_1_prompt}],
                "temperature": 0.1,
                "max_tokens": 300
            }
            try:
                async with httpx.AsyncClient() as client:
                    resp_s1 = await client.post(
                        "https://integrate.api.nvidia.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {nvidia_api_key}", "Content-Type": "application/json"},
                        json=payload_s1,
                        timeout=15.0
                    )
                    resp_s1.raise_for_status()
                    content_s1 = resp_s1.json()["choices"][0]["message"]["content"]
                    parsed_s1 = _clean_json_response(content_s1)
                    search_keywords = parsed_s1.get("keywords", search_keywords)
                    target_subreddits = parsed_s1.get("target_subreddits", ["smallbusiness", "ConsumerReports", "entrepreneur"])
                    # Ensure no generics
                    banned = ["all", "askreddit", "advice", "pics"]
                    target_subreddits = [s for s in target_subreddits if s.lower() not in banned][:3]
                    if len(target_subreddits) == 0:
                        target_subreddits = ["smallbusiness", "ConsumerReports", "entrepreneur"]
            except Exception as e:
                # Stage 1 failed, fallback
                target_subreddits = ["all"]
        else:
            target_subreddits = ["all"]

    # =========================================================================
    # STAGE 2: PARALLEL TARGETED MULTI-SCRAPE FETCHING
    # =========================================================================
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 StealthOSINT/2.0"
    }
    
    async def fetch_subreddit(subreddit: str):
        # Fallback to search.json for global if subreddit is "all"
        if subreddit.lower() == "all":
            url = f"https://www.reddit.com/search.json?q={search_keywords}&limit=40&sort=new"
        else:
            url = f"https://www.reddit.com/r/{subreddit}/search.json?q={search_keywords}&restrict_sr=1&limit=40&sort=new"
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, timeout=10.0)
                response.raise_for_status()
                return response.json()
        except Exception:
            return None

    results = await asyncio.gather(*(fetch_subreddit(sub) for sub in target_subreddits))
    
    now = datetime(2026, 5, 17, tzinfo=timezone.utc) # Fixed date reference for this sandbox environment
    cutoff_30_days = now - timedelta(days=30)
    
    raw_posts = []
    seen_ids = set()
    
    for result in results:
        if not result: continue
        children = result.get("data", {}).get("children", [])
        for child in children:
            post = child.get("data", {})
            post_id = post.get("id")
            if not post_id or post_id in seen_ids:
                continue
                
            seen_ids.add(post_id)
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
                "id": post_id,
                "title": title,
                "selftext": selftext,
                "subreddit": subreddit,
                "absolute_url": absolute_url,
                "author": author,
                "post_time": post_time,
                "year": year
            })

    # Prepare LLM payload (limit payload size)
    llm_payload_posts = []
    for idx, post in enumerate(raw_posts[:70]): # limit to 70 posts as specified previously
        post["idx"] = idx
        llm_payload_posts.append({
            "index": idx,
            "title": post["title"],
            "text": post["selftext"][:400],
            "year": post["year"]
        })

    # Prepare default timeline structure
    timeline_buckets = {
        "2026": {"concerns": 0, "demand": 0, "links": [], "summary_snippets": []},
        "2025": {"concerns": 0, "demand": 0, "links": [], "summary_snippets": []},
        "2024": {"concerns": 0, "demand": 0, "links": [], "summary_snippets": []},
        "Older": {"concerns": 0, "demand": 0, "links": [], "summary_snippets": []}
    }

    lead_feed = []
    
    # Store top 3 links for ALL blocks
    for post in raw_posts:
        yr = post["year"]
        if len(timeline_buckets[yr]["links"]) < 3:
            timeline_buckets[yr]["links"].append({
                "title": f"[{post['post_time'].year}] {post['title'][:60]}...",
                "subreddit": post["subreddit"],
                "url": post["absolute_url"],
                "year": post["post_time"].year
            })

    # =========================================================================
    # STAGE 3: SEMANTIC METRIC SYNTHESIS
    # =========================================================================
    use_fallback = False
    if nvidia_api_key == "MISSING_NVIDIA_API_KEY" or not raw_posts:
        use_fallback = True
    else:
        stage_3_prompt = f"""You are a strict Market Intelligence & Intent Extraction system. 
Analyze the provided batch of Reddit posts against the user's query context: "{query}".
Return a strictly minified JSON structure containing:
1. "live_leads": An array of integer "index" values identifying posts created within the past 30 days that show actual purchase intent, hiring intent, or an immediate addressable business problem. Filter out unrelated conversational gossip or spam.
2. "timeline": A dictionary with keys strictly "2026", "2025", "2024", and "Older". For each year, estimate the approximate macro market size using your pre-trained knowledge and extrapolate realistic historical and current macroeconomic numbers based on the trend in the sample data. Do not just count the sample size.
   - "concerns_count": Integer, estimated approximate macro total number of users venting about a relevant pain point, discomfort, or operational problem for that year (e.g. 2999 for 2026, 2000 for 2025).
   - "demands_count": Integer, estimated approximate macro total number of users explicitly asking for a product recommendation, service, or tool for that year.
   - "summary_snippets": An array of strings. Concise, bulleted semantic synthesis explaining the exact shifting consumer patterns during that year. Max 2 items per year.

Output ONLY valid JSON without markdown wrapping or extra conversational text."""

        payload_s3 = {
            "model": "meta/llama-3-nemotron-70b-instruct",
            "messages": [
                {"role": "system", "content": stage_3_prompt},
                {"role": "user", "content": json.dumps(llm_payload_posts)}
            ],
            "temperature": 0.2,
            "max_tokens": 1500
        }
        
        try:
            async with httpx.AsyncClient() as client:
                resp_s3 = await client.post(
                    "https://integrate.api.nvidia.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {nvidia_api_key}", "Content-Type": "application/json"},
                    json=payload_s3,
                    timeout=25.0
                )
                resp_s3.raise_for_status()
                content_s3 = resp_s3.json()["choices"][0]["message"]["content"]
                parsed_s3 = _clean_json_response(content_s3)
                
                llm_leads = set(parsed_s3.get("live_leads", []))
                
                # Apply LLM Leads mapped to actual posts
                for post in raw_posts:
                    if post.get("idx") in llm_leads and post["post_time"] >= cutoff_30_days:
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
                llm_timeline = parsed_s3.get("timeline", {})
                for yr in ["2026", "2025", "2024", "Older"]:
                    if yr in llm_timeline:
                        timeline_buckets[yr]["concerns"] = llm_timeline[yr].get("concerns_count", 0)
                        timeline_buckets[yr]["demand"] = llm_timeline[yr].get("demands_count", 0)
                        timeline_buckets[yr]["summary_snippets"] = llm_timeline[yr].get("summary_snippets", [])
                        
        except Exception as e:
            use_fallback = True

    if use_fallback:
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
