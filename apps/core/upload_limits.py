"""Bounded multipart photo uploads for the WSGI and ASGI entry points."""

import json
import re

from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django.http import JsonResponse
from django.utils.translation import gettext

_UUID_PATH = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_PHOTO_PATH = re.compile(
    rf"/(?:api/v1/products/{_UUID_PATH}/photos|catalogue/{_UUID_PATH}/photographie)/?$"
)


def _is_photo_upload(method, path, content_type):
    return (
        method == "POST"
        and _PHOTO_PATH.search(path)
        and content_type.partition(";")[0].strip().lower() == "multipart/form-data"
    )


def _body_limit():
    return settings.PHOTO_MAX_REQUEST_BYTES


def _too_large_response(request=None):
    error = {"code": "REQUEST_REFUSED", "message": gettext("Fichier trop volumineux.")}
    correlation_id = getattr(request, "correlation_id", None)
    if correlation_id is not None:
        error["correlation_id"] = str(correlation_id)
    return JsonResponse({"error": error}, status=413)


def _invalid_length_response(request=None):
    error = {"code": "REQUEST_REFUSED", "message": "Requête refusée."}
    correlation_id = getattr(request, "correlation_id", None)
    if correlation_id is not None:
        error["correlation_id"] = str(correlation_id)
    return JsonResponse({"error": error}, status=400)


class PhotoUploadByteLimitHandler(FileUploadHandler):
    """Stops file parsing before a file can exceed the configured photo limit."""

    def __init__(self, request=None):
        super().__init__(request)
        self.received = 0

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > settings.PHOTO_MAX_BYTES:
            self.request._photo_upload_too_large = True
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        # The following Django handler owns the temporary file and result.
        return None


class PhotoUploadLimitMiddleware:
    """Reject declared oversize bodies before CSRF parses multipart uploads."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not _is_photo_upload(
            request.method,
            request.path_info,
            request.META.get("CONTENT_TYPE", ""),
        ):
            return self.get_response(request)

        raw_length = request.META.get("CONTENT_LENGTH", "")
        if raw_length:
            try:
                content_length = int(raw_length)
            except (TypeError, ValueError):
                return _invalid_length_response(request)
            if content_length < 0:
                return _invalid_length_response(request)
            if content_length > _body_limit():
                return _too_large_response(request)

        request.upload_handlers.insert(0, PhotoUploadByteLimitHandler(request))
        response = self.get_response(request)
        if getattr(request, "_photo_upload_too_large", False):
            return _too_large_response(request)
        return response


class PhotoUploadBodyLimitASGI:
    """Bound photo request bodies before Django's ASGI handler spools them."""

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", ()))
        content_type = headers.get(b"content-type", b"").decode("latin1")
        if not _is_photo_upload(
            scope.get("method", "").upper(), scope.get("path", ""), content_type
        ):
            return await self.application(scope, receive, send)

        limit = _body_limit()
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except (TypeError, ValueError):
                return await self._send_error(send, 400, "Requête refusée.")
            if content_length < 0:
                return await self._send_error(send, 400, "Requête refusée.")
            if content_length > limit:
                return await self._send_error(send, 413, gettext("Fichier trop volumineux."))

        received = 0
        too_large = False
        response_started = False

        async def limited_receive():
            nonlocal received, too_large
            message = await receive()
            if message.get("type") == "http.request":
                chunk_size = len(message.get("body", b""))
                if received + chunk_size > limit:
                    too_large = True
                    # Django closes its bounded spool on disconnect. The wrapper
                    # then returns a 413 without delivering the oversized chunk.
                    return {"type": "http.disconnect"}
                received += chunk_size
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        await self.application(scope, limited_receive, tracked_send)
        if too_large and not response_started:
            await self._send_error(send, 413, gettext("Fichier trop volumineux."))

    @staticmethod
    async def _send_error(send, status, message):
        body = json.dumps(
            {"error": {"code": "REQUEST_REFUSED", "message": message}},
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
