from app.schemas.pipeline import PipelineRequest, PipelineResult
from app.services.pipeline.orchestrator import PipelineOrchestrator


async def execute_pipeline(
    request: PipelineRequest, orchestrator: PipelineOrchestrator
) -> PipelineResult:
    """Worker entry point; durable queue/recovery will be added with the pipeline implementation."""
    return await orchestrator.execute(request)
