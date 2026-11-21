"""Verify a webhook, ask the bridge, and post one comment review."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Protocol

from hermes_pr_reviewer.bridge import BridgeError
from hermes_pr_reviewer.events import PullRequestEvent, parse_event
from hermes_pr_reviewer.forges import Forge, ReviewError
from hermes_pr_reviewer.prompt import build_prompt, format_review
from hermes_pr_reviewer.signatures import identify


class Verifier(Protocol):
    def verify(self, provider: str, body: bytes, headers: Mapping[str, str], *, now: float) -> bool: ...


class Asker(Protocol):
    def ask(self, prompt: str, *, timeout: float) -> str: ...


class ReviewService:
    def __init__(
        self,
        *,
        verifier: Verifier,
        bridge: Asker,
        forge: Forge,
        sync: bool,
        review_drafts: bool,
        max_prompt_chars: int,
        timeout: float,
    ) -> None:
        self.verifier = verifier
        self.bridge = bridge
        self.forge = forge
        self.sync = sync
        self.review_drafts = review_drafts
        self.max_prompt_chars = max_prompt_chars
        self.timeout = timeout
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def handle(self, body: bytes, headers: Mapping[str, str], *, now: float | None = None) -> tuple[int, dict[str, str]]:
        lowered = {key.lower(): value for key, value in headers.items()}
        provider = identify(lowered)
        if provider is None:
            return 400, {"status": "unknown"}
        if not self.verifier.verify(provider, body, lowered, now=time.time() if now is None else now):
            return 401, {"status": "unauthorized"}
        event = parse_event(provider, body, lowered)
        if event is None or not event.should_review or (event.draft and not self.review_drafts):
            return 202, {"status": "ignored"}
        if not self._remember(event.delivery_id):
            return 202, {"status": "duplicate"}
        if self.sync:
            try:
                self.review(event)
            except (ReviewError, BridgeError):
                self._forget(event.delivery_id)
                return 502, {"status": "error"}
            return 200, {"status": "reviewed"}
        threading.Thread(target=self._safe_review, args=(event,), name="hermes-pr-review", daemon=True).start()
        return 202, {"status": "accepted"}

    def review(self, event: PullRequestEvent) -> None:
        diff = self.forge.fetch_diff(event)
        prompt = build_prompt(event, diff, max_chars=self.max_prompt_chars)
        reply = self.bridge.ask(prompt, timeout=self.timeout)
        self.forge.post_review(event, format_review(reply))

    def _safe_review(self, event: PullRequestEvent) -> None:
        try:
            self.review(event)
        except (ReviewError, BridgeError):
            return

    def _remember(self, delivery_id: str) -> bool:
        with self._lock:
            if delivery_id in self._seen:
                return False
            self._seen.add(delivery_id)
            if len(self._seen) > 1000:
                self._seen.clear()
                self._seen.add(delivery_id)
            return True

    def _forget(self, delivery_id: str) -> None:
        with self._lock:
            self._seen.discard(delivery_id)
