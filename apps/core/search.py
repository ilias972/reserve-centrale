"""Recherche de lecture commune : accents, séparateurs et mots partiels."""

import re
import unicodedata

from django.db.models import Case, CharField, F, Func, IntegerField, Q, Value, When
from django.db.models.functions import Coalesce, Lower, Replace, Trim

ACCENTS = "àâäáãåçèéêëìíîïñòóôöõùúûüýÿ"
LETTERS = "aaaaaaceeeeiiiinooooouuuuyy"
MARKS = "".join(chr(code) for code in range(0x300, 0x370))


def normalized(value):
    value = unicodedata.normalize("NFKD", value[:150].lower().replace("œ", "oe").replace("æ", "ae"))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", value))


def matching(query, text, fields):
    """Les champs sont des constantes internes, jamais des noms fournis par HTTP."""
    needle = normalized(text)
    if not needle:
        query = query.none() if text.strip() else query
        return query.annotate(_search_rank=Value(0, output_field=IntegerField()))
    expressions = {}
    for index, field in enumerate(fields):
        value = Lower(Coalesce(F(field), Value(""), output_field=CharField()))
        value = Replace(
            Replace(
                Replace(Replace(value, Value("œ"), Value("oe")), Value("æ"), Value("ae")),
                Value("Œ"),
                Value("oe"),
            ),
            Value("Æ"),
            Value("ae"),
        )
        value = Func(
            value,
            Value(ACCENTS + ACCENTS.upper() + MARKS),
            Value(LETTERS + LETTERS),
            function="TRANSLATE",
            output_field=CharField(),
        )
        expressions[f"_search_{index}"] = Trim(
            Func(
                value,
                Value("[^[:alnum:]]+"),
                Value(" "),
                Value("g"),
                function="REGEXP_REPLACE",
                output_field=CharField(),
            )
        )
    query = query.annotate(**expressions)
    for word in needle.split()[:16]:
        condition = Q()
        for alias in expressions:
            condition |= Q(**{f"{alias}__contains": word})
        query = query.filter(condition)
    prefix = Q()
    for alias in expressions:
        prefix |= Q(**{f"{alias}__startswith": needle})
    return query.annotate(
        _search_rank=Case(
            When(prefix, then=Value(0)), default=Value(1), output_field=IntegerField()
        )
    ).distinct()
