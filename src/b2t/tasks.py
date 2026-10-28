from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock
from typing import Callable

from b2t.database import AppDatabase
from b2t.library import WorkspaceLibrary
from b2t.llm import LLMClient, LLMCancelled
from b2t.models import TaskRecord
from b2t.pipeline import B2TPipeline
from b2t.progress import ProgressCallback, ProgressReporter
from b2t.user_config import LLMConfig


PipelineFactory = Callable[[str, str], B2TPipeline]


class TaskService:
    def __init__(self, *, database: AppDatabase, library: WorkspaceLibrary, pipeline_factory: PipelineFactory) -> None:
        self.database = database
        self.library = library
        self.pipeline_factory = pipeline_factory
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="b2t-task")
        self._listeners: dict[str, list[ProgressCallback]] = {}
        self._futures: dict[str, Future[object]] = {}
        self._cancel_flags: dict[str, Event] = {}
        self._lock = Lock()

    def ensure_indexed(self) -> None:
        self.library.index_existing_workspace()

    def submit_transcription(
        self,
        *,
        source: str,
        provider: str,
        model: str,
        prompt: str = "",
        listener: ProgressCallback | None = None,
    ) -> TaskRecord:
        task = self.database.create_task(
            kind="transcription",
            source_input=source,
            provider=provider,
            model=model,
        )
        if listener is not None:
            self.add_listener(task.id, listener)
        reporter = ProgressReporter(task.id, callback=self._handle_progress)
        reporter.queued("queued")
        future = self.executor.submit(self._run_transcription, task.id, source, provider, model, prompt)
        with self._lock:
            self._futures[task.id] = future
        return task

    def submit_correction(
        self,
        *,
        video_id: int,
        source_version_id: int,
        text: str,
        llm: LLMConfig,
    ) -> TaskRecord:
        task = self.database.create_task(
            kind="correction",
            source_input=f"video:{video_id}",
            provider="llm",
            model=llm.model,
            video_id=video_id,
        )
        reporter = ProgressReporter(task.id, callback=self._handle_progress)
        reporter.queued("queued")
        flag = Event()
        with self._lock:
            self._cancel_flags[task.id] = flag
            future = self.executor.submit(
                self._run_correction,
                task.id,
                video_id,
                source_version_id,
                text,
                llm,
                flag,
            )
            self._futures[task.id] = future
        return task

    def cancel_task(self, task_id: str) -> bool:
        task = self.database.get_task(task_id)
        if task is None or task.status not in ("queued", "running"):
            return False
        with self._lock:
            flag = self._cancel_flags.get(task_id)
            future = self._futures.get(task_id)
        if flag is not None:
            flag.set()
        if future is not None:
            if future.cancel():
                with self._lock:
                    self._cancel_flags.pop(task_id, None)
                    self._futures.pop(task_id, None)
        self.database.cancel_task(task_id)
        return True

    def wait_for_task(self, task_id: str) -> TaskRecord:
        with self._lock:
            future = self._futures.get(task_id)
        if future is not None:
            future.result()
        task = self.database.get_task(task_id)
        if task is None:
            raise RuntimeError(f"task not found: {task_id}")
        return task

    def add_listener(self, task_id: str, callback: ProgressCallback) -> None:
        with self._lock:
            self._listeners.setdefault(task_id, []).append(callback)

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self.database.get_task(task_id)

    def list_tasks(self) -> list[TaskRecord]:
        return self.database.list_tasks()

    def _run_transcription(self, task_id: str, source: str, provider: str, model: str, prompt: str) -> None:
        reporter = ProgressReporter(task_id, callback=self._handle_progress)
        try:
            reporter.running("preparing", message="preparing")
            pipeline = self.pipeline_factory(provider, model)
            result = pipeline.transcribe(source, prompt=prompt or None, progress=reporter)
            reporter.running("indexing", message="indexing", stage_progress=0.5)
            video_id = self.library.register_transcript_result(result)
            reporter.completed("completed")
            self.database.complete_task(task_id, video_id=video_id, message="completed")
        except Exception as exc:
            reporter.failed(str(exc))
            self.database.fail_task(task_id, error_message=str(exc))
            raise

    def _run_correction(
        self,
        task_id: str,
        video_id: int,
        source_version_id: int,
        text: str,
        llm: LLMConfig,
        cancel_flag: Event,
    ) -> None:
        reporter = ProgressReporter(task_id, callback=self._handle_progress)
        try:
            reporter.emit(status="running", stage="correcting", message="准备修正", percent=0.0)
            client = LLMClient(llm)

            def on_progress(index: int, total: int) -> None:
                reporter.emit(
                    status="running",
                    stage="correcting",
                    message=f"第 {index}/{total} 段",
                    percent=max(0.0, min(0.95, index / total * 0.95)),
                    detail={"chunk": index, "total": total},
                )

            corrected = client.correct(
                text,
                on_progress=on_progress,
                is_cancelled=cancel_flag.is_set,
            )
            if cancel_flag.is_set():
                raise LLMCancelled("cancelled")
            self.database.save_correction_preview(
                task_id=task_id,
                video_id=video_id,
                source_version_id=source_version_id,
                text=corrected,
            )
            reporter.completed("completed")
            self.database.complete_task(task_id, video_id=video_id, message="completed")
        except LLMCancelled:
            self.database.cancel_task(task_id)
        except Exception as exc:
            reporter.failed(str(exc))
            self.database.fail_task(task_id, error_message=str(exc))
            raise
        finally:
            with self._lock:
                self._cancel_flags.pop(task_id, None)
                self._futures.pop(task_id, None)

    def _handle_progress(self, snapshot) -> None:  # type: ignore[no-untyped-def]
        self.database.record_progress(snapshot)
        with self._lock:
            callbacks = list(self._listeners.get(snapshot.task_id, []))
        for callback in callbacks:
            callback(snapshot)
