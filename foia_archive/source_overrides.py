"""Curated publication-source gaps not represented in FOIA.gov metadata.

Entries here are intentionally narrow and keyed to stable FOIA.gov agency-component
entity IDs. They are used only when live component metadata yields no automatic
publication source.
"""
from __future__ import annotations

from typing import Dict, List, TypedDict


class CuratedSource(TypedDict):
    url: str
    source_type: str
    note: str


CURATED_COMPONENT_SOURCES: Dict[str, List[CuratedSource]] = {
    # Civil Rights Cold Case Records Review Board: case records approved for release.
    "87c8ee1a-1ed2-411a-b995-8535fabb988b": [
        {
            "url": "https://www.coldcaserecords.gov/cases/",
            "source_type": "curated_public_records",
            "note": "Board-authorized cold case releases with links to NARA case files.",
        }
    ],
    # Commission of Fine Arts.
    "596b681a-a06c-4a5f-818e-de7344eb1676": [
        {
            "url": "https://www.cfa.gov/foia",
            "source_type": "curated_foia",
            "note": "Current CFA FOIA page with FOIA reports and public-record guidance.",
        },
        {
            "url": "https://www.cfa.gov/records-research",
            "source_type": "curated_public_records",
            "note": "CFA online records and research collections.",
        },
    ],
    # Denali Commission.
    "2829a7fc-a7cd-48ef-8d3f-29e5ad0df44f": [
        {
            "url": "https://denali.gov/freedom-of-information-act/",
            "source_type": "curated_foia",
            "note": "Current FOIA page, annual FOIA report, and public project database.",
        }
    ],
    # Federal Mediation and Conciliation Service.
    "ff13ca4b-ec79-4d51-8105-550f5de13f57": [
        {
            "url": "https://www.fmcs.gov/foia/",
            "source_type": "curated_foia",
            "note": "FMCS electronic FOIA reading-room landing page.",
        },
        {
            "url": "https://www.fmcs.gov/resources/documents-and-data/",
            "source_type": "curated_public_records",
            "note": "FMCS reports, FOIA officer reports, logs, and public datasets.",
        },
    ],
    # James Madison Memorial Fellowship Foundation.
    "4ccc22c7-358d-4106-b6c1-fe8c26715175": [
        {
            "url": "https://www.jamesmadison.gov/required-statements/foia",
            "source_type": "curated_foia",
            "note": "Current FOIA page with annual and Chief FOIA Officer reports.",
        }
    ],
    # Marine Mammal Commission.
    "38718a0e-eee3-4465-818f-e5c4c8fbe36d": [
        {
            "url": "https://www.mmc.gov/letters-and-reports/",
            "source_type": "curated_public_records",
            "note": "Commission letters, agency responses, and public reports.",
        }
    ],
    # National Capital Planning Commission.
    "5430dab2-8dd1-4add-8285-c43dde4312a8": [
        {
            "url": "https://www.ncpc.gov/policies/foia/",
            "source_type": "curated_foia",
            "note": "Current NCPC FOIA reports, datasheets, and public-record guidance.",
        }
    ],
    # Presidio Trust.
    "af460f38-0565-4eef-80d0-ca0732ca8bfe": [
        {
            "url": "https://presidio.gov/about/presidio-trust/documents",
            "source_type": "curated_public_records",
            "note": "Presidio Trust public documents, including FOIA documents and reports.",
        }
    ],
    # Surface Transportation Board.
    "40554d9e-8701-46d6-a720-eba749b57484": [
        {
            "url": "https://www.stb.gov/foia/",
            "source_type": "curated_foia",
            "note": "Current STB FOIA page with Reading Room and FOIA reports/data.",
        }
    ],
    # U.S. Election Assistance Commission.
    "c1efb796-3bb7-4747-a8b7-415992834318": [
        {
            "url": "https://www.eac.gov/foia/foia-reading-room",
            "source_type": "curated_foia",
            "note": "Current EAC FOIA Reading Room with frequently requested records.",
        }
    ],
    # U.S. Interagency Council on Homelessness.
    "d7db4ef5-cae0-45d9-8d10-c127ef627fad": [
        {
            "url": "https://www.usich.gov/foia",
            "source_type": "curated_foia",
            "note": "USICH FOIA page and published FOIA reports.",
        }
    ],
    # U.S. Access Board.
    "ba4b51e1-4283-4b2d-9819-76d2a1e716d1": [
        {
            "url": "https://www.access-board.gov/about/policy/foia.html",
            "source_type": "curated_foia",
            "note": "Current Access Board FOIA page with annual FOIA reporting.",
        }
    ],
    # U.S. African Development Foundation.
    "d388d986-3e30-4746-9105-13c2be1011db": [
        {
            "url": "https://usadf.gov/oversight",
            "source_type": "curated_public_records",
            "note": "USADF Legal Notices and Reports / oversight repository.",
        }
    ],
    # United States Institute of Peace.
    "d3e4465c-bc1f-4391-b8c0-8cd2cde69c0c": [
        {
            "url": "https://www.usip.org/freedom-information-act-foia",
            "source_type": "curated_foia",
            "note": "USIP's current FOIA webpage, identified in its annual FOIA reports.",
        }
    ],
}


# FOIA.gov sometimes retains an obsolete URL after an agency has moved its
# reading room. Normalize only replacements verified against the agency's
# current official site; this avoids crawling both stale and current copies.
SOURCE_URL_REPLACEMENTS: Dict[str, str] = {
    "https://www.cia.gov/library/readingroom/": "https://www.cia.gov/readingroom/",
    "https://www.cia.gov/library/readingroom/what-electronic-reading-room": "https://www.cia.gov/readingroom/",
    "http://www.dia.mil/FOIA/FOIA-Electronic-Reading-Room/": "https://www.dia.mil/FOIA/FOIA-Electronic-Reading-Room/",
    "https://www.rmda.army.mil/readingroom/": "https://foia.army.mil/",
    "http://www.dcms.uscg.mil/Our-Organization/Assistant-Commandant-for-C4IT-CG-6/The-Office-of-Information-Management-CG-61/FOIA-Library/": "https://www.dcms.uscg.mil/Our-Organization/Assistant-Commandant-for-C4IT-CG-6/The-Office-of-Information-Management-CG-61/FOIA-Library/",
    "http://www.hqmc.marines.mil/Agencies/USMC-FOIA/USMC-FOIA-Reading-Room/": "https://www.hqmc.marines.mil/Agencies/USMC-FOIA/USMC-FOIA-Reading-Room/",
    "http://www.secnav.navy.mil/foia/readingroom/SitePages/Home.aspx": "https://www.secnav.navy.mil/foia/readingroom/SitePages/Home.aspx",
    "http://www.abilityone.gov/laws,_regulations_and_policy/foia_reading_room.html": "https://www.abilityone.gov/laws%2C_regulations_and_policy/foia_reading_room.html",
    "http://www.fmshrc.gov/foia/e-reading-room": "https://www.fmshrc.gov/foia/e-reading-room",
    "https://www.imls.gov/foia-electronic-reading-room": "https://www.imls.gov/communities-impact/additional-resources/foia-reading-room",
    "https://www.ncd.gov/FOIA/FOIA-e-library": "https://www.ncd.gov/foia/",
    "http://www.nmb.gov/documents/press-contacts/reading-room-certificate.pdf": "https://nmb.gov/NMB_Application/index.php/foia/",
    "http://www.prc.gov/foia": "https://www.prc.gov/foia",
    "https://www.ibwc.gov/Organization/FOIA_RR.html": "https://www.ibwc.gov/foia/",
    "https://osc.gov/Pages/FOIA-Resources.aspx": "https://www.osc.gov/about/foia/",
    "https://www.restorethegulf.gov/resources/council-documents-foia-library": "https://www.restorethegulf.gov/reports/",
    "https://www.whitehouse.gov/ipec/legal/": "https://www.whitehouse.gov/ipec",
    "https://www.ntsb.gov/about/foia/Pages/default.aspx": "https://securefoia.ntsb.gov/app/ReadingRoom.aspx",
}


def current_source_url(url: str) -> str:
    return SOURCE_URL_REPLACEMENTS.get(url, url)


# FOIA.gov still exposes these defunct agencies as agency components. They are
# retained in the census for historical transparency but are not current source gaps.
HISTORICAL_COMPONENTS: Dict[str, str] = {
    "5f76475b-9b0a-41c1-bfe0-4367c27af3df": (
        "Office of Federal Housing Enterprise Oversight was abolished and its "
        "functions transferred to the Federal Housing Finance Agency."
    ),
    "034ea4e5-220d-497d-9e69-1889f005bc81": (
        "Recovery Accountability and Transparency Board is a terminated agency."
    ),
    "b07c0cd6-dc80-4645-85fa-ae7e520fcb47": (
        "Special Inspector General for Iraq Reconstruction is a terminated office."
    ),
    "4de0da30-b524-4167-a32d-ccb7442fe13e": (
        "National Commission on Military, National, and Public Service was a temporary "
        "commission whose authorizing statute required termination within 36 months."
    ),
    "4656129a-68f0-4843-bf62-5f4b6c66525b": (
        "National Security Commission on Artificial Intelligence was a temporary "
        "commission whose statutory life ended in 2021."
    ),
    "48ce1125-6beb-4e16-9ccd-aee48cb41207": (
        "Overseas Private Investment Corporation was replaced by the U.S. International "
        "Development Finance Corporation in 2020."
    ),
    "3c760e1c-6c1d-4d0b-a227-0a0e14752674": (
        "Special Inspector General for Afghanistan Reconstruction closed on "
        "January 31, 2026."
    ),
}


def curated_sources_for_component(component_id: str | None) -> List[CuratedSource]:
    if not component_id:
        return []
    return list(CURATED_COMPONENT_SOURCES.get(component_id, ()))


def historical_component_note(component_id: str | None) -> str | None:
    if not component_id:
        return None
    return HISTORICAL_COMPONENTS.get(component_id)
