"""Sanity check that every model is actually registered in Django admin —
cheap to verify, easy to silently regress (a model added without an
@admin.register call just doesn't show up, with no error anywhere else).
"""

from dashboard.models import Client, CorrelationRun, CoverageSnapshot, Device
from django.contrib import admin


def test_every_dashboard_model_is_registered():
    for model in (Client, Device, CorrelationRun, CoverageSnapshot):
        assert model in admin.site._registry, f"{model.__name__} is not registered in admin"
