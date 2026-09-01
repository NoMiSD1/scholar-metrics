# Google Scholar metrics

This repository retrieves public author metrics from Google Scholar through the
[SerpApi Google Scholar Author API](https://serpapi.com/google-scholar-author-api),
generates a metrics JSON file and compact SVG for each configured researcher, and
publishes the committed files through GitHub Pages. When citation history is
available in the same response, it also generates an optional combined metrics
and citation-history SVG for placement above a publication list. The standalone
compact SVGs are intended for embedding elsewhere in university profile pages.

SerpApi is the deliberate data source. The updater does not use `scholarly` and
does not scrape Google Scholar directly, because direct requests from GitHub
Actions are prone to blocking.

## Researcher configuration

All researchers are configured in [`scholars.json`](scholars.json). Its
`legacy_slug` selects the researcher whose primary metrics and compact SVG also
feed the backward-compatible root files. The initial configuration has this
shape:

```json
{
  "legacy_slug": "simon-betzold",
  "scholars": [
    {
      "slug": "simon-betzold",
      "name": "Simon Betzold",
      "scholar_id": "YXmK2hcAAAAJ"
    }
  ]
}
```

To add a researcher, append another object to `scholars`. No Python or GitHub
Actions workflow change is required:

```json
{
  "slug": "jane-doe",
  "name": "Jane Doe",
  "scholar_id": "GOOGLE_SCHOLAR_AUTHOR_ID",
  "orcid": "0000-0000-0000-0000",
  "openalex_id": "A1234567890"
}
```

`slug`, `name`, and `scholar_id` are required. Slugs must be unique, lowercase
kebab-case names because they become public filenames. `orcid` and `openalex_id`
are optional metadata fields reserved for future integrations; the updater does
not retrieve OpenAlex data.

## SerpApi key and request usage

The updater reads the API key from the `SERPAPI_KEY` environment variable. For
GitHub Actions, create a repository Actions secret named `SERPAPI_KEY` under
**Settings → Secrets and variables → Actions**. Never put the key in
`scholars.json`, source files, generated files, or commit history.

Each update uses exactly one SerpApi search per configured researcher. Primary
metrics and available author metadata, first-page publication data, and citation
history all come from that same Google Scholar Author response. Citation history
is optional: the updater does not paginate or make another request when it is
missing or unusable. This keeps usage predictable for SerpApi's 250-search
monthly free-plan allowance; manual workflow runs consume searches in addition
to scheduled runs.

## Schedule and failure behavior

The GitHub Actions workflow runs every Monday and Thursday at 04:00 UTC and can
also be started manually with `workflow_dispatch`. It tests the parser and SVG
generation with saved fixture data before making a live API request. Successful
changes are committed by `github-actions[bot]`; no commit is created when the
generated files are unchanged.

A temporary SerpApi or profile-response failure is isolated to that researcher.
The updater logs a concise error, keeps that researcher's last valid files, and
continues with the remaining entries. Handled retrieval failures therefore do not
fail the scheduled workflow. Invalid configuration, programming errors, and file
write failures remain fatal so repository defects are visible.

Citation-history problems are handled more narrowly. If the response still has
valid citation, h-index, and i10-index values but its citation graph is missing,
empty, or invalid, the primary JSON and compact SVG are updated normally. An
existing chart for that researcher is retained; if no chart exists yet, none is
created. This partial success never triggers an extra SerpApi request.

## Generated files

For every configured researcher with valid primary metrics, the updater writes:

```text
metrics/<slug>.json
svg/<slug>.svg
```

The per-person JSON is the rich output. It includes the researcher's name and
Scholar ID, total citations, h-index, i10-index, and an update date. It may also
contain citation counts by year and useful author, since-year, interest, and
first-page publication fields available in the same response.

The compact metrics SVG has a transparent 280 × 22 canvas and preserves the
established horizontal appearance:

```text
1,519 citations · h-index 19 · i10-index 24
```

When the response contains usable citation history, the updater also writes:

```text
charts/<slug>-citations.svg
```

This optional combined chart is a transparent, responsive 520 × 240 SVG. It
repeats total citations, h-index, and i10-index above restrained vertical bars in
University of Würzburg blue (`#00549F`). The current year is marked with `*` and
identified as year to date. A missing, empty, or invalid graph does not block the
two primary outputs and does not cause another API request.

For backward compatibility, the compact SVG for the researcher selected by
`legacy_slug` is also written to:

```text
scholar-metrics.svg
```

The root `metrics.json` is deliberately a smaller compatibility format rather
than a copy of the rich per-person JSON. It always has exactly these four fields:

```json
{
  "citations": 1521,
  "hindex": 19,
  "i10index": 24,
  "updated": "2026-09-01"
}
```

All richer fields—including names, Scholar IDs, citation history, since-year
metrics, affiliations, interests, optional identifiers, and publication
data—exist only in `metrics/<slug>.json`. The root files keep the existing TYPO3
embedding and `index.html` consumer working without changes.

The relevant repository structure is:

```text
.github/workflows/update-scholar.yml
charts/        # present when at least one valid citation graph has been received
metrics/
svg/
tests/
  fixtures/
  test_update_scholar.py
index.html
metrics.json
requirements.txt
scholar-metrics.svg
scholars.json
update_scholar.py
```

## Public URLs

GitHub Pages serves the generated files directly from this repository. Per-person
files use these patterns; the chart URL exists only after a valid citation graph
has been received:

```text
https://nomisd1.github.io/scholar-metrics/metrics/<slug>.json
https://nomisd1.github.io/scholar-metrics/svg/<slug>.svg
https://nomisd1.github.io/scholar-metrics/charts/<slug>-citations.svg
```

The repository did not previously store citation-by-year data. The per-person
metrics JSON and compact SVG become available after the researcher completes a
successful primary-metrics update. The chart becomes available only after a
response also contains usable citation history. Once created, an existing chart
is retained if a later response has valid primary metrics but unusable history.
The existing root compatibility files remain live in the meantime.

For Simon Betzold, the two image URLs are:

- <https://nomisd1.github.io/scholar-metrics/svg/simon-betzold.svg>
- <https://nomisd1.github.io/scholar-metrics/charts/simon-betzold-citations.svg>

The existing backward-compatible URLs remain:

- <https://nomisd1.github.io/scholar-metrics/metrics.json>
- <https://nomisd1.github.io/scholar-metrics/scholar-metrics.svg>

## TYPO3 and HTML embedding

Embed the compact metric line with:

```html
<img
  src="https://nomisd1.github.io/scholar-metrics/svg/simon-betzold.svg"
  alt="Google Scholar metrics"
>
```

When its optional chart URL exists, embed the responsive combined metrics and
citations-per-year graphic with:

```html
<img
  src="https://nomisd1.github.io/scholar-metrics/charts/simon-betzold-citations.svg"
  alt="Google Scholar metrics and citations per year"
  style="width:100%; max-width:520px; height:auto;"
>
```

The legacy compact SVG URL can remain in existing pages; migration to the
per-person URL is optional.

## Local validation

Install the dependency and run the fixture-based test suite without consuming a
SerpApi search:

```bash
python -m pip install --requirement requirements.txt
python -m unittest discover -s tests -v
```

Running the updater itself performs one live search per configured researcher and
therefore requires `SERPAPI_KEY`:

```bash
python update_scholar.py
```
