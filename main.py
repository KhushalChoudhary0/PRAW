import os
import json
import asyncio
import re
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import httpx
from dotenv import load_dotenv
import urllib.parse

# Load environment variables from .env if present
load_dotenv()

# ─── FastAPI App ──────────────────────────────────────────────────────────────
app = FastAPI(title="Stealth Reddit OSINT Microservice")

templates = Jinja2Templates(directory="templates")

# ─── Pydantic request model ──────────────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    query: str
    intent_mode: str

# ─────────────────────────────────────────────────────────────────────────────
# GLOBALS & CACHE
# ─────────────────────────────────────────────────────────────────────────────
# In-memory cache for the final JSON response (TTL: 15 minutes)
SEARCH_CACHE = {}

ROUTING_CACHE = {
    "massager": ["biohackers", "painmanagement", "AskOldPeople"],
    "massage": ["biohackers", "painmanagement", "AskOldPeople"],
    "foot": ["biohackers", "painmanagement", "AskOldPeople"],
    "website": ["smallbusiness", "forhire", "startups"],
    "dev": ["smallbusiness", "forhire", "startups"],
    "app": ["smallbusiness", "forhire", "startups"]
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS & CLASSES
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

def _clean_json_response(content: str) -> dict:
    """Robust JSON sanitization helper to strip markdown blocks and filler."""
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    
    if content.endswith("```"):
        content = content[:-3]
    
    start_idx = content.find("{")
    end_idx = content.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
        content = content[start_idx:end_idx+1]
        
    return json.loads(content)

def _translate_query(query: str, intent_mode: str) -> str:
    """Translate natural language into structured Reddit search strings based on intent."""
    # Tokenize words to create an inclusive OR query group
    cleaned = re.sub(r'[^\w\s]', '', query)
    tokens = [t.strip() for t in cleaned.split() if len(t.strip()) > 1]
    keyword_clause = " OR ".join(tokens) if tokens else query
        
    if intent_mode == 'job_search':
        return f'title:({keyword_clause}) AND (hire OR hiring OR "looking for") AND self:yes'
    elif intent_mode == 'hiring_leads':
        return f'title:({keyword_clause}) AND ("for hire" OR available OR portfolio) AND self:yes'
    elif intent_mode == 'lead_generation':
        # Use a cleaner, highly-compatible Reddit search syntax structure
        # This ensures Reddit returns raw matches, preventing API ghost towns
        return f"({keyword_clause}) self:yes"
    elif intent_mode == 'product_discovery':
        return f'title:({keyword_clause}) AND (review OR vs OR best OR alternative) AND self:yes'
    else:
        return f'title:({keyword_clause}) AND self:yes'

class PatternScorer:
    """Fast Python regex engine to bypass LLM counting overhead, tailored by intent."""
    def __init__(self, intent_mode: str):
        self.intent_mode = intent_mode
        
        if intent_mode == 'job_search':
            self.concerns_re = re.compile(r'(struggling|unemployed|laid off|reject|ghosted|tough market)', re.IGNORECASE)
            self.demands_re = re.compile(r'(hiring|open role|position|salary|we are looking|join our team)', re.IGNORECASE)
        elif intent_mode == 'hiring_leads':
            self.concerns_re = re.compile(r'(need to hire|looking to hire|hard to find|who can build)', re.IGNORECASE)
            self.demands_re = re.compile(r'(for hire|portfolio|available|my work|hire me|freelancer ready)', re.IGNORECASE)
        elif intent_mode == 'lead_generation':
            self.concerns_re = re.compile(r'(pain|numb|hurt|sore|problem|issue|ache|discomfort|neuropathy|fail|broken|stuck|error|fix|expensive|waste)', re.IGNORECASE)
            self.demands_re = re.compile(r'(looking for|recommend|suggest|buy|massager|hire|service|builder|developer|budget|agency|freelancer|cost|price|quotes|paying|looking for a)', re.IGNORECASE)
        elif intent_mode == 'product_discovery':
            self.concerns_re = re.compile(r'(review|comparison|vs|worth it|anyone tried|thoughts on)', re.IGNORECASE)
            self.demands_re = re.compile(r'(best|alternative|recommendation|top rated|which one)', re.IGNORECASE)
        else:
            self.concerns_re = re.compile(r'(problem|issue)', re.IGNORECASE)
            self.demands_re = re.compile(r'(buy|looking for)', re.IGNORECASE)

    def score(self, text: str):
        c_matches = len(self.concerns_re.findall(text))
        d_matches = len(self.demands_re.findall(text))
        
        # Negative scoring for competitors in lead_generation
        if self.intent_mode == 'lead_generation':
            competitor_re = re.compile(r'(\[for hire\]|my portfolio|i can build)', re.IGNORECASE)
            if competitor_re.search(text):
                return {"concerns": 0, "demands": 0, "intent_score": -1}
                
        intent_score = c_matches + d_matches
        return {
            "concerns": 1 if c_matches > 0 else 0, # count users, not total words
            "demands": 1 if d_matches > 0 else 0,
            "intent_score": intent_score
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

@app.post("/api/analyze")
async def analyze(request: Request, payload: AnalyzeRequest):
    """
    High-Performance Intent-Driven Endpoint.
    """
    query = payload.query.strip()
    intent_mode = payload.intent_mode.strip()
    
    if not query:
        raise HTTPException(status_code=422, detail="Query cannot be empty.")
    if not intent_mode:
        raise HTTPException(status_code=422, detail="Intent mode must be selected.")

    now = datetime(2026, 5, 17, tzinfo=timezone.utc)
    cache_key = (query, intent_mode)
    
    # ─── 1. Cache Check ───────────────────────────────────────────────────────
    cached = SEARCH_CACHE.get(cache_key)
    if cached:
        cached_time, cached_data = cached
        if (now - cached_time) < timedelta(minutes=15):
            return JSONResponse(content=cached_data)

    nvidia_api_key = os.getenv("NVIDIA_API_KEY", "MISSING_NVIDIA_API_KEY")
    query_lower = query.lower()

    # ─── 2. Smart Intent Router ───────────────────────────────────────────────
    target_subreddits = []
    
    for key, subs in ROUTING_CACHE.items():
        if key in query_lower:
            target_subreddits = subs
            break
            
    if not target_subreddits:
        if nvidia_api_key != "MISSING_NVIDIA_API_KEY":
            stage_1_prompt = f"""Act as a Reddit Data Archeologist. Analyze the user's business intent: "{query}" with mode "{intent_mode}".
Output a strict JSON structure containing:
1. "target_subreddits": An array of exactly 3 specific, highly active subreddits where individuals experiencing this target problem congregate.
CRITICAL RULE: NEVER return generic subreddits like r/all, r/AskReddit, r/Advice, r/pics, or any NSFW subreddits. If you cannot find specific niches, default to ["smallbusiness", "ConsumerReports", "entrepreneur"].
Output ONLY valid JSON."""
            payload_s1 = {
                "model": "meta/llama-3-nemotron-70b-instruct",
                "messages": [{"role": "system", "content": stage_1_prompt}],
                "temperature": 0.1,
                "max_tokens": 150
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
                    target_subreddits = parsed_s1.get("target_subreddits", ["smallbusiness", "ConsumerReports", "entrepreneur"])
                    banned = ["all", "askreddit", "advice", "pics"]
                    target_subreddits = [s for s in target_subreddits if s.lower() not in banned][:3]
                    if not target_subreddits:
                        target_subreddits = ["smallbusiness", "ConsumerReports", "entrepreneur"]
            except Exception:
                target_subreddits = ["smallbusiness", "ConsumerReports", "entrepreneur"]
        else:
            target_subreddits = ["smallbusiness", "ConsumerReports", "entrepreneur"]

    search_string = _translate_query(query, intent_mode)
    encoded_search = urllib.parse.quote(search_string)

    # ─── 3. Parallel Multi-Scrape Fetching ────────────────────────────────────
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 StealthOSINT/4.0"
    }
    
    async def fetch_subreddit(subreddit: str):
        url = f"https://www.reddit.com/r/{subreddit}/search.json?q={encoded_search}&restrict_sr=1&limit=40&sort=new"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, timeout=10.0)
                response.raise_for_status()
                return response.json()
        except Exception:
            return None

    results = await asyncio.gather(*(fetch_subreddit(sub) for sub in target_subreddits))
    
    cutoff_30_days = now - timedelta(days=30)
    raw_posts = []
    seen_ids = set()
    scorer = PatternScorer(intent_mode)
    
    # Timeline buckets prep
    timeline_buckets = {
        "2026": {"concerns": 0, "demand": 0, "links": [], "summary_snippets": []},
        "2025": {"concerns": 0, "demand": 0, "links": [], "summary_snippets": []},
        "2024": {"concerns": 0, "demand": 0, "links": [], "summary_snippets": []},
        "Older": {"concerns": 0, "demand": 0, "links": [], "summary_snippets": []}
    }
    
    lead_feed = []

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

            full_text = f"{title} {selftext}"
            score_data = scorer.score(full_text)
            
            # Local scoring applied to buckets
            timeline_buckets[year]["concerns"] += score_data["concerns"]
            timeline_buckets[year]["demand"] += score_data["demands"]

            if len(timeline_buckets[year]["links"]) < 3:
                timeline_buckets[year]["links"].append({
                    "title": f"[{post_time.year}] {title[:60]}...",
                    "subreddit": subreddit,
                    "url": absolute_url,
                    "year": post_time.year
                })

            # Check if it's a live lead
            if post_time >= cutoff_30_days and score_data["intent_score"] > 0:
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

            raw_posts.append({
                "title": title,
                "text": selftext[:400],
                "year": year
            })

    # Sort lead feed newest to oldest
    lead_feed.sort(key=lambda x: x["created_utc"], reverse=True)

    # ─── 4. Stage 3 LLM (Only qualitative summaries) ──────────────────────────
    if nvidia_api_key != "MISSING_NVIDIA_API_KEY" and raw_posts:
        subset = raw_posts[:50]
        stage_3_prompt = f"""Analyze this Reddit dataset for the query: "{query}" and intent: "{intent_mode}".
Return a strictly minified JSON structure containing:
{{
  "timeline": {{
    "2026": {{"summary_snippets": ["trend 1", "trend 2"]}},
    "2025": {{"summary_snippets": ["trend 1"]}},
    "2024": {{"summary_snippets": []}},
    "Older": {{"summary_snippets": []}}
  }}
}}
Write concise, bulleted semantic syntheses explaining exact shifting consumer patterns for each year. Max 2 items per year. Output ONLY valid JSON."""

        payload_s3 = {
            "model": "meta/llama-3-nemotron-70b-instruct",
            "messages": [
                {"role": "system", "content": stage_3_prompt},
                {"role": "user", "content": json.dumps(subset)}
            ],
            "temperature": 0.2,
            "max_tokens": 1000
        }
        
        try:
            async with httpx.AsyncClient() as client:
                resp_s3 = await client.post(
                    "https://integrate.api.nvidia.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {nvidia_api_key}", "Content-Type": "application/json"},
                    json=payload_s3,
                    timeout=20.0
                )
                if resp_s3.status_code == 200:
                    parsed_s3 = _clean_json_response(resp_s3.json()["choices"][0]["message"]["content"])
                    llm_timeline = parsed_s3.get("timeline", {})
                    for yr in ["2026", "2025", "2024", "Older"]:
                        if yr in llm_timeline:
                            timeline_buckets[yr]["summary_snippets"] = llm_timeline[yr].get("summary_snippets", [])
        except Exception:
            pass 

    # ─── 5. Final Output & Cache Store ────────────────────────────────────────
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
                {"label": "Concerns / Issues", "value": bucket["concerns"]},
                {"label": "Demand / Solutions", "value": bucket["demand"]}
            ],
            "summary_snippets": bucket["summary_snippets"],
            "historical_threads": bucket["links"]
        })

    response_data = {
        "query": query,
        "intent_mode": intent_mode,
        "data": {
            "lead_feed": lead_feed,
            "timeline": timeline_output
        }
    }
    
    SEARCH_CACHE[cache_key] = (now, response_data)

    return JSONResponse(content=response_data)
