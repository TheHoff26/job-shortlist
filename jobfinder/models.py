from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Job:
    """One open position, normalised across all job-board systems."""

    company: str
    ats: str
    job_id: str
    title: str
    url: str
    location: str = ""
    department: str = ""
    description_html: str = ""
    posted: str = ""                 # ISO date (YYYY-MM-DD) when known
    workplace: str = ""              # remote | hybrid | onsite | ""
    employment_type: str = ""
    salary: str = ""
    seniority_hint: str = ""         # seniority supplied by the job board itself
    years_hint: str = ""             # years of experience supplied by the board

    # Filled in by extract.py / score.py
    summary: str = ""
    responsibilities: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    nice_to_have: list[str] = field(default_factory=list)
    seniority: str = ""
    years_experience: str = ""
    languages: list[str] = field(default_factory=list)
    education: str = ""
    skills_matched: list[str] = field(default_factory=list)
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    first_seen: str = ""
    is_new: bool = False

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.company}:{self.job_id}"

    def to_public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("description_html", None)
        data.pop("seniority_hint", None)
        data.pop("years_hint", None)
        data["key"] = self.key
        return data
