"""Local-only structured-output model.

The safety-critical SYSTEM_PROMPT now lives in rewrite_common.py (shared with
the Lambda). This file only holds the pydantic shape the Anthropic SDK's
messages.parse() validates against, which is used by the local runner.
"""

from pydantic import BaseModel, Field


class Rewrite(BaseModel):
    """The only thing the model is allowed to produce (local runner)."""

    plain_headline: str = Field(
        description="A clear one-line headline in sentence case, 12 words or fewer."
    )
    plain_summary: str = Field(
        description="One to three short plain-language sentences a general reader can understand."
    )
