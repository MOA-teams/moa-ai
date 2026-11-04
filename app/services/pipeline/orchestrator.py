from app.core.errors import FeatureNotReady
from app.schemas.pipeline import PipelineRequest, PipelineResult


class PipelineOrchestrator:
    async def execute(self, request: PipelineRequest) -> PipelineResult:
        # Implement stage execution here, without chat/GitHub side effects.
        raise FeatureNotReady("Pipeline execution")
