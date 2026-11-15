"""Integrity checks for the public candidate documentation."""

import os
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DOCUMENTS = [
    ROOT / "README.md",
    ROOT / "LICENSE",
    ROOT / "THIRD_PARTY_NOTICES.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "CODE_OF_CONDUCT.md",
    ROOT / "CHANGELOG.md",
    ROOT / "docs/ARCHITECTURE.md",
    ROOT / "docs/INSTALLATION.md",
    ROOT / "docs/LIMITATIONS.md",
    ROOT / "docs/ROADMAP.md",
    ROOT / "docs/DEMO.md",
    ROOT / "tests/assets/README.md",
]


class PublicDocumentationTests(SimpleTestCase):
    def test_test_settings_use_a_separate_test_role(self):
        self.assertEqual(settings.DATABASES["default"]["USER"], settings.TEST_DATABASE_USER)
        self.assertNotEqual(settings.TEST_DATABASE_USER, settings.APPLICATION_DATABASE_USER)
        self.assertNotEqual(settings.TEST_DATABASE_USER, settings.APPLICATION_RUNTIME_DATABASE_USER)
        self.assertEqual(os.environ["DATABASE_USER"], settings.TEST_DATABASE_USER)

    def test_public_documents_exist_and_internal_links_resolve(self):
        for path in PUBLIC_DOCUMENTS:
            with self.subTest(document=path.name):
                self.assertTrue(path.is_file())
                text = path.read_text()
                self.assertTrue(text.strip())
                self.assertNotIn("N" + "+1", text)
                for target in re.findall(r"\]\(([^)]+)\)", text):
                    if target.startswith(("https://", "http://", "#")):
                        continue
                    destination = target.split("#", 1)[0]
                    if destination:
                        self.assertTrue(
                            (path.parent / destination).exists(), f"{path.name}: {target}"
                        )

    def test_mit_scope_and_third_party_notices_are_explicit(self):
        license_text = (ROOT / "LICENSE").read_text()
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
        readme = (ROOT / "README.md").read_text()
        asset_notes = (ROOT / "tests/assets/README.md").read_text()
        self.assertIn("MIT License", license_text)
        self.assertIn("Copyright (c) 2026 Ilias El Baghdadi", license_text)
        self.assertNotIn("Scope and third-party materials", license_text)
        self.assertTrue(license_text.rstrip().endswith("SOFTWARE."))
        self.assertIn("LUCIDE-LICENSE.txt", notices)
        self.assertIn("AXE-LICENSE.txt", notices)
        self.assertIn("AXE-THIRD-PARTY-LICENSES.txt", notices)
        self.assertIn("AXE-THIRD-PARTY-LICENSES.txt", asset_notes)
        self.assertIn(
            "project code identified in [`LICENSE`](LICENSE) is offered under the MIT License",
            readme,
        )
        self.assertIn("Third-party components retain their own licences", readme)
        self.assertIn("The project licence does not replace third-party terms", readme)
