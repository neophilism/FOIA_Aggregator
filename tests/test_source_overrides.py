import unittest

from foia_archive.source_overrides import (
    current_source_url,
    historical_component_note,
)


class SourceReplacementTests(unittest.TestCase):
    def test_stale_current_agency_sources_map_to_current_official_pages(self):
        replacements = {
            "http://www.abilityone.gov/laws,_regulations_and_policy/foia_reading_room.html":
                "https://www.abilityone.gov/laws%2C_regulations_and_policy/foia_reading_room.html",
            "http://www.fmshrc.gov/foia/e-reading-room":
                "https://www.fmshrc.gov/foia/e-reading-room",
            "https://www.imls.gov/foia-electronic-reading-room":
                "https://www.imls.gov/communities-impact/additional-resources/foia-reading-room",
            "https://www.ncd.gov/FOIA/FOIA-e-library":
                "https://www.ncd.gov/foia/",
            "http://www.nmb.gov/documents/press-contacts/reading-room-certificate.pdf":
                "https://nmb.gov/NMB_Application/index.php/foia/",
            "http://www.prc.gov/foia":
                "https://www.prc.gov/foia",
            "https://www.ibwc.gov/Organization/FOIA_RR.html":
                "https://www.ibwc.gov/foia/",
            "https://osc.gov/Pages/FOIA-Resources.aspx":
                "https://www.osc.gov/about/foia/",
            "https://www.restorethegulf.gov/resources/council-documents-foia-library":
                "https://www.restorethegulf.gov/reports/",
            "https://www.whitehouse.gov/ipec/legal/":
                "https://www.whitehouse.gov/ipec",
            "https://www.ntsb.gov/about/foia/Pages/default.aspx":
                "https://securefoia.ntsb.gov/app/ReadingRoom.aspx",
        }

        for old_url, new_url in replacements.items():
            with self.subTest(old_url=old_url):
                self.assertEqual(current_source_url(old_url), new_url)

    def test_unknown_source_url_is_unchanged(self):
        url = "https://example.gov/foia/reading-room/"
        self.assertEqual(current_source_url(url), url)


class HistoricalAgencyTests(unittest.TestCase):
    def test_recently_terminated_entities_are_historical(self):
        component_ids = {
            "4de0da30-b524-4167-a32d-ccb7442fe13e",
            "4656129a-68f0-4843-bf62-5f4b6c66525b",
            "48ce1125-6beb-4e16-9ccd-aee48cb41207",
            "3c760e1c-6c1d-4d0b-a227-0a0e14752674",
        }

        for component_id in component_ids:
            with self.subTest(component_id=component_id):
                self.assertIsNotNone(
                    historical_component_note(component_id)
                )

    def test_current_component_is_not_marked_historical(self):
        self.assertIsNone(
            historical_component_note(
                "ff29c5f4-ca5b-4eb1-9fea-d59216110f95"
            )
        )


if __name__ == "__main__":
    unittest.main()
