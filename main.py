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
    if not FIND_BUDGET_SECRET:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization")
    token = authorization[7:]
    if token != FIND_BUDGET_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")


def extract_links_from_dom(nova, base_url: str) -> list[dict]:
    """Use Playwright page.evaluate() to extract REAL href attributes from the DOM.
    This bypasses Nova Act's AI to avoid hallucinated URLs."""

    links = nova.page.evaluate("""() => {
        const results = [];
        const anchors = document.querySelectorAll('a[href]');
        for (const a of anchors) {
            const href = a.href;
            const text = a.textContent.trim();
            const lowerText = text.toLowerCase();
            const lowerHref = href.toLowerCase();
            if (
                lowerText.includes('budget') ||
                lowerText.includes('financial') ||
                lowerText.includes('fiscal') ||
                lowerHref.endsWith('.pdf') ||
                lowerHref.includes('budget')
            ) {
                results.push({ title: text, url: href });
            }
        }
        return results;
    }""")

    return links or []


def check_pdf_size(url: str) -> str:
    """Do a HEAD request to get PDF file size. Returns human-readable size string."""
    import urllib.request
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "BudgetCompass/1.0")
        with urllib.request.urlopen(req, timeout=5) as resp:
            length = resp.headers.get("Content-Length")
            if length:
                mb = int(length) / (1024 * 1024)
                return f"{mb:.1f} MB"
            # Follow redirect and check again
            final_url = resp.url
            if final_url != url:
                req2 = urllib.request.Request(final_url, method="HEAD")
                req2.add_header("User-Agent", "BudgetCompass/1.0")
                with urllib.request.urlopen(req2, timeout=5) as resp2:
                    length2 = resp2.headers.get("Content-Length")
                    if length2:
                        mb = int(length2) / (1024 * 1024)
                        return f"{mb:.1f} MB"
    except Exception:
        pass
    return ""


def run_nova_act_search(city: str, state: str) -> dict:
    """Run Nova Act browser search in a separate thread (sync Playwright)."""
    steps: list[str] = []
    results: list[dict] = []
    city_lower = city.lower().strip()

    # Strategy 1: Check if we have a known budget page URL
    known_url = KNOWN_BUDGET_PAGES.get(city_lower)

    if known_url:
        steps.append(f"Found known budget page for {city}")
        target_url = known_url
    else:
        # Strategy 2: Search Wisconsin Policy Forum
        steps.append(f"No known budget page. Searching Wisconsin Policy Forum...")
        target_url = WPF_SEARCH_URL.format(query=f"budget+brief+{city.replace(' ', '+')}")

    steps.append(f"Nova Act opening browser...")
    steps.append(f"Navigating to {city} government website...")

    with NovaAct(starting_page=target_url, headless=True) as nova:
        steps.append("Page loaded. Scanning for budget documents...")

        # Use DOM extraction (real hrefs) instead of AI extraction (hallucinated)
        pdf_links = extract_links_from_dom(nova, target_url)
        steps.append(f"Found {len(pdf_links)} budget-related links on the page")

        steps.append("Checking file sizes...")
        for link in pdf_links[:8]:
            url = link.get("url", "")
            title = link.get("title", "Budget Document")
            title = " ".join(title.split())
            if not title:
                title = "Budget Document"

            size = check_pdf_size(url)
            if size:
                title = f"{title} ({size})"

            results.append({
                "title": title,
                "url": url,
                "source_page": target_url,
                "file_type": "pdf" if url.lower().endswith(".pdf") else "page",
            })

        # If no budget links found on the page, try clicking into a budget subpage
        if not results and known_url:
            steps.append("No budget PDFs on main page. Looking for budget subpage...")
            try:
                nova.act(
                    f"Look for a link that says 'budget' or 'financial reports' and click it."
                )
                steps.append("Navigated to subpage. Extracting links...")

                pdf_links = extract_links_from_dom(nova, target_url)
                steps.append(f"Found {len(pdf_links)} links on subpage")

                for link in pdf_links[:8]:
                    url = link.get("url", "")
                    title = " ".join(link.get("title", "Budget Document").split())
                    if not title:
                        title = "Budget Document"
                    results.append({
                        "title": title,
                        "url": url,
                        "source_page": target_url,
                        "file_type": "pdf" if url.lower().endswith(".pdf") else "page",
                    })
            except Exception as e:
                steps.append(f"Could not navigate to subpage: {str(e)[:100]}")

    if not results:
        steps.append(f"No budget PDFs found for {city}, {state}")
    else:
        steps.append(f"Done! Found {len(results)} budget documents for {city}")

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
