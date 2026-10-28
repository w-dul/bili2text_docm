from __future__ import annotations

import difflib
import html
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from b2t.config import Settings
from b2t.database import AppDatabase
from b2t.i18n import tr
from b2t.inputs import parse_source_list
from b2t.library import WorkspaceLibrary
from b2t.llm import LLMClient, format_paragraphs
from b2t.models import TaskRecord
from b2t.segments import practice_text, segment_text, visible_char_count
from b2t.tasks import TaskService
from b2t.user_config import AppConfig, LLMConfig

class TranscribeTaskRequest(BaseModel):
    source: str
    provider: str = "whisper"
    model: str = "small"
    prompt: str = ""


class BatchTranscribeTaskRequest(BaseModel):
    sources: list[str] | None = None
    source_text: str | None = None
    provider: str = "whisper"
    model: str = "small"
    prompt: str = ""


class TranscriptUpdateRequest(BaseModel):
    text: str


class CategoryRequest(BaseModel):
    name: str | None = None
    category_id: int | None = None


class TagRequest(BaseModel):
    name: str | None = None
    tag_id: int | None = None


class TypingRecordRequest(BaseModel):
    version_id: int
    segment_seq: int
    segment_text: str = ""
    mode: str = "strict"
    duration_seconds: float
    correct_chars: int
    error_count: int


class CustomTypingRecordRequest(BaseModel):
    version_id: int = 0
    text: str
    duration_seconds: float
    correct_chars: int
    error_count: int


def create_app(
    *,
    task_service: TaskService,
    library: WorkspaceLibrary,
    database: AppDatabase,
    default_provider: str = "whisper",
    default_model: str = "small",
    language: str = "zh-CN",
    settings: Settings | None = None,
    app_config: AppConfig | None = None,
) -> FastAPI:
    templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
    app = FastAPI(title="bili2text")
    runtime_settings = settings or database.settings

    if app_config is None:
        providers = ["whisper", "sensevoice", "volcengine"]
    else:
        providers = list(app_config.enabled_providers)
        if default_provider not in providers:
            providers.append(default_provider)

    def page(name: str, request: Request, **extra: object) -> HTMLResponse:
        status_code = int(extra.pop("status_code", 200))  # type: ignore[arg-type]
        payload: dict[str, object] = {
            "lang": language,
            "t": lambda key, **kwargs: tr(language, key, **kwargs),
        }
        payload.update(extra)
        return templates.TemplateResponse(request, name, payload, status_code=status_code)

    def load_config() -> AppConfig:
        return AppConfig.load(runtime_settings)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        videos = database.list_videos()
        tasks = task_service.list_tasks()
        return page(
            "index.html",
            request,
            error=None,
            values={
                "source": "",
                "provider": default_provider,
                "model": default_model,
                "prompt": "",
            },
            videos=videos[:5],
            total_videos=len(videos),
            tasks=[asdict(task) for task in tasks[:5]],
            providers=providers,
        )

    @app.post("/transcribe", response_class=HTMLResponse)
    async def transcribe_from_form(
        request: Request,
        source: str = Form(...),
        provider: str = Form("whisper"),
        model: str = Form("small"),
        prompt: str = Form(""),
    ) -> HTMLResponse:
        try:
            sources = parse_source_list(source)
        except ValueError as exc:
            videos = database.list_videos()
            tasks = task_service.list_tasks()
            return page(
                "index.html",
                request,
                error=str(exc),
                values={
                    "source": source,
                    "provider": provider,
                    "model": model,
                    "prompt": prompt,
                },
                videos=videos[:5],
                total_videos=len(videos),
                tasks=[asdict(task) for task in tasks[:5]],
                providers=providers,
                status_code=400,
            )

        tasks = _submit_transcription_tasks(
            task_service,
            sources=sources,
            provider=provider,
            model=model,
            prompt=prompt,
        )
        if len(tasks) > 1:
            return page(
                "batch.html",
                request,
                tasks=[asdict(task) for task in tasks],
            )

        task = tasks[0]
        return page("task.html", request, task_id=task.id, task=asdict(task))

    @app.get("/tasks/batch", response_class=HTMLResponse)
    async def batch_task_page(request: Request, ids: str = Query("")) -> HTMLResponse:
        task_ids = [task_id.strip() for task_id in ids.split(",") if task_id.strip()]
        tasks = []
        for task_id in task_ids:
            task = database.get_task(task_id)
            if task is not None:
                tasks.append(asdict(task))
        return page("batch.html", request, tasks=tasks)

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_page(request: Request, task_id: str) -> HTMLResponse:
        task = database.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return page("task.html", request, task_id=task_id, task=asdict(task))

    @app.get("/videos/{video_id}", response_class=HTMLResponse)
    async def video_page(request: Request, video_id: int) -> HTMLResponse:
        video = database.get_video(video_id)
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        transcript = library.load_active_transcript(video_id)
        versions = [asdict(version) for version in database.list_transcript_versions(video_id)]
        version_id = int(transcript["version_id"])
        active_text = str(transcript["text"])
        segments_count = database.count_segments(version_id)
        cleaned = visible_char_count(active_text)
        llm = load_config().llm
        correction_task = database.get_active_correction_task(video_id)
        return page(
            "video.html",
            request,
            video=video,
            transcript=transcript,
            versions=versions,
            segments_count=segments_count,
            active_char_count=cleaned,
            can_practice=cleaned >= 20,
            llm_ready=_llm_ready(llm),
            llm_enabled=llm.enabled,
            correction_task=asdict(correction_task) if correction_task else None,
            typing_records=database.list_typing_records(video_id, limit=5),
            categories=database.list_categories(),
            tags=database.list_tags(),
            notice=request.query_params.get("notice") or "",
            error=request.query_params.get("error") or "",
        )

    @app.post("/videos/{video_id}/edit")
    async def edit_video_transcript(video_id: int, text: str = Form(...)) -> RedirectResponse:
        library.save_edited_transcript(video_id, text)
        return RedirectResponse(url=f"/videos/{video_id}?notice={quote('已保存为新的激活版本')}", status_code=303)

    @app.post("/videos/{video_id}/category")
    async def assign_video_category(video_id: int, category_name: str = Form("")) -> RedirectResponse:
        category_name = category_name.strip()
        category_id = None
        if category_name:
            category = database.create_category(category_name)
            category_id = int(category["id"])
        database.assign_category(video_id, category_id)
        return RedirectResponse(url=f"/videos/{video_id}", status_code=303)

    @app.post("/videos/{video_id}/tags")
    async def add_video_tag(video_id: int, tag_name: str = Form("")) -> RedirectResponse:
        tag_name = tag_name.strip()
        if tag_name:
            tag = database.create_tag(tag_name)
            database.add_video_tag(video_id, int(tag["id"]))
        return RedirectResponse(url=f"/videos/{video_id}", status_code=303)

    @app.post("/videos/{video_id}/versions/{version_id}/activate")
    async def activate_video_version(video_id: int, version_id: int) -> RedirectResponse:
        if database.get_transcript_version(video_id, version_id) is None:
            raise HTTPException(status_code=404, detail="version not found")
        database.activate_transcript_version(video_id, version_id)
        return RedirectResponse(url=f"/videos/{video_id}?notice={quote('已切换激活版本')}", status_code=303)

    @app.get("/videos/{video_id}/versions/{version_id}/download")
    async def download_video_version(video_id: int, version_id: int) -> Response:
        version = database.get_transcript_version(video_id, version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="version not found")
        content = Path(version.file_path).read_text(encoding="utf-8")
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="transcript-{video_id}-{version_id}.txt"'
            },
        )

    @app.post("/videos/{video_id}/segments/rebuild")
    async def rebuild_segments(video_id: int) -> RedirectResponse:
        version = database.get_active_transcript_version(video_id)
        if version is None:
            raise HTTPException(status_code=400, detail="active transcript not found")
        text = Path(version.file_path).read_text(encoding="utf-8")
        if visible_char_count(text) < 20:
            return RedirectResponse(
                url=f"/videos/{video_id}?error={quote('逐字稿内容不足 20 字，无法分段')}",
                status_code=303,
            )
        library.rebuild_segments(video_id, version.id, text)
        return RedirectResponse(url=f"/videos/{video_id}?notice={quote('分段已重新生成')}", status_code=303)

    @app.post("/videos/{video_id}/correct")
    async def start_correction(video_id: int) -> RedirectResponse:
        video = database.get_video(video_id)
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        llm = load_config().llm
        if not _llm_ready(llm):
            return RedirectResponse(
                url=f"/settings?error={quote('未配置 LLM，请先在设置页完成配置')}",
                status_code=303,
            )
        if database.get_active_correction_task(video_id) is not None:
            return RedirectResponse(
                url=f"/videos/{video_id}?error={quote('修正进行中，请等待当前任务结束')}",
                status_code=303,
            )
        transcript = library.load_active_transcript(video_id)
        task = task_service.submit_correction(
            video_id=video_id,
            source_version_id=int(transcript["version_id"]),
            text=str(transcript["text"]),
            llm=llm,
        )
        return RedirectResponse(url=f"/videos/{video_id}/correction/{task.id}", status_code=303)

    @app.get("/videos/{video_id}/correction/{task_id}", response_class=HTMLResponse)
    async def correction_page(
        request: Request,
        video_id: int,
        task_id: str,
        error: str = Query(""),
    ) -> HTMLResponse:
        return _render_correction_page(
            request,
            page,
            database=database,
            library=library,
            video_id=video_id,
            task_id=task_id,
            error=error,
        )

    @app.post("/videos/{video_id}/correction/{task_id}/apply")
    async def apply_correction(
        request: Request,
        video_id: int,
        task_id: str,
        text: str = Form(...),
    ) -> Response:
        preview = database.get_correction_preview(task_id)
        if preview is None:
            raise HTTPException(status_code=404, detail="preview not found")
        source = library.load_transcript_version(video_id, int(preview["source_version_id"]))
        source_text = str(source["text"])
        candidate = text.rstrip() + "\n"
        if not candidate.strip():
            return _render_correction_page(
                request,
                page,
                database=database,
                library=library,
                video_id=video_id,
                task_id=task_id,
                error="修正稿不能为空",
                corrected_override=text,
            )
        drift = abs(len(candidate) - len(source_text)) / max(1, len(source_text))
        if drift > 0.15:
            return _render_correction_page(
                request,
                page,
                database=database,
                library=library,
                video_id=video_id,
                task_id=task_id,
                error=f"修正稿与原文长度差异 {drift:.0%}，超过 15% 限制",
                corrected_override=text,
            )
        version_id = library.save_ai_corrected_transcript(video_id, candidate)
        database.delete_correction_preview(task_id)
        return RedirectResponse(
            url=f"/videos/{video_id}?notice={quote(f'已应用修正并切换到新版本（{version_id}）')}",
            status_code=303,
        )

    @app.post("/videos/{video_id}/correction/{task_id}/discard")
    async def discard_correction(video_id: int, task_id: str) -> RedirectResponse:
        database.delete_correction_preview(task_id)
        return RedirectResponse(
            url=f"/videos/{video_id}?notice={quote('已放弃本次修正预览')}",
            status_code=303,
        )

    @app.get("/videos/{video_id}/typing", response_class=HTMLResponse)
    async def typing_select_page(
        request: Request,
        video_id: int,
        error: str = Query(""),
    ) -> HTMLResponse:
        video = database.get_video(video_id)
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        version = database.get_active_transcript_version(video_id)
        if version is None:
            raise HTTPException(status_code=400, detail="active transcript not found")
        text = Path(version.file_path).read_text(encoding="utf-8")
        reason = ""
        segments: list[dict[str, object]] = []
        if visible_char_count(text) < 20:
            reason = "逐字稿内容不足 20 字，无法练习"
        else:
            segments = library.ensure_segments(video_id, version.id)
            if not segments:
                reason = "分段失败，请回到逐字稿页重新分段"
        return page(
            "typing_select.html",
            request,
            video=video,
            version_id=version.id,
            segments=segments,
            reason=reason,
            error=error,
            active_char_count=visible_char_count(text),
        )

    @app.get("/videos/{video_id}/typing/{version_id}/{seq}", response_class=HTMLResponse)
    async def typing_practice_page(
        request: Request,
        video_id: int,
        version_id: int,
        seq: int,
    ) -> HTMLResponse:
        video = database.get_video(video_id)
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        version = database.get_transcript_version(video_id, version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="version not found")
        segment = database.get_segment(video_id, version_id, seq)
        if segment is None:
            raise HTTPException(status_code=404, detail="segment not found")
        target = practice_text(str(segment["text"]))
        if not target:
            raise HTTPException(status_code=400, detail="segment is empty")
        segments = database.list_segments(version_id)
        completed = database.list_completed_segment_seqs(video_id, version_id)
        candidates = list(range(seq + 1, len(segments) + 1)) + list(range(1, seq))
        next_seq = next((candidate for candidate in candidates if candidate not in completed), 1)
        return page(
            "typing.html",
            request,
            video=video,
            version_id=version_id,
            heading=f"第 {seq}/{len(segments)} 段",
            target=target,
            target_blocks=[{"label": "", "text": target}],
            custom_mode=False,
            practice_url=f"/videos/{video_id}/typing/{version_id}/{seq}",
            next_url=f"/videos/{video_id}/typing/{version_id}/{next_seq}",
            next_label="换一段",
            select_url=f"/videos/{video_id}/typing",
            save_url=f"/api/videos/{video_id}/typing/records",
            save_extra={"version_id": version_id, "segment_seq": seq},
            save_mode="strict",
        )

    @app.post("/videos/{video_id}/typing/custom", response_class=HTMLResponse)
    async def custom_typing_page(
        request: Request,
        video_id: int,
        text: str = Form(...),
    ) -> HTMLResponse:
        video = database.get_video(video_id)
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        target = practice_text(text)
        if visible_char_count(target) < 10:
            return RedirectResponse(
                url=f"/videos/{video_id}/typing?error={quote('自定义文本至少需要 10 个字符')}",
                status_code=303,
            )
        active = database.get_active_transcript_version(video_id)
        version_id = active.id if active is not None else 0
        return page(
            "typing.html",
            request,
            video=video,
            version_id=version_id,
            heading="自定义文本",
            target=target,
            target_blocks=[{"label": "", "text": target}],
            custom_mode=True,
            practice_url=f"/videos/{video_id}/typing",
            next_url=f"/videos/{video_id}/typing",
            next_label="重新选择",
            select_url=f"/videos/{video_id}/typing",
            save_url=f"/api/videos/{video_id}/typing/custom-records",
            save_extra={"version_id": version_id, "text": target},
            save_mode="custom",
        )

    @app.post("/videos/{video_id}/typing/combined", response_class=HTMLResponse)
    async def combined_typing_page(
        request: Request,
        video_id: int,
        seqs: list[int] = Form(default=[]),
    ) -> Response:
        video = database.get_video(video_id)
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        version = database.get_active_transcript_version(video_id)
        if version is None:
            raise HTTPException(status_code=400, detail="active transcript not found")
        selected = sorted(set(seqs))
        if not selected:
            return RedirectResponse(
                url=f"/videos/{video_id}/typing?error={quote('请至少选择一个段落')}",
                status_code=303,
            )
        if len(selected) == 1:
            return RedirectResponse(
                url=f"/videos/{video_id}/typing/{version.id}/{selected[0]}",
                status_code=303,
            )
        segments = {int(item["seq"]): item for item in database.list_segments(version.id)}
        if any(seq not in segments for seq in selected):
            return RedirectResponse(
                url=f"/videos/{video_id}/typing?error={quote('所选段落不存在，请重新选择')}",
                status_code=303,
            )
        blocks: list[dict[str, str]] = []
        for seq in selected:
            block_text = practice_text(str(segments[seq]["text"]))
            if block_text:
                blocks.append({"label": f"第 {seq} 段", "text": block_text})
        target = "".join(block["text"] for block in blocks)
        if visible_char_count(target) < 10:
            return RedirectResponse(
                url=f"/videos/{video_id}/typing?error={quote('所选段落内容不足 10 字')}",
                status_code=303,
            )
        return page(
            "typing.html",
            request,
            video=video,
            version_id=version.id,
            heading=f"所选 {len(selected)} 段",
            target=target,
            target_blocks=blocks,
            custom_mode=True,
            practice_url=f"/videos/{video_id}/typing",
            next_url=f"/videos/{video_id}/typing",
            next_label="重新选择",
            select_url=f"/videos/{video_id}/typing",
            save_url=f"/api/videos/{video_id}/typing/custom-records",
            save_extra={"version_id": version.id, "text": target},
            save_mode="custom",
        )

    @app.get("/videos/{video_id}/typing/combined")
    async def combined_typing_redirect(video_id: int) -> RedirectResponse:
        return RedirectResponse(url=f"/videos/{video_id}/typing", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(
        request: Request,
        error: str = Query(""),
        notice: str = Query(""),
    ) -> HTMLResponse:
        config = load_config()
        return page(
            "settings.html",
            request,
            config=config,
            llm=config.llm,
            api_key_masked=_mask_key(config.llm.api_key),
            default_provider=default_provider,
            default_model=default_model,
            storage=_storage_payload(runtime_settings),
            error=error,
            notice=notice,
            test_result=None,
        )

    @app.post("/settings/llm", response_class=HTMLResponse)
    async def save_llm_settings(
        request: Request,
        enabled: str = Form(""),
        base_url: str = Form(""),
        api_key: str = Form(""),
        model: str = Form(""),
        timeout_seconds: str = Form("60"),
        glossary: str = Form(""),
    ) -> Response:
        config = load_config()
        candidate, error = _llm_from_form(
            enabled=enabled,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=_parse_timeout(timeout_seconds),
            glossary=glossary,
            current=config.llm,
        )
        if candidate is None:
            return _settings_response(
                request, page, config, error, notice="", test_result=None,
                default_provider=default_provider, default_model=default_model,
                storage=_storage_payload(runtime_settings), status_code=400,
            )
        config.llm = candidate
        config.save(runtime_settings)
        return RedirectResponse(url="/settings?notice=" + quote("LLM 配置已保存"), status_code=303)

    @app.post("/settings/llm/test", response_class=HTMLResponse)
    async def test_llm_settings(
        request: Request,
        enabled: str = Form(""),
        base_url: str = Form(""),
        api_key: str = Form(""),
        model: str = Form(""),
        timeout_seconds: str = Form("60"),
        glossary: str = Form(""),
    ) -> HTMLResponse:
        config = load_config()
        candidate, error = _llm_from_form(
            enabled=enabled,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=_parse_timeout(timeout_seconds),
            glossary=glossary,
            current=config.llm,
        )
        test_result: dict[str, object] | None = None
        if candidate is None:
            status_code = 400
        else:
            status_code = 200
            ok, message, elapsed = LLMClient(candidate).test_connection()
            test_result = {"ok": ok, "message": message, "elapsed": round(elapsed, 2)}
        return _settings_response(
            request, page, config, error, notice="", test_result=test_result,
            default_provider=default_provider, default_model=default_model,
            storage=_storage_payload(runtime_settings), status_code=status_code,
        )

    @app.post("/settings/llm/reset")
    async def reset_llm_settings() -> RedirectResponse:
        config = load_config()
        config.llm = LLMConfig()
        config.save(runtime_settings)
        return RedirectResponse(url="/settings?notice=" + quote("LLM 配置已恢复默认"), status_code=303)

    @app.post("/api/tasks/transcribe")
    async def create_transcription_task(payload: TranscribeTaskRequest) -> JSONResponse:
        task = task_service.submit_transcription(
            source=payload.source,
            provider=payload.provider,
            model=payload.model,
            prompt=payload.prompt,
        )
        return JSONResponse({"task_id": task.id, "status": task.status})

    @app.post("/api/tasks/batch")
    async def create_batch_transcription_tasks(payload: BatchTranscribeTaskRequest) -> JSONResponse:
        source_chunks = list(payload.sources or [])
        if payload.source_text:
            source_chunks.append(payload.source_text)
        try:
            sources = parse_source_list("\n".join(source_chunks))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tasks = _submit_transcription_tasks(
            task_service,
            sources=sources,
            provider=payload.provider,
            model=payload.model,
            prompt=payload.prompt,
        )
        return JSONResponse(
            {
                "items": [asdict(task) for task in tasks],
                "count": len(tasks),
            }
        )

    @app.get("/api/tasks")
    async def list_tasks(
        status: str | None = Query(None),
        provider: str | None = Query(None),
    ) -> JSONResponse:
        return JSONResponse(
            {
                "items": [asdict(task) for task in task_service.list_tasks() if (status is None or task.status == status) and (provider is None or task.provider == provider)],
                "filters": {"status": status, "provider": provider},
            }
        )

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> JSONResponse:
        task = task_service.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return JSONResponse(asdict(task))

    @app.get("/api/tasks/{task_id}/progress")
    async def get_task_progress(task_id: str) -> JSONResponse:
        task = task_service.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return JSONResponse(asdict(task))

    @app.get("/api/tasks/{task_id}/events")
    async def get_task_events(task_id: str) -> JSONResponse:
        task = task_service.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return JSONResponse({"items": database.list_task_events(task_id)})

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> JSONResponse:
        task = task_service.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        cancelled = task_service.cancel_task(task_id)
        updated = task_service.get_task(task_id)
        return JSONResponse(
            {
                "cancelled": cancelled,
                "status": updated.status if updated is not None else task.status,
            }
        )

    @app.get("/api/videos")
    async def list_videos_api(
        query: str | None = Query(None),
        category_id: int | None = Query(None),
        tag_id: int | None = Query(None),
    ) -> JSONResponse:
        return JSONResponse(
            {
                "items": database.list_videos(query=query, category_id=category_id, tag_id=tag_id),
                "filters": {
                    "query": query,
                    "category_id": category_id,
                    "tag_id": tag_id,
                },
            }
        )

    @app.get("/api/videos/{video_id}")
    async def get_video_api(video_id: int) -> JSONResponse:
        video = database.get_video(video_id)
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        return JSONResponse(video)

    @app.get("/api/videos/{video_id}/transcript")
    async def get_video_transcript(video_id: int, version_id: int | None = Query(None)) -> JSONResponse:
        if version_id is None:
            return JSONResponse(library.load_active_transcript(video_id))
        return JSONResponse(library.load_transcript_version(video_id, version_id))

    @app.get("/api/videos/{video_id}/metadata")
    async def get_video_metadata(video_id: int) -> JSONResponse:
        return JSONResponse(library.load_video_metadata(video_id))

    @app.put("/api/videos/{video_id}/transcript")
    async def update_video_transcript(video_id: int, payload: TranscriptUpdateRequest) -> JSONResponse:
        version_id = library.save_edited_transcript(video_id, payload.text)
        return JSONResponse({"video_id": video_id, "version_id": version_id})

    @app.get("/api/videos/{video_id}/versions")
    async def list_video_versions(video_id: int) -> JSONResponse:
        versions = [asdict(version) for version in database.list_transcript_versions(video_id)]
        return JSONResponse({"items": versions})

    @app.get("/api/videos/{video_id}/versions/{version_id}")
    async def get_video_version(video_id: int, version_id: int) -> JSONResponse:
        return JSONResponse(library.load_transcript_version(video_id, version_id))

    @app.post("/api/videos/{video_id}/versions/{version_id}/activate")
    async def activate_video_version_api(video_id: int, version_id: int) -> JSONResponse:
        database.activate_transcript_version(video_id, version_id)
        return JSONResponse({"video_id": video_id, "version_id": version_id})

    @app.get("/api/videos/{video_id}/segments")
    async def list_video_segments(
        video_id: int,
        version_id: int | None = Query(None),
    ) -> JSONResponse:
        version = (
            database.get_active_transcript_version(video_id)
            if version_id is None
            else database.get_transcript_version(video_id, version_id)
        )
        if version is None:
            raise HTTPException(status_code=404, detail="version not found")
        segments = library.ensure_segments(video_id, version.id)
        return JSONResponse(
            {
                "video_id": video_id,
                "version_id": version.id,
                "items": segments,
                "count": len(segments),
            }
        )

    @app.get("/api/videos/{video_id}/typing/records")
    async def list_typing_records_api(video_id: int, limit: int = Query(5)) -> JSONResponse:
        records = database.list_typing_records(video_id, limit=max(1, min(50, limit)))
        return JSONResponse({"items": records})

    @app.post("/api/videos/{video_id}/typing/records")
    async def create_typing_record(video_id: int, payload: TypingRecordRequest) -> JSONResponse:
        version = database.get_transcript_version(video_id, payload.version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="version not found")
        segment = database.get_segment(video_id, payload.version_id, payload.segment_seq)
        if segment is None:
            raise HTTPException(status_code=404, detail="segment not found")
        segment_text = practice_text(str(segment["text"]))
        correct = max(0, min(int(payload.correct_chars), len(segment_text)))
        errors = max(0, int(payload.error_count))
        duration = max(0.1, float(payload.duration_seconds))
        accuracy = round(correct / (correct + errors) * 100) if correct + errors else 100
        cpm = round(correct / (duration / 60)) if duration > 0 else 0
        record = database.add_typing_record(
            video_id=video_id,
            version_id=payload.version_id,
            segment_seq=payload.segment_seq,
            segment_text=segment_text,
            mode="strict",
            duration_seconds=duration,
            correct_chars=correct,
            error_count=errors,
            cpm=cpm,
            accuracy=accuracy,
        )
        return JSONResponse(record)

    @app.post("/api/videos/{video_id}/typing/custom-records")
    async def create_custom_typing_record(
        video_id: int, payload: CustomTypingRecordRequest
    ) -> JSONResponse:
        if database.get_video(video_id) is None:
            raise HTTPException(status_code=404, detail="video not found")
        segment_text = practice_text(payload.text)
        if not segment_text:
            raise HTTPException(status_code=400, detail="text is required")
        version_id = 0
        if payload.version_id:
            version = database.get_transcript_version(video_id, payload.version_id)
            version_id = version.id if version is not None else 0
        correct = max(0, min(int(payload.correct_chars), len(segment_text)))
        errors = max(0, int(payload.error_count))
        duration = max(0.1, float(payload.duration_seconds))
        accuracy = round(correct / (correct + errors) * 100) if correct + errors else 100
        cpm = round(correct / (duration / 60)) if duration > 0 else 0
        record = database.add_typing_record(
            video_id=video_id,
            version_id=version_id,
            segment_seq=0,
            segment_text=segment_text,
            mode="custom",
            duration_seconds=duration,
            correct_chars=correct,
            error_count=errors,
            cpm=cpm,
            accuracy=accuracy,
        )
        return JSONResponse(record)

    @app.get("/api/categories")
    async def list_categories_api() -> JSONResponse:
        return JSONResponse({"items": database.list_categories()})

    @app.post("/api/categories")
    async def create_category_api(payload: CategoryRequest) -> JSONResponse:
        if not payload.name:
            raise HTTPException(status_code=400, detail="name is required")
        category = database.create_category(payload.name)
        return JSONResponse(category)

    @app.put("/api/categories/{category_id}")
    async def update_category_api(category_id: int, payload: CategoryRequest) -> JSONResponse:
        if not payload.name:
            raise HTTPException(status_code=400, detail="name is required")
        category = database.update_category(category_id, payload.name)
        if category is None:
            raise HTTPException(status_code=404, detail="category not found")
        return JSONResponse(category)

    @app.delete("/api/categories/{category_id}")
    async def delete_category_api(category_id: int) -> JSONResponse:
        database.delete_category(category_id)
        return JSONResponse({"category_id": category_id})

    @app.post("/api/videos/{video_id}/category")
    async def assign_category_api(video_id: int, payload: CategoryRequest) -> JSONResponse:
        category_id = payload.category_id
        if category_id is None and payload.name:
            category = database.create_category(payload.name)
            category_id = int(category["id"])
        database.assign_category(video_id, category_id)
        return JSONResponse({"video_id": video_id, "category_id": category_id})

    @app.get("/api/tags")
    async def list_tags_api() -> JSONResponse:
        return JSONResponse({"items": database.list_tags()})

    @app.post("/api/tags")
    async def create_tag_api(payload: TagRequest) -> JSONResponse:
        if not payload.name:
            raise HTTPException(status_code=400, detail="name is required")
        tag = database.create_tag(payload.name)
        return JSONResponse(tag)

    @app.put("/api/tags/{tag_id}")
    async def update_tag_api(tag_id: int, payload: TagRequest) -> JSONResponse:
        if not payload.name:
            raise HTTPException(status_code=400, detail="name is required")
        tag = database.update_tag(tag_id, payload.name)
        if tag is None:
            raise HTTPException(status_code=404, detail="tag not found")
        return JSONResponse(tag)

    @app.delete("/api/tags/{tag_id}")
    async def delete_tag_api(tag_id: int) -> JSONResponse:
        database.delete_tag(tag_id)
        return JSONResponse({"tag_id": tag_id})

    @app.post("/api/videos/{video_id}/tags")
    async def add_video_tag_api(video_id: int, payload: TagRequest) -> JSONResponse:
        tag_id = payload.tag_id
        if tag_id is None and payload.name:
            tag = database.create_tag(payload.name)
            tag_id = int(tag["id"])
        if tag_id is None:
            raise HTTPException(status_code=400, detail="tag is required")
        database.add_video_tag(video_id, tag_id)
        return JSONResponse({"video_id": video_id, "tag_id": tag_id})

    @app.delete("/api/videos/{video_id}/tags/{tag_id}")
    async def remove_video_tag_api(video_id: int, tag_id: int) -> JSONResponse:
        database.remove_video_tag(video_id, tag_id)
        return JSONResponse({"video_id": video_id, "tag_id": tag_id})

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return app


def _render_correction_page(
    request: Request,
    page,
    *,
    database: AppDatabase,
    library: WorkspaceLibrary,
    video_id: int,
    task_id: str,
    error: str = "",
    corrected_override: str | None = None,
) -> HTMLResponse:
    video = database.get_video(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")
    task = database.get_task(task_id)
    if task is None or task.video_id != video_id:
        raise HTTPException(status_code=404, detail="task not found")
    preview = database.get_correction_preview(task_id)
    left_html = ""
    right_html = ""
    corrected = corrected_override or ""
    estimate = 0
    source_text = ""
    if preview is not None:
        source = library.load_transcript_version(video_id, int(preview["source_version_id"]))
        source_text = str(source["text"])
        corrected = (
            corrected_override
            if corrected_override is not None
            else format_paragraphs(str(preview["text"]))
        )
        left_html, right_html = _diff_html(source_text, corrected)
        estimate = len(segment_text(corrected))
    return page(
        "correction.html",
        request,
        video=video,
        task=asdict(task),
        preview=preview,
        source_text=source_text,
        corrected=corrected,
        left_html=left_html,
        right_html=right_html,
        estimate=estimate,
        error=error,
    )


def _settings_response(
    request: Request,
    page,
    config: AppConfig,
    error: str,
    *,
    notice: str,
    test_result: dict[str, object] | None,
    default_provider: str,
    default_model: str,
    storage: dict[str, str],
    status_code: int = 200,
) -> HTMLResponse:
    return page(
        "settings.html",
        request,
        config=config,
        llm=config.llm,
        api_key_masked=_mask_key(config.llm.api_key),
        default_provider=default_provider,
        default_model=default_model,
        storage=storage,
        error=error,
        notice=notice,
        test_result=test_result,
        status_code=status_code,
    )


def _llm_ready(llm: LLMConfig) -> bool:
    return bool(llm.enabled and llm.base_url and llm.api_key and llm.model)


def _mask_key(key: str) -> str:
    if not key:
        return ""
    return f"****{key[-4:]}" if len(key) > 4 else "****"


def _storage_payload(settings: Settings) -> dict[str, str]:
    return {
        "workspace_root": str(settings.workspace_root),
        "downloads": str(settings.downloads_dir),
        "audio": str(settings.audio_dir),
        "transcripts": str(settings.transcripts_dir),
        "metadata": str(settings.metadata_dir),
        "database": str(settings.app_db_path),
        "config": str(settings.config_path),
    }


def _parse_timeout(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _llm_from_form(
    *,
    enabled: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: int,
    glossary: str,
    current: LLMConfig,
) -> tuple[LLMConfig | None, str]:
    base_url = base_url.strip()
    model = model.strip()
    key = api_key.strip() or current.api_key
    is_enabled = enabled.strip().lower() in ("on", "true", "1", "yes")
    if not 10 <= int(timeout_seconds) <= 300:
        return None, "请求超时需在 10–300 秒之间"
    if base_url and not (base_url.startswith("http://") or base_url.startswith("https://")):
        return None, "Base URL 需以 http:// 或 https:// 开头"
    if is_enabled and not base_url:
        return None, "启用 LLM 修正时必须填写 Base URL"
    if is_enabled and not key:
        return None, "启用 LLM 修正时必须填写 API Key"
    if is_enabled and not model:
        return None, "启用 LLM 修正时必须填写模型名"
    return (
        LLMConfig(
            enabled=is_enabled,
            base_url=base_url,
            api_key=key,
            model=model,
            timeout_seconds=int(timeout_seconds),
            glossary=glossary.strip(),
        ),
        "",
    )


def _diff_html(original: str, corrected: str) -> tuple[str, str]:
    matcher = difflib.SequenceMatcher(a=original, b=corrected, autojunk=False)
    left: list[str] = []
    right: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old = html.escape(original[i1:i2])
        new = html.escape(corrected[j1:j2])
        if tag == "equal":
            left.append(old)
            right.append(new)
        elif tag == "delete":
            left.append(f'<span class="diff-del">{old}</span>')
        elif tag == "insert":
            right.append(f'<span class="diff-add">{new}</span>')
        else:
            left.append(f'<span class="diff-del">{old}</span>')
            right.append(f'<span class="diff-add">{new}</span>')
    return "".join(left), "".join(right)


def _submit_transcription_tasks(
    task_service: TaskService,
    *,
    sources: list[str],
    provider: str,
    model: str,
    prompt: str = "",
) -> list[TaskRecord]:
    return [
        task_service.submit_transcription(
            source=source,
            provider=provider,
            model=model,
            prompt=prompt,
        )
        for source in sources
    ]
