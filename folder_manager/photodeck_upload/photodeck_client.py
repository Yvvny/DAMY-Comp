import hashlib
import mimetypes
import os
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from typing import Dict, Optional
from urllib.parse import urlparse

import requests
from requests import Response
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException


def _env_flag(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    return value not in {"", "0", "false", "no", "off"}


def default_optional_post_params() -> Dict[str, Optional[str]]:
    return {
        'media[replace]': 1,
        'media[add_to_medias_collections][]': None,
        'media[publish_to_galleries][]': None,
        'artist_id': None,
    }


def default_optional_put_params() -> Dict[str, Optional[str]]:
    return {
        "media[title]": None,
        "media[description]": None,
        "media[keywords]": None,
        "media[add_keywords]": None,
        "media[remove_keywords]": None,
        "media[location]": None,
        "media[city]": None,
        "media[state]": None,
        "media[country]": None,
        "media[region]": None,
        "media[author]": None,
        "media[copyright]": None,
        "media[model_release]": None,
        "media[property_release]": None,
        "media[artist_id]": None,
        "media[medias_collections][]": None,
        "media[add_to_medias_collections][]": None,
        "media[remove_from_medias_collections][]": None,
        "media[galleries][]": None,
        "media[publish_to_galleries][]": None,
        "media[unpublish_from_galleries][]": None,
        "media[add_pricing_profiles][]": os.getenv("PRICING_PROFILE"),
        "media[remove_pricing_profiles][]": None,
    }


class RateLimiter:
    def __init__(self, rps: float = 3.0) -> None:
        self._interval = 1.0 / rps if rps > 0 else 0
        self._lock = threading.Lock()
        self._next_available = 0.0

    def acquire(self) -> None:
        if self._interval <= 0:
            return

        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_available - now)
            self._next_available = max(self._next_available, now) + self._interval

        if wait > 0:
            time.sleep(wait)


class PhotoDeckClient:
    API_HOST = "api.photodeck.com"
    MEDIA_ENDPOINT = "https://api.photodeck.com/medias.xml"
    MAX_429_RETRIES = 5
    MAX_NETWORK_RETRIES = 3
    API_TIMEOUT = (15, 120)
    MAX_STORAGE_RETRIES = 4
    STORAGE_TIMEOUT = (15, 300)
    RETRYABLE_STORAGE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(self, api_key: str, api_secret: str, email: str, password: str, rate_limiter: Optional[RateLimiter] = None) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self._email = email
        self._password = password
        self._debug_enabled = _env_flag("PHOTODECK_DEBUG")
        self.rate_limiter = rate_limiter or RateLimiter()
        self._api_session_local = threading.local()
        self._storage_session_local = threading.local()

    def create_media(
        self,
        file_path: str,
        optional_post: Optional[Dict[str, Optional[str]]] = None,
        file_name_override: Optional[str] = None,
    ) -> ET.Element:
        file_name = file_name_override or os.path.basename(file_path)
        body = {
            'media[content][upload_location]': 'REQUEST',
            'media[content][file_name]': file_name,
            'media[content][file_size]': os.path.getsize(file_path),
            'media[content][mime_type]': mimetypes.guess_type(file_path)[0],
        }

        body.update(self._filter_optional(optional_post))

        response = self.api_call('POST', self.MEDIA_ENDPOINT, data=body)
        return ET.fromstring(response.content)

    def upload_file_to_storage(
        self,
        initial_xml: ET.Element,
        file_path: str,
        file_name_override: Optional[str] = None,
    ) -> None:
        upload_url = initial_xml.findtext('media/upload-url')
        upload_method = initial_xml.findtext('media/upload-method', default='POST').upper()
        upload_param = initial_xml.findtext('media/upload-file-param')
        if not upload_url:
            raise ValueError("Unable to locate upload URL in response payload.")
        if not upload_param:
            raise ValueError("Unable to locate upload file parameter in response payload.")

        params_xml = initial_xml.find('media/upload-params')
        post_body = {}
        if params_xml is not None:
            post_body = {child.tag: child.text for child in list(params_xml)}

        headers = self._signed_headers(upload_method, upload_url)
        headers['Connection'] = 'close'

        mime_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
        upload_name = file_name_override or os.path.basename(file_path)
        self._debug_request(
            label="storage-upload",
            method=upload_method,
            url=upload_url,
            headers=headers,
            data_keys=post_body.keys(),
            session_auth="none",
            extra={
                "file_name": upload_name,
                "file_size": os.path.getsize(file_path),
                "mime_type": mime_type,
                "upload_param": upload_param,
            },
        )

        network_retries = 0
        while True:
            try:
                with open(file_path, 'rb') as file_handle:
                    response = self._session(authenticated=False).request(
                        upload_method,
                        upload_url,
                        data=post_body,
                        files={upload_param: (upload_name, file_handle, mime_type)},
                        headers=headers,
                        timeout=self.STORAGE_TIMEOUT,
                    )
            except RequestException as exc:
                if network_retries < self.MAX_STORAGE_RETRIES:
                    network_retries += 1
                    sleep_for = min(2 ** network_retries, 10)
                    print(
                        f"[WARN] Storage upload error calling {upload_url} ({exc}). "
                        f"Retrying in {sleep_for}s ({network_retries}/{self.MAX_STORAGE_RETRIES})"
                    )
                    time.sleep(sleep_for)
                    continue
                raise

            if response.status_code in self.RETRYABLE_STORAGE_STATUS_CODES and network_retries < self.MAX_STORAGE_RETRIES:
                network_retries += 1
                sleep_for = min(2 ** network_retries, 10)
                self._debug_response("storage-upload", upload_method, upload_url, response)
                print(
                    f"[WARN] Storage upload returned HTTP {response.status_code} for {upload_url}. "
                    f"Retrying in {sleep_for}s ({network_retries}/{self.MAX_STORAGE_RETRIES})"
                )
                response.close()
                time.sleep(sleep_for)
                continue

            self._debug_response("storage-upload", upload_method, upload_url, response)
            response.raise_for_status()
            return

    def finalize_media(self, initial_xml: ET.Element, file_path: str, optional_put: Optional[Dict[str, Optional[str]]] = None) -> None:
        uuid = self._get_media_uuid(initial_xml)
        url = f"https://api.photodeck.com/medias/{uuid}.xml"

        body = {
            'media[content][upload_location]': initial_xml.findtext('media/upload-location'),
            'media[content][file_name]': initial_xml.findtext('media/file-name'),
            'media[content][file_size]': os.path.getsize(file_path),
            'media[content][mime_type]': mimetypes.guess_type(file_path)[0],
        }

        body.update(self._filter_optional(optional_put))

        try:
            self.api_call('PUT', url, data=body)
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            detail = (getattr(response, "text", "") or str(exc)).lower()
            if getattr(response, "status_code", None) == 422 and "content file already processed" in detail:
                print(f"[WARN] PhotoDeck already processed content for {os.path.basename(file_path)}; continuing.")
                return
            raise

    def delete_media(self, initial_xml: ET.Element) -> None:
        uuid = self._get_media_uuid(initial_xml)
        url = f"https://api.photodeck.com/medias/{uuid}.xml"
        self.api_call('DELETE', url)

    def update_gallery(self, website_url_name: str, gallery_uuid: str, fields: Dict[str, Optional[str]]) -> None:
        url = f"https://api.photodeck.com/websites/{website_url_name}/galleries/{gallery_uuid}.xml"
        body = self._filter_optional(fields)
        if not body:
            return
        self.api_call('PUT', url, data=body)

    def api_call(self, method: str, url: str, *, rate_limited: bool = True, **kwargs) -> Response:
        method = method.upper()
        retries = 0
        network_retries = 0
        is_api = urlparse(url).netloc == self.API_HOST

        while True:
            request_kwargs = dict(kwargs)
            headers = dict(request_kwargs.pop('headers', {}) or {})
            if 'timeout' not in request_kwargs:
                request_kwargs['timeout'] = self.API_TIMEOUT

            if is_api:
                headers.update(self._signed_headers(method, url, request_kwargs.get('params')))

            if is_api and rate_limited:
                self.rate_limiter.acquire()

            request_kwargs['headers'] = headers
            self._debug_request(
                label="api-call",
                method=method,
                url=url,
                headers=headers,
                params_keys=(request_kwargs.get('params') or {}).keys(),
                data_keys=(request_kwargs.get('data') or {}).keys() if isinstance(request_kwargs.get('data'), dict) else None,
                session_auth="basic",
            )

            try:
                response = self._session(authenticated=True).request(method, url, **request_kwargs)
            except RequestException as exc:
                if network_retries < self.MAX_NETWORK_RETRIES:
                    network_retries += 1
                    sleep_for = min(2 ** network_retries, 10)
                    print(f"[WARN] Network error calling {url} ({exc}). Retrying in {sleep_for}s "
                          f"({network_retries}/{self.MAX_NETWORK_RETRIES})")
                    time.sleep(sleep_for)
                    continue
                raise

            if response.status_code == 429 and retries < self.MAX_429_RETRIES:
                retries += 1
                self._debug_response("api-call", method, url, response)
                sleep_for = self._retry_after_seconds(response)
                time.sleep(sleep_for)
                continue

            self._debug_response("api-call", method, url, response)
            if response.status_code >= 400:
                detail = (response.text or "").strip()
                if len(detail) > 1200:
                    detail = detail[:1200] + "..."
                message = f"{response.status_code} Client Error for url: {url}"
                if detail:
                    message += f"\nPhotoDeck response:\n{detail}"
                raise requests.HTTPError(message, response=response)
            return response

    def _signed_headers(self, method: str, url: str, params: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        timestamp = formatdate(usegmt=True)
        parsed = urlparse(url)
        endpoint = parsed.path.lstrip('/')
        query_string = parsed.query

        if params:
            prepared = requests.Request(method, url, params=params).prepare()
            query_string = urlparse(prepared.url).query

        sign_data = f"{method}\n/{endpoint}\n{query_string}\n{self.api_secret}\n{timestamp}\n"
        signature = hashlib.sha1(sign_data.encode('utf-8')).hexdigest()

        return {
            'X-PhotoDeck-Authorization': f"{self.api_key}:{signature}",
            'X-PhotoDeck-Timestamp': timestamp,
        }

    def _session(self, *, authenticated: bool) -> requests.Session:
        local = self._api_session_local if authenticated else self._storage_session_local
        session = getattr(local, "session", None)
        if session is None:
            session = requests.Session()
            if authenticated:
                session.auth = HTTPBasicAuth(self._email, self._password)
            local.session = session
        return session

    def _debug_request(
        self,
        *,
        label: str,
        method: str,
        url: str,
        headers: Dict[str, str],
        params_keys=None,
        data_keys=None,
        session_auth: Optional[str] = None,
        extra: Optional[Dict[str, object]] = None,
    ) -> None:
        if not self._debug_enabled:
            return

        parts = [
            f"type={label}",
            f"thread={threading.current_thread().name}",
            f"method={method}",
            f"url={self._safe_url_for_log(url)}",
            f"header_names={sorted(headers.keys())}",
            f"has_x_photodeck_auth={'X-PhotoDeck-Authorization' in headers}",
            f"has_x_photodeck_timestamp={'X-PhotoDeck-Timestamp' in headers}",
        ]
        if session_auth is not None:
            parts.append(f"session_auth={session_auth}")
        if params_keys is not None:
            parts.append(f"param_keys={sorted(params_keys)}")
        if data_keys is not None:
            parts.append(f"data_keys={sorted(data_keys)}")
        if extra:
            extra_text = ", ".join(f"{key}={value!r}" for key, value in extra.items())
            parts.append(f"extra={{ {extra_text} }}")

        print(f"[DEBUG] {' '.join(parts)}")

    def _debug_response(self, label: str, method: str, url: str, response: Response) -> None:
        if not self._debug_enabled:
            return

        request_id = (
            response.headers.get("x-amz-request-id")
            or response.headers.get("x-request-id")
            or response.headers.get("x-photodeck-request-id")
        )
        message = (
            f"[DEBUG] type={label} thread={threading.current_thread().name} "
            f"method={method} url={self._safe_url_for_log(url)} "
            f"status={response.status_code}"
        )
        if request_id:
            message += f" request_id={request_id}"
        print(message)

    @staticmethod
    def _safe_url_for_log(url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path or "/"
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    @staticmethod
    def _retry_after_seconds(response: Response) -> float:
        header = response.headers.get('Retry-After')
        if not header:
            return 1.0

        try:
            return float(header)
        except ValueError:
            try:
                retry_time = parsedate_to_datetime(header)
                if retry_time is not None:
                    if retry_time.tzinfo is None:
                        retry_time = retry_time.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    return max(0.0, (retry_time - now).total_seconds())
            except (TypeError, ValueError):
                pass

        return 1.0

    @staticmethod
    def _filter_optional(optional: Optional[Dict[str, Optional[str]]]) -> Dict[str, Optional[str]]:
        if not optional:
            return {}
        return {key: value for key, value in optional.items() if value is not None}

    @staticmethod
    def _get_media_uuid(initial_xml: ET.Element) -> str:
        uuid = initial_xml.findtext('media/uuid')
        if not uuid:
            raise ValueError("Unable to locate media UUID in response payload.")
        return uuid
