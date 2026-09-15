from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sap_explorer.action_recorder import _default_event_binder
from sap_explorer.com_events import DirectEventConnection, _event_map, _find_event_interface, bind_direct_events
from sap_explorer.events import SapSessionEventSink
from sap_explorer.object_tree import default_text_sanitizer
from tests.test_events import NOW


class TypeAttributes(tuple):
    def __new__(cls, iid="session", kind=4, functions=0, implementations=0):
        return super().__new__(cls, (iid, 0, 0, 0, 0, kind, functions, 0, implementations))

    @property
    def typekind(self):
        return self[5]


class EventTypeInfo:
    def __init__(self, names=None, iid="events"):
        self.names = names or {42: "change", 81: "startRequest", 91: "endRequest"}
        self.iid = iid

    def GetTypeAttr(self):
        return TypeAttributes(self.iid, functions=len(self.names))

    def GetFuncDesc(self, index):
        return (list(self.names)[index],)

    def GetNames(self, dispid):
        return (self.names[dispid],)


def fake_session(point=None, event_type=None):
    point = point or Mock()
    point.GetConnectionInterface.return_value = "events"
    enumerator = Mock()
    enumerator.Next.side_effect = [(point,), ()]
    container = Mock()
    container.EnumConnectionPoints.return_value = enumerator
    container.FindConnectionPoint.return_value = point
    library = Mock()
    library.GetTypeInfoOfGuid.return_value = event_type or EventTypeInfo()
    library.GetTypeInfoCount.return_value = 0
    session_type = Mock()
    session_type.GetTypeAttr.return_value = TypeAttributes("session")
    session_type.GetContainingTypeLib.return_value = (library, 0)
    ole = Mock()
    ole.GetTypeInfo.return_value = session_type
    ole.QueryInterface.return_value = container
    return SimpleNamespace(_oleobj_=ole), container, library


COM_CONSTANTS = SimpleNamespace(IID_IConnectionPointContainer="CPC", TKIND_DISPATCH=4,
                                TKIND_COCLASS=5, IMPLTYPEFLAG_FSOURCE=2)


class EventDiscoveryTests(unittest.TestCase):
    def test_reads_actual_dispids_and_matches_event_names_case_insensitively(self):
        self.assertEqual({42: "OnChange", 81: "OnStartRequest", 91: "OnEndRequest"},
                         _event_map(EventTypeInfo(), SapSessionEventSink))

    def test_discovers_interface_without_makepy(self):
        session, container, _ = fake_session()
        point, iid, mapping = _find_event_interface(session, SapSessionEventSink, COM_CONSTANTS)
        self.assertEqual("events", iid)
        self.assertEqual("OnChange", mapping[42])
        container.FindConnectionPoint.assert_not_called()

    def test_falls_back_to_matching_coclass_when_enumeration_is_not_implemented(self):
        session, container, library = fake_session()
        container.EnumConnectionPoints.side_effect = OSError("E_NOTIMPL")
        coclass = Mock()
        coclass.GetTypeAttr.return_value = TypeAttributes("coclass", kind=5, implementations=2)
        coclass.GetImplTypeFlags.side_effect = [0, 2]
        coclass.GetRefTypeOfImplType.side_effect = [0, 1]
        coclass.GetRefTypeInfo.side_effect = [session._oleobj_.GetTypeInfo(), EventTypeInfo()]
        library.GetTypeInfoCount.return_value = 1
        library.GetTypeInfoType.return_value = 5
        library.GetTypeInfo.return_value = coclass
        _, iid, _ = _find_event_interface(session, SapSessionEventSink, COM_CONSTANTS)
        self.assertEqual("events", iid)
        container.FindConnectionPoint.assert_called_once_with("events")

    def test_rejects_interface_without_session_events(self):
        session, _, _ = fake_session(event_type=EventTypeInfo({1: "OtherEvent"}))
        with self.assertRaisesRegex(RuntimeError, "candidatas=0"):
            _find_event_interface(session, SapSessionEventSink, COM_CONSTANTS)

    def test_ambiguous_interfaces_are_not_selected_silently(self):
        session, container, _ = fake_session()
        first, second = Mock(), Mock()
        first.GetConnectionInterface.return_value = "first"
        second.GetConnectionInterface.return_value = "second"
        container.EnumConnectionPoints.return_value.Next.side_effect = [(first,), (second,), ()]
        with self.assertRaisesRegex(RuntimeError, "candidatas=2"):
            _find_event_interface(session, SapSessionEventSink, COM_CONSTANTS)

    def test_close_is_idempotent_even_for_zero_cookie(self):
        point = Mock()
        connection = DirectEventConnection(object(), SapSessionEventSink(), point, object(), 0)
        connection.close()
        connection.close()
        point.Unadvise.assert_called_once_with(0)


@unittest.skipUnless(sys.platform == "win32", "Teste do gateway COM requer Windows/pywin32")
class WindowsComTests(unittest.TestCase):
    def test_installed_sap_typelib_discovers_events_without_generating_classes(self):
        import pythoncom
        candidates = [Path(r"C:\Program Files (x86)\SAP\FrontEnd\SAPgui\sapfewse.ocx"),
                      Path(r"C:\Program Files\SAP\FrontEnd\SAPgui\sapfewse.ocx")]
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            self.skipTest("Biblioteca SAP não instalada nos caminhos padrão")
        library = pythoncom.LoadTypeLib(str(path))
        infos = {library.GetDocumentation(i)[0]: library.GetTypeInfo(i)
                 for i in range(library.GetTypeInfoCount())}
        target = infos["ISapSessionTarget"]
        outgoing = infos["ISapSessionEvents"]
        point = Mock()
        point.GetConnectionInterface.return_value = outgoing.GetTypeAttr()[0]
        container = Mock()
        container.EnumConnectionPoints.side_effect = OSError("E_NOTIMPL")
        container.FindConnectionPoint.return_value = point
        ole = Mock()
        ole.GetTypeInfo.return_value = target
        ole.QueryInterface.return_value = container
        session = SimpleNamespace(_oleobj_=ole)
        with patch("win32com.client.gencache.EnsureModule", side_effect=AssertionError("makepy must not run")):
            _, iid, mapping = _find_event_interface(session, SapSessionEventSink, pythoncom)
        self.assertEqual(outgoing.GetTypeAttr()[0], iid)
        self.assertTrue({"OnChange", "OnStartRequest", "OnEndRequest"}.issubset(mapping.values()))

    def test_makepy_assertion_falls_back_and_preserves_handler(self):
        with patch("win32com.client.WithEvents", side_effect=AssertionError()), \
             patch("sap_explorer.com_events.bind_direct_events", return_value="direct") as direct, \
             self.assertLogs("sap_explorer.action_recorder", level="WARNING"):
            session = object()
            self.assertEqual("direct", _default_event_binder(session, SapSessionEventSink))
            direct.assert_called_once_with(session, SapSessionEventSink)

    def test_working_with_events_does_not_attach_a_second_listener(self):
        with patch("win32com.client.WithEvents", return_value="normal"), \
             patch("sap_explorer.com_events.bind_direct_events") as direct:
            self.assertEqual("normal", _default_event_binder(object(), SapSessionEventSink))
            direct.assert_not_called()

    def test_both_binding_errors_appear_in_diagnostic(self):
        with patch("win32com.client.WithEvents", side_effect=AssertionError()), \
             patch("sap_explorer.com_events.bind_direct_events", side_effect=OSError("Advise failed")), \
             self.assertLogs("sap_explorer.action_recorder", level="WARNING"):
            with self.assertRaisesRegex(RuntimeError, "AssertionError.*Advise failed"):
                _default_event_binder(object(), SapSessionEventSink)

    def test_real_com_gateway_delivers_change_and_converts_dispatch_without_makepy(self):
        import pythoncom
        import pywintypes
        from win32com.server.connect import ConnectableServer
        from win32com.server.util import wrap
        pythoncom.CoInitialize()
        connection = None
        iid = pywintypes.IID("{77F1C27E-0A9E-4CC2-BB58-402A071C9F6B}")
        class Source(ConnectableServer):
            _connect_interfaces_ = [iid]
        class Component:
            _public_methods_ = []
            _public_attrs_ = ["Id", "Type", "Name"]
            Id = "wnd[0]/usr/txtTEST"
            Type = "GuiTextField"
            Name = "TEST"
        source = Source()
        try:
            point = wrap(source, pythoncom.IID_IConnectionPoint)
            # Metadata discovery is simulated; Advise, QueryInterface, Invoke,
            # argument marshalling and Unadvise use real pywin32 COM gateways.
            with patch("sap_explorer.com_events._find_event_interface", return_value=(point, iid, {42: "OnChange"})), \
                 patch("win32com.client.gencache.EnsureModule", side_effect=AssertionError("makepy must not run")):
                connection = bind_direct_events(object(), SapSessionEventSink)
                received = []
                connection.configure(received.append, sanitizer=default_text_sanitizer, clock=lambda: NOW)
                callback = next(iter(source.connections.values()))
                callback.Invoke(42, 0, pythoncom.DISPATCH_METHOD, True,
                                None, wrap(Component()), (("SP", "Text", "teste"),))
                self.assertEqual(1, len(received))
                self.assertEqual("Change", received[0].event_type)
                self.assertEqual("wnd[0]/usr/txtTEST", received[0].component_id)
                self.assertEqual(("teste",), received[0].commands[0].parameters)
                connection.close()
                self.assertEqual({}, source.connections)
        finally:
            if connection is not None:
                connection.close()
            pythoncom.CoUninitialize()
