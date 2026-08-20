"""Local LLM backend — Chat Completions against the gateway."""
from ..config import GATEWAY_BASE_URL, LOCAL_LLM_MODEL, require_gateway_key


def call_local_llm(prompt: str) -> str:
    """OpenAI-compatible Chat Completions against the gateway."""
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required for the gateway LLM. Install it with:\n"
            "    pip install -r requirements.txt"
        ) from e

    if not GATEWAY_BASE_URL:
        raise RuntimeError(
            "BASE_URL is not set. Add your OpenAI-compatible gateway URL to .env."
        )

    client = OpenAI(base_url=GATEWAY_BASE_URL, api_key=require_gateway_key())
    response = client.chat.completions.create(
        model=LOCAL_LLM_MODEL,
        temperature=0.7,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""
