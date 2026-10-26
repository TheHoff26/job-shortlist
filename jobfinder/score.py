"""Relevance filter and score (0-100) against the profile in config/profile.yaml.

Score components (max points):
  Title match ............ 35   a wanted title appears in the job title
  Seniority .............. 20   level matches (adjacent level: 8, unknown: 10)
  CV skills .............. 30   share of your cv_skills found in the posting
                                 (6 matches = full points)
  CV text similarity ..... 10   only if a CV text is provided, else rescaled
  Keyword bonus ..........  5
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from pathlib import Path

from .extract import LEVELS, plain_text
from .models import Job

DEFAULTS = {
    "titles": [], "require_title_match": True, "exclude_titles": [], "keywords": [],
    "seniority": [], "locations": [], "remote_ok": True, "cv_skills": [], "cv_file": "",
    "min_score": 35, "new_days": 3, "max_jobs_per_company": 0,
    "linkedin": {"enabled": True, "location": "", "posted_within_days": 7},
}


def load_profile(data: dict | None, base_dir: Path) -> dict:
    prof = {**DEFAULTS, **(data or {})}
    for k in ("titles", "exclude_titles", "keywords", "seniority", "locations", "cv_skills"):
        prof[k] = [str(x).strip() for x in (prof.get(k) or []) if str(x).strip()]
    prof["seniority"] = [s.lower() for s in prof["seniority"]]
    prof["linkedin"] = {**DEFAULTS["linkedin"], **(prof.get("linkedin") or {})}
    cv = os.environ.get("JOBFINDER_CV_TEXT", "")
    if not cv and prof.get("cv_file"):
        p = (base_dir / prof["cv_file"]).resolve()
        if p.is_file():
            cv = p.read_text(encoding="utf-8", errors="ignore")
    prof["_cv_vector"] = _vector(cv) if cv.strip() else None
    return prof


# --------------------------------------------------------------------------- #
def _rx(term: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.I)


def contains(text: str, term: str) -> bool:
    return bool(_rx(term).search(text or ""))


def title_filter(profile: dict):
    """Cheap pre-filter applied before downloading job details."""
    titles, excl = profile["titles"], profile["exclude_titles"]
    require = profile["require_title_match"] and bool(titles)

    def keep(title: str) -> bool:
        if any(contains(title, x) for x in excl):
            return False
        return (not require) or any(contains(title, t) for t in titles)
    return keep


STOP = set("""a an and are as at be by for from has have in is it of on or our that the their this to
we will with you your yours who what which can into across within about all also more most other such
than then them they these those using use used work working team teams role new including help well
der die das und mit für von wir sie ist ein eine auf als bei zu den im des dich du ihr
ja on tai ovat olla että myös sekä kanssa tämä meidän sinä""".split())


def _vector(text: str) -> Counter:
    toks = re.findall(r"[a-zA-ZäöüåÄÖÜÅß+#]{3,}", text.lower())
    return Counter(t for t in toks if t not in STOP)


def _cosine(a: Counter, b: Counter) -> float:
    common = set(a) & set(b)
    num = sum(a[t] * b[t] for t in common)
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / den if den else 0.0


def location_ok(job: Job, profile: dict) -> tuple[bool, str]:
    locs = profile["locations"]
    if not locs:
        return True, ""
    if any(contains(job.location, l) for l in locs):
        return True, f"Location: {job.location}"
    if profile["remote_ok"] and (job.workplace == "remote" or "remote" in job.location.lower()):
        return True, "Location: remote"
    if not job.location.strip():
        return True, "Location: not stated"
    return False, ""


def score_job(job: Job, profile: dict) -> bool:
    """Fill job.score / reasons / skills_matched. Returns False if filtered out."""
    if any(contains(job.title, x) for x in profile["exclude_titles"]):
        return False
    ok, loc_reason = location_ok(job, profile)
    if not ok:
        return False

    text = plain_text(job)
    reasons, pts, max_pts = [], 0.0, 90.0

    # Title -----------------------------------------------------------------
    t_hit = next((t for t in profile["titles"] if contains(job.title, t)), None)
    if t_hit:
        pts += 35
        reasons.append(f"Title matches “{t_hit}”")
    elif profile["titles"]:
        if profile["require_title_match"]:
            return False
        d_hit = next((t for t in profile["titles"] if contains(text, t)), None)
        if d_hit:
            pts += 15
            reasons.append(f"Description mentions “{d_hit}”")
    else:
        pts += 20

    # Seniority -------------------------------------------------------------
    wanted = profile["seniority"]
    lvl = job.seniority
    if not wanted:
        pts += 20
    elif not lvl:
        pts += 10
        reasons.append("Seniority: not stated")
    elif lvl in wanted:
        pts += 20
        reasons.append(f"Seniority: {lvl} ✓")
    else:
        idx = LEVELS.index(lvl) if lvl in LEVELS else -9
        near = any(abs(idx - LEVELS.index(w)) == 1 for w in wanted if w in LEVELS)
        pts += 8 if near else 0
        reasons.append(f"Seniority: {lvl} ({'close to' if near else 'outside'} your target)")

    # CV skills ---------------------------------------------------------------
    skills = profile["cv_skills"]
    matched = [s for s in skills if contains(text, s)]
    job.skills_matched = matched
    if skills:
        pts += 30 * min(1.0, len(matched) / min(len(skills), 6))
        reasons.append(f"CV skills {len(matched)}/{len(skills)}"
                       + (f": {', '.join(matched[:8])}" if matched else ""))
    else:
        pts += 15

    # CV similarity -------------------------------------------------------------
    if profile.get("_cv_vector"):
        max_pts = 100.0
        sim = _cosine(profile["_cv_vector"], _vector(text))
        pts += 10 * min(1.0, sim / 0.25)
        reasons.append(f"CV text similarity {sim:.0%}")

    # Keywords ------------------------------------------------------------------
    kw = [k for k in profile["keywords"] if contains(text, k)]
    if kw:
        pts += 5 * min(1.0, len(kw) / 2)
        reasons.append("Keywords: " + ", ".join(kw))

    if loc_reason:
        reasons.append(loc_reason)
    job.score = int(round(pts * 100 / max_pts))
    job.reasons = reasons
    return job.score >= int(profile["min_score"])
