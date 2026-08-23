"""
Pulumi GCP Infrastructure for KORDA Platform.
Provisions:
- Cloud Storage Bucket for media/booklet uploads
- Cloud Tasks Queue for asynchronous document processing
- IAM Service Account & least-privilege role bindings
- Cloud Run v2 Services: korda-web & korda-worker
"""

import os

import pulumi
import pulumi_gcp as gcp

config = pulumi.Config()
gcp_config = pulumi.Config("gcp")

# Dynamic project resolution: Pulumi config -> GCP_PROJECT_ID / GOOGLE_CLOUD_PROJECT env vars
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

region = gcp_config.get("region") or os.getenv("GCP_REGION") or "asia-southeast2"

# Stack configuration parameters
release_version = config.get("release_version") or "1.5.0"
image_tag = config.get("image_tag") or "latest"
web_min_instances = config.get_int("web_min_instances") or 0
web_max_instances = config.get_int("web_max_instances") or 5
worker_min_instances = config.get_int("worker_min_instances") or 0
worker_max_instances = config.get_int("worker_max_instances") or 5

# Explicit GCP Provider passing resolved project and region dynamically
gcp_provider = gcp.Provider("gcp-provider", project=project, region=region)

# 1. Cloud Storage Media Bucket
media_bucket = gcp.storage.Bucket(
    "korda-media-bucket",
    name=f"{project}-media-korda",
    location=region,
    project=project,
    uniform_bucket_level_access=True,
    versioning=gcp.storage.BucketVersioningArgs(enabled=False),
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
)

# Least-privilege IAM bindings
iam_roles = [
    "roles/aiplatform.user",  # Vertex AI / Gemini 2.5
    "roles/secretmanager.secretAccessor",  # Secret Manager Access
    "roles/cloudtasks.enqueuer",  # Enqueue to Cloud Tasks
    "roles/run.invoker",  # Invoke Cloud Run internal endpoints
]

iam_members = []
for idx, role in enumerate(iam_roles):
    member = gcp.projects.IAMMember(
        f"korda-iam-{idx}",
        project=project,
        role=role,
        member=service_account.email.apply(lambda email: f"serviceAccount:{email}"),
    )
    iam_members.append(member)

# Grant service account access to the media bucket
gcp.storage.BucketIAMMember(
    "korda-bucket-iam",
    bucket=media_bucket.name,
    role="roles/storage.objectAdmin",
    member=service_account.email.apply(lambda email: f"serviceAccount:{email}"),
)

# 4. Container Image Reference
container_image = f"{region}-docker.pkg.dev/{project}/cloud-run-source-deploy/korda:{image_tag}"

# Common Environment Variables
common_env = [
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="RELEASE_VERSION", value=release_version),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="DJANGO_DEBUG", value="False"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GCP_PROJECT_ID", value=project),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GCP_REGION", value=region),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GCP_QUEUE_NAME", value=tasks_queue.name),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GS_BUCKET_NAME", value=media_bucket.name),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="SURREAL_NS", value="korda"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="SURREAL_DB", value="extractor"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="SURREALDB_OFFLINE", value="False"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GEMINI_MODEL", value="gemini-2.5-flash"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GEMINI_MODEL_BATCH", value="gemini-2.5-flash-lite"),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="CLOUD_TASKS_SERVICE_ACCOUNT", value=service_account.email),
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="CLOUD_TASKS_QUEUE", value=queue_name),
]

# Standard Secret Manager Volume / Environment Bindings
secret_keys = [
    "DJANGO_SECRET_KEY",
    "GEMINI_API_KEY",
    "SURREAL_URL",
    "SURREAL_USER",
    "SURREAL_PASS",
    "ADMIN_EMAIL",
    "SUPABASE_URL",
    "SUPABASE_PUBLIC_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "CF_TURNSTILE_SITE_KEY",
]

secret_envs = [
    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
        name=key,
        value_source=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceArgs(
            secret_key_ref=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceSecretKeyRefArgs(
                secret=key,
                version="latest",
            )
        ),
    )
    for key in secret_keys
]

# 5. Cloud Run Worker Service (korda-worker)
worker_service = gcp.cloudrunv2.Service(
    "korda-worker",
    name="korda-worker",
    location=region,
    project=project,
    deletion_protection=False,
    ingress="INGRESS_TRAFFIC_ALL",
    template=gcp.cloudrunv2.ServiceTemplateArgs(
        service_account=service_account.email,
        timeout="900s",
        scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(
            min_instance_count=worker_min_instances,
            max_instance_count=worker_max_instances,
        ),
        containers=[
            gcp.cloudrunv2.ServiceTemplateContainerArgs(
                image=container_image,
                name="worker",
                envs=common_env
                + secret_envs
                + [
                    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GUNICORN_WORKERS", value="1"),
                ],
                resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                    limits={"cpu": "2000m", "memory": "2Gi"},
                ),
            )
        ],
    ),
    opts=pulumi.ResourceOptions(provider=gcp_provider, depends_on=iam_members),
)

# 6. Cloud Run Web Service (korda-web)
web_service = gcp.cloudrunv2.Service(
    "korda-web",
    name="korda-web",
    location=region,
    project=project,
    deletion_protection=False,
    ingress="INGRESS_TRAFFIC_ALL",
    template=gcp.cloudrunv2.ServiceTemplateArgs(
        service_account=service_account.email,
        timeout="900s",
        scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(
            min_instance_count=web_min_instances,
            max_instance_count=web_max_instances,
        ),
        containers=[
            gcp.cloudrunv2.ServiceTemplateContainerArgs(
                image=container_image,
                name="web",
                envs=common_env
                + secret_envs
                + [
                    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="GUNICORN_WORKERS", value="2"),
                    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name="WORKER_URL", value=worker_service.uri),
                ],
                resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                    limits={"cpu": "1000m", "memory": "1Gi"},
                ),
            )
        ],
    ),
    opts=pulumi.ResourceOptions(provider=gcp_provider, depends_on=iam_members),
)

# Public access IAM policy for korda-web
gcp.cloudrunv2.ServiceIamMember(
    "korda-web-public-access",
    name=web_service.name,
    location=region,
    project=project,
    role="roles/run.invoker",
    member="allUsers",
    opts=pulumi.ResourceOptions(provider=gcp_provider),
)

# Stack Outputs
pulumi.export("web_url", web_service.uri)
pulumi.export("worker_url", worker_service.uri)
pulumi.export("bucket_name", media_bucket.name)
pulumi.export("service_account_email", service_account.email)
pulumi.export("queue_name", tasks_queue.name)
