"""Process-wide application state, attached to `app.state.fb` and injected into routes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from fetchbox.core.db import Database
from fetchbox.core.events import EventBus
from fetchbox.core.health import HealthRegistry
from fetchbox.core.providers import ProviderRegistry


@dataclass
class AppState:
    data_dir: Path
    db: Database
    bus: EventBus
    health: HealthRegistry
    providers: ProviderRegistry
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.fb
    return state


State = Annotated[AppState, Depends(get_state)]
