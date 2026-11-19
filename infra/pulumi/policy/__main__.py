"""CrossGuard policy pack enforcing KORDA cost, security, and storage invariants."""

from __future__ import annotations

from pulumi_policy import (
    EnforcementLevel,
    PolicyPack,
    ReportViolation,
    ResourceValidationArgs,
    ResourceValidationPolicy,
)


def validate_ephemeral_bucket(args: ResourceValidationArgs, report_violation: ReportViolation) -> None:
    if args.resource_type != "gcp:storage/bucket:Bucket":
        return
    name = str(args.props.get("name", ""))
    if "backup" in name:
        return

    soft_delete = args.props.get("softDeletePolicy") or {}
    retention = soft_delete.get("retentionDurationSeconds")
    if retention is None or int(retention) > 0:
        report_violation(f"Ephemeral GCS bucket '{name}' must disable Soft Delete (retentionDurationSeconds=0).")

    versioning = args.props.get("versioning") or {}
    if versioning.get("enabled") is not False:
        report_violation(f"Ephemeral GCS bucket '{name}' must disable object versioning.")

    if args.props.get("uniformBucketLevelAccess") is not True:
        report_violation(f"GCS bucket '{name}' must enable uniformBucketLevelAccess.")


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
    if args.resource_type != "gcp:cloudrunv2/serviceIamMember:ServiceIamMember":
        return
    service_name = str(args.props.get("name", ""))
    member = str(args.props.get("member", ""))
    if "worker" in service_name and member in {"allUsers", "allAuthenticatedUsers"}:
        report_violation(f"Worker service '{service_name}' must remain private; '{member}' is forbidden.")


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
