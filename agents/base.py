"""
Base Agent — Dual-mode LLM integration.

Supports:
1. Anthropic native API (direct Claude access)
2. OpenAI-compatible API (Generali GHO LLM Gateway)

Implements the ReAct loop with tool_use, structured logging,
correlation_id tracking, and graceful error handling.
"""
import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Callable, Any, Optional
import structlog

from config.settings import get_config
from tools.schema_builder import build_tool_schema, build_tool_schema_openai

log = structlog.get_logger()


@dataclass
class AgentConfig:
    """Agent-level configuration."""
    name: str = "base_agent"
    model: str = ""  # empty = use global config
    max_tokens: int = 4096
    temperature: float = 0.0
    system_prompt: str = ""
    max_iterations: int = 10
    use_openai_gateway: bool = False  # True for GHO


class BaseAgent:
    """
    Production-ready base agent with:
    - Dual LLM mode (Anthropic native / OpenAI-compatible)
    - ReAct tool_use loop
    - Structured logging with correlation_id
    - Graceful timeout handling
    """

    def __init__(self, config: AgentConfig, tools: list[Callable] = None):
        self.agent_config = config
        self.app_config = get_config()
        self.tools = tools or []
        self._tool_map = {t.__name__: t for t in self.tools}
        
        # Resolve model
        self.model = config.model or self.app_config.llm.model
        
        # Initialize LLM client based on mode
        if config.use_openai_gateway:
            self._init_openai_client()
        else:
            self._init_anthropic_client()

    def _init_anthropic_client(self):
        """Initialize Anthropic native client."""
        import anthropic
        self.client = anthropic.Anthropic(api_key=self.app_config.llm.api_key)
        self._mode = "anthropic"
        self._tool_schemas = [build_tool_schema(t) for t in self.tools]

    def _init_openai_client(self):
        """Initialize OpenAI-compatible client (GHO Gateway)."""
        from openai import OpenAI
        self.client = OpenAI(
            api_key=self.app_config.llm.api_key,
            base_url=self.app_config.llm.gateway_url,
        )
        self._mode = "openai"
        self._tool_schemas = [build_tool_schema_openai(t) for t in self.tools]

    async def run(
        self,
        task: str,
        context: dict = None,
        correlation_id: str = None,
    ) -> str:
        """
        Execute the agent's ReAct loop.
        
        :param task: The task/prompt to execute
        :param context: Additional context dict
        :param correlation_id: Request correlation ID for tracing
        :returns: Final text response from the agent
        """
        cid = correlation_id or str(uuid.uuid4())[:8]
        
        log.info("agent.run.start",
            agent=self.agent_config.name,
            model=self.model,
            mode=self._mode,
            correlation_id=cid,
        )
        
        if self._mode == "anthropic":
            return await self._run_anthropic(task, context, cid)
        else:
            return await self._run_openai(task, context, cid)

    # =========================================================================
    # ANTHROPIC NATIVE MODE
    # =========================================================================

    async def _run_anthropic(self, task: str, context: dict, cid: str) -> str:
        """ReAct loop using Anthropic native API."""
        messages = [{"role": "user", "content": task}]
        
        for iteration in range(self.agent_config.max_iterations):
            log.info("agent.iteration",
                agent=self.agent_config.name,
                iteration=iteration,
                correlation_id=cid,
            )
            
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=self.agent_config.max_tokens,
                    temperature=self.agent_config.temperature,
                    system=self.agent_config.system_prompt,
                    tools=self._tool_schemas if self.tools else [],
                    messages=messages,
                )
            except Exception as e:
                log.error("agent.llm_error",
                    agent=self.agent_config.name,
                    error=str(e),
                    correlation_id=cid,
                )
                raise

            # Check for final response
            if response.stop_reason == "end_turn":
                result = self._extract_text_anthropic(response)
                log.info("agent.run.complete",
                    agent=self.agent_config.name,
                    iterations=iteration + 1,
                    correlation_id=cid,
                )
                return result

            # Handle tool calls
            if response.stop_reason == "tool_use":
                tool_results = await self._execute_tools_anthropic(response, cid)
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

        log.warning("agent.max_iterations_reached",
            agent=self.agent_config.name,
            correlation_id=cid,
        )
        return self._extract_text_anthropic(response)

    async def _execute_tools_anthropic(self, response, cid: str) -> list:
        """Execute tool calls from Anthropic response."""
        results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = self._tool_map.get(block.name)
                if fn:
                    log.info("agent.tool_call",
                        agent=self.agent_config.name,
                        tool=block.name,
                        inputs=block.input,
                        correlation_id=cid,
                    )
                    try:
                        if asyncio.iscoroutinefunction(fn):
                            result = await fn(**block.input)
                        else:
                            result = fn(**block.input)
                        
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str),
                        })
                    except Exception as e:
                        log.error("agent.tool_error",
                            tool=block.name,
                            error=str(e),
                            correlation_id=cid,
                        )
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps({"error": str(e)}),
                            "is_error": True,
                        })
        return results

    def _extract_text_anthropic(self, response) -> str:
        return next(
            (b.text for b in response.content if hasattr(b, "text")),
            ""
        )

    # =========================================================================
    # OPENAI-COMPATIBLE MODE (GHO Gateway)
    # =========================================================================

    async def _run_openai(self, task: str, context: dict, cid: str) -> str:
        """ReAct loop using OpenAI-compatible API (GHO)."""
        messages = [
            {"role": "system", "content": self.agent_config.system_prompt},
            {"role": "user", "content": task},
        ]
        
        for iteration in range(self.agent_config.max_iterations):
            log.info("agent.iteration",
                agent=self.agent_config.name,
                iteration=iteration,
                mode="openai",
                correlation_id=cid,
            )
            
            kwargs = {
                "model": self.model,
                "messages": messages,
                "max_tokens": self.agent_config.max_tokens,
                "temperature": self.agent_config.temperature,
            }
            if self.tools:
                kwargs["tools"] = self._tool_schemas
            
            try:
                response = self.client.chat.completions.create(**kwargs)
            except Exception as e:
                log.error("agent.llm_error", error=str(e), correlation_id=cid)
                raise
            
            choice = response.choices[0]
            
            # Final response
            if choice.finish_reason == "stop":
                log.info("agent.run.complete",
                    agent=self.agent_config.name,
                    iterations=iteration + 1,
                    correlation_id=cid,
                )
                return choice.message.content or ""
            
            # Tool calls
            if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                messages.append(choice.message)
                
                for tc in choice.message.tool_calls:
                    fn = self._tool_map.get(tc.function.name)
                    if fn:
                        args = json.loads(tc.function.arguments)
                        log.info("agent.tool_call",
                            tool=tc.function.name,
                            inputs=args,
                            correlation_id=cid,
                        )
                        try:
                            if asyncio.iscoroutinefunction(fn):
                                result = await fn(**args)
                            else:
                                result = fn(**args)
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": json.dumps(result, default=str),
                            })
                        except Exception as e:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": json.dumps({"error": str(e)}),
                            })
        
        return choice.message.content or ""
