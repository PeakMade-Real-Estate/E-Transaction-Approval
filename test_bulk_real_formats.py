"""
Item 6 follow-up: how the bulk listing reader treats the structures of the real Treasury documents
(protected workbook, PDF printout, headerless ACH Pull CSV). Fixtures are synthetic: every banking value is fake.
"""
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
import unittest

import bulk_listing
import test_bulk_recipients as base

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def protected_workbook():
    return OLE2_MAGIC + b"\x00" * 600 + "EncryptedPackage".encode("utf-16-le") + b"\x00" * 600


def legacy_xls_workbook():
    return OLE2_MAGIC + b"\x00" * 600 + "Workbook".encode("utf-16-le") + b"\x00" * 600


class ProtectedAndLegacyWorkbookTests(unittest.TestCase):
    def _errors(self, content, name="listing.xlsx"):
        result = bulk_listing.validate_bulk_listing(content, name, originating_bank_account_key=None, matcher=base.matcher())
        self.assertFalse(result.ok)
        self.assertFalse(result.parse_ok)
        return result.errors

    def test_encrypted_or_sensitivity_labelled_workbook_gets_an_actionable_message(self):
        errors = self._errors(protected_workbook())
        self.assertEqual(len(errors), 1)
        self.assertIn("encrypted", errors[0])
        self.assertIn("unprotected copy", errors[0])

    def test_legacy_xls_saved_as_xlsx_gets_an_actionable_message(self):
        errors = self._errors(legacy_xls_workbook())
        self.assertIn("older Excel (.xls)", errors[0])
        self.assertIn("Save As", errors[0])

    def test_matching_is_never_attempted_for_an_unreadable_workbook(self):
        def boom(_pairs):
            raise AssertionError("the matcher must not run")
        result = bulk_listing.validate_bulk_listing(protected_workbook(), "listing.xlsx",
                                                    originating_bank_account_key=None, matcher=boom)
        self.assertFalse(result.ok)

    def test_a_genuine_xlsx_is_unaffected(self):
        result = bulk_listing.validate_bulk_listing(base.good_xlsx(), "listing.xlsx",
                                                    originating_bank_account_key=None, matcher=base.matcher())
        self.assertTrue(result.ok)
        self.assertEqual(result.matched_count, 3)


class UnsupportedFormatTests(unittest.TestCase):
    def test_a_pdf_listing_is_not_accepted(self):
        with self.assertRaises(bulk_listing.ListingError):
            bulk_listing.parse_listing(b"%PDF-1.7\n" + b"0" * 100, "ACH Request.pdf")

    def test_a_pdf_renamed_xlsx_is_not_readable(self):
        with self.assertRaises(bulk_listing.ListingError):
            bulk_listing.parse_listing(b"%PDF-1.7\n" + b"0" * 100, "ACH Request.xlsx")

    def test_a_headerless_csv_in_the_ach_pull_shape_is_not_accepted(self):
        csv_bytes = f"Property One,{base.RTN},{base.ACCT[1]},1,100.00\r\n".encode()
        with self.assertRaises(bulk_listing.ListingError):
            bulk_listing.parse_listing(csv_bytes, "pull.csv")


class HeaderRecognitionTests(unittest.TestCase):
    """The reader recognises only the documented headers; it never guesses which column is which."""

    def test_pdf_style_listing_headers_are_not_recognised(self):
        content = base.make_xlsx(base.GOOD_ROWS, headers=("Account", "Routing"))
        with self.assertRaises(bulk_listing.ListingError) as ctx:
            bulk_listing.parse_listing(content, "listing.xlsx")
        self.assertIn("Account Number", str(ctx.exception))

    def test_a_totals_row_without_banking_values_blocks_the_listing(self):
        content = base.make_xlsx(base.GOOD_ROWS + [("Total", None)])
        # A labelled totals row is reported, never skipped silently.
        _, problems, _ = bulk_listing.parse_listing(content, "listing.xlsx")
        self.assertTrue(any(p.startswith("Row 5:") for p in problems))

    def test_long_numeric_identifiers_are_rejected_not_repaired(self):
        content = base.make_xlsx([(base.ACCT[1], base.RTN)], numeric={(0, 0)})
        rows, problems, _ = bulk_listing.parse_listing(content, "listing.xlsx")
        self.assertEqual(rows, [])
        self.assertTrue(any("not text" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
