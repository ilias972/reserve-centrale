"""Public, CSRF-protected language selection endpoint."""

from django.conf import settings
from django.http import HttpResponseBadRequest
from django.utils.translation import gettext
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST
from django.views.i18n import set_language as django_set_language


@csrf_protect
@require_POST
def set_language(request):
    supported = {code for code, _label in settings.LANGUAGES}
    if request.POST.get("language") not in supported:
        return HttpResponseBadRequest(gettext("Langue non prise en charge."))
    return django_set_language(request)


set_language.m0_access = "public"
set_language.temporary_access = True
