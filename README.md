# gorules_jdm_agent
Agent for creating and modifying rule files compatible with Go Rules Zen Business Rule Engine (https://gorules.io)

## Hugging Face with Together

Set the following values in your local `.env` file to route LLM requests through
Hugging Face Inference Providers using Together:

```env
LLM_PROVIDER=huggingface
HF_TOKEN=hf_your_token
HF_MODEL_NAME=deepseek-ai/DeepSeek-V4-Flash-0731
HF_INFERENCE_PROVIDER=together
```

The agent adds `:together` to the model automatically. You may instead include
the suffix directly in `HF_MODEL_NAME`.

## OpenRouter

Set the following values in your local `.env` file to route LLM requests through
OpenRouter:

```env
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=or_your_key
OPENROUTER_MODEL_NAME=inclusionai/ling-3.0-flash-fin:free
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_REASONING_ENABLED=true
```

`OPENROUTER_SITE_URL` and `OPENROUTER_APP_NAME` are optional attribution headers
you can set if you want requests to identify your app.
