from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, override_settings
from django.utils import translation
from django.utils.translation import gettext

from apps.accounts.middleware import AccountSessionMiddleware
from apps.catalog.views import REFERENCES
from apps.core.i18n import set_language
from apps.core.middleware import ExplicitLanguageMiddleware
from apps.core.presentation import case_navigation
from apps.core.templatetags.presentation import ui_date, ui_datetime
from config.urls import javascript_catalog


class TemporaryPasswordI18nTests(SimpleTestCase):
    def test_language_assets_remain_available_during_required_password_change(self):
        from types import SimpleNamespace

        request = RequestFactory().get("/")
        request.user = SimpleNamespace(is_authenticated=True, must_change_password=True)
        middleware = AccountSessionMiddleware(lambda req: None)
        for view in (javascript_catalog, set_language):
            with self.subTest(view=view):
                self.assertTrue(view.m0_access == "public")
                self.assertTrue(view.temporary_access)
                self.assertIsNone(middleware.process_view(request, view, (), {}))


class ExplicitLanguageMiddlewareTests(SimpleTestCase):
    @override_settings(
        LANGUAGE_CODE="en",
        LANGUAGES=(("en", "English"), ("fr", "Français"), ("ar", "العربية")),
        LANGUAGE_COOKIE_NAME="reserve_language",
    )
    def test_new_visitors_default_to_english_even_with_arabic_browser(self):
        request = RequestFactory().get("/", HTTP_ACCEPT_LANGUAGE="ar")
        middleware = ExplicitLanguageMiddleware(
            lambda req: HttpResponse(translation.get_language())
        )
        response = middleware(request)
        self.assertEqual(response.content, b"en")
        self.assertEqual(response["Content-Language"], "en")

    @override_settings(
        LANGUAGE_CODE="en",
        LANGUAGES=(("en", "English"), ("fr", "Français"), ("ar", "العربية")),
        LANGUAGE_COOKIE_NAME="reserve_language",
    )
    def test_valid_explicit_choice_is_applied(self):
        request = RequestFactory().get("/", HTTP_ACCEPT_LANGUAGE="en")
        request.COOKIES["reserve_language"] = "ar"
        middleware = ExplicitLanguageMiddleware(
            lambda req: HttpResponse(translation.get_language())
        )
        response = middleware(request)
        self.assertEqual(response.content, b"ar")

    @override_settings(
        LANGUAGE_CODE="en",
        LANGUAGES=(("en", "English"), ("fr", "Français"), ("ar", "العربية")),
        LANGUAGE_COOKIE_NAME="reserve_language",
    )
    def test_invalid_cookie_falls_back_to_default(self):
        request = RequestFactory().get("/", HTTP_ACCEPT_LANGUAGE="fr")
        request.COOKIES["reserve_language"] = "ar-SA"
        middleware = ExplicitLanguageMiddleware(
            lambda req: HttpResponse(translation.get_language())
        )
        response = middleware(request)
        self.assertEqual(response.content, b"en")

    def test_language_endpoint_rejects_get(self):
        response = set_language(RequestFactory().get("/i18n/setlang/"))
        self.assertEqual(response.status_code, 405)

    def test_language_endpoint_rejects_unsupported_language(self):
        request = RequestFactory().post("/i18n/setlang/", {"language": "es"})
        request._dont_enforce_csrf_checks = True
        response = set_language(request)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, response.cookies)

    def test_language_endpoint_sets_only_a_supported_language_cookie(self):
        request = RequestFactory().post("/i18n/setlang/", {"language": "fr", "next": "/"})
        request._dont_enforce_csrf_checks = True
        response = set_language(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "fr")
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME]["httponly"], True)


class LanguageCatalogTests(SimpleTestCase):
    def test_missing_catalog_message_uses_documented_french_source_fallback(self):
        from django.utils.translation import gettext

        source = "Message de repli français réservé au test"
        for language in ("en", "fr", "ar"):
            with self.subTest(language=language), translation.override(language):
                self.assertEqual(gettext(source), source)

    @override_settings(LANGUAGE_CODE="en")
    def test_three_languages_are_configured_and_english_is_default(self):
        self.assertEqual(settings.LANGUAGE_CODE, "en")
        self.assertEqual([code for code, _name in settings.LANGUAGES], ["en", "fr", "ar"])

    def test_template_catalogs_are_compiled_for_english_and_arabic(self):
        from pathlib import Path

        root = Path(settings.BASE_DIR) / "locale"
        for language in ("en", "ar"):
            self.assertTrue((root / language / "LC_MESSAGES" / "django.mo").is_file())

    def test_message_catalogs_translate_english_and_arabic(self):
        from django.utils.translation import gettext

        with translation.override("en"):
            self.assertEqual(gettext("En vérification"), "Under review")
        with translation.override("ar"):
            self.assertEqual(gettext("En vérification"), "قيد المراجعة")

    def test_arabic_plural_forms_are_rendered_by_django(self):
        from django.utils.translation import ngettext

        singular = "%(counter)s notification non lue"
        plural = "%(counter)s notifications non lues"
        with translation.override("en"):
            self.assertEqual(
                ngettext(singular, plural, 1) % {"counter": 1}, "1 unread notification"
            )
            self.assertEqual(
                ngettext(singular, plural, 2) % {"counter": 2}, "2 unread notifications"
            )
        with translation.override("ar"):
            one = ngettext(singular, plural, 1) % {"counter": 1}
            two = ngettext(singular, plural, 2) % {"counter": 2}
            few = ngettext(singular, plural, 5) % {"counter": 5}
            self.assertIn("إشعار غير مقروء", one)
            self.assertIn("إشعاران غير مقروءين", two)
            self.assertIn("إشعارات غير مقروءة", few)

    def test_javascript_catalogs_are_compiled_for_all_languages(self):
        from pathlib import Path

        root = Path(settings.BASE_DIR) / "locale"
        for language in ("en", "fr", "ar"):
            self.assertTrue((root / language / "LC_MESSAGES" / "djangojs.mo").is_file())

    @override_settings(
        LANGUAGE_CODE="en",
        LANGUAGES=(("en", "English"), ("fr", "Français"), ("ar", "العربية")),
    )
    def test_real_template_rendering_honors_explicit_language_cookie(self):
        client = Client()
        english = client.get("/connexion")
        self.assertContains(english, '<html lang="en" dir="ltr">', html=False)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
        arabic = client.get("/connexion")
        self.assertContains(arabic, '<html lang="ar" dir="rtl">', html=False)
        self.assertContains(arabic, "اختر اللغة", html=False)
        change = client.post("/i18n/setlang/", {"language": "fr", "next": "/connexion"})
        self.assertEqual(change.status_code, 302)
        self.assertEqual(client.cookies[settings.LANGUAGE_COOKIE_NAME].value, "fr")
        french = client.get("/connexion")
        self.assertContains(french, '<html lang="fr" dir="ltr">', html=False)
        self.assertContains(french, "Se connecter", html=False)

    @override_settings(
        LANGUAGE_CODE="en",
        LANGUAGES=(("en", "English"), ("fr", "Français"), ("ar", "العربية")),
    )
    def test_public_manifest_metadata_matches_selected_language(self):
        expected = {
            "en": ("Stock management", "Store"),
            "fr": ("Gestion des stocks", "Réserve"),
            "ar": ("إدارة المخزون", "المخزن"),
        }
        client = Client()
        for language, (name, short_name) in expected.items():
            with self.subTest(language=language):
                client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
                response = client.get("/manifest.webmanifest")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["lang"], language)
                self.assertEqual(response.json()["name"], name)
                self.assertEqual(response.json()["short_name"], short_name)

    def test_javascript_catalog_is_available_to_public_pages(self):
        response = Client().get("/i18n/jsi18n/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "globals.gettext = django.gettext", html=False)


class LocalizedNavigationLabelsTests(SimpleTestCase):
    def test_reference_labels_follow_selected_language(self):
        expected = {
            "en": ("Units", "unit", "Categories", "category", "Suppliers", "supplier"),
            "fr": ("Unités", "unité", "Catégories", "catégorie", "Fournisseurs", "fournisseur"),
            "ar": ("الوحدات", "وحدة", "الفئات", "فئة", "الموردون", "مورد"),
        }
        for language, labels in expected.items():
            with self.subTest(language=language), translation.override(language):
                self.assertEqual(
                    tuple(
                        str(REFERENCES[key][index])
                        for key, index in [
                            ("floor", 1),
                            ("floor", 2),
                            ("category", 1),
                            ("category", 2),
                            ("supplier", 1),
                            ("supplier", 2),
                        ]
                    ),
                    labels,
                )

    def test_case_return_labels_follow_selected_language(self):
        from types import SimpleNamespace

        case = SimpleNamespace(delivery_id=None, stock_case_id=None)
        expected = {
            "en": ("Back to delivery issues", "Back to cases", "Back to stock reports"),
            "fr": (
                "Retour aux problèmes de remise",
                "Retour aux dossiers",
                "Retour aux signalements de stock",
            ),
            "ar": (
                "العودة إلى مشكلات التسليم",
                "العودة إلى الحالات",
                "العودة إلى بلاغات المخزون",
            ),
        }
        for language, labels in expected.items():
            with self.subTest(language=language), translation.override(language):
                actual = tuple(
                    case_navigation(RequestFactory().get("/dossiers/x", {"from": origin}), case)[
                        "case_back_label"
                    ]
                    for origin in ("deliveries", "cases", "stock")
                )
                self.assertEqual(actual, labels)

    def test_purchase_back_link_translates_prefix_and_preserves_product_name(self):
        source = "Retour au besoin « %(product)s »"
        expected = {
            "en": "Back to purchase recommendation for “Gants fictifs”",
            "fr": "Retour au besoin « Gants fictifs »",
            "ar": "العودة إلى توصية الشراء للمنتج “Gants fictifs”",
        }
        for language, label in expected.items():
            with self.subTest(language=language), translation.override(language):
                actual = gettext(source) % {"product": "Gants fictifs"}
                self.assertEqual(actual, label)


class LocalizedDateDisplayTests(SimpleTestCase):
    def test_dates_follow_the_selected_language_and_keep_paris_timezone(self):
        value = datetime(2026, 11, 8, 19, 5, tzinfo=ZoneInfo("Europe/Paris"))
        expected = {
            "en": ("Nov. 8, 2026", "Nov. 8, 2026, 7:05 p.m."),
            "fr": ("8 novembre 2026", "8 novembre 2026 19:05"),
            "ar": ("8 نوفمبر 2026", "8 نوفمبر 2026، 19:05"),
        }
        for language, (date_text, datetime_text) in expected.items():
            with self.subTest(language=language), translation.override(language):
                self.assertEqual(ui_date(value), date_text)
                self.assertEqual(ui_datetime(value), datetime_text)

    def test_unavailable_date_message_is_translated(self):
        expected = {
            "en": "Date unavailable",
            "fr": "Date indisponible",
            "ar": "التاريخ غير متاح",
        }
        for language, message in expected.items():
            with self.subTest(language=language), translation.override(language):
                self.assertEqual(ui_date(None), message)
                self.assertEqual(ui_datetime(None), message)


class BackArrowTemplateTests(SimpleTestCase):
    def test_directional_arrow_is_hidden_from_accessible_name(self):
        from django.template.loader import render_to_string

        expected = {"en": "←", "fr": "←", "ar": "→"}
        for language, arrow in expected.items():
            with self.subTest(language=language), translation.override(language):
                rendered = render_to_string("ui/back-arrow.html")
                self.assertIn(f'aria-hidden="true">{arrow}', rendered)
