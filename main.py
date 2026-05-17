import os
import secrets
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import praw
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Environment variable check
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_REDIRECT_URI = os.getenv("REDDIT_REDIRECT_URI")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "web:stealth-scraper-v1:0.1")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY")

# Check if we should fall back to Developer Mock Mode
IS_CREDENTIAL_PLACEHOLDER = REDDIT_CLIENT_ID in ["your_client_id_here", "", None] or REDDIT_CLIENT_SECRET in ["your_client_secret_here", "", None]
MOCK_MODE = IS_CREDENTIAL_PLACEHOLDER

app = FastAPI(title="Stealth Reddit OAuth2 Microservice")

# Configure Session Middleware for lightweight cookie session management
app.add_middleware(
    SessionMiddleware,
    secret_key=APP_SECRET_KEY or "fallback-temporary-development-key",
    session_cookie="stealth_session",
    max_age=3600 * 24 * 7,  # 7 days
    same_site="lax",
    https_only=False,  # Set to True in production with SSL
)

# Set up HTML templates directory
templates = Jinja2Templates(directory="templates")


def get_praw_client() -> praw.Reddit:
    """
    Initializes a new PRAW Reddit instance with standard web/script configs.
    """
    return praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        redirect_uri=REDDIT_REDIRECT_URI,
        user_agent=REDDIT_USER_AGENT
    )


@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request):
    """
    Renders the index template.
    Displays dashboard if authenticated (username in session), otherwise shows login page.
    """
    username = request.session.get("username")
    error = request.session.pop("error_msg", None)  # Retrieve and remove temporary errors
    
    # Let the UI know if mock mode is currently running
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "username": username, 
            "error": error,
            "mock_mode": MOCK_MODE and not username
        }
    )


@app.get("/login")
async def login(request: Request):
    """
    Generates a secure state token, caches it in session to prevent CSRF,
    and redirects the user to the Reddit Authorization endpoint.
    """
    # 1. Generate unique random state token for CSRF protection
    oauth_state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = oauth_state

    # 2. Handle Mock Mode fallback if live API keys are not ready
    if MOCK_MODE:
        print("💡 Developer Info: Running in Mock Mode. Redirecting to callback simulated route...")
        # Direct simulation of redirect loop back to auth callback
        return RedirectResponse(url=f"/auth/callback?code=mock_dev_code&state={oauth_state}")

    if not all([REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_REDIRECT_URI]):
        request.session["error_msg"] = "Server OAuth environment variables not set up. Please edit your .env file."
        return RedirectResponse(url="/", status_code=303)

    # 3. Get real Reddit auth URL
    try:
        reddit = get_praw_client()
        auth_url = reddit.auth.url(
            scopes=["identity", "read", "history"],
            state=oauth_state,
            duration="permanent"
        )
        return RedirectResponse(url=auth_url)
    except Exception as e:
        request.session["error_msg"] = f"Failed to connect to Reddit Auth: {str(e)}"
        return RedirectResponse(url="/", status_code=303)


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = None, state: str = None, error: str = None):
    """
    Processes OAuth2 callback, validates state token against cookie session, 
    exchanges callback code for authorization tokens, and retrieves user profile details.
    """
    if error:
        request.session["error_msg"] = f"Reddit Auth Error: {error}"
        return RedirectResponse(url="/", status_code=303)

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing required authorization parameters.")

    # 1. CSRF Verification Check
    stored_state = request.session.get("oauth_state")
    if not stored_state or state != stored_state:
        request.session.pop("oauth_state", None)
        raise HTTPException(status_code=403, detail="State verification mismatch (CSRF Protection triggered).")

    # Clean up the state token from session after validation
    request.session.pop("oauth_state", None)

    # 2. Simulated Successful Callback in Mock Mode
    if MOCK_MODE:
        request.session["username"] = "StealthDeveloper_Mock"
        request.session["refresh_token"] = "mock_refresh_token_xyz123"
        return RedirectResponse(url="/", status_code=303)

    try:
        # 3. Real Code Exchange for Refresh & Access Tokens
        reddit = get_praw_client()
        refresh_token = reddit.auth.authorize(code)
        
        # Connect as authorized user to verify and obtain identity username
        authorized_reddit = praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            refresh_token=refresh_token,
            user_agent=REDDIT_USER_AGENT
        )
        
        user_me = authorized_reddit.user.me()
        username = user_me.name

        # Save session context in secure HTTP-only cookies
        request.session["username"] = username
        request.session["refresh_token"] = refresh_token
        
    except Exception as e:
        request.session["error_msg"] = f"Failed to authenticate with Reddit API: {str(e)}"
        return RedirectResponse(url="/", status_code=303)

    return RedirectResponse(url="/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    """
    Terminates the local user session.
    """
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
