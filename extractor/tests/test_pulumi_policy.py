import importlib.util
import sys
import types
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


def _load_policy_module():
    # Stub minimal pulumi_policy symbols so tests run without requiring pulumi-policy in app venv.
    stub = types.ModuleType("pulumi_policy")

    class _EnforcementLevel:
        MANDATORY = "mandatory"

    class _PolicyPack:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _ResourceValidationPolicy:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    stub.EnforcementLevel = _EnforcementLevel
    stub.PolicyPack = _PolicyPack
    stub.ReportViolation = object
    stub.ResourceValidationArgs = object
    stub.ResourceValidationPolicy = _ResourceValidationPolicy

    policy_path = Path(__file__).resolve().parents[2] / "infra" / "pulumi" / "policy" / "__main__.py"
    spec = importlib.util.spec_from_file_location("korda_pulumi_policy", policy_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    with patch.dict(sys.modules, {"pulumi_policy": stub}):
        spec.loader.exec_module(mod)
    return mod


class CrossGuardPolicyTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.policy = _load_policy_module()

    def test_ephemeral_bucket_rejects_soft_delete_and_versioning(self):
        violations = []
        args = types.SimpleNamespace(
            name="bad-bucket",
            resource_type="gcp:storage/bucket:Bucket",
            props={
                "name": "proj-media-korda",
                "softDeletePolicy": {"retentionDurationSeconds": 604800},
                "versioning": {"enabled": True},
                "uniformBucketLevelAccess": False,
            },
        )
        self.policy.validate_ephemeral_bucket(args, violations.append)
        self.assertEqual(len(violations), 3)

    def test_ephemeral_bucket_passes_when_compliant(self):
        violations = []
        args = types.SimpleNamespace(
            name="good-bucket",
            resource_type="gcp:storage/bucket:Bucket",
            props={
                "name": "proj-media-korda",
                "softDeletePolicy": {"retentionDurationSeconds": 0},
                "versioning": {"enabled": False},
                "uniformBucketLevelAccess": True,
            },
        )
        self.policy.validate_ephemeral_bucket(args, violations.append)
        self.assertEqual(violations, [])

    def test_cloud_run_rejects_nonzero_min_instances(self):
        violations = []
        args = types.SimpleNamespace(
            name="korda-worker",
            resource_type="gcp:cloudrunv2/service:Service",
            props={"name": "korda-worker", "template": {"scaling": {"minInstanceCount": 1}}},
        )
        self.policy.validate_scale_to_zero_cloud_run(args, violations.append)
        self.assertEqual(len(violations), 1)

    def test_private_worker_rejects_all_users(self):
        violations = []
        args = types.SimpleNamespace(
            name="worker-iam",
            resource_type="gcp:cloudrunv2/serviceIamMember:ServiceIamMember",
            props={"name": "korda-worker", "member": "allUsers"},
        )
        self.policy.validate_private_worker_iam(args, violations.append)
        self.assertEqual(len(violations), 1)
