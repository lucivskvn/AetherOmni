from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Adds MonthlySpendLog – a persistent accumulator for AI compute spend
    that survives SourceDocument deletions.
    """

    dependencies = [
        ("extractor", "0015_systemsettings_currency"),
    ]

    operations = [
        migrations.CreateModel(
            name="MonthlySpendLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("year", models.SmallIntegerField(db_index=True)),
                ("month", models.SmallIntegerField()),
                (
                    "accumulated_cost_usd",
                    models.DecimalField(decimal_places=6, default=0, max_digits=12),
                ),
                ("accumulated_input_tokens", models.BigIntegerField(default=0)),
                ("accumulated_output_tokens", models.BigIntegerField(default=0)),
            ],
            options={
                "ordering": ["-year", "-month"],
                "unique_together": {("year", "month")},
            },
        ),
    ]
