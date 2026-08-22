import json
import html
from datetime import datetime, timezone

from scholarly import scholarly


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
    print(f"Fetching Google Scholar profile: {SCHOLAR_ID}")

    author = scholarly.search_author_id(SCHOLAR_ID)
    author = scholarly.fill(author, sections=["basics", "indices"])

    citations = author.get("citedby")
    hindex = author.get("hindex")
    i10index = author.get("i10index")

    if citations is None or hindex is None or i10index is None:
        raise RuntimeError("Could not retrieve all Scholar metrics.")

    metrics = {
        "citations": citations,
        "hindex": hindex,
        "i10index": i10index,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
