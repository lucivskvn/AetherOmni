from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("extractor", "0021_alter_sourcedocument_retry_count_and_more")]

    operations = [
        migrations.AddField(
            model_name="sourcedocument",
            name="cloud_task_name",
            field=models.CharField(max_length=512, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="sourcedocument",
            name="cancel_requested",
            field=models.BooleanField(default=False),
        ),
    ]
