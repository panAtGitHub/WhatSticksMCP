from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


class FixtureReader:
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def get(self, relative_path: str) -> bytes:
        try:
            return self.files[relative_path]
        except KeyError as exc:
            raise server.NotFoundError(f"missing fixture: {relative_path}") from exc


class FailingReader:
    def get(self, _relative_path: str) -> bytes:
        raise server.MCPError("fixture network down")


def make_fixture() -> dict[str, bytes]:
    project_id = "00000000-0000-0000-0000-000000000001"
    trace_id = "00000000-0000-0000-0000-000000000002"
    practice_id = "00000000-0000-0000-0000-000000000003"
    records = {
        "projects": [{
            "id": project_id,
            "title": "阅读与学习",
            "note": "中文 MCP 测试",
            "updatedAt": "2026-10-19T04:00:00.000Z",
            "archivedAt": None,
            "deletedAt": None,
        }],
        "traces": [{
            "id": trace_id,
            "projectID": project_id,
            "text": "WS-MCP-20261019 今天完成中文检索验收",
            "textPreview": "WS-MCP-20261019 今天完成中文检索验收",
            "contentKindRawValue": "text",
            "createdAt": "2026-10-19T03:00:00.000Z",
            "updatedAt": "2026-10-19T03:00:00.000Z",
            "primaryAssetID": None,
            "deletedAt": None,
        }],
        "dailyPractices": [{
            "id": practice_id,
            "title": "每日阅读",
            "note": "最终状态统计",
            "sortOrder": 0,
            "archivedAt": None,
            "updatedAt": "2026-10-19T04:00:00.000Z",
        }],
        "dailyPracticeDays": [
            {"id": "00000000-0000-0000-0000-000000000004", "practiceID": practice_id, "dayKey": 20261018, "isCompleted": True, "completedAt": "2026-10-18T03:00:00.000Z"},
            {"id": "00000000-0000-0000-0000-000000000005", "practiceID": practice_id, "dayKey": 20261019, "isCompleted": False, "completedAt": None},
        ],
        "dailyPracticeEntries": [],
        "mediaAssets": [],
        "activityMetrics": [],
        "activityEvents": [],
        "sharedLinks": [],
    }
    files: dict[str, bytes] = {}
    partitions = []
    for name, values in sorted(records.items()):
        envelope = {
            "formatVersion": 1,
            "logicalPartition": name,
            "records": values,
        }
        data = server.json_bytes(envelope)
        digest = server.sha256(data)
        path = f"data/{name}/{digest}.json"
        files[path] = data
        partitions.append({
            "logicalPartition": name,
            "path": path,
            "sha256": digest,
            "sizeBytes": len(data),
            "recordCount": len(values),
        })
    release = {
        "formatVersion": 1,
        "schemaIdentifier": server.SCHEMA_IDENTIFIER,
        "generationID": "fixture-generation",
        "createdAt": "2026-10-19T04:00:00.000Z",
        "sourceAppVersion": "fixture",
        "partitions": partitions,
        "entityCounts": {name: len(values) for name, values in records.items()},
        "media": [],
    }
    release_data = server.json_bytes(release)
    release_hash = server.sha256(release_data)
    release_path = "releases/fixture-generation.json"
    files[release_path] = release_data
    current = {
        "formatVersion": 1,
        "generationID": "fixture-generation",
        "releasePath": release_path,
        "publishedAt": "2026-10-19T04:00:00.000Z",
        "deviceID": "fixture-device",
        "releaseSHA256": release_hash,
    }
    files["current.json"] = server.json_bytes(current)
    return files


class WhatSticksMCPTests(unittest.TestCase):
    def make_repository(self) -> tuple[server.MirrorRepository, tempfile.TemporaryDirectory[str]]:
        cache = tempfile.TemporaryDirectory()
        config = server.ServerConfig(
            webdav_url="https://fixture.invalid/dav/",
            username="fixture@example.com",
            remote_folder="WhatSticks",
            cache_dir=Path(cache.name),
            credential_service="fixture",
        )
        repository = server.MirrorRepository(config, "not-used")
        repository.reader = FixtureReader(make_fixture())  # type: ignore[assignment]
        return repository, cache

    def test_fixture_loads_chinese_search_and_daily_final_state(self) -> None:
        repository, cache = self.make_repository()
        self.addCleanup(cache.cleanup)
        service = server.ToolService(repository)

        result = service.search_traces({"query": "WS-MCP-20261019"})
        self.assertEqual(len(result["traces"]), 1)
        self.assertEqual(result["traces"][0]["projectTitle"], "阅读与学习")

        stats = service.daily_stats({"from": "20261018", "to": "20261019"})
        self.assertEqual(stats["practices"][0]["completedDays"], 1)
        self.assertEqual(stats["practices"][0]["denominatorDays"], 2)

    def test_stdio_initialize_tools_list_and_tool_call(self) -> None:
        repository, cache = self.make_repository()
        self.addCleanup(cache.cleanup)
        mcp = server.MCPServer(Path("/unused/config.json"))
        mcp.repository = repository
        requests = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "whatsticks_search_traces", "arguments": {"query": "中文"}}}),
        ]) + "\n"
        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(requests), io.StringIO()
        try:
            self.assertEqual(mcp.run(), 0)
            output = sys.stdout.getvalue().splitlines()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
        responses = [json.loads(line) for line in output]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "whatsticks")
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], server.SERVER_VERSION)
        names = {item["name"] for item in responses[1]["result"]["tools"]}
        self.assertIn("whatsticks_daily_stats", names)
        self.assertFalse(responses[2]["result"]["isError"])
        self.assertIn("WS-MCP-20261019", responses[2]["result"]["structuredContent"]["traces"][0]["textPreview"])

    def test_invalid_remote_falls_back_to_complete_cache(self) -> None:
        repository, cache = self.make_repository()
        self.addCleanup(cache.cleanup)
        first = repository.load()
        self.assertFalse(first.stale)
        repository.reader = FailingReader()  # type: ignore[assignment]
        repository._snapshot = None
        fallback = repository.load()
        self.assertTrue(fallback.stale)
        self.assertEqual(fallback.current["generationID"], "fixture-generation")

    def test_config_uses_platform_neutral_credential_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            path.write_text(json.dumps({
                "webdavUrl": "https://dav.example.com/dav/",
                "username": "user@example.com",
                "remoteFolder": "WhatSticks",
                "cacheDir": directory,
                "credentialService": "com.example.whatsticks",
            }), encoding="utf-8")
            config = server.ServerConfig.from_file(path)
        self.assertEqual(config.credential_service, "com.example.whatsticks")

    def test_unsupported_platform_reports_secure_store_requirement(self) -> None:
        config = server.ServerConfig(
            webdav_url="https://fixture.invalid/dav/",
            username="fixture@example.com",
            remote_folder="WhatSticks",
            cache_dir=Path("/tmp/whatsticks-test-cache"),
            credential_service="fixture",
        )
        with patch.object(server.sys, "platform", "linux"), patch.object(server.os, "name", "posix"):
            with self.assertRaisesRegex(server.MCPError, "支持 macOS 和 Windows"):
                server.credential_password(config)

    def test_setup_accepts_non_sensitive_values_without_reprompting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "mcp.json"
            with patch.object(server, "credential_backend_name", return_value="test secure store"), \
                    patch.object(server.getpass, "getpass", return_value="secret"), \
                    patch.object(server, "store_password") as store_password, \
                    patch("builtins.input") as input_function:
                result = server.main([
                    "--setup",
                    "--config",
                    str(config_path),
                    "--webdav-url",
                    "https://dav.example.com/dav/",
                    "--username",
                    "user@example.com",
                    "--remote-folder",
                    "WhatSticks",
                    "--cache-dir",
                    str(Path(directory) / "cache"),
                ])

            self.assertEqual(result, 0)
            input_function.assert_not_called()
            store_password.assert_called_once_with(
                "com.dusk.WhatSticks.webdav",
                "user@example.com",
                "secret",
            )
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["webdavUrl"], "https://dav.example.com/dav/")
            self.assertEqual(saved["username"], "user@example.com")
            self.assertEqual(saved["remoteFolder"], "WhatSticks")
            self.assertNotIn("password", saved)

    def test_setup_help_exposes_only_non_sensitive_setup_arguments(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            with self.assertRaises(SystemExit) as exit_result:
                server.main(["--help"])

        self.assertEqual(exit_result.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("--webdav-url", help_text)
        self.assertIn("--username", help_text)
        self.assertIn("--remote-folder", help_text)
        self.assertIn("--cache-dir", help_text)
        self.assertNotIn("--password", help_text)

    def test_macos_keychain_receives_two_password_lines_without_password_in_argv(self) -> None:
        password = "secret-value"
        command_result = server.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch.object(server.subprocess, "run", return_value=command_result) as run:
            server._store_macos_password("com.example.service", "user@example.com", password)

        command = run.call_args.args[0]
        self.assertNotIn(password, command)
        self.assertEqual(command[-1], "-w")
        self.assertEqual(run.call_args.kwargs["input"], f"{password}\n{password}\n")
        self.assertTrue(run.call_args.kwargs["text"])

    def test_macos_keychain_input_failure_does_not_leak_password(self) -> None:
        password = "secret-value"
        command_result = server.subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="security: passwords do not match\n",
        )
        with patch.object(server.subprocess, "run", return_value=command_result):
            with self.assertRaisesRegex(server.MCPError, "用户输入失败") as raised:
                server._store_macos_password("com.example.service", "user@example.com", password)

        self.assertNotIn(password, str(raised.exception))

    def test_macos_keychain_permission_failure_is_distinct_and_does_not_leak_password(self) -> None:
        password = "secret-value"
        command_result = server.subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="security: User interaction is not allowed.\n",
        )
        with patch.object(server.subprocess, "run", return_value=command_result):
            with self.assertRaisesRegex(server.MCPError, "权限问题") as raised:
                server._store_macos_password("com.example.service", "user@example.com", password)

        self.assertNotIn(password, str(raised.exception))

    def test_setup_keychain_failure_does_not_leave_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "mcp.json"
            with patch.object(server, "credential_backend_name", return_value="macOS Keychain"), \
                    patch.object(server.getpass, "getpass", return_value="secret-value"), \
                    patch.object(server, "store_password", side_effect=server.MCPError("权限问题：Keychain 拒绝写入")):
                with self.assertRaisesRegex(server.MCPError, "权限问题"):
                    server.setup(
                        config_path,
                        webdav_url="https://dav.example.com/dav/",
                        username="user@example.com",
                        remote_folder="WhatSticks",
                        cache_dir=str(Path(directory) / "cache"),
                    )

            self.assertFalse(config_path.exists())

    def test_config_replace_failure_preserves_previous_config_and_cleans_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "mcp.json"
            previous = '{"existing": true}\n'
            config_path.write_text(previous, encoding="utf-8")
            config = {"webdavUrl": "https://dav.example.com/dav/", "username": "user@example.com"}

            with patch.object(server.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(server.MCPError, "配置写入失败"):
                    server._write_config_atomically(config_path, config)

            self.assertEqual(config_path.read_text(encoding="utf-8"), previous)
            self.assertEqual(list(Path(directory).glob(".mcp.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
