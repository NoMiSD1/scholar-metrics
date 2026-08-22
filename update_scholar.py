import json
import html
from datetime import datetime, timezone

from scholarly import scholarly


SCHOLAR_ID = "YXmK2hcAAAAJ"


def create_svg(citations, hindex, i10index):
    text = (
        f"{citations:,} citations"
        f"  ·  h-index {hindex}"
        f"  ·  i10-index {i10index}"
    )

    text = html.escape(text)

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg"
     width="300"
     height="28"
     viewBox="0 0 300 28">

  <rect width="100%" height="100%" fill="none"/>

  <text
    x="0"
    y="18"
    font-family="Arial, Helvetica, sans-serif"
    font-size="14"
    fill="#444444">
    {text}
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
