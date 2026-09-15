"""Descoberta e seleção explícita de sessões existentes do SAP GUI.

Este módulo não executa comandos na sessão SAP. Ele limita-se a obter o
Scripting Engine, enumerar as conexões e sessões abertas e devolver a sessão
explicitamente escolhida pelo chamador.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, TypeAlias


logger = logging.getLogger(__name__)

ComObject: TypeAlias = Any
GetObjectCallable: TypeAlias = Callable[[str], ComObject]


class SapConnectorError(RuntimeError):
    """Erro base da camada de conexão com o SAP GUI."""


class SapDependencyError(SapConnectorError):
    """A dependência pywin32 não está disponível."""


class SapGuiNotRunningError(SapConnectorError):
    """O objeto SAPGUI não pôde ser obtido."""


class SapScriptingUnavailableError(SapConnectorError):
    """O SAP GUI Scripting Engine não está disponível."""


class NoSapConnectionsError(SapConnectorError):
    """Não há conexões SAP abertas no Scripting Engine."""


class NoSapSessionsError(SapConnectorError):
    """As conexões abertas não contêm sessões SAP."""


class InvalidSessionSelectionError(SapConnectorError):
    """Os índices informados não correspondem a uma sessão enumerada."""


class InvalidSapSessionError(SapConnectorError):
    """Uma sessão anteriormente enumerada não é mais válida."""


class SapComError(SapConnectorError):
    """Falha inesperada ao acessar um objeto COM do SAP GUI."""


@dataclass(frozen=True, slots=True)
class SapSessionInfo:
    """Metadados de uma sessão obtidos da API do SAP GUI Scripting."""

    connection_index: int
    session_index: int
    connection_id: str | None
    connection_description: str | None
    session_id: str | None
    system_name: str | None
    client: str | None
    user: str | None
    transaction: str | None
    program: str | None
    screen_number: int | None


def _default_get_object(prog_id: str) -> ComObject:
    """Carrega o ponto de entrada COM do pywin32 somente quando necessário."""

    try:
        import win32com.client  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SapDependencyError(
            "pywin32 não está instalado. Execute: pip install -r requirements.txt"
        ) from exc

    return win32com.client.GetObject(prog_id)


class SapConnector:
    """Localiza, enumera e seleciona sessões já abertas do SAP GUI.

    ``get_object`` é um ponto de injeção usado pelos testes. Em produção, o
    valor padrão chama ``win32com.client.GetObject``.
    """

    def __init__(self, get_object: GetObjectCallable | None = None) -> None:
        self._get_object = get_object or _default_get_object
        self._sap_gui: ComObject | None = None
        self._scripting_engine: ComObject | None = None
        self._session_refs: dict[tuple[int, int], ComObject] = {}
        self._last_discovery: tuple[SapSessionInfo, ...] = ()
        self._discovery_performed = False

    def connect_to_scripting_engine(self) -> ComObject:
        """Obtém o objeto SAPGUI e seu Scripting Engine.

        A chamada apenas conecta à infraestrutura de scripting. Nenhuma sessão
        é selecionada e nenhuma ação é enviada ao SAP.
        """

        try:
            sap_gui = self._get_object("SAPGUI")
        except SapDependencyError:
            raise
        except Exception as exc:
            raise SapGuiNotRunningError(
                "Não foi possível obter o objeto SAPGUI. Verifique se o SAP GUI "
                "for Windows está aberto."
            ) from exc

        if sap_gui is None:
            raise SapGuiNotRunningError(
                "O objeto SAPGUI não foi encontrado. Verifique se o SAP GUI está aberto."
            )

        try:
            scripting_engine = sap_gui.GetScriptingEngine
        except Exception as exc:
            raise SapScriptingUnavailableError(
                "O SAP GUI foi encontrado, mas o Scripting Engine não está disponível."
            ) from exc

        if scripting_engine is None:
            raise SapScriptingUnavailableError(
                "O SAP GUI Scripting Engine retornou um objeto vazio."
            )

        self._sap_gui = sap_gui
        self._scripting_engine = scripting_engine
        logger.debug("SAP GUI Scripting Engine obtido com sucesso.")
        return scripting_engine

    def discover_sessions(self) -> list[SapSessionInfo]:
        """Enumera todas as conexões e todas as sessões abertas.

        A enumeração sempre é refeita, pois conexões e sessões podem mudar a
        qualquer momento no SAP GUI. Conexões sem sessão são registradas como
        aviso e não impedem que sessões de outras conexões sejam retornadas.
        """

        engine = self.connect_to_scripting_engine()
        session_refs: dict[tuple[int, int], ComObject] = {}
        discovered: list[SapSessionInfo] = []
        disabled_connection_count = 0

        try:
            connections = engine.Children
            connection_count = self._collection_count(connections)
        except SapConnectorError:
            raise
        except Exception as exc:
            raise SapComError("Falha ao enumerar as conexões do SAP GUI.") from exc

        if connection_count == 0:
            self._replace_discovery(discovered, session_refs)
            raise NoSapConnectionsError("Nenhuma conexão SAP aberta foi encontrada.")

        for connection_index in range(connection_count):
            try:
                connection = self._collection_item(connections, connection_index)
                if self._optional_bool(connection, "DisabledByServer"):
                    disabled_connection_count += 1
                    logger.warning(
                        "O scripting está desabilitado pelo servidor na conexão SAP %d.",
                        connection_index,
                    )
                    continue
                sessions = connection.Children
                session_count = self._collection_count(sessions)
            except Exception as exc:
                raise SapComError(
                    f"Falha ao enumerar a conexão SAP {connection_index}."
                ) from exc

            if session_count == 0:
                logger.warning(
                    "A conexão SAP %d está aberta, mas não possui sessões.",
                    connection_index,
                )
                continue

            for session_index in range(session_count):
                try:
                    session = self._collection_item(sessions, session_index)
                    info = self._build_session_info(
                        connection,
                        session,
                        connection_index=connection_index,
                        session_index=session_index,
                    )
                except Exception as exc:
                    raise SapComError(
                        "Falha ao ler a sessão SAP "
                        f"({connection_index}, {session_index})."
                    ) from exc

                session_refs[(connection_index, session_index)] = session
                discovered.append(info)

        self._replace_discovery(discovered, session_refs)
        if not discovered:
            if disabled_connection_count == connection_count:
                raise SapScriptingUnavailableError(
                    "O scripting está desabilitado pelo servidor em todas as "
                    "conexões SAP abertas."
                )
            raise NoSapSessionsError(
                "Há conexões SAP abertas, mas nenhuma sessão foi encontrada."
            )

        logger.info(
            "%d sessão(ões) SAP encontrada(s) em %d conexão(ões).",
            len(discovered),
            connection_count,
        )
        return list(discovered)

    def select_session(
        self, connection_index: int, session_index: int
    ) -> ComObject:
        """Seleciona explicitamente uma sessão previamente enumerada.

        Se ainda não houve enumeração, ela é executada antes da seleção. Os
        índices precisam corresponder exatamente a um par retornado por
        :meth:`discover_sessions`.
        """

        if connection_index < 0 or session_index < 0:
            raise InvalidSessionSelectionError(
                "Os índices de conexão e sessão devem ser não negativos."
            )

        if not self._discovery_performed or not self._last_discovery:
            self.discover_sessions()

        key = (connection_index, session_index)
        session = self._session_refs.get(key)
        if session is None:
            available = ", ".join(
                f"({item.connection_index}, {item.session_index})"
                for item in self._last_discovery
            )
            suffix = f" Disponíveis: {available}." if available else ""
            raise InvalidSessionSelectionError(
                f"A sessão ({connection_index}, {session_index}) não foi enumerada."
                f"{suffix}"
            )

        self._validate_session(session, connection_index, session_index)
        logger.info(
            "Sessão SAP (%d, %d) selecionada explicitamente.",
            connection_index,
            session_index,
        )
        return session

    @property
    def last_discovery(self) -> tuple[SapSessionInfo, ...]:
        """Resultado imutável da enumeração mais recente."""

        return self._last_discovery

    @staticmethod
    def _collection_count(collection: ComObject) -> int:
        count = int(collection.Count)
        if count < 0:
            raise SapComError("A coleção COM retornou uma contagem inválida.")
        return count

    @staticmethod
    def _collection_item(collection: ComObject, index: int) -> ComObject:
        """Obtém um item sem assumir a primeira conexão ou sessão."""

        try:
            return collection.Item(index)
        except (AttributeError, TypeError):
            # O dispatch dinâmico do pywin32 também pode expor Item como a
            # chamada padrão da própria coleção.
            return collection(index)

    @classmethod
    def _build_session_info(
        cls,
        connection: ComObject,
        session: ComObject,
        *,
        connection_index: int,
        session_index: int,
    ) -> SapSessionInfo:
        session_info = session.Info
        return SapSessionInfo(
            connection_index=connection_index,
            session_index=session_index,
            connection_id=cls._optional_text(connection, "Id"),
            connection_description=cls._optional_text(connection, "Description"),
            session_id=cls._optional_text(session, "Id"),
            system_name=cls._optional_text(session_info, "SystemName"),
            client=cls._optional_text(session_info, "Client"),
            user=cls._optional_text(session_info, "User"),
            transaction=cls._optional_text(session_info, "Transaction"),
            program=cls._optional_text(session_info, "Program"),
            screen_number=cls._optional_int(session_info, "ScreenNumber"),
        )

    @staticmethod
    def _optional_text(obj: ComObject, attribute: str) -> str | None:
        try:
            value = getattr(obj, attribute)
        except Exception:
            logger.debug("Atributo COM opcional indisponível: %s", attribute)
            return None
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _optional_int(obj: ComObject, attribute: str) -> int | None:
        try:
            value = getattr(obj, attribute)
            return None if value is None else int(value)
        except (AttributeError, TypeError, ValueError):
            logger.debug("Atributo COM numérico indisponível: %s", attribute)
            return None
        except Exception:
            logger.debug("Erro COM ao ler atributo opcional: %s", attribute)
            return None

    @staticmethod
    def _optional_bool(obj: ComObject, attribute: str) -> bool | None:
        try:
            value = getattr(obj, attribute)
        except Exception:
            logger.debug("Atributo COM booleano indisponível: %s", attribute)
            return None
        return None if value is None else bool(value)

    @staticmethod
    def _validate_session(
        session: ComObject, connection_index: int, session_index: int
    ) -> None:
        try:
            info = session.Info
            # A leitura força uma chamada COM e detecta referências desconectadas.
            getattr(info, "SystemName")
        except Exception as exc:
            raise InvalidSapSessionError(
                "A sessão SAP selecionada não é mais válida. "
                "Enumere novamente as sessões. "
                f"Índices: ({connection_index}, {session_index})."
            ) from exc

    def _replace_discovery(
        self,
        discovered: list[SapSessionInfo],
        session_refs: dict[tuple[int, int], ComObject],
    ) -> None:
        self._last_discovery = tuple(discovered)
        self._session_refs = session_refs
        self._discovery_performed = True
