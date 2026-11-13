# AI-Powered Micro-Influencer Outreach System

A Streamlit prototype for discovering technology creators, importing verified profile data, qualifying creators, generating evidence-grounded email/Instagram DM drafts, reviewing drafts, and recording simulated email outreach.

## Workflow

1. Discover up to 100 technology, programming, and AI YouTube channels with the official YouTube Data API, or import an authorized CSV.
2. Filter/classify creators using verified follower count (5,000–100,000), engagement (at least 1%), and sourced technology/programming/AI evidence. Missing or unverified facts stay in review.
3. Generate personalized 60–90 word email and 15–30 word Instagram DM drafts using Groq. The model may select only exact excerpts from cited verified evidence; fixed templates compose the message.
4. Edit and approve each message version. Only creators with a current PASS, a verified public email, and an approved current email draft can enter the email simulation.
5. Record a simulated send, timestamp, status, message version, and recipient in the append-only activity log. A second simulation for the same creator is blocked. No email or Instagram DM is sent.
6. Export the influencer dataset and current personalized messages as CSV from the Overview tab.

## Setup

Requires Python 3.10 or later. In the VS Code PowerShell terminal:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
streamlit run app.py
```

The dashboard and tests work without API keys. YouTube discovery requires a Google Cloud project with YouTube Data API v3 enabled and an API key; enter it in the discovery panel. The key is used for the request and not saved by the app. Draft generation requires `GROQ_API_KEY` and `LLM_MODEL` in local `.env`. Never commit `.env`.

To run automated checks:

```powershell
python -m pytest -q
```

## Discovery and data quality

The YouTube provider searches channel results for `technology reviews`, `programming tutorials`, and `artificial intelligence creator`. It keeps channels with visible subscriber counts in the configured micro-influencer range, retrieves up to five latest public uploads, and calculates engagement as the mean of `(likes + comments) / subscribers * 100` for videos with public like and comment counts. It can return fewer than the requested 50 when public metrics are unavailable or search results do not yield enough matching channels. The dashboard reports the actual count; it never pads the dataset with invented profiles or metrics.

YouTube's public Data API does not expose creator contact email addresses. Those values are stored as `Not Found`. The API also does not provide audience demographics or website data in this workflow. Review source links and current metrics before outreach. Subscriber counts can be rounded by YouTube; engagement describes the sampled recent videos and is not an audited audience metric.

CSV import is also available. Download the header-only template from the app and use records from sources whose terms permit the research and business use. Required columns are `name`, `platform`, `profile_url`, `source_url`, and `collected_at`; optional fields support profile metrics, content evidence, themes, email and field-specific provenance. Empty email is stored as `Not Found`. No missing data is guessed. Rows with malformed URLs, emails, numbers, timestamps, or provenance are rejected with row-level reasons. Duplicate platform/profile URLs are skipped unless update mode is selected.

API source documentation: [YouTube Data API search.list](https://developers.google.com/youtube/v3/docs/search/list), [channels.list](https://developers.google.com/youtube/v3/docs/channels/list), [playlistItems.list](https://developers.google.com/youtube/v3/docs/playlistItems/list), and [videos.list](https://developers.google.com/youtube/v3/docs/videos/list).

## Qualification and message review

Follower and engagement values count only when marked `VERIFIED` with a field-specific source URL. A creator passes only when both metrics meet thresholds and sourced verified profile/content evidence supports a Technology, Artificial Intelligence, or Programming match. Verified evidence is an operator/source attestation; the application does not independently prove that a source is accurate or current. Missing or unverified required metrics result in `REVIEW_REQUIRED`. A verified out-of-range follower count or low engagement results in `FAIL`.

Draft generation requires the latest evaluation to be `PASS` and current source-backed niche evidence. Imported profile text is treated as untrusted input; the model has no tools and cannot add factual claims outside exact evidence excerpts and the fixed message templates. Word counts are enforced before save, edit, and approval. Each version stores citations, provider/model, timestamps, and approval status. Editing or regenerating creates a new pending-review version.

Instagram DMs are prepared for manual use only. This app does not automate Instagram sending. Email simulation is also only a demonstration: it makes no SMTP connection, sends no message, and records `SIMULATED_SENT` in SQLite. It requires a current approved email draft and verified contact provenance, and blocks repeated demo campaigns per creator.

## Data, configuration, and architecture

- `app.py`: Streamlit dashboard.
- `outreach/discovery/`: YouTube Data API and strict CSV providers plus shared import service.
- `outreach/qualification/`: deterministic qualification and evidence history.
- `outreach/personalization/`: Groq adapter, evidence-gated generation, versioning, editing, approval.
- `outreach/sending.py`: eligibility checks, simulated email attempt, duplicate protection, tracker query.
- `outreach/database.py`: SQLite schema and migrations through v7, including campaign, attempt, and append-only activity tables.
- `tests/`: isolated tests with temporary databases and fake providers; no live API or LLM requests.

SQLite defaults to `data/outreach.sqlite3`. `.env`, virtual environments, and local SQLite files are ignored by Git. Useful settings include `DATABASE_PATH`, `DEMO_MODE`, `MIN_FOLLOWERS` (default 5000), `MAX_FOLLOWERS` (default 100000), `MIN_ENGAGEMENT_RATE` (default 1.0), `LLM_PROVIDER` (default `groq`), and `LLM_MODEL`. SMTP settings in `.env.example` are validated only; live sending is not implemented.

## Current limitations and submission demo

The bundled local SQLite database is empty and is ignored by Git. Run YouTube discovery with your own API key (or import your sourced CSV) to create the test dataset. A 50-profile result depends on API availability and the search returning enough publicly measurable channels; this is not guaranteed by the provider. The app does not collect demographics, scrape social platforms, or send real email/DMs. For a demo, populate at least 50 profiles if the API returns them, show PASS/FAIL/review reasons, generate and approve drafts for qualified profiles, then use the Outreach tracker to record at least one simulated email. Capture screenshots or video after running the app; no demo media is included yet.
