# codey-cli

Save CLI code agent conversations and align them with git commits in a single `codey.html` file.

![demo](https://codey.elsetech.app/demo.png)

## Install

```bash
pip install codey-cli
```

## Quick Start

From your project root:

```bash
codey codex .
```

Then use the agent normally. When you exit the agent, `./codey.html` will be created/updated in the current directory.

## How It Works

- `codey` runs your agent command inside a PTY (keeps the original TUI).
- On exit, it reads new git commits since last run.
- It records your input lines (the text you submit with Enter).
- It writes/updates `codey.html` with a two‑column timeline:
  - Left: git commits (message, author, hash, time)
  - Right: your conversation inputs

## Commands

You can use any CLI agent:

```bash
codey codex .
codey opencode .
codey claude .
```

Or pass any CLI command:

```bash
codey <your-command> [args...]
```

## Output

`codey.html` is saved in the current working directory and updated incrementally.
State is stored in a hidden JSON block inside the HTML, so reruns only append new data.

## Notes

- Only user input lines are recorded (assistant output is not stored).
- Input text is cleaned to remove terminal control sequences.
- Works on macOS and Linux.
