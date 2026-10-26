"""Offline end-to-end test: feeds sample responses (in each job board's real
format) through fetch -> extract -> score -> render.  Run:  python -m pytest -q
or simply:  python tests/test_pipeline.py"""

from __future__ import annotations

import html
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobfinder.main import run  # noqa: E402

GH_PM = """<h3>About the role</h3><p>As Senior Product Manager for Payments you will own the
roadmap for our B2B checkout platform used by thousands of SaaS businesses. You will work with
engineering, design and sales to ship features that reduce payment failures.</p>
<h3>What you'll do</h3><ul><li>Own the product roadmap and prioritisation</li>
<li>Run A/B testing and analyse results with SQL</li><li>Align stakeholders across three countries</li></ul>
<h3>Who you are</h3><ul><li>5+ years of experience in product management, ideally in fintech</li>
<li>Bachelor's degree in business, engineering or equivalent experience</li>
<li>Fluent English; German is a plus</li><li>Comfortable with SQL and Python for analysis</li>
<li>Experience with agile delivery (Scrum, Jira)</li></ul>
<h3>Nice to have</h3><ul><li>Experience with Power BI or Tableau</li></ul>
<h3>What we offer</h3><ul><li>Hybrid work from Helsinki</li><li>Stock options</li></ul>"""

GH_ENG = "<p>Build distributed systems in Go.</p>"

LEVER = [{
    "id": "abc-1", "text": "Data Analyst", "hostedUrl": "https://jobs.lever.co/spotify/abc-1",
    "createdAt": 1761000000000, "workplaceType": "remote",
    "categories": {"location": "Stockholm", "team": "Insights", "commitment": "Permanent"},
    "description": "<div>We are a leading audio company founded in 2006.</div>"
                   "<div>In this role you turn listening data into decisions for product teams.</div>",
    "lists": [{"text": "What you'll do", "content": "<li>Build Tableau dashboards</li><li>Define KPIs</li>"},
              {"text": "Who you are", "content": "<li>2-4 years of experience as an analyst</li>"
                                                  "<li>Strong SQL and Python</li><li>Fluent in English</li>"}],
    "additional": "<div>We offer great benefits.</div>",
}]

ASHBY = {"jobs": [{
    "id": "as-1", "title": "Product Owner", "location": "Berlin", "department": "Product",
    "isRemote": False, "workplaceType": "Hybrid", "isListed": True,
    "jobUrl": "https://jobs.ashbyhq.com/linear/as-1", "publishedAt": "2026-10-20T10:00:00.000Z",
    "employmentType": "FullTime",
    "descriptionHtml": "<p><strong>Your role</strong></p><p>You shape the backlog of our issue tracker "
                       "together with engineers. You talk to customers every week.</p>"
                       "<p><strong>Your profile</strong></p><ul><li>Three years of experience as product owner"
                       "</li><li>Experience with Jira and agile roadmap planning</li>"
                       "<li>Sehr gute Deutsch- und Englischkenntnisse</li></ul>",
    "compensation": {"compensationTierSummary": "€70K – €85K"},
}]}

SR_LIST = {"totalFound": 2, "content": [
    {"id": "sr-1", "name": "Junior Business Analyst", "releasedDate": "2026-10-24T08:00:00.000Z",
     "location": {"city": "Espoo", "country": "fi", "remote": False},
     "department": {"label": "Finance"}, "typeOfEmployment": {"label": "Full-time"},
     "experienceLevel": {"label": "Entry Level"}},
    {"id": "sr-2", "name": "Mechanical Engineer", "location": {"city": "Stuttgart", "country": "de"}},
]}
SR_DETAIL = {"postingUrl": "https://jobs.smartrecruiters.com/BoschGroup/sr-1", "jobAd": {"sections": {
    "jobDescription": {"title": "Job Description", "text": "<p>You support the finance team with reporting "
                       "and analysis of business processes.</p><ul><li>Prepare monthly reports in Power BI</li></ul>"},
    "qualifications": {"title": "Qualifications", "text": "<ul><li>University degree in economics</li>"
                       "<li>First experience with SQL</li><li>Fluent Finnish and English</li></ul>"}}}}

PERSONIO = """<?xml version="1.0" encoding="UTF-8"?><workzag-jobs>
<position><id>9001</id><office>Helsinki</office><department>Product</department>
<name>Program Manager (m/w/d)</name><employmentType>permanent</employmentType><schedule>full-time</schedule>
<seniority>experienced</seniority><yearsOfExperience>3-5</yearsOfExperience><createdAt>2026-10-01T09:00:00+00:00</createdAt>
<jobDescriptions>
<jobDescription><name>Deine Aufgaben</name><value><![CDATA[<ul><li>Coordinate cross-team programmes</li><li>Report to leadership</li></ul>]]></value></jobDescription>
<jobDescription><name>Das bringst du mit</name><value><![CDATA[<ul><li>Experience in stakeholder management</li><li>Scrum certification</li></ul>]]></value></jobDescription>
</jobDescriptions></position>
<position><id>9002</id><office>Paris</office><name>Product Manager</name><jobDescriptions/></position>
</workzag-jobs>"""


class FakeResp:
    def __init__(self, body, ctype="application/json", status=200):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.content = self.text.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def get(self, url, params=None, **kw):
        if "greenhouse" in url:
            return FakeResp({"jobs": [
                {"id": 1, "title": "Senior Product Manager, Payments", "absolute_url": "https://stripe.com/jobs/1",
                 "location": {"name": "Helsinki, Finland"}, "first_published": "2026-10-22T09:00:00-04:00",
                 "departments": [{"name": "Product"}], "content": html.escape(GH_PM)},
                {"id": 2, "title": "Backend Engineer", "absolute_url": "x", "location": {"name": "Dublin"},
                 "content": html.escape(GH_ENG)}]})
        if "api.lever.co" in url:
            return FakeResp(LEVER if (params or {}).get("skip", 0) == 0 else [])
        if "ashbyhq" in url:
            return FakeResp(ASHBY)
        if "smartrecruiters" in url:
            return FakeResp(SR_DETAIL if re.search(r"/postings/sr-", url) else SR_LIST)
        if "personio" in url:
            return FakeResp(PERSONIO, "application/xml")
        return FakeResp({"error": "not found"}, status=404)


def test_pipeline():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        meta = run(ROOT / "config", tmp / "docs", tmp / "seen.json", session=FakeSession())
        data = json.loads((tmp / "docs" / "jobs.json").read_text())
        titles = {j["title"]: j for j in data["jobs"]}

        assert "Backend Engineer" not in titles            # title filter
        assert "Product Manager" not in titles             # Paris: location filter
        pm = titles["Senior Product Manager, Payments"]
        assert pm["seniority"] == "senior" and pm["years_experience"] == "5+ years"
        assert "English" in pm["languages"] and "German (plus)" in pm["languages"]
        assert pm["education"].startswith("Bachelor's") and pm["criteria"] and pm["summary"]
        assert {"SQL", "Python", "A/B testing"} <= set(pm["skills_matched"])
        assert titles["Data Analyst"]["years_experience"] == "2–4 years"
        assert titles["Data Analyst"]["workplace"] == "remote"
        assert titles["Junior Business Analyst"]["seniority"] == "junior"
        assert titles["Program Manager (m/w/d)"]["seniority"] == "mid"
        assert titles["Program Manager (m/w/d)"]["years_experience"] == "3–5 years"
        assert pm["summary"].endswith("payment failures.") or "ship features" in pm["summary"]
        assert any("5+ years" in c for c in pm["criteria"]) and pm["nice_to_have"] == ["Experience with Power BI or Tableau"]
        assert "German" in titles["Product Owner"]["languages"]
        assert (tmp / "docs" / "index.html").read_text().count("Senior Product Manager") >= 1

        # LinkedIn: one search link per company, nothing fetched from LinkedIn
        links = {l["company"]: l["url"] for l in meta["linkedin"]}
        assert set(links) == {c["company"] for c in meta["companies"]}
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(links["Stripe"]).query)
        assert q["keywords"][0].startswith('"Stripe" ("product manager" OR')
        assert q["location"] == ["Finland"] and q["f_TPR"] == ["r604800"] and q["f_E"] == ["3,4"]

        from jobfinder.linkedin import search_url
        from jobfinder.sources import fetch_all
        prof = {"titles": ["data analyst"], "seniority": ["senior"], "locations": ["Helsinki"],
                "linkedin": {"posted_within_days": 0}}
        q = parse_qs(urlparse(search_url({"name": "Kone", "linkedin_id": 1234}, prof)).query)
        assert q == {"keywords": ['"data analyst"'], "location": ["Helsinki"], "f_C": ["1234"], "f_E": ["4"]}
        jobs, status = fetch_all([{"name": "Supercell", "ats": "linkedin"}], lambda t: True, session=FakeSession())
        assert jobs == [] and status[0]["ok"] and status[0]["error"] == "link only"
        print(json.dumps(meta["companies"], indent=1))
        for j in data["jobs"]:
            print(f"{j['score']:>3}  {j['company']:<8} {j['title']:<35} {j['seniority']:<7} {j['reasons']}")
        return tmp


if __name__ == "__main__":
    test_pipeline()
    print("OK")
