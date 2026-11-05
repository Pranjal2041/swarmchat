# Agent teams on ProgramBench

[ProgramBench](https://github.com/facebookresearch/ProgramBench) gives an agent a compiled program and its
documentation, and asks it to rebuild the program from scratch. This folder runs coding agents on it, alone or as a
team that talks over swarmchat. You describe the run in a config file: the tasks, the time, the agent and its model,
the team's size, its roles and channels, and the prompts each role gets.

## Quick start

On a Linux x86_64 machine with Docker and Python 3.11 or later. The ProgramBench images are built for x86_64 only.

```bash
# 1. The code, and ProgramBench's scorer.
git clone https://github.com/Pranjal2041/swarmchat.git
cd swarmchat/extras/programbench
pip install programbench                    # or: uv tool install programbench

# 2. The agent's command-line tool. Claude Code installs to ~/.local/bin/claude, where the configs look for it.
curl -fsSL https://claude.ai/install.sh | bash

# 3. Your API key. run.py adds it to the agents' model calls, and it never enters a container.
export ANTHROPIC_API_KEY=...

# 4. See the team and each agent's prompt. This starts nothing.
python3 run.py configs/flat.toml --dry-run

# 5. A short try: eight agents, one task, 15 minutes.
python3 run.py configs/flat.toml --task sitkevij__hex.61ae69b --hours 0.25 --name try

# 6. Score it.
programbench eval runs/try/submissions
```

Without `--task` and `--hours`, a config runs all its tasks for its full time. While a team works, `run.py` prints a
command that opens its chat in your browser. Ctrl-C stops every agent and still saves the submissions. Running the
same config again skips the tasks that already finished.

### With Codex

From [Codex 0.159.2](https://github.com/openai/codex/releases/tag/rust-v0.159.2), download
`codex-x86_64-unknown-linux-musl.tar.gz` and `codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz`. Unpack both into
`~/agents/` and rename them `codex` and `codex-code-mode-host`. Codex runs its tools through the second one. Then use
the Codex lines of the config's `[agent]` section, and `export OPENAI_API_KEY=...`.

## What a run writes

```
runs/<name>/submissions/<task>/submission.tar.gz                 the final submissions, for programbench eval
runs/<name>/snapshots/<minutes>/<task>/submission.tar.gz         a snapshot every snapshot_minutes
runs/<name>/work/<task>/                                         the chat, each agent's logs and its workspace
```

Each snapshot folder is scored the same way, for example `programbench eval runs/flat8/snapshots/00060`. The score
over time in the blog post came from snapshots like these.

## How a run works

- Each agent has its own container from the task's `task_cleanroom_v6` image, with no network, 20 CPUs and 60 GB,
  as in the official baseline.
- The reference program is mounted read-only at `/reference/executable`, and its copy in `/workspace` is removed,
  so the agent's own build at `./executable` cannot replace it. The prompt tells the agent this.
- The agent's model calls go to the model API through `run.py`, which adds your API key. The key never enters a
  container.
- A team shares a git repository, `/shared/repo.git`, which is the `origin` remote of each agent's `/workspace`. The
  team's submission is that repository's branch when the time runs out. A single agent's submission is its
  `/workspace`, as in the official baseline.
- A team's chat is a swarmchat server with one user per agent. Each new message reaches the agent while it works:
  Claude Code gets it through its hooks, and Codex gets it added to its running turn.
- Each time an agent ends its turn early, it is sent back to work with the role's loop message, until the time runs
  out. Ten minutes before the end, it gets the official time warning.

## The config

`configs/` has one example for each arrangement:

| Config | The team |
| --- | --- |
| `single.toml` | One agent, set up like the official baseline. |
| `flat.toml` | Eight agents that organize themselves in #general. The blog post ran this with 2, 4, 8 and 16. |
| `lead.toml` | One agent leads and writes no features. Seven workers. |
| `features.toml` | Three sub-teams, each owning an area of the program. |
| `phased.toml` | For 40 minutes the team writes a spec and tests, and nobody writes the program. Then it builds. |
| `testers.toml` | Builders write the program. Testers turn differences from the reference into tests. |
| `owner.toml` | One agent alone pushes to the shared branch. The others send it patches through a second repository. |

The settings:

| Key | Meaning |
| --- | --- |
| `name` | The run's folder under `runs/`. |
| `tasks` | ProgramBench task ids. |
| `hours` | Agent time per task. |
| `snapshot_minutes` | How often to save the submission. 0 saves only the final one. |
| `parallel` | How many tasks run at once. |
| `loop` | Send an agent back to work when it ends its turn early. Default true. |
| `[agent] kind` | `claude`, `codex` or `command`. |
| `[agent] binary` | The agent's Linux x86_64 tool. It is mounted read-only into each container. |
| `[agent] model`, `effort` | The model and its reasoning effort. |
| `[agent] api_base`, `api_key_env`, `api_auth` | Where model calls go, the variable holding the key, and whether the key goes in `x-api-key` or `Authorization: Bearer`. They default to Anthropic's or OpenAI's API. |
| `[agent] tools` | Claude Code's tools. Default `Bash`, `Read`, `Edit` and `Write`. |
| `[agent] files` | More files mounted beside the binary. For Codex it defaults to `codex-code-mode-host`. |
| `[docker] command`, `cpus`, `memory`, `image_tag` | How to call Docker, for example `"sudo docker"`, and each container's limits. |
| `[team] members` | The team's size. Leave out `[team]` for a single agent. |
| `[team] chat` | Whether the team has a chat. Default true. |
| `[team.channels]` | Each channel's members: `"all"`, or a list of role names. Default `general = "all"`. |
| `[[team.roles]]` | A role: its `name`, its `prompt` file, and optionally its `loop` file, `count` or `share` of the members, and `push = false` to keep it from pushing to the shared branch. Roles with no count or share split the remaining members evenly. |
| `[team.phase]` | A first phase: its `minutes`, the `loop` file used during it, and the `notice` every agent gets when it ends. |
| `[prompts]` | Other files in place of the base prompts below. |
| `[hide]` | For a task, files in the image that read as empty in the containers. |

Paths are relative to the config file.

## Prompts

The prompts are text files in `prompts/`. An agent's prompt is built from them in this order:

- `task.md`: the official task. Where it says `{{submission}}`, a single agent gets `single.md`, and a team member
  gets `team.md`.
- `team.md`: who the agent is and how the team submits. Where it says `{{chat}}` it gets `chat.md`, if the team has
  a chat, and where it says `{{role_prompt}}` it gets the role's prompt file.
- `system.md`: the official rules, added to the agent's system prompt.
- `loop.md` and `team-loop.md`: the loop message, when a role has none of its own. `time-warning.md`: the official
  warning, with `{{minutes}}`.

A role's prompt can use `{{id}}`, `{{n}}`, `{{others}}`, `{{team}}`, `{{role}}`, `{{branch}}`, `{{shared}}`,
`{{incoming}}`, `{{phase_minutes}}`, and any role's name, which gives the agents in that role. For example `{{lead}}`
is `a1` in `lead.toml`. `--dry-run` shows the result, and it stops on a placeholder it does not know.

`system.md`, `task.md` and `time-warning.md` are adapted from mini-swe-agent's
[programbench.yaml](https://github.com/SWE-agent/mini-swe-agent), under its MIT license in
`prompts/LICENSE-mini-swe-agent.md`.

## Your own agent

With `kind = "command"`, `run.py` runs `command` in the container, from `/workspace`, as the agent. For example:

```toml
[agent]
kind = "command"
binary = "./my-agent"                  # mounted at /opt/agent/my-agent
command = ["/opt/agent/my-agent"]
model = "my-model"
api_base = "https://api.example.com"
api_key_env = "EXAMPLE_API_KEY"
```

It finds what it needs in its environment:

| Variable | Meaning |
| --- | --- |
| `AGENT_PROMPT_FILE` | The task prompt, and on later runs the loop message with any new messages. |
| `AGENT_SYSTEM_FILE` | The official rules. |
| `AGENT_MODEL`, `AGENT_EFFORT` | From the config. |
| `AGENT_MODEL_URL` | `http://127.0.0.1:8765`. Requests keep their path, and `run.py` sends them to `api_base` with your key. |
| `AGENT_NOTICES` | A folder where each new chat message, and the time warning, arrives as a JSON file with a `text` field. Remove a file once the agent has it. |
| `AGENT_DEADLINE` | When the time runs out, in seconds since 1970. |
| `SWARMCHAT_SERVER`, `SWARMCHAT_TOKEN` | The team's chat. The `swarmchat` command is on the PATH. |

When the command exits before the time runs out, it is run again with the loop message.

## The ten tasks in the blog post

```toml
tasks = ["drew-alleman__datasurgeon.d257cee", "sitkevij__hex.61ae69b", "blacknon__hwatch.edfcb62",
         "yoav-lavi__melody.f4af9b4", "pier-cli__pier.5e1bde9", "ismaelgv__rnr.fc0733b", "rvben__rumdl.2d75c4d",
         "ecumene__rust-sloth.051c559", "alexpovel__srgn.89f943b", "sqlite__sqlite.839433d"]

# The SQLite image has SQLite's own library and headers, which a build could use in place of its own code.
[hide]
"sqlite__sqlite.839433d" = ["/usr/include/sqlite3.h", "/usr/include/sqlite3ext.h",
    "/usr/lib/x86_64-linux-gnu/libsqlite3.a", "/usr/lib/x86_64-linux-gnu/libsqlite3.so.0.8.6",
    "/usr/lib/x86_64-linux-gnu/pkgconfig/sqlite3.pc",
    "/usr/lib/python3.10/lib-dynload/_sqlite3.cpython-310-x86_64-linux-gnu.so"]
```
