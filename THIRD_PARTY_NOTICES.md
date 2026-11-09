# Third-party notices

This file records notices for third-party materials included in this candidate.
It does not relicense them under the project MIT license.

| Material | Location | License / notice |
|---|---|---|
| Lucide Static 1.61.0 icon sprite (24 selected symbols) | static/ui/icons.svg | ISC; the upstream package license is preserved in static/ui/LUCIDE-LICENSE.txt, including its MIT notice for Feather-derived portions. Reproducible source archive SHA-256: `62773cd2ee411ad2e37c1973d1e4c4b49f9a6c57da6b5d5d0abca08756ba75f1`; regenerate with `python3 scripts/build_icon_sprite.py /path/to/lucide-static-1.61.0.tgz`. |
| axe-core 4.10.3 test bundle | tests/assets/axe.min.js | MPL-2.0 in tests/assets/AXE-LICENSE.txt; bundled third-party MIT and ISC notices are in tests/assets/AXE-THIRD-PARTY-LICENSES.txt. Used only by browser tests. |

The exact Python dependencies are pinned in requirements.txt and requirements-dev.txt and installed separately; their source is not vendored in this repository. Check the license metadata and notices for the exact distributions before producing a packaged build or redistributing dependency files. Development-only direct dependencies are listed in requirements-dev.in.
