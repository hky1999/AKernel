#!/usr/bin/env python3

"""Static cross-provider contract for guided Agent DX deployments."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent


class AdxTerraformContractTest(unittest.TestCase):
    def test_each_provider_exposes_adx_control_and_data_directly(self) -> None:
        for provider in ("aliyun", "huaweicloud"):
            with self.subTest(provider=provider):
                values = (ROOT / provider / "values-akernel.yaml.tmpl").read_text()
                self.assertIn("publicService:", values)
                self.assertIn("enabled: ${adx_public_service_enabled}", values)
                self.assertIn("controlPort: ${adx_control_port}", values)
                self.assertIn("dataPort: ${adx_data_port}", values)
                self.assertNotIn("traefik", values.lower())
                main = (ROOT / provider / "main.tf").read_text()
                variables = (ROOT / provider / "variables.tf").read_text()
                self.assertNotIn("traefik", main.lower())
                self.assertNotIn("traefik", variables.lower())

    def test_each_provider_prepares_identity_before_helm(self) -> None:
        for provider in ("aliyun", "huaweicloud"):
            with self.subTest(provider=provider):
                main = (ROOT / provider / "main.tf").read_text()
                self.assertIn('resource "null_resource" "ensure_adx_secret"', main)
                self.assertIn("../../scripts/ensure-adx-secret.sh", main)
                helm = main[main.index('resource "helm_release" "akernel_core"') :]
                self.assertIn("null_resource.ensure_adx_secret", helm)

    def test_each_provider_renders_the_adx_topology(self) -> None:
        for provider in ("aliyun", "huaweicloud"):
            with self.subTest(provider=provider):
                values = (ROOT / provider / "values-akernel.yaml.tmpl").read_text()
                self.assertIn("adx:", values)
                self.assertIn("  coordinator:\n    replicas: 1", values)
                self.assertIn("  ingressApi:\n    replicas: 1", values)
                self.assertIn('repository: "${master_image_repository}"', values)
                self.assertIn('tag: "${master_image_tag}"', values)
                self.assertIn(
                    'schedulePlacementPolicy: "${schedule_placement_policy}"', values
                )
                self.assertIn("existingSecret: akernel-adx-tls", values)
                self.assertIn("mode: managed", values)
                self.assertIn("appendfsync: everysec", values)
                self.assertIn("replicas: 1", values)


if __name__ == "__main__":
    unittest.main()
