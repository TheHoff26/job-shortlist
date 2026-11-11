"""Offline test for the Avature and EasyCruit readers.  Run: python tests/test_new_ats.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jobfinder.extract import enrich  # noqa: E402
from jobfinder.sources import fetch_avature, fetch_easycruit  # noqa: E402

KEEP = lambda t: any(w in t.lower() for w in ("origination", "head", "investment", "director"))  # noqa: E731

AV_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>EnBW</title>
<item><title>Manager Origination PPA (f/m/d)</title><link>https://careers.enbw.com/en_US/careers/JobDetail/Manager-Origination-PPA/21969</link><description>short</description></item>
<item><title>Electrician (f/m/d)</title><link>https://careers.enbw.com/en_US/careers/JobDetail/Electrician/21970</link></item>
</channel></rss>"""
AV_PAGE = """<html><script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting",
"title":"Manager Origination PPA","datePosted":"2026-11-02","validThrough":"2026-11-30T23:59:59Z","employmentType":"FULL_TIME",
"jobLocation":{"@type":"Place","address":{"addressLocality":"Karlsruhe","addressCountry":"DE"}},
"description":"&lt;h3&gt;Your tasks&lt;/h3&gt;&lt;p&gt;Originate PPAs with industrial customers.&lt;/p&gt;&lt;h3&gt;Your profile&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;5+ years in power markets&lt;/li&gt;&lt;/ul&gt;"}
</script><body>x</body></html>"""
AV_LIST = """<html><article class="article--result"><h3 class="article__header__text__title"><a href="/en_US/jobs/JobDetail/Head-of-Hydrogen-Strategy/9001">Head of Hydrogen Strategy</a></h3></article></html>"""

EC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<VacancyList><Vacancy id="3446079" date_start="2026-10-28" date_end="2026-11-25">
 <Versions><Version language="en"><Title>Investment Director, Energy Transition Fund</Title>
 <Location>Copenhagen</Location><Description>&lt;p&gt;Lead investments in Nordic renewables.&lt;/p&gt;</Description></Version></Versions>
 <Departments><Department id="207841"><Name>Investment Management</Name></Department></Departments>
</Vacancy>
<Vacancy id="3446080"><Versions><Version><Title>Receptionist</Title></Version></Versions></Vacancy></VacancyList>"""


class R:
    def __init__(self, body, status=200):
        self.status_code, self.text, self.content = status, body, body.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class Net:
    def __init__(self, feed_ok=True):
        self.feed_ok, self.calls = feed_ok, []

    def get(self, url, params=None, **kw):
        self.calls.append(url)
        if url.endswith("/SearchJobs/feed/"):
            return R(AV_RSS) if self.feed_ok else R("nope", 404)
        if url.endswith("/SearchJobs"):
            return R(AV_LIST)
        if "/JobDetail/" in url:
            return R(AV_PAGE)
        if url == "https://cip.easycruit.com/export/xml/vacancy/list.xml":
            return R(EC_XML)
        return R("not found", 404)


def test():
    jobs = fetch_avature(Net(), {"name": "EnBW", "url": "https://careers.enbw.com/en_US/careers/SearchJobs"}, KEEP)
    assert len(jobs) == 1, jobs
    j = enrich(jobs[0])
    assert j.title.startswith("Manager Origination") and j.location == "Karlsruhe, DE" and j.posted == "2026-11-02"
    assert j.deadline == "2026-11-30" and j.deadline_source == "board" and j.job_id == "21969"
    assert "industrial customers" in j.summary or j.responsibilities, j

    jobs = fetch_avature(Net(feed_ok=False), {"name": "Siemens Energy", "url": "https://jobs.siemens-energy.com/en_US/jobs"}, KEEP)
    assert [x.title for x in jobs] == ["Head of Hydrogen Strategy"]
    assert jobs[0].url == "https://jobs.siemens-energy.com/en_US/jobs/JobDetail/Head-of-Hydrogen-Strategy/9001"

    jobs = fetch_easycruit(Net(), {"name": "CIP", "url": "https://cip.easycruit.com"}, KEEP)
    assert len(jobs) == 1
    e = jobs[0]
    assert e.title == "Investment Director, Energy Transition Fund" and e.location == "Copenhagen"
    assert e.posted == "2026-10-28" and e.deadline == "2026-11-25"
    assert e.url == "https://cip.easycruit.com/vacancy/3446079/207841?iso=gb", e.url
    print(json.dumps({"avature": j.title, "easycruit": e.title, "deadline": e.deadline}))


if __name__ == "__main__":
    test()
    print("OK")
