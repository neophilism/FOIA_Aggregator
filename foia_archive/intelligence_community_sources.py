"""Explicit public-source coverage for the 18 U.S. Intelligence Community elements.

FOIA.gov models some IC elements directly and collapses others into a parent
department/service. This registry makes the coverage relationship explicit and
also provides supplemental crawl roots when the parent metadata does not expose
an element-specific source.
"""
from __future__ import annotations

from typing import List, TypedDict

from .source_overrides import current_source_url


class ICSource(TypedDict):
    url: str
    mode: str
    note: str


class ICElement(TypedDict):
    slug: str
    name: str
    parent_agency: str
    sources: List[ICSource]


INTELLIGENCE_COMMUNITY_ELEMENTS: List[ICElement] = [
    {
        "slug": "odni",
        "name": "Office of the Director of National Intelligence",
        "parent_agency": "Office of the Director of National Intelligence",
        "sources": [
            {
                "url": "https://www.odni.gov/accountability/",
                "mode": "dedicated",
                "note": "ODNI accountability/FOIA publication source.",
            }
        ],
    },
    {
        "slug": "cia",
        "name": "Central Intelligence Agency",
        "parent_agency": "Central Intelligence Agency",
        "sources": [
            {
                "url": "https://www.cia.gov/readingroom/",
                "mode": "dedicated",
                "note": "CIA FOIA Electronic Reading Room.",
            }
        ],
    },
    {
        "slug": "dia",
        "name": "Defense Intelligence Agency",
        "parent_agency": "Department of Defense",
        "sources": [
            {
                "url": "https://www.dia.mil/FOIA/FOIA-Electronic-Reading-Room/",
                "mode": "dedicated",
                "note": "DIA FOIA Electronic Reading Room.",
            }
        ],
    },
    {
        "slug": "fbi-intelligence-branch",
        "name": "Federal Bureau of Investigation, Intelligence Branch",
        "parent_agency": "Department of Justice",
        "sources": [
            {
                "url": "https://vault.fbi.gov/",
                "mode": "shared",
                "note": "FBI Vault is the FBI-wide released-record repository.",
            }
        ],
    },
    {
        "slug": "nga",
        "name": "National Geospatial-Intelligence Agency",
        "parent_agency": "Department of Defense",
        "sources": [
            {
                "url": "https://www.nga.mil/resources/Freedom_of_Information_Act_(FOIA).html",
                "mode": "dedicated",
                "note": "NGA FOIA publication page.",
            }
        ],
    },
    {
        "slug": "nro",
        "name": "National Reconnaissance Office",
        "parent_agency": "Department of Defense",
        "sources": [
            {
                "url": "https://www.nro.gov/foia-home/foia-resources-reading-room/",
                "mode": "dedicated",
                "note": "NRO FOIA Resources Reading Room.",
            }
        ],
    },
    {
        "slug": "nsa",
        "name": "National Security Agency",
        "parent_agency": "Department of Defense",
        "sources": [
            {
                "url": "https://www.nsa.gov/Helpful-Links/NSA-FOIA/Reading-Room/",
                "mode": "dedicated",
                "note": "NSA FOIA Reading Room.",
            }
        ],
    },
    {
        "slug": "dea-onsi",
        "name": "Drug Enforcement Administration, Office of National Security Intelligence",
        "parent_agency": "Department of Justice",
        "sources": [
            {
                "url": "https://www.dea.gov/foia/foia-library",
                "mode": "shared",
                "note": "DEA-wide FOIA Library covers ONSI releases.",
            }
        ],
    },
    {
        "slug": "doe-intelligence-counterintelligence",
        "name": "Department of Energy, Office of Intelligence and Counterintelligence",
        "parent_agency": "Department of Energy",
        "sources": [
            {
                "url": "https://www.energy.gov/gc/foia-reading-room",
                "mode": "shared",
                "note": "DOE-wide FOIA Reading Room covers records processed for DOE-IN.",
            }
        ],
    },
    {
        "slug": "dhs-intelligence-analysis",
        "name": "Department of Homeland Security, Office of Intelligence and Analysis",
        "parent_agency": "Department of Homeland Security",
        "sources": [
            {
                "url": "https://www.dhs.gov/intelligence-and-analysis-foia-library",
                "mode": "dedicated",
                "note": "DHS Intelligence & Analysis FOIA Library.",
            },
            {
                "url": "https://www.dhs.gov/foia-library",
                "mode": "shared",
                "note": "DHS-wide FOIA Library also covers I&A.",
            },
        ],
    },
    {
        "slug": "state-inr",
        "name": "Department of State, Bureau of Intelligence and Research",
        "parent_agency": "U.S. Department of State",
        "sources": [
            {
                "url": "https://foia.state.gov/FOIALIBRARY/SearchResults.aspx",
                "mode": "shared",
                "note": "State's release-to-one/release-to-all Virtual Reading Room covers INR records.",
            }
        ],
    },
    {
        "slug": "treasury-oia",
        "name": "Department of the Treasury, Office of Intelligence and Analysis",
        "parent_agency": "Department of the Treasury",
        "sources": [
            {
                "url": "https://home.treasury.gov/footer/freedom-of-information-act/electronic-reading-room",
                "mode": "shared",
                "note": "Treasury Departmental Offices Electronic Reading Room covers OIA releases.",
            }
        ],
    },
    {
        "slug": "army-intelligence",
        "name": "U.S. Army Intelligence and Security Enterprise",
        "parent_agency": "Department of Defense",
        "sources": [
            {
                "url": "https://www.usainscom.army.mil/FOIA/",
                "mode": "dedicated",
                "note": "INSCOM FOIA office for Army intelligence and security records.",
            },
            {
                "url": "https://foia.army.mil/",
                "mode": "shared",
                "note": "Army FOIA Library contains released INSCOM-originated records.",
            },
        ],
    },
    {
        "slug": "air-force-intelligence",
        "name": "U.S. Air Force Intelligence, Surveillance and Reconnaissance",
        "parent_agency": "Department of Defense",
        "sources": [
            {
                "url": "https://efoia.cce.af.mil/app/ReadingRoom.aspx",
                "mode": "shared",
                "note": "Department of the Air Force Electronic FOIA Library.",
            }
        ],
    },
    {
        "slug": "coast-guard-intelligence",
        "name": "U.S. Coast Guard Intelligence",
        "parent_agency": "Department of Homeland Security",
        "sources": [
            {
                "url": "https://www.dcms.uscg.mil/Our-Organization/Assistant-Commandant-for-C4IT-CG-6/The-Office-of-Information-Management-CG-61/FOIA-Library/",
                "mode": "shared",
                "note": "Coast Guard FOIA Library covers CG-2/intelligence records.",
            }
        ],
    },
    {
        "slug": "marine-corps-intelligence",
        "name": "U.S. Marine Corps, Marine Corps Intelligence Activity",
        "parent_agency": "Department of Defense",
        "sources": [
            {
                "url": "https://www.hqmc.marines.mil/Agencies/USMC-FOIA/USMC-FOIA-Reading-Room/",
                "mode": "shared",
                "note": "HQMC FOIA Reading Room covers MCIA releases.",
            }
        ],
    },
    {
        "slug": "naval-intelligence",
        "name": "U.S. Navy, Naval Intelligence",
        "parent_agency": "Department of Defense",
        "sources": [
            {
                "url": "https://www.oni.navy.mil/Contact-Us/Freedom-of-Information-Act/Reading-Room/",
                "mode": "dedicated",
                "note": "Office of Naval Intelligence FOIA Reading Room.",
            }
        ],
    },
    {
        "slug": "space-force-intelligence",
        "name": "U.S. Space Force Intelligence",
        "parent_agency": "Department of Defense",
        "sources": [
            {
                "url": "https://efoia.cce.af.mil/app/ReadingRoom.aspx",
                "mode": "shared",
                "note": "Space Force FOIA releases are handled through the Department of the Air Force Electronic FOIA Library.",
            }
        ],
    },
]


def normalized_ic_elements() -> List[ICElement]:
    normalized: List[ICElement] = []
    for element in INTELLIGENCE_COMMUNITY_ELEMENTS:
        normalized.append(
            {
                **element,
                "sources": [
                    {
                        **source,
                        "url": current_source_url(source["url"]),
                    }
                    for source in element["sources"]
                ],
            }
        )
    return normalized


def unique_ic_source_urls() -> List[str]:
    return sorted(
        {
            source["url"]
            for element in normalized_ic_elements()
            for source in element["sources"]
        }
    )
