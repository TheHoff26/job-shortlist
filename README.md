# Job shortlist

Collects open positions from the companies you choose, keeps the ones that fit
your profile, and publishes a ranked page with a short summary and the key
applicant criteria for each job. Runs daily on GitHub Actions and is hosted
free on GitHub Pages. No AI service or API key needed.

## What you get per job
- Match score 0–100 with the reasons behind it
- 1–2 sentence summary, main tasks, requirements, nice-to-haves
- Seniority, years of experience, languages, education, workplace, salary (when published)
- Which of your CV skills the posting mentions
- "New" flag for positions that appeared in the last few days
- Downloads: `jobs.csv` (Excel) and `jobs.json`
- A "Check LinkedIn" link per company: opens LinkedIn job search pre-filled with
  your titles, location, seniority and "posted in the last 7 days"

## Setup (about 10 minutes)
1. Create a new GitHub repository and upload the contents of this folder
   (keep the `.github` folder).
   - GitHub Free: Pages requires a **public** repository. Do not put your full
     CV in the repo; use the secret in step 4 instead.
2. **Settings → Pages**: Source "Deploy from a branch", branch `main`, folder `/docs`.
3. **Settings → Actions → General → Workflow permissions**: "Read and write permissions".
4. Optional: **Settings → Secrets and variables → Actions → New secret**
   `JOBFINDER_CV_TEXT` with the plain text of your CV (adds a similarity signal).
5. **Actions → Update job shortlist → Run workflow**. After 1–2 minutes the page
   is live at `https://<your-user>.github.io/<repo>/`.

Afterwards it runs every morning, and again whenever you change a file in `config/`.

## Adjust it
- `config/companies.yaml` – the company list. The file explains how to find a
  company's id from its careers URL. Supported: Greenhouse, Lever, Ashby,
  SmartRecruiters, Personio, Recruitee, Workday (experimental).
- `config/profile.yaml` – titles, excluded titles, seniority, locations, CV skills,
  minimum score, LinkedIn link settings.
- Companies whose careers site isn't supported can be added with `ats: linkedin`;
  they get a LinkedIn link but are not fetched. Add `linkedin_id` to any company
  to make its LinkedIn link filter by that exact company.

The page footer shows which companies were checked and which failed (usually a
wrong id or a company that uses an unsupported system).

## Run locally
```bash
pip install -r requirements.txt
python -m jobfinder          # writes docs/index.html, docs/jobs.csv, docs/jobs.json
python tests/test_pipeline.py   # offline test with sample data
```

## Limits
- LinkedIn is not read automatically: it has no public jobs API and its User
  Agreement forbids scraping. LinkedIn jobs therefore don't appear in the ranked
  list; use the links.
- Summaries are rule-based: they rely on headings such as "Requirements",
  "What you'll do", "Odotamme", "Das bringst du mit". Unusual layouts give rougher results.
- Companies with their own custom careers site (no supported job-board system)
  can't be read without a dedicated scraper.
- Workday has no official public API; it may stop working without notice.
