"""Vínculo de eventos por connection point, sem geração de classes makepy.

O IID e os DISPIDs são lidos da type library da própria sessão. A conexão
Advise/Unadvise segue o mecanismo usado pelos sinks gerados pelo pywin32.
"""
from __future__ import annotations

import logging
from typing import Any

from .events import Clock, EventConsumer, SapSessionEventSink
from .object_tree import TextSanitizer

logger = logging.getLogger(__name__)


def _event_map(type_info: Any, handler: type[SapSessionEventSink]) -> dict[int, str]:
    available = {name[2:].casefold(): name for name in dir(handler) if name.startswith("On")}
    result = {}
    # TYPEATTR.cFuncs; não inspeciona propriedades enumeradoras de outras classes.
    for index in range(type_info.GetTypeAttr()[6]):
        descriptor = type_info.GetFuncDesc(index)
        dispid = descriptor[0]
        names = type_info.GetNames(dispid)
        if names and names[0].casefold() in available:
            result[dispid] = available[names[0].casefold()]
    return result


def _find_event_interface(session: Any, handler: type[SapSessionEventSink],
                          pythoncom: Any) -> tuple[Any, Any, dict[int, str]]:
    ole = session._oleobj_
    session_type = ole.GetTypeInfo()
    library, _ = session_type.GetContainingTypeLib()
    container = ole.QueryInterface(pythoncom.IID_IConnectionPointContainer)
    candidates: dict[str, tuple[Any, Any, dict[int, str]]] = {}

    def consider(point: Any, type_info: Any) -> None:
        if type_info.GetTypeAttr().typekind != pythoncom.TKIND_DISPATCH:
            return
        mapping = _event_map(type_info, handler)
        if not ({"OnChange", "OnStartRequest", "OnEndRequest"} & set(mapping.values())):
            return
        iid = point.GetConnectionInterface()
        candidates[str(iid)] = (point, iid, mapping)

    try:
        enumerator = container.EnumConnectionPoints()
        while points := enumerator.Next(1):
            point = points[0]
            iid = point.GetConnectionInterface()
            try:
                consider(point, library.GetTypeInfoOfGuid(iid))
            except Exception as exc:
                logger.debug("Interface de eventos %s não pôde ser inspecionada: %s", iid, exc)
    except Exception as exc:
        logger.debug("EnumConnectionPoints indisponível; procurando coclass da sessão: %s", exc)

    if not candidates:
        # Alguns servidores implementam FindConnectionPoint, mas não EnumConnectionPoints.
        session_iid = session_type.GetTypeAttr()[0]
        for index in range(library.GetTypeInfoCount()):
            if library.GetTypeInfoType(index) != pythoncom.TKIND_COCLASS:
                continue
            info = library.GetTypeInfo(index)
            references = []
            try:
                for implementation in range(info.GetTypeAttr()[8]):
                    flags = info.GetImplTypeFlags(implementation)
                    reference = info.GetRefTypeInfo(info.GetRefTypeOfImplType(implementation))
                    references.append((flags, reference))
                matches = info.GetTypeAttr()[0] == session_iid or any(
                    not flags & pythoncom.IMPLTYPEFLAG_FSOURCE and ref.GetTypeAttr()[0] == session_iid
                    for flags, ref in references)
                if not matches:
                    continue
                for flags, ref in references:
                    if flags & pythoncom.IMPLTYPEFLAG_FSOURCE:
                        point = container.FindConnectionPoint(ref.GetTypeAttr()[0])
                        consider(point, ref)
            except Exception as exc:
                logger.debug("Coclass %d não pôde ser inspecionada: %s", index, exc)

    if len(candidates) != 1:
        raise RuntimeError(
            "Não foi possível identificar uma interface única de eventos de GuiSession "
            f"na type library (candidatas={len(candidates)})."
        )
    return next(iter(candidates.values()))


class DirectEventConnection:
    """Retém o handler, a sessão e o gateway COM até Unadvise."""

    def __init__(self, session: Any, sink: SapSessionEventSink, point: Any,
                 gateway: Any, cookie: int) -> None:
        self._session = session
        self._sink = sink
        self._point = point
        self._gateway = gateway
        self._cookie = cookie

    def configure(self, consumer: EventConsumer, *, sanitizer: TextSanitizer, clock: Clock) -> None:
        self._sink.configure(consumer, sanitizer=sanitizer, clock=clock)

    def close(self) -> None:
        if self._point is not None:
            self._point.Unadvise(self._cookie)
            self._point = self._gateway = self._session = None


def bind_direct_events(session: Any, handler: type[SapSessionEventSink]) -> DirectEventConnection:
    import pythoncom
    from win32com.client import dynamic
    from win32com.server.policy import EventHandlerPolicy
    from win32com.server.util import wrap

    class DynamicEventPolicy(EventHandlerPolicy):
        def _transform_args_(self, args, kwArgs, dispid, lcid, wFlags, serviceProvider):
            converted = []
            for argument in args:
                if isinstance(argument, pythoncom.TypeIIDs[pythoncom.IID_IDispatch]):
                    argument = dynamic.Dispatch(argument)
                elif isinstance(argument, pythoncom.TypeIIDs[pythoncom.IID_IUnknown]):
                    try:
                        argument = dynamic.Dispatch(argument.QueryInterface(pythoncom.IID_IDispatch))
                    except pythoncom.com_error:
                        pass
                converted.append(argument)
            return tuple(converted), kwArgs

    point, iid, mapping = _find_event_interface(session, handler, pythoncom)
    # Oferece um gateway IDispatch sob o IID de saída, como os sinks gerados
    # pelo pywin32. Retornar apenas 1 exigiria um gateway registrado para o IID.
    sink_class = type("SapDirectEventSink", (handler,), {
        "_public_methods_": [],
        "_dispid_to_func_": mapping,
        "_query_interface_": lambda self, requested: wrap(self, usePolicy=DynamicEventPolicy) if requested == iid else 0,
    })
    sink = sink_class()
    gateway = wrap(sink, usePolicy=DynamicEventPolicy)
    cookie = point.Advise(gateway)
    logger.info("Listener COM direto ativo | interface=%s | eventos=%s", iid,
                ", ".join(sorted(name[2:] for name in mapping.values())))
    return DirectEventConnection(session, sink, point, gateway, cookie)
