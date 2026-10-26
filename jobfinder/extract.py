"""Rule-based extraction: turns a job description into a short summary and the
key applicant criteria. Works with English, German and Finnish postings.

No AI service is used. The approach:
  1. Convert the HTML into sections (heading + paragraphs + bullet points).
  2. Classify each section by its heading (role / requirements / nice-to-have /
     offer / about company).
  3. Summary   = first sentences of the role section (or first relevant paragraph).
     Criteria  = bullets of the requirements section (fallback: sentences with
                 typical requirement wording such as "experience", "degree").
  4. Pull structured facts with patterns: years of experience, languages,
     education level, seniority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .models import Job

# --------------------------------------------------------------------------- #
# Section classification
# --------------------------------------------------------------------------- #
SECTION_PATTERNS = {
    "nice": r"nice[\s-]to[\s-]have|bonus|preferred|plus if|would be a plus|"
            r"eduksi|katsomme eduksi|plussaa|wünschenswert|von vorteil|idealerweise",
    "requirements": r"require|qualifica|what you('ll)? bring|what we('re| are)? looking for|"
                    r"who you are|about you|you have|you are|your profile|your skills|"
                    r"must[\s-]have|skills|experience|competenc|ideal candidate|"
                    r"odotamme|toivomme|edellytä|vaatimuk|osaaminen|sinulla on|olet|"
                    r"anforderung|profil|qualifikation|mitbringst|bringst du mit|das zeichnet dich aus",
    "role": r"responsib|what you('ll| will)? do|the role|your role|about the (role|job|position)|"
            r"the job|the position|tasks|duties|you will|your mission|day[\s-]to[\s-]day|in this role|"
            r"tehtäv|työnkuva|aufgaben|deine rolle|ihre rolle|das erwartet dich",
    "offer": r"we offer|what we offer|benefits|perks|why join|why you('ll)? love|compensation|"
             r"tarjoamme|meillä saat|wir bieten|benefits|was wir bieten|salary|pay range",
    "about": r"about (us|the company|the team)|who we are|our company|our story|our mission|"
             r"meistä|keitä olemme|über uns|wer wir sind|equal opportunit|diversity|"
             r"how to apply|hakeminen|privacy",
}
_SECTION_RE = {k: re.compile(v, re.I) for k, v in SECTION_PATTERNS.items()}
_ORDER = ("nice", "offer", "about", "role", "requirements")

REQ_CUE = re.compile(
    r"\b(experience|years|degree|knowledge of|proficien\w*|familiar\w*|fluent\w*|"
    r"skills? in|ability to|able to|background in|understanding of|expertise|track record|"
    r"bachelor|master|hands-on|you have|you bring|strong|"
    r"kokemus\w*|osaamis\w*|tutkinto\w*|erfahrung\w*|kenntniss\w*|abgeschlossen\w*)\b", re.I)
NICE_CUE = re.compile(r"nice[\s-]to[\s-]have|\bbonus\b|\ba plus\b|\bplus\b:|preferred|ideally|"
                      r"advantage|eduksi|plussaa|von vorteil|wünschenswert|idealerweise", re.I)
NICE_START = re.compile(r"^\W*(nice[\s-]to[\s-]have|bonus|plus\b|ideally|preferably|"
                        r"eduksi|plussaa|idealerweise|wünschenswert)", re.I)
NICE_END = re.compile(r"(is|are|would be|will be)?\s*(a |an )?(big |strong )?"
                      r"(plus|bonus|advantage|nice to have|preferred|eduksi|von vorteil)\W*$", re.I)


def is_nice(item: str) -> bool:
    """A requirement bullet that is really optional ("Bonus: ...", "... is a plus")."""
    if NICE_START.search(item):
        return True
    return bool(NICE_END.search(item)) and not re.search(r"[;,]", item)


ABOUT_CUE = re.compile(r"\b(founded|our mission|is a (leading|global|fast)|we are a|we're a|"
                       r"headquartered|employees|since (19|20)\d\d|customers in|backed by|"
                       r"unicorn|join us|perustettu|gegründet)\b", re.I)


def classify(heading: str) -> str:
    for kind in _ORDER:
        if _SECTION_RE[kind].search(heading or ""):
            return kind
    return "other"


@dataclass
class Section:
    heading: str = ""
    kind: str = "other"
    paragraphs: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# HTML -> sections
# --------------------------------------------------------------------------- #
_BULLET_PREFIX = re.compile(r"^\s*([•·▪◦\-\*–—]|\d{1,2}[.)])\s+")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def html_to_sections(raw: str) -> list[Section]:
    if not raw:
        return []
    soup = BeautifulSoup(raw, "html.parser")
    for bad in soup(["script", "style"]):
        bad.decompose()

    # Paragraphs that only contain bold text are de-facto headings.
    for p in soup.find_all(["p", "div"]):
        strong = p.find(["strong", "b"])
        text = _clean(p.get_text(" "))
        if strong and text and _clean(strong.get_text(" ")) == text and len(text) < 90:
            p.name = "h4"

    for node in soup.find_all(string=True):
        if "\n" in node or "\r" in node:
            node.replace_with(re.sub(r"[\r\n]+", " ", str(node)))
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        h.insert_before("\n§§ ")
        h.insert_after("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n•• ")
        li.insert_after("\n")
    for blk in soup.find_all(["p", "div", "ul", "ol", "section", "tr"]):
        blk.insert_after("\n")

    text = soup.get_text("")
    sections: list[Section] = [Section()]
    for line in text.split("\n"):
        line = line.replace("\xa0", " ").strip()
        if not line:
            continue
        if line.startswith("§§"):
            heading = _clean(line[2:]).rstrip(":")
            if heading:
                sections.append(Section(heading=heading, kind=classify(heading)))
            continue
        is_bullet = line.startswith("••") or bool(_BULLET_PREFIX.match(line))
        content = _clean(_BULLET_PREFIX.sub("", line.lstrip("•").strip()))
        if not content:
            continue
        # Short line ending with ":" -> heading ("What you bring:")
        if not is_bullet and content.endswith(":") and len(content) < 90:
            sections.append(Section(heading=content.rstrip(":"), kind=classify(content)))
            continue
        cur = sections[-1]
        (cur.bullets if is_bullet else cur.paragraphs).append(content)
    return [s for s in sections if s.paragraphs or s.bullets]


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜÅ0-9])")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text) if len(s.strip()) > 3]


def shorten(text: str, limit: int) -> str:
    text = _clean(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:-–")
    return cut + "…"


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for it in items:
        k = re.sub(r"\W+", "", it.lower())[:60]
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    return out


# --------------------------------------------------------------------------- #
# Structured facts
# --------------------------------------------------------------------------- #
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5,
            "kaksi": 2, "kolme": 3, "neljä": 4, "viisi": 5}
_NUM = r"(\d{1,2}|" + "|".join(_WORDNUM) + r")"
YEARS_RE = re.compile(
    _NUM + r"\s*(?:\+|plus)?\s*(?:(?:-|–|to|bis)\s*" + _NUM + r"\s*)?\+?\s*"
    r"(?:years?|yrs?|jahre[n]?|vuo(?:den|tta|si))", re.I)
EXPERIENCE_WORD = re.compile(r"experience|erfahrung|kokemus|background|track record", re.I)


def _num(tok: str) -> int:
    return int(tok) if tok.isdigit() else _WORDNUM.get(tok.lower(), 0)


def find_years(texts: list[str]) -> tuple[str, int | None]:
    for t in texts:
        if not EXPERIENCE_WORD.search(t):
            continue
        m = YEARS_RE.search(t)
        if m:
            lo = _num(m.group(1))
            hi = _num(m.group(2)) if m.group(2) else None
            if 0 < lo <= 25:
                return (f"{lo}–{hi} years" if hi and hi > lo else f"{lo}+ years"), lo
    return "", None


LANGUAGES = {
    "English": r"english\b|englan\w*|englisch\w*",
    "Finnish": r"finnish\b|suomen kiel\w*|suomi\b|finnisch\w*",
    "Swedish": r"swedish\b|ruotsi\w*|schwedisch\w*",
    "German": r"german\b|saksa\w*|deutsch(e|en|kenntnisse)?\b",
    "French": r"french\b|ranska\w*|französisch\w*",
    "Spanish": r"spanish\b|espanja\w*|spanisch\w*",
    "Dutch": r"dutch\b|hollanti\w*|niederländisch\w*",
    "Norwegian": r"norwegian\b|norja\w*",
    "Danish": r"danish\b|tanska\w*",
    "Estonian": r"estonian\b|viro(n|a)?\b",
    "Italian": r"italian\b|italienisch\w*",
    "Polish": r"polish\b|polnisch\w*",
    "Portuguese": r"portuguese\b|portugiesisch\w*",
    "Mandarin": r"mandarin\b|chinese\b|chinesisch\w*",
    "Japanese": r"japanese\b|japanisch\w*",
}
_LANG_RE = {k: re.compile(r"\b(" + v + r")", re.I) for k, v in LANGUAGES.items()}
LANG_CUE = re.compile(r"fluen|proficien|language|spoken|written|native|business[- ]level|"
                      r"\b[ABC][12]\b|kielitai|kielen|sprach|kenntnisse|verhandlungssicher", re.I)


def find_languages(req: list[str], nice: list[str]) -> list[str]:
    found: dict[str, bool] = {}
    for texts, required in ((req, True), (nice, False)):
        for t in texts:
            if not LANG_CUE.search(t):
                continue
            # judge each clause separately: "Fluent English; German is a plus"
            for clause in re.split(r"[;,]| and | but |\bwhile\b", t):
                is_req = required and not NICE_CUE.search(clause)
                for lang, rx in _LANG_RE.items():
                    if rx.search(clause):
                        found[lang] = found.get(lang, False) or is_req
    return [l if req_ else f"{l} (plus)" for l, req_ in found.items()]


EDU_RULES = [
    ("PhD", r"\bph\.?d\b|doctorate|doctoral|tohtori|promotion\b"),
    ("Master's", r"\bmaster'?s?\b|\bm\.?sc\b|\bmba\b|diplom\b|maisteri|ylempi korkeakoulu|\bdi\b"),
    ("Bachelor's", r"bachelor'?s?|\bb\.?sc\b|\bb\.?a\.?\b|kandidaat|\bamk\b"),
    ("Degree", r"\bdegree\b|university|universität|hochschul|studium|korkeakoulu|tutkinto"),
]
EQUIV = re.compile(r"or equivalent|equivalent (practical |work )?experience|or similar|"
                   r"vergleichbar|tai vastaava", re.I)


def find_education(texts: list[str]) -> str:
    joined = " ".join(texts)
    for label, rx in EDU_RULES:
        if re.search(rx, joined, re.I):
            return label + (" (or equivalent)" if EQUIV.search(joined) else "")
    return ""


# Seniority ------------------------------------------------------------------
LEVELS = ["intern", "junior", "mid", "senior", "lead", "executive"]
TITLE_LEVELS = [
    ("intern", r"\bintern(ship)?\b|trainee|harjoittelij|praktikant|werkstudent|working student|"
               r"thesis|summer (job|worker)|kesätyö"),
    ("executive", r"\bchief\b|\bc[a-z]?o\b|\bvp\b|vice president|director|head of|johtaja|leiter"),
    ("lead", r"\blead\b|principal|\bstaff\b|team lead|esihenkilö|teamleiter"),
    ("senior", r"\bsenior\b|\bsr\.?\b|experienced|kokenut|\biii\b|\biv\b"),
    ("junior", r"\bjunior\b|\bjr\.?\b|graduate|entry[\s-]level|\bassociate\b|nuorempi"),
    ("mid", r"\bii\b|\bmid[\s-]?level\b|intermediate"),
]
HINT_LEVELS = [
    ("intern", r"intern|student|trainee"),
    ("executive", r"executive|director"),
    ("", r"mid[\s-]?senior|experienced|professional"),   # broad: decide by years
    ("senior", r"senior"),
    ("junior", r"entry|junior|associate|graduate"),
    ("mid", r"mid"),
]


def detect_seniority(title: str, hint: str, min_years: int | None) -> tuple[str, str]:
    """Return (level, source) where source is 'title', 'board', 'years' or ''."""
    for level, rx in TITLE_LEVELS:
        if re.search(rx, title, re.I):
            return level, "title"
    broad = False
    for level, rx in HINT_LEVELS:
        if hint and re.search(rx, hint, re.I):
            if level:
                return level, "board"
            broad = True
            break
    if min_years is not None:
        if min_years <= 1:
            return "junior", "years"
        if min_years <= 4:
            return "mid", "years"
        if min_years <= 7:
            return "senior", "years"
        return "lead", "years"
    return ("mid", "board") if broad else ("", "")


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def enrich(job: Job) -> Job:
    sections = html_to_sections(job.description_html)

    role = [s for s in sections if s.kind == "role"]
    reqs = [s for s in sections if s.kind == "requirements"]
    nices = [s for s in sections if s.kind == "nice"]

    # ---- requirements & nice-to-have --------------------------------------
    req_items: list[str] = []
    nice_items: list[str] = []
    req_raw: list[str] = []
    for s in reqs:
        items = s.bullets or [x for p in s.paragraphs for x in sentences(p)]
        req_raw += items
        for it in items:
            (nice_items if is_nice(it) else req_items).append(it)
    for s in nices:
        nice_items += s.bullets or [x for p in s.paragraphs for x in sentences(p)]

    if not req_items:  # fallback: requirement-like bullets/sentences elsewhere
        for s in sections:
            if s.kind in ("offer", "about"):
                continue
            pool = s.bullets + [x for p in s.paragraphs for x in sentences(p)]
            req_items += [x for x in pool if REQ_CUE.search(x) and not ABOUT_CUE.search(x)]
        req_items = req_items[:8]

    # ---- responsibilities ---------------------------------------------------
    resp_items = [b for s in role for b in s.bullets]
    if not resp_items:
        resp_items = [b for s in sections if s.kind == "other" for b in s.bullets
                      if not REQ_CUE.search(b)]

    # ---- summary --------------------------------------------------------------
    candidates = [p for s in role for p in s.paragraphs]
    candidates += [p for s in sections if s.kind == "other" for p in s.paragraphs]
    preferred = [p for p in candidates if not ABOUT_CUE.search(p) and len(p) > 40]
    pool = preferred or candidates
    summary = ""
    if pool:
        summary = " ".join(sentences(pool[0])[:2]) or pool[0]
    elif resp_items:
        summary = "Main tasks: " + "; ".join(r.rstrip(".") for r in resp_items[:3]) + "."

    job.summary = shorten(summary, 320)
    job.responsibilities = [shorten(x, 140) for x in _dedupe(resp_items)[:4]]
    job.criteria = [shorten(x, 160) for x in _dedupe(req_items)[:6]]
    job.nice_to_have = [shorten(x, 140) for x in _dedupe(nice_items)[:3]]

    # ---- structured facts -----------------------------------------------------
    all_text = [p for s in sections for p in (s.paragraphs + s.bullets)]
    years_txt, min_years = find_years(req_items + nice_items + all_text)
    if not years_txt and job.years_hint:
        years_txt = job.years_hint.replace("-", "–")
        if re.fullmatch(r"[\d–+ ]+", years_txt):
            years_txt += " years"
        m = re.search(r"\d+", job.years_hint)
        min_years = int(m.group()) if m else None
    job.years_experience = years_txt
    job.languages = find_languages(req_raw or req_items or all_text,
                                   [n for n in nice_items if n not in req_raw])
    job.education = find_education(req_items or all_text)
    job.seniority, _src = detect_seniority(job.title, job.seniority_hint, min_years)

    if not job.workplace:
        blob = (job.location + " " + job.title).lower()
        if "remote" in blob:
            job.workplace = "remote"
        elif "hybrid" in blob:
            job.workplace = "hybrid"
    return job


def plain_text(job: Job) -> str:
    """Full description as plain text (used for skill matching)."""
    secs = html_to_sections(job.description_html)
    return " \n".join([job.title] + [p for s in secs for p in (s.paragraphs + s.bullets)])
