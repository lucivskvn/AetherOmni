"""
Pulumi GCP Infrastructure for KORDA Platform.
Provisions:
- Ephemeral & Backup GCS buckets via ComplianceBucket (Soft Delete OFF on ephemeral)
- Cloud Tasks Queue for asynchronous document processing
- Least-privilege IAM Service Account & worker-scoped bindings
- Cloud Run v2 Services: korda-web & korda-worker via ScaleToZeroWorker
- Eventarc Trigger scoped to raw/ GCS uploads
"""

import base64
import os

import pulumi
import pulumi_gcp as gcp
from components import ComplianceBucket, EventarcRawTrigger, ScaleToZeroWorker, WorkerSizing

config = pulumi.Config()
gcp_config = pulumi.Config("gcp")

project = (
    gcp_config.get("project")
    or os.getenv("GCP_PROJECT_ID")
    or os.getenv("GOOGLE_CLOUD_PROJECT")
    or os.getenv("CLOUDSDK_CORE_PROJECT")
)
if not project or "${" in project:
    raise ValueError(
        "GCP project ID must be specified via 'pulumi config set gcp:project <id>' or GCP_PROJECT_ID env var."
    )

region = config.get("resource_region") or os.getenv("GCP_REGION") or "asia-southeast1"
provider_region = gcp_config.get("region") or region
queue_region = config.get("queue_region") or provider_region


def _is_semver(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 3 and all(part.isascii() and part.isdigit() for part in parts)


# Stack configuration parameters
release_version = config.get("release_version") or os.getenv("RELEASE_VERSION")
image_tag = config.get("image_tag") or os.getenv("IMAGE_TAG")
if not release_version or not _is_semver(release_version):
    raise ValueError("A computed semantic release_version is required for the Pulumi stack.")
if not image_tag or image_tag == "latest":
    raise ValueError("A computed IMAGE_TAG is required; floating 'latest' is not deployable.")
if image_tag != release_version:
    raise ValueError("IMAGE_TAG must match the computed release_version for release verification.")
web_min_instances = config.get_int("web_min_instances") or 0
web_max_instances = config.get_int("web_max_instances") or 5
worker_min_instances = config.get_int("worker_min_instances") or 0
worker_max_instances = config.get_int("worker_max_instances") or 5

# Explicit GCP Provider passing resolved project and region dynamically
gcp_provider = gcp.Provider("gcp-provider", project=project, region=provider_region)
project_info = gcp.organizations.get_project(project_id=project, opts=pulumi.InvokeOptions(provider=gcp_provider))

# 1. Ephemeral Media & Export Bucket (Soft Delete OFF, Versioning OFF, Prefix Lifecycles)
media_bucket = ComplianceBucket(
    "korda-media",
    bucket_name=f"{project}-media-korda",
    location=region,
    project=project,
    ephemeral=True,
    legacy_name="korda-media-bucket",
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

# 1b. Off-Host SurrealDB Backup Bucket (Soft Delete ON, Versioning ON)
backup_bucket = ComplianceBucket(
    "korda-backup",
    bucket_name=f"{project}-backup-korda",
    location=region,
    project=project,
    ephemeral=False,
    backup_retention_days=90,
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

queue_name = config.get("queue_name") or os.getenv("CLOUD_TASKS_QUEUE") or "extractor-tasks-v2"

# 2. Cloud Tasks Queue
tasks_queue = gcp.cloudtasks.Queue(
    "korda-tasks-queue",
    name=queue_name,
    location=queue_region,
    project=project,
    rate_limits=gcp.cloudtasks.QueueRateLimitsArgs(
        max_concurrent_dispatches=10,
        max_dispatches_per_second=50.0,
    ),
    retry_config=gcp.cloudtasks.QueueRetryConfigArgs(
        max_attempts=5,
        min_backoff="1s",
        max_backoff="300s",
        max_doublings=4,
    ),
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

# 3. Dedicated Service Account for Cloud Run & Tasks
service_account = gcp.serviceaccount.Account(
    "korda-service-account",
    account_id="korda-runtime",
    display_name="KORDA Cloud Run Runtime Service Account",
    project=project,
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

# Keep task tokens separate from the runtime identity's data-access permissions.
task_invoker_account = gcp.serviceaccount.Account(
    "korda-task-invoker-account",
    account_id="korda-tasks-invoker",
    display_name="KORDA Cloud Tasks worker invocation",
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

# Least-privilege IAM bindings
RUN_INVOKER_ROLE = "roles/run.invoker"
iam_roles = [
    "roles/aiplatform.user",
    "roles/cloudtasks.enqueuer",
    "roles/run.viewer",
    "roles/eventarc.eventReceiver",
]

iam_members = []
for idx, role in enumerate(iam_roles):
    member = gcp.projects.IAMMember(
        f"korda-iam-{idx}",
        project=project,
        role=role,
        member=service_account.email.apply(lambda email: f"serviceAccount:{email}"),
        opts=pulumi.ResourceOptions(provider=gcp_provider),
    )
    iam_members.append(member)

for sa_idx, sa_role in enumerate(["roles/iam.serviceAccountUser", "roles/iam.serviceAccountTokenCreator"]):
    sa_member = gcp.serviceaccount.IAMMember(
        f"korda-sa-self-iam-{sa_idx}",
        service_account_id=service_account.name,
        role=sa_role,
        member=service_account.email.apply(lambda email: f"serviceAccount:{email}"),
        opts=pulumi.ResourceOptions(provider=gcp_provider),
    )
    iam_members.append(sa_member)

for resource_name, principal in (
    ("korda-task-creator-act-as", service_account.email.apply(lambda email: f"serviceAccount:{email}")),
    (
        "korda-task-agent-act-as",
        f"serviceAccount:service-{project_info.number}@gcp-sa-cloudtasks.iam.gserviceaccount.com",
    ),
):
    iam_members.append(
        gcp.serviceaccount.IAMMember(
            resource_name,
            service_account_id=task_invoker_account.name,
            role="roles/iam.serviceAccountUser",
            member=principal,
            opts=pulumi.ResourceOptions(provider=gcp_provider),
        )
    )

gcp.projects.IAMMember(
    "korda-gcs-pubsub-publisher",
    project=project,
    role="roles/pubsub.publisher",
    member=f"serviceAccount:service-{project_info.number}@gs-project-accounts.iam.gserviceaccount.com",
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

gcp.cloudtasks.QueueIamMember(
    "korda-queue-task-deleter",
    name=tasks_queue.name,
    location=queue_region,
    project=project,
    role="roles/cloudtasks.taskDeleter",
    member=service_account.email.apply(lambda email: f"serviceAccount:{email}"),
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

media_bucket_object_admin = gcp.storage.BucketIAMMember(
    "korda-bucket-object-admin",
    bucket=media_bucket.name,
    role="roles/storage.objectAdmin",
    member=service_account.email.apply(lambda email: f"serviceAccount:{email}"),
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

backup_bucket_object_creator = gcp.storage.BucketIAMMember(
    "korda-backup-bucket-object-creator",
    bucket=backup_bucket.name,
    role="roles/storage.objectCreator",
    member=service_account.email.apply(lambda email: f"serviceAccount:{email}"),
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

# The GitHub-connected Cloud Build trigger runs as the default compute service
# account. scripts/verify_gcp_prerequisites.py performs get/getIamPolicy reads
# on the media bucket, runtime/invoker service accounts, worker service, and
# tasks queue; without these permissions every deploy fails at the
# validate-prerequisites step. Grant a narrow custom role covering exactly
# those reads — no write or IAM-mutation permissions.
deployer_preflight_role = gcp.projects.IAMCustomRole(
    "korda-deploy-preflight-role",
    project=project,
    role_id="kordaDeployPreflight",
    title="KORDA Deploy Preflight Reader",
    description=(
        "Read-only resource and IAM-policy access required by scripts/verify_gcp_prerequisites.py during Cloud Build."
    ),
    permissions=[
        "storage.buckets.get",
        "storage.buckets.getIamPolicy",
        "iam.serviceAccounts.get",
        "iam.serviceAccounts.getIamPolicy",
        "run.services.get",
        "run.services.getIamPolicy",
        "cloudtasks.queues.get",
        "cloudtasks.queues.getIamPolicy",
    ],
    stage="GA",
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

gcp.projects.IAMMember(
    "korda-deploy-preflight-binding",
    project=project,
    role=deployer_preflight_role.name,
    member=f"serviceAccount:{project_info.number}-compute@developer.gserviceaccount.com",
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

# 4. Artifact Registry Repository (for container images)
artifact_repo_id = config.get("artifact_repo_id") or "cloud-run-source-deploy"
artifact_repo = gcp.artifactregistry.Repository(
    "korda-artifact-registry",
    repository_id=artifact_repo_id,
    location=region,
    format="DOCKER",
    description="Cloud Run Source Deployments",
    cleanup_policy_dry_run=False,
    cleanup_policies=[
        gcp.artifactregistry.RepositoryCleanupPolicyArgs(
            id="delete-all-versions-after-30-days",
            action="DELETE",
            condition=gcp.artifactregistry.RepositoryCleanupPolicyConditionArgs(
                older_than="2592000s",
                tag_state="ANY",
            ),
        ),
        gcp.artifactregistry.RepositoryCleanupPolicyArgs(
            id="delete-untagged-after-1-day",
            action="DELETE",
            condition=gcp.artifactregistry.RepositoryCleanupPolicyConditionArgs(
                older_than="86400s",
                tag_state="UNTAGGED",
            ),
        ),
        gcp.artifactregistry.RepositoryCleanupPolicyArgs(
            id="keep-10-most-recent-per-package",
            action="KEEP",
            most_recent_versions=gcp.artifactregistry.RepositoryCleanupPolicyMostRecentVersionsArgs(keep_count=10),
        ),
    ],
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

# Retain the pre-existing registry so state reconciliation never deletes its images.
legacy_artifact_repo = gcp.artifactregistry.Repository(
    "korda-repo",
    repository_id="korda-repo",
    location=region,
    format="DOCKER",
    description="Docker container registry for KORDA images",
    opts=pulumi.ResourceOptions(provider=gcp_provider, protect=True),
)

container_image = (
    config.get("container_image") or f"{region}-docker.pkg.dev/{project}/{artifact_repo_id}/korda:{image_tag}"
)

custom_domain = config.get("custom_domain")
extra_allowed_hosts = config.get("extra_allowed_hosts") or ""
extra_csrf_origins = config.get("extra_csrf_origins") or ""

# The task handler verifies OIDC audience against this WORKER_URL value, so
# scheduler dispatches must target the same project-number URL form. Derive it
# from the single service-name constant so a rename cannot desync the env var,
# the OIDC audience, and the Cloud Run service itself.
worker_service_name = "korda-worker"
worker_task_base_url = f"https://{worker_service_name}-{project_info.number}.{region}.run.app"

common_env = [
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="WORKER_URL", value=worker_task_base_url),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="DJANGO_DEBUG", value="False"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="RELEASE_VERSION", value=release_version),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GCP_PROJECT_ID", value=project),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GCP_REGION", value=region),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GCP_QUEUE_LOCATION", value=queue_region),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GCP_QUEUE_NAME", value=tasks_queue.name),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GS_BUCKET_NAME", value=media_bucket.name),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="BACKUP_BUCKET_NAME", value=backup_bucket.name),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="SURREAL_NS", value="korda"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="SURREAL_DB", value="extractor"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="SURREALDB_OFFLINE", value="False"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GEMINI_MODEL", value="gemini-2.5-flash"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GEMINI_MODEL_BATCH", value="gemini-2.5-flash-lite"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GEMINI_MODEL_LITE", value="gemini-2.5-flash-lite"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GEMINI_MODEL_PRO", value="gemini-2.5-flash"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
        name="CLOUD_TASKS_SERVICE_ACCOUNT", value=task_invoker_account.email
    ),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
        name="EVENTARC_SERVICE_ACCOUNT",
        value=service_account.email,
    ),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="CLOUD_TASKS_QUEUE", value=queue_name),
]

if extra_allowed_hosts:
    common_env.append(
        gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="EXTRA_ALLOWED_HOSTS", value=extra_allowed_hosts)
    )
if extra_csrf_origins:
    common_env.append(
        gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="EXTRA_CSRF_ORIGINS", value=extra_csrf_origins)
    )

secret_keys = [
    "DJANGO_SECRET_KEY",
    "SUPABASE_DATABASE_URL",
    "GEMINI_API_KEY",
    "SURREAL_URL",
    "SURREAL_USER",
    "SURREAL_PASS",
    "ADMIN_EMAIL",
    "SUPABASE_URL",
    "SUPABASE_PUBLIC_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "CF_TURNSTILE_SITE_KEY",
    "SENTRY_DSN",
]

secret_envs = [
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
        name="DATABASE_URL" if key == "SUPABASE_DATABASE_URL" else key,
        value_source=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceArgs(
            secret_key_ref=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceSecretKeyRefArgs(
                secret=key,
                version="latest",
            )
        ),
    )
    for key in secret_keys
]

# Scope secret reads to exactly the secrets mounted into the services instead
# of a project-wide secretAccessor grant. Cloud Run validates secret access at
# revision creation, so these bindings must exist before the services apply.
secret_access_members = []
for idx, key in enumerate(secret_keys):
    secret_access_members.append(
        gcp.secretmanager.SecretIamMember(
            f"korda-secret-access-{idx}",
            secret_id=key,
            role="roles/secretmanager.secretAccessor",
            member=service_account.email.apply(lambda email: f"serviceAccount:{email}"),
            opts=pulumi.ResourceOptions(provider=gcp_provider),
        )
    )

# 5. Cloud Run Worker Service (korda-worker, private, scale-to-zero)
worker_component = ScaleToZeroWorker(
    "korda-worker",
    service_name=worker_service_name,
    location=region,
    project=project,
    image=container_image,
    service_account_email=service_account.email,
    envs=common_env
    + secret_envs
    + [
        gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GUNICORN_WORKERS", value="1"),
        gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GUNICORN_TIMEOUT", value="900"),
        gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="RUN_MIGRATIONS_ON_STARTUP", value="False"),
    ],
    sizing=WorkerSizing(
        cpu="2000m",
        memory="2Gi",
        min_instances=worker_min_instances,
        max_instances=worker_max_instances,
    ),
    is_public=False,
    legacy_name="korda-worker",
    opts=pulumi.ResourceOptions(
        provider=gcp_provider,
        depends_on=[
            *iam_members,
            *secret_access_members,
            media_bucket_object_admin,
            backup_bucket_object_creator,
        ],
    ),
)
worker_service = worker_component.service

# Cloud Tasks signs worker callbacks with this project-scoped OIDC identity.
worker_invoker_binding = gcp.cloudrunv2.ServiceIamMember(
    "korda-worker-cloud-tasks-invoker",
    name=worker_service.name,
    location=region,
    project=project,
    role=RUN_INVOKER_ROLE,
    member=task_invoker_account.email.apply(lambda email: f"serviceAccount:{email}"),
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

worker_runtime_invoker = gcp.cloudrunv2.ServiceIamMember(
    "korda-worker-runtime-invoker",
    name=worker_service.name,
    location=region,
    project=project,
    role=RUN_INVOKER_ROLE,
    member=service_account.email.apply(lambda email: f"serviceAccount:{email}"),
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

# 5b. Eventarc Trigger scoped to raw/ uploads -> korda-worker
raw_trigger = EventarcRawTrigger(
    "korda-raw-upload",
    trigger_name="korda-raw-upload-trigger",
    location=region,
    project=project,
    bucket_name=media_bucket.name,
    worker_service_name=worker_service.name,
    service_account_email=service_account.email,
    opts=pulumi.ResourceOptions(
        provider=gcp_provider, depends_on=[*iam_members, worker_runtime_invoker, worker_invoker_binding]
    ),
)

# 6. Cloud Run Web Service (korda-web, public, scale-to-zero)
web_env = [
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GUNICORN_WORKERS", value="2"),
    # Match the worker/Cloud Run 900s request ceiling: SSE RAG streams and large
    # exports legitimately exceed Gunicorn's 120s default worker timeout.
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GUNICORN_TIMEOUT", value="900"),
]
if custom_domain:
    web_env.append(gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="APP_URL", value=f"https://{custom_domain}"))

web_component = ScaleToZeroWorker(
    "korda-web",
    service_name="korda-web",
    location=region,
    project=project,
    image=container_image,
    service_account_email=service_account.email,
    envs=common_env + secret_envs + web_env,
    sizing=WorkerSizing(
        cpu="1000m",
        memory="1Gi",
        min_instances=web_min_instances,
        max_instances=web_max_instances,
    ),
    is_public=True,
    legacy_name="korda-web",
    opts=pulumi.ResourceOptions(
        provider=gcp_provider,
        depends_on=[*iam_members, *secret_access_members, media_bucket_object_admin, worker_invoker_binding],
    ),
)
web_service = web_component.service

if custom_domain:
    gcp.cloudrun.DomainMapping(
        "korda-custom-domain-mapping",
        name=custom_domain,
        location=region,
        project=project,
        metadata=gcp.cloudrun.DomainMappingMetadataArgs(namespace=project),
        spec=gcp.cloudrun.DomainMappingSpecArgs(route_name=web_service.name),
        opts=pulumi.ResourceOptions(provider=gcp_provider),
    )

# 7. Cloud Run Job for Scheduled Off-Host SurrealDB Backups
# The job only reads SurrealDB and writes to the backup bucket; mounting
# every service secret would over-provision it. It needs the Django secret
# key (settings fail closed without it), the SurrealDB credentials, and the
# common env (bucket names, GCP_PROJECT_ID, SURREAL_NS/DB).
# SUPABASE_DATABASE_URL is mounted as DATABASE_URL defensively: if Cloud Run
# ever sets K_SERVICE for jobs, settings require DATABASE_URL to be configured.
backup_secret_keys = [
    "DJANGO_SECRET_KEY",
    "SUPABASE_DATABASE_URL",
    "SURREAL_URL",
    "SURREAL_USER",
    "SURREAL_PASS",
]
# Mirror the service remap: settings read DATABASE_URL, not the secret's name.
backup_secret_envs = [
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
        name="DATABASE_URL" if key == "SUPABASE_DATABASE_URL" else key,
        value_source=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceArgs(
            secret_key_ref=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceSecretKeyRefArgs(
                secret=key,
                version="latest",
            )
        ),
    )
    for key in backup_secret_keys
]

backup_job = gcp.cloudrunv2.Job(
    "korda-backup-job",
    name="korda-backup-job",
    location=region,
    project=project,
    template=gcp.cloudrunv2.JobTemplateArgs(
        template=gcp.cloudrunv2.JobTemplateTemplateArgs(
            service_account=service_account.email,
            max_retries=1,
            containers=[
                gcp.cloudrunv2.JobTemplateTemplateContainerArgs(
                    image=container_image,
                    args=["manage.py", "backup_surreal"],
                    envs=common_env + backup_secret_envs,
                    resources=gcp.cloudrunv2.JobTemplateTemplateContainerResourcesArgs(
                        limits={"cpu": "1000m", "memory": "1Gi"}
                    ),
                )
            ],
        )
    ),
    opts=pulumi.ResourceOptions(
        provider=gcp_provider,
        depends_on=[*iam_members, *secret_access_members, backup_bucket_object_creator],
    ),
)

gcp.cloudrunv2.JobIamMember(
    "korda-backup-job-invoker",
    name=backup_job.name,
    location=region,
    project=project,
    role=RUN_INVOKER_ROLE,
    member=service_account.email.apply(lambda email: f"serviceAccount:{email}"),
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

backup_schedule = gcp.cloudscheduler.Job(
    "korda-backup-schedule",
    name="korda-backup-daily",
    description="Daily off-host SurrealDB backup export to GCS",
    schedule="0 3 * * *",
    time_zone="UTC",
    region=region,
    project=project,
    http_target=gcp.cloudscheduler.JobHttpTargetArgs(
        http_method="POST",
        uri=f"https://{region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/{project}/jobs/korda-backup-job:run",
        oauth_token=gcp.cloudscheduler.JobHttpTargetOauthTokenArgs(
            service_account_email=service_account.email,
        ),
    ),
    opts=pulumi.ResourceOptions(provider=gcp_provider, depends_on=[backup_job]),
)

# 8. Cloud Scheduler maintenance dispatches -> private worker task handler.
#    The worker scales to zero, so in-process periodic threads cannot run in
#    production; scheduled OIDC POSTs wake it for bounded maintenance work.
#    The task-invoker identity is already allowlisted by the handler's OIDC
#    check and holds run.invoker on the worker.
#
#    NOTE: these jobs were first created imperatively (gcloud) before this
#    code existed. On the first `pulumi up` after this change, import them
#    once or the create will fail with 409 AlreadyExists:
#      pulumi import gcp:cloudscheduler/job:Job \
#        projects/esbpcs-lab-un/locations/asia-southeast1/jobs/korda-reaper-schedule \
#        <pulumi-resource-name>
#    (same for korda-cleanup-schedule). After import, subsequent ups converge.
worker_maintenance_jobs = {
    "korda-reaper-schedule": (
        "*/15 * * * *",
        "reap_stale_tasks",
        "Recover documents stuck in non-terminal states",
    ),
    "korda-cleanup-schedule": (
        "0 * * * *",
        "cleanup_expired_documents",
        "Purge documents past their retention expiry",
    ),
}
for sched_name, (cron, task_name, description) in worker_maintenance_jobs.items():
    gcp.cloudscheduler.Job(
        sched_name,
        name=sched_name,
        description=description,
        schedule=cron,
        time_zone="UTC",
        region=region,
        project=project,
        http_target=gcp.cloudscheduler.JobHttpTargetArgs(
            http_method="POST",
            uri=f"{worker_task_base_url}/internal/tasks/{task_name}/",
            headers={"Content-Type": "application/json"},
            body=base64.b64encode(b"{}").decode("ascii"),
            oidc_token=gcp.cloudscheduler.JobHttpTargetOidcTokenArgs(
                service_account_email=task_invoker_account.email,
                audience=worker_task_base_url,
            ),
        ),
        # Bounded retries: a transient worker/SurrealDB 503 must not wait a full
        # cron period for the next attempt (the Cloud Tasks queue retry policy
        # does not apply to direct Scheduler dispatches).
        retry_config=gcp.cloudscheduler.JobRetryConfigArgs(
            retry_count=3,
            min_backoff_duration="30s",
            max_backoff_duration="300s",
            max_doublings=3,
        ),
        opts=pulumi.ResourceOptions(provider=gcp_provider, depends_on=[worker_service, worker_invoker_binding]),
    )

# Stack Outputs
pulumi.export("web_url", web_service.uri)
pulumi.export("worker_url", worker_service.uri)
pulumi.export("bucket_name", media_bucket.name)
pulumi.export("backup_bucket_name", backup_bucket.name)
pulumi.export("backup_job_name", backup_job.name)
pulumi.export("backup_schedule_name", backup_schedule.name)
pulumi.export("service_account_email", service_account.email)
pulumi.export("queue_name", tasks_queue.name)
pulumi.export("artifact_registry_name", artifact_repo.name)
if custom_domain:
    pulumi.export("custom_domain", custom_domain)
