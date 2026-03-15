import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from nova_act import NovaAct

app = FastAPI(title="Budget Compass - Nova Act Budget Finder")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

FIND_BUDGET_SECRET = os.environ.get("FIND_BUDGET_SECRET", "")

# Thread pool for running Nova Act (sync Playwright) outside asyncio loop
executor = ThreadPoolExecutor(max_workers=2)

# Known budget page patterns for Wisconsin cities
# Nova Act navigates these directly instead of searching Google (avoids CAPTCHA)
KNOWN_BUDGET_PAGES: dict[str, str] = {
    "green bay": "https://www.greenbaywi.gov/Archive.aspx?AMID=36",
    "madison": "https://www.cityofmadison.com/finance/budget",
    "racine": "https://www.racine-county.com/departments/finance",
    "kenosha": "https://www.kenosha.org/departments/finance",
    "appleton": "https://www.appleton.org/government/finance",
    "eau claire": "https://www.eauclairewi.gov/government/departments-divisions/finance",
    "oshkosh": "https://www.ci.oshkosh.wi.us/Finance/",
    "janesville": "https://www.janesvillewi.gov/departments/finance",
    "waukesha": "https://www.waukesha-wi.gov/departments/finance/",
}

# Wisconsin Policy Forum publishes budget briefs
WPF_SEARCH_URL = "https://wispolicyforum.org/?s={query}&post_type=research"


class FindBudgetRequest(BaseModel):
    city: str
    state: str


class BudgetResult(BaseModel):
    title: str
    url: str
    source_page: str
    file_type: str


class FindBudgetResponse(BaseModel):
    status: str
    city: str
    state: str
    results: list[BudgetResult]
    search_steps: list[str]
    error: Optional[str] = None


def verify_auth(authorization: Optional[str]) -> None:
    """Verify the shared secret from the Vercel proxy."""
    if not FIND_BUDGET_SECRET:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization")
    token = authorization[7:]
    if token != FIND_BUDGET_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")


def run_nova_act_search(city: str, state: str) -> dict:
    """Run Nova Act browser search in a separate thread (sync Playwright)."""
    steps: list[str] = []
    results: list[dict] = []
    city_lower = city.lower().strip()

    # Strategy 1: Check if we have a known budget page URL
    known_url = KNOWN_BUDGET_PAGES.get(city_lower)

    if known_url:
        steps.append(f"Found known budget page for {city}: {known_url}")
        target_url = known_url
    else:
        # Strategy 2: Search Wisconsin Policy Forum
        steps.append(f"No known budget page for {city}. Searching Wisconsin Policy Forum...")
        target_url = WPF_SEARCH_URL.format(query=f"budget+brief+{city.replace(' ', '+')}")

    # Use Nova Act to navigate and extract PDF links
    steps.append(f"Opening browser and navigating to: {target_url}")

    with NovaAct(starting_page=target_url, headless=True) as nova:
        steps.append("Page loaded. Looking for budget PDF links...")

        # Extract PDF links from the page
        pdf_extraction = nova.act(
            f"Look at this page carefully. Find all links to PDF documents related to "
            f"budgets, financial reports, budget summaries, or budget briefs for {city}. "
            f"Also look for links that say 'download', 'view PDF', or 'budget document'. "
            f"Return the title/text of each link and its full URL. "
            f"Include links that end in .pdf or point to document downloads.",
            schema={
                "type": "object",
                "properties": {
                    "pdf_links": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "url": {"type": "string"},
                            },
                            "required": ["title", "url"],
                        },
                    }
                },
                "required": ["pdf_links"],
            },
        )

        if pdf_extraction.matches_schema and pdf_extraction.parsed_response:
            pdf_links = pdf_extraction.parsed_response.get("pdf_links", [])
            steps.append(f"Found {len(pdf_links)} PDF links on the page")

            for link in pdf_links[:5]:
                url = link.get("url", "")
                results.append({
                    "title": link.get("title", "Budget Document"),
                    "url": url,
                    "source_page": target_url,
                    "file_type": "pdf" if url.lower().endswith(".pdf") else "page",
                })
        else:
            steps.append("Could not extract PDF links from the page")

        # If no PDFs found on known page, try navigating deeper
        if not results and known_url:
            steps.append("No PDFs found on main page. Looking for budget subpages...")
            nova.act(
                f"Look for any links on this page that contain the word 'budget' "
                f"and click on the most relevant one."
            )

            # Try extraction again on the subpage
            pdf_extraction_2 = nova.act(
                "Find all PDF links on this page related to budgets or financial documents. "
                "Return the title and URL for each.",
                schema={
                    "type": "object",
                    "properties": {
                        "pdf_links": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "url": {"type": "string"},
                                },
                                "required": ["title", "url"],
                            },
                        }
                    },
                    "required": ["pdf_links"],
                },
            )

            if pdf_extraction_2.matches_schema and pdf_extraction_2.parsed_response:
                pdf_links = pdf_extraction_2.parsed_response.get("pdf_links", [])
                steps.append(f"Found {len(pdf_links)} PDF links on subpage")

                for link in pdf_links[:5]:
                    url = link.get("url", "")
                    results.append({
                        "title": link.get("title", "Budget Document"),
                        "url": url,
                        "source_page": target_url,
                        "file_type": "pdf" if url.lower().endswith(".pdf") else "page",
                    })

    if not results:
        steps.append(f"No budget PDFs found for {city}, {state}")

    return {"steps": steps, "results": results, "error": None}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/find-budget", response_model=FindBudgetResponse)
async def find_budget(
    req: FindBudgetRequest,
    authorization: Optional[str] = Header(None),
):
    verify_auth(authorization)

    city = req.city.strip()
    state = req.state.strip().upper()

    if not city or not state:
        raise HTTPException(status_code=400, detail="city and state are required")

    try:
        # Run Nova Act in a thread to avoid async/sync Playwright conflict
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            executor,
            run_nova_act_search,
            city,
            state,
        )

        return FindBudgetResponse(
            status="success" if result["results"] else "no_results",
            city=city,
            state=state,
            results=[BudgetResult(**r) for r in result["results"]],
            search_steps=result["steps"],
            error=result.get("error"),
        )

    except Exception as e:
        return FindBudgetResponse(
            status="error",
            city=city,
            state=state,
            results=[],
            search_steps=[f"Error: {str(e)}"],
            error=str(e),
        )
