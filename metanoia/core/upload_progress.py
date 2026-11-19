"""Redirects for upload-progress requests (static/js/upload-progress.js).

A file form sent by XMLHttpRequest (so the page can show the bytes going up)
would follow a redirect itself, and the page it fetched would use up the
"Uploaded" message the view just queued. For a request marked
``X-Upload-Progress: 1`` a redirect answer becomes ``200 {"redirect": url}``
instead; the script then loads that URL as a normal page, message and all.
Cookies and other headers on the response are kept.
"""
import json


class UploadRedirectMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (request.headers.get('X-Upload-Progress') == '1'
                and response.status_code in (301, 302, 303, 307, 308) and response.has_header('Location')):
            location = response['Location']
            del response['Location']
            response.status_code = 200
            response.reason_phrase = 'OK'
            response['Content-Type'] = 'application/json'
            response['X-Upload-Redirect'] = '1'
            response.content = json.dumps({'redirect': location}).encode()
        return response
