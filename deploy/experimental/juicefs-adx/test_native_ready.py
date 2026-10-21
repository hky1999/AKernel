import unittest

from native_ready import NotReady, admission_ready, empty_page, services_ready


class NativeReadyTests(unittest.TestCase):
    def page(self):
        return {"count": 0, "instanceInfos": [], "node": {
            "node_id": "a0-source", "accepting_allocations": True, "scheduling_paused": False}}

    def status(self):
        return {"ok": True, "services": [
            {"id": role, "role": role, "pid": 100 + i, "restarts": 0, "failed": False}
            for i, role in enumerate(("redis", "coordinator", "apiserver", "adxlet"))]}

    def test_accepts_registered_empty_node_and_live_services(self):
        self.assertTrue(admission_ready(self.page(), "source")["accepting_allocations"])
        self.assertEqual(len(services_ready(self.status(), "source")), 4)

    def test_rejects_closed_admission_even_with_a_live_pid(self):
        page = self.page()
        page["node"]["accepting_allocations"] = False
        with self.assertRaises(NotReady):
            admission_ready(page, "source")

    def test_rejects_paused_or_wrong_node(self):
        for key, value in (("scheduling_paused", True), ("node_id", "a0-target")):
            page = self.page()
            page["node"][key] = value
            with self.assertRaises(NotReady):
                admission_ready(page, "source")

    def test_rejects_pending_or_inconsistent_initial_instances(self):
        self.assertFalse(empty_page({"count": 1, "instanceInfos": []}))
        self.assertFalse(empty_page({"count": 0, "instanceInfos": [{}]}))
        self.assertFalse(empty_page({"count": 0}))

    def test_rejects_restart_stopped_or_failed_service(self):
        for key, value in (("restarts", 1), ("pid", None), ("pid", 0), ("failed", True)):
            status = self.status()
            status["services"][-1][key] = value
            with self.assertRaises(NotReady):
                services_ready(status, "source")

    def test_rejects_missing_required_service(self):
        status = self.status()
        status["services"].pop()
        with self.assertRaises(NotReady):
            services_ready(status, "source")


if __name__ == "__main__":
    unittest.main()
