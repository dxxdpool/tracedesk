"""Thin HTTP client for the Evidence Ledger API.

Settings, so each Switch agent (and the human) runs `st` with its own token and name:
  TRACEDESK_API    base URL (default http://localhost:8000)
  TRACEDESK_TOKEN  bearer token: AGENT_TOKEN, VALIDATOR_TOKEN or APPROVER_TOKEN
  TRACEDESK_ACTOR  name recorded on every row, e.g. "investigator"

Environment variables win. Switch cannot set per-agent variables, so TOKEN and ACTOR may also come
from a `tracedesk.env` file in the current directory (each agent's own Switch folder). The API URL is
never read from that file: a cloned repo could plant one, and must not be able to send a real token
to its own server.
"""

import os
from pathlib import Path
from typing import Any

import httpx

DEFAULT_API_URL = "http://localhost:8000"
TIMEOUT_SECONDS = 30.0
CONFIG_FILE_NAME = "tracedesk.env"
FILE_KEYS = frozenset({"TRACEDESK_TOKEN", "TRACEDESK_ACTOR"})


class CliError(Exception):
    """Anything `st` should report and exit non-zero on: a server rejection or a local problem."""

    def __init__(self, reason_code: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.status_code = status_code

    def as_dict(self) -> dict[str, Any]:
        return {"status_code": self.status_code, "reason_code": self.reason_code, "message": self.message}


def read_config_file(directory: Path) -> dict[str, str]:
    """KEY=VALUE lines from `tracedesk.env`; only TRACEDESK_TOKEN and TRACEDESK_ACTOR are kept."""
    path = directory / CONFIG_FILE_NAME
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, has_equals, value = line.partition("=")
        if has_equals and key.strip() in FILE_KEYS:
            values[key.strip()] = value.strip()
    return values


def _required_setting(name: str, file_values: dict[str, str]) -> str:
    value = os.environ.get(name, "").strip() or file_values.get(name, "")
    if not value:
        raise CliError("config_missing", f"Set {name} in the environment or in ./{CONFIG_FILE_NAME}")
    return value


def http_client_from_env() -> httpx.Client:
    file_values = read_config_file(Path.cwd())
    base_url = os.environ.get("TRACEDESK_API", "").strip() or DEFAULT_API_URL
    headers = {
        "Authorization": f"Bearer {_required_setting('TRACEDESK_TOKEN', file_values)}",
        "X-Actor": _required_setting("TRACEDESK_ACTOR", file_values),
    }
    return httpx.Client(base_url=base_url, headers=headers, timeout=TIMEOUT_SECONDS)


def _validation_message(detail: Any) -> str:
    """FastAPI's 422 body lists field errors; turn it into one readable line."""
    if not isinstance(detail, list):
        return str(detail)
    parts = []
    for error in detail:
        location = ".".join(str(part) for part in error.get("loc", [])[1:]) or "request"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts)


def _rejection(resp: httpx.Response) -> CliError:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if isinstance(body, dict) and "reason_code" in body:
        return CliError(body["reason_code"], body.get("message", ""), resp.status_code)
    if isinstance(body, dict) and "detail" in body:
        return CliError("invalid_request", _validation_message(body["detail"]), resp.status_code)
    return CliError(f"http_{resp.status_code}", resp.text[:500] or resp.reason_phrase, resp.status_code)


class ApiClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def request(self, method: str, path: str, *, json: Any = None, params: dict[str, str] | None = None) -> Any:
        try:
            resp = self._http.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise CliError("api_unreachable", f"Cannot reach the API at {self._http.base_url}: {exc}") from exc
        if resp.is_error:
            raise _rejection(resp)
        return resp.json()

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, json: Any) -> Any:
        return self.request("POST", path, json=json)
