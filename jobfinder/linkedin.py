"""Pre-filled LinkedIn job-search links (no scraping).

LinkedIn offers no public jobs API and its User Agreement forbids automated
scraping, so the tool does not fetch LinkedIn. Instead it builds a search link
per company that you open yourself:

  https://www.linkedin.com/jobs/search/?keywords=...&location=...&f_TPR=r604800

Parameters used:
  keywords  your titles OR-combined, plus the company name (if no linkedin_id)
  location  profile.linkedin.location (default: first entry of profile.locations)
  f_C       LinkedIn company id, if given as `linkedin_id` in companies.yaml
  f_E       experience levels derived from profile.seniority
  f_TPR     "posted within", from profile.linkedin.posted_within_days
"""

from __future__ import annotations

from urllib.parse import urlencode

BASE = "https://www.linkedin.com/jobs/search/"

# LinkedIn experience-level filter values
_LEVEL_CODES = {
    "intern": ["1"],          # Internship
    "junior": ["2", "3"],     # Entry level, Associate
    "mid": ["3", "4"],        # Associate, Mid-Senior level
    "senior": ["4"],          # Mid-Senior level
    "lead": ["4", "5"],       # Mid-Senior level, Director
    "executive": ["5", "6"],  # Director, Executive
}


def _keywords(titles: list[str], company: str | None) -> str:
    quoted = [f'"{t}"' for t in titles]
    title_part = quoted[0] if len(quoted) == 1 else f"({' OR '.join(quoted)})" if quoted else ""
    parts = [f'"{company}"' if company else "", title_part]
    return " ".join(p for p in parts if p)


def search_url(company: dict, profile: dict) -> str:
    li = profile.get("linkedin") or {}
    company_id = str(company.get("linkedin_id") or "").strip()
    params: dict[str, str] = {
        "keywords": _keywords(profile.get("titles", []), None if company_id else company.get("name")),
    }
    location = li.get("location") or (profile.get("locations") or [""])[0]
    if location:
        params["location"] = location
    if company_id:
        params["f_C"] = company_id
    codes = sorted({c for lvl in profile.get("seniority", []) for c in _LEVEL_CODES.get(lvl, [])})
    if codes:
        params["f_E"] = ",".join(codes)
    days = int(li.get("posted_within_days") or 0)
    if days > 0:
        params["f_TPR"] = f"r{days * 86400}"
    return BASE + "?" + urlencode(params)


def build_links(companies: list[dict], profile: dict) -> list[dict]:
    li = profile.get("linkedin") or {}
    if li.get("enabled") is False:
        return []
    return [{"company": c.get("name", "?"), "url": search_url(c, profile)}
            for c in companies if c.get("enabled") is not False and c.get("linkedin") is not False]
