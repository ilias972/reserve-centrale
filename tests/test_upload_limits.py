from unittest.mock import Mock

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.files.uploadhandler import StopUpload
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.core.upload_limits import (
    PhotoUploadBodyLimitASGI,
    PhotoUploadByteLimitHandler,
    PhotoUploadLimitMiddleware,
)


class PhotoUploadLimitTests(SimpleTestCase):
    @override_settings(PHOTO_MAX_BYTES=8, PHOTO_MAX_REQUEST_BYTES=16)
    def test_wsgi_rejects_declared_oversize_before_reading_multipart_body(self):
        request = RequestFactory().post(
            "/catalogue/12345678-1234-1234-1234-123456789abc/photographie",
            data=b"x" * 17,
            content_type="multipart/form-data; boundary=test",
        )
        get_response = Mock(return_value=HttpResponse("reached"))
        response = PhotoUploadLimitMiddleware(get_response)(request)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(request._read_started)
        get_response.assert_not_called()

    @override_settings(PHOTO_MAX_BYTES=8, PHOTO_MAX_REQUEST_BYTES=16)
    def test_upload_handler_stops_before_storing_an_oversize_chunk(self):
        request = RequestFactory().post(
            "/api/v1/products/12345678-1234-1234-1234-123456789abc/photos",
            data=b"",
            content_type="multipart/form-data; boundary=test",
        )
        handler = PhotoUploadByteLimitHandler(request)
        self.assertEqual(handler.receive_data_chunk(b"12345678", 0), b"12345678")
        with self.assertRaises(StopUpload) as stopped:
            handler.receive_data_chunk(b"9", 8)
        self.assertTrue(stopped.exception.connection_reset)
        self.assertTrue(request._photo_upload_too_large)

    @override_settings(PHOTO_MAX_BYTES=8, PHOTO_MAX_REQUEST_BYTES=16)
    def test_asgi_rejects_declared_oversize_without_reading_body(self):
        app = Mock()
        wrapper = PhotoUploadBodyLimitASGI(app)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/products/12345678-1234-1234-1234-123456789abc/photos",
            "headers": [
                (b"content-type", b"multipart/form-data; boundary=test"),
                (b"content-length", b"17"),
            ],
        }
        messages = []

        async def receive():
            self.fail("The ASGI body should not be read after the length check.")

        async def send(message):
            messages.append(message)

        async_to_sync(wrapper)(scope, receive, send)
        self.assertEqual(messages[0]["status"], 413)
        app.assert_not_called()

    @override_settings(PHOTO_MAX_BYTES=8, PHOTO_MAX_REQUEST_BYTES=16)
    def test_asgi_bounds_chunked_body_when_content_length_is_missing(self):
        chunks = [
            {"type": "http.request", "body": b"12345678", "more_body": True},
            {"type": "http.request", "body": b"abcdefghi", "more_body": False},
        ]
        delivered = 0
        responses = []

        async def app(scope, receive, send):
            nonlocal delivered
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                delivered += len(message.get("body", b""))
                if not message.get("more_body", False):
                    await send({"type": "http.response.start", "status": 204, "headers": []})
                    return

        async def receive():
            return chunks.pop(0)

        async def send(message):
            responses.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/catalogue/12345678-1234-1234-1234-123456789abc/photographie",
            "headers": [(b"content-type", b"multipart/form-data; boundary=test")],
        }
        async_to_sync(PhotoUploadBodyLimitASGI(app))(scope, receive, send)
        self.assertEqual(delivered, 8)
        self.assertEqual(
            [m.get("status") for m in responses if m["type"] == "http.response.start"], [413]
        )

    def test_default_body_cap_is_photo_limit_plus_multipart_overhead(self):
        self.assertGreater(settings.PHOTO_MAX_REQUEST_BYTES, settings.PHOTO_MAX_BYTES)
