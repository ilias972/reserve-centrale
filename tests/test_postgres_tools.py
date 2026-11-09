"""PostgreSQL client tools must match the server used for recovery."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.core.postgres_tools import binary


class PostgreSQLToolsTests(SimpleTestCase):
    def _fake_tool(self, folder: Path, major: int) -> Path:
        tool = folder / "pg_dump"
        tool.write_text(f"#!/bin/sh{chr(10)}echo 'pg_dump (PostgreSQL) {major}.4'{chr(10)}")
        tool.chmod(0o700)
        return tool

    def test_matching_client_major_is_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            expected = self._fake_tool(folder, 14)
            with (
                patch.dict(os.environ, {"PG_BIN": str(folder)}),
                patch("apps.core.postgres_tools.shutil.which", return_value=None),
            ):
                self.assertEqual(binary("pg_dump", server_major=14), str(expected))

    def test_client_from_newer_major_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self._fake_tool(folder, 18)
            with (
                patch.dict(os.environ, {"PG_BIN": str(folder)}),
                patch("apps.core.postgres_tools.shutil.which", return_value=None),
            ):
                with self.assertRaisesRegex(FileNotFoundError, "version majeure PostgreSQL 14"):
                    binary("pg_dump", server_major=14)

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(ValueError):
            binary("psql", server_major=14)
