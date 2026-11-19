"""
Pulumi GCP Infrastructure for KORDA Platform.
Provisions:
- Ephemeral & Backup GCS buckets via ComplianceBucket (Soft Delete OFF on ephemeral)
- Cloud Tasks Queue for asynchronous document processing
- Least-privilege IAM Service Account & worker-scoped bindings
- Cloud Run v2 Services: korda-web & korda-worker via ScaleToZeroWorker
- Eventarc Trigger scoped to raw/ GCS uploads
"""

import os

import pulumi
import pulumi_gcp as gcp
from components import ComplianceBucket, EventarcRawTrigger, ScaleToZeroWorker

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

region = gcp_config.get("region") or os.getenv("GCP_REGION") or "asia-southeast1"

release_version = config.get("release_version") or "1.5.0"
image_tag = config.get("image_tag") or os.getenv("IMAGE_TAG") or release_version
web_min_instances = config.get_int("web_min_instances") or 0
web_max_instances = config.get_int("web_max_instances") or 5
worker_min_instances = config.get_int("worker_min_instances") or 0
worker_max_instances = config.get_int("worker_max_instances") or 5

gcp_provider = gcp.Provider("gcp-provider", project=project, region=region)
project_info = gcp.organizations.get_project(project_id=project, opts=pulumi.InvokeOptions(provider=gcp_provider))
cloud_tasks_oidc_member = f"serviceAccount:{project_info.number}-compute@developer.gserviceaccount.com"

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
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

queue_name = config.get("queue_name") or os.getenv("CLOUD_TASKS_QUEUE") or "extractor-tasks-v2"

# 2. Cloud Tasks Queue
tasks_queue = gcp.cloudtasks.Queue(
    "korda-tasks-queue",
    name=queue_name,
    location=region,
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
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

# Least-privilege project-level IAM bindings (run.invoker is scoped to korda-worker below)
RUN_INVOKER_ROLE = "roles/run.invoker"
iam_roles = [
    "roles/aiplatform.user",
    "roles/secretmanager.secretAccessor",
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
    location=region,
    project=project,
    role="roles/cloudtasks.taskDeleter",
    member=service_account.email.apply(lambda email: f"serviceAccount:{email}"),
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

gcp.storage.BucketIAMMember(
    "korda-bucket-object-admin",
    bucket=media_bucket.name,
    role="roles/storage.objectAdmin",
    member=service_account.email.apply(lambda email: f"serviceAccount:{email}"),
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

gcp.storage.BucketIAMMember(
    "korda-backup-bucket-object-creator",
    bucket=backup_bucket.name,
    role="roles/storage.objectCreator",
    member=service_account.email.apply(lambda email: f"serviceAccount:{email}"),
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

# 4. Artifact Registry Repository
artifact_repo = gcp.artifactregistry.Repository(
    "korda-repo",
    repository_id="korda-repo",
    location=region,
    format="DOCKER",
    description="Docker container registry for KORDA images",
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

container_image = (
    config.get("container_image") or f"{region}-docker.pkg.dev/{project}/cloud-run-source-deploy/korda:{image_tag}"
)

custom_domain = config.get("custom_domain")
extra_allowed_hosts = config.get("extra_allowed_hosts") or ""
extra_csrf_origins = config.get("extra_csrf_origins") or ""

common_env = [
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="DJANGO_DEBUG", value="False"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="RELEASE_VERSION", value=release_version),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GCP_PROJECT_ID", value=project),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GCP_REGION", value=region),
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
        name="CLOUD_TASKS_SERVICE_ACCOUNT",
        value=service_account.email,
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

# 5. Cloud Run Worker Service (korda-worker, private, scale-to-zero)
worker_component = ScaleToZeroWorker(
    "korda-worker",
    service_name="korda-worker",
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
    cpu="2000m",
    memory="2Gi",
    min_instances=worker_min_instances,
    max_instances=worker_max_instances,
    is_public=False,
    legacy_name="korda-worker",
    opts=pulumi.ResourceOptions(provider=gcp_provider, depends_on=iam_members),
)
worker_service = worker_component.service

gcp.cloudrunv2.ServiceIamMember(
    "korda-worker-cloud-tasks-invoker",
    name=worker_service.name,
    location=region,
    project=project,
    role=RUN_INVOKER_ROLE,
    member=cloud_tasks_oidc_member,
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

gcp.cloudrunv2.ServiceIamMember(
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
    opts=pulumi.ResourceOptions(provider=gcp_provider, depends_on=iam_members),
)

# 6. Cloud Run Web Service (korda-web, public, scale-to-zero)
web_env = [
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GUNICORN_WORKERS", value="2"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="WORKER_URL", value=worker_service.uri),
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
    cpu="1000m",
    memory="1Gi",
    min_instances=web_min_instances,
    max_instances=web_max_instances,
    is_public=True,
    legacy_name="korda-web",
    opts=pulumi.ResourceOptions(provider=gcp_provider, depends_on=iam_members),
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
                    envs=common_env + secret_envs,
                    resources=gcp.cloudrunv2.JobTemplateTemplateContainerResourcesArgs(
                        limits={"cpu": "1000m", "memory": "1Gi"}
                    ),
                )
            ],
        )
    ),
    opts=pulumi.ResourceOptions(provider=gcp_provider, depends_on=iam_members),
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
