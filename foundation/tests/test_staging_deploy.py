"""Unit tests for Task A15: CI pipeline, Docker staging, seeded pilot-branch data (FD-1).

Verifies the Done Condition: 'One command deploys staging',
covering:
1. One-command staging deployment (`StagingDeployer.deploy_staging` & CLI `main`)
   seeding all 3 pilot branches (`1`, `2`, `3`) and passing end-to-end smoke checks.
2. Idempotent seeding: running `deploy_staging()` or `seed_all()` multiple times
   never duplicates branches, suppliers, or inventory rows.
3. Docker & GitHub Actions CI manifest generation and validation (`Dockerfile`,
   `docker-compose.staging.yml`, and `.github/workflows/ci.yml`).
"""
import tempfile
import unittest
from pathlib import Path

from foundation.staging_deploy import (
    DOCKERFILE_CONTENT,
    DOCKER_COMPOSE_STAGING_CONTENT,
    GITHUB_CI_WORKFLOW_CONTENT,
    PilotBranchSeeder,
    StagingDeployer,
    main as staging_cli_main,
)


class TestStagingDeployA15(unittest.TestCase):
    def test_one_command_deploys_and_verifies_staging(self) -> None:
        """Task A15 Done Condition: One command deploys staging and verifies all 3 pilot branches."""
        deployer = StagingDeployer()
        result = deployer.deploy_staging()

        self.assertEqual(result.status, "DEPLOYED_AND_VERIFIED")
        self.assertEqual(result.environment, "staging")
        self.assertEqual(result.timezone, "Africa/Cairo")
        self.assertTrue(result.smoke_test_passed)
        self.assertEqual(result.verified_branches, (1, 2, 3))
        self.assertEqual(result.seed_summary.branches_count, 3)
        self.assertEqual(result.seed_summary.suppliers_count, 3)
        self.assertEqual(result.seed_summary.inventory_count, 3)

        # Verify CLI one-command entrypoint returns exit code 0
        exit_code = staging_cli_main(["--verify-only"])
        self.assertEqual(exit_code, 0)

    def test_pilot_branch_seeding_is_idempotent(self) -> None:
        """Re-running seed_all() or deploy_staging() never creates duplicate pilot records."""
        seeder = PilotBranchSeeder()
        first = seeder.seed_all()
        second = seeder.seed_all()

        self.assertEqual(first, second)
        self.assertEqual(len(seeder.branches), 3)
        self.assertEqual(len(seeder.suppliers), 3)
        self.assertEqual(len(seeder.inventory), 3)

    def test_docker_and_ci_manifests_are_valid_and_materialize_cleanly(self) -> None:
        """Verifies Dockerfile, docker-compose.staging.yml, and .github/workflows/ci.yml."""
        self.assertIn("FROM python:3.12-slim", DOCKERFILE_CONTENT)
        self.assertIn("TZ=Africa/Cairo", DOCKERFILE_CONTENT)
        self.assertIn("postgres-staging:", DOCKER_COMPOSE_STAGING_CONTENT)
        self.assertIn("retail-agents-staging:", DOCKER_COMPOSE_STAGING_CONTENT)
        self.assertIn("001_shared_foundation_schema.sql", DOCKER_COMPOSE_STAGING_CONTENT)
        self.assertIn("python -m unittest discover -s foundation/tests", GITHUB_CI_WORKFLOW_CONTENT)

        with tempfile.TemporaryDirectory() as tmpdir:
            deployer = StagingDeployer()
            res = deployer.deploy_staging(project_root=tmpdir, write_manifests=True)
            self.assertEqual(len(res.manifests_written), 3)
            self.assertTrue((Path(tmpdir) / "Dockerfile").exists())
            self.assertTrue((Path(tmpdir) / "docker-compose.staging.yml").exists())
            self.assertTrue((Path(tmpdir) / ".github" / "workflows" / "ci.yml").exists())


if __name__ == "__main__":
    unittest.main()
