"""Several agent engines behind one Engine: each job names the one it runs on."""

from __future__ import annotations

import logging

from agent_worker.engine import Emit, Engine, EngineError, Outcome, RunSpec

logger = logging.getLogger(__name__)

# How the engines show up in /v1 model ids: "claude-code/claude-sonnet-5-5". Cursor ids stay bare,
# because Cursor itself offers Claude models under names such as "claude-4.5-sonnet".
MODEL_PREFIX = {"claude": "claude-code/"}


class EngineRouter:
    def __init__(self, engines: dict[str, Engine], default: str = "") -> None:
        self.engines = engines
        self._default = default

    @property
    def available(self) -> bool:
        return any(engine.available for engine in self.engines.values())

    def default(self) -> str:
        """DEFAULT_ENGINE, else cursor, else the first engine that is set up."""
        if self._default in self.engines:
            return self._default
        cursor = self.engines.get("cursor")
        if cursor is not None and cursor.available:
            return "cursor"
        ready = next((name for name, engine in self.engines.items() if engine.available), "")
        return ready or next(iter(self.engines), "cursor")

    def pick(self, requested: str) -> str:
        name = (requested or "").strip().lower() or self.default()
        if name not in self.engines:
            raise EngineError(f"Unknown engine {name!r}. Use one of: {', '.join(self.engines)}.")
        return name

    def model_for(self, engine: str) -> str:
        settings = getattr(self.engines[engine], "settings", None)
        if settings is None:
            return ""
        return settings.claude_model if engine == "claude" else settings.model

    async def run(self, spec: RunSpec, emit: Emit) -> Outcome:
        return await self.engines[spec.engine].run(spec, emit)

    async def models(self) -> list[dict[str, str]]:
        """Every engine's models; ids carry the engine prefix where one is needed."""
        rows: list[dict[str, str]] = []
        for name, engine in self.engines.items():
            if not engine.available:
                continue
            prefix = MODEL_PREFIX.get(name, "")
            for row in await engine.models():
                rows.append({"id": prefix + row["id"], "name": row.get("name") or row["id"], "engine": name})
        return rows

    async def aclose(self) -> None:
        for engine in self.engines.values():
            try:
                await engine.aclose()
            except Exception:
                logger.debug("engine close failed", exc_info=True)


def split_engine(model: str) -> tuple[str, str]:
    """'claude-code/claude-opus-5-5' -> ('claude', 'claude-opus-5-5'); anything else -> ('', model)."""
    name = (model or "").strip()
    for engine, prefix in MODEL_PREFIX.items():
        if name == prefix.rstrip("/"):
            # The bare prefix ("claude-code") means that engine with its default model.
            return engine, ""
        if name.startswith(prefix):
            return engine, name[len(prefix) :]
    return "", name
