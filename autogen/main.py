import nest_asyncio
nest_asyncio.apply()  # must be before any event loop is running

import asyncio
import warnings
import yaml
import os
import re

# Suppress autogen duplicate-function registration warnings that clutter the log
# (developer + tester both register the same MCP filesystem tools to UserProxy)
warnings.filterwarnings(
    "ignore", category=UserWarning, message="Function '.*' is being overridden.*"
)
from autogen import (
    AssistantAgent,
    UserProxyAgent,
    GroupChat,
    GroupChatManager
)
from mcp_tools import (
    register_mcp_tools,
    detect_text_tool_call,
    execute_text_tool_call,
    get_tool_registry,
)


class _NotifyList(list):
    """A list that forwards every appended GroupChat message to an asyncio.Queue."""
    def __init__(self, queue):
        self._queue = queue
        super().__init__()

    def append(self, item):          # type: ignore[override]
        super().append(item)
        try:
            self._queue.put_nowait(item)
        except Exception:
            pass


class LocalMultiAgentTeam:
    def __init__(self, config_path="/configs/agent_config.yaml"):
        self.config_path = config_path
        self.agents = {}
        self.load_config()

    def load_config(self):
        """Load agent configuration from YAML file"""
        if not os.path.exists(self.config_path):
            print(f"Config file not found at {self.config_path}, using default configuration")
            self.create_default_config()
        else:
            with open(self.config_path, 'r') as f:
                self.config = yaml.safe_load(f)

        # Configure LLM with lower temperature for more predictable responses
        self.llm_config = {
            "config_list": [{
                "model": self.config['models']['local_llm']['model'],
                "base_url": self.config['models']['local_llm']['base_url'],
                "api_key": "not-needed",
                "timeout": 1200,  # 20 min — CPU inference can be slow; default 10 min is not enough
            }],
            "temperature": 0.1,  # Lower temperature for more focused responses
            "max_tokens": 2048,  # enough for code + tool calls
            "top_p": 0.9,
        }

    def create_default_config(self):
        """Create a default configuration if none exists"""
        self.config = {
            'models': {
                'local_llm': {
                    'model': 'llama2',
                    'base_url': 'http://localhost:11434',
                    'api_type': 'ollama',
                    'temperature': 0.1,
                    'max_tokens': 500
                }
            },
            'agents': {
                'team_lead': {
                    'name': 'TeamLead',
                    'system_message': """You are the TeamLead. You MUST follow these rules EXACTLY:

ROLE: ONLY assign tasks. NEVER do other agents' work.

OUTPUT FORMAT (use EXACTLY this structure):
@[AgentName]: [One specific task]

EXAMPLES:
@WebResearcher: Research efficient Fibonacci calculation methods in Python
@Developer: Write code based on the research provided
@Tester: Test the Fibonacci script with various inputs
@Reviewer: Review the code and test results

RULES:
1. NEVER write code, research, test, or review yourself
2. Assign ONE task at a time to ONE agent
3. Wait for that agent to complete before assigning next task
4. Keep responses under 3 sentences
5. End with "TERMINATE" only after ALL tasks complete

Current task: Fibonacci script creation""",
                    'code_execution_config': False
                },
                'developer': {
                    'name': 'Developer',
                    'system_message': """You are the Developer. You MUST follow these rules EXACTLY:

ROLE: ONLY write code when TeamLead assigns you.

OUTPUT FORMAT (use EXACTLY this structure):
```python
[Your complete Python code here]

def fibonacci(n):
    if n <= 0:
        return []
    # ... rest of code
```""",
                    'code_execution_config': {'work_dir': 'coding', 'use_docker': False}
                },
                'reviewer': {
                    'name': 'Reviewer',
                    'system_message': """You are the Reviewer. You MUST follow these rules EXACTLY:

ROLE: ONLY review code when TeamLead assigns you.

OUTPUT FORMAT (use EXACTLY this structure):
ISSUES FOUND: [List issues or "None"]
SUGGESTIONS: [List improvements or "None"]
VERDICT: [APPROVED/NEEDS REVISION]

RULES:
1. NEVER write code or suggest complete solutions
2. NEVER respond unless TeamLead says "@Reviewer"
3. ONLY review the most recently provided code
4. Keep review under 5 bullet points
5. Be specific about issues

Example:
ISSUES FOUND: No input validation
SUGGESTIONS: Add try-except for user input
VERDICT: NEEDS REVISION""",
                    'code_execution_config': False
                },
                'web_researcher': {
                    'name': 'WebResearcher',
                    'system_message': """You are the WebResearcher. You MUST follow these rules EXACTLY:

ROLE: ONLY provide research when TeamLead assigns you.

OUTPUT FORMAT (use EXACTLY this structure):
KEY FINDINGS:
- [Finding 1]
- [Finding 2]
- [Finding 3]

RECOMMENDATIONS:
- [Recommendation based on findings]

RULES:
1. NEVER write code or suggest implementations
2. NEVER respond unless TeamLead says "@WebResearcher"
3. ONLY provide factual information
4. Keep research under 5 bullet points
5. Focus on the specific research question asked

Example:
KEY FINDINGS:
- Iterative method is O(n), recursive is O(2^n)
- Use with open() for safe file handling
- Validate input as positive integer

RECOMMENDATIONS: Use iterative approach for efficiency""",
                    'code_execution_config': False
                },
                'tester': {
                    'name': 'Tester',
                    'system_message': """You are the Tester. You MUST follow these rules EXACTLY:

ROLE: ONLY test code when TeamLead assigns you.

OUTPUT FORMAT (use EXACTLY this structure):
TEST CASES EXECUTED:
- [Test case 1]: [PASS/FAIL]
- [Test case 2]: [PASS/FAIL]
- [Test case 3]: [PASS/FAIL]

COVERAGE: [List what was tested]
RESULTS: [Overall PASS/FAIL]

RULES:
1. NEVER fix code or suggest implementations
2. NEVER respond unless TeamLead says "@Tester"
3. ONLY test the most recently provided code
4. Include edge cases in testing
5. Report results clearly without opinions

Example:
TEST CASES EXECUTED:
- n=5: PASS
- n=0: PASS
- n=-1: FAIL (handled with error message)
- Input "abc": PASS (handles ValueError)

COVERAGE: Valid inputs, edge cases, error handling
RESULTS: PASS (with note about negative input handling)""",
                    'code_execution_config': {'work_dir': 'tests', 'use_docker': False}
                }
            }
        }

    def create_agents(self):
        """Create all agents from configuration"""
        for agent_name, agent_config in self.config['agents'].items():
            self.agents[agent_name] = AssistantAgent(
                name=agent_config['name'],
                system_message=agent_config['system_message'],
                llm_config=self.llm_config,
                code_execution_config=agent_config.get('code_execution_config', False)
            )

        # Create user proxy with termination detection
        # max_consecutive_auto_reply > 0 so it can relay MCP tool-call results back
        self.user_proxy = UserProxyAgent(
            name="UserProxy",
            human_input_mode="NEVER",
            max_consecutive_auto_reply=100,  # GroupChat: sender is always GCManager so this
                                              # effectively caps total tool-call executions.
                                              # 10 (old default) was too low for multi-step tasks.
            is_termination_msg=lambda x: x.get("content", "").rstrip().endswith("TERMINATE"),
            code_execution_config=False
        )

        # Register MCP tools with agents that are configured to use them
        register_mcp_tools(self.config, self.agents, self.user_proxy)

    def validate_response(self, message, expected_role):
        """Validate that response follows role-specific format"""
        if expected_role == "Developer" and "```python" not in message:
            return False, "Developer must provide code in ```python blocks"
        elif expected_role == "Reviewer" and "ISSUES FOUND:" not in message:
            return False, "Reviewer must use ISSUES FOUND: format"
        elif expected_role == "Tester" and "TEST CASES EXECUTED:" not in message:
            return False, "Tester must use TEST CASES EXECUTED: format"
        elif expected_role == "WebResearcher" and "KEY FINDINGS:" not in message:
            return False, "WebResearcher must use KEY FINDINGS: format"
        elif expected_role == "TeamLead" and "@" not in message and "TERMINATE" not in message:
            return False, "TeamLead must assign tasks with @AgentName or end with TERMINATE"
        return True, "Valid"

    async def run_development_team(self, task, message_queue=None):
        """Run the multi-agent development team with strict turn-taking.

        Args:
            task: Task description string.
            message_queue: Optional asyncio.Queue.  When supplied every GroupChat
                message is forwarded to it so callers (e.g. the web server) can
                stream progress in real time.
        """

        agents_list = [
            self.agents['team_lead'],
            self.agents['web_researcher'],
            self.agents['developer'],
            self.agents['tester'],
            self.agents['reviewer'],
            self.user_proxy
        ]

        # Track task completion
        task_stage = {
            'research_done': False,
            'code_written': False,
            'tested': False,
            'reviewed': False
        }
        # Track which agent issued a text-format tool call (local model quirk)
        pending_text_caller: dict = {"name": None}
        # Loop-break counters: consecutive empty replies and repeated delegations
        _empty_count: dict = {}      # agent_name -> consecutive empty reply count
        _delegate_count: dict = {}   # agent_name -> consecutive delegation count by TeamLead
        _tool_call_count: dict = {}  # agent_name -> tool-call executions in current stage
        TOOL_CALL_LIMIT = 8          # force-advance after this many tool calls per stage

        # Reverse lookup: agent.name ("WebResearcher") → agent object
        # self.agents uses YAML keys ("web_researcher"), not display names.
        agents_by_name: dict = {
            agent.name: agent for agent in self.agents.values()
        }
        agents_by_name[self.user_proxy.name] = self.user_proxy

        def custom_speaker_selector(last_speaker, groupchat):
            """
            Route speakers using a strict state machine.
            Only inspect @mentions when TeamLead is the last speaker with
            non-empty content.  All other cases default back to TeamLead.
            Pending tool_calls are routed to UserProxy for execution.
            """
            messages = groupchat.messages
            if not messages:
                return self.agents['team_lead']

            last_msg = messages[-1]
            last_speaker_name = last_speaker.name
            last_content = (last_msg.get("content") or "").strip()

            # --- Route STRUCTURED tool_calls to the executor (UserProxy) -----
            if last_msg.get("tool_calls"):
                pending_text_caller["name"] = None  # clear text-call state
                _empty_count[last_speaker_name] = 0  # structured call = active
                return self.user_proxy

            # --- Route TEXT-FORMAT tool calls to UserProxy -------------------
            # (local models like llama3.1:8b output raw JSON instead of tool_calls)
            if last_content and last_speaker_name != "UserProxy":
                parsed = detect_text_tool_call(last_content)
                if parsed and parsed["name"] in get_tool_registry():
                    pending_text_caller["name"] = last_speaker_name
                    _empty_count[last_speaker_name] = 0
                    return self.user_proxy

            # --- After UserProxy executes a tool, route back to the caller ---
            if last_speaker_name == "UserProxy":
                # 1. Structured tool_calls path
                for msg in reversed(messages[:-1]):
                    if msg.get("tool_calls"):
                        caller_name = msg.get("name", "")
                        if caller_name in agents_by_name:
                            pending_text_caller["name"] = None
                            # Track how many tool-call round-trips this specialist has used
                            _tool_call_count[caller_name] = _tool_call_count.get(caller_name, 0) + 1
                            if _tool_call_count[caller_name] >= TOOL_CALL_LIMIT:
                                print(f"[loop-break] {caller_name} used {_tool_call_count[caller_name]} "
                                      f"tool calls — force-advancing stage.")
                                _tool_call_count[caller_name] = 0
                                _empty_count[caller_name] = 0
                                _delegate_count[caller_name] = 0
                                _stage_done_for(caller_name)
                                return self.agents['team_lead']
                            return agents_by_name[caller_name]
                # 2. Text tool call path
                if pending_text_caller["name"] and pending_text_caller["name"] in agents_by_name:
                    name = pending_text_caller["name"]
                    pending_text_caller["name"] = None
                    return agents_by_name[name]

            # --- Helpers for loop detection -----------------------------------
            def _stage_done_for(agent_name: str) -> None:
                """Force-advance whichever stage this agent owns."""
                if agent_name == "WebResearcher":
                    task_stage['research_done'] = True
                elif agent_name == "Developer":
                    task_stage['code_written'] = True
                elif agent_name == "Tester":
                    task_stage['tested'] = True
                elif agent_name == "Reviewer":
                    task_stage['reviewed'] = True

            EMPTY_LIMIT = 2      # consecutive empty replies before force-advancing
            DELEGATE_LIMIT = 3   # repeated TeamLead delegations before force-advancing

            # --- After a specialist responds, mark stage and return TeamLead ---
            # Count empty replies; force-advance after threshold
            if last_speaker_name in ("WebResearcher", "Developer", "Tester", "Reviewer"):
                if last_content:
                    _empty_count[last_speaker_name] = 0
                    _delegate_count[last_speaker_name] = 0
                    _tool_call_count[last_speaker_name] = 0  # reset on clean text reply
                    _stage_done_for(last_speaker_name)
                else:
                    _empty_count[last_speaker_name] = _empty_count.get(last_speaker_name, 0) + 1
                    if _empty_count[last_speaker_name] >= EMPTY_LIMIT:
                        print(f"[loop-break] {last_speaker_name} sent {_empty_count[last_speaker_name]} "
                              f"empty replies — force-advancing stage.")
                        _empty_count[last_speaker_name] = 0
                        _delegate_count[last_speaker_name] = 0
                        _tool_call_count[last_speaker_name] = 0
                        _stage_done_for(last_speaker_name)
                return self.agents['team_lead']

            # --- After TeamLead speaks, route to the mentioned specialist -----
            # Enforce strict stage order: Research → Develop → Test → Review
            if last_speaker_name == "TeamLead" and last_content:
                # Count consecutive delegations to the same agent (loop detection)
                for agent_name in ("WebResearcher", "Developer", "Tester", "Reviewer"):
                    if f"@{agent_name}" in last_content:
                        _delegate_count[agent_name] = _delegate_count.get(agent_name, 0) + 1
                        _tool_call_count[agent_name] = 0  # fresh delegation = fresh tool-call budget
                        if _delegate_count[agent_name] > DELEGATE_LIMIT:
                            print(f"[loop-break] TeamLead delegated to {agent_name} "
                                  f"{_delegate_count[agent_name]}x — force-advancing stage.")
                            _delegate_count[agent_name] = 0
                            _empty_count[agent_name] = 0
                            _stage_done_for(agent_name)
                        break
                if not task_stage['research_done'] and "@WebResearcher" in last_content:
                    return self.agents['web_researcher']
                if not task_stage['code_written'] and "@Developer" in last_content:
                    return self.agents['developer']
                if not task_stage['tested'] and "@Tester" in last_content:
                    return self.agents['tester']
                # Gate: Reviewer and TERMINATE require testing to be done first
                if not task_stage['tested']:
                    return self.agents['tester']
                if not task_stage['reviewed'] and "@Reviewer" in last_content:
                    return self.agents['reviewer']

            # Default: TeamLead speaks (initial call, empty turns, stage transitions)
            return self.agents['team_lead']

        # Create group chat with strict rules
        group_chat = GroupChat(
            agents=agents_list,
            messages=[],
            max_round=50,
            speaker_selection_method=custom_speaker_selector,
            allow_repeat_speaker=True  # TeamLead may need multiple turns to re-delegate
        )

        # Attach live-streaming capture when called from the web server
        if message_queue is not None:
            group_chat.messages = _NotifyList(message_queue)

        manager = GroupChatManager(
            groupchat=group_chat,
            llm_config=self.llm_config
        )

        # --- Intercept text-format tool calls (llama3.1:8b quirk) -----------
        # Local models sometimes output {"name":"..","parameters":{..}} as plain
        # text instead of using the structured tool_calls field.  This reply
        # function runs before UserProxy's default auto-reply, detects the JSON
        # pattern, executes the tool via MCP and returns the result as the reply.
        def _text_tool_intercept(recipient, messages=None, sender=None, config=None):
            if not messages:
                return False, None
            last = messages[-1]
            content = (last.get("content") or "").strip()
            if not content:
                return False, None
            parsed = detect_text_tool_call(content)
            if not parsed:
                return False, None
            tool_name = parsed["name"]
            tool_args  = parsed.get("arguments") or {}
            registry = get_tool_registry()
            if tool_name not in registry:
                return False, None
            print(f"\n[TextToolCall] Intercepted text call → '{tool_name}' args={tool_args}")
            try:
                result = execute_text_tool_call(tool_name, tool_args)
                if result is None:
                    return False, None
                snippet = result[:300].replace("\n", " ")
                print(f"[TextToolCall] Result: {snippet}...")
                return True, f"Tool '{tool_name}' result:\n{result}"
            except Exception as exc:
                return True, f"Tool '{tool_name}' error: {exc}"

        self.user_proxy.register_reply(
            trigger=lambda _: True,   # fire for any sender inside GroupChat
            reply_func=_text_tool_intercept,
            position=0,               # run before default auto-reply
        )

        # Initial message from UserProxy — plain task description, no @mentions
        # that could falsely trigger the speaker selector.
        initial_message = f"""TASK: {task}

TeamLead, coordinate the team through these stages in order:
  1. Research  — delegate to the WebResearcher
  2. Development — delegate to the Developer (save code to /workspace via MCP filesystem tools)
  3. Testing   — delegate to the Tester (run the tests via MCP shell tools)
  4. Review    — delegate to the Reviewer

Use the format:  @AgentName: <specific instruction>
When all stages are done reply with exactly: TERMINATE
"""

        # Start the conversation
        await self.user_proxy.a_initiate_chat(
            manager,
            message=initial_message,
            clear_history=True
        )

    def save_team_output(self, output_dir="output"):
        """Save team conversation and artifacts"""
        os.makedirs(output_dir, exist_ok=True)
        print(f"Outputs saved to {output_dir}")

async def main(team):
    # Create necessary directories
    os.makedirs("coding", exist_ok=True)
    os.makedirs("tests", exist_ok=True)
    os.makedirs("output", exist_ok=True)

    tasks = [
        "Create a Python script that calculates Fibonacci numbers and saves results to a file",
    ]

    print(f"Using LLM: {team.llm_config['config_list'][0]['model']}")
    print(f"Temperature: 0.1 | Max tokens: 800 | Timeout: 1200s")

    for task in tasks:
        print(f"\n{'='*60}")
        print(f"Executing task: {task}")
        print(f"{'='*60}\n")
        await team.run_development_team(task)
        team.save_team_output()
        print(f"\n{'='*60}")
        print(f"Task completed!")
        print(f"{'='*60}\n")

if __name__ == "__main__":
    print("Initializing Multi-Agent Team...")
    # Initialise agents BEFORE asyncio.run() so that MCP tool registration
    # (which uses asyncio.run internally) doesn't conflict with the running loop.
    team = LocalMultiAgentTeam()
    team.create_agents()
    asyncio.run(main(team))
