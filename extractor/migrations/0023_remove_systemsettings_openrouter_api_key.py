"""Purge the retired provider credential from the offline Django mirror."""

from django.db import migrations


def purge_retired_openrouter_values(apps, schema_editor):
    """Clear obsolete secrets before the column is irreversibly removed."""
    system_settings = apps.get_model("extractor", "SystemSettings")
    system_settings.objects.exclude(openrouter_api_key="").update(openrouter_api_key="")


class Migration(migrations.Migration):
    dependencies = [("extractor", "0022_document_task_cancellation")]

    operations = [
        migrations.RunPython(purge_retired_openrouter_values, migrations.RunPython.noop),
        migrations.RemoveField(model_name="systemsettings", name="openrouter_api_key"),
    ]
