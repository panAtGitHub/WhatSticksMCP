#!/usr/bin/env python3
"""Read-only stdio MCP server for the WhatSticks WebDAV mirror.

Only GET requests are implemented.  Configuration stores the WebDAV URL,
username, remote folder, and cache location; the password is loaded from the
host operating system's secure credential store (or an explicitly named test
environment variable).
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


FORMAT_VERSION = 1
SCHEMA_IDENTIFIER = "WhatSticksCoreV2SchemaV3"
SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
MAX_LIMIT = 100
MAX_TRACE_TEXT = 20_000
HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class MCPError(RuntimeError):
    """An actionable error safe to expose to an MCP client."""


class NotFoundError(MCPError):
    pass


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, separators=(",", ": ")).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_parts(path: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(path, str) or "\x00" in path:
        raise MCPError("远端路径无效。")
    if path.startswith("/"):
        raise MCPError("远端路径不能是绝对路径。")
    parts = [part for part in path.split("/") if part]
    if not parts and not allow_empty:
        raise MCPError("远端目录不能为空。")
    if any(part in {".", ".."} or "\\" in part for part in parts):
        raise MCPError("远端路径包含非法段。")
    return parts


def normalized_folder(value: str) -> str:
    parts = safe_parts(value, allow_empty=False)
    return "/".join(parts)


def is_safe_relative_path(value: str) -> bool:
    try:
        parts = safe_parts(value, allow_empty=False)
    except MCPError:
        return False
    return bool(parts)


def parse_datetime(value: Any, *, end_of_day: bool = False) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        text = str(value)
    else:
        text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        date_value = dt.datetime.strptime(text, "%Y%m%d").date()
        if end_of_day:
            return dt.datetime.combine(date_value, dt.time.max, tzinfo=dt.timezone.utc)
        return dt.datetime.combine(date_value, dt.time.min, tzinfo=dt.timezone.utc)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        date_value = dt.date.fromisoformat(text)
        if end_of_day:
            return dt.datetime.combine(date_value, dt.time.max, tzinfo=dt.timezone.utc)
        return dt.datetime.combine(date_value, dt.time.min, tzinfo=dt.timezone.utc)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise MCPError(f"无法解析日期或 UTC 时间：{text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def iso_date(day_key: int) -> str:
    text = str(day_key)
    if not re.fullmatch(r"\d{8}", text):
        return text
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def day_key_from_date(value: dt.date) -> int:
    return int(value.strftime("%Y%m%d"))


def record_datetime(record: dict[str, Any], field: str = "createdAt") -> dt.datetime | None:
    try:
        return parse_datetime(record.get(field))
    except MCPError:
        return None


def paginate(items: list[Any], arguments: dict[str, Any]) -> tuple[list[Any], str | None]:
    raw_limit = arguments.get("limit", 20)
    try:
        limit = max(1, min(MAX_LIMIT, int(raw_limit)))
    except (TypeError, ValueError) as exc:
        raise MCPError("limit 必须是 1 到 100 的整数。") from exc
    cursor = arguments.get("cursor")
    try:
        offset = max(0, int(cursor or 0))
    except (TypeError, ValueError) as exc:
        raise MCPError("cursor 必须是非负整数偏移量。") from exc
    page = items[offset : offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(items) else None
    return page, next_cursor


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        old = urlsplit(req.full_url)
        new = urlsplit(newurl)
        if (
            new.scheme.lower() != "https"
            or new.hostname != old.hostname
            or (new.port or (443 if new.scheme == "https" else None))
            != (old.port or (443 if old.scheme == "https" else None))
        ):
            raise MCPError("WebDAV 请求被重定向到未授权的主机，已停止请求。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_cache_dir() -> Path:
    if sys.platform == "darwin":
        return Path("~/Library/Caches/WhatSticksMCP").expanduser()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "WhatSticksMCP" / "Cache"
    return Path("~/.cache/WhatSticksMCP").expanduser()


def credential_backend_name() -> str | None:
    if sys.platform == "darwin":
        return "macOS Keychain"
    if os.name == "nt":
        return "Windows Credential Manager"
    return None


@dataclass(frozen=True)
class ServerConfig:
    webdav_url: str
    username: str
    remote_folder: str
    cache_dir: Path
    credential_service: str
    password_env: str | None = None

    @classmethod
    def from_file(cls, path: Path) -> "ServerConfig":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise MCPError(f"找不到 MCP 配置文件：{path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise MCPError(f"无法读取 MCP 配置文件：{exc}") from exc
        if not isinstance(raw, dict):
            raise MCPError("MCP 配置必须是 JSON 对象。")
        webdav_url = str(raw.get("webdavUrl", "")).strip()
        parts = urlsplit(webdav_url)
        if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise MCPError("webdavUrl 必须是没有用户名/密码的 HTTPS 地址。")
        username = str(raw.get("username", "")).strip()
        if not username:
            raise MCPError("MCP 配置缺少 username。")
        folder = normalized_folder(str(raw.get("remoteFolder", "WhatSticks")))
        cache_value = str(raw.get("cacheDir", default_cache_dir()))
        cache_dir = Path(cache_value).expanduser()
        service = str(raw.get("credentialService", raw.get("keychainService", "com.dusk.WhatSticks.webdav")))
        password_env = raw.get("passwordEnv")
        if password_env is not None:
            password_env = str(password_env)
        return cls(
            webdav_url=webdav_url,
            username=username,
            remote_folder=folder,
            cache_dir=cache_dir,
            credential_service=service,
            password_env=password_env,
        )


def _credential_target(service: str, username: str) -> str:
    return f"WhatSticksMCP/{service}/{username}"


def _macos_keychain_password(config: ServerConfig) -> str:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", config.credential_service, "-a", config.username, "-w"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MCPError("无法访问 macOS Keychain，请先运行 setup。") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise MCPError("没有找到 WebDAV Keychain 凭据，请先运行 setup。")
    return result.stdout.rstrip("\r\n")


def _windows_credential_password(config: ServerConfig) -> str:
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_read = advapi32.CredReadW
    cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    cred_read.restype = wintypes.BOOL
    cred_free = advapi32.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    cred_free.restype = wintypes.BOOL
    credential_pointer = ctypes.POINTER(CREDENTIAL)()
    target = _credential_target(config.credential_service, config.username)
    if not cred_read(target, 1, 0, ctypes.byref(credential_pointer)):
        raise MCPError("没有找到 Windows Credential Manager 凭据，请先运行 setup。")
    try:
        credential = credential_pointer.contents
        raw_password = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return raw_password.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise MCPError("Windows Credential Manager 中的 WebDAV 凭据无效，请重新运行 setup。") from exc
    finally:
        cred_free(credential_pointer)


def credential_password(config: ServerConfig) -> str:
    if config.password_env:
        value = os.environ.get(config.password_env)
        if value:
            return value
    if sys.platform == "darwin":
        return _macos_keychain_password(config)
    if os.name == "nt":
        return _windows_credential_password(config)
    raise MCPError("当前系统暂不支持安全凭据存储；v1.0.0 目前支持 macOS 和 Windows。")


class WebDAVReader:
    """A GET-only WebDAV reader scoped to one configured remote folder."""

    def __init__(self, config: ServerConfig, password: str):
        self.config = config
        self.password = password
        self._opener = build_opener(SafeRedirectHandler())

    def _url(self, relative_path: str) -> str:
        parts = urlsplit(self.config.webdav_url)
        base_path = parts.path.rstrip("/")
        path_parts = safe_parts(self.config.remote_folder, allow_empty=False) + safe_parts(relative_path)
        encoded = "/".join(quote(part, safe="") for part in path_parts)
        full_path = f"{base_path}/{encoded}" if encoded else f"{base_path}/"
        return urlunsplit((parts.scheme, parts.netloc, full_path, "", ""))

    def get(self, relative_path: str) -> bytes:
        if relative_path != "current.json" and not is_safe_relative_path(relative_path):
            raise MCPError("MCP 访问了不安全的远端路径。")
        url = self._url(relative_path)
        token = base64.b64encode(f"{self.config.username}:{self.password}".encode("utf-8")).decode("ascii")
        request = Request(url, method="GET", headers={"Authorization": f"Basic {token}"})
        try:
            with self._opener.open(request, timeout=30) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 404:
                raise NotFoundError("远端镜像文件不存在。") from exc
            if exc.code == 401:
                raise MCPError("WebDAV 认证失败（401）。") from exc
            if exc.code == 403:
                raise MCPError("WebDAV 拒绝访问（403）。") from exc
            raise MCPError(f"WebDAV 读取失败（{exc.code}）。") from exc
        except URLError as exc:
            raise MCPError(f"WebDAV 网络不可达：{exc.reason}") from exc
        except TimeoutError as exc:
            raise MCPError("WebDAV 请求超时。") from exc


@dataclass
class Snapshot:
    current: dict[str, Any]
    release: dict[str, Any]
    records: dict[str, list[dict[str, Any]]]
    stale: bool
    cache_error: str | None


class MirrorRepository:
    def __init__(self, config: ServerConfig, password: str):
        self.config = config
        self.reader = WebDAVReader(config, password)
        self._snapshot: Snapshot | None = None

    @property
    def cache_dir(self) -> Path:
        return self.config.cache_dir

    def load(self, *, force: bool = False) -> Snapshot:
        if self._snapshot is not None and not force:
            return self._snapshot
        try:
            snapshot = self._load_remote()
        except MCPError as remote_error:
            try:
                snapshot = self._load_cached()
                snapshot.stale = True
                snapshot.cache_error = str(remote_error)
            except MCPError:
                raise remote_error
        self._snapshot = snapshot
        return snapshot

    def _load_remote(self) -> Snapshot:
        current_bytes = self.reader.get("current.json")
        current = self._decode_object(current_bytes, "current.json")
        self._validate_current(current)
        release_bytes = self.reader.get(current["releasePath"])
        if sha256(release_bytes) != current["releaseSHA256"]:
            raise MCPError("远端 release SHA-256 校验失败。")
        release = self._decode_object(release_bytes, current["releasePath"])
        self._validate_release(release, current)
        records = self._load_partitions(release)
        self._write_cache(current_bytes, release_bytes, current, release)
        return Snapshot(current=current, release=release, records=records, stale=False, cache_error=None)

    def _load_cached(self) -> Snapshot:
        current_path = self.cache_dir / "current.json"
        if not current_path.exists():
            raise MCPError("没有可用的本地镜像缓存。")
        current_bytes = current_path.read_bytes()
        current = self._decode_object(current_bytes, "cached current.json")
        self._validate_current(current)
        release_path = self.cache_dir / "releases" / f"{current['releaseSHA256']}.json"
        if not release_path.exists():
            raise MCPError("本地缓存缺少上次有效 release。")
        release_bytes = release_path.read_bytes()
        if sha256(release_bytes) != current["releaseSHA256"]:
            raise MCPError("本地 release 缓存校验失败。")
        release = self._decode_object(release_bytes, "cached release")
        self._validate_release(release, current)
        records = self._load_partitions(release, remote=False)
        return Snapshot(current=current, release=release, records=records, stale=True, cache_error=None)

    def _load_partitions(self, release: dict[str, Any], *, remote: bool = True) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for partition in release["partitions"]:
            path = str(partition["path"])
            digest = str(partition["sha256"])
            cache_path = self.cache_dir / "partitions" / f"{digest}.json"
            data: bytes | None = None
            if cache_path.exists():
                candidate = cache_path.read_bytes()
                if len(candidate) == int(partition["sizeBytes"]) and sha256(candidate) == digest:
                    data = candidate
            if data is None:
                if not remote:
                    raise MCPError(f"本地缓存缺少分片：{partition['logicalPartition']}")
                data = self.reader.get(path)
                if len(data) != int(partition["sizeBytes"]) or sha256(data) != digest:
                    raise MCPError(f"远端分片校验失败：{partition['logicalPartition']}")
                self._write_atomic(cache_path, data)
            envelope = self._decode_object(data, path)
            if envelope.get("formatVersion") != FORMAT_VERSION or envelope.get("logicalPartition") != partition["logicalPartition"]:
                raise MCPError(f"分片协议版本或名称不匹配：{partition['logicalPartition']}")
            records = envelope.get("records")
            if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
                raise MCPError(f"分片记录结构无效：{partition['logicalPartition']}")
            if len(records) != int(partition["recordCount"]):
                raise MCPError(f"分片记录数校验失败：{partition['logicalPartition']}")
            result[partition["logicalPartition"]] = records
        return result

    def _write_cache(self, current_bytes: bytes, release_bytes: bytes, current: dict[str, Any], release: dict[str, Any]) -> None:
        self._write_atomic(self.cache_dir / "current.json", current_bytes)
        self._write_atomic(self.cache_dir / "releases" / f"{current['releaseSHA256']}.json", release_bytes)
        state = {
            "generationID": current["generationID"],
            "releaseSHA256": current["releaseSHA256"],
            "cachedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self._write_atomic(self.cache_dir / "state.json", json_bytes(state))

    def _write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        temporary.write_bytes(data)
        os.replace(temporary, path)

    @staticmethod
    def _decode_object(data: bytes, label: str) -> dict[str, Any]:
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPError(f"{label} 不是有效 JSON。") from exc
        if not isinstance(value, dict):
            raise MCPError(f"{label} 必须是 JSON 对象。")
        return value

    @staticmethod
    def _validate_current(current: dict[str, Any]) -> None:
        if current.get("formatVersion") != FORMAT_VERSION:
            raise MCPError("current.json 的协议版本不受支持。")
        release_path = current.get("releasePath")
        if not isinstance(release_path, str) or not release_path.startswith("releases/") or not release_path.endswith(".json") or not is_safe_relative_path(release_path):
            raise MCPError("current.json 的 releasePath 不安全。")
        if not isinstance(current.get("generationID"), str) or not current["generationID"]:
            raise MCPError("current.json 缺少 generationID。")
        if not isinstance(current.get("deviceID"), str) or not current["deviceID"]:
            raise MCPError("current.json 缺少 deviceID。")
        if not isinstance(current.get("releaseSHA256"), str) or not HEX_64.fullmatch(current["releaseSHA256"]):
            raise MCPError("current.json 的 releaseSHA256 无效。")

    @staticmethod
    def _validate_release(release: dict[str, Any], current: dict[str, Any]) -> None:
        if release.get("formatVersion") != FORMAT_VERSION or release.get("schemaIdentifier") != SCHEMA_IDENTIFIER:
            raise MCPError("release 的协议或 Schema 版本不受支持。")
        if release.get("generationID") != current.get("generationID"):
            raise MCPError("release 与 current.json 的 generationID 不一致。")
        partitions = release.get("partitions")
        if not isinstance(partitions, list):
            raise MCPError("release 缺少 partitions。")
        for partition in partitions:
            if not isinstance(partition, dict):
                raise MCPError("release partition 结构无效。")
            logical = partition.get("logicalPartition")
            path = partition.get("path")
            if not isinstance(logical, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", logical):
                raise MCPError("release 的 logicalPartition 无效。")
            digest = partition.get("sha256")
            if not isinstance(digest, str) or not HEX_64.fullmatch(digest):
                raise MCPError("release 的 partition SHA-256 无效。")
            if (
                not isinstance(path, str)
                or path != f"data/{logical}/{digest}.json"
                or not is_safe_relative_path(path)
            ):
                raise MCPError("release 的数据路径与分区校验值不匹配。")
            if not isinstance(partition.get("sizeBytes"), int) or partition["sizeBytes"] < 0:
                raise MCPError("release 的 sizeBytes 无效。")
            if not isinstance(partition.get("recordCount"), int) or partition["recordCount"] < 0:
                raise MCPError("release 的 recordCount 无效。")
        media = release.get("media", [])
        if not isinstance(media, list):
            raise MCPError("release 的 media 结构无效。")
        for item in media:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"].startswith("media/") or not is_safe_relative_path(item["path"]):
                raise MCPError("release 的媒体路径不安全。")
            if not isinstance(item.get("sha256"), str) or not HEX_64.fullmatch(item["sha256"]):
                raise MCPError("release 的媒体 SHA-256 无效。")

    def status(self) -> dict[str, Any]:
        snapshot = self.load()
        return {
            "formatVersion": snapshot.release["formatVersion"],
            "schemaIdentifier": snapshot.release["schemaIdentifier"],
            "generationID": snapshot.current["generationID"],
            "publishedAt": snapshot.current.get("publishedAt"),
            "sourceDeviceID": snapshot.current["deviceID"],
            "sourceAppVersion": snapshot.release.get("sourceAppVersion"),
            "entityCounts": snapshot.release.get("entityCounts", {}),
            "partitionCount": len(snapshot.release["partitions"]),
            "mediaCount": len(snapshot.release.get("media", [])),
            "cache": {
                "state": "stale" if snapshot.stale else "latest",
                "error": snapshot.cache_error,
            },
        }

    def records(self, name: str) -> list[dict[str, Any]]:
        return list(self.load().records.get(name, []))


def project_records(repo: MirrorRepository) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    projects = repo.records("projects")
    traces = repo.records("traces")
    project_map = {str(record.get("id")): record for record in projects if record.get("id")}
    active_trace_counts: dict[str, int] = {}
    for trace in traces:
        if trace.get("deletedAt") is None:
            project_id = str(trace.get("projectID"))
            active_trace_counts[project_id] = active_trace_counts.get(project_id, 0) + 1
    return project_map, traces


def is_deleted(record: dict[str, Any]) -> bool:
    return record.get("deletedAt") is not None


def project_output(project: dict[str, Any], trace_count: int) -> dict[str, Any]:
    return {
        "id": project.get("id"),
        "title": project.get("title", ""),
        "status": "deleted" if is_deleted(project) else "archived" if project.get("archivedAt") else "active",
        "updatedAt": project.get("updatedAt"),
        "archivedAt": project.get("archivedAt"),
        "deletedAt": project.get("deletedAt"),
        "traceCount": trace_count,
        "lifeDimensionID": project.get("lifeDimensionID"),
    }


def trace_output(trace: dict[str, Any], project: dict[str, Any] | None, *, include_text: bool = False) -> dict[str, Any]:
    text = str(trace.get("text", ""))
    output = {
        "id": trace.get("id"),
        "projectID": trace.get("projectID"),
        "projectTitle": project.get("title") if project else None,
        "textPreview": trace.get("textPreview") or text[:240],
        "contentKind": trace.get("contentKindRawValue"),
        "createdAt": trace.get("createdAt"),
        "updatedAt": trace.get("updatedAt"),
        "deletedAt": trace.get("deletedAt"),
        "primaryAssetID": trace.get("primaryAssetID"),
    }
    if include_text:
        output["text"] = text[:MAX_TRACE_TEXT]
        output["textTruncated"] = len(text) > MAX_TRACE_TEXT
    return output


def tool_definitions() -> list[dict[str, Any]]:
    def schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            result["required"] = required
        return result

    return [
        {
            "name": "whatsticks_status",
            "description": "读取 WhatSticks 当前已发布镜像、来源设备、实体计数和缓存状态。只读。",
            "inputSchema": schema({}),
        },
        {
            "name": "whatsticks_list_projects",
            "description": "按名称或备注搜索项目；默认排除已删除和已归档项目。",
            "inputSchema": schema({
                "query": {"type": "string", "description": "项目名称/备注子串，可省略。"},
                "includeArchived": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "cursor": {"type": "string"},
            }),
        },
        {
            "name": "whatsticks_search_traces",
            "description": "在痕迹正文和预览中进行大小写无关的普通子串搜索，支持项目和 UTC 时间过滤。",
            "inputSchema": schema({
                "query": {"type": "string"},
                "projectID": {"type": "string"},
                "from": {"type": "string", "description": "YYYY-MM-DD、YYYYMMDD 或 ISO 8601 UTC 时间。"},
                "to": {"type": "string", "description": "YYYY-MM-DD、YYYYMMDD 或 ISO 8601 UTC 时间。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "cursor": {"type": "string"},
            }, ["query"]),
        },
        {
            "name": "whatsticks_get_trace",
            "description": "按 ID 读取一条痕迹的完整正文和媒体关联元数据，不返回图片二进制。",
            "inputSchema": schema({"traceID": {"type": "string"}}, ["traceID"]),
        },
        {
            "name": "whatsticks_project_timeline",
            "description": "读取指定项目的痕迹与活动事件时间线，支持 UTC 时间过滤和分页。",
            "inputSchema": schema({
                "projectID": {"type": "string"},
                "from": {"type": "string"},
                "to": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "cursor": {"type": "string"},
            }, ["projectID"]),
        },
        {
            "name": "whatsticks_daily_stats",
            "description": "按本地日历 dayKey（YYYYMMDD）统计日课最终完成状态；不会把多次切换计为多次完成。",
            "inputSchema": schema({
                "from": {"type": "string", "description": "YYYY-MM-DD 或 YYYYMMDD。"},
                "to": {"type": "string", "description": "YYYY-MM-DD 或 YYYYMMDD。"},
                "practiceID": {"type": "string"},
                "includeArchived": {"type": "boolean"},
            }, ["from", "to"]),
        },
        {
            "name": "whatsticks_list_daily_practices",
            "description": "列出日课定义和关联项目/共享链接入口。",
            "inputSchema": schema({"includeArchived": {"type": "boolean"}}),
        },
    ]


class ToolService:
    def __init__(self, repository: MirrorRepository):
        self.repository = repository

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "whatsticks_status": self.status,
            "whatsticks_list_projects": self.list_projects,
            "whatsticks_search_traces": self.search_traces,
            "whatsticks_get_trace": self.get_trace,
            "whatsticks_project_timeline": self.project_timeline,
            "whatsticks_daily_stats": self.daily_stats,
            "whatsticks_list_daily_practices": self.list_daily_practices,
        }
        if name not in handlers:
            raise MCPError(f"未知工具：{name}")
        return handlers[name](arguments)

    def status(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return self.repository.status()

    def list_projects(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_map, traces = project_records(self.repository)
        active_counts: dict[str, int] = {}
        for trace in traces:
            if not is_deleted(trace):
                project_id = str(trace.get("projectID"))
                active_counts[project_id] = active_counts.get(project_id, 0) + 1
        query = str(arguments.get("query", "")).casefold().strip()
        include_archived = bool(arguments.get("includeArchived", False))
        projects = []
        for project in project_map.values():
            if is_deleted(project) or (project.get("archivedAt") and not include_archived):
                continue
            searchable = f"{project.get('title', '')}\n{project.get('note') or ''}".casefold()
            if query and query not in searchable:
                continue
            projects.append(project_output(project, active_counts.get(str(project.get("id")), 0)))
        projects.sort(key=lambda item: (str(item.get("updatedAt") or ""), str(item.get("id") or "")), reverse=True)
        page, next_cursor = paginate(projects, arguments)
        return {"projects": page, "nextCursor": next_cursor}

    def search_traces(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", "")).casefold().strip()
        if not query:
            raise MCPError("query 不能为空。")
        project_map, _ = project_records(self.repository)
        project_id = arguments.get("projectID")
        from_value = parse_datetime(arguments.get("from"))
        to_value = parse_datetime(arguments.get("to"), end_of_day=True)
        matches = []
        for trace in self.repository.records("traces"):
            project = project_map.get(str(trace.get("projectID")))
            if is_deleted(trace) or project is None or is_deleted(project):
                continue
            if project_id and str(trace.get("projectID")) != str(project_id):
                continue
            created = record_datetime(trace)
            if from_value and (created is None or created < from_value):
                continue
            if to_value and (created is None or created > to_value):
                continue
            haystack = f"{trace.get('text', '')}\n{trace.get('textPreview', '')}\n{project.get('title', '')}".casefold()
            if query not in haystack:
                continue
            matches.append(trace_output(trace, project))
        matches.sort(key=lambda item: (str(item.get("createdAt") or ""), str(item.get("id") or "")), reverse=True)
        page, next_cursor = paginate(matches, arguments)
        return {
            "query": query,
            "timeSemantics": "from/to are interpreted as UTC timestamps or UTC calendar-day bounds.",
            "traces": page,
            "nextCursor": next_cursor,
        }

    def get_trace(self, arguments: dict[str, Any]) -> dict[str, Any]:
        trace_id = str(arguments.get("traceID", "")).strip()
        if not trace_id:
            raise MCPError("traceID 不能为空。")
        project_map, _ = project_records(self.repository)
        trace = next((item for item in self.repository.records("traces") if str(item.get("id")) == trace_id), None)
        if trace is None:
            raise MCPError("找不到指定痕迹。")
        project = project_map.get(str(trace.get("projectID")))
        output = trace_output(trace, project, include_text=True)
        asset_id = trace.get("primaryAssetID")
        if asset_id:
            asset = next((item for item in self.repository.records("mediaAssets") if str(item.get("id")) == str(asset_id)), None)
            media_item = next((item for item in self.repository.load().release.get("media", []) if item.get("assetID") == str(asset_id).lower()), None)
            output["media"] = {
                "assetID": asset_id,
                "available": media_item is not None,
                "contentType": asset.get("contentType") if asset else None,
                "width": asset.get("width") if asset else None,
                "height": asset.get("height") if asset else None,
                "sha256": media_item.get("sha256") if media_item else None,
                "sizeBytes": media_item.get("sizeBytes") if media_item else None,
            }
        else:
            output["media"] = None
        return output

    def project_timeline(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = str(arguments.get("projectID", "")).strip()
        if not project_id:
            raise MCPError("projectID 不能为空。")
        project_map, _ = project_records(self.repository)
        project = project_map.get(project_id)
        if project is None or is_deleted(project):
            raise MCPError("找不到指定项目，或项目已删除。")
        from_value = parse_datetime(arguments.get("from"))
        to_value = parse_datetime(arguments.get("to"), end_of_day=True)
        items: list[dict[str, Any]] = []
        for trace in self.repository.records("traces"):
            if str(trace.get("projectID")) != project_id or is_deleted(trace):
                continue
            created = record_datetime(trace)
            if from_value and (created is None or created < from_value):
                continue
            if to_value and (created is None or created > to_value):
                continue
            item = trace_output(trace, project)
            item["kind"] = "trace"
            item["occurredAt"] = trace.get("createdAt")
            items.append(item)
        metrics = {str(metric.get("id")): metric for metric in self.repository.records("activityMetrics")}
        for event in self.repository.records("activityEvents"):
            if str(event.get("projectID")) != project_id or is_deleted(event):
                continue
            occurred = record_datetime(event, "occurredAt")
            if from_value and (occurred is None or occurred < from_value):
                continue
            if to_value and (occurred is None or occurred > to_value):
                continue
            metric = metrics.get(str(event.get("metricID")))
            items.append({
                "kind": "activity",
                "id": event.get("id"),
                "projectID": project_id,
                "metricID": event.get("metricID"),
                "metricName": metric.get("name") if metric else None,
                "valueBase": event.get("valueBase"),
                "occurredAt": event.get("occurredAt"),
                "dayKey": event.get("dayKey"),
            })
        items.sort(key=lambda item: (str(item.get("occurredAt") or ""), str(item.get("id") or "")), reverse=True)
        page, next_cursor = paginate(items, arguments)
        return {
            "project": project_output(project, sum(1 for item in items if item.get("kind") == "trace")),
            "timeSemantics": "from/to are interpreted as UTC timestamps or UTC calendar-day bounds.",
            "items": page,
            "nextCursor": next_cursor,
        }

    def list_daily_practices(self, arguments: dict[str, Any]) -> dict[str, Any]:
        include_archived = bool(arguments.get("includeArchived", False))
        project_map, _ = project_records(self.repository)
        links = {str(item.get("id")): item for item in self.repository.records("sharedLinks")}
        entries_by_practice: dict[str, list[dict[str, Any]]] = {}
        for entry in self.repository.records("dailyPracticeEntries"):
            target_id = str(entry.get("targetID"))
            kind = entry.get("kindRawValue")
            target = project_map.get(target_id) if kind == "project" else links.get(target_id)
            entries_by_practice.setdefault(str(entry.get("practiceID")), []).append({
                "id": entry.get("id"),
                "kind": kind,
                "targetID": entry.get("targetID"),
                "targetName": target.get("title") if target else None,
                "sortOrder": entry.get("sortOrder"),
            })
        practices = []
        for practice in self.repository.records("dailyPractices"):
            if practice.get("archivedAt") and not include_archived:
                continue
            practice_id = str(practice.get("id"))
            practices.append({
                "id": practice.get("id"),
                "title": practice.get("title"),
                "note": practice.get("note"),
                "sortOrder": practice.get("sortOrder"),
                "archivedAt": practice.get("archivedAt"),
                "updatedAt": practice.get("updatedAt"),
                "entries": sorted(entries_by_practice.get(practice_id, []), key=lambda item: item.get("sortOrder", 0)),
            })
        practices.sort(key=lambda item: (item.get("sortOrder", 0), str(item.get("id") or "")))
        return {"practices": practices}

    def daily_stats(self, arguments: dict[str, Any]) -> dict[str, Any]:
        start = parse_datetime(arguments.get("from"))
        end = parse_datetime(arguments.get("to"), end_of_day=True)
        if start is None or end is None or start > end:
            raise MCPError("日课统计需要有效的 from/to 日期范围。")
        start_day = start.date()
        end_day = end.date()
        start_key = day_key_from_date(start_day)
        end_key = day_key_from_date(end_day)
        include_archived = bool(arguments.get("includeArchived", False))
        requested_practice = arguments.get("practiceID")
        practices = [
            practice for practice in self.repository.records("dailyPractices")
            if (not practice.get("archivedAt") or include_archived)
            and (not requested_practice or str(practice.get("id")) == str(requested_practice))
        ]
        if requested_practice and not practices:
            raise MCPError("找不到指定日课，或日课已归档且 includeArchived=false。")
        days_by_practice: dict[str, dict[int, dict[str, Any]]] = {}
        for day in self.repository.records("dailyPracticeDays"):
            day_key = int(day.get("dayKey", 0))
            if start_key <= day_key <= end_key:
                days_by_practice.setdefault(str(day.get("practiceID")), {})[day_key] = day
        denominator = (end_day - start_day).days + 1
        summaries = []
        for practice in practices:
            practice_id = str(practice.get("id"))
            final_days = days_by_practice.get(practice_id, {})
            completed_keys = {key for key, value in final_days.items() if bool(value.get("isCompleted"))}
            by_date = []
            cursor = start_day
            while cursor <= end_day:
                key = day_key_from_date(cursor)
                record = final_days.get(key)
                by_date.append({
                    "dayKey": key,
                    "date": cursor.isoformat(),
                    "isCompleted": bool(record and record.get("isCompleted")),
                    "completedAt": record.get("completedAt") if record else None,
                    "recordID": record.get("id") if record else None,
                })
                cursor += dt.timedelta(days=1)
            summaries.append({
                "id": practice.get("id"),
                "title": practice.get("title"),
                "archivedAt": practice.get("archivedAt"),
                "completedDays": len(completed_keys),
                "denominatorDays": denominator,
                "completionRate": len(completed_keys) / denominator if denominator else 0,
                "byDate": by_date,
            })
        return {
            "from": start_day.isoformat(),
            "to": end_day.isoformat(),
            "dayKeySemantics": "按设备业务日历的 YYYYMMDD dayKey 统计；每个 practiceID+dayKey 只取最终 isCompleted 状态，切换次数不计数。",
            "practices": summaries,
        }


class MCPServer:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.repository: MirrorRepository | None = None
        self.repository_error: str | None = None

    def _service(self) -> ToolService:
        if self.repository is None:
            try:
                config = ServerConfig.from_file(self.config_path)
                self.repository = MirrorRepository(config, credential_password(config))
                self.repository_error = None
            except MCPError as exc:
                self.repository_error = str(exc)
                raise
        return ToolService(self.repository)

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if method == "notifications/initialized" or method == "notifications/cancelled":
            return None
        if method == "initialize":
            requested = str(request.get("params", {}).get("protocolVersion", DEFAULT_PROTOCOL_VERSION))
            protocol_version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "whatsticks", "version": "1.0.0"},
                    "instructions": "只读读取 WhatSticks WebDAV 镜像；镜像缓存可能滞后并会在 status 中说明。",
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_definitions()}}
        if method == "tools/call":
            params = request.get("params") or {}
            name = str(params.get("name", ""))
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                result = self._service().call(name, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                        "structuredContent": result,
                        "isError": False,
                    },
                }
            except MCPError as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def run(self) -> int:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise MCPError("MCP 请求必须是 JSON 对象。")
                response = self.handle(request)
                if response is not None:
                    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                    sys.stdout.flush()
            except json.JSONDecodeError as exc:
                sys.stdout.write(json.dumps({
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Invalid JSON"},
                }) + "\n")
                sys.stdout.flush()
                print(f"invalid JSON request: {exc}", file=sys.stderr)
            except MCPError as exc:
                sys.stdout.write(json.dumps({
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32000, "message": str(exc)},
                }, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        return 0


def default_config_path() -> Path:
    value = os.environ.get("WHATSTICKS_MCP_CONFIG")
    if value:
        return Path(value).expanduser()
    if sys.platform == "darwin":
        return Path("~/Library/Application Support/WhatSticks/mcp.json").expanduser()
    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        base = Path(app_data) if app_data else Path.home() / "AppData" / "Roaming"
        return base / "WhatSticks" / "mcp.json"
    return Path("~/.config/WhatSticks/mcp.json").expanduser()


def _store_macos_password(service: str, username: str, password: str) -> None:
    try:
        result = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service, "-a", username, "-w"],
            input=f"{password}\n",
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MCPError("无法访问 macOS Keychain，请检查系统权限。") from exc
    if result.returncode != 0:
        raise MCPError("Keychain 凭据写入失败，请检查系统权限。")


def _store_windows_password(service: str, username: str, password: str) -> None:
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    password_bytes = password.encode("utf-8")
    password_buffer = ctypes.create_string_buffer(password_bytes)
    credential = CREDENTIAL()
    credential.Type = 1  # CRED_TYPE_GENERIC
    credential.TargetName = _credential_target(service, username)
    credential.CredentialBlobSize = len(password_bytes)
    credential.CredentialBlob = ctypes.cast(password_buffer, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = username

    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_write = advapi32.CredWriteW
    cred_write.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    cred_write.restype = wintypes.BOOL
    if not cred_write(ctypes.byref(credential), 0):
        raise MCPError("Windows Credential Manager 凭据写入失败，请检查系统权限。")


def store_password(service: str, username: str, password: str) -> None:
    if sys.platform == "darwin":
        _store_macos_password(service, username, password)
        return
    if os.name == "nt":
        _store_windows_password(service, username, password)
        return
    raise MCPError("当前系统暂不支持安全凭据存储；v1.0.0 目前支持 macOS 和 Windows。")


def setup(
    config_path: Path,
    *,
    webdav_url: str | None = None,
    username: str | None = None,
    remote_folder: str | None = None,
    cache_dir: str | None = None,
) -> int:
    backend = credential_backend_name()
    if backend is None:
        raise MCPError("当前系统暂不支持安全凭据存储；v1.0.0 目前支持 macOS 和 Windows。")
    default_cache = default_cache_dir()
    print(f"WhatSticks MCP WebDAV 配置（密码只写入 {backend}）", file=sys.stderr)
    url = (
        webdav_url.strip()
        if webdav_url is not None
        else input("WebDAV HTTPS 地址 [https://dav.jianguoyun.com/dav/]: ").strip()
        or "https://dav.jianguoyun.com/dav/"
    )
    username = username.strip() if username is not None else input("邮箱或用户名: ").strip()
    folder = (
        remote_folder.strip()
        if remote_folder is not None
        else input("远端目录 [WhatSticks]: ").strip() or "WhatSticks"
    )
    cache_value = (
        cache_dir.strip()
        if cache_dir is not None
        else input(f"本地缓存目录 [{default_cache}]: ").strip() or str(default_cache)
    )
    password = getpass.getpass("第三方应用密码（不会写入配置文件）: ")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
        raise MCPError("地址必须是没有用户名/密码的 HTTPS 地址。")
    if not username:
        raise MCPError("用户名不能为空。")
    folder = normalized_folder(folder)
    if not cache_value:
        raise MCPError("本地缓存目录不能为空。")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "webdavUrl": url,
        "username": username,
        "remoteFolder": folder,
        "cacheDir": cache_value,
        "credentialService": "com.dusk.WhatSticks.webdav",
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    store_password(config["credentialService"], username, password)
    print(f"已写入配置：{config_path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WhatSticks read-only WebDAV MCP server")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--setup", action="store_true", help="交互式写入配置和当前系统的安全凭据")
    parser.add_argument(
        "--webdav-url",
        help="setup 使用的 HTTPS WebDAV 地址；提供后不再重复询问",
    )
    parser.add_argument(
        "--username",
        help="setup 使用的 WebDAV 用户名；提供后不再重复询问（不是密码）",
    )
    parser.add_argument(
        "--remote-folder",
        help="setup 使用的远端目录；提供后不再重复询问",
    )
    parser.add_argument(
        "--cache-dir",
        help="setup 使用的本地缓存目录；提供后不再重复询问",
    )
    args = parser.parse_args(argv)
    try:
        if args.setup:
            return setup(
                args.config.expanduser(),
                webdav_url=args.webdav_url,
                username=args.username,
                remote_folder=args.remote_folder,
                cache_dir=args.cache_dir,
            )
        return MCPServer(args.config.expanduser()).run()
    except (MCPError, EOFError, KeyboardInterrupt) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
