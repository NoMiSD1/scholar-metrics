from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

import requests


REPOSITORY_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "scholars.json"
SERPAPI_URL = "https://serpapi.com/search.json"
REQUEST_TIMEOUT = (5, 30)
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SINCE_KEY_PATTERN = re.compile(r"^[a-z_]+_(\d{4})$")


class ConfigError(RuntimeError):
    """Raised when scholars.json is missing or unsafe."""


class ScholarUpdateError(RuntimeError):
    """Raised for a handled request or researcher-response failure."""


@dataclass(frozen=True)
class Scholar:
    slug: str
    name: str
    scholar_id: str
    orcid: str | None = None
    openalex_id: str | None = None


@dataclass(frozen=True)
class Configuration:
    scholars: tuple[Scholar, ...]
    legacy_slug: str


@dataclass(frozen=True)
class UpdateSummary:
    updated: tuple[str, ...]
    retained: tuple[str, ...]
    partial: tuple[str, ...]
    unavailable: tuple[str, ...]
    chart_retained: tuple[str, ...] = ()
    chart_unavailable: tuple[str, ...] = ()


def _required_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{location} must be a non-empty string.")
    return value.strip()


def _optional_string(value: Any, location: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, location)


def load_config(path: Path) -> Configuration:
    """Load and validate every scholar before any paid request is made."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid JSON in {path} at line {exc.lineno}, column {exc.colno}."
        ) from exc

    if not isinstance(raw, dict):
        raise ConfigError("The configuration root must be a JSON object.")

    legacy_slug = _required_string(raw.get("legacy_slug"), "legacy_slug")
    if not SLUG_PATTERN.fullmatch(legacy_slug):
        raise ConfigError("legacy_slug must use lowercase kebab-case.")

    entries = raw.get("scholars")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("scholars must be a non-empty JSON array.")

    scholars: list[Scholar] = []
    slugs: set[str] = set()
    scholar_ids: set[str] = set()

    for index, entry in enumerate(entries):
        location = f"scholars[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{location} must be a JSON object.")

        slug = _required_string(entry.get("slug"), f"{location}.slug")
        if not SLUG_PATTERN.fullmatch(slug):
            raise ConfigError(
                f"{location}.slug must use lowercase kebab-case without path separators."
            )

        name = _required_string(entry.get("name"), f"{location}.name")
        scholar_id = _required_string(
            entry.get("scholar_id"), f"{location}.scholar_id"
        )
        if any(character.isspace() for character in scholar_id) or any(
            character in scholar_id for character in "/\\"
        ):
            raise ConfigError(
                f"{location}.scholar_id must not contain whitespace or path separators."
            )

        if slug in slugs:
            raise ConfigError(f"Duplicate scholar slug: {slug}")
        if scholar_id in scholar_ids:
            raise ConfigError(f"Duplicate Google Scholar author ID: {scholar_id}")

        slugs.add(slug)
        scholar_ids.add(scholar_id)
        scholars.append(
            Scholar(
                slug=slug,
                name=name,
                scholar_id=scholar_id,
                orcid=_optional_string(entry.get("orcid"), f"{location}.orcid"),
                openalex_id=_optional_string(
                    entry.get("openalex_id"), f"{location}.openalex_id"
                ),
            )
        )

    if legacy_slug not in slugs:
        raise ConfigError("legacy_slug must match one configured scholar slug.")

    return Configuration(scholars=tuple(scholars), legacy_slug=legacy_slug)


def fetch_author(
    scholar_id: str,
    api_key: str,
    session: Any = requests,
) -> Mapping[str, Any]:
    """Make exactly one, non-retried SerpApi Author API request."""

    try:
        response = session.get(
            SERPAPI_URL,
            params={
                "engine": "google_scholar_author",
                "author_id": scholar_id,
                "hl": "en",
                "api_key": api_key,
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.Timeout:
        raise ScholarUpdateError("SerpApi request timed out") from None
    except requests.ConnectionError:
        raise ScholarUpdateError("could not connect to SerpApi") from None
    except requests.RequestException:
        # Raw Requests exception text can contain the URL, including api_key.
        raise ScholarUpdateError("SerpApi request failed") from None

    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int):
        raise ScholarUpdateError("SerpApi returned an invalid HTTP response")
    if status_code >= 400:
        raise ScholarUpdateError(f"SerpApi returned HTTP {status_code}")

    try:
        payload = response.json()
    except (ValueError, requests.RequestException):
        raise ScholarUpdateError("SerpApi returned invalid JSON") from None

    if not isinstance(payload, dict):
        raise ScholarUpdateError("SerpApi returned an invalid JSON object")
    return payload


def _nonnegative_int(value: Any, location: str) -> int:
    if isinstance(value, bool):
        raise ScholarUpdateError(f"{location} must be a non-negative integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        result = int(value.strip())
    else:
        raise ScholarUpdateError(f"{location} must be a non-negative integer")

    if result < 0:
        raise ScholarUpdateError(f"{location} must be a non-negative integer")
    return result


def _metric_table(cited_by: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    table = cited_by.get("table")
    if not isinstance(table, list):
        raise ScholarUpdateError("SerpApi response is missing the metrics table")

    result: dict[str, Mapping[str, Any]] = {}
    for entry in table:
        if not isinstance(entry, dict):
            continue
        for name, values in entry.items():
            if isinstance(name, str) and isinstance(values, dict):
                result[name] = values
    return result


def _parse_history(
    cited_by: Mapping[str, Any], current_year: int
) -> list[dict[str, int]]:
    graph = cited_by.get("graph")
    if not isinstance(graph, list):
        raise ScholarUpdateError("SerpApi response is missing citation history")
    if not graph:
        raise ScholarUpdateError("SerpApi returned empty citation history")

    history: list[dict[str, int]] = []
    seen_years: set[int] = set()
    for index, point in enumerate(graph):
        if not isinstance(point, dict):
            raise ScholarUpdateError(
                f"cited_by.graph[{index}] must be a JSON object"
            )
        year = _nonnegative_int(point.get("year"), f"cited_by.graph[{index}].year")
        citations = _nonnegative_int(
            point.get("citations"), f"cited_by.graph[{index}].citations"
        )
        if year < 1000 or year > current_year:
            raise ScholarUpdateError(
                f"cited_by.graph[{index}].year is outside the supported range"
            )
        if year in seen_years:
            raise ScholarUpdateError(f"citation history contains duplicate year {year}")
        seen_years.add(year)
        history.append({"year": year, "citations": citations})

    history.sort(key=lambda point: point["year"])
    return history


def _safe_strings(source: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in fields:
        value = source.get(field)
        if isinstance(value, str) and value.strip():
            result[field] = value.strip()
    return result


def _author_details(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_author = payload.get("author")
    if not isinstance(raw_author, dict):
        return {}

    author: dict[str, Any] = _safe_strings(
        raw_author, ("name", "affiliations", "email", "thumbnail")
    )
    raw_interests = raw_author.get("interests")
    if isinstance(raw_interests, list):
        interests: list[dict[str, str]] = []
        for interest in raw_interests:
            if isinstance(interest, dict):
                safe_interest = _safe_strings(interest, ("title", "link"))
                if "title" in safe_interest:
                    interests.append(safe_interest)
        if interests:
            author["interests"] = interests
    return author


def _article_details(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_articles = payload.get("articles")
    if not isinstance(raw_articles, list):
        return []

    articles: list[dict[str, Any]] = []
    for raw_article in raw_articles:
        if not isinstance(raw_article, dict):
            continue
        article: dict[str, Any] = _safe_strings(
            raw_article,
            ("title", "link", "citation_id", "authors", "publication"),
        )
        if "title" not in article:
            continue

        try:
            article["year"] = _nonnegative_int(
                raw_article.get("year"), "articles[].year"
            )
        except ScholarUpdateError:
            pass

        raw_cited_by = raw_article.get("cited_by")
        if isinstance(raw_cited_by, dict):
            cited_by = _safe_strings(raw_cited_by, ("link", "cites_id"))
            try:
                cited_by["value"] = _nonnegative_int(
                    raw_cited_by.get("value"), "articles[].cited_by.value"
                )
            except ScholarUpdateError:
                pass
            if cited_by:
                article["cited_by"] = cited_by
        articles.append(article)
    return articles


def _coauthor_details(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    raw_coauthors = payload.get("co_authors")
    if not isinstance(raw_coauthors, list):
        return []

    coauthors: list[dict[str, str]] = []
    for raw_coauthor in raw_coauthors:
        if not isinstance(raw_coauthor, dict):
            continue
        coauthor = _safe_strings(
            raw_coauthor,
            (
                "name",
                "link",
                "author_id",
                "affiliations",
                "email",
                "thumbnail",
            ),
        )
        if "name" in coauthor:
            coauthors.append(coauthor)
    return coauthors


def _public_access_details(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_public_access = payload.get("public_access")
    if not isinstance(raw_public_access, dict):
        return {}

    public_access: dict[str, Any] = _safe_strings(raw_public_access, ("link",))
    for field in ("available", "not_available"):
        try:
            public_access[field] = _nonnegative_int(
                raw_public_access.get(field), f"public_access.{field}"
            )
        except ScholarUpdateError:
            pass
    return public_access


def _parse_author_response_with_history_status(
    scholar: Scholar,
    data: Mapping[str, Any],
    updated: date,
    current_year: int | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Parse required metrics and report optional citation-history failures."""

    if data.get("error") is not None:
        raise ScholarUpdateError("SerpApi returned an error response")

    search_metadata = data.get("search_metadata")
    if isinstance(search_metadata, dict):
        status = search_metadata.get("status")
        if status is not None and status != "Success":
            raise ScholarUpdateError("SerpApi search did not complete successfully")

    cited_by = data.get("cited_by")
    if not isinstance(cited_by, dict):
        raise ScholarUpdateError("SerpApi response is missing cited_by data")

    table = _metric_table(cited_by)
    metric_sources = {
        "citations": table.get("citations"),
        "hindex": table.get("h_index"),
        "i10index": table.get("i10_index"),
    }

    metrics: dict[str, int] = {}
    for output_name, source in metric_sources.items():
        if not isinstance(source, dict) or "all" not in source:
            raise ScholarUpdateError(f"SerpApi response is missing {output_name}")
        metrics[output_name] = _nonnegative_int(
            source["all"], f"cited_by.table.{output_name}.all"
        )

    result: dict[str, Any] = {
        "schema_version": 1,
        "slug": scholar.slug,
        "name": scholar.name,
        "scholar_id": scholar.scholar_id,
        "citations": metrics["citations"],
        "hindex": metrics["hindex"],
        "i10index": metrics["i10index"],
        "updated": updated.isoformat(),
    }

    chart_year = current_year if current_year is not None else updated.year
    citation_history_error: str | None = None
    try:
        result["citations_by_year"] = _parse_history(cited_by, chart_year)
    except ScholarUpdateError as exc:
        citation_history_error = str(exc)
    if scholar.orcid:
        result["orcid"] = scholar.orcid
    if scholar.openalex_id:
        result["openalex_id"] = scholar.openalex_id

    since_periods: dict[int, dict[str, int]] = {}
    for output_name, source in metric_sources.items():
        assert isinstance(source, dict)
        for key, value in source.items():
            if key == "all" or not isinstance(key, str):
                continue
            match = SINCE_KEY_PATTERN.fullmatch(key)
            if not match:
                continue
            since_year = int(match.group(1))
            try:
                parsed_value = _nonnegative_int(
                    value, f"cited_by.table.{output_name}.{key}"
                )
            except ScholarUpdateError:
                continue
            since_periods.setdefault(since_year, {})[output_name] = parsed_value

    if since_periods:
        since_year = max(since_periods)
        result["metrics_since"] = {
            "year": since_year,
            **since_periods[since_year],
        }

    author = _author_details(data)
    if author:
        result["author"] = author

    articles = _article_details(data)
    if articles:
        result["articles"] = articles

    public_access = _public_access_details(data)
    if public_access:
        result["public_access"] = public_access

    coauthors = _coauthor_details(data)
    if coauthors:
        result["coauthors"] = coauthors

    return result, citation_history_error


def parse_author_response(
    scholar: Scholar,
    data: Mapping[str, Any],
    updated: date,
    current_year: int | None = None,
) -> dict[str, Any]:
    """Parse primary metrics and any valid optional citation history."""

    result, _ = _parse_author_response_with_history_status(
        scholar,
        data,
        updated,
        current_year,
    )
    return result


def render_metrics_json(metrics: Mapping[str, Any]) -> str:
    return json.dumps(metrics, ensure_ascii=False, indent=2) + "\n"


def render_legacy_metrics_json(metrics: Mapping[str, Any]) -> str:
    """Render the exact four-field JSON schema used by existing consumers."""

    updated = metrics.get("updated")
    if not isinstance(updated, str):
        raise ScholarUpdateError("updated must use YYYY-MM-DD")
    try:
        parsed_updated = date.fromisoformat(updated)
    except ValueError:
        raise ScholarUpdateError("updated must use YYYY-MM-DD") from None
    if parsed_updated.isoformat() != updated:
        raise ScholarUpdateError("updated must use YYYY-MM-DD")

    legacy_metrics = {
        "citations": _nonnegative_int(metrics.get("citations"), "citations"),
        "hindex": _nonnegative_int(metrics.get("hindex"), "hindex"),
        "i10index": _nonnegative_int(metrics.get("i10index"), "i10index"),
        "updated": updated,
    }
    return json.dumps(legacy_metrics, ensure_ascii=False, indent=2)


def render_compact_svg(metrics: Mapping[str, Any]) -> str:
    citations = _nonnegative_int(metrics.get("citations"), "citations")
    hindex = _nonnegative_int(metrics.get("hindex"), "hindex")
    i10index = _nonnegative_int(metrics.get("i10index"), "i10index")
    citations_text = f"{citations:,}"

    return f"""<svg xmlns="http://www.w3.org/2000/svg"
     width="280"
     height="22"
     viewBox="0 0 280 22">

  <text
    x="0"
    y="15"
    font-family="Arial, Helvetica, sans-serif"
    font-size="13"
    fill="#444444">

    <tspan font-weight="bold">{citations_text}</tspan>
    <tspan> citations · h-index </tspan>
    <tspan font-weight="bold">{hindex}</tspan>
    <tspan> · i10-index </tspan>
    <tspan font-weight="bold">{i10index}</tspan>

  </text>

</svg>
"""


def _nice_axis(maximum: int) -> tuple[int, list[int]]:
    step = max(1, (maximum + 3) // 4)
    axis_maximum = step * 4
    return axis_maximum, [step * index for index in range(5)]


def _coordinate(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def render_citations_svg(
    metrics: Mapping[str, Any], current_year: int
) -> str:
    raw_history = metrics.get("citations_by_year")
    if not isinstance(raw_history, list):
        raise ScholarUpdateError("citations_by_year must be a list")

    history: list[dict[str, int]] = []
    seen_years: set[int] = set()
    for index, point in enumerate(raw_history):
        if not isinstance(point, dict):
            raise ScholarUpdateError(f"citations_by_year[{index}] must be an object")
        year = _nonnegative_int(point.get("year"), f"citations_by_year[{index}].year")
        citations = _nonnegative_int(
            point.get("citations"), f"citations_by_year[{index}].citations"
        )
        if year in seen_years:
            raise ScholarUpdateError(f"citations_by_year contains duplicate year {year}")
        seen_years.add(year)
        history.append({"year": year, "citations": citations})

    history.sort(key=lambda point: point["year"])
    display_history = list(history)
    if current_year not in seen_years:
        display_history.append({"year": current_year, "citations": 0})
        display_history.sort(key=lambda point: point["year"])

    if not display_history:
        display_history = [{"year": current_year, "citations": 0}]

    plot_left = 46.0
    plot_right = 510.0
    plot_top = 36.0
    plot_bottom = 210.0
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top
    slot_width = plot_width / len(display_history)
    bar_width = max(2.0, min(24.0, slot_width * 0.66))
    maximum, ticks = _nice_axis(
        max(point["citations"] for point in display_history)
    )

    label_step = max(1, math.ceil(30 / slot_width))
    count_step = max(1, math.ceil(22 / slot_width))
    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="520" height="270" viewBox="0 0 520 270" role="img" aria-labelledby="chart-title chart-description">',
        '  <title id="chart-title">Citations per year</title>',
        '  <desc id="chart-description">Google Scholar citation counts by year. The current year is year to date.</desc>',
        '  <g font-family="Arial, Helvetica, sans-serif">',
        '    <text x="46" y="18" font-size="13" font-weight="600" fill="#444444">Citations per year</text>',
    ]

    for tick in ticks:
        y = plot_bottom - (tick / maximum * plot_height)
        tick_label = str(tick)
        lines.extend(
            [
                f'    <line x1="{_coordinate(plot_left)}" y1="{_coordinate(y)}" x2="{_coordinate(plot_right)}" y2="{_coordinate(y)}" stroke="#e5e5e5" stroke-width="1"/>',
                f'    <text x="39" y="{_coordinate(y + 3)}" text-anchor="end" font-size="9" fill="#666666">{tick_label}</text>',
            ]
        )

    for index, point in enumerate(display_history):
        center_x = plot_left + slot_width * (index + 0.5)
        bar_height = point["citations"] / maximum * plot_height
        bar_y = plot_bottom - bar_height
        bar_x = center_x - bar_width / 2
        year = point["year"]
        citations = point["citations"]
        label = f"{year}*" if year == current_year else str(year)
        safe_tooltip = html.escape(f"{year}: {citations} citations", quote=True)
        lines.append(
            f'    <rect x="{_coordinate(bar_x)}" y="{_coordinate(bar_y)}" width="{_coordinate(bar_width)}" height="{_coordinate(max(bar_height, 0.8))}" fill="#4285f4"><title>{safe_tooltip}</title></rect>'
        )

        show_count = index % count_step == 0 or index == len(display_history) - 1
        if show_count:
            count_y = max(30.0, bar_y - 4)
            lines.append(
                f'    <text x="{_coordinate(center_x)}" y="{_coordinate(count_y)}" text-anchor="middle" font-size="9" fill="#444444">{citations}</text>'
            )

        show_year = (
            index % label_step == 0
            or index == len(display_history) - 1
            or year == current_year
        )
        if show_year:
            lines.append(
                f'    <text x="{_coordinate(center_x)}" y="228" text-anchor="middle" font-size="9" fill="#555555">{label}</text>'
            )

    lines.extend(
        [
            '    <text x="510" y="258" text-anchor="end" font-size="9" fill="#666666">* year to date</text>',
            "  </g>",
            "</svg>",
            "",
        ]
    )
    return "\n".join(lines)


def _validate_artifacts(
    metrics_json: str,
    compact_svg: str,
    citations_svg: str | None,
    legacy_metrics_json: str | None,
    forbidden_secret: str,
) -> None:
    try:
        json.loads(metrics_json)
        ET.fromstring(compact_svg)
        if citations_svg is not None:
            ET.fromstring(citations_svg)
        if legacy_metrics_json is not None:
            legacy_payload = json.loads(legacy_metrics_json)
            if list(legacy_payload) != [
                "citations",
                "hindex",
                "i10index",
                "updated",
            ]:
                raise RuntimeError("Legacy metrics JSON schema validation failed")
    except (json.JSONDecodeError, ET.ParseError) as exc:
        raise RuntimeError("Generated artifact validation failed") from exc
    artifacts = [metrics_json, compact_svg]
    if citations_svg is not None:
        artifacts.append(citations_svg)
    if legacy_metrics_json is not None:
        artifacts.append(legacy_metrics_json)
    if forbidden_secret and any(
        forbidden_secret in artifact for artifact in artifacts
    ):
        raise ScholarUpdateError("SerpApi response contained request credentials")


def _artifact_paths(output_root: Path, slug: str) -> tuple[Path, Path, Path]:
    return (
        output_root / "metrics" / f"{slug}.json",
        output_root / "svg" / f"{slug}.svg",
        output_root / "charts" / f"{slug}-citations.svg",
    )


def _atomic_write_bundle(outputs: Mapping[Path, str]) -> None:
    temporary_paths: list[tuple[Path, Path]] = []
    backups: dict[Path, Path | None] = {}
    replaced: list[Path] = []
    preserved_backups: set[Path] = set()
    try:
        for destination, content in outputs.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            )
            temporary = Path(handle.name)
            temporary_paths.append((temporary, destination))
            try:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()

        for _, destination in temporary_paths:
            if destination.is_file():
                backup_handle = tempfile.NamedTemporaryFile(
                    prefix=f".{destination.name}.",
                    suffix=".backup",
                    dir=destination.parent,
                    delete=False,
                )
                backup = Path(backup_handle.name)
                backup_handle.close()
                backups[destination] = backup
                shutil.copy2(destination, backup)
            else:
                backups[destination] = None

        for temporary, destination in temporary_paths:
            os.replace(temporary, destination)
            replaced.append(destination)
    except Exception:
        rollback_errors: list[OSError] = []
        for destination in reversed(replaced):
            backup = backups.get(destination)
            try:
                if backup is None:
                    destination.unlink(missing_ok=True)
                else:
                    os.replace(backup, destination)
                    backups[destination] = None
            except OSError as rollback_error:
                if backup is not None:
                    preserved_backups.add(backup)
                rollback_errors.append(rollback_error)
        if rollback_errors:
            raise RuntimeError("Artifact update failed and rollback was incomplete") from rollback_errors[0]
        raise
    finally:
        for temporary, _ in temporary_paths:
            temporary.unlink(missing_ok=True)
        for backup in backups.values():
            if backup is not None and backup not in preserved_backups:
                backup.unlink(missing_ok=True)


def run_updates(
    config: Configuration,
    api_key: str,
    output_root: Path,
    *,
    fetcher: Callable[[str, str], Mapping[str, Any]] = fetch_author,
    today: date | None = None,
    out: TextIO = sys.stdout,
) -> UpdateSummary:
    """Update each researcher independently and print a concise summary."""

    update_date = today or datetime.now(timezone.utc).date()
    updated: list[str] = []
    retained: list[tuple[str, str]] = []
    partial: list[tuple[str, str]] = []
    unavailable: list[tuple[str, str]] = []
    chart_retained: list[tuple[str, str]] = []
    chart_unavailable: list[tuple[str, str]] = []

    for scholar in config.scholars:
        print(f"Fetching Google Scholar profile via SerpApi: {scholar.name}", file=out)
        artifact_paths = _artifact_paths(output_root, scholar.slug)
        existing_paths = list(artifact_paths[:2])
        if scholar.slug == config.legacy_slug:
            existing_paths.extend(
                [output_root / "metrics.json", output_root / "scholar-metrics.svg"]
            )
        chart_existed = artifact_paths[2].is_file()

        try:
            response = fetcher(scholar.scholar_id, api_key)
            metrics, citation_history_error = _parse_author_response_with_history_status(
                scholar,
                response,
                updated=update_date,
                current_year=update_date.year,
            )
            metrics_json = render_metrics_json(metrics)
            compact_svg = render_compact_svg(metrics)
            citations_svg = (
                render_citations_svg(metrics, update_date.year)
                if citation_history_error is None
                else None
            )
            legacy_metrics_json = (
                render_legacy_metrics_json(metrics)
                if scholar.slug == config.legacy_slug
                else None
            )
            _validate_artifacts(
                metrics_json,
                compact_svg,
                citations_svg,
                legacy_metrics_json,
                forbidden_secret=api_key,
            )
        except ScholarUpdateError as exc:
            reason = str(exc)
            if api_key:
                reason = reason.replace(api_key, "[REDACTED]")
            print(f"Could not update {scholar.name}: {reason}.", file=out)
            existing = [path.is_file() for path in existing_paths]
            if all(existing):
                target = retained
            elif any(existing):
                target = partial
            else:
                target = unavailable
            target.append((scholar.name, reason))
            continue

        outputs: dict[Path, str] = {
            artifact_paths[0]: metrics_json,
            artifact_paths[1]: compact_svg,
        }
        if citations_svg is not None:
            outputs[artifact_paths[2]] = citations_svg
        if scholar.slug == config.legacy_slug:
            assert legacy_metrics_json is not None
            outputs[output_root / "metrics.json"] = legacy_metrics_json
            outputs[output_root / "scholar-metrics.svg"] = compact_svg

        _atomic_write_bundle(outputs)
        updated.append(scholar.name)
        if citation_history_error is not None:
            target = chart_retained if chart_existed else chart_unavailable
            target.append((scholar.name, citation_history_error))

    print("\nUpdated primary metrics:", file=out)
    if updated:
        for name in updated:
            print(f"  {name}", file=out)
    else:
        print("  (none)", file=out)

    print("\nRetained previous primary metrics:", file=out)
    if retained:
        for name, reason in retained:
            print(f"  {name} - {reason}", file=out)
    else:
        print("  (none)", file=out)

    if partial:
        print(
            "\nPartially retained previous primary metrics (artifact set incomplete):",
            file=out,
        )
        for name, reason in partial:
            print(f"  {name} - {reason}", file=out)

    if unavailable:
        print("\nNo previous primary metrics available:", file=out)
        for name, reason in unavailable:
            print(f"  {name} - {reason}", file=out)

    if chart_retained:
        print("\nCitation charts retained unchanged:", file=out)
        for name, reason in chart_retained:
            print(f"  {name} - {reason}", file=out)

    if chart_unavailable:
        print("\nCitation charts unavailable (no previous chart):", file=out)
        for name, reason in chart_unavailable:
            print(f"  {name} - {reason}", file=out)

    return UpdateSummary(
        updated=tuple(updated),
        retained=tuple(name for name, _ in retained),
        partial=tuple(name for name, _ in partial),
        unavailable=tuple(name for name, _ in unavailable),
        chart_retained=tuple(name for name, _ in chart_retained),
        chart_unavailable=tuple(name for name, _ in chart_unavailable),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update Google Scholar metrics.")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to scholars.json (default: repository scholars.json)",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        raise ConfigError("SERPAPI_KEY environment variable is not set.")

    run_updates(config, api_key, REPOSITORY_ROOT)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        print(f"Fatal configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
