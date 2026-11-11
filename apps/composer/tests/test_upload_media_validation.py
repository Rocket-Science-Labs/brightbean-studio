"""Composer upload_media decides the media type from the file's bytes.

It used to trust the browser's Content-Type, so an HTML page labelled
image/png was stored as an image and served back from same-origin storage
(stored XSS), and any unknown type was accepted as a "document".
"""

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.models import User
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


def _png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 10, 10)).save(buf, "PNG")
    return buf.getvalue()


def _gif_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, "GIF")
    return buf.getvalue()


class UploadMediaValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Test Workspace")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user, workspace=self.workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        self.client.force_login(self.user)
        self.url = reverse("composer:upload_media", kwargs={"workspace_id": self.workspace.id})

    def _upload(self, name, data, content_type):
        return self.client.post(self.url, data={"file": SimpleUploadedFile(name, data, content_type=content_type)})

    def test_html_disguised_as_png_is_rejected(self):
        response = self._upload("evil.png", b"<html><script>alert(document.cookie)</script></html>", "image/png")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported", response.json()["error"])
        self.assertFalse(MediaAsset.objects.exists())

    def test_unknown_type_is_no_longer_stored_as_a_document(self):
        response = self._upload("notes.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MediaAsset.objects.exists())

    def test_real_png_is_stored_as_image_with_sniffed_mime(self):
        response = self._upload("slide.png", _png_bytes(), "application/octet-stream")
        self.assertEqual(response.status_code, 200)
        asset = MediaAsset.objects.get()
        self.assertEqual(asset.media_type, MediaAsset.MediaType.IMAGE)
        self.assertEqual(asset.mime_type, "image/png")

    def test_gif_stays_an_image_for_the_publisher(self):
        response = self._upload("anim.gif", _gif_bytes(), "image/gif")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MediaAsset.objects.get().media_type, MediaAsset.MediaType.IMAGE)

    def test_iphone_heic_photo_is_rejected_not_stored_as_video(self):
        heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 16
        response = self._upload("IMG_0001.HEIC", heic, "image/heic")
        self.assertEqual(response.status_code, 400)
        self.assertIn("JPG or PNG", response.json()["error"])
        self.assertFalse(MediaAsset.objects.exists())

    def test_pdf_is_stored_as_document(self):
        response = self._upload("carousel.pdf", b"%PDF-1.7\n%%EOF\n", "application/pdf")
        self.assertEqual(response.status_code, 200)
        asset = MediaAsset.objects.get()
        self.assertEqual(asset.media_type, MediaAsset.MediaType.DOCUMENT)
        self.assertEqual(asset.mime_type, "application/pdf")
        self.assertIn(b'data-media-type="document"', response.content)

    def test_stored_key_uses_the_sniffed_extension_not_the_uploaders(self):
        # A real PNG named .html must not be stored (and later served) as .html.
        response = self._upload("poc.html", _png_bytes(), "image/png")
        self.assertEqual(response.status_code, 200)
        asset = MediaAsset.objects.get()
        self.assertEqual(asset.filename, "poc.html")
        self.assertTrue(asset.file.name.endswith(".png"), asset.file.name)
