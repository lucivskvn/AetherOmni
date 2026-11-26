"""Reusable Pulumi ComponentResources for KORDA GCP infrastructure."""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp


class ComplianceBucket(pulumi.ComponentResource):
    """GCS bucket enforcing ephemeral (zero soft-delete, prefix TTL) or backup compliance."""

    def __init__(
        self,
        name: str,
        *,
        bucket_name: str,
        location: str,
        project: str,
        ephemeral: bool = True,
        export_retention_days: int = 14,
        backup_retention_days: int | None = None,
        legacy_name: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("korda:storage:ComplianceBucket", name, None, opts)
        aliases = [pulumi.Alias(name=legacy_name, parent=pulumi.ROOT_STACK_OBJECT)] if legacy_name else None
        child_opts = pulumi.ResourceOptions(parent=self, aliases=aliases)

        if ephemeral:
            soft_delete = gcp.storage.BucketSoftDeletePolicyArgs(retention_duration_seconds=0)
            versioning = gcp.storage.BucketVersioningArgs(enabled=False)
            lifecycle_rules = [
                gcp.storage.BucketLifecycleRuleArgs(
                    action=gcp.storage.BucketLifecycleRuleActionArgs(type="Delete"),
                    condition=gcp.storage.BucketLifecycleRuleConditionArgs(
                        matches_prefixes=["raw/ephemeral/"],
                        age=1,
                    ),
                ),
                gcp.storage.BucketLifecycleRuleArgs(
                    action=gcp.storage.BucketLifecycleRuleActionArgs(type="Delete"),
                    condition=gcp.storage.BucketLifecycleRuleConditionArgs(
                        matches_prefixes=["exports/"],
                        age=export_retention_days,
                    ),
                ),
                gcp.storage.BucketLifecycleRuleArgs(
                    action=gcp.storage.BucketLifecycleRuleActionArgs(type="AbortIncompleteMultipartUpload"),
                    condition=gcp.storage.BucketLifecycleRuleConditionArgs(age=1),
                ),
            ]
        else:
            soft_delete = gcp.storage.BucketSoftDeletePolicyArgs(retention_duration_seconds=604800)
            versioning = gcp.storage.BucketVersioningArgs(enabled=True)
            lifecycle_rules = [
                gcp.storage.BucketLifecycleRuleArgs(
                    action=gcp.storage.BucketLifecycleRuleActionArgs(type="AbortIncompleteMultipartUpload"),
                    condition=gcp.storage.BucketLifecycleRuleConditionArgs(age=1),
                ),
            ]
            if backup_retention_days is not None:
                # Daily backup exports accumulate forever without an age-based
                # rule; bound retention independently of soft-delete/versioning.
                lifecycle_rules.append(
                    gcp.storage.BucketLifecycleRuleArgs(
                        action=gcp.storage.BucketLifecycleRuleActionArgs(type="Delete"),
                        condition=gcp.storage.BucketLifecycleRuleConditionArgs(
                            matches_prefixes=["backups/"],
                            age=backup_retention_days,
                        ),
                    )
                )

        self.bucket = gcp.storage.Bucket(
            f"{name}-bucket",
            name=bucket_name,
            location=location,
            project=project,
            uniform_bucket_level_access=True,
            soft_delete_policy=soft_delete,
            versioning=versioning,
            lifecycle_rules=lifecycle_rules,
            labels={"korda-tier": "ephemeral" if ephemeral else "backup"},
            opts=child_opts,
        )
        self.name = self.bucket.name
        self.url = self.bucket.url
        self.register_outputs({"bucket_name": self.bucket.name, "bucket_url": self.bucket.url})


from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerSizing:
    """Resource limits, scaling bounds, and execution settings for a Cloud Run service."""

    cpu: str = "2000m"
    memory: str = "2Gi"
    min_instances: int = 0
    max_instances: int = 5
    timeout: str = "900s"
    execution_environment: str | None = None


class ScaleToZeroWorker(pulumi.ComponentResource):
    """Cloud Run v2 service enforcing scale-to-zero ($0 idle) and least-privilege ingress."""

    def __init__(
        self,
        name: str,
        *,
        service_name: str,
        location: str,
        project: str,
        image: str,
        service_account_email: pulumi.Input[str],
        envs: list[gcp.cloudrunv2.ServiceTemplateContainerEnvArgs],
        sizing: WorkerSizing | None = None,
        is_public: bool = False,
        legacy_name: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("korda:compute:ScaleToZeroWorker", name, None, opts)
        cfg = sizing or WorkerSizing()
        aliases = [pulumi.Alias(name=legacy_name, parent=pulumi.ROOT_STACK_OBJECT)] if legacy_name else None
        child_opts = pulumi.ResourceOptions(parent=self, aliases=aliases)

        scaling = gcp.cloudrunv2.ServiceTemplateScalingArgs(
            min_instance_count=cfg.min_instances,
            max_instance_count=cfg.max_instances,
        )
        template_kwargs: dict = {
            "service_account": service_account_email,
            "scaling": scaling,
            "timeout": cfg.timeout,
            "containers": [
                gcp.cloudrunv2.ServiceTemplateContainerArgs(
                    image=image,
                    ports=[gcp.cloudrunv2.ServiceTemplateContainerPortArgs(container_port=8080)],
                    envs=envs,
                    resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                        limits={"cpu": cfg.cpu, "memory": cfg.memory},
                    ),
                )
            ],
        }
        if cfg.execution_environment:
            template_kwargs["execution_environment"] = cfg.execution_environment

        self.service = gcp.cloudrunv2.Service(
            f"{name}-service",
            name=service_name,
            location=location,
            project=project,
            ingress="INGRESS_TRAFFIC_ALL",
            labels={"korda-role": "web" if is_public else "worker"},
            template=gcp.cloudrunv2.ServiceTemplateArgs(**template_kwargs),
            opts=child_opts,
        )

        if is_public:
            gcp.cloudrunv2.ServiceIamMember(
                f"{name}-public-invoker",
                name=self.service.name,
                location=location,
                project=project,
                role="roles/run.invoker",
                member="allUsers",
                opts=pulumi.ResourceOptions(parent=self),
            )

        self.uri = self.service.uri
        self.register_outputs({"service_name": self.service.name, "uri": self.service.uri})


class EventarcRawTrigger(pulumi.ComponentResource):
    """Eventarc trigger scoped to GCS object finalization under the raw/ prefix."""

    def __init__(
        self,
        name: str,
        *,
        trigger_name: str,
        location: str,
        project: str,
        bucket_name: pulumi.Input[str],
        worker_service_name: pulumi.Input[str],
        service_account_email: pulumi.Input[str],
        destination_path: str = "/internal/events/gcs-raw/",
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("korda:events:EventarcRawTrigger", name, None, opts)
        child_opts = pulumi.ResourceOptions(parent=self)

        self.trigger = gcp.eventarc.Trigger(
            f"{name}-trigger",
            name=trigger_name,
            location=location,
            project=project,
            service_account=service_account_email,
            matching_criterias=[
                gcp.eventarc.TriggerMatchingCriteriaArgs(
                    attribute="type",
                    value="google.cloud.storage.object.v1.finalized",
                ),
                gcp.eventarc.TriggerMatchingCriteriaArgs(
                    attribute="bucket",
                    value=bucket_name,
                ),
                gcp.eventarc.TriggerMatchingCriteriaArgs(
                    attribute="subject",
                    operator="match-path-pattern",
                    value="objects/raw/**",
                ),
            ],
            destination=gcp.eventarc.TriggerDestinationArgs(
                cloud_run_service=gcp.eventarc.TriggerDestinationCloudRunServiceArgs(
                    service=worker_service_name,
                    path=destination_path,
                    region=location,
                )
            ),
            opts=child_opts,
        )
        self.register_outputs({"trigger_name": self.trigger.name})
