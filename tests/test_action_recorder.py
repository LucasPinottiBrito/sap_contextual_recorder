from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sap_explorer.action_recorder import (  # noqa: E402
    SapActionRecorder,
    SapEventBindingError,
)


NOW = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)


class FakeInfo:
    def __init__(self, recording_disabled: bool = False) -> None:
        self.ScriptingModeRecordingDisabled = recording_disabled


class FakeSession:
    def __init__(self, *, record: bool = False, recording_disabled: bool = False) -> None:
        self.Id = "/app/con[0]/ses[0]"
        self.Busy = False
        self.Info = FakeInfo(recording_disabled)
        self._record = record
        self.record_writes: list[bool] = []

    @property
    def Record(self) -> bool:
        return self._record

    @Record.setter
    def Record(self, value: bool) -> None:
        self.record_writes.append(bool(value))
        self._record = bool(value)


class IgnoredRecordSession(FakeSession):
    @property
    def Record(self) -> bool:
        return False

    @Record.setter
    def Record(self, value: bool) -> None:
        self.record_writes.append(bool(value))


class DisconnectedSession(FakeSession):
    def __init__(self) -> None:
        self.Info = FakeInfo(False)
        self.Busy = False
        self._record = False
        self.record_writes: list[bool] = []

    @property
    def Id(self) -> str:
        raise OSError("SAP session disconnected")


class FakeSink:
    def __init__(self, handler_class: type) -> None:
        self.handler = handler_class()
        self.closed = False

    def configure(self, consumer, *, sanitizer, clock) -> None:
        self.handler.configure(consumer, sanitizer=sanitizer, clock=clock)

    def close(self) -> None:
        self.closed = True


class FakeBinding:
    def __init__(self) -> None:
        self.sink: FakeSink | None = None

    def __call__(self, _session, handler_class):
        self.sink = FakeSink(handler_class)
        return self.sink


def make_recorder(session, received, binding, **overrides) -> SapActionRecorder:
    return SapActionRecorder(
        session,
        received.append,
        event_binder=binding,
        message_pump=overrides.pop("message_pump", lambda: None),
        co_initialize=overrides.pop("co_initialize", lambda: None),
        co_uninitialize=overrides.pop("co_uninitialize", lambda: None),
        clock=lambda: NOW,
        **overrides,
    )


class SapActionRecorderTests(unittest.TestCase):
    def test_final_change_is_delivered_before_sink_is_closed(self) -> None:
        binding = FakeBinding()
        received = []
        class FlushSession(FakeSession):
            @FakeSession.Record.setter
            def Record(self, value):
                self._record = value
                if not value:
                    self.assert_sink_open = not binding.sink.closed
                    binding.sink.handler.OnChange(self, None, ("M", "press", ()))
        session = FlushSession()
        recorder = make_recorder(session, received, binding)
        recorder.start()
        recorder.stop()
        self.assertTrue(session.assert_sink_open)
        self.assertEqual("Change", received[0].event_type)
        self.assertTrue(binding.sink.closed)

    def test_persistence_failure_propagates_and_still_releases_sink(self) -> None:
        session, binding = FakeSession(), FakeBinding()
        lifecycle = []
        def fail(event):
            raise OSError("disk full")
        def pump():
            if not lifecycle:
                lifecycle.append("emitted")
                binding.sink.handler.OnChange(session, None, ("M", "press", ()))
        recorder = SapActionRecorder(session, fail, event_binder=binding, message_pump=pump,
                                     co_initialize=lambda: None, co_uninitialize=lambda: lifecycle.append("closed"),
                                     propagate_consumer_errors=True)
        with self.assertRaises(OSError):
            recorder.run()
        self.assertTrue(binding.sink.closed)
        self.assertEqual("closed", lifecycle[-1])
        self.assertEqual("consumer_error", recorder.stop_reason)

    def test_liveness_checks_even_after_missing_end_request(self) -> None:
        binding = FakeBinding()
        recorder = make_recorder(DisconnectedSession(), [], binding)
        recorder.start()
        binding.sink.handler.OnStartRequest(None)
        recorder.drain_events()
        self.assertFalse(recorder._session_is_alive())
        recorder.stop()

    def test_activates_record_and_restores_it_after_detaching_listener(self) -> None:
        session = FakeSession(record=False)
        received: list = []
        binding = FakeBinding()
        recorder = make_recorder(session, received, binding)

        capabilities = recorder.start()
        self.assertTrue(capabilities.change_events_expected)
        self.assertTrue(capabilities.recording_activated_by_recorder)
        self.assertEqual([True], session.record_writes)

        recorder.stop()

        assert binding.sink is not None
        self.assertTrue(binding.sink.closed)
        self.assertEqual([True, False], session.record_writes)

    def test_does_not_disable_recording_that_was_already_active(self) -> None:
        session = FakeSession(record=True)
        binding = FakeBinding()
        recorder = make_recorder(session, [], binding)

        capabilities = recorder.start()
        recorder.stop()

        self.assertTrue(capabilities.recording_was_active)
        self.assertFalse(capabilities.recording_activated_by_recorder)
        self.assertEqual([], session.record_writes)

    def test_recording_disabled_still_attaches_listener_without_writing_record(self) -> None:
        session = FakeSession(record=False, recording_disabled=True)
        binding = FakeBinding()
        recorder = make_recorder(session, [], binding)

        with self.assertLogs("sap_explorer.action_recorder", level="WARNING"):
            capabilities = recorder.start()
        recorder.stop()

        self.assertTrue(capabilities.events_attached)
        self.assertFalse(capabilities.change_events_expected)
        self.assertTrue(capabilities.recording_disabled_by_server)
        self.assertEqual([], session.record_writes)

    def test_detects_when_record_setter_is_silently_ignored(self) -> None:
        session = IgnoredRecordSession()
        binding = FakeBinding()
        recorder = make_recorder(session, [], binding)

        with self.assertLogs("sap_explorer.action_recorder", level="WARNING"):
            capabilities = recorder.start()
        recorder.stop()

        self.assertFalse(capabilities.change_events_expected)
        self.assertFalse(capabilities.recording_activated_by_recorder)
        self.assertEqual([True], session.record_writes)

    def test_simulated_callback_is_delivered_outside_sink(self) -> None:
        session = FakeSession()
        received: list = []
        binding = FakeBinding()
        recorder = make_recorder(session, received, binding)
        recorder.start()
        assert binding.sink is not None

        binding.sink.handler.OnStartRequest(session)

        self.assertEqual([], received)
        self.assertEqual(1, recorder.drain_events())
        self.assertEqual("StartRequest", received[0].event_type)
        recorder.stop()

    def test_destroy_event_requests_clean_stop(self) -> None:
        session = FakeSession()
        received: list = []
        binding = FakeBinding()
        recorder = make_recorder(session, received, binding)
        recorder.start()
        assert binding.sink is not None

        binding.sink.handler.OnDestroy(session)
        recorder.drain_events()

        self.assertEqual("session_destroyed", recorder.stop_reason)
        recorder.stop()

    def test_transient_message_pump_error_does_not_crash_run(self) -> None:
        session = FakeSession()
        binding = FakeBinding()
        calls = 0
        recorder: SapActionRecorder

        def pump() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary")
            recorder.request_stop()

        recorder = make_recorder(
            session,
            [],
            binding,
            message_pump=pump,
            pump_interval_seconds=0.001,
        )

        with self.assertLogs("sap_explorer.action_recorder", level="WARNING"):
            recorder.run()

        self.assertGreaterEqual(calls, 2)
        self.assertFalse(recorder.is_started)

    def test_binding_failure_releases_com_initialization(self) -> None:
        lifecycle: list[str] = []

        def broken_binder(_session, _handler):
            raise TypeError("type library unavailable")

        recorder = SapActionRecorder(
            FakeSession(),
            lambda _: None,
            event_binder=broken_binder,
            message_pump=lambda: None,
            co_initialize=lambda: lifecycle.append("init"),
            co_uninitialize=lambda: lifecycle.append("uninit"),
        )

        with self.assertRaises(SapEventBindingError):
            recorder.start()

        self.assertEqual(["init", "uninit"], lifecycle)

    def test_repeated_liveness_failures_end_disconnected_session(self) -> None:
        binding = FakeBinding()
        recorder = make_recorder(
            DisconnectedSession(),
            [],
            binding,
            pump_interval_seconds=0.001,
            liveness_interval_seconds=0.001,
            max_liveness_failures=2,
        )

        with self.assertLogs("sap_explorer.action_recorder", level="WARNING"):
            recorder.run()

        self.assertEqual("session_disconnected", recorder.stop_reason)
        self.assertFalse(recorder.is_started)


if __name__ == "__main__":
    unittest.main()
