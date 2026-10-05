import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from foia_archive.storage import get_connection, get_schema_version, init_db
from foia_archive.utils import load_config
from ui import server


async def asgi_get(app, path):
    messages = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    status = next(
        item["status"]
        for item in messages
        if item["type"] == "http.response.start"
    )
    body = b"".join(
        item.get("body", b"")
        for item in messages
        if item["type"] == "http.response.body"
    )
    return status, body.decode("utf-8")


class DeploymentConfigTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config_path = self.root / "settings.yaml"
        self.config_path.write_text(
            yaml.safe_dump(
                {
                    "storage": {
                        "backend": "local",
                        "db_path": "data/original.db",
                        "files_dir": "data/original-files",
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_storage_paths_and_backend_can_be_overridden_by_environment(self):
        with patch.dict(
            "os.environ",
            {
                "FOIA_DB_PATH": "/data/runtime.db",
                "FOIA_FILES_DIR": "/data/runtime-files",
                "FOIA_STORAGE_BACKEND": "b2",
                "FOIA_CRAWLER_DRY_RUN": "false",
                "FOIA_MAX_DOCS_PER_SOURCE": "3",
                "FOIA_CRAWLER_INTERVAL_HOURS": "4.5",
                "FOIA_SOURCE_LIMIT": "80",
                "FOIA_MAX_PAGES_PER_SOURCE": "4",
                "FOIA_MAX_DEPTH": "1",
            },
            clear=False,
        ):
            config = load_config(str(self.config_path))

        self.assertEqual(config.storage["db_path"], "/data/runtime.db")
        self.assertEqual(config.storage["files_dir"], "/data/runtime-files")
        self.assertEqual(config.storage["backend"], "b2")
        self.assertFalse(config.crawler["dry_run"])
        self.assertEqual(config.crawler["max_docs_per_source"], 3)
        self.assertEqual(config.crawler["interval_hours"], 4.5)
        self.assertEqual(config.crawler["source_limit"], 80)
        self.assertEqual(config.crawler["max_pages_per_source"], 4)
        self.assertEqual(config.crawler["max_depth"], 1)

    def test_blank_environment_values_do_not_replace_yaml_defaults(self):
        with patch.dict(
            "os.environ",
            {
                "FOIA_DB_PATH": "",
                "FOIA_FILES_DIR": "",
                "FOIA_STORAGE_BACKEND": "",
            },
            clear=False,
        ):
            config = load_config(str(self.config_path))

        self.assertEqual(config.storage["db_path"], "data/original.db")
        self.assertEqual(config.storage["files_dir"], "data/original-files")
        self.assertEqual(config.storage["backend"], "local")


class RenderBlueprintTests(unittest.TestCase):
    def test_free_render_blueprint_uses_b2_bootstrap_without_committed_secrets(self):
        blueprint = yaml.safe_load(
            Path("render.yaml").read_text(encoding="utf-8")
        )
        services = blueprint.get("services") or []
        self.assertEqual(len(services), 1)

        service = services[0]
        self.assertEqual(service["type"], "web")
        self.assertEqual(service["runtime"], "docker")
        self.assertEqual(service["plan"], "free")
        self.assertEqual(service["healthCheckPath"], "/healthz")

        env = {
            item["key"]: item
            for item in service.get("envVars") or []
            if "key" in item
        }
        self.assertEqual(env["FOIA_STORAGE_BACKEND"]["value"], "b2")
        self.assertEqual(
            env["FOIA_BOOTSTRAP_DB_FROM_B2"]["value"],
            "true",
        )
        self.assertFalse(env["B2_KEY_ID"]["sync"])
        self.assertFalse(env["B2_APPLICATION_KEY"]["sync"])
        self.assertNotIn("value", env["B2_KEY_ID"])
        self.assertNotIn("value", env["B2_APPLICATION_KEY"])


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "archive.db"
        self.files_dir = root / "files"
        init_db(self.db_path, self.files_dir)

    def tearDown(self):
        self.tempdir.cleanup()

    def get_db(self):
        return get_connection(self.db_path)

    def test_health_endpoint_checks_database_and_reports_schema(self):
        conn = self.get_db()
        try:
            expected_version = get_schema_version(conn)
        finally:
            conn.close()

        with patch("ui.server.get_db", side_effect=self.get_db):
            status, body = asyncio.run(asgi_get(server.app, "/healthz"))

        self.assertEqual(status, 200)
        self.assertIn('"status":"ok"', body)
        self.assertIn(f'"schema_version":{expected_version}', body)


if __name__ == "__main__":
    unittest.main()
