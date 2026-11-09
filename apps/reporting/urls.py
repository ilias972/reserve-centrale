from django.urls import path

from . import views

urlpatterns = [
    path("tableau-de-bord", views.dashboard, name="reporting-dashboard"),
    path("analyses/<str:name>", views.analysis, name="reporting-analysis"),
    path("analyses/<str:name>/export.xlsx", views.export, name="reporting-export"),
    path("historique-personnel", views.analysis, {"personal": True}, name="reporting-personal"),
]
