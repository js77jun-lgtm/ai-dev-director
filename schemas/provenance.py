from typing import List, Optional
from pydantic import BaseModel, Field

class AgentProvenanceRecord(BaseModel):
    agent: str = Field(..., description="Agent role: HEAD, BUILDER, or REVIEWER")
    provider: str = Field(..., description="Provider name: e.g. anthropic, google, codex, mock")
    runtime: str = Field(default="cli", description="Runtime identifier: claude-code-cli, antigravity-cli, codex-cli, mock")
    model: str = Field(..., description="Model name or CLI profile")
    executable: str = Field(..., description="Path to official CLI executable")
    invocation_command: str = Field(..., description="Exact invocation mode or CLI command called")
    version: str = Field(..., description="CLI executable version")
    exit_code: Optional[int] = Field(default=None, description="Observed process exit code; None when no process ran")
    duration_sec: Optional[float] = Field(default=None, description="Execution duration in seconds (observed)")
    # --- declared policy values (NOT measured at runtime) ---
    authentication_mode: str = Field(default="subscription", description="DECLARED: official CLI signed-in subscription session is the only permitted auth mode")
    api_key_policy: str = Field(default="disabled", description="DECLARED: API keys are stripped from the CLI environment (get_sanitized_env)")
    api_key_used: bool = Field(default=False, description="DECLARED by policy: no API key is ever passed to a CLI")
    observed_fields: List[str] = Field(default_factory=lambda: ["executable", "version", "exit_code", "duration_sec", "invocation_command"],
                                       description="Fields measured from the actual process execution")
    declared_fields: List[str] = Field(default_factory=lambda: ["authentication_mode", "api_key_policy", "api_key_used"],
                                       description="Fields asserted by policy, not measured")
    timestamp: str = Field(..., description="ISO 8601 timestamp of invocation")

class ProvenanceSchema(BaseModel):
    run_id: str
    records: List[AgentProvenanceRecord] = Field(default_factory=list)
