---
name: Cerebras Inference
description: Use this to write code to call an LLM using LiteLLM and OpenRouter with the Cerebras inference provider
---

# Calling an LLM via Cerebras

These instructions allow you write code to call an LLM with Cerebras specified as the inference provider.  
This method uses LiteLLM and OpenRouter.

## Setup

The OPENROUTER_API_KEY must be set in the .env file and loaded in as an environment variable
(e.g. `python-dotenv`) before calling `completion`.

The uv project must include litellm and pydantic.
`uv add litellm pydantic`

## Code snippets

Use code like these examples in order to use Cerebras.

### Imports and constants

```python
from litellm import completion
MODEL = "openrouter/openai/gpt-oss-120b"
# `only` pins the request to Cerebras. `order` alone still allows OpenRouter to
# fall back to another provider silently, so do not use it.
EXTRA_BODY = {"provider": {"only": ["cerebras"]}}
```

### Code to call via Cerebras for a text response

```python
response = completion(model=MODEL, messages=messages, reasoning_effort="low", max_tokens=4000, extra_body=EXTRA_BODY)
result = response.choices[0].message.content
```

### Code to call via Cerebras for a Structured Outputs response

```python
response = completion(model=MODEL, messages=messages, response_format=MyBaseModelSubclass, reasoning_effort="low", max_tokens=4000, extra_body=EXTRA_BODY)
result = response.choices[0].message.content
result_as_object = MyBaseModelSubclass.model_validate_json(result)
```

### Notes

- Always pass `max_tokens`. Without it OpenRouter reserves a very large output
  budget and rejects the request with HTTP 402 ("requires more credits, or fewer
  max_tokens") on the Cerebras route. A few thousand tokens is plenty for filling
  document fields.
- To confirm the provider that served a response: `response.model_extra.get("provider")`
  should read `"Cerebras"`.