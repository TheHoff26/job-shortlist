"""Fetchers for public job-board feeds.

Each fetcher returns a list of `Job` objects. All endpoints are the public,
keyless feeds that power the companies' own careers pages:

  greenhouse       GET  https://boards-api.greenhouse.io/v1/boards/{id}/jobs?content=true
  lever            GET  https://api.lever.co/v0/postings/{id}?mode=json  (EU host as fallback)
  ashby            GET  https://api.ashbyhq.com/posting-api/job-board/{id}?includeCompensation=true
  smartrecruiters  GET  https://api.smartrecruiters.com/v1/companies/{id}/postings (+ detail)
  personio         GET  https://{id}.jobs.personio.de/xml?language=en  (.com as fallback)
  recruitee        GET  https://{id}.recruitee.com/api/offers/
  workday          POST https://{host}/wday/cxs/{tenant}/{site}/jobs  (+ detail; unofficial)
"""

from __future__ import annotations

import html
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Callable, Iterable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import Job

log = logging.getLogger("jobfinder")

TitleFilter = Callable[[str], bool]
TIMEOUT = 30
USER_AGENT = "jobfinder/1.0 (personal job search; +https://github.com)"


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "POST"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def _date(value) -> str:
    """Normalise timestamps (ISO strings or epoch ms) to YYYY-MM-DD."""
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
        text = str(value).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return str(value)[:10]


def _workplace(value: str | None, remote_flag: bool | None = None) -> str:
    v = (value or "").lower().replace("-", "").replace("_", "").replace(" ", "")
    if "remote" in v:
        return "remote"
    if "hybrid" in v:
        return "hybrid"
    if v in ("onsite", "office", "inoffice"):
        return "onsite"
    if remote_flag:
        return "remote"
    return ""


def _json(session: requests.Session, url: str, **kw):
    r = session.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "")
    if "json" not in ctype and not r.text.lstrip().startswith(("{", "[")):
        raise ValueError(f"Expected JSON from {url}, got {ctype or 'unknown content'}")
    return r.json()


# --------------------------------------------------------------------------- #
# Greenhouse
# --------------------------------------------------------------------------- #
def fetch_greenhouse(session, company: dict, keep: TitleFilter) -> list[Job]:
    board = company["id"]
    data = _json(session, f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                 params={"content": "true"})
    jobs = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not keep(title):
            continue
        offices = ", ".join(o.get("name", "") for o in j.get("offices", []) if o.get("name"))
        jobs.append(Job(
            company=company["name"], ats="greenhouse", job_id=str(j.get("id")),
            title=title, url=j.get("absolute_url", ""),
            location=(j.get("location") or {}).get("name", "") or offices,
            department=", ".join(d.get("name", "") for d in j.get("departments", []) if d.get("name")),
            description_html=html.unescape(j.get("content", "") or ""),
            posted=_date(j.get("first_published") or j.get("updated_at")),
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Lever (global host, EU host as fallback)
# --------------------------------------------------------------------------- #
def fetch_lever(session, company: dict, keep: TitleFilter) -> list[Job]:
    site = company["id"]
    postings: list = []
    for host in ("https://api.lever.co", "https://api.eu.lever.co"):
        try:
            postings, skip = [], 0
            while True:
                page = _json(session, f"{host}/v0/postings/{site}",
                             params={"mode": "json", "skip": skip, "limit": 100})
                if not isinstance(page, list):
                    break
                postings.extend(page)
                if len(page) < 100:
                    break
                skip += 100
            if postings:
                break
        except (requests.HTTPError, ValueError) as exc:
            log.debug("Lever %s on %s: %s", site, host, exc)
    jobs = []
    for p in postings:
        title = p.get("text", "")
        if not keep(title):
            continue
        cat = p.get("categories") or {}
        parts = [p.get("description", "") or p.get("openingPlain", "")]
        for lst in p.get("lists", []) or []:
            parts.append(f"<h3>{lst.get('text', '')}</h3><ul>{lst.get('content', '')}</ul>")
        parts.append(p.get("additional", "") or "")
        sal = p.get("salaryRange") or {}
        salary = ""
        if sal.get("min") and sal.get("max"):
            salary = f"{sal['min']:,}–{sal['max']:,} {sal.get('currency', '')} {sal.get('interval', '')}".strip()
        jobs.append(Job(
            company=company["name"], ats="lever", job_id=str(p.get("id")),
            title=title, url=p.get("hostedUrl", ""),
            location=cat.get("location", "") or ", ".join(cat.get("allLocations", []) or []),
            department=cat.get("team", "") or cat.get("department", ""),
            description_html="\n".join(parts),
            posted=_date(p.get("createdAt")),
            workplace=_workplace(p.get("workplaceType")),
            employment_type=cat.get("commitment", ""),
            salary=salary,
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Ashby
# --------------------------------------------------------------------------- #
def fetch_ashby(session, company: dict, keep: TitleFilter) -> list[Job]:
    board = company["id"]
    data = _json(session, f"https://api.ashbyhq.com/posting-api/job-board/{board}",
                 params={"includeCompensation": "true"})
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        title = j.get("title", "")
        if not keep(title):
            continue
        locs = [j.get("location", "")] + [
            (s.get("location") if isinstance(s, dict) else str(s))
            for s in j.get("secondaryLocations", []) or []]
        comp = j.get("compensation") or {}
        jobs.append(Job(
            company=company["name"], ats="ashby", job_id=str(j.get("id")),
            title=title, url=j.get("jobUrl", "") or j.get("applyUrl", ""),
            location=", ".join(l for l in locs if l),
            department=j.get("department", "") or j.get("team", ""),
            description_html=j.get("descriptionHtml", "") or j.get("descriptionPlain", ""),
            posted=_date(j.get("publishedAt")),
            workplace=_workplace(j.get("workplaceType"), j.get("isRemote")),
            employment_type=j.get("employmentType", ""),
            salary=comp.get("compensationTierSummary", "") or comp.get("scrapeableCompensationSalarySummary", ""),
        ))
    return jobs


# --------------------------------------------------------------------------- #
# SmartRecruiters (list + one detail call per *pre-filtered* posting)
# --------------------------------------------------------------------------- #
def fetch_smartrecruiters(session, company: dict, keep: TitleFilter) -> list[Job]:
    cid = company["id"]
    base = f"https://api.smartrecruiters.com/v1/companies/{cid}/postings"
    listing, offset = [], 0
    while True:
        page = _json(session, base, params={"limit": 100, "offset": offset})
        content = page.get("content", [])
        listing.extend(content)
        offset += len(content)
        if not content or offset >= page.get("totalFound", 0) or offset >= 5000:
            break
    jobs = []
    for p in listing:
        title = p.get("name", "")
        if not keep(title):
            continue
        detail = {}
        try:
            detail = _json(session, f"{base}/{p.get('id')}")
        except Exception as exc:  # detail is optional
            log.debug("SmartRecruiters detail failed for %s: %s", p.get("id"), exc)
        sections = ((detail.get("jobAd") or {}).get("sections") or {})
        body = []
        for key in ("jobDescription", "qualifications", "additionalInformation"):
            sec = sections.get(key) or {}
            if sec.get("text"):
                body.append(f"<h3>{sec.get('title', '')}</h3>{sec['text']}")
        loc = p.get("location") or {}
        location = loc.get("fullLocation") or ", ".join(
            x for x in (loc.get("city"), loc.get("country", "").upper()) if x)
        jobs.append(Job(
            company=company["name"], ats="smartrecruiters", job_id=str(p.get("id")),
            title=title,
            url=detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{cid}/{p.get('id')}",
            location=location,
            department=(p.get("department") or {}).get("label", "") or (p.get("function") or {}).get("label", ""),
            description_html="\n".join(body),
            posted=_date(p.get("releasedDate")),
            workplace="remote" if loc.get("remote") else ("hybrid" if loc.get("hybrid") else ""),
            employment_type=(p.get("typeOfEmployment") or {}).get("label", ""),
            seniority_hint=(p.get("experienceLevel") or {}).get("label", ""),
        ))
        time.sleep(0.2)
    return jobs


# --------------------------------------------------------------------------- #
# Personio (XML feed, .de then .com)
# --------------------------------------------------------------------------- #
def _txt(el, tag: str) -> str:
    found = el.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def fetch_personio(session, company: dict, keep: TitleFilter) -> list[Job]:
    slug = company["id"]
    lang = company.get("language", "en")
    root = None
    last_exc: Exception | None = None
    for tld in ("de", "com"):
        try:
            r = session.get(f"https://{slug}.jobs.personio.{tld}/xml",
                            params={"language": lang}, timeout=TIMEOUT,
                            headers={"Accept": "application/xml"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
            break
        except Exception as exc:
            last_exc = exc
    if root is None:
        raise RuntimeError(f"Personio feed not reachable: {last_exc}")
    jobs = []
    for pos in root.iter("position"):
        title = _txt(pos, "name")
        if not keep(title):
            continue
        offices = [_txt(pos, "office")] + [
            (o.text or "").strip() for o in pos.findall("additionalOffices/office")]
        body = []
        for d in pos.findall("jobDescriptions/jobDescription"):
            body.append(f"<h3>{_txt(d, 'name')}</h3>{_txt(d, 'value')}")
        pid = _txt(pos, "id")
        jobs.append(Job(
            company=company["name"], ats="personio", job_id=pid, title=title,
            url=f"https://{slug}.jobs.personio.de/job/{pid}?language={lang}",
            location=", ".join(o for o in offices if o),
            department=_txt(pos, "department"),
            description_html="\n".join(body),
            posted=_date(_txt(pos, "createdAt")),
            employment_type=" / ".join(x for x in (_txt(pos, "employmentType"), _txt(pos, "schedule")) if x),
            seniority_hint=_txt(pos, "seniority"),
            years_hint=_txt(pos, "yearsOfExperience"),
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Recruitee
# --------------------------------------------------------------------------- #
def fetch_recruitee(session, company: dict, keep: TitleFilter) -> list[Job]:
    slug = company["id"]
    data = _json(session, f"https://{slug}.recruitee.com/api/offers/")
    jobs = []
    for o in data.get("offers", []):
        title = o.get("title", "")
        if not keep(title):
            continue
        wp = "remote" if o.get("remote") else "hybrid" if o.get("hybrid") else "onsite" if o.get("on_site") else ""
        sal = o.get("salary") or {}
        salary = ""
        if sal.get("min") and sal.get("max"):
            salary = f"{sal['min']}–{sal['max']} {sal.get('currency') or ''} {sal.get('period') or ''}".strip()
        jobs.append(Job(
            company=company["name"], ats="recruitee", job_id=str(o.get("id")),
            title=title, url=o.get("careers_url", ""),
            location=o.get("location", "") or ", ".join(x for x in (o.get("city"), o.get("country_code")) if x),
            department=o.get("department", "") or "",
            description_html=(o.get("description", "") or "") + "\n" + (
                f"<h3>Requirements</h3>{o['requirements']}" if o.get("requirements") else ""),
            posted=_date(o.get("published_at")),
            workplace=wp,
            employment_type=(o.get("employment_type_code") or "").replace("_", " "),
            salary=salary,
            seniority_hint=(o.get("experience_code") or "").replace("_", " "),
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Workday (unofficial JSON used by the career site itself; experimental)
# --------------------------------------------------------------------------- #
def _workday_parts(url: str) -> tuple[str, str, str]:
    u = urlparse(url)
    host = u.netloc
    tenant = host.split(".")[0]
    segs = [s for s in u.path.split("/") if s]
    if segs and len(segs[0]) in (2, 5) and (segs[0][2:3] in ("-", "") ):
        segs = segs[1:]  # drop locale like en-US
    if not segs:
        raise ValueError("Workday url must include the site name, e.g. .../en-US/MySite")
    return host, tenant, segs[0]


def fetch_workday(session, company: dict, keep: TitleFilter) -> list[Job]:
    host, tenant, site = _workday_parts(company["url"])
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "Accept-Language": "en-US"}
    listing, offset, total = [], 0, None
    while total is None or offset < total:
        r = session.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": 20,
                                              "offset": offset, "searchText": ""},
                         headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        page = r.json()
        if total is None:
            total = min(page.get("total", 0), 2000)
        posts = page.get("jobPostings", [])
        if not posts:
            break
        listing.extend(posts)
        offset += 20
        time.sleep(0.3)
    jobs = []
    for p in listing:
        title = p.get("title", "")
        if not keep(title) or not p.get("externalPath"):
            continue
        info = {}
        try:
            d = _json(session, api + p["externalPath"], headers=headers)
            info = d.get("jobPostingInfo", d)
        except Exception as exc:
            log.debug("Workday detail failed: %s", exc)
        jobs.append(Job(
            company=company["name"], ats="workday",
            job_id=str(info.get("jobReqId") or p["externalPath"].rsplit("_", 1)[-1]),
            title=title,
            url=info.get("externalUrl") or f"https://{host}/{site}{p['externalPath']}",
            location=info.get("location") or p.get("locationsText", ""),
            description_html=info.get("jobDescription", ""),
            posted=_date(info.get("startDate")) if info.get("startDate") else "",
            workplace=_workplace(info.get("remoteType")),
            employment_type=info.get("timeType", ""),
        ))
        time.sleep(0.3)
    return jobs


FETCHERS: dict[str, Callable] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "personio": fetch_personio,
    "recruitee": fetch_recruitee,
    "workday": fetch_workday,
}


def fetch_all(companies: Iterable[dict], keep: TitleFilter,
              session: requests.Session | None = None) -> tuple[list[Job], list[dict]]:
    """Fetch every enabled company. Returns (jobs, status rows for the report)."""
    session = session or make_session()
    all_jobs, status = [], []
    for c in companies:
        if c.get("enabled") is False:
            continue
        ats = (c.get("ats") or "linkedin").lower()
        fetcher = FETCHERS.get(ats)
        row = {"company": c.get("name", "?"), "ats": ats, "ok": False, "count": 0, "error": ""}
        if ats in ("linkedin", "none"):
            row.update(ok=True, error="link only")
            status.append(row)
            continue
        if not fetcher:
            row["error"] = f"unknown ats '{ats}'"
        else:
            try:
                jobs = fetcher(session, c, keep)
                all_jobs.extend(jobs)
                row.update(ok=True, count=len(jobs))
                if not jobs:
                    row["error"] = "no matching open positions (or wrong id)"
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("%s (%s): %s", row["company"], ats, row["error"])
        log.info("%-25s %-16s %s", row["company"], ats,
                 f"{row['count']} jobs" if row["ok"] else "FAILED")
        status.append(row)
    return all_jobs, status
