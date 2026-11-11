"""LinkedIn multi-image and document (PDF) posts, against a fake LinkedIn API.

LinkedIn has no organic carousel. Its two multi-page formats are:
* multiImage: 2-20 images from /rest/images, shown as a gallery;
* document: one PDF from /rest/documents, shown as a swipeable viewer.

Every HTTP call is answered by ``FakeLinkedIn`` through httpx's transport, so
these tests prove the exact request sequence and bodies without any network.
"""

import json
from unittest.mock import patch

import httpx
import pytest
from PIL import Image

from providers import linkedin as linkedin_module
from providers.exceptions import PublishError
from providers.linkedin_company import LinkedInCompanyProvider
from providers.linkedin_personal import LinkedInPersonalProvider
from providers.types import PostType, PublishContent

ORG = "urn:li:organization:98765"
REMOTE_PDF = b"%PDF-1.7\n" + b"0" * 64 + b"\n%%EOF\n"


class FakeLinkedIn:
    """Answers LinkedIn REST calls with canned responses and records each request."""

    def __init__(self, document_statuses=("PROCESSING", "AVAILABLE")):
        self.calls: list[dict] = []
        self._image_n = 0
        self._doc_n = 0
        self._doc_statuses = list(document_statuses)

    def handle(self, transport, request: httpx.Request) -> httpx.Response:
        body = request.read()
        url = request.url
        call = {
            "method": request.method,
            "url": str(url),
            "path": url.path,
            "action": url.params.get("action"),
            "headers": dict(request.headers),
            "bytes": body,
            "json": json.loads(body) if body and request.headers.get("content-type") == "application/json" else None,
        }
        self.calls.append(call)

        if url.host == "cdn.example" and request.method == "GET":
            return httpx.Response(200, content=REMOTE_PDF)
        if url.path == "/v2/userinfo":
            return httpx.Response(200, json={"sub": "MOCKPERSON", "name": "Mock Person"})
        if url.path == "/rest/images" and call["action"] == "initializeUpload":
            self._image_n += 1
            n = self._image_n
            return httpx.Response(
                200,
                json={
                    "value": {
                        "uploadUrlExpiresAt": 1,
                        "uploadUrl": f"https://www.linkedin.com/dms-uploads/mock-image-{n}",
                        "image": f"urn:li:image:MOCKIMG{n}",
                    }
                },
            )
        if url.path == "/rest/documents" and call["action"] == "initializeUpload":
            self._doc_n += 1
            n = self._doc_n
            return httpx.Response(
                200,
                json={
                    "value": {
                        "uploadUrlExpiresAt": 1,
                        "uploadUrl": f"https://www.linkedin.com/dms-uploads/mock-document-{n}",
                        "document": f"urn:li:document:MOCKDOC{n}",
                    }
                },
            )
        if url.path.startswith("/dms-uploads/") and request.method == "PUT":
            return httpx.Response(201)
        if url.path.startswith("/rest/documents/") and request.method == "GET":
            status = self._doc_statuses.pop(0) if len(self._doc_statuses) > 1 else self._doc_statuses[0]
            return httpx.Response(200, json={"id": "urn:li:document:MOCKDOC1", "status": status})
        if url.path == "/rest/posts" and request.method == "POST":
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:MOCKPOST"})
        return httpx.Response(404, json={"message": f"unmocked {request.method} {url}"})

    def summary(self) -> list[tuple[str, str]]:
        return [(c["method"], c["path"] + (f"?action={c['action']}" if c["action"] else "")) for c in self.calls]


@pytest.fixture
def fake_linkedin():
    fake = FakeLinkedIn()
    with (
        patch.object(httpx.HTTPTransport, "handle_request", autospec=True, side_effect=fake.handle),
        patch.object(linkedin_module.time, "sleep"),
    ):
        yield fake


def _png(path, size=(64, 80), color=(10, 120, 200)):
    Image.new("RGB", size, color).save(path, "PNG")
    return str(path)


@pytest.fixture
def six_pngs(tmp_path):
    return [_png(tmp_path / f"slide-0{i}.png", color=(i * 30, 100, 150)) for i in range(1, 7)]


@pytest.fixture
def pdf_file(tmp_path):
    path = tmp_path / "carousel.pdf"
    path.write_bytes(b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    return str(path)


class TestMultiImagePost:
    def test_six_images_upload_each_then_one_multi_image_post(self, fake_linkedin, six_pngs):
        content = PublishContent(
            text="Coming soon (P02)",
            media_files=six_pngs,
            post_type=PostType.MULTI_IMAGE,
            extra={"author": ORG, "media_alt_texts": ["Cover slide", "", "", "", "", "Last slide"]},
        )

        result = LinkedInCompanyProvider().publish_post("fake-token", content)

        expected = []
        for _ in range(6):
            expected += [("POST", "/rest/images?action=initializeUpload"), ("PUT", None)]
        expected.append(("POST", "/rest/posts"))
        got = fake_linkedin.summary()
        assert len(got) == len(expected) == 13
        for (method, path), (exp_method, exp_path) in zip(got, expected, strict=True):
            assert method == exp_method
            if exp_path:
                assert path == exp_path

        inits = [c for c in fake_linkedin.calls if c["action"] == "initializeUpload"]
        assert all(c["json"] == {"initializeUploadRequest": {"owner": ORG}} for c in inits)
        assert all(c["headers"]["linkedin-version"] == "202604" for c in inits)
        assert all(c["headers"]["x-restli-protocol-version"] == "2.0.0" for c in inits)

        puts = [c for c in fake_linkedin.calls if c["method"] == "PUT"]
        assert [c["path"] for c in puts] == [f"/dms-uploads/mock-image-{i}" for i in range(1, 7)]
        for put, source in zip(puts, six_pngs, strict=True):
            with open(source, "rb") as f:
                assert put["bytes"] == f.read()

        post = fake_linkedin.calls[-1]["json"]
        assert post["author"] == ORG
        assert post["commentary"] == "Coming soon \\(P02\\)"
        assert post["lifecycleState"] == "PUBLISHED"
        assert post["content"] == {
            "multiImage": {
                "images": [
                    {"id": "urn:li:image:MOCKIMG1", "altText": "Cover slide"},
                    {"id": "urn:li:image:MOCKIMG2"},
                    {"id": "urn:li:image:MOCKIMG3"},
                    {"id": "urn:li:image:MOCKIMG4"},
                    {"id": "urn:li:image:MOCKIMG5"},
                    {"id": "urn:li:image:MOCKIMG6", "altText": "Last slide"},
                ]
            }
        }
        assert result.platform_post_id == "urn:li:share:MOCKPOST"
        assert result.extra["image_urns"] == [f"urn:li:image:MOCKIMG{i}" for i in range(1, 7)]

    def test_carousel_hint_is_published_as_multi_image_not_text(self, fake_linkedin, six_pngs):
        content = PublishContent(text="x", media_files=six_pngs[:2], post_type=PostType.CAROUSEL, extra={"author": ORG})
        LinkedInCompanyProvider().publish_post("fake-token", content)
        assert "multiImage" in fake_linkedin.calls[-1]["json"]["content"]

    @pytest.mark.parametrize("count", [1, 21])
    def test_rejects_counts_outside_two_to_twenty_before_any_request(self, fake_linkedin, tmp_path, count):
        files = [_png(tmp_path / f"i{i}.png") for i in range(count)]
        content = PublishContent(text="x", media_files=files, post_type=PostType.MULTI_IMAGE, extra={"author": ORG})
        with pytest.raises(PublishError, match="2 to 20 images") as exc:
            LinkedInCompanyProvider().publish_post("fake-token", content)
        assert exc.value.retryable is False
        assert fake_linkedin.calls == []

    def test_rejects_a_pdf_mixed_into_images_before_any_upload(self, fake_linkedin, six_pngs, pdf_file):
        content = PublishContent(
            text="x", media_files=[six_pngs[0], pdf_file], post_type=PostType.MULTI_IMAGE, extra={"author": ORG}
        )
        with pytest.raises(PublishError, match="Image 2 is not a JPG, PNG or GIF"):
            LinkedInCompanyProvider().publish_post("fake-token", content)
        assert fake_linkedin.calls == []

    def test_rejects_webp(self, fake_linkedin, six_pngs, tmp_path):
        webp = tmp_path / "x.webp"
        Image.new("RGB", (8, 8)).save(webp, "WEBP")
        content = PublishContent(
            text="x", media_files=[six_pngs[0], str(webp)], post_type=PostType.MULTI_IMAGE, extra={"author": ORG}
        )
        with pytest.raises(PublishError, match="Image 2 is WEBP"):
            LinkedInCompanyProvider().publish_post("fake-token", content)
        assert fake_linkedin.calls == []

    def test_rejects_image_at_the_pixel_limit(self, fake_linkedin, six_pngs, tmp_path):
        big = tmp_path / "big.png"
        Image.new("1", (6100, 6000)).save(big, "PNG")  # 36.6M px >= 36,152,320
        content = PublishContent(
            text="x", media_files=[six_pngs[0], str(big)], post_type=PostType.MULTI_IMAGE, extra={"author": ORG}
        )
        with pytest.raises(PublishError, match="Image 2 is 6100x6000"):
            LinkedInCompanyProvider().publish_post("fake-token", content)
        assert fake_linkedin.calls == []

    def test_rejects_alt_text_over_limit(self, fake_linkedin, six_pngs):
        content = PublishContent(
            text="x",
            media_files=six_pngs[:2],
            post_type=PostType.MULTI_IMAGE,
            extra={"author": ORG, "media_alt_texts": ["a" * 4087, ""]},
        )
        with pytest.raises(PublishError, match="Alt text for image 1"):
            LinkedInCompanyProvider().publish_post("fake-token", content)
        assert fake_linkedin.calls == []

    def test_accepts_a_phone_jpeg_that_pillow_reads_as_mpo(self, fake_linkedin, six_pngs, tmp_path):
        # Phone cameras write JPEGs with an extra embedded image (Multi-Picture
        # Format). The bytes are a JPEG, but Pillow reports the format as "MPO".
        mpo = tmp_path / "phone.jpg"
        Image.new("RGB", (16, 16), (1, 2, 3)).save(
            mpo, "MPO", save_all=True, append_images=[Image.new("RGB", (16, 16), (4, 5, 6))]
        )
        with Image.open(mpo) as img:
            assert img.format == "MPO"
        content = PublishContent(
            text="x", media_files=[six_pngs[0], str(mpo)], post_type=PostType.MULTI_IMAGE, extra={"author": ORG}
        )

        LinkedInCompanyProvider().publish_post("fake-token", content)

        assert fake_linkedin.calls[-1]["json"]["content"]["multiImage"]["images"][1] == {"id": "urn:li:image:MOCKIMG2"}

    def test_decompression_bomb_reports_the_pixel_limit(self, fake_linkedin, six_pngs, monkeypatch):
        # Pillow refuses to open images over twice MAX_IMAGE_PIXELS; shrink the
        # threshold so a small test image trips it.
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
        content = PublishContent(
            text="x", media_files=six_pngs[:2], post_type=PostType.MULTI_IMAGE, extra={"author": ORG}
        )
        with pytest.raises(PublishError, match="Image 1 has too many pixels") as exc:
            LinkedInCompanyProvider().publish_post("fake-token", content)
        assert "36,152,320" in str(exc.value)
        assert "not a JPG" not in str(exc.value)
        assert exc.value.retryable is False
        assert fake_linkedin.calls == []


class TestDocumentPost:
    def test_pdf_upload_waits_for_available_then_posts_document(self, fake_linkedin, pdf_file):
        content = PublishContent(
            text="Swipe through",
            media_files=[pdf_file],
            post_type=PostType.DOCUMENT,
            extra={"document_title": "carousel.pdf"},
        )

        # Personal account in OIDC mode: no author in extra, so it comes from /v2/userinfo.
        result = LinkedInPersonalProvider().publish_post("fake-token", content)

        assert fake_linkedin.summary() == [
            ("GET", "/v2/userinfo"),
            ("POST", "/rest/documents?action=initializeUpload"),
            ("PUT", "/dms-uploads/mock-document-1"),
            ("GET", "/rest/documents/urn:li:document:MOCKDOC1"),
            ("GET", "/rest/documents/urn:li:document:MOCKDOC1"),
            ("POST", "/rest/posts"),
        ]
        init, put, poll = fake_linkedin.calls[1], fake_linkedin.calls[2], fake_linkedin.calls[3]
        assert init["json"] == {"initializeUploadRequest": {"owner": "urn:li:person:MOCKPERSON"}}
        with open(pdf_file, "rb") as f:
            assert put["bytes"] == f.read()
        # The URN is percent-encoded in the path, as LinkedIn requires.
        assert "urn%3Ali%3Adocument%3AMOCKDOC1" in poll["url"]

        post = fake_linkedin.calls[-1]["json"]
        assert post["author"] == "urn:li:person:MOCKPERSON"
        assert post["content"] == {"media": {"title": "carousel.pdf", "id": "urn:li:document:MOCKDOC1"}}
        assert result.extra["document_urn"] == "urn:li:document:MOCKDOC1"

    def test_post_title_wins_over_filename(self, fake_linkedin, pdf_file):
        content = PublishContent(
            text="x",
            title="Our 6-step guide",
            media_files=[pdf_file],
            post_type=PostType.DOCUMENT,
            extra={"author": ORG, "document_title": "carousel.pdf"},
        )
        LinkedInCompanyProvider().publish_post("fake-token", content)
        assert fake_linkedin.calls[-1]["json"]["content"]["media"]["title"] == "Our 6-step guide"

    def test_rejects_more_than_one_file(self, fake_linkedin, pdf_file, six_pngs):
        content = PublishContent(
            text="x", media_files=[pdf_file, six_pngs[0]], post_type=PostType.DOCUMENT, extra={"author": ORG}
        )
        with pytest.raises(PublishError, match="exactly one PDF"):
            LinkedInCompanyProvider().publish_post("fake-token", content)
        assert fake_linkedin.calls == []

    def test_rejects_a_file_that_is_not_a_pdf(self, fake_linkedin, six_pngs):
        content = PublishContent(
            text="x", media_files=[six_pngs[0]], post_type=PostType.DOCUMENT, extra={"author": ORG}
        )
        with pytest.raises(PublishError, match="not a PDF"):
            LinkedInCompanyProvider().publish_post("fake-token", content)
        assert fake_linkedin.calls == []

    def test_rejects_pdf_over_size_limit(self, fake_linkedin, pdf_file, monkeypatch):
        monkeypatch.setattr(linkedin_module, "DOCUMENT_MAX_BYTES", 10)
        content = PublishContent(text="x", media_files=[pdf_file], post_type=PostType.DOCUMENT, extra={"author": ORG})
        with pytest.raises(PublishError, match="up to 100 MB"):
            LinkedInCompanyProvider().publish_post("fake-token", content)
        assert fake_linkedin.calls == []

    def test_remote_pdf_is_fetched_once_under_the_document_ceiling(self, fake_linkedin, monkeypatch):
        # A URL source goes through the provider's bounded fetch. Documents get
        # LinkedIn's 100 MB ceiling, not the 20 MB remote-image one.
        monkeypatch.setattr(linkedin_module, "MAX_REMOTE_MEDIA_BYTES", 10)
        content = PublishContent(
            text="x",
            media_urls=["https://cdn.example/guide.pdf?sig=abc"],
            post_type=PostType.DOCUMENT,
            extra={"author": ORG},
        )

        LinkedInCompanyProvider().publish_post("fake-token", content)

        assert [c for c in fake_linkedin.calls if "cdn.example" in c["url"]] == fake_linkedin.calls[:1]
        put = next(c for c in fake_linkedin.calls if c["method"] == "PUT")
        assert put["bytes"] == REMOTE_PDF
        assert fake_linkedin.calls[-1]["json"]["content"]["media"]["title"] == "guide.pdf"

    def test_processing_failed_stops_before_posting(self, pdf_file):
        fake = FakeLinkedIn(document_statuses=("PROCESSING_FAILED",))
        content = PublishContent(text="x", media_files=[pdf_file], post_type=PostType.DOCUMENT, extra={"author": ORG})
        with (
            patch.object(httpx.HTTPTransport, "handle_request", autospec=True, side_effect=fake.handle),
            patch.object(linkedin_module.time, "sleep"),
            pytest.raises(PublishError, match="rejected the document during processing"),
        ):
            LinkedInCompanyProvider().publish_post("fake-token", content)
        assert ("POST", "/rest/posts") not in fake.summary()


def test_linkedin_advertises_the_new_post_types():
    provider = LinkedInCompanyProvider()
    assert PostType.MULTI_IMAGE in provider.supported_post_types
    assert PostType.DOCUMENT in provider.supported_post_types
