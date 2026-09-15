from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.test_transition_recorder import observation, event
from sap_explorer.analyzer import analyze, write_report
from sap_explorer.models import SapTransition
from sap_explorer.repository import SapRepository


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.repo = SapRepository(self.path, timeout=0.01)
        self.run_id = self.repo.start_run({"connection_index": 0, "session_index": 0})

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def transition(self, before=None, after=None):
        before, after = before or observation(), after or observation(200)
        self.repo.save_observation(self.run_id, before)
        action = event()
        action_id = self.repo.save_event(self.run_id, action, before)
        transition = SapTransition(before, (action,), after, (action_id,))
        self.repo.save_transition(self.run_id, transition)
        return transition

    def test_screen_definition_and_transition_deduplicate_across_runs(self):
        for _ in range(2):
            self.transition()
            self.repo.finish_run(self.run_id, "test")
            self.run_id = self.repo.start_run({})
        data = self.repo.export()
        self.assertEqual(2, len(data["states"]))
        self.assertEqual(1, len(data["transitions"]))
        self.assertEqual(2, data["transitions"][0]["count"])
        self.assertEqual(4, len(data["observations"]))
        self.assertEqual(0, self.repo.stats()["new_states_last_run"])

    def test_idempotent_transition_save_does_not_inflate_counts(self):
        transition = self.transition()
        self.repo.save_transition(self.run_id, transition)
        self.assertEqual(1, self.repo.export()["transitions"][0]["count"])
        self.assertEqual(2, self.repo.stats()["observations"])

    def test_original_commands_and_before_observation_are_persisted(self):
        transition = self.transition()
        data = self.repo.export()
        self.assertEqual(transition.before_state.id, data["actions"][0]["before_observation"])
        self.assertEqual("press", data["actions"][0]["event"]["commands"][0]["member_name"])
        self.assertEqual(list(transition.action_ids), data["history"][0]["action_ids"])

    def test_incomplete_transition_is_retained_outside_map(self):
        self.repo.save_transition(self.run_id, SapTransition(observation(), (), None, status="incomplete", reason="disconnect"))
        self.assertEqual(0, self.repo.stats()["transitions"])
        self.assertEqual(1, self.repo.stats()["incomplete_transitions"])

    def test_transaction_rolls_back_observation_on_failure(self):
        self.repo.connection.execute("""CREATE TRIGGER fail_transition BEFORE INSERT ON transition_observations
            BEGIN SELECT RAISE(ABORT, 'simulated full disk'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.save_transition(self.run_id, SapTransition(observation(), (), observation(200)))
        self.assertEqual(0, self.repo.stats()["screens"])
        self.assertEqual(0, self.repo.stats()["observations"])
        self.assertEqual(0, self.repo.stats()["transitions"])

    def test_lock_failure_is_explicit_and_does_not_write_partial_data(self):
        other = sqlite3.connect(self.path)
        try:
            other.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                self.repo.save_observation(self.run_id, observation())
        finally:
            other.rollback()
            other.close()
        self.assertEqual(0, self.repo.stats()["screens"])

    def test_data_survives_reopening_with_unfinished_run_detectable(self):
        self.transition()
        with SapRepository(self.path) as reopened:
            self.assertEqual(1, reopened.stats()["unfinished_runs"])
            self.assertEqual(1, reopened.stats()["actions"])

    def test_analysis_reports_branch_frequencies_rare_path_popup_and_dead_end(self):
        for _ in range(24):
            self.transition()
        popup = observation(200, popup=True)
        self.transition(after=popup)
        report = analyze(self.repo)
        self.assertEqual(1, len(report["multiple_outcomes"]))
        self.assertEqual(0.04, report["rare_paths"][0]["frequency"])
        self.assertEqual([popup.fingerprint.structural_hash], report["popups"])
        self.assertIn(popup.fingerprint.structural_hash, report["states_without_exit"])
        write_report(self.repo, self.temp.name)
        self.assertTrue((Path(self.temp.name) / "exploration_report.json").is_file())
        self.assertIn("4.0%", (Path(self.temp.name) / "exploration_report.md").read_text(encoding="utf-8"))

    def test_empty_database_generates_valid_report(self):
        report = write_report(self.repo, self.temp.name)
        self.assertEqual([], report["paths"])
