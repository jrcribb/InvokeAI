import torch
from PIL.Image import Image

from invokeai.app.invocations.baseinvocation import BaseInvocation, invocation
from invokeai.app.invocations.fields import ImageField, InputField, UIComponent
from invokeai.app.invocations.primitives import StringOutput
from invokeai.app.services.shared.invocation_context import InvocationContext
from invokeai.backend.llava_onevision_model import LlavaOnevisionModel
from invokeai.app.invocations.model import ModelIdentifierField
from invokeai.app.invocations.fields import FieldDescriptions, UIType
from invokeai.backend.model_manager.config import BaseModelType, ModelType
from invokeai.backend.util.devices import TorchDevice


@invocation("llava_onevision_vllm", title="LLaVA OneVision VLLM", tags=["vllm"], category="vllm", version="1.0.0")
class LlavaOnevisionVllmInvocation(BaseInvocation):
    """Run a LLaVA OneVision VLLM model."""

    images: list[ImageField] | ImageField | None = InputField(default=None, description="Input image.")
    prompt: str = InputField(
        default="",
        description="Input text prompt.",
        ui_component=UIComponent.Textarea,
    )
    vllm_model: ModelIdentifierField = InputField(
        title="LLaVA Model Type",
        description=FieldDescriptions.vllm_model,
        ui_type=UIType.LlavaOnevisionModel,
    )

    def _get_images(self, context: InvocationContext) -> list[Image]:
        if self.images is None:
            return []

        image_fields = self.images if isinstance(self.images, list) else [self.images]
        return [context.images.get_pil(image_field.image_name, "RGB") for image_field in image_fields]

    @torch.no_grad()
    def invoke(self, context: InvocationContext) -> StringOutput:
        images = self._get_images(context)

        with context.models.load(self.vllm_model) as vllm_model:
            assert isinstance(vllm_model, LlavaOnevisionModel)
            output = vllm_model.run(
                prompt=self.prompt,
                images=images,
                device=TorchDevice.choose_torch_device(),
                dtype=TorchDevice.choose_torch_dtype(),
            )

        return StringOutput(value=output)
