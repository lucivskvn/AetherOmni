from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("extractor", "0024_user_email_unique")]

    operations = [
        migrations.AddField(
            model_name="sourcedocument",
            name="pending_operation",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
