from django import forms
from django.db.models import Q
from django.utils.translation import gettext, gettext_lazy

from apps.accounts.forms import LabeledForm, OperationForm

from .models import Category


class VersionForm(OperationForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput, min_value=0)


class ProductForm(VersionForm):
    name = forms.CharField(label=gettext_lazy("Nom du produit"), max_length=150)
    category_id = forms.ModelChoiceField(
        label=gettext_lazy("Catégorie (facultative)"),
        queryset=Category.objects.none(),
        required=False,
        empty_label=gettext_lazy("Sans catégorie"),
    )
    unit = forms.CharField(
        label=gettext_lazy("Unité de comptage unique"),
        max_length=40,
        help_text=gettext_lazy("Par exemple : carton, boîte, paquet ou pièce. Aucune conversion."),
    )
    minimum = forms.IntegerField(
        label=gettext_lazy("Stock bas à"),
        min_value=0,
        max_value=1000000000,
        help_text=gettext_lazy("À cette quantité disponible ou moins, le stock est considéré bas."),
    )
    target = forms.IntegerField(
        label=gettext_lazy("Quantité souhaitée"),
        min_value=0,
        max_value=1000000000,
        help_text=gettext_lazy("Quantité disponible souhaitée après réapprovisionnement."),
    )

    def __init__(self, *args, product=None, **kwargs):
        super().__init__(*args, **kwargs)
        categories = Q(active=True)
        if product and product.category_id:
            categories |= Q(pk=product.category_id)
        self.fields["category_id"].queryset = Category.objects.filter(categories).order_by("name")
        if product:
            if product.counted_at:
                self.fields["unit"].disabled = True
                self.fields["unit"].help_text = gettext_lazy(
                    "Verrouillée depuis le premier inventaire, même nul. Créez une nouvelle référence pour changer d'unité."
                )
        else:
            self.fields.pop("expected_version")

    def clean(self):
        result = super().clean()
        if (
            result.get("target") is not None
            and result.get("minimum") is not None
            and result["target"] < result["minimum"]
        ):
            self.add_error(
                "target",
                gettext("La quantité souhaitée doit être au moins égale à la limite de stock bas."),
            )
        return result


class ReferenceForm(VersionForm):
    name = forms.CharField(label=gettext_lazy("Nom"), max_length=120)
    active = forms.BooleanField(label=gettext_lazy("Actif"), required=False)
    website = forms.URLField(
        label=gettext_lazy("Site Web (facultatif)"),
        max_length=500,
        required=False,
        assume_scheme="https",
    )

    def __init__(self, *args, kind, reference=None, **kwargs):
        super().__init__(*args, **kwargs)
        if kind != "supplier":
            self.fields.pop("website")
        else:
            self.fields["name"] = forms.CharField(label=gettext_lazy("Nom"), max_length=150)
        if reference is None:
            self.fields.pop("expected_version")


class PhotoForm(VersionForm):
    photo = forms.FileField(
        label=gettext_lazy("Photographie du produit"),
        help_text=gettext_lazy(
            "JPEG, PNG ou WebP fixe ; 5 Mio, 20 mégapixels et 8 192 pixels par côté maximum. Les métadonnées seront retirées."
        ),
        widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
    )


class ArchiveForm(VersionForm):
    confirmed = forms.BooleanField(
        label=gettext_lazy(
            "Je confirme le retrait des nouvelles demandes, sans supprimer l'historique ni les engagements"
        )
    )


class ActivationForm(VersionForm):
    inventory_verified = forms.BooleanField(
        label=gettext_lazy("J'ai vérifié l'inventaire confirmé")
    )
    photo_verified = forms.BooleanField(
        label=gettext_lazy("J'ai vérifié que la photo correspond à l'unité comptée")
    )


class PermissionRowForm(LabeledForm):
    floor_id = forms.UUIDField(widget=forms.HiddenInput)
    demandable = forms.BooleanField(label=gettext_lazy("Demande autorisée"), required=False)
    maximum = forms.IntegerField(
        label=gettext_lazy("Maximum par demande"), min_value=1, max_value=1000000000, required=False
    )

    def clean(self):
        result = super().clean()
        result["visible"] = bool(result.get("demandable"))
        if result.get("demandable") and result.get("maximum") is None:
            raise forms.ValidationError(
                gettext("Indiquez un maximum par demande pour autoriser ce produit.")
            )
        return result


class SupplierRowForm(LabeledForm):
    supplier_id = forms.UUIDField(widget=forms.HiddenInput)
    included = forms.BooleanField(label=gettext_lazy("Associer ce fournisseur"), required=False)
    preferred = forms.BooleanField(label=gettext_lazy("Fournisseur habituel"), required=False)
    reference = forms.CharField(
        label=gettext_lazy("Référence du produit chez le fournisseur (facultative)"),
        max_length=150,
        required=False,
        help_text=gettext_lazy(
            "Facultatif : code de l’article dans le catalogue de ce fournisseur, pas le numéro d’une commande."
        ),
    )
    website = forms.URLField(
        label=gettext_lazy("Lien produit"), max_length=500, required=False, assume_scheme="https"
    )
    indicative_price = forms.DecimalField(
        label=gettext_lazy("Prix indicatif"),
        min_value=0,
        max_digits=14,
        decimal_places=2,
        required=False,
    )
    currency = forms.RegexField(
        regex=r"^[A-Za-z]{3}$", required=False, max_length=3, widget=forms.HiddenInput
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        currency = self.initial.get("currency") or "EUR"
        self.initial["currency"] = currency
        self.fields["currency"].disabled = True
        self.fields["indicative_price"].help_text = (
            "En euros (€). Facultatif."
            if currency == "EUR"
            else f"Devise déjà enregistrée : {currency}. Conservée sans conversion."
        )

    def clean(self):
        values = super().clean()
        if values.get("indicative_price") is None:
            values["currency"] = ""
        if values.get("preferred") and not values.get("included"):
            self.add_error(
                "preferred", gettext("Associez le fournisseur pour le choisir comme habituel.")
            )
        if values.get("included") and (
            (values.get("indicative_price") is None) != (not values.get("currency"))
        ):
            raise forms.ValidationError(
                gettext(
                    "Renseignez ensemble le prix indicatif et sa devise, ou laissez les deux vides."
                )
            )
        return values
