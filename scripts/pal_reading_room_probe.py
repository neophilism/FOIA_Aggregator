"""Inspect public AINS/PAL FOIA reading-room form structure.

This script is diagnostic only: it performs GET requests against known public
reading-room pages and prints form/control/script metadata without submitting
requests or downloading records.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


TARGETS = {
    "air_force": "https://efoia.cce.af.mil/app/ReadingRoom.aspx",
    "ntsb": "https://securefoia.ntsb.gov/app/ReadingRoom.aspx",
}


def summarize(url: str) -> None:
    session = requests.Session()
    response = session.get(
        url,
        timeout=45,
        headers={"User-Agent": "FOIAArchiveBot/0.1 diagnostic"},
    )
    print(f"URL {url}")
    print(f"STATUS {response.status_code}")
    print(f"FINAL {response.url}")
    print(f"CONTENT_TYPE {response.headers.get('Content-Type')}")
    print(f"LENGTH {len(response.content)}")
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.find("form")
    if form is None:
        print("FORM none")
        return

    print(
        "FORM",
        {
            "id": form.get("id"),
            "name": form.get("name"),
            "method": form.get("method"),
            "action": urljoin(response.url, form.get("action") or ""),
        },
    )

    for element in form.find_all(["input", "select", "button", "textarea"]):
        name = element.get("name")
        element_id = element.get("id")
        kind = element.name
        input_type = element.get("type")
        value = element.get("value")
        if value and len(value) > 120:
            value = f"<{len(value)} chars>"
        if not name and not element_id:
            continue

        label = ""
        if element_id:
            explicit = soup.find("label", attrs={"for": element_id})
            if explicit:
                label = " ".join(explicit.stripped_strings)
        if not label:
            parent = element.find_parent(["td", "li", "tr"])
            if parent:
                label = " ".join(parent.stripped_strings)
                label = re.sub(r"\s+", " ", label)[:180]

        print(
            "CONTROL",
            {
                "tag": kind,
                "type": input_type,
                "name": name,
                "id": element_id,
                "value": value,
                "label": label,
            },
        )

    for script in soup.find_all("script"):
        src = script.get("src")
        if src:
            print("SCRIPT_SRC", urljoin(response.url, src))
            continue
        body = script.get_text("\n", strip=True)
        if not body:
            continue
        lower = body.lower()
        fn_index = lower.find("function fnsearch")
        if fn_index >= 0:
            print("FNSEARCH_BLOCK", body[fn_index:fn_index + 7000])
        params_match = re.search(
            r"var\\s+searchParams\\s*=\\s*\\{.*?\\};",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if params_match:
            print("SEARCH_PARAMS_BLOCK", params_match.group(0)[:5000])
        ajax_index = lower.find('url: "searchdocs.aspx"')
        if ajax_index >= 0:
            start = max(0, ajax_index - 1500)
            print("AJAX_BLOCK", body[start:ajax_index + 2500])


def main() -> None:
    for name, url in TARGETS.items():
        print(f"===== {name} =====")
        try:
            summarize(url)
        except Exception as exc:
            print(f"ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
