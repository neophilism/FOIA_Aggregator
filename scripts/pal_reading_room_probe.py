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

        payload = {
            "tag": kind,
            "type": input_type,
            "name": name,
            "id": element_id,
            "value": value,
            "label": label,
        }
        if input_type == "checkbox":
            payload["attrs"] = dict(element.attrs)
            row = element.find_parent("tr")
            if row is not None:
                payload["row"] = re.sub(
                    r"\\s+",
                    " ",
                    str(row),
                )[:1200]
        print("CONTROL", payload)

    cabinet_ids = [
        element.get("lang")
        for element in soup.find_all("input")
        if element.get("lang") and "ChildClick" in (element.get("onclick") or "")
    ]
    if cabinet_ids:
        if "efoia.cce.af.mil" in response.url:
            selected_ids = ["5"] if "5" in cabinet_ids else cabinet_ids[:1]
        else:
            selected_ids = cabinet_ids

        search_url = urljoin(response.url, "SearchDocs.aspx")
        search_params = {
            "doctypes": ",".join(selected_ids),
            "filename": "*",
            "sdate": "",
            "edate": "",
            "content": "",
            "sortBy": "",
            "sortOrder": "",
            "custom": '{"customFields":[]}',
            "pageIndex": "0",
        }
        search_response = session.post(
            search_url,
            data=search_params,
            timeout=45,
            headers={
                "User-Agent": "FOIAArchiveBot/0.1 diagnostic",
                "Referer": response.url,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        print("SEARCH_URL", search_url)
        print("SEARCH_DOCTYPES", search_params["doctypes"])
        print("SEARCH_STATUS", search_response.status_code)
        print("SEARCH_LENGTH", len(search_response.content))
        print("SEARCH_CONTENT_TYPE", search_response.headers.get("Content-Type"))
        search_response.raise_for_status()

        result_soup = BeautifulSoup(search_response.text, "html.parser")
        for result_script in result_soup.find_all("script"):
            result_body = result_script.get_text("\n", strip=True)
            if not result_body:
                continue
            for function_name in (
                "showDocs",
                "download",
                "btnNextPrevClicked",
            ):
                match = re.search(
                    rf"function\\s+{function_name}\\s*\\([^)]*\\)\\s*\\{{",
                    result_body,
                    flags=re.IGNORECASE,
                )
                if match:
                    print(
                        f"RESULT_{function_name.upper()}_BLOCK",
                        result_body[match.start():match.start() + 4500],
                    )

        page_index = result_soup.find(id="pageIndexOption")
        if page_index is not None:
            print(
                "PAGE_INDEX_OPTIONS",
                [
                    {
                        "value": option.get("value"),
                        "text": " ".join(option.stripped_strings),
                        "selected": option.has_attr("selected"),
                    }
                    for option in page_index.find_all("option")
                ][:30],
            )
        result_script_sources = [
            urljoin(search_url, script.get("src"))
            for script in result_soup.find_all("script", src=True)
        ]
        if result_script_sources:
            print("RESULT_SCRIPT_SOURCES", result_script_sources)

        aspx_refs = sorted(
            set(
                match.group(1)
                for match in re.finditer(
                    r"""["']([^"']+\\.aspx[^"']*)["']""",
                    search_response.text,
                    flags=re.IGNORECASE,
                )
            )
        )
        if aspx_refs:
            print("RESULT_ASPX_REFS", aspx_refs[:80])

        show_links = []
        for link in result_soup.find_all("a", href=True):
            href = (link.get("href") or "").strip()
            match = re.match(
                r"""javascript:showDocs\\(['"](\\d+)['"],['"]([A-Za-z])['"]\\);?""",
                href,
                flags=re.IGNORECASE,
            )
            if match:
                show_links.append((match.group(1), match.group(2)))
        if show_links:
            first_id, first_kind = show_links[0]
            folder_url = urljoin(
                search_url,
                f"AddAttachment.aspx?docid={first_id}&ispaldoc={first_kind}",
            )
            folder_response = session.get(
                folder_url,
                timeout=45,
                headers={
                    "User-Agent": "FOIAArchiveBot/0.1 diagnostic",
                    "Referer": response.url,
                },
            )
            print("FOLDER_URL", folder_url)
            print("FOLDER_STATUS", folder_response.status_code)
            print("FOLDER_FINAL", folder_response.url)
            print("FOLDER_LENGTH", len(folder_response.content))
            print("FOLDER_CONTENT_TYPE", folder_response.headers.get("Content-Type"))
            if folder_response.status_code == 200:
                folder_soup = BeautifulSoup(folder_response.text, "html.parser")
                for folder_link in folder_soup.find_all("a", href=True)[:80]:
                    print(
                        "FOLDER_LINK",
                        {
                            "href": urljoin(folder_response.url, folder_link.get("href")),
                            "text": " ".join(folder_link.stripped_strings)[:300],
                            "title": folder_link.get("title"),
                            "onclick": (folder_link.get("onclick") or "")[:500],
                        },
                    )

        for link in result_soup.find_all("a", href=True)[:60]:
            print(
                "RESULT_LINK",
                {
                    "href": urljoin(search_url, link.get("href")),
                    "text": " ".join(link.stripped_strings)[:300],
                    "title": link.get("title"),
                    "onclick": (link.get("onclick") or "")[:500],
                },
            )
        for element in result_soup.find_all(["input", "button"]):
            onclick = element.get("onclick") or ""
            if any(token in onclick.lower() for token in ("download", "attach", "document", "view")):
                print(
                    "RESULT_ACTION",
                    {
                        "tag": element.name,
                        "id": element.get("id"),
                        "value": element.get("value"),
                        "onclick": onclick[:700],
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
        for function_name in (
            "HeaderClick",
            "ChildClick",
            "PrepareAddList",
            "showDocs",
            "download",
            "btnNextPrevClicked",
        ):
            match = re.search(
                rf"function\\s+{function_name}\\s*\\([^)]*\\)\\s*\\{{",
                body,
                flags=re.IGNORECASE,
            )
            if match:
                print(
                    f"{function_name.upper()}_BLOCK",
                    body[match.start():match.start() + 5000],
                )
        for hidden_match in re.finditer("hidDocTypes", body, flags=re.IGNORECASE):
            start = max(0, hidden_match.start() - 800)
            end = min(len(body), hidden_match.end() + 1200)
            snippet = body[start:end]
            if "fnSearch" not in snippet:
                print("HIDDOCTYPES_CONTEXT", snippet)

        lower = body.lower()
        fn_index = lower.find("function fnsearch")
        if fn_index >= 0:
            fn_block = body[fn_index:fn_index + 14000]
            print("FNSEARCH_BLOCK", fn_block)
            for line in fn_block.splitlines():
                if any(
                    token in line
                    for token in (
                        "doctypes",
                        "filename",
                        "sdate",
                        "edate",
                        "content =",
                        "hidDocTypes",
                    )
                ):
                    print("SEARCH_ASSIGNMENT", line.strip())
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
