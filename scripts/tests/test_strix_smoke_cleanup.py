"""Fallos de create/start y post-start no dejan recursos del smoke."""

from __future__ import annotations

import logging
import unittest

from scripts.strix_smoke_cleanup import SmokeCleanup, SmokeCleanupError


class FakeDocker:
    def __init__(self) -> None:
        self.containers: set[str] = set()
        self.networks: set[str] = {"buildx_buildkit_desktop-linux"}
        self.events: list[str] = []
        self.transient_remove_failures = 0

    def inventory(self) -> dict[str, set[str]]:
        return {"containers": set(self.containers), "networks": set(self.networks)}

    def create_network(self) -> str:
        self.networks.add("fenix-team-strix-test")
        return "fenix-team-strix-test"

    def create_container(self) -> str:
        self.containers.add("fenix-strix-smoke-created")
        return "fenix-strix-smoke-created"

    def remove_container(self, container_id: str) -> None:
        self.events.append(f"container:{container_id}")
        if self.transient_remove_failures:
            self.transient_remove_failures -= 1
            raise OSError("Docker ocupado")
        self.containers.discard(container_id)

    def remove_network(self, network_id: str) -> None:
        self.events.append(f"network:{network_id}")
        self.networks.discard(network_id)


class CleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.docker = FakeDocker()
        self.scope = SmokeCleanup(self.docker, sleep=lambda _: None)

    def created(self) -> None:
        self.scope.track_network(self.docker.create_network())
        self.scope.track_container(self.docker.create_container())

    def assert_clean(self) -> None:
        report = self.scope.report
        self.assertIsNotNone(report)
        if report is None:
            self.fail("El smoke no generó inventario final")
        self.assertEqual(report.as_dict()["CLEANUP"], "PASS")
        self.assertEqual(report.as_dict()["ORPHANS"], {"containers": [], "networks": []})
        self.assertEqual(self.docker.containers, set())
        self.assertEqual(self.docker.networks, {"buildx_buildkit_desktop-linux"})
        self.assertEqual(report.resources_before["networks"], {"buildx_buildkit_desktop-linux"})
        self.assertEqual(report.resources_created["containers"], {"fenix-strix-smoke-created"})
        self.assertEqual(report.resources_removed["containers"], {"fenix-strix-smoke-created"})
        self.assertEqual(self.docker.events[-2:], [
            "container:fenix-strix-smoke-created",
            "network:fenix-team-strix-test",
        ])

    def test_start_deny_limpia_created_y_network(self) -> None:
        with self.assertRaisesRegex(PermissionError, "DENY"):
            with self.scope:
                self.created()
                raise PermissionError("start DENY")
        self.assert_clean()

    def test_excepcion_inmediata_tras_create(self) -> None:
        with self.assertRaisesRegex(ValueError, "parse"):
            with self.scope:
                self.created()
                raise ValueError("parse falló")
        self.assert_clean()

    def test_create_sin_id_legible_se_recupera_por_inventario(self) -> None:
        with self.assertRaisesRegex(ValueError, "respuesta ilegible"):
            with self.scope:
                self.scope.track_network(self.docker.create_network())
                self.docker.create_container()
                raise ValueError("respuesta ilegible")
        self.assert_clean()

    def test_error_posterior_a_start(self) -> None:
        with self.assertRaisesRegex(AssertionError, "test FAIL"):
            with self.scope:
                self.created()
                started = True
                self.assertTrue(started)
                raise AssertionError("test FAIL")
        self.assert_clean()

    def test_error_transitorio_registra_y_reintenta(self) -> None:
        self.created()
        self.docker.transient_remove_failures = 1
        with self.assertLogs("scripts.strix_smoke_cleanup", logging.ERROR) as logs:
            report = self.scope.close()
        self.assertIn("intento 1", " ".join(logs.output))
        self.assertEqual(report.as_dict()["CLEANUP"], "PASS")
        self.assertEqual(len([e for e in self.docker.events if e.startswith("container:")]), 2)
        self.assertEqual(self.docker.events[-1], "network:fenix-team-strix-test")

    def test_error_persistente_no_declara_pass_ni_elimina_red_antes(self) -> None:
        self.created()
        self.docker.transient_remove_failures = 3
        with self.assertRaises(SmokeCleanupError):
            self.scope.close()
        self.assertEqual(self.scope.report.as_dict()["CLEANUP"], "FAIL")
        self.assertEqual(
            self.scope.report.as_dict()["ORPHANS"],
            {
                "containers": ["fenix-strix-smoke-created"],
                "networks": ["fenix-team-strix-test"],
            },
        )
        self.assertFalse(any(event.startswith("network:") for event in self.docker.events))


if __name__ == "__main__":
    unittest.main()
