"""Security regression tests for media_library uploads (V3 + V8)."""

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.media_library.services import create_asset
from apps.media_library.validators import (
    ALLOWED_EXTENSIONS,
    ALLOWED_MIME_TYPES,
    sniff_mime,
    validate_file,
)
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 14
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 28


class SvgDisallowedTest(SimpleTestCase):
    """V3: SVG must be stripped from the upload allow-list entirely."""

    def test_svg_not_in_allowed_mimes(self):
        self.assertNotIn("image/svg+xml", ALLOWED_MIME_TYPES["image"])

    def test_svg_not_in_allowed_extensions(self):
        self.assertNotIn("svg", ALLOWED_EXTENSIONS["image"])


class SniffMimeTest(SimpleTestCase):
    """V8: magic-byte sniffer must catch spoofed content-types."""

    def test_recognises_jpeg(self):
        f = SimpleUploadedFile("a.jpg", JPEG, content_type="application/octet-stream")
        self.assertEqual(sniff_mime(f), "image/jpeg")

    def test_recognises_png(self):
        f = SimpleUploadedFile("a.png", PNG, content_type="text/html")
        self.assertEqual(sniff_mime(f), "image/png")

    def test_rejects_unknown_signature(self):
        f = SimpleUploadedFile("evil.jpg", b"<svg><script>alert(1)</script></svg>", content_type="image/jpeg")
        self.assertIsNone(sniff_mime(f))

    def test_validate_file_rejects_html_labelled_as_jpeg(self):
        f = SimpleUploadedFile(
            "fake.jpg",
            b"<html><body>not a jpeg</body></html>",
            content_type="image/jpeg",
        )
        file_type, errors = validate_file(f)
        self.assertIsNone(file_type)
        self.assertTrue(errors)

    def test_validate_file_accepts_real_jpeg(self):
        f = SimpleUploadedFile("real.jpg", JPEG, content_type="image/jpeg")
        f.size = len(JPEG)
        file_type, errors = validate_file(f)
        self.assertEqual(file_type, "image")
        self.assertEqual(errors, [])


def _ftyp(major, *compatible):
    """An ISO-BMFF file head: the ftyp box with its major and compatible brands."""
    brands = major + b"\x00\x00\x00\x00" + b"".join(compatible)
    return (8 + len(brands)).to_bytes(4, "big") + b"ftyp" + brands + b"\x00" * 16


class SniffIsoBmffBrandTest(SimpleTestCase):
    """iPhone HEIC/HEIF and AVIF photos share MP4's ftyp box; the brand tells them apart."""

    def _sniff(self, head):
        return sniff_mime(SimpleUploadedFile("f", head, content_type="application/octet-stream"))

    def test_heic_and_heif_brands_are_images_not_video(self):
        for brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            with self.subTest(brand=brand):
                self.assertEqual(self._sniff(_ftyp(brand, b"mif1", b"heic")), "image/heic")

    def test_avif_brands_are_images_not_video(self):
        for brand in (b"avif", b"avis"):
            with self.subTest(brand=brand):
                self.assertEqual(self._sniff(_ftyp(brand, b"mif1", b"miaf")), "image/avif")

    def test_video_brands_stay_video(self):
        for brand in (b"isom", b"iso2", b"mp41", b"mp42", b"M4V ", b"avc1", b"dash"):
            with self.subTest(brand=brand):
                self.assertEqual(self._sniff(_ftyp(brand, b"isom")), "video/mp4")
        self.assertEqual(self._sniff(_ftyp(b"qt  ", b"qt  ")), "video/quicktime")

    def test_validate_file_rejects_heic_with_export_advice(self):
        f = SimpleUploadedFile("IMG_0001.HEIC", _ftyp(b"heic", b"mif1", b"heic"), content_type="image/heic")
        file_type, errors = validate_file(f)
        self.assertIsNone(file_type)
        self.assertEqual(len(errors), 1)
        self.assertIn("HEIC", errors[0])
        self.assertIn("JPG or PNG", errors[0])

    def test_validate_file_rejects_avif_with_export_advice(self):
        f = SimpleUploadedFile("photo.avif", _ftyp(b"avif", b"mif1"), content_type="image/avif")
        file_type, errors = validate_file(f)
        self.assertIsNone(file_type)
        self.assertIn("AVIF", errors[0])
        self.assertIn("JPG or PNG", errors[0])


class DocumentSizeLimitTest(SimpleTestCase):
    """PDFs get their own cap (LinkedIn allows 100 MB), not the 20 MB image cap."""

    def test_document_cap_comes_from_its_own_setting(self):
        from django.conf import settings

        from apps.media_library.validators import MAX_FILE_SIZES

        self.assertEqual(settings.MEDIA_LIBRARY_MAX_DOCUMENT_SIZE, 100 * 1024 * 1024)
        self.assertEqual(MAX_FILE_SIZES["document"], settings.MEDIA_LIBRARY_MAX_DOCUMENT_SIZE)

    def test_a_50_mb_pdf_is_accepted(self):
        f = SimpleUploadedFile("deck.pdf", b"%PDF-1.7\n%%EOF\n", content_type="application/pdf")
        f.size = 50 * 1024 * 1024
        file_type, errors = validate_file(f)
        self.assertEqual(file_type, "document")
        self.assertEqual(errors, [])

    def test_a_pdf_over_100_mb_is_rejected(self):
        f = SimpleUploadedFile("deck.pdf", b"%PDF-1.7\n%%EOF\n", content_type="application/pdf")
        f.size = 100 * 1024 * 1024 + 1
        _file_type, errors = validate_file(f)
        self.assertEqual(errors, ["File too large. Maximum size for document files is 100MB."])


class CreateAssetMimeSourceTest(TestCase):
    """V8: stored mime_type comes from the sniffer, NOT the client header."""

    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        self.user = User.objects.create_user(
            email="up@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )

    def test_uploaded_content_type_is_overridden_by_sniff(self):
        # Client lies about content_type, sending JPEG bytes labelled as PNG.
        uploaded = SimpleUploadedFile("a.jpg", JPEG, content_type="image/png")
        asset = create_asset(self.org, self.workspace, uploaded, self.user)
        self.assertEqual(asset.mime_type, "image/jpeg")
        self.assertEqual(asset.media_type, "image")

    def test_rejects_html_payload_labelled_as_image(self):
        uploaded = SimpleUploadedFile(
            "fake.jpg",
            b"<html><body>hi</body></html>",
            content_type="image/jpeg",
        )
        with self.assertRaises(ValidationError):
            create_asset(self.org, self.workspace, uploaded, self.user)
