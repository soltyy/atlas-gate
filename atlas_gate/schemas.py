"""Формы запросов и ответов HTTP-протокола (имена полей — как их ждёт харнес, camelCase)."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _empty_object_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}}


class ToolDefinition(BaseModel):
    """Определение инструмента из рукопожатия — то же, что харнес шлёт любому провайдеру."""

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=_empty_object_schema)


class AgentSpec(BaseModel):
    """Субагент харнеса (issue #135): роль с подмножеством инструментов сессии."""

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str
    prompt: str
    tools: list[str] = Field(default_factory=list)
    model: str | None = None
    maxTurns: int | None = None


class SessionCreate(BaseModel):
    system: str | None = None
    tools: list[ToolDefinition] = Field(default_factory=list)
    model: str | None = None
    # False (умолчание) — изоляция от диска (`setting_sources=[]`): в сессии только наш набор.
    # True — `setting_sources=["user"]` плюс пользовательские MCP-серверы человека из ~/.claude.json
    # (`mcpServers`) передаются в сессию явно; их вызовы исполняет сам Claude Code, итог приходит
    # событием `tool_done`. Проверено 15.09.2026: одних setting_sources для этого недостаточно.
    foreignTools: bool = False
    # Имена серверов из ~/.claude.json, которые подключать (null — все). Только при foreignTools.
    foreignServers: list[str] | None = None
    # ВОЗОБНОВИТЬ СЕССИЮ SDK, А НЕ ОТКРЫТЬ ПУСТУЮ (issue #126 харнеса). Реестр сессий роутера
    # живёт в ПАМЯТИ и умирает с перезапуском службы, а история беседы лежит на ДИСКЕ у Claude
    # Code (`CLAUDE_CONFIG_DIR/projects/<каталог>/<session_id>.jsonl`). Харнес помнит `sessionId`
    # из события `session` и присылает его при переоткрытии — тогда модель помнит разговор,
    # и ни досылать историю, ни пересчитывать индикатор не нужно. Возобновить не удалось
    # (файла нет) — сессия открывается ПУСТОЙ, а ответ честно говорит об этом полями
    # `resumed`/`resumeError`: молча подсунуть беспамятную сессию значит соврать.
    resumeSessionId: str | None = None
    # СУБАГЕНТЫ ХАРНЕСА (issue #135): модель порождает их встроенным `Agent`; инструменты каждого —
    # подмножество `tools`. Пусто — субагентов нет, `Agent` погашен вместе со встроенными.
    agents: list[AgentSpec] = Field(default_factory=list)


class SessionCreated(BaseModel):
    id: str
    tools: int
    # Принятые субагенты и отказы словами (issue #135); старый роутер этих полей не шлёт.
    agents: list[str] = Field(default_factory=list)
    agentsRefused: list[str] = Field(default_factory=list)
    # Возобновлена ли история беседы у SDK (issue #126 харнеса). False без `resumeSessionId` —
    # обычная новая сессия; False С ним — возобновить не вышло, причина в `resumeError`.
    resumed: bool = False
    resumeError: str | None = None


class SessionInfo(BaseModel):
    id: str
    model: str | None
    tools: int
    turns: int
    busy: bool
    createdAt: str
    # Идентификатор сессии вендорского SDK и признак возобновления (issue #126 харнеса).
    sdkSessionId: str | None = None
    resumed: bool = False


class SessionList(BaseModel):
    sessions: list[SessionInfo]


class Attachment(BaseModel):
    """Файл, приложенный человеком кнопкой «+» в композере (issue #83 харнеса).

    Тот же словарь, что уходит спутнику: ``kind`` — ``"text"`` (инлайнится текстом) или
    ``"image"`` (блок изображения Anthropic, ``data`` — base64 без переносов строк).
    """

    kind: str
    filename: str = ""
    mediaType: str | None = None
    data: str = ""
    sizeBytes: int = 0


class PromptRequest(BaseModel):
    id: str
    text: str
    attachments: list[Attachment] = Field(default_factory=list)


TurnState = Literal["running", "waiting_tool", "done", "error", "interrupted"]


class TurnStarted(BaseModel):
    """Ответ `POST …/prompt?mode=async` (202): ход запущен фоном, события забирать опросом."""

    turnId: str
    seq: int = 0


class TurnStatus(BaseModel):
    """`GET …/turns/{turn_id}` — состояние хода без выдачи событий."""

    turnId: str
    state: TurnState
    seq: int
    done: bool


class TurnEvents(BaseModel):
    """`GET …/turns/{turn_id}/events?after=N&wait=MS` — события хода с `seq > after`.

    `events` — те же JSON-объекты, что уходят в SSE строкой `data:`, в том же порядке, каждому
    добавлено поле `seq`. `state = waiting_tool` — последний `tool_call` ещё без `tool_result`.
    """

    turnId: str
    events: list[dict[str, Any]]
    nextAfter: int
    state: TurnState
    done: bool


class ToolsUpdate(BaseModel):
    """`POST …/tools` (issue #125 харнеса): новый набор инструментов ЖИВОЙ сессии.

    Тот же словарь, что в рукопожатии, — второго описания инструмента нет и быть не может.
    """

    tools: list[ToolDefinition] = Field(default_factory=list)


class ToolsUpdated(BaseModel):
    """Итог смены набора: сколько инструментов теперь видит модель и что именно изменилось."""

    ok: bool = True
    tools: int
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    refused: list[str] = Field(default_factory=list)


class ToolResultRequest(BaseModel):
    callId: str
    content: str = ""
    isError: bool = False


class SteerRequest(BaseModel):
    """Реплика человека в идущий ход (issue #145 харнеса)."""

    text: str


class SteerAccepted(BaseModel):
    ok: bool = True
    uuid: str
    turn: str | None = None


class OkResponse(BaseModel):
    ok: bool = True


class ToolResultAccepted(BaseModel):
    """`POST …/tool_result` 200 (#118): подтверждение доставки с идентификатором вызова."""

    ok: bool = True
    callId: str
    status: Literal["accepted"] = "accepted"


class ToolResultRefused(BaseModel):
    """`POST …/tool_result` 409: результат НЕ принят — `status` называет, почему."""

    detail: str
    callId: str
    status: Literal["unknown_call", "answered", "turn_not_waiting"]


class InterruptResponse(BaseModel):
    """`POST …/interrupt` (#118): какие вызовы закрыты текстом «не доставлен» и освободилась ли сессия."""

    ok: bool = True
    closedCalls: list[str] = []
    settled: bool = True


class LoginCodeRequest(BaseModel):
    code: str


class AuthStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    loggedIn: bool
    email: str = ""
    method: str = ""


class LoginStarted(BaseModel):
    url: str


class LoginCodeResult(BaseModel):
    ok: bool
    status: AuthStatus


class ModelInfo(BaseModel):
    value: str
    resolved: str
    name: str
    description: str = ""


class ModelList(BaseModel):
    model_config = ConfigDict(extra="allow")

    models: list[ModelInfo]
    source: str
    # Имена ключей верхнего уровня ответа `initialize` SDK (у бэкенда Claude), для диагностики формы.
    initializeKeys: list[str] = Field(default_factory=list)


class ContextUsageResponse(BaseModel):
    """`GET …/context` (issue #100 харнеса) — те же числа, что CLI-команда `/context`.

    Реальный расход сессии от SDK (`ClaudeSDKClient.get_context_usage()`), а не оценка харнеса по
    локальной истории: харнес шлёт транспорту только последнюю реплику, всю беседу ведёт SDK.
    """

    totalTokens: int
    maxTokens: int
    percentage: float = 0.0
    model: str | None = None


class CompactRequest(BaseModel):
    """`POST …/compact` (issue #99 харнеса): на что сделать упор в выжимке (необязательно)."""

    instructions: str | None = None


class CompactResponse(BaseModel):
    """Итог штатного сжатия сессии на стороне SDK (`/compact`, issue #99 харнеса).

    Сессия — ТА ЖЕ (тот же `session_id`), история сообщений на стороне SDK заменена выжимкой;
    `summary` — её текст, который модель теперь видит вместо истории (харнес кладёт его в своё
    зеркало по факту; `null`, если SDK текст не отдал); `preTokens`/`postTokens` — размер истории
    сообщений до/после (без системной подсказки и инструментов); `context` — реальный размер окна
    уже после сжатия, те же числа, что `GET …/context`.
    """

    ok: bool = True
    summary: str | None = None
    preTokens: int = 0
    postTokens: int = 0
    durationMs: int = 0
    context: ContextUsageResponse | None = None
    contextError: str | None = None
