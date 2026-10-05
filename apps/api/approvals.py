"""Process-local LangGraph checkpoints for opt-in SQL approvals."""

from __future__ import annotations

from collections import defaultdict
from threading import RLock

from langgraph.checkpoint.memory import InMemorySaver

from src.orchestration.langgraph_orchestrator import build_checkpoint_serializer


class ApprovalCheckpoints:
    """Own resumable threads and remove them with their workspace lifecycle."""

    def __init__(self) -> None:
        self.saver = InMemorySaver(serde=build_checkpoint_serializer())
        self._lock = RLock()
        self._workspace_threads: dict[str, set[str]] = defaultdict(set)

    def register(self, workspace_id: str, thread_id: str) -> None:
        with self._lock:
            self._workspace_threads[workspace_id].add(thread_id)

    def discard(self, workspace_id: str, thread_id: str) -> None:
        with self._lock:
            self._workspace_threads[workspace_id].discard(thread_id)
            self.saver.delete_thread(thread_id)

    def delete_workspace(self, workspace_id: str) -> None:
        with self._lock:
            for thread_id in self._workspace_threads.pop(workspace_id, set()):
                self.saver.delete_thread(thread_id)

    def pending_count(self, workspace_id: str) -> int:
        with self._lock:
            return len(self._workspace_threads.get(workspace_id, ()))
