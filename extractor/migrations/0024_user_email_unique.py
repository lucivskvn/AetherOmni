"""Deduplicate Django users sharing an email and enforce case-insensitive uniqueness.

AUD-390: concurrent first-logins could create two User rows for the same
email because the lookup-then-create path had no database-level guard. This
migration merges duplicate rows onto the lowest-pk account (reassigning all
concrete foreign keys first) and adds a partial unique index on LOWER(email)
so a racing insert now fails closed and the existing IntegrityError fallback
re-fetches the canonical account.
"""

from django.conf import settings
from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Lower


def _iter_user_fk_fields(apps, user_model):
    """Yield (model, attname) for every concrete FK pointing at the user model."""
    user_concrete = user_model._meta.concrete_model
    # include_auto_created picks up M2M through tables (auth_user_groups,
    # auth_user_permissions) so memberships on the duplicate are reassigned
    # instead of silently deleted with dup.delete().
    for model in apps.get_models(include_auto_created=True):
        if model._meta.proxy:
            continue
        for field in model._meta.local_fields:
            if (
                field.is_relation
                and field.many_to_one
                and field.concrete
                and not field.primary_key
                and field.related_model is not None
                and field.related_model._meta.concrete_model is user_concrete
            ):
                yield model, field.attname


def _reassign_user_references(apps, dup, canonical) -> None:
    user_model = canonical.__class__
    for model, attname in _iter_user_fk_fields(apps, user_model):
        model.objects.filter(**{attname: dup.pk}).update(**{attname: canonical.pk})


def dedupe_user_emails(apps, schema_editor):
    """Merge duplicate-email users onto the lowest-pk account."""
    user_model = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    # Blank emails are shared by many accounts and carry no identity; exclude
    # them from both dedupe and the partial unique index below.
    dup_groups = (
        user_model.objects.exclude(email="")
        .annotate(email_lower=Lower("email"))
        .values("email_lower")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    for group in dup_groups:
        members = list(user_model.objects.filter(email__iexact=group["email_lower"]).order_by("pk"))
        canonical = members[0]
        for dup in members[1:]:
            _reassign_user_references(apps, dup, canonical)
            dup.delete()


class Migration(migrations.Migration):
    dependencies = [("extractor", "0023_remove_systemsettings_openrouter_api_key")]

    operations = [
        migrations.RunPython(dedupe_user_emails, migrations.RunPython.noop),
        # Partial index: only non-blank emails are identity-bearing. Works on
        # both Postgres (Supabase) and SQLite (offline) dialects.
        migrations.RunSQL(
            sql=(
                "CREATE UNIQUE INDEX IF NOT EXISTS auth_user_email_lower_uniq "
                "ON auth_user (LOWER(email)) WHERE email <> '';"
            ),
            reverse_sql="DROP INDEX IF EXISTS auth_user_email_lower_uniq;",
        ),
    ]
