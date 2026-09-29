"""Deterministic live-surface probe for UI paths and OpenAPI discovery."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import urljoin, urlparse

from pydantic import ValidationError

from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.surface import (
    API_DISCOVERY_PATH,
    UI_EXPLORATION_PATH,
    ApiAuth,
    ApiDiscoveryDocument,
    ApiFamily,
    ApiOperation,
    ApiRequestShape,
    ApiResponseShape,
    ExploredPage,
    HttpMethod,
    SurfaceProbeInputV1,
    SurfaceProbeResultV1,
    UiExplorationDocument,
    UiFeature,
)
from assurance_quality.operations.common import InputError, failed_input, succeeded, validate_input

_API_FAMILIES = frozenset({"api", "fuzz", "performance"})

Fetch = Callable[[str], tuple[int, bytes, str]]

_HTTP_METHODS: frozenset[str] = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_VERSION_SEGMENT = re.compile(r"^v\d+$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SurfaceProbeRequest:
    change_id: str
    api_base_url: str | None
    ui_base_url: str | None
    ui_paths: tuple[str, ...]
    needs_api: bool
    needs_ui: bool


@dataclass(frozen=True, slots=True)
class SurfaceProbeResult:
    ui: UiExplorationDocument
    api: ApiDiscoveryDocument


def collect_surface(request: SurfaceProbeRequest, fetch: Fetch) -> SurfaceProbeResult:
    return SurfaceProbeResult(
        ui=_collect_ui(request, fetch),
        api=_collect_api(request, fetch),
    )


def _collect_api(request: SurfaceProbeRequest, fetch: Fetch) -> ApiDiscoveryDocument:
    if not request.needs_api:
        return ApiDiscoveryDocument(
            schema_version="1",
            change_id=request.change_id,
            source="unused",
            base_url="",
            warnings=(),
            families=(),
        )

    if not request.api_base_url:
        return ApiDiscoveryDocument(
            schema_version="1",
            change_id=request.change_id,
            source="unavailable",
            base_url="",
            warnings=("api base url is missing",),
            families=(),
        )

    base_url = request.api_base_url.rstrip("/")
    openapi_url = f"{base_url}/openapi.json"
    try:
        status, body, _final_url = fetch(openapi_url)
    except OSError as error:
        return ApiDiscoveryDocument(
            schema_version="1",
            change_id=request.change_id,
            source="unavailable",
            base_url=base_url,
            warnings=(str(error),),
            families=(),
        )

    if status >= 500:
        return ApiDiscoveryDocument(
            schema_version="1",
            change_id=request.change_id,
            source="unavailable",
            base_url=base_url,
            warnings=(f"HTTP {status}",),
            families=(),
        )

    try:
        payload = json.loads(body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return ApiDiscoveryDocument(
            schema_version="1",
            change_id=request.change_id,
            source="unavailable",
            base_url=base_url,
            warnings=(str(error),),
            families=(),
        )

    paths = payload.get("paths") if isinstance(payload, dict) else None
    if not isinstance(paths, dict):
        return ApiDiscoveryDocument(
            schema_version="1",
            change_id=request.change_id,
            source="unavailable",
            base_url=base_url,
            warnings=("OpenAPI paths object is missing or invalid",),
            families=(),
        )

    families = _families_from_paths(paths)
    return ApiDiscoveryDocument(
        schema_version="1",
        change_id=request.change_id,
        source="live",
        base_url=base_url,
        warnings=(),
        families=families,
    )


def _families_from_paths(paths: dict[str, Any]) -> tuple[ApiFamily, ...]:
    grouped: dict[str, list[ApiOperation]] = {}
    family_auth: dict[str, ApiAuth] = {}

    for path, item in paths.items():
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        if not isinstance(item, dict):
            continue
        raw_path_parameters = item.get("parameters")
        path_parameters: list[Any] = raw_path_parameters if isinstance(raw_path_parameters, list) else []
        for method_key, operation in item.items():
            if method_key not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            method = cast(HttpMethod, method_key.upper())
            api_operation = _operation_from_openapi(
                method=method,
                path=path,
                operation=operation,
                path_parameters=path_parameters,
            )
            family_name = _family_name(path)
            grouped.setdefault(family_name, []).append(api_operation)
            auth = _auth_from_parameters(_merged_parameters(path_parameters, operation.get("parameters")))
            previous = family_auth.get(family_name, "none")
            family_auth[family_name] = _prefer_auth(previous, auth)

    return tuple(
        ApiFamily(
            name=name,
            auth=family_auth.get(name, "none"),
            operations=tuple(operations),
        )
        for name, operations in grouped.items()
        if operations
    )


def _operation_from_openapi(
    *,
    method: HttpMethod,
    path: str,
    operation: dict[str, Any],
    path_parameters: list[Any],
) -> ApiOperation:
    parameters = _merged_parameters(path_parameters, operation.get("parameters"))
    required_headers = tuple(
        str(param["name"])
        for param in parameters
        if isinstance(param, dict)
        and param.get("in") == "header"
        and param.get("required") is True
        and isinstance(param.get("name"), str)
    )
    query = tuple(
        str(param["name"])
        for param in parameters
        if isinstance(param, dict) and param.get("in") == "query" and isinstance(param.get("name"), str)
    )
    body_fields = _request_body_fields(operation.get("requestBody"))
    status_codes, response_fields = _response_shape(operation.get("responses"))
    return ApiOperation(
        method=method,
        path=path,
        request=ApiRequestShape(
            required_headers=required_headers,
            query=query,
            body_fields=body_fields,
        ),
        response=ApiResponseShape(status_codes=status_codes, body_fields=response_fields),
    )


def _merged_parameters(path_parameters: list[Any], operation_parameters: Any) -> list[Any]:
    merged: list[Any] = list(path_parameters)
    if isinstance(operation_parameters, list):
        merged.extend(operation_parameters)
    return merged


def _request_body_fields(request_body: Any) -> tuple[str, ...]:
    schema = _json_schema(request_body)
    if schema is None:
        return ()
    required = schema.get("required")
    if not isinstance(required, list):
        return ()
    return tuple(str(name) for name in required if isinstance(name, str))


def _response_shape(responses: Any) -> tuple[tuple[int, ...], tuple[str, ...]]:
    if not isinstance(responses, dict):
        return (200,), ()
    status_codes: list[int] = []
    body_fields: tuple[str, ...] = ()
    for key, response in responses.items():
        try:
            code = int(key)
        except (TypeError, ValueError):
            continue
        status_codes.append(code)
        if 200 <= code < 300 and not body_fields:
            schema = _json_schema(response)
            if schema is not None:
                properties = schema.get("properties")
                if isinstance(properties, dict):
                    body_fields = tuple(str(name) for name in properties if isinstance(name, str))
    if not status_codes:
        return (200,), ()
    return tuple(sorted(status_codes)), body_fields


def _json_schema(container: Any) -> dict[str, Any] | None:
    if not isinstance(container, dict):
        return None
    content = container.get("content")
    if not isinstance(content, dict):
        return None
    media = content.get("application/json")
    if not isinstance(media, dict):
        return None
    schema = media.get("schema")
    return schema if isinstance(schema, dict) else None


def _auth_from_parameters(parameters: list[Any]) -> ApiAuth:
    auth: ApiAuth = "none"
    for param in parameters:
        if not isinstance(param, dict) or param.get("in") != "header":
            continue
        name = param.get("name")
        if not isinstance(name, str):
            continue
        lowered = name.lower()
        if lowered in {"authorization", "token"}:
            auth = _prefer_auth(auth, "bearer")
        elif lowered == "x-api-key":
            auth = _prefer_auth(auth, "api_key")
    return auth


def _prefer_auth(current: ApiAuth, candidate: ApiAuth) -> ApiAuth:
    rank = {"none": 0, "basic": 1, "api_key": 2, "bearer": 3}
    return candidate if rank[candidate] > rank[current] else current


def _family_name(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if parts and parts[0].lower() == "api":
        parts = parts[1:]
    if parts and _VERSION_SEGMENT.match(parts[0]):
        parts = parts[1:]
    return parts[0] if parts else "root"


def _collect_ui(request: SurfaceProbeRequest, fetch: Fetch) -> UiExplorationDocument:
    if not request.needs_ui:
        return UiExplorationDocument(
            schema_version="1",
            change_id=request.change_id,
            source="unused",
            base_url="",
            warnings=(),
            features=(),
        )

    if not request.ui_base_url:
        return UiExplorationDocument(
            schema_version="1",
            change_id=request.change_id,
            source="unavailable",
            base_url="",
            warnings=("ui base url is missing",),
            features=(),
        )

    if not request.ui_paths:
        return UiExplorationDocument(
            schema_version="1",
            change_id=request.change_id,
            source="unavailable",
            base_url=request.ui_base_url.rstrip("/"),
            warnings=("ui paths are not configured",),
            features=(),
        )

    base_url = request.ui_base_url.rstrip("/")
    features: list[UiFeature] = []
    for index, path in enumerate(request.ui_paths):
        url = f"{base_url}{path}"
        try:
            status, _body, final_url = fetch(url)
        except OSError as error:
            if index == 0:
                return UiExplorationDocument(
                    schema_version="1",
                    change_id=request.change_id,
                    source="unavailable",
                    base_url=base_url,
                    warnings=(str(error),),
                    features=(),
                )
            features.append(_unreached_feature(path))
            continue

        if status >= 500:
            return UiExplorationDocument(
                schema_version="1",
                change_id=request.change_id,
                source="unavailable",
                base_url=base_url,
                warnings=(f"HTTP {status}",),
                features=(),
            )

        if 200 <= status < 300:
            features.append(
                _reached_feature(
                    path=path,
                    status="explored",
                    use_cases_reached=1,
                    use_cases_total=1,
                    landed_path=_url_path(final_url),
                )
            )
        elif 300 <= status < 400:
            features.append(
                _reached_feature(
                    path=path,
                    status="partial",
                    use_cases_reached=1,
                    use_cases_total=2,
                    landed_path=_url_path(final_url),
                )
            )
        else:
            features.append(_unreached_feature(path))

    return UiExplorationDocument(
        schema_version="1",
        change_id=request.change_id,
        source="live",
        base_url=base_url,
        warnings=(),
        features=tuple(features),
    )


def _feature_name(path: str) -> str:
    if path == "/":
        return "root"
    return path.lstrip("/")


def _reached_feature(
    *,
    path: str,
    status: Literal["explored", "partial"],
    use_cases_reached: int,
    use_cases_total: int,
    landed_path: str,
) -> UiFeature:
    flow = f"GET {path}"
    return UiFeature(
        name=_feature_name(path),
        status=status,
        use_cases_reached=use_cases_reached,
        use_cases_total=use_cases_total,
        flows=(flow,),
        pages=(ExploredPage(path=path, landed_path=landed_path),),
        summary=f"{flow} -> {landed_path}",
    )


def _unreached_feature(path: str) -> UiFeature:
    flow = f"GET {path}"
    return UiFeature(
        name=_feature_name(path),
        status="unreached",
        use_cases_reached=0,
        use_cases_total=1,
        flows=(flow,),
        pages=(),
        summary=f"{flow} unreached",
    )


def _url_path(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    return path if path.startswith("/") else f"/{path}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def urllib_fetch(url: str) -> tuple[int, bytes, str]:
    request = urllib.request.Request(url, method="GET")
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=5) as response:
            return int(response.status), response.read(), response.geturl()
    except urllib.error.HTTPError as error:
        body = error.read() if error.fp is not None else b""
        status = int(error.code)
        if 300 <= status < 400:
            location = error.headers.get("Location")
            return status, body, urljoin(url, location) if location else url
        return status, body, error.geturl()


def _write_document(
    write_root: Path,
    relative: str,
    document: UiExplorationDocument | ApiDiscoveryDocument,
) -> EvidenceArtifactRefV1:
    data = canonical_json_bytes(cast(Any, document.model_dump(mode="json")))
    destination = write_root.joinpath(*PurePosixPath(relative).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())


def run_surface_probe(
    request: SurfaceProbeInputV1, write_root: Path, fetch: Fetch = urllib_fetch
) -> SurfaceProbeResultV1:
    families = set(request.candidate_test_families)
    probed = collect_surface(
        SurfaceProbeRequest(
            change_id=request.change_id,
            api_base_url=request.api_base_url,
            ui_base_url=request.ui_base_url,
            ui_paths=request.ui_paths,
            needs_api=bool(families & _API_FAMILIES),
            needs_ui="e2e" in families,
        ),
        fetch,
    )
    return SurfaceProbeResultV1(
        ui_exploration_ref=_write_document(write_root, UI_EXPLORATION_PATH, probed.ui),
        api_discovery_ref=_write_document(write_root, API_DISCOVERY_PATH, probed.api),
        ui_source=probed.ui.source,
        api_source=probed.api.source,
    )


class SurfaceBaselineHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(SurfaceProbeInputV1, request.input)
            result = run_surface_probe(payload, context.write_root, urllib_fetch)
            return succeeded(cast(dict[str, object], result.model_dump(mode="json")))
        except (InputError, ValidationError, OSError, ValueError) as error:
            return failed_input(error)
