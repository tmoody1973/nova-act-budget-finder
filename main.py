import os
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

    steps: list[str] = []
    results: list[dict] = []

    try:
        # Stage 1: Google search for budget PDFs
        search_query = f"{city} {state} city budget 2025 2026 PDF site:.gov"
        steps.append(f"Searching Google for '{search_query}'")

        with NovaAct(starting_page="https://www.google.com") as nova:
            nova.act(
                f"Search for: {search_query}. "
                f"Type the query into the search box and press Enter."
            )
            steps.append("Google search complete")

            # Extract top government results
            search_results = nova.act(
                "Look at the search results. Find the top 3 results that link to "
                "government websites (.gov) related to city or municipal budgets. "
                "Return each result's title and URL.",
                schema={
                    "type": "object",
                    "properties": {
                        "results": {
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
                    "required": ["results"],
                },
            )

            gov_urls = []
            if search_results.matches_schema and search_results.parsed_response:
                gov_urls = search_results.parsed_response.get("results", [])
                steps.append(f"Found {len(gov_urls)} government website results")

            # Fallback: try Wisconsin Policy Forum
            if not gov_urls:
                fallback_query = f"site:wispolicyforum.org budget brief {city}"
                steps.append(f"No .gov results. Trying: '{fallback_query}'")
                nova.act(f"Search for: {fallback_query}")

                fallback_results = nova.act(
                    "Find any results linking to PDF budget briefs. "
                    "Return the title and URL for each.",
                    schema={
                        "type": "object",
                        "properties": {
                            "results": {
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
                        "required": ["results"],
                    },
                )
                if fallback_results.matches_schema and fallback_results.parsed_response:
                    gov_urls = fallback_results.parsed_response.get("results", [])
                    steps.append(f"Found {len(gov_urls)} Wisconsin Policy Forum results")

            if not gov_urls:
                steps.append("No budget documents found")
                return FindBudgetResponse(
                    status="no_results",
                    city=city,
                    state=state,
                    results=[],
                    search_steps=steps,
                )

            # Stage 2: Navigate to the best result
            target_url = gov_urls[0]["url"]
            steps.append(f"Navigating to: {target_url}")
            nova.act(f"Navigate to {target_url}")

            # Stage 3: Extract PDF links from the page
            pdf_extraction = nova.act(
                "Look at this page and find all links to PDF documents related to "
                "budgets, financial reports, or budget summaries. "
                "Return the title/text of each link and its full URL. "
                "Only include links that end in .pdf or are clearly PDF downloads.",
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
                    results.append({
                        "title": link.get("title", "Budget Document"),
                        "url": link.get("url", ""),
                        "source_page": target_url,
                        "file_type": "pdf",
                    })
            else:
                steps.append("Could not extract PDF links from the page")
                # Return the search results as fallback
                for r in gov_urls[:3]:
                    results.append({
                        "title": r.get("title", "Budget Page"),
                        "url": r.get("url", ""),
                        "source_page": "google.com",
                        "file_type": "page",
                    })
                steps.append("Returning search result URLs instead")

    except Exception as e:
        steps.append(f"Error: {str(e)}")
        return FindBudgetResponse(
            status="error",
            city=city,
            state=state,
            results=[BudgetResult(**r) for r in results],
            search_steps=steps,
            error=str(e),
        )

    return FindBudgetResponse(
        status="success" if results else "no_results",
        city=city,
        state=state,
        results=[BudgetResult(**r) for r in results],
        search_steps=steps,
    )
