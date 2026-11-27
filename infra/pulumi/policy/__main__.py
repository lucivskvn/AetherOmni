"""CrossGuard policy pack enforcing KORDA cost, security, and storage invariants."""

from __future__ import annotations

from pulumi_policy import (
    EnforcementLevel,
    PolicyPack,
    ReportViolation,
    ResourceValidationArgs,
    ResourceValidationPolicy,
)


def _validate_bucket_tier_rules(
    name: str,
    tier: str,
    retention: int | str | None,
    versioning: dict,
    report_violation: ReportViolation,
) -> None:
    is_backup = tier == "backup" or (not tier and "backup" in name)
    if is_backup:
        if retention is None or int(retention) <= 0:
            report_violation(f"Backup GCS bucket '{name}' must enable Soft Delete (retentionDurationSeconds > 0).")
        if versioning.get("enabled") is not True:
            report_violation(f"Backup GCS bucket '{name}' must enable object versioning.")
        return

    if retention is None or int(retention) > 0:
        report_violation(f"Ephemeral GCS bucket '{name}' must disable Soft Delete (retentionDurationSeconds=0).")
    if versioning.get("enabled") is not False:
        report_violation(f"Ephemeral GCS bucket '{name}' must disable object versioning.")


def validate_ephemeral_bucket(args: ResourceValidationArgs, report_violation: ReportViolation) -> None:
    if args.resource_type != "gcp:storage/bucket:Bucket":
        return
    name = str(args.props.get("name") or args.name)
    if args.props.get("uniformBucketLevelAccess") is not True:
        report_violation(f"GCS bucket '{name}' must enable uniformBucketLevelAccess.")

    soft_delete = args.props.get("softDeletePolicy") or {}
    versioning = args.props.get("versioning") or {}
    labels = args.props.get("labels") or {}
    _validate_bucket_tier_rules(
        name,
        str(labels.get("korda-tier") or ""),
        soft_delete.get("retentionDurationSeconds"),
        versioning,
        report_violation,
    )


def validate_scale_to_zero_cloud_run(args: ResourceValidationArgs, report_violation: ReportViolation) -> None:
    if args.resource_type != "gcp:cloudrunv2/service:Service":
        return
    template = args.props.get("template") or {}
    scaling = template.get("scaling") or {}
    min_count = scaling.get("minInstanceCount")
    if min_count is None or int(min_count) > 0:
        report_violation(
            f"Cloud Run service '{args.props.get('name', args.name)}' must set minInstanceCount=0 for $0 idle cost."
        )


def validate_private_worker_iam(args: ResourceValidationArgs, report_violation: ReportViolation) -> None:
    iam_types = {
        "gcp:cloudrunv2/serviceIamMember:ServiceIamMember",
        "gcp:cloudrunv2/serviceIamBinding:ServiceIamBinding",
        "gcp:cloudrunv2/serviceIamPolicy:ServiceIamPolicy",
    }
    if args.resource_type not in iam_types:
        return
    service_name = str(args.props.get("name") or args.name)
    role = str(args.props.get("role") or "")
    # Only the web service's public invoker binding may reference allUsers.
    # Match the exact service name and role rather than a substring heuristic
    # so a future "korda-webhook"-style name cannot silently inherit the
    # exemption, and a non-invoker allUsers grant on the web service is still
    # flagged.
    is_public_web_invoker = service_name == "korda-web" and role == "roles/run.invoker"

    forbidden = {"allUsers", "allAuthenticatedUsers"}
    member = str(args.props.get("member", ""))
    if member in forbidden and not is_public_web_invoker:
        report_violation(f"Service '{service_name}' must remain private; '{member}' is forbidden.")
    for m in args.props.get("members") or []:
        if str(m) in forbidden and not is_public_web_invoker:
            report_violation(f"Service '{service_name}' must remain private; '{m}' is forbidden.")
    policy_data = str(args.props.get("policyData") or "")
    if any(f in policy_data for f in forbidden):
        report_violation(f"Worker service '{service_name}' IAM policyData must not grant public access.")


PolicyPack(
    name="korda-compliance-policy",
    enforcement_level=EnforcementLevel.MANDATORY,
    policies=[
        ResourceValidationPolicy(
            name="ephemeral-bucket-zero-soft-delete",
            description="Ephemeral media buckets must disable soft delete and versioning and enable uniform access.",
            validate=validate_ephemeral_bucket,
        ),
        ResourceValidationPolicy(
            name="cloud-run-scale-to-zero",
            description="All Cloud Run v2 services must enforce minInstanceCount = 0.",
            validate=validate_scale_to_zero_cloud_run,
        ),
        ResourceValidationPolicy(
            name="private-worker-no-all-users",
            description="Worker Cloud Run services must never grant allUsers or allAuthenticatedUsers.",
            validate=validate_private_worker_iam,
        ),
    ],
)
