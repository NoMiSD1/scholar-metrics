import json
import os
from datetime import datetime, timezone

import requests


SCHOLAR_ID = "YXmK2hcAAAAJ"


def create_svg(citations, hindex, i10index):
    citations_text = f"{citations:,}"

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg"
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

    with open("scholar-metrics.svg", "w", encoding="utf-8") as f:
        f.write(svg)


def main():
    print(f"Fetching Google Scholar profile via SerpApi: {SCHOLAR_ID}")

    api_key = os.environ.get("SERPAPI_KEY")

    if not api_key:
        raise RuntimeError("SERPAPI_KEY environment variable is not set.")

    response = requests.get(
        "https://serpapi.com/search.json",
        params={
            "engine": "google_scholar_author",
            "author_id": SCHOLAR_ID,
            "hl": "en",
            "api_key": api_key,
        },
        timeout=30,
    )

    response.raise_for_status()
    data = response.json()

    # Extract Google Scholar metrics from SerpApi response
    table = data.get("cited_by", {}).get("table", [])

    metrics_by_name = {}

    for entry in table:
        for name, values in entry.items():
            metrics_by_name[name] = values.get("all")

    citations = metrics_by_name.get("citations")
    hindex = metrics_by_name.get("h_index")
    i10index = metrics_by_name.get("i10_index")

    if citations is None or hindex is None or i10index is None:
        raise RuntimeError("Could not retrieve all Scholar metrics from SerpApi.")

    metrics = {
        "citations": citations,
        "hindex": hindex,
        "i10index": i10index,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    print(metrics)

    # JSON for GitHub Pages
    with open("metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    # SVG for embedding on external websites
    create_svg(citations, hindex, i10index)

    print("metrics.json and scholar-metrics.svg updated successfully.")


if __name__ == "__main__":
    main()
