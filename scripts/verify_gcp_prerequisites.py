#!/usr/bin/env python3
"""Verify deployment IAM and storage contracts using metadata-only GCP reads."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # nosec B404 # Metadata checks require bounded CLI execution without a shell.
import sys
from typing import TypedDict, cast


class IamBinding(TypedDict, total=False):
    role: str
    members: list[str]
    condition: dict[str, str]


class Metadata(TypedDict, total=False):
    email: str
    disabled: bool
    bindings: list[IamBinding]


class PreflightError(RuntimeError):
    """A required deployment contract could not be confirmed."""


def _metadata(project: str, *arguments: str) -> Metadata:
    executable = shutil.which("gcloud")
    if executable is None:
        raise PreflightError("gcloud is required for deployment metadata checks.")
    command = [executable, *arguments, f"--project={project}", "--format=json"]
    try:
        result = subprocess.run(  # nosec B603 # Resolved gcloud executable; identifiers remain separate arguments.
            command, capture_output=True, text=True, timeout=30, check=False
        )
    except OSError as exc:
        raise PreflightError("gcloud could not be executed.") from exc
    except subprocess.SubprocessError as exc:
        raise PreflightError("The GCP metadata lookup did not complete.") from exc
    if result.returncode:
        raise PreflightError("Required GCP metadata is unavailable; check resource existence and metadata permissions.")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise PreflightError("GCP metadata is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise PreflightError("GCP metadata must be an object.")
    return cast(Metadata, payload)


def _require_binding(policy: Metadata, role: str, member: str, resource: str) -> None:
    bindings = policy.get("bindings", [])
    if not isinstance(bindings, list):
        raise PreflightError(f"Invalid IAM bindings on {resource}.")
    for binding in bindings:
        if not isinstance(binding, dict) or "condition" in binding:
            continue
        members = binding.get("members", [])
        if binding.get("role") == role and isinstance(members, list) and member in members:
            return
    raise PreflightError(f"Missing unconditional {role} for {member} on {resource}.")


def verify(project: str, project_number: str, region: str, queue_region: str, queue: str) -> None:
    """Require the Pulumi-managed identities and their resource-scoped bindings."""
    if not project_number.isdigit():
        raise PreflightError("A numeric GCP project number is required.")
    runtime = f"korda-runtime@{project}.iam.gserviceaccount.com"
    invoker = f"korda-tasks-invoker@{project}.iam.gserviceaccount.com"
    runtime_member = f"serviceAccount:{runtime}"
    invoker_member = f"serviceAccount:{invoker}"
    agent_member = f"serviceAccount:service-{project_number}@gcp-sa-cloudtasks.iam.gserviceaccount.com"
    bucket = f"gs://{project}-media-korda"

    _metadata(project, "storage", "buckets", "describe", bucket)
    for account in (runtime, invoker):
        account_metadata = _metadata(project, "iam", "service-accounts", "describe", account)
        if account_metadata.get("email") != account or account_metadata.get("disabled"):
            raise PreflightError(f"Deployment service account is disabled or unconfirmed: {account}.")
    _require_binding(
        _metadata(project, "storage", "buckets", "get-iam-policy", bucket),
        "roles/storage.objectAdmin",
        runtime_member,
        "media bucket",
    )
    invoker_policy = _metadata(project, "iam", "service-accounts", "get-iam-policy", invoker)
    for principal in (runtime_member, agent_member):
        _require_binding(invoker_policy, "roles/iam.serviceAccountUser", principal, "task OIDC identity")
    _require_binding(
        _metadata(project, "run", "services", "get-iam-policy", "korda-worker", f"--region={region}"),
        "roles/run.invoker",
        invoker_member,
        "worker service",
    )
    _require_binding(
        _metadata(project, "tasks", "queues", "get-iam-policy", queue, f"--location={queue_region}"),
        "roles/cloudtasks.taskDeleter",
        runtime_member,
        "application queue",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--project-number", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--queue-region", required=True)
    parser.add_argument("--queue", default="extractor-tasks-v2")
    args = parser.parse_args(argv)
    try:
        verify(args.project, args.project_number, args.region, args.queue_region, args.queue)
    except PreflightError as exc:
        print(f"Deployment preflight failed: {exc}", file=sys.stderr)
        return 1
    print("Storage, task OIDC, worker invocation, and queue cancellation contracts verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
