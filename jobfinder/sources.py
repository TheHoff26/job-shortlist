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
  successfactors   GET  https://{career-site}/services/rss/job/?locale=en_US&keywords=(...)
                   (RSS feed of SAP SuccessFactors career sites; job page as fallback)
  teamtailor       GET  https://{career-site}/jobs.rss
  avature          GET  https://{site}/{locale}/{portal}/SearchJobs/feed/  (RSS; HTML list as fallback)
                        + job page (JSON-LD JobPosting) for matching titles
  easycruit        GET  https://{id}.easycruit.com/export/xml/vacancy/list.xml  (vacancy list page as fallback)
  phenom           GET  https://{site}/{country}/{lang}/search-results?from=N  (JSON embedded in the page)
                        + job page for the description
"""

from __future__ import annotations

import html
import logging
import re
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
    """Normalise timestamps (ISO strings, RFC 822 or epoch ms) to YYYY-MM-DD."""
    if value in (None, ""):
        return ""
    if isinstance(value, str) and "," in value[:5]:  # RFC 822: "Mon, 20 Oct 2026 ..."
        from email.utils import parsedate_to_datetime
        try:
            return parsedate_to_datetime(value).date().isoformat()
        except (TypeError, ValueError):
            pass
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
        text = str(value).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
        m = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})", text)       # 30.11.2026 / 30/11/2026
        if m:
            return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
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
    data, last_exc = None, None
    for host in ("https://boards-api.greenhouse.io", "https://boards-api.eu.greenhouse.io"):
        try:
            data = _json(session, f"{host}/v1/boards/{board}/jobs", params={"content": "true"})
            break
        except (requests.HTTPError, ValueError) as exc:
            last_exc = exc
    if data is None:
        raise RuntimeError(f"Greenhouse board not reachable: {last_exc}")
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
            deadline=_date(o.get("close_at")) if o.get("close_at") else "",
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
    listing, seen = [], set()
    for term in (company.get("_terms") or [""]):     # search per title, then merge
        offset, total = 0, None
        while total is None or offset < total:
            r = session.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": 20,
                                                  "offset": offset, "searchText": term},
                             headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            page = r.json()
            if total is None:
                total = min(page.get("total", 0), 2000)
            posts = page.get("jobPostings", [])
            if not posts:
                break
            for p in posts:
                if p.get("externalPath") and p["externalPath"] not in seen:
                    seen.add(p["externalPath"])
                    listing.append(p)
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
            deadline=_date(info.get("endDate")) if info.get("endDate") else "",
        ))
        time.sleep(0.3)
    return jobs


# --------------------------------------------------------------------------- #
# SAP SuccessFactors career sites (e.g. jobs.neste.com, careers.metso.com)
# --------------------------------------------------------------------------- #
SF_LOCALES = ["en_US", "en_GB", "fi_FI", "sv_SE", "de_DE", "nb_NO", "da_DK"]
_SF_TITLE_LOC = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


def _sf_page_description(session, url: str) -> str:
    """Fetch the job page and pull the description block (fallback)."""
    from bs4 import BeautifulSoup
    r = session.get(url, timeout=TIMEOUT, headers={"Accept": "text/html"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    block = (soup.select_one(".jobdescription") or soup.select_one("[itemprop=description]")
             or soup.select_one(".job-description") or soup.select_one(".jobDisplay"))
    return block.decode_contents() if block else ""


def fetch_successfactors(session, company: dict, keep: TitleFilter) -> list[Job]:
    base = company["url"].rstrip("/")
    u = urlparse(base)
    bases = [base] + ([f"{u.scheme}://{u.netloc}"] if u.path.strip("/") else [])
    # Jobs are published per language setting, so read the feed in several locales
    # (no keyword = all jobs) and filter by title afterwards.
    locales = company.get("locales") or SF_LOCALES
    items: dict[str, dict] = {}
    feed_ok, last_exc = False, None

    def read(b, loc, keywords=""):
        nonlocal feed_ok, last_exc
        try:
            r = session.get(f"{b}/services/rss/job/", timeout=TIMEOUT,
                            params={"locale": loc, "keywords": keywords},
                            headers={"Accept": "application/rss+xml, application/xml, text/xml"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as exc:
            last_exc = exc
            return
        feed_ok = True
        for it in root.iter("item"):
            link = (it.findtext("link") or "").strip()
            ids = re.findall(r"/(\d{5,})/?", link)
            key = ids[-1] if ids else link
            if link and key not in items:
                items[key] = {"link": link, **{k: (it.findtext(k) or "") for k in ("title", "description", "pubDate")}}
        time.sleep(0.3)

    for b in bases:
        for loc in locales:
            read(b, loc)
        if items:
            break
    if feed_ok and not items:                      # some sites only answer keyword searches
        for term in (company.get("_terms") or [])[:40]:
            read(bases[-1], "en_US", f"({term})")
    if not feed_ok:
        raise RuntimeError(f"SuccessFactors RSS feed not reachable: {last_exc}")

    jobs = []
    for _key, it in items.items():
        link = it["link"]
        raw_title = html.unescape(it["title"]).strip()
        m = _SF_TITLE_LOC.match(raw_title)
        title, location = (m.group(1), m.group(2)) if m else (raw_title, "")
        if not keep(title):
            continue
        desc = html.unescape(it["description"])
        if len(re.sub(r"<[^>]+>", "", desc)) < 400:
            try:
                desc = _sf_page_description(session, link) or desc
                time.sleep(0.3)
            except Exception as exc:
                log.debug("SuccessFactors page fetch failed for %s: %s", link, exc)
        job_id = re.findall(r"/(\d{5,})/?", link)
        jobs.append(Job(
            company=company["name"], ats="successfactors",
            job_id=job_id[-1] if job_id else link, title=title, url=link,
            location=location, description_html=desc, posted=_date(it["pubDate"]),
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Teamtailor career sites (public RSS feed at <careers site>/jobs.rss)
# --------------------------------------------------------------------------- #
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _tt_location(it) -> str:
    places = []
    for el in it.iter():
        if _local(el.tag) == "location":
            parts = {_local(c.tag): (c.text or "").strip() for c in el}
            name = ", ".join(x for x in (parts.get("city"), parts.get("country")) if x) or parts.get("name") \
                or (el.text or "").strip()
            if name and name not in places:
                places.append(name)
    return "; ".join(places)


def fetch_teamtailor(session, company: dict, keep: TitleFilter) -> list[Job]:
    base = company["url"].rstrip("/")
    items, offset = [], 0
    while True:
        r = session.get(f"{base}/jobs.rss", params={"per_page": 100, "offset": offset}, timeout=TIMEOUT,
                        headers={"Accept": "application/rss+xml, application/xml, text/xml"})
        r.raise_for_status()
        page = list(ET.fromstring(r.content).iter("item"))
        items.extend(page)
        if len(page) < 100 or offset >= 900:
            break
        offset += 100
    jobs = []
    for it in items:
        title = (it.findtext("title") or "").strip()
        if not keep(title):
            continue
        link = (it.findtext("link") or "").strip()
        extra = {_local(c.tag): (c.text or "").strip() for c in it}
        ids = re.findall(r"/jobs/(\d+)", link)
        remote = (extra.get("remotestatus") or extra.get("remote-status") or extra.get("remote_status") or "").lower()
        jobs.append(Job(
            company=company["name"], ats="teamtailor", job_id=ids[0] if ids else link, title=title, url=link,
            location=_tt_location(it), department=extra.get("department", ""),
            description_html=html.unescape(it.findtext("description") or ""),
            posted=_date(it.findtext("pubDate") or ""),
            workplace=_workplace(remote),
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Shared: schema.org JobPosting (JSON-LD) on a job page
# --------------------------------------------------------------------------- #
def _jsonld_posting(page_html: str) -> dict:
    import json as _json
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page_html, re.S | re.I):
        try:
            data = _json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                if str(d.get("@type", "")).lower() == "jobposting":
                    return d
                stack.extend(v for v in d.values() if isinstance(v, (list, dict)))
            elif isinstance(d, list):
                stack.extend(d)
    return {}


def _posting_location(p: dict) -> str:
    locs = p.get("jobLocation") or []
    locs = locs if isinstance(locs, list) else [locs]
    out = []
    for l in locs:
        a = (l or {}).get("address") or {}
        if isinstance(a, str):
            out.append(a)
            continue
        country = a.get("addressCountry")
        country = country.get("name") if isinstance(country, dict) else country
        name = ", ".join(x for x in (a.get("addressLocality"), country) if x)
        if name and name not in out:
            out.append(name)
    return " / ".join(out)


def _page_description(page_html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page_html, "html.parser")
    for sel in ("[itemprop=description]", ".article__content", ".job-description", ".jobDescription",
                ".section__content", "article", "main"):
        block = soup.select_one(sel)
        if block and len(block.get_text(strip=True)) > 200:
            return block.decode_contents()
    return ""


# --------------------------------------------------------------------------- #
# Avature career sites (e.g. careers.enbw.com/en_US/careers, jobs.siemens-energy.com/en_US/jobs)
# --------------------------------------------------------------------------- #
_AV_ROOT = re.compile(r"^(https?://[^/]+/[a-z]{2}_[A-Z]{2}/[^/?#]+)")


def _avature_root(url: str) -> str:
    m = _AV_ROOT.match(url.strip())
    return m.group(1) if m else url.rstrip("/")


_AV_DETAIL = re.compile(r"/(?:JobDetail|FolderDetail)/", re.I)


def _avature_html_list(session, start: str) -> list[tuple[str, str, str]]:
    """Read the job list pages of an Avature portal (JobDetail or FolderDetail links),
    following the 'next' link (jobOffset or folderOffset paging)."""
    from bs4 import BeautifulSoup
    out, seen, url, pages = [], set(), start, 0
    params = {"jobRecordsPerPage": 100, "folderRecordsPerPage": 100}
    while url and pages < 80:
        r = session.get(url, params=params if pages == 0 else None, timeout=TIMEOUT, headers={"Accept": "text/html"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        new = 0
        for a in soup.select("a[href]"):
            href = requests.compat.urljoin(url, a["href"])
            text = a.get_text(" ", strip=True)
            if not _AV_DETAIL.search(href) or not text or len(text) < 4:
                continue
            key = re.sub(r"[?#].*$", "", href)
            if key in seen:
                continue
            seen.add(key)
            out.append((text, key, ""))
            new += 1
        pages += 1
        nxt = soup.select_one("a.paginationNextLink, a[rel=next], a[aria-label*='Next' i], a[title*='Next' i]")
        if not nxt:
            nxt = next((a for a in soup.select("a[href]") if re.match(r"^\s*(next|weiter|nächste)\b", a.get_text(strip=True), re.I)
                        and re.search(r"(job|folder)Offset=", a["href"])), None)
        url = requests.compat.urljoin(url, nxt["href"]) if nxt and new else None
    return out


def _avature_listing(session, url: str) -> list[tuple[str, str, str]]:
    """(title, url, description) for every open position of an Avature portal."""
    root = _avature_root(url)
    try:
        r = session.get(f"{root}/SearchJobs/feed/", params={"jobRecordsPerPage": 1000}, timeout=TIMEOUT,
                        headers={"Accept": "application/rss+xml, application/xml, text/xml"})
        r.raise_for_status()
        items = list(ET.fromstring(r.content).iter("item"))
        if items:
            return [((it.findtext("title") or "").strip(), (it.findtext("link") or "").strip(),
                     html.unescape(it.findtext("description") or "")) for it in items]
    except (requests.RequestException, ET.ParseError) as e:
        log.info("avature feed not usable for %s (%s) – reading the job list pages", root, e)
    # list page: the configured URL if it is one (e.g. .../jobs/Jobs or .../SearchJobsGermany), else SearchJobs
    starts = [url] if url.rstrip("/") != root else []
    starts += [f"{root}/SearchJobs", f"{root}/Jobs"]
    errors = []
    for start in dict.fromkeys(starts):
        try:
            out = _avature_html_list(session, start)
            if out:
                return out
        except requests.RequestException as e:
            errors.append(f"{start}: {e}")
    raise RuntimeError("Avature: no job list found – " + ("; ".join(errors)[:300] or "list pages had no job links"))


def fetch_avature(session, company: dict, keep: TitleFilter) -> list[Job]:
    jobs = []
    for title, link, desc in _avature_listing(session, company["url"]):
        if not title or not keep(title):
            continue
        ids = re.findall(r"/(?:JobDetail|FolderDetail)/[^/]+/(\d+)", link) or re.findall(r"(\d+)/?$", link)
        posting, page_desc = {}, ""
        try:
            pr = session.get(link, timeout=TIMEOUT, headers={"Accept": "text/html"})
            pr.raise_for_status()
            posting = _jsonld_posting(pr.text)
            if not posting.get("description") and len(desc) < 300:
                page_desc = _page_description(pr.text)
        except requests.RequestException:
            pass
        jobs.append(Job(
            company=company["name"], ats="avature", job_id=ids[0] if ids else link, title=title, url=link,
            location=_posting_location(posting),
            description_html=html.unescape(posting.get("description") or "") or page_desc or desc,
            posted=_date(posting.get("datePosted") or ""),
            employment_type=str(posting.get("employmentType") or "").replace("_", " ").lower(),
            deadline=_date(posting.get("validThrough")) if posting.get("validThrough") else "",
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Phenom career sites (e.g. careers.enbw.com/global/en) – the search page embeds
# its results as JSON ("eagerLoadRefineSearch"); the job page embeds "jobDetail".
# --------------------------------------------------------------------------- #
def _embedded_json(text: str, marker: str):
    i = text.find(f'"{marker}":')
    if i < 0:
        return None
    import json as _json
    try:
        obj, _ = _json.JSONDecoder().raw_decode(text[i + len(marker) + 3:].lstrip())
        return obj
    except ValueError:
        return None


def _phenom_bases(url: str) -> list[str]:
    u = urlparse(url)
    path = re.sub(r"/(search-results|jobs)/?$", "", u.path.rstrip("/"))
    host = f"{u.scheme}://{u.netloc}"
    cands = [host + path] if path else []
    cands += [host + "/global/en", host + "/us/en", host + "/en", host]
    return list(dict.fromkeys(cands))


def fetch_phenom(session, company: dict, keep: TitleFilter) -> list[Job]:
    from urllib.parse import parse_qsl
    url = company["url"]
    extra = dict(parse_qsl(urlparse(url).query))          # e.g. keywords=... from the register
    base, first, errors = None, None, []
    for b in _phenom_bases(url.split("?")[0]):
        try:
            r = session.get(f"{b}/search-results", params={**extra, "from": 0, "s": 1}, timeout=TIMEOUT,
                            headers={"Accept": "text/html"})
            if r.status_code == 200:
                data = _embedded_json(r.text, "eagerLoadRefineSearch")
                if data and (data.get("data") or {}).get("jobs") is not None:
                    base, first = b, data
                    break
            errors.append(f"{b}: HTTP {r.status_code}")
        except requests.RequestException as e:
            errors.append(f"{b}: {e}")
    if not base:
        raise RuntimeError("Phenom: no search results found – " + "; ".join(errors)[:300])
    total = first.get("totalHits") or first.get("hits") or 0
    listing, data, offset, seen = [], first, 0, set()
    while True:
        page = (data.get("data") or {}).get("jobs") or []
        fresh = [j for j in page if (j.get("jobId") or j.get("jobSeqNo")) not in seen]
        if not fresh:
            break
        for j in fresh:
            seen.add(j.get("jobId") or j.get("jobSeqNo"))
        listing.extend(fresh)
        offset += len(page)
        if (total and offset >= total) or offset >= 3000:
            break
        r = session.get(f"{base}/search-results", params={**extra, "from": offset, "s": 1}, timeout=TIMEOUT,
                        headers={"Accept": "text/html"})
        r.raise_for_status()
        data = _embedded_json(r.text, "eagerLoadRefineSearch") or {}
    jobs = []
    for j in listing:
        title = (j.get("title") or "").strip()
        if not title or not keep(title):
            continue
        jid = str(j.get("jobId") or j.get("jobSeqNo") or "")
        link = f"{base}/job/{jid}"
        loc = j.get("location") or j.get("cityStateCountry") or ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        desc, deadline = j.get("descriptionTeaser") or "", ""
        try:
            pr = session.get(link, timeout=TIMEOUT, headers={"Accept": "text/html"})
            if pr.status_code == 200:
                det = ((_embedded_json(pr.text, "jobDetail") or {}).get("data") or {}).get("job") or {}
                desc = det.get("description") or det.get("jobDescription") or desc
                posting = _jsonld_posting(pr.text)
                desc = desc or html.unescape(posting.get("description") or "")
                if posting.get("validThrough"):
                    deadline = _date(posting["validThrough"])
        except requests.RequestException:
            pass
        jobs.append(Job(
            company=company["name"], ats="phenom", job_id=jid or link, title=title, url=link,
            location=loc if isinstance(loc, str) else ", ".join(map(str, loc)),
            department=str(j.get("category") or ""), description_html=desc,
            posted=_date(j.get("postedDate") or j.get("dateCreated") or ""),
            employment_type=str(j.get("type") or "").lower(), deadline=deadline,
        ))
    return jobs


# --------------------------------------------------------------------------- #
# EasyCruit (Visma) – e.g. cip.easycruit.com
# --------------------------------------------------------------------------- #
_EC_TITLE = ("title", "name", "jobtitle", "vacancytitle", "positiontitle", "heading", "headline")
_EC_URL = ("url", "link", "vacancyurl", "applicationurl", "applyurl", "href", "adurl")
_EC_LOC = ("location", "city", "workplace", "municipality", "region", "place")
_EC_DEADLINE = ("applicationdeadline", "deadline", "date_end", "enddate", "closingdate", "todate", "lastapplicationdate")
_EC_POSTED = ("date_start", "startdate", "publishdate", "published", "fromdate", "date")
_EC_DESC = ("description", "jobdescription", "adtext", "vacancytext", "text", "body", "content", "intro")


def _ec_fields(el) -> dict:
    """All child texts and attributes of a vacancy, by lower-case local name (first wins)."""
    vals = {k.lower(): v for k, v in el.attrib.items()}
    for sub in el.iter():
        if sub is el:
            continue
        for k, v in sub.attrib.items():
            vals.setdefault(f"{_local(sub.tag)}.{k.lower()}", v)
        t = (sub.text or "").strip()
        if t:
            vals.setdefault(_local(sub.tag), t)
    return vals


def _first(vals: dict, keys) -> str:
    return next((vals[k] for k in keys if vals.get(k)), "")


def _easycruit_html(session, company: dict, keep: TitleFilter, base: str) -> list[Job]:
    from bs4 import BeautifulSoup
    found, errors = {}, []
    for start in (f"{base}/?iso=gb", f"{base}/", f"{base}/index.html?iso=gb"):
        try:
            r = session.get(start, timeout=TIMEOUT, headers={"Accept": "text/html"})
            if r.status_code != 200:
                errors.append(f"{start}: HTTP {r.status_code}")
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a[href*='/vacancy/']"):
                href = requests.compat.urljoin(start, a["href"])
                m = re.search(r"/vacancy/(\d+)", href)
                t = a.get_text(" ", strip=True)
                if m and t and m.group(1) not in found:
                    found[m.group(1)] = (t, href if "iso=" in href else href + ("&" if "?" in href else "?") + "iso=gb")
            if found:
                break
        except requests.RequestException as e:
            errors.append(f"{start}: {e}")
    if not found:
        raise RuntimeError("EasyCruit: no vacancy list found – " + ("; ".join(errors)[:300] or "list page had no vacancy links"))
    jobs = []
    for vid, (title, link) in found.items():
        if not keep(title):
            continue
        desc, loc, deadline = "", "", ""
        try:
            pr = session.get(link, timeout=TIMEOUT, headers={"Accept": "text/html"})
            if pr.status_code == 200:
                posting = _jsonld_posting(pr.text)
                desc = html.unescape(posting.get("description") or "") or _page_description(pr.text)
                loc = _posting_location(posting)
                if posting.get("validThrough"):
                    deadline = _date(posting["validThrough"])
        except requests.RequestException:
            pass
        jobs.append(Job(company=company["name"], ats="easycruit", job_id=vid, title=title, url=link,
                        location=loc, description_html=desc, deadline=deadline))
    return jobs


def fetch_easycruit(session, company: dict, keep: TitleFilter) -> list[Job]:
    ident = company.get("id") or urlparse(company.get("url", "")).netloc.split(".")[0]
    base = f"https://{ident}.easycruit.com"
    r = session.get(f"{base}/export/xml/vacancy/list.xml", timeout=TIMEOUT,
                    headers={"Accept": "application/xml, text/xml"})
    if r.status_code in (403, 404):                      # XML export not enabled -> read the vacancy list page
        return _easycruit_html(session, company, keep, base)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    vacancies = [el for el in root.iter() if _local(el.tag) == "vacancy"]
    jobs = []
    for v in vacancies:
        f = _ec_fields(v)
        title = _first(f, _EC_TITLE)
        if not title or not keep(title):
            continue
        vid = f.get("id") or f.get("vacancyid") or ""
        url = _first(f, _EC_URL)
        if not url.startswith("http"):
            dept = f.get("department.id") or ""
            url = f"{base}/vacancy/{vid}/{dept}?iso=gb" if dept else f"{base}/vacancy/{vid}?iso=gb"
        deadline = _first(f, _EC_DEADLINE)
        jobs.append(Job(
            company=company["name"], ats="easycruit", job_id=vid or url, title=title, url=url,
            location=_first(f, _EC_LOC), department=f.get("department") or f.get("departmentname") or "",
            description_html=html.unescape(_first(f, _EC_DESC)),
            posted=_date(_first(f, _EC_POSTED)),
            deadline=_date(deadline) if deadline else "",
        ))
    if not vacancies:
        log.info("easycruit: feed for %s returned no <Vacancy> elements", ident)
    return jobs


FETCHERS: dict[str, Callable] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "personio": fetch_personio,
    "recruitee": fetch_recruitee,
    "workday": fetch_workday,
    "successfactors": fetch_successfactors,
    "teamtailor": fetch_teamtailor,
    "avature": fetch_avature,
    "easycruit": fetch_easycruit,
    "phenom": fetch_phenom,
}


def fetch_all(companies: Iterable[dict], keep: TitleFilter,
              session: requests.Session | None = None,
              search_terms: list[str] | None = None) -> tuple[list[Job], list[dict]]:
    """Fetch every enabled company. Returns (jobs, status rows for the report).
    `search_terms` (your titles) narrow server-side searches where supported."""
    session = session or make_session()
    all_jobs, status = [], []
    for c in companies:
        if c.get("enabled") is False:
            continue
        ats = (c.get("ats") or "linkedin").lower()
        fetcher = FETCHERS.get(ats)
        c = {**c, "_terms": search_terms or []}
        row = {"company": c.get("name", "?"), "ats": ats, "ok": False, "count": 0, "error": "",
               "source": str(c.get("url") or c.get("id") or ""),
               "careers_url": c.get("careers_url", ""),
               "note": str(c.get("detected") or c.get("detect_failed") or c.get("link_reason") or ""),
               "rating": c.get("rating", ""), "segment": c.get("segment", "")}
        if ats in ("linkedin", "none"):
            row.update(ok=True, error="link only")
            status.append(row)
            continue
        if not fetcher:
            row["error"] = f"unknown ats '{ats}'"
        else:
            try:
                jobs = fetcher(session, c, keep)
                for j in jobs:
                    j.rating = c.get("rating", "")
                    j.segment = c.get("segment", "")
                    j.segments_secondary = list(c.get("segments_secondary") or [])
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
