"""
One-time data migration to raise the monthly budget cap to $50 USD.
This unblocks processing without requiring a manual settings change.
The cap can be re-adjusted any time via the Console Settings panel.
"""

from decimal import Decimal

from django.db import migrations


def raise_budget_cap(apps, schema_editor):
    SystemSettings = apps.get_model("extractor", "SystemSettings")
    obj, created = SystemSettings.objects.get_or_create(id=1)
    # Raise if it's at or below $10 (covers the $5 live value and the $10 default).
    # Does NOT override a custom value already set above $10.
    if obj.monthly_budget_usd <= Decimal("10.00"):
        obj.monthly_budget_usd = Decimal("50.00")
        obj.save()


def noop(apps, schema_editor):
    pass  # irreversible; leave cap as-is on rollback


class Migration(migrations.Migration):
    dependencies = [
        ("extractor", "0016_monthlyspendlog"),
    ]

    operations = [
        migrations.RunPython(raise_budget_cap, noop),
    ]
