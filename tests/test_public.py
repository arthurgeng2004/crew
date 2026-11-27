"""Run with HOME / CREW_HOME / CREW_TEST_ROOT pointing at a disposable sandbox."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from crewlib import core, mcp, mcp_bridge, platform, wf
from crewlib.cli import make_task
from crewlib.clauderun import quota_held
from crewlib.codexrun import CodexRun
from crewlib.worker import Worker

FIXTURE = Path(__file__).with_name("mcp_fixture.py")
spec = importlib.util.spec_from_file_location("fixture", FIXTURE)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class PublicInterfaces(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["CREW_TEST_ROOT"])
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(mcp_bridge.close)
        mcp._cache.update(t=0, servers={}, errors={})
        mcp_bridge._SESSION.clear()
        mcp_bridge._INSTRUCTIONS.clear()
        self.cfg = {"mcp_servers": {"demo": {"command": sys.executable, "args": [str(FIXTURE)]}}}

    def test_no_implicit_services(self):
        with patch("subprocess.Popen", side_effect=AssertionError("must not spawn")):
            self.assertEqual(mcp_bridge.servers({"mcp_servers": {}}), {})
            self.assertFalse(mcp.available({"mcp_servers": {}}))
            _, error = mcp_bridge._rpc("unconfigured", "tools/list", {}, 1, {"mcp_servers": {}})
            self.assertIn("没有配置", error)
        with patch.dict(os.environ, {"MCP_URL_DEMO": "https://example.invalid/override"}):
            explicit = {"mcp_servers": {"demo": {"url": "https://example.invalid/explicit"}}}
            self.assertEqual(mcp_bridge.servers(explicit)["demo"]["url"], explicit["mcp_servers"]["demo"]["url"])

    def test_stdio_catalog_call_description_and_images(self):
        text = mcp.brief(self.cfg)
        self.assertIn("Fixture instructions", text)
        self.assertIn("echo", text)
        self.assertIn("缺必填参数", mcp.check_args(self.cfg, "demo", "echo", {}))
        value, error, _ = mcp.call(self.cfg, "demo", "echo", {"message": "hello"})
        self.assertIsNone(error)
        self.assertEqual(value, {"message": "hello"})
        self.cfg["mcp_servers"]["demo"]["description"] = "Configured description"
        self.assertEqual(mcp_bridge.description("demo", self.cfg), "Configured description")
        _, error, paths = mcp.call(self.cfg, "demo", "image", {}, img_dir=self.temp.name)
        self.assertIsNone(error)
        self.assertEqual(len(paths), 1)
        self.assertEqual(Path(paths[0]).read_bytes(), b"fixture-image")
        _, error, _ = mcp.call(self.cfg, "demo", "error", {})
        self.assertEqual(error, "Fixture tool error")

    def test_http_stateless_handshake_and_configured_description(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(request["method"])
                result = fixture.reply(request)
                body = json.dumps(result).encode() if result else b""
                self.send_response(200 if result else 202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        cfg = {"mcp_servers": {"http-demo": {"url": f"http://127.0.0.1:{server.server_port}/mcp", "description": "HTTP demo"}}}
        self.assertIn("HTTP demo", mcp.brief(cfg))
        value, error, _ = mcp.call(cfg, "http-demo", "echo", {"message": "http"})
        self.assertEqual(value, {"message": "http"})
        self.assertIsNone(error)
        self.assertEqual(seen[:3], ["initialize", "notifications/initialized", "tools/list"])

    def test_models_unrestricted_and_no_default_override(self):
        cfg = core.deep_merge(core.DEFAULT_CONFIG, {"profiles": {"gpt": {"models": ["unused-model"]}}})
        for name in ("user/custom:model", "sonnet", "gpt"):
            task = make_task({"prompt": "p", "model": name, "cwd": self.temp.name}, cfg, "test")
            self.assertEqual(task["model"], name)
            worker = Worker.__new__(Worker)
            worker.d = SimpleNamespace(tasks={})
            worker.tid, worker.task, worker.prof, worker.cfg = "x", task, cfg["profiles"]["gpt"], cfg
            self.assertEqual(worker.model_params()[0], name)
        worker.task = make_task({"prompt": "p", "cwd": self.temp.name}, cfg, "test")
        self.assertIsNone(worker.model_params()[0])
        worker.dir = self.temp.name
        runner = CodexRun(worker)
        self.assertEqual(runner.overrides()[0:2], ["codex", "app-server"])
        self.assertFalse(any("model_provider=" in arg or "model=" in arg for arg in runner.overrides()))

    def test_claude_missing_quota_is_not_held(self):
        with patch("crewlib.clauderun.QUOTA", str(Path(self.temp.name) / "absent.json")):
            self.assertEqual(quota_held({"quota_stop": .9}), "")

    def test_darwin_identity_rejects_reused_pid_and_zombie(self):
        answers = ["Fri Nov 27 10:00:00 2026", f"node {wf.RUNNER} run demo", "S"]
        with patch.object(sys, "platform", "darwin"), patch("subprocess.check_output", side_effect=answers) as ps:
            info = platform.process_info(123)
            self.assertEqual(info[0], answers[0])
            self.assertEqual([x.args[0][2] for x in ps.call_args_list], ["lstart=", "command=", "stat="])
        with patch.object(platform, "process_info", return_value=tuple(answers)):
            self.assertTrue(platform.session_alive(123, answers[0]))
            self.assertFalse(platform.session_alive(123, "different-process"))
            with patch("os.waitpid", side_effect=ChildProcessError):
                self.assertTrue(wf.pid_alive(123, answers[0]))
                self.assertFalse(wf.pid_alive(123, "different-process"))
        with patch.object(platform, "process_info", return_value=(answers[0], answers[1], "Z")):
            self.assertFalse(platform.session_alive(123, answers[0]))

    def test_claude_long_socket_path_is_portable_and_cleaned(self):
        from crewlib import claudemcp
        from crewlib.clauderun import ClaudeRun
        taskdir = Path(self.temp.name) / ("long-" * 15)
        taskdir.mkdir()
        run = ClaudeRun.__new__(ClaudeRun)
        run.w = SimpleNamespace(dir=str(taskdir), tid="test")
        run.sock = run.sock_path = run.sock_dir = run.proc = None
        run.lock = threading.Lock()
        run.dyn_call = lambda name, args: ("echo:" + args["message"], True)
        with patch("tempfile.gettempdir", return_value=os.environ["CREW_TEST_ROOT"]):
            run.serve_tools()
        self.addCleanup(run.close)
        self.assertLessEqual(len(os.fsencode(run.sock_path)), 100)
        self.assertFalse(run.sock_path.startswith("@"))
        self.assertEqual(claudemcp.call(run.sock_path, "echo", {"message": "hello"}), ("echo:hello", True))
        tempdir = Path(run.sock_dir)
        run.close()
        self.assertFalse(tempdir.exists())

    def test_tagged_alert_and_mock_worker_team_tools(self):
        hits = core.scan_alerts("[CREW-ALERT:fixture] example failure", core.DEFAULT_CONFIG, code=0)
        self.assertEqual(hits[0][0], "fixture")
        from crewlib import team
        self.assertEqual(set(team.NAMES), {"team_list", "team_read", "team_post"})


if __name__ == "__main__":
    unittest.main()
