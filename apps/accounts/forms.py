import uuid

from django import forms
from django.db.models import Q
from django.utils.translation import gettext, gettext_lazy

from apps.catalog.models import Floor

from .models import REQUESTER_ROLES, User


class LabeledForm(forms.Form):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)


class LoginForm(LabeledForm):
    identifier = forms.CharField(
        label=gettext_lazy("Identifiant"),
        max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "username", "autocapitalize": "none"}),
    )
    password = forms.CharField(
        label=gettext_lazy("Mot de passe"),
        max_length=256,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )
    persistent = forms.BooleanField(
        label=gettext_lazy("Rester connecté sur cet appareil individuel"), required=False
    )


class ChangePasswordForm(LabeledForm):
    current_password = forms.CharField(
        label=gettext_lazy("Mot de passe actuel ou temporaire"),
        max_length=256,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )
    new_password = forms.CharField(
        label=gettext_lazy("Nouveau mot de passe"),
        min_length=15,
        max_length=256,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    confirm_password = forms.CharField(
        label=gettext_lazy("Confirmer le nouveau mot de passe"),
        max_length=256,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean(self):
        result = super().clean()
        if result.get("new_password") != result.get("confirm_password"):
            self.add_error(
                "confirm_password",
                gettext("Les deux nouveaux mots de passe doivent être identiques."),
            )
        return result


class OperationForm(LabeledForm):
    operation_id = forms.UUIDField(widget=forms.HiddenInput)
    operation_key = forms.CharField(widget=forms.HiddenInput, min_length=16, max_length=128)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["operation_id"].initial = uuid.uuid4
        self.fields["operation_key"].initial = lambda: uuid.uuid4().hex


class AdminOperationForm(OperationForm):
    admin_password = forms.CharField(
        label=gettext_lazy("Votre mot de passe Gestionnaire (si connexion ancienne)"),
        max_length=256,
        strip=False,
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        help_text=gettext_lazy(
            "Demandé après 15 minutes par défaut provisoire. Ne renseignez jamais le mot de passe du salarié."
        ),
    )


class UserForm(AdminOperationForm):
    display_name = forms.CharField(label=gettext_lazy("Nom affiché"), max_length=150)
    role = forms.ChoiceField(
        label=gettext_lazy("Rôle"),
        help_text=gettext_lazy(
            "Choisissez le profil du compte. Le profil de gestionnaire est unique ; son remplacement suit la procédure contrôlée du service informatique."
        ),
        choices=[
            (User.Role.REPLENISHER, gettext_lazy("Préparateur")),
            (User.Role.ASSISTANT, gettext_lazy("Demandeur — profil A")),
            (User.Role.DOCTOR, gettext_lazy("Demandeur — profil B")),
        ],
    )
    floor_id = forms.ModelChoiceField(
        label=gettext_lazy("Unité d’affectation"),
        queryset=Floor.objects.none(),
        required=False,
        empty_label=gettext_lazy("Aucun unité"),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        query = Q(active=True)
        if user and user.floor_id:
            query |= Q(pk=user.floor_id)
        self.fields["floor_id"].queryset = Floor.objects.filter(query).order_by("code")
        if user and user.role == User.Role.ADMIN:
            self.fields["role"].choices = [(User.Role.ADMIN, "Gestionnaire")]
        if not self.fields["floor_id"].queryset.exists():
            self.fields["floor_id"].help_text = gettext_lazy(
                "Créez ou réactivez un unité avant d’affecter un compte demandeur."
            )

    def clean(self):
        values = super().clean()
        if values.get("role") in REQUESTER_ROLES and not values.get("floor_id"):
            self.add_error("floor_id", gettext("Choisissez un unité actif pour ce compte."))
        if values.get("role") not in REQUESTER_ROLES and values.get("floor_id"):
            self.add_error(
                "floor_id",
                gettext("Un préparateur ou la gestionnaire n'est pas affecté à un unité."),
            )
        return values


class CreateUserForm(UserForm):
    username = forms.RegexField(
        label=gettext_lazy("Identifiant de connexion"),
        help_text=gettext_lazy(
            "Nom unique utilisé pour se connecter, par exemple prenom.nom. Ce n’est pas un numéro administratif."
        ),
        regex=r"^[a-zA-Z0-9_.@+-]{1,150}$",
        max_length=150,
    )


class UpdateUserForm(UserForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
    is_active = forms.BooleanField(label=gettext_lazy("Compte actif"), required=False)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, user=user, **kwargs)
        if user and user.role == User.Role.ADMIN:
            self.fields["is_active"].disabled = True
            self.fields["is_active"].help_text = gettext_lazy(
                "Le dernier accès Gestionnaire ne peut pas être désactivé ici."
            )


class ResetAccessForm(AdminOperationForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
    identity_verified = forms.BooleanField(
        label=gettext_lazy("J'ai vérifié l'identité de cette personne")
    )
