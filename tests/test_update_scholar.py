import copy
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import update_scholar


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "serpapi_author.json"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"


def load_fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def scholar_entry(
    slug="simon-betzold",
    name="Simon Betzold",
    scholar_id="YXmK2hcAAAAJ",
    **optional,
):
    return {
        "slug": slug,
        "name": name,
        "scholar_id": scholar_id,
        **optional,
    }


def load_test_config(root, scholars, legacy_slug="simon-betzold"):
    path = Path(root) / "scholars.json"
    path.write_text(
        json.dumps(
            {"legacy_slug": legacy_slug, "scholars": scholars},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return update_scholar.load_config(path)


def fixture_for(scholar_id, api_name):
    data = load_fixture()
    data["search_parameters"]["author_id"] = scholar_id
    data["author"]["name"] = api_name
    return data


def local_name(element):
    return element.tag.rsplit("}", 1)[-1]


def normalized_svg_text(root):
    return " ".join("".join(root.itertext()).split())


class ConfigurationTests(unittest.TestCase):
    def test_loads_optional_identifiers_without_code_changes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = load_test_config(
                temporary_directory,
                [
                    scholar_entry(
                        orcid="0000-0002-1825-0097",
                        openalex_id="A123456789",
                    )
                ],
            )

        self.assertEqual(config.legacy_slug, "simon-betzold")
        self.assertEqual(len(config.scholars), 1)
        scholar = config.scholars[0]
        self.assertEqual(scholar.slug, "simon-betzold")
        self.assertEqual(scholar.name, "Simon Betzold")
        self.assertEqual(scholar.scholar_id, "YXmK2hcAAAAJ")
        self.assertEqual(scholar.orcid, "0000-0002-1825-0097")
        self.assertEqual(scholar.openalex_id, "A123456789")

    def test_rejects_unsafe_slugs(self):
        unsafe_slugs = (
            "../escape",
            "/absolute",
            "nested/path",
            "contains space",
            "Uppercase",
            ".",
            "-leading",
            "trailing-",
            "double--hyphen",
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            for unsafe_slug in unsafe_slugs:
                with self.subTest(slug=unsafe_slug):
                    with self.assertRaises(update_scholar.ConfigError):
                        load_test_config(
                            temporary_directory,
                            [scholar_entry(slug=unsafe_slug)],
                            legacy_slug=unsafe_slug,
                        )

    def test_rejects_duplicate_slugs_and_scholar_ids(self):
        duplicate_cases = (
            [
                scholar_entry(),
                scholar_entry(
                    name="Other Name",
                    scholar_id="OtherScholarAAAAJ",
                ),
            ],
            [
                scholar_entry(),
                scholar_entry(
                    slug="other-name",
                    name="Other Name",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            for scholars in duplicate_cases:
                with self.subTest(scholars=scholars):
                    with self.assertRaises(update_scholar.ConfigError):
                        load_test_config(temporary_directory, scholars)

    def test_rejects_unknown_legacy_slug(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(update_scholar.ConfigError):
                load_test_config(
                    temporary_directory,
                    [scholar_entry()],
                    legacy_slug="not-configured",
                )


class ResponseParsingTests(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            self.scholar = load_test_config(
                temporary_directory,
                [scholar_entry()],
            ).scholars[0]

    def test_parses_official_mixed_integer_and_string_shapes(self):
        metrics = update_scholar.parse_author_response(
            self.scholar,
            load_fixture(),
            updated=date(2026, 9, 1),
            current_year=2026,
        )

        self.assertEqual(metrics["name"], "Simon Betzold")
        self.assertEqual(metrics["scholar_id"], "YXmK2hcAAAAJ")
        self.assertEqual(metrics["citations"], 1521)
        self.assertEqual(metrics["hindex"], 19)
        self.assertEqual(metrics["i10index"], 24)
        self.assertEqual(metrics["updated"], "2026-09-01")
        self.assertEqual(
            metrics["citations_by_year"],
            [
                {"year": 2024, "citations": 101},
                {"year": 2025, "citations": 212},
                {"year": 2026, "citations": 37},
            ],
        )
        self.assertTrue(
            all(
                type(point[field]) is int
                for point in metrics["citations_by_year"]
                for field in ("year", "citations")
            )
        )

    def test_preserves_since_profile_and_articles_but_whitelists_payload(self):
        metrics = update_scholar.parse_author_response(
            self.scholar,
            load_fixture(),
            updated=date(2026, 9, 1),
            current_year=2026,
        )

        self.assertEqual(
            metrics["metrics_since"],
            {
                "year": 2021,
                "citations": 817,
                "hindex": 15,
                "i10index": 18,
            },
        )
        self.assertEqual(metrics["author"]["name"], "Simon Betzold")
        self.assertEqual(
            metrics["author"]["affiliations"],
            "University of W\u00fcrzburg & Institute <Physics>",
        )
        self.assertEqual(
            [interest["title"] for interest in metrics["author"]["interests"]],
            ["Exciton-polaritons & photonics", "Quantum fluids"],
        )
        self.assertTrue(metrics["author"]["interests"][0]["link"].startswith("https://"))
        self.assertEqual(
            metrics["articles"][0]["title"],
            "Light & matter <strong coupling>",
        )
        self.assertEqual(metrics["articles"][0]["year"], 2025)
        self.assertIs(type(metrics["articles"][0]["year"]), int)
        self.assertEqual(metrics["articles"][0]["cited_by"]["value"], 17)
        self.assertEqual(metrics["articles"][1]["year"], 2024)
        self.assertIs(type(metrics["articles"][1]["year"]), int)

        serialized = json.dumps(metrics, ensure_ascii=False)
        self.assertNotIn("fixture-secret-must-not-leak", serialized)
        self.assertNotIn("unrecognized_profile_field", serialized)
        self.assertNotIn("unrecognized_article_field", serialized)
        self.assertNotIn("unrecognized_top_level_field", serialized)
        self.assertNotIn("search_metadata", metrics)
        self.assertNotIn("search_parameters", metrics)
        self.assertNotIn("serpapi_pagination", metrics)

    def test_missing_empty_or_invalid_graph_omits_only_optional_history(self):
        graph_variants = {
            "missing": None,
            "empty": [],
            "invalid": [{"year": 2027, "citations": "not-a-number"}],
        }

        for name, graph in graph_variants.items():
            with self.subTest(graph=name):
                data = load_fixture()
                if graph is None:
                    del data["cited_by"]["graph"]
                else:
                    data["cited_by"]["graph"] = graph

                metrics = update_scholar.parse_author_response(
                    self.scholar,
                    data,
                    updated=date(2026, 9, 1),
                    current_year=2026,
                )

                self.assertEqual(metrics["citations"], 1521)
                self.assertEqual(metrics["hindex"], 19)
                self.assertEqual(metrics["i10index"], 24)
                self.assertEqual(metrics["updated"], "2026-09-01")
                self.assertNotIn("citations_by_year", metrics)
                self.assertIn("metrics_since", metrics)
                self.assertIn("author", metrics)
                self.assertIn("articles", metrics)


class FetchTests(unittest.TestCase):
    def test_fetch_author_makes_one_non_paginated_request(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = load_fixture()
        session = Mock()
        session.get.return_value = response

        result = update_scholar.fetch_author(
            "YXmK2hcAAAAJ",
            "test-api-key",
            session=session,
        )

        self.assertEqual(result["cited_by"]["table"][0]["citations"]["all"], 1521)
        session.get.assert_called_once()
        request_url, = session.get.call_args.args
        request_options = session.get.call_args.kwargs
        self.assertEqual(request_url, "https://serpapi.com/search.json")
        self.assertEqual(
            request_options["params"],
            {
                "engine": "google_scholar_author",
                "author_id": "YXmK2hcAAAAJ",
                "hl": "en",
                "api_key": "test-api-key",
            },
        )
        self.assertIn("timeout", request_options)
        timeout = request_options["timeout"]
        timeout_values = timeout if isinstance(timeout, tuple) else (timeout,)
        self.assertTrue(timeout_values)
        self.assertTrue(all(float(value) > 0 for value in timeout_values))


class SvgRenderingTests(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            scholar = load_test_config(
                temporary_directory,
                [scholar_entry()],
            ).scholars[0]
        self.metrics = update_scholar.parse_author_response(
            scholar,
            load_fixture(),
            updated=date(2026, 9, 1),
            current_year=2026,
        )

    def test_compact_svg_is_exactly_backward_compatible_and_valid_xml(self):
        expected = """<svg xmlns="http://www.w3.org/2000/svg"
     width="280"
     height="22"
     viewBox="0 0 280 22">

  <text
    x="0"
    y="15"
    font-family="Arial, Helvetica, sans-serif"
    font-size="13"
    fill="#444444">

    <tspan font-weight="bold">1,521</tspan>
    <tspan> citations \u00b7 h-index </tspan>
    <tspan font-weight="bold">19</tspan>
    <tspan> \u00b7 i10-index </tspan>
    <tspan font-weight="bold">24</tspan>

  </text>

</svg>
"""

        svg = update_scholar.render_compact_svg(self.metrics)
        self.assertEqual(svg, expected)

        root = ET.fromstring(svg)
        self.assertEqual(root.tag, f"{{{SVG_NAMESPACE}}}svg")
        self.assertEqual(root.attrib["width"], "280")
        self.assertEqual(root.attrib["height"], "22")
        self.assertEqual(root.attrib["viewBox"], "0 0 280 22")
        self.assertFalse(list(root.findall(f"{{{SVG_NAMESPACE}}}rect")))

    def test_chart_is_valid_responsive_plain_svg_with_readable_ytd_data(self):
        svg = update_scholar.render_citations_svg(self.metrics, current_year=2026)
        root = ET.fromstring(svg)
        width = float(root.attrib["width"].removesuffix("px"))

        self.assertEqual(root.tag, f"{{{SVG_NAMESPACE}}}svg")
        self.assertGreaterEqual(width, 450)
        self.assertLessEqual(width, 550)
        self.assertIn("viewBox", root.attrib)

        text = normalized_svg_text(root)
        self.assertIn("Citations per year", text)
        self.assertIn("2026*", text)
        self.assertIn("* year to date", text)
        for citation_count in ("101", "212", "37"):
            self.assertIn(citation_count, text)

        elements = list(root.iter())
        forbidden = {
            "linearGradient",
            "radialGradient",
            "filter",
            "feDropShadow",
            "image",
            "script",
            "foreignObject",
        }
        self.assertFalse(forbidden.intersection(local_name(item) for item in elements))

        rectangles = [item for item in elements if local_name(item) == "rect"]
        self.assertGreaterEqual(len(rectangles), len(self.metrics["citations_by_year"]))
        self.assertFalse(
            any(
                item.attrib.get("x", "0") == "0"
                and item.attrib.get("y", "0") == "0"
                and item.attrib.get("width") in {"100%", root.attrib.get("width")}
                and item.attrib.get("height") in {"100%", root.attrib.get("height")}
                for item in rectangles
            ),
            "The chart must not contain an opaque full-canvas background rectangle.",
        )

    def test_chart_marks_current_year_even_when_api_omits_zero_ytd_point(self):
        metrics = copy.deepcopy(self.metrics)
        metrics["citations_by_year"] = metrics["citations_by_year"][:-1]

        root = ET.fromstring(
            update_scholar.render_citations_svg(metrics, current_year=2026)
        )
        text = normalized_svg_text(root)

        self.assertIn("2026*", text)
        self.assertIn("* year to date", text)

    def test_low_and_zero_citation_axes_use_only_integer_tick_labels(self):
        cases = (
            (
                [{"year": 2026, "citations": 0}],
                ["0", "1", "2", "3", "4"],
            ),
            (
                [
                    {"year": 2025, "citations": 3},
                    {"year": 2026, "citations": 1},
                ],
                ["0", "1", "2", "3", "4"],
            ),
        )

        for history, expected_ticks in cases:
            with self.subTest(history=history):
                root = ET.fromstring(
                    update_scholar.render_citations_svg(
                        {"citations_by_year": history},
                        current_year=2026,
                    )
                )
                tick_labels = [
                    element.text
                    for element in root.iter(f"{{{SVG_NAMESPACE}}}text")
                    if element.attrib.get("x") == "39"
                ]
                self.assertEqual(tick_labels, expected_ticks)
                self.assertTrue(
                    all(label is not None and label.isdecimal() for label in tick_labels)
                )

    def test_chart_scaling_handles_arbitrarily_large_integer_counts(self):
        metrics = copy.deepcopy(self.metrics)
        metrics["citations_by_year"] = [
            {"year": 2026, "citations": 10**400},
        ]

        svg = update_scholar.render_citations_svg(metrics, current_year=2026)
        root = ET.fromstring(svg)

        self.assertEqual(root.tag, f"{{{SVG_NAMESPACE}}}svg")
        self.assertIn(str(10**400), normalized_svg_text(root))

    def test_tallest_bar_count_label_is_positioned_above_its_bar(self):
        history = [
            {"year": 2024, "citations": 1},
            {"year": 2025, "citations": 4},
            {"year": 2026, "citations": 2},
        ]
        root = ET.fromstring(
            update_scholar.render_citations_svg(
                {"citations_by_year": history},
                current_year=2026,
            )
        )

        tallest_bar = next(
            rectangle
            for rectangle in root.iter(f"{{{SVG_NAMESPACE}}}rect")
            if rectangle.find(f"{{{SVG_NAMESPACE}}}title").text
            == "2025: 4 citations"
        )
        bar_center = float(tallest_bar.attrib["x"]) + (
            float(tallest_bar.attrib["width"]) / 2
        )
        count_label = next(
            element
            for element in root.iter(f"{{{SVG_NAMESPACE}}}text")
            if element.text == "4"
            and abs(float(element.attrib.get("x", "nan")) - bar_center) < 0.01
        )

        self.assertLess(float(count_label.attrib["y"]), float(tallest_bar.attrib["y"]))


class UpdateIntegrationTests(unittest.TestCase):
    API_KEY = "fixture-secret-must-not-leak"

    @staticmethod
    def expected_paths(root, slug, legacy=False):
        paths = (
            Path(root) / "metrics" / f"{slug}.json",
            Path(root) / "svg" / f"{slug}.svg",
            Path(root) / "charts" / f"{slug}-citations.svg",
        )
        if legacy:
            paths += (
                Path(root) / "metrics.json",
                Path(root) / "scholar-metrics.svg",
            )
        return paths

    def test_failure_retains_all_bytes_logs_safely_and_continues(self):
        simon = scholar_entry()
        ada = scholar_entry(
            slug="ada-example",
            name="Ada Example",
            scholar_id="AdaExampleAAAAJ",
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = load_test_config(root, [simon, ada])
            retained_content = {}
            for index, path in enumerate(
                self.expected_paths(root, "simon-betzold", legacy=True)
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.suffix == ".json":
                    content = (
                        b'{"name":"Simon Betzold","scholar_id":"YXmK2hcAAAAJ",'
                        b'"citations":1,"hindex":1,"i10index":0,'
                        b'"updated":"2026-08-31","citations_by_year":[]}\n'
                    )
                else:
                    content = (
                        f'<svg xmlns="{SVG_NAMESPACE}"><!-- sentinel {index} --></svg>\n'
                    ).encode("utf-8")
                path.write_bytes(content)
                retained_content[path] = content

            calls = []

            def fetcher(scholar_id, api_key):
                calls.append((scholar_id, api_key))
                if scholar_id == "YXmK2hcAAAAJ":
                    raise update_scholar.ScholarUpdateError(
                        "request failed at "
                        f"https://example.invalid?api_key={api_key}"
                    )
                return fixture_for(scholar_id, "Ada API Profile")

            output = io.StringIO()
            summary = update_scholar.run_updates(
                config,
                self.API_KEY,
                root,
                fetcher=fetcher,
                today=date(2026, 9, 1),
                out=output,
            )

            self.assertEqual(
                calls,
                [
                    ("YXmK2hcAAAAJ", self.API_KEY),
                    ("AdaExampleAAAAJ", self.API_KEY),
                ],
            )
            for path, content in retained_content.items():
                self.assertEqual(path.read_bytes(), content, path)

            for path in self.expected_paths(root, "ada-example"):
                self.assertTrue(path.is_file(), path)

            self.assertEqual(summary.updated, ("Ada Example",))
            self.assertEqual(summary.retained, ("Simon Betzold",))
            self.assertEqual(summary.partial, ())
            self.assertEqual(summary.unavailable, ())
            self.assertEqual(summary.chart_retained, ())
            self.assertEqual(summary.chart_unavailable, ())
            log = output.getvalue()
            self.assertIn("Ada Example", log)
            self.assertIn("Simon Betzold", log)
            self.assertIn("Retained previous primary metrics", log)
            self.assertNotIn(self.API_KEY, log)

    def test_failed_new_scholar_is_reported_unavailable_without_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = load_test_config(root, [scholar_entry()])

            def fetcher(_scholar_id, _api_key):
                raise update_scholar.ScholarUpdateError("temporary upstream error")

            summary = update_scholar.run_updates(
                config,
                self.API_KEY,
                root,
                fetcher=fetcher,
                today=date(2026, 9, 1),
                out=io.StringIO(),
            )

            self.assertEqual(summary.updated, ())
            self.assertEqual(summary.retained, ())
            self.assertEqual(summary.partial, ())
            self.assertEqual(summary.unavailable, ("Simon Betzold",))
            self.assertEqual(summary.chart_retained, ())
            self.assertEqual(summary.chart_unavailable, ())
            for path in self.expected_paths(root, "simon-betzold", legacy=True):
                self.assertFalse(path.exists(), path)

    def test_api_key_in_whitelisted_url_is_blocked_before_write_and_never_logged(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = load_test_config(root, [scholar_entry()])

            def fetcher(scholar_id, _api_key):
                data = fixture_for(scholar_id, "Simon Betzold")
                del data["cited_by"]["graph"]
                data["author"]["thumbnail"] = (
                    "https://images.example.invalid/avatar?api_key=" + self.API_KEY
                )
                return data

            output = io.StringIO()
            summary = update_scholar.run_updates(
                config,
                self.API_KEY,
                root,
                fetcher=fetcher,
                today=date(2026, 9, 1),
                out=output,
            )

            self.assertEqual(summary.updated, ())
            self.assertEqual(summary.retained, ())
            self.assertEqual(summary.partial, ())
            self.assertEqual(summary.unavailable, ("Simon Betzold",))
            self.assertEqual(summary.chart_retained, ())
            self.assertEqual(summary.chart_unavailable, ())
            for path in self.expected_paths(root, "simon-betzold", legacy=True):
                self.assertFalse(path.exists(), path)
            self.assertFalse(list(root.rglob("*.tmp")))
            self.assertIn("contained request credentials", output.getvalue())
            self.assertNotIn(self.API_KEY, output.getvalue())

    def test_legacy_root_files_alone_are_classified_partial(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = load_test_config(root, [scholar_entry()])
            legacy_content = {
                root / "metrics.json": b'{"citations": 1}\n',
                root / "scholar-metrics.svg": (
                    f'<svg xmlns="{SVG_NAMESPACE}"><!-- legacy --></svg>\n'
                ).encode("utf-8"),
            }
            for path, content in legacy_content.items():
                path.write_bytes(content)

            def fetcher(_scholar_id, _api_key):
                raise update_scholar.ScholarUpdateError("temporary upstream error")

            output = io.StringIO()
            summary = update_scholar.run_updates(
                config,
                self.API_KEY,
                root,
                fetcher=fetcher,
                today=date(2026, 9, 1),
                out=output,
            )

            self.assertEqual(summary.updated, ())
            self.assertEqual(summary.retained, ())
            self.assertEqual(summary.partial, ("Simon Betzold",))
            self.assertEqual(summary.unavailable, ())
            self.assertEqual(summary.chart_retained, ())
            self.assertEqual(summary.chart_unavailable, ())
            for path, content in legacy_content.items():
                self.assertEqual(path.read_bytes(), content)
            for path in self.expected_paths(root, "simon-betzold"):
                self.assertFalse(path.exists(), path)
            self.assertIn(
                "Partially retained previous primary metrics "
                "(artifact set incomplete)",
                output.getvalue(),
            )

    def test_optional_graph_variants_update_primary_and_report_chart_status(self):
        graph_variants = {
            "missing": None,
            "empty": [],
            "invalid": [{"year": "invalid", "citations": 4}],
        }

        for graph_name, graph in graph_variants.items():
            for chart_exists in (False, True):
                with self.subTest(graph=graph_name, chart_exists=chart_exists):
                    with tempfile.TemporaryDirectory() as temporary_directory:
                        root = Path(temporary_directory)
                        config = load_test_config(root, [scholar_entry()])
                        chart_path = (
                            root
                            / "charts"
                            / "simon-betzold-citations.svg"
                        )
                        previous_chart = (
                            f'<svg xmlns="{SVG_NAMESPACE}">'
                            f'<!-- retained {graph_name} --></svg>\n'
                        ).encode("utf-8")
                        if chart_exists:
                            chart_path.parent.mkdir(parents=True, exist_ok=True)
                            chart_path.write_bytes(previous_chart)

                        calls = []

                        def fetcher(scholar_id, api_key):
                            calls.append((scholar_id, api_key))
                            data = fixture_for(scholar_id, "Simon Betzold")
                            if graph is None:
                                del data["cited_by"]["graph"]
                            else:
                                data["cited_by"]["graph"] = copy.deepcopy(graph)
                            return data

                        output = io.StringIO()
                        summary = update_scholar.run_updates(
                            config,
                            self.API_KEY,
                            root,
                            fetcher=fetcher,
                            today=date(2026, 9, 1),
                            out=output,
                        )

                        self.assertEqual(
                            calls,
                            [("YXmK2hcAAAAJ", self.API_KEY)],
                        )
                        self.assertEqual(summary.updated, ("Simon Betzold",))
                        self.assertEqual(summary.retained, ())
                        self.assertEqual(summary.partial, ())
                        self.assertEqual(summary.unavailable, ())
                        if chart_exists:
                            self.assertEqual(
                                summary.chart_retained,
                                ("Simon Betzold",),
                            )
                            self.assertEqual(summary.chart_unavailable, ())
                            self.assertEqual(chart_path.read_bytes(), previous_chart)
                        else:
                            self.assertEqual(summary.chart_retained, ())
                            self.assertEqual(
                                summary.chart_unavailable,
                                ("Simon Betzold",),
                            )
                            self.assertFalse(chart_path.exists())

                        canonical_path = (
                            root / "metrics" / "simon-betzold.json"
                        )
                        compact_path = root / "svg" / "simon-betzold.svg"
                        self.assertTrue(canonical_path.is_file())
                        self.assertTrue(compact_path.is_file())
                        ET.fromstring(compact_path.read_text(encoding="utf-8"))

                        canonical = json.loads(
                            canonical_path.read_text(encoding="utf-8")
                        )
                        self.assertEqual(canonical["citations"], 1521)
                        self.assertEqual(canonical["hindex"], 19)
                        self.assertEqual(canonical["i10index"], 24)
                        self.assertNotIn("citations_by_year", canonical)
                        self.assertIn("author", canonical)
                        self.assertIn("articles", canonical)

                        legacy_path = root / "metrics.json"
                        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
                        self.assertEqual(
                            list(legacy),
                            ["citations", "hindex", "i10index", "updated"],
                        )
                        self.assertEqual(
                            legacy,
                            {
                                "citations": 1521,
                                "hindex": 19,
                                "i10index": 24,
                                "updated": "2026-09-01",
                            },
                        )
                        self.assertFalse(legacy_path.read_bytes().endswith(b"\n"))
                        self.assertEqual(
                            (root / "scholar-metrics.svg").read_bytes(),
                            compact_path.read_bytes(),
                        )

                        log = output.getvalue()
                        self.assertIn("Updated primary metrics:", log)
                        if chart_exists:
                            self.assertIn("Citation charts retained unchanged:", log)
                            self.assertNotIn("Citation charts unavailable", log)
                        else:
                            self.assertIn(
                                "Citation charts unavailable (no previous chart):",
                                log,
                            )
                            self.assertNotIn("Citation charts retained", log)

    def test_chartless_primary_update_is_complete_for_later_failure(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = load_test_config(root, [scholar_entry()])
            calls = []

            def graphless_fetcher(scholar_id, api_key):
                calls.append((scholar_id, api_key))
                data = fixture_for(scholar_id, "Simon Betzold")
                del data["cited_by"]["graph"]
                return data

            first_summary = update_scholar.run_updates(
                config,
                self.API_KEY,
                root,
                fetcher=graphless_fetcher,
                today=date(2026, 9, 1),
                out=io.StringIO(),
            )
            chart_path = root / "charts" / "simon-betzold-citations.svg"
            primary_paths = tuple(
                path
                for path in self.expected_paths(
                    root,
                    "simon-betzold",
                    legacy=True,
                )
                if path != chart_path
            )
            previous_bytes = {path: path.read_bytes() for path in primary_paths}

            self.assertEqual(first_summary.updated, ("Simon Betzold",))
            self.assertEqual(first_summary.chart_unavailable, ("Simon Betzold",))
            self.assertFalse(chart_path.exists())

            def failing_fetcher(scholar_id, api_key):
                calls.append((scholar_id, api_key))
                raise update_scholar.ScholarUpdateError("temporary upstream error")

            output = io.StringIO()
            second_summary = update_scholar.run_updates(
                config,
                self.API_KEY,
                root,
                fetcher=failing_fetcher,
                today=date(2026, 9, 2),
                out=output,
            )

            self.assertEqual(
                calls,
                [
                    ("YXmK2hcAAAAJ", self.API_KEY),
                    ("YXmK2hcAAAAJ", self.API_KEY),
                ],
            )
            self.assertEqual(second_summary.updated, ())
            self.assertEqual(second_summary.retained, ("Simon Betzold",))
            self.assertEqual(second_summary.partial, ())
            self.assertEqual(second_summary.unavailable, ())
            self.assertEqual(second_summary.chart_retained, ())
            self.assertEqual(second_summary.chart_unavailable, ())
            for path, content in previous_bytes.items():
                self.assertEqual(path.read_bytes(), content, path)
            self.assertFalse(chart_path.exists())
            self.assertIn("Retained previous primary metrics:", output.getvalue())

    def test_atomic_bundle_rolls_back_every_artifact_after_second_replace_fails(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            destinations = self.expected_paths(
                root,
                "simon-betzold",
                legacy=True,
            )
            previous_content = {}
            outputs = {}
            for index, destination in enumerate(destinations):
                destination.parent.mkdir(parents=True, exist_ok=True)
                old_bytes = f"old artifact {index}\n".encode("utf-8")
                destination.write_bytes(old_bytes)
                previous_content[destination] = old_bytes
                outputs[destination] = f"new artifact {index}\n"

            real_replace = update_scholar.os.replace
            replace_calls = 0

            def fail_once_on_second_replace(source, destination):
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 2:
                    raise OSError("injected second os.replace failure")
                return real_replace(source, destination)

            with patch.object(
                update_scholar.os,
                "replace",
                side_effect=fail_once_on_second_replace,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "injected second os.replace failure",
                ):
                    update_scholar._atomic_write_bundle(outputs)

            self.assertGreaterEqual(replace_calls, 3)
            for destination, old_bytes in previous_content.items():
                self.assertEqual(destination.read_bytes(), old_bytes, destination)

            remaining_files = {
                path for path in root.rglob("*") if path.is_file()
            }
            self.assertEqual(remaining_files, set(destinations))
            self.assertFalse(
                [
                    path
                    for path in remaining_files
                    if path.suffix in {".tmp", ".backup"}
                ]
            )

    def test_legacy_outputs_keep_narrow_json_and_svg_alias_when_reordered(self):
        ada = scholar_entry(
            slug="ada-example",
            name="Ada Example",
            scholar_id="AdaExampleAAAAJ",
        )
        simon = scholar_entry()

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = load_test_config(root, [ada, simon])
            calls = []

            def fetcher(scholar_id, api_key):
                calls.append((scholar_id, api_key))
                api_name = (
                    "Ada API Profile"
                    if scholar_id == "AdaExampleAAAAJ"
                    else "Simon Betzold"
                )
                return fixture_for(scholar_id, api_name)

            summary = update_scholar.run_updates(
                config,
                self.API_KEY,
                root,
                fetcher=fetcher,
                today=date(2026, 9, 1),
                out=io.StringIO(),
            )

            self.assertEqual(
                calls,
                [
                    ("AdaExampleAAAAJ", self.API_KEY),
                    ("YXmK2hcAAAAJ", self.API_KEY),
                ],
            )
            self.assertEqual(summary.updated, ("Ada Example", "Simon Betzold"))
            self.assertEqual(summary.retained, ())
            self.assertEqual(summary.partial, ())
            self.assertEqual(summary.unavailable, ())
            self.assertEqual(summary.chart_retained, ())
            self.assertEqual(summary.chart_unavailable, ())

            canonical_bytes = (
                root / "metrics" / "simon-betzold.json"
            ).read_bytes()
            legacy_bytes = (root / "metrics.json").read_bytes()
            canonical = json.loads(canonical_bytes)
            legacy = json.loads(legacy_bytes)
            self.assertNotEqual(legacy_bytes, canonical_bytes)
            self.assertEqual(
                legacy_bytes.decode("utf-8"),
                """{
  "citations": 1521,
  "hindex": 19,
  "i10index": 24,
  "updated": "2026-09-01"
}""",
            )
            self.assertEqual(
                list(legacy),
                ["citations", "hindex", "i10index", "updated"],
            )
            self.assertEqual(
                legacy,
                {key: canonical[key] for key in legacy},
            )
            self.assertIn("name", canonical)
            self.assertIn("scholar_id", canonical)
            self.assertIn("citations_by_year", canonical)
            self.assertIn("author", canonical)
            self.assertIn("articles", canonical)
            self.assertEqual(
                (root / "scholar-metrics.svg").read_bytes(),
                (root / "svg" / "simon-betzold.svg").read_bytes(),
            )
            self.assertNotEqual(
                (root / "metrics.json").read_bytes(),
                (root / "metrics" / "ada-example.json").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
