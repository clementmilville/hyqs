# Job #3758: Remove cancelled generic inference-gateway artifacts

**Date:** 2026-08-16

This diff removes the entire inference gateway module—three files handling strategy inference for financial decisions via Claude and Codex providers. The gateway enforced a closed-vocabulary contract on requests (fixed roles, provider/model allowlists, projection bounds), validated proposals against a schema, and ran inference turns in isolated temp directories with strict tool and file-mutation rejection to prevent LLM-driven code execution. The removal suggests the inference-based strategy feature is being deprecated or consolidated into a different architecture.
This diff removes the entire inference gateway module—three files handling strategy inference for financial decisions via Claude and Codex providers. The gateway enforced a closed-vocabulary contract on requests (fixed roles, provider/model allowlists, projection bounds), validated proposals against a schema, and ran inference turns in isolated temp directories with strict tool and file-mutation rejection to prevent LLM-driven code execution. The removal suggests the inference-based strategy feature is being deprecated or consolidated into a different architecture.

## Files touched
- hyqs/inference_gateway_contracts.py
- hyqs/inference_gateway_providers.py
- hyqs/inference_gateway_store.py
- hyqs/pipeline/supervisor.py
- tests/test_inference_gateway_contracts.py
- tests/test_inference_gateway_providers.py
- tests/test_inference_gateway_store.py
- tests/test_pipeline_loop_fixes.py
- tests/test_pipeline_notifier.py
- tests/test_prompt_speed_pass.py
