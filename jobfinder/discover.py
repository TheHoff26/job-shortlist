"""Automatic detection of where a company publishes its job openings.

A company can be added with just a name (and ideally its careers page URL):

    - name: Fortum
      careers_url: https://www.fortum.com/careers     # optional, recommended

On the next run, `resolve_companies()` works out which job system the company
uses and writes the result back into config/companies.yaml:

    - name: Fortum
      careers_url: https://www.fortum.com/careers
      ats: successfactors
      url: "https://jobs.fortum.com"
      detected: "2026-10-30, from careers page"

Detection order
  1. The careers URL itself points to a known job system.
  2. The careers page (and up to 6 job-related pages it links to) contains a
     link to a known job system, or is a SAP SuccessFactors career site.
  3. Name guess: the company name is tried as an account name on the public
     job systems, and as jobs.<name>.com / careers.<name>.com.
  4. Nothing found: the company gets a LinkedIn link only; detection is
     retried after 7 days.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

log = logging.getLogger("jobfinder")
TIMEOUT = 20
RETRY_DAYS = 7

# --------------------------------------------------------------------------- #
# Patterns for links that reveal the job system
# --------------------------------------------------------------------------- #
PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("workday", re.compile(
        r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", re.I), "url"),
    ("greenhouse", re.compile(
        r"(?:boards-api\.greenhouse\.io/v1/boards/|(?:job-)?boards(?:\.eu)?\.greenhouse\.io/"
        r"(?:embed/job_board(?:/js)?\?for=)?)([\w-]+)", re.I), "id"),
    ("lever", re.compile(r"(?:jobs|api)\.(?:eu\.)?lever\.co/(?:v0/postings/)?([\w.-]+)", re.I), "id"),
    ("ashby", re.compile(r"(?:jobs\.ashbyhq\.com/|posting-api/job-board/)([\w.%-]+)", re.I), "id"),
    ("smartrecruiters", re.compile(
        r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)|api\.smartrecruiters\.com/v1/companies/([\w-]+)", re.I), "id"),
    ("personio", re.compile(r"https?://([\w-]+)\.jobs\.personio\.(?:de|com)", re.I), "id"),
    ("recruitee", re.compile(r"https?://([\w-]+)\.recruitee\.com", re.I), "id"),
]
_BAD_IDS = {"embed", "js", "jobs", "v1", "api", "www", "careers", "job", "search", "static", "assets"}
SF_MARKERS = re.compile(r"rmkcdn\.successfactors|successfactors\.(?:com|eu)|/services/rss/job|"
                        r"career\?company=|jobs2web|sapsf", re.I)
JOB_LINK = re.compile(r"job|career|vacanc|position|opening|avoimet|työpaik|tyopaik|karriere|stellen|"
                      r"lediga|jobb|opportunit|join", re.I)
CANDIDATE_HOST = re.compile(r"https?://((?:jobs|careers?|career|rekry|recruit\w*|apply)\.[\w.-]+)", re.I)


def _match_systems(text: str) -> dict | None:
    for ats, rx, kind in PATTERNS:
        for m in rx.finditer(text):
            if ats == "workday":
                tenant, shard, site = m.group(1), m.group(2), m.group(3)
                if site.lower() in ("wday", "en", "job"):
                    continue
                return {"ats": "workday", "url": f"https://{tenant}.{shard}.myworkdayjobs.com/{site}"}
            ident = next(g for g in m.groups() if g)
            if ident.lower() in _BAD_IDS:
                continue
            return {"ats": ats, kind: ident}
    return None


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #
def _get(session, url, **kw):
    kw.setdefault("timeout", TIMEOUT)
    return session.get(url, **kw)


def _probe_successfactors(session, base: str) -> bool:
    """True if `base` serves a SuccessFactors job RSS feed."""
    try:
        r = _get(session, f"{base.rstrip('/')}/services/rss/job/", params={"locale": "en_US"},
                 headers={"Accept": "application/rss+xml, application/xml, text/xml"})
        if r.status_code != 200:
            return False
        root = ET.fromstring(r.content)
        return root.tag == "rss" or root.find("channel") is not None
    except Exception:
        return False


def _probe_workday(session, url: str) -> bool:
    try:
        from .sources import _workday_parts
        host, tenant, site = _workday_parts(url)
        r = session.post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                         json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                         headers={"Accept": "application/json", "Content-Type": "application/json"},
                         timeout=TIMEOUT)
        return r.status_code == 200 and "total" in r.json()
    except Exception:
        return False


def _probe_slug(session, ats: str, slug: str) -> bool:
    """Check whether `slug` is a live account on a public job system."""
    try:
        if ats == "greenhouse":
            r = _get(session, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            return r.status_code == 200 and "jobs" in r.json()
        if ats == "lever":
            for host in ("https://api.lever.co", "https://api.eu.lever.co"):
                r = _get(session, f"{host}/v0/postings/{slug}", params={"mode": "json", "limit": 1})
                if r.status_code == 200 and isinstance(r.json(), list) and r.json():
                    return True
            return False
        if ats == "ashby":
            r = _get(session, f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            return r.status_code == 200 and "jobs" in r.json()
        if ats == "smartrecruiters":
            # the API answers 200 for any name, so require at least one posting
            r = _get(session, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                     params={"limit": 1})
            return r.status_code == 200 and r.json().get("totalFound", 0) > 0
        if ats == "personio":
            r = _get(session, f"https://{slug}.jobs.personio.de/xml", headers={"Accept": "application/xml"})
            return r.status_code == 200 and ET.fromstring(r.content).tag == "workzag-jobs"
        if ats == "recruitee":
            r = _get(session, f"https://{slug}.recruitee.com/api/offers/")
            return r.status_code == 200 and "offers" in r.json()
    except Exception:
        return False
    return False


def _validate(session, found: dict) -> bool:
    if found["ats"] == "workday":
        return _probe_workday(session, found["url"])
    if found["ats"] == "successfactors":
        return _probe_successfactors(session, found["url"])
    return _probe_slug(session, found["ats"], found["id"])


# --------------------------------------------------------------------------- #
# Name -> slug candidates
# --------------------------------------------------------------------------- #
_SUFFIXES = re.compile(r"\b(oyj|oy|ab|abp|asa|as|a/s|plc|ltd|limited|inc|corp|corporation|gmbh|ag|se|"
                       r"group|holding|holdings|company|co)\b\.?", re.I)


def slugs(name: str) -> list[str]:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = _SUFFIXES.sub(" ", ascii_name).strip()
    words = re.findall(r"[a-z0-9]+", base.lower())
    full = re.findall(r"[a-z0-9]+", ascii_name.lower())
    out: list[str] = []
    for w in (words, full):
        if not w:
            continue
        for s in ("".join(w), "-".join(w), w[0]):
            if len(s) >= 2 and s not in out:
                out.append(s)
    return out[:4]


# --------------------------------------------------------------------------- #
# Main detection
# --------------------------------------------------------------------------- #
def _scan_page(session, url: str) -> tuple[dict | None, str, list[str], set[str]]:
    """Return (found, html, job_links, candidate_hosts) for one page."""
    try:
        r = _get(session, url, headers={"Accept": "text/html,application/xhtml+xml"}, allow_redirects=True)
    except Exception as exc:
        log.debug("detect: cannot open %s: %s", url, exc)
        return None, "", [], set()
    final = getattr(r, "url", url) or url
    if r.status_code >= 400:
        return None, "", [], set()
    text = r.text or ""
    found = _match_systems(final) or _match_systems(text)
    if not found and SF_MARKERS.search(text):
        host = urlparse(final)
        found = {"ats": "successfactors", "url": f"{host.scheme}://{host.netloc}"}
    links = []
    for href, label in re.findall(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", text, re.I | re.S):
        absolute = urljoin(final, href.strip())
        if absolute.startswith("http") and (JOB_LINK.search(href) or JOB_LINK.search(re.sub("<[^>]+>", "", label))):
            if absolute not in links:
                links.append(absolute)
    hosts = {m.group(1).lower() for m in CANDIDATE_HOST.finditer(text)}
    return found, text, links, hosts


def detect(session, name: str, careers_url: str | None = None) -> dict:
    """Return {"ats": ..., "url"/"id": ..., "how": ...}; ats == "linkedin" if nothing found."""
    if careers_url:
        direct = _match_systems(careers_url)
        if direct and _validate(session, direct):
            return {**direct, "how": "from careers URL"}

        found, _, links, hosts = _scan_page(session, careers_url)
        if found and _validate(session, found):
            return {**found, "how": "from careers page"}
        if not found and _probe_successfactors(session, careers_url):
            u = urlparse(careers_url)
            return {"ats": "successfactors", "url": f"{u.scheme}://{u.netloc}", "how": "from careers page"}

        own = urlparse(careers_url).netloc.split(".")[-2] if "." in urlparse(careers_url).netloc else ""
        for link in links[:6]:
            f2, _, _, h2 = _scan_page(session, link)
            hosts |= h2
            if f2 and _validate(session, f2):
                return {**f2, "how": "from careers page"}
        for host in sorted(hosts):
            if own and own not in host:
                continue
            base = f"https://{host}"
            if _probe_successfactors(session, base):
                return {"ats": "successfactors", "url": base, "how": "from careers page"}

    for slug in slugs(name):
        for ats in ("greenhouse", "lever", "ashby", "personio", "recruitee", "smartrecruiters"):
            if _probe_slug(session, ats, slug):
                return {"ats": ats, "id": slug, "how": "name guess – please verify"}
        for sub in ("jobs", "careers"):
            for tld in ("com", "fi"):
                base = f"https://{sub}.{slug}.{tld}"
                if _probe_successfactors(session, base):
                    return {"ats": "successfactors", "url": base, "how": "name guess – please verify"}

    return {"ats": "linkedin", "how": "no supported job system found"
            + ("" if careers_url else " – add the careers page URL")}


# --------------------------------------------------------------------------- #
# companies.yaml: find unresolved entries, write results back (keeps comments)
# --------------------------------------------------------------------------- #
_ENTRY = re.compile(r"^(\s*)-\s+name:\s*(.+?)\s*$")


def _blocks(lines: list[str]) -> list[tuple[int, int, int]]:
    """(start, end, indent) line ranges of each `- name:` entry."""
    out, i = [], 0
    while i < len(lines):
        m = _ENTRY.match(lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        j = i + 1
        while j < len(lines):
            ln = lines[j]
            stripped = ln.strip()
            if not stripped or stripped.startswith("#"):
                break
            if len(ln) - len(ln.lstrip()) <= indent:
                break
            j += 1
        out.append((i, j, indent))
        i = j
    return out


def needs_detection(c: dict, today: date) -> bool:
    if c.get("ats") or c.get("enabled") is False:
        return False
    failed = c.get("detect_failed")
    if failed:
        try:
            return date.fromisoformat(str(failed)[:10]) <= today - timedelta(days=RETRY_DAYS)
        except ValueError:
            return True
    return True


def resolve_companies(path: Path, session, today: date | None = None) -> list[dict]:
    """Detect sources for new companies, update the YAML file, return the company list."""
    import yaml
    today = today or date.today()
    text = path.read_text(encoding="utf-8")
    companies = (yaml.safe_load(text) or {}).get("companies", []) or []
    todo = [i for i, c in enumerate(companies) if needs_detection(c, today)]
    if not todo:
        return companies

    lines = text.split("\n")
    blocks = _blocks(lines)
    if len(blocks) != len(companies):
        log.warning("companies.yaml layout not understood; detection results not saved")
        blocks = []

    for idx in sorted(todo, reverse=True):          # bottom-up keeps line numbers valid
        c = companies[idx]
        res = detect(session, str(c.get("name", "")), c.get("careers_url"))
        log.info("detect: %-28s -> %s (%s)", c.get("name"), res["ats"], res["how"])
        new = {}
        if res["ats"] == "linkedin":
            new["detect_failed"] = f"{today.isoformat()}, {res['how']}"
        else:
            new["ats"] = res["ats"]
            new["url" if "url" in res else "id"] = res.get("url") or res.get("id")
            new["detected"] = f"{today.isoformat()}, {res['how']}"
        c.pop("detect_failed", None)
        c.update(new)
        if blocks:
            start, end, indent = blocks[idx]
            body = [ln for ln in lines[start + 1:end] if not re.match(r"\s*detect_failed:", ln)]
            pad = " " * (indent + 2)
            body += [f"{pad}{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in new.items()]
            lines[start + 1:end] = body
    if blocks:
        path.write_text("\n".join(lines), encoding="utf-8")
    return companies
