# Translation workflow

The interface uses Django's gettext catalogs. English (`en`) is the default, French (`fr`) is the source language, and Arabic (`ar`) uses Modern Standard Arabic with right-to-left layout. User-entered names and stored business codes are not translated.

## Add or update a message

1. Wrap user-visible Python strings in `gettext()` or `gettext_lazy()` and template strings in `{% translate %}` or `{% blocktranslate %}`. Keep technical identifiers and user-entered data unchanged.
2. Extract messages from the project root:

   ```sh
   .venv/bin/python manage.py makemessages --all --no-obsolete
   ```

3. Edit `locale/en/LC_MESSAGES/django.po`, `locale/fr/LC_MESSAGES/django.po`, and `locale/ar/LC_MESSAGES/django.po`. Keep interpolation variable names unchanged. Add the correct plural forms for each language; do not translate stored values.
4. For JavaScript, wrap visible strings with `gettext()` from `djangojs` and edit the corresponding `djangojs.po` catalogs.
5. Compile and validate every catalog:

   ```sh
   for language in en fr ar; do
     .venv/bin/python -m django compilemessages -l "$language"
   done
   ```

6. Run the i18n tests, the browser checks in all languages, and `make quality`.

## Missing-translation fallback

French is the source language of the existing interface strings. If a catalog entry is missing, Django deliberately falls back to that French source text. This keeps the message available but can produce mixed-language screens. The release check must therefore report zero untranslated and zero fuzzy entries for all three catalogs; keep `msgfmt --check --check-format --statistics` in the validation checklist.

## Right-to-left review

Review Arabic pages at narrow and wide viewports. Check logical alignment, focus order, directional icons, forms, tables, mixed Arabic/Latin identifiers, dates, quantities, and error messages. Keep codes, email addresses, URLs, and numeric values intact. Do not add external font or translation services. A catalog compiling successfully does not replace review by a fluent Arabic speaker.

## Language selection

Users choose English, French, or Arabic in the page language selector. The explicit selection is stored in a same-site language cookie for one year. New visitors default to English; browser locale alone does not override that default. The selection is per browser, not synchronized between devices or stored in a user profile. A Web Push subscription keeps the language selected when it was created; revoke and enable the subscription again to apply a later language choice to push messages.
