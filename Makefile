PYTHON ?= .venv/bin/python
DJANGO_SETTINGS ?= config.settings.development

.PHONY: check migrate run test quality

check:
	$(PYTHON) manage.py check --settings=$(DJANGO_SETTINGS)

migrate:
	$(PYTHON) manage.py migrate --settings=config.settings.migration --noinput

run:
	$(PYTHON) manage.py runserver 127.0.0.1:8000 --settings=config.settings.development

test:
	DATABASE_NAME=postgres $(PYTHON) manage.py test tests --settings=config.settings.test --noinput --verbosity=2

quality:
	$(PYTHON) -m ruff check apps config tests manage.py
	$(PYTHON) -m ruff format --check apps config tests manage.py
	$(PYTHON) -m pip check
	$(PYTHON) manage.py check --settings=config.settings.migration
	DATABASE_NAME=postgres $(PYTHON) manage.py makemigrations --check --dry-run --settings=config.settings.test
