from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import unittest


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sap_explorer.connector import (  # noqa: E402
    InvalidSapSessionError,
    InvalidSessionSelectionError,
    NoSapConnectionsError,
    NoSapSessionsError,
    SapComError,
    SapConnector,
    SapGuiNotRunningError,
    SapScriptingUnavailableError,
)


class FakeCollection:
    def __init__(self, *items: object) -> None:
        self._items = items

    @property
    def Count(self) -> int:
        return len(self._items)

    def Item(self, index: int) -> object:
        return self._items[index]

    def __call__(self, index: int) -> object:
        return self._items[index]


@dataclass
class FakeInfo:
    SystemName: str
    Client: str
    User: str
    Transaction: str
    Program: str
    ScreenNumber: int


class FakeSession:
    def __init__(self, session_id: str, info: FakeInfo) -> None:
        self.Id = session_id
        self.Info = info


class FakeConnection:
    def __init__(
        self,
        connection_id: str,
        description: str,
        *sessions: FakeSession,
        disabled_by_server: bool = False,
    ) -> None:
        self.Id = connection_id
        self.Description = description
        self.DisabledByServer = disabled_by_server
        self.Children = FakeCollection(*sessions)


class FakeEngine:
    def __init__(self, *connections: FakeConnection) -> None:
        self.Children = FakeCollection(*connections)


class BrokenEngine:
    @property
    def Children(self) -> object:
        raise OSError("COM disconnected")


class FakeSapGui:
    def __init__(self, engine: object | None) -> None:
        self.GetScriptingEngine = engine


def make_session(number: int, system: str = "QAS") -> FakeSession:
    return FakeSession(
        f"ses[{number}]",
        FakeInfo(
            SystemName=system,
            Client="100",
            User="TEST.USER",
            Transaction="SESSION_MANAGER",
            Program="SAPLSMTR_NAVIGATION",
            ScreenNumber=100,
        ),
    )


class SapConnectorTests(unittest.TestCase):
    def test_enumerates_every_connection_and_session(self) -> None:
        first = make_session(0)
        second = make_session(1)
        third = make_session(2, system="PRD")
        engine = FakeEngine(
            FakeConnection("con[0]", "Quality", first, second),
            FakeConnection("con[1]", "Production", third),
        )
        connector = SapConnector(lambda _: FakeSapGui(engine))

        sessions = connector.discover_sessions()

        self.assertEqual(3, len(sessions))
        self.assertEqual((0, 0), (sessions[0].connection_index, sessions[0].session_index))
        self.assertEqual((0, 1), (sessions[1].connection_index, sessions[1].session_index))
        self.assertEqual((1, 0), (sessions[2].connection_index, sessions[2].session_index))
        self.assertEqual("Production", sessions[2].connection_description)
        self.assertEqual("PRD", sessions[2].system_name)
        self.assertEqual(100, sessions[2].screen_number)

    def test_selects_only_the_explicitly_requested_session(self) -> None:
        first = make_session(0)
        second = make_session(1)
        engine = FakeEngine(FakeConnection("con[0]", "Quality", first, second))
        connector = SapConnector(lambda _: FakeSapGui(engine))
        connector.discover_sessions()

        selected = connector.select_session(0, 1)

        self.assertIs(second, selected)

    def test_selection_triggers_enumeration_when_needed(self) -> None:
        session = make_session(0)
        calls: list[str] = []

        def get_object(name: str) -> FakeSapGui:
            calls.append(name)
            return FakeSapGui(
                FakeEngine(FakeConnection("con[0]", "Quality", session))
            )

        connector = SapConnector(get_object)

        self.assertIs(session, connector.select_session(0, 0))
        self.assertEqual(["SAPGUI"], calls)

    def test_reports_sap_gui_not_running(self) -> None:
        def fail(_: str) -> object:
            raise OSError("moniker unavailable")

        connector = SapConnector(fail)

        with self.assertRaises(SapGuiNotRunningError):
            connector.discover_sessions()

    def test_reports_scripting_unavailable(self) -> None:
        connector = SapConnector(lambda _: FakeSapGui(None))

        with self.assertRaises(SapScriptingUnavailableError):
            connector.discover_sessions()

    def test_wraps_unexpected_com_error(self) -> None:
        connector = SapConnector(lambda _: FakeSapGui(BrokenEngine()))

        with self.assertRaises(SapComError):
            connector.discover_sessions()

    def test_reports_no_connections(self) -> None:
        connector = SapConnector(lambda _: FakeSapGui(FakeEngine()))

        with self.assertRaises(NoSapConnectionsError):
            connector.discover_sessions()

    def test_tolerates_empty_connection_when_another_has_a_session(self) -> None:
        session = make_session(0)
        engine = FakeEngine(
            FakeConnection("con[0]", "Empty"),
            FakeConnection("con[1]", "Quality", session),
        )
        connector = SapConnector(lambda _: FakeSapGui(engine))

        sessions = connector.discover_sessions()

        self.assertEqual(1, len(sessions))
        self.assertEqual((1, 0), (sessions[0].connection_index, sessions[0].session_index))

    def test_reports_no_sessions(self) -> None:
        engine = FakeEngine(FakeConnection("con[0]", "Empty"))
        connector = SapConnector(lambda _: FakeSapGui(engine))

        with self.assertRaises(NoSapSessionsError):
            connector.discover_sessions()

    def test_reports_when_all_connections_are_disabled_by_server(self) -> None:
        engine = FakeEngine(
            FakeConnection(
                "con[0]",
                "Blocked",
                disabled_by_server=True,
            )
        )
        connector = SapConnector(lambda _: FakeSapGui(engine))

        with self.assertRaises(SapScriptingUnavailableError):
            connector.discover_sessions()

    def test_rejects_unknown_or_negative_selection(self) -> None:
        session = make_session(0)
        engine = FakeEngine(FakeConnection("con[0]", "Quality", session))
        connector = SapConnector(lambda _: FakeSapGui(engine))
        connector.discover_sessions()

        with self.assertRaises(InvalidSessionSelectionError):
            connector.select_session(0, 2)
        with self.assertRaises(InvalidSessionSelectionError):
            connector.select_session(-1, 0)

    def test_reports_stale_session_reference(self) -> None:
        session = make_session(0)
        engine = FakeEngine(FakeConnection("con[0]", "Quality", session))
        connector = SapConnector(lambda _: FakeSapGui(engine))
        connector.discover_sessions()

        del session.Info

        with self.assertRaises(InvalidSapSessionError):
            connector.select_session(0, 0)


if __name__ == "__main__":
    unittest.main()
