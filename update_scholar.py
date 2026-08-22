import json
from datetime import datetime, timezone

from scholarly import scholarly


SCHOLAR_ID = "YXmK2hcAAAAJ"


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

    with open("metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    print("metrics.json updated successfully.")


if __name__ == "__main__":
    main()
