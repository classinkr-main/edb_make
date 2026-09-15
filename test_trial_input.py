import unittest

from problem_parser import PdfInfo
from trial_input import (
    A3_AREA_PT,
    InputLimits,
    REJECTIONS,
    TrialRejected,
    check_pdf_info,
    check_upload_head,
    sniff_kind,
)

LIMITS = InputLimits(max_bytes=4_000_000, max_pages=3, max_source_pages=100, max_page_area_pt=2 * A3_AREA_PT)


def _info(**overrides):
    values = {"page_count": 3, "scanned_pages": 3, "pages_without_text": 0, "max_page_area_pt": 595.0 * 842.0}
    values.update(overrides)
    return PdfInfo(**values)


class TestSniffKind(unittest.TestCase):
    def test_pdf_header_at_start(self):
        self.assertEqual("pdf", sniff_kind(b"%PDF-1.7\n..."))

    def test_pdf_header_after_leading_junk_within_1024_bytes(self):
        self.assertEqual("pdf", sniff_kind(b"\x00" * 500 + b"%PDF-1.4"))

    def test_pdf_header_beyond_1024_bytes_is_unknown(self):
        self.assertEqual("unknown", sniff_kind(b"\x00" * 1100 + b"%PDF-1.4"))

    def test_png_and_jpeg(self):
        self.assertEqual("png", sniff_kind(b"\x89PNG\r\n\x1a\n rest"))
        self.assertEqual("jpeg", sniff_kind(b"\xff\xd8\xff\xe0 rest"))

    def test_other_bytes(self):
        self.assertEqual("unknown", sniff_kind(b"PK\x03\x04 zip"))
        self.assertEqual("unknown", sniff_kind(b""))


class TestCheckUploadHead(unittest.TestCase):
    def test_images_are_redirected_to_scan_popup(self):
        with self.assertRaises(TrialRejected) as caught:
            check_upload_head(b"\x89PNG\r\n\x1a\n")
        self.assertEqual("image_not_supported", caught.exception.rejection.code)
        self.assertEqual(415, caught.exception.rejection.status)
        self.assertEqual("scan", caught.exception.rejection.feature)

    def test_unknown_is_bad_type_without_popup(self):
        with self.assertRaises(TrialRejected) as caught:
            check_upload_head(b"hello")
        self.assertEqual("bad_type", caught.exception.rejection.code)
        self.assertIsNone(caught.exception.rejection.feature)

    def test_pdf_passes(self):
        check_upload_head(b"%PDF-1.7")


class TestCheckPdfInfo(unittest.TestCase):
    def assertRejected(self, info, code):
        with self.assertRaises(TrialRejected) as caught:
            check_pdf_info(info, LIMITS)
        self.assertEqual(code, caught.exception.rejection.code)
        return caught.exception.rejection

    def test_normal_exam_passes(self):
        check_pdf_info(_info(page_count=16), LIMITS)

    def test_source_over_hundred_pages(self):
        rejection = self.assertRejected(_info(page_count=101), "too_many_pages")
        self.assertEqual(422, rejection.status)
        self.assertEqual("limit_pages", rejection.feature)

    def test_exactly_hundred_pages_passes(self):
        check_pdf_info(_info(page_count=100), LIMITS)

    def test_empty_document(self):
        self.assertRejected(_info(page_count=0, scanned_pages=0), "no_text_layer")

    def test_oversized_leading_page(self):
        self.assertRejected(_info(max_page_area_pt=2 * A3_AREA_PT + 1), "page_too_large")

    def test_textless_leading_page(self):
        rejection = self.assertRejected(_info(pages_without_text=1), "no_text_layer")
        self.assertEqual("scan", rejection.feature)

    def test_page_size_checked_before_text_layer(self):
        self.assertRejected(_info(pages_without_text=1, max_page_area_pt=10 * A3_AREA_PT), "page_too_large")


class TestRejectionCatalog(unittest.TestCase):
    def test_codes_are_unique_and_statuses_are_client_or_server_errors(self):
        codes = [rejection.code for rejection in REJECTIONS.values()]
        self.assertEqual(len(codes), len(set(codes)))
        for rejection in REJECTIONS.values():
            self.assertIn(rejection.status, {400, 413, 415, 422, 429, 500, 503})
            self.assertTrue(rejection.message)

    def test_payload_shape(self):
        payload = REJECTIONS["daily_limit"].payload(remaining_today=0)
        self.assertEqual(
            {"error": {"code": "daily_limit", "message": REJECTIONS["daily_limit"].message, "feature": "limit_daily"}, "remaining_today": 0},
            payload,
        )


if __name__ == "__main__":
    unittest.main()
