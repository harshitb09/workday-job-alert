import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "poll_workday.py"

# Stub third-party imports so the module can be imported in this minimal test env.
requests_stub = types.ModuleType("requests")
requests_stub.request = lambda *args, **kwargs: None
requests_stub.post = lambda *args, **kwargs: None
sys.modules.setdefault("requests", requests_stub)

yaml_stub = types.ModuleType("yaml")
yaml_stub.safe_load = lambda stream: {}
sys.modules.setdefault("yaml", yaml_stub)

spec = importlib.util.spec_from_file_location("poll_workday", MODULE_PATH)
poll_workday = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poll_workday)


class PollWorkdayTests(unittest.TestCase):
    def test_matches_location_filters_usa_and_germany(self):
        self.assertTrue(poll_workday.matches_location("San Francisco, CA, United States", ["united states", "germany"]))
        self.assertTrue(poll_workday.matches_location("Berlin, Germany", ["united states", "germany"]))
        self.assertFalse(poll_workday.matches_location("Toronto, Canada", ["united states", "germany"]))

    def test_backfill_mode_seeds_state_without_alerting(self):
        company = {
            "name": "Example",
            "ats": "greenhouse",
            "board_token": "example",
            "keywords": ["software engineer"],
            "locations": ["germany"],
        }
        config = {"companies": [company], "page_size": 20, "max_jobs_per_company": 300}
        initial_state = {"greenhouse::example": ["old-id"]}
        postings = [{
            "id": "new-id",
            "title": "Software Engineer",
            "url": "https://example.test/jobs/new-id",
            "posted": "2026-01-01T00:00:00Z",
            "location": "Berlin, Germany",
        }]

        with mock.patch.object(poll_workday, "load_config", return_value=config), \
             mock.patch.object(poll_workday, "load_state", return_value=initial_state), \
             mock.patch.object(poll_workday, "save_state") as save_state, \
             mock.patch.object(poll_workday, "fetch_jobs", return_value=postings), \
             mock.patch.object(poll_workday, "send_discord_alert", return_value=True) as send_alert:
            poll_workday.main(backfill=True)

        saved_state = save_state.call_args[0][0]
        self.assertIn("new-id", saved_state["greenhouse::example"])
        self.assertIn("old-id", saved_state["greenhouse::example"])
        send_alert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
