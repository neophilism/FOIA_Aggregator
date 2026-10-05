import unittest

from foia_archive.pal_adapter import (
    build_search_params,
    cabinet_ids_from_page,
    is_pal_reading_room,
    parse_search_page,
    search_url,
)


class PalAdapterParsingTests(unittest.TestCase):
    def test_recognizes_only_known_pal_reading_rooms(self):
        self.assertTrue(
            is_pal_reading_room(
                "https://efoia.cce.af.mil/app/ReadingRoom.aspx"
            )
        )
        self.assertTrue(
            is_pal_reading_room(
                "https://securefoia.ntsb.gov/app/ReadingRoom.aspx"
            )
        )
        self.assertFalse(
            is_pal_reading_room("https://example.gov/app/ReadingRoom.aspx")
        )
        self.assertFalse(
            is_pal_reading_room("https://securefoia.ntsb.gov/app/Other.aspx")
        )

    def test_discovers_cabinet_ids_from_lang_attributes(self):
        html = """
        <input id="chkHeader" name="DocTypes" type="checkbox">
        <input id="chk10" lang="10" onclick="javascript: ChildClick(this);">
        <input id="chk9" lang="9" onclick="javascript: ChildClick(this);">
        <input id="duplicate" lang="10" onclick="ChildClick(this)">
        <input id="other" lang="11">
        """
        self.assertEqual(cabinet_ids_from_page(html), ("10", "9"))

    def test_builds_exact_ajax_search_contract(self):
        params = build_search_params(["10", "9", "10"], page_index=3)
        self.assertEqual(
            params,
            {
                "doctypes": "10,9",
                "filename": "*",
                "sdate": "",
                "edate": "",
                "content": "",
                "sortBy": "",
                "sortOrder": "",
                "custom": '{"customFields":[]}',
                "pageIndex": "3",
            },
        )

    def test_search_url_uses_pal_app_directory(self):
        self.assertEqual(
            search_url(
                "https://securefoia.ntsb.gov/app/ReadingRoom.aspx"
            ),
            "https://securefoia.ntsb.gov/app/SearchDocs.aspx",
        )

    def test_parses_folders_dates_and_page_indexes(self):
        html = """
        <table>
          <tr>
            <td><a href="javascript:showDocs('55','F');"
              title="NTSB FY23 FOIA LOG Click to view the Folder">
              NTSB FY23 FOIA LOG
            </a></td>
            <td>10/03/2023</td>
            <td><a href="javascript:download('55','F');">download</a></td>
          </tr>
          <tr>
            <td><a href="javascript:showDocs('2315','P');">
              Making and Sealing Synthetic Sapphire Windows
            </a></td>
            <td>01/15/2020</td>
          </tr>
        </table>
        <select id="pageIndexOption">
          <option value="0">1</option>
          <option value="1">2</option>
          <option value="2">3</option>
          <option value="3">4</option>
        </select>
        """
        page = parse_search_page(html)

        self.assertEqual(page.total_pages, 4)
        self.assertEqual(len(page.folders), 2)
        self.assertEqual(page.folders[0].document_id, "55")
        self.assertEqual(page.folders[0].document_kind, "F")
        self.assertEqual(page.folders[0].title, "NTSB FY23 FOIA LOG")
        self.assertEqual(page.folders[0].published_date, "2023-10-03")
        self.assertEqual(page.folders[1].document_kind, "P")
        self.assertEqual(page.folders[1].published_date, "2020-01-15")


if __name__ == "__main__":
    unittest.main()
