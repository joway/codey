import os
import pty
import re
import select
import subprocess
import sys
import termios
import tty
import json
import codecs
import fcntl
import signal
import struct
from datetime import datetime, timezone
from pathlib import Path

CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")
RGB_RE = re.compile(r"(?:rgb:|gb:)[0-9a-fA-F]{4}/[0-9a-fA-F]{4}/[0-9a-fA-F]{4}")
ITERM_OK_RE = re.compile(r"[>|\\s]*iTerm2\\s+[^;]*;OK")
ITERM_GI_RE = re.compile(r"Gi=31337;OK")
ITERM_PREFIX_RE = re.compile(r"[>|\\s]*iTerm2\\s+[0-9.]+/?")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


class SimpleLineRecorder:
    def __init__(self):
        self.buf = []
        self.esc = False
        self.esc_buf = ""
        self.esc_mode = None
        self.osc_escape = False
        self.decoder = codecs.getincrementaldecoder("utf-8")()

    def feed(self, data):
        lines = []
        text = self.decoder.decode(data)
        for ch in text:
            if self.esc:
                if self.esc_mode == "esc_start":
                    if ch == "[":
                        self.esc_mode = "csi"
                    elif ch == "O":
                        self.esc_mode = "ss3"
                    elif ch == "]":
                        self.esc_mode = "osc"
                    else:
                        self.esc = False
                        self.esc_mode = None
                    continue
                if self.esc_mode == "csi":
                    if ch.isalpha() or ch == "~":
                        self.esc = False
                        self.esc_mode = None
                    continue
                if self.esc_mode == "ss3":
                    self.esc = False
                    self.esc_mode = None
                    continue
                if self.esc_mode == "osc":
                    if self.osc_escape:
                        if ch == "\\":
                            self.esc = False
                            self.esc_mode = None
                        self.osc_escape = False
                        continue
                    if ch == "\x07":
                        self.esc = False
                        self.esc_mode = None
                        continue
                    if ch == "\x1b":
                        self.osc_escape = True
                    continue
                continue
            if ch == "\x1b":
                self.esc = True
                self.esc_mode = "esc_start"
                self.esc_buf = ""
                continue
            if ch in ("\x7f", "\x08"):
                if self.buf:
                    self.buf.pop()
                continue
            if ch == "\x01":
                continue
            if ch == "\x05":
                continue
            if ch == "\x04":
                continue
            if ch in ("\r", "\n"):
                line = "".join(self.buf)
                lines.append(line)
                self.buf = []
                continue
            if ch >= " ":
                self.buf.append(ch)
        return lines




class OutputBuilder:
    def __init__(self):
        self.current = []
        self.cursor = 0

    def _flush_line(self):
        line = "".join(self.current)
        self.current = []
        self.cursor = 0
        return line

    def feed_text(self, text):
        lines = []
        for ch in text:
            if ch == "\n":
                lines.append(self._flush_line())
                continue
            if ch == "\r":
                self.cursor = 0
                continue
            if ch in ("\b", "\x7f"):
                if self.cursor > 0:
                    del self.current[self.cursor - 1]
                    self.cursor -= 1
                continue
            if ch == "\t":
                ch = "    "
            if len(ch) > 1:
                for sub in ch:
                    lines.extend(self.feed_text(sub))
                continue
            if self.cursor == len(self.current):
                self.current.append(ch)
            else:
                self.current[self.cursor] = ch
            self.cursor += 1
        return lines

    def flush_tail(self):
        if not self.current:
            return ""
        return self._flush_line()


def strip_ansi(text):
    text = OSC_RE.sub("", text)
    text = CSI_RE.sub("", text)
    return text


def sanitize_text(text):
    text = strip_ansi(text)
    text = RGB_RE.sub("", text)
    text = ITERM_OK_RE.sub("", text)
    text = ITERM_GI_RE.sub("", text)
    text = ITERM_PREFIX_RE.sub("", text)
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    return text.strip()


def parse_state(html_text):
    match = re.search(r"<script id=\"codey-state\" type=\"application/json\">(.*?)</script>", html_text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def load_state(path):
    if not path.exists():
        return {"version": 1, "last_commit": None, "timeline": [], "conversations": []}
    html_text = path.read_text(encoding="utf-8")
    state = parse_state(html_text)
    if not state:
        return {"version": 1, "last_commit": None, "timeline": [], "conversations": []}
    state.setdefault("version", 1)
    state.setdefault("last_commit", None)
    state.setdefault("timeline", [])
    state.setdefault("conversations", [])
    return state


def render_html(render_state, state_json, title):
    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>codey</title>
  <style>
    :root {{
      --bg: #f6f8fa;
      --panel: #ffffff;
      --text: #24292f;
      --muted: #57606a;
      --border: #d0d7de;
      --accent: #0969da;
      --git: #F05032;
      --code: #f6f8fa;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    }}
    header {{
      padding: 16px 20px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
      font-family: "Mona Sans", "Hubot Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      font-weight: 700;
      font-size: 20px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    main {{
      display: grid;
      grid-template-columns: 1fr 2fr;
      gap: 16px;
      padding: 16px;
      max-width: 1200px;
      margin: 0 auto;
    }}
    section {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      overflow: auto;
      min-height: 70vh;
    }}
    .timeline {{
      display: grid;
      grid-template-columns: 1fr 40px 2fr;
      gap: 16px 0;
      align-items: start;
    }}
    .row {{
      display: contents;
    }}
    .cell {{
      padding: 6px 12px;
    }}
    .axis {{
      position: relative;
      padding: 0;
    }}
    .axis:before {{
      content: "";
      position: absolute;
      left: 50%;
      top: 0;
      bottom: 0;
      width: 2px;
      background: var(--border);
      transform: translateX(-1px);
    }}
    .dot {{
      position: absolute;
      left: 50%;
      top: 18px;
      width: 12px;
      height: 12px;
      background: var(--accent);
      border: 2px solid var(--accent);
      border-radius: 50%;
      transform: translateX(-6px);
      z-index: 1;
    }}
    .dot.git {{ border-color: var(--git); background: var(--git); }}
    .dot.user {{ border-color: var(--accent); background: var(--accent); }}
    h2 {{
      margin: 0 0 12px 0;
      font-size: 14px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
    }}
    .commit {{
      padding: 8px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      position: relative;
    }}
    .commit-title {{ font-weight: 600; }}
    .commit-meta {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    .hash {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }}
    .message {{ margin: 12px 0; }}
    .message.user {{ background: #eaeef2; border-radius: 8px; padding: 8px 10px; border: 1px solid var(--border); }}
    .message.assistant {{ background: var(--code); border-radius: 8px; padding: 8px 10px; border: 1px solid var(--border); }}
    .message-header {{ font-size: 12px; color: var(--muted); margin-bottom: 6px; }}
    .message-body {{ white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }}
    @media (max-width: 900px) {{
      main {{ grid-template-columns: 1fr; }}
      .timeline {{ grid-template-columns: 1fr; }}
      .axis {{ display: none; }}
    }}
  </style>
</head>
<body>
  <header>{escape_html(title)}</header>
  <main>
    <section style="grid-column: 1 / -1;">
      <div class="timeline">
        <div><h2>Commits</h2></div>
        <div></div>
        <div><h2>Conversation</h2></div>
        {render_timeline(render_state.get("timeline", []), render_state.get("conversations", []))}
      </div>
    </section>
  </main>
  <script id=\"codey-state\" type=\"application/json\">{state_json}</script>
</body>
</html>
"""


def render_timeline(commits, messages):
    groups = _merge_timeline(commits, messages)
    if not groups:
        return (
            "<div class=\"row\"><div class=\"cell\">"
            "<div class=\"commit\"><div class=\"commit-title\">No commits yet</div></div>"
            "</div><div class=\"cell axis\"><span class=\"dot\"></span></div><div class=\"cell\">"
            "<div class=\"message assistant\"><div class=\"message-header\">assistant</div>"
            "<div class=\"message-body\">No conversation yet</div></div>"
            "</div></div>"
        )
    parts = []
    for group in groups:
        left = []
        right = []
        for c in group.get("commits", []):
            msg = escape_html(c.get("message", ""))
            author = escape_html(c.get("author", ""))
            date = escape_html(c.get("date", ""))
            h = escape_html(c.get("hash", ""))
            left.append(
                f"<div class=\"commit\"><div class=\"commit-title\">{msg}</div>"
                f"<div class=\"commit-meta\">{author} · {date} · <span class=\"hash\">{h[:7]}</span></div></div>"
            )
        for m in group.get("messages", []):
            role = m.get("role", "assistant")
            role_cls = "user" if role == "user" else "assistant"
            body = escape_html(m.get("text", ""))
            ts = escape_html(m.get("ts", ""))
            agent = escape_html(m.get("agent", "agent"))
            right.append(
                f"<div class=\"message {role_cls}\"><div class=\"message-header\">{role} · {agent} · {ts}</div>"
                f"<div class=\"message-body\">{body}</div></div>"
            )
        left_html = "".join(left) if left else ""
        right_html = "".join(right) if right else ""
        dot_class = "dot"
        if left_html and not right_html:
            dot_class = "dot git"
        elif right_html and not left_html:
            dot_class = "dot user"
        dot = f"<span class=\"{dot_class}\"></span>" if left_html or right_html else ""
        parts.append(
            f"<div class=\"row\"><div class=\"cell\">{left_html}</div>"
            f"<div class=\"cell axis\">{dot}</div><div class=\"cell\">{right_html}</div></div>"
        )
    return "".join(parts)


def escape_html(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _parse_ts(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            dt = dt.replace(tzinfo=local_tz)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _merge_timeline(commits, messages):
    groups = {}
    for c in commits:
        ts = c.get("date", "")
        key = ts
        group = groups.setdefault(key, {"ts": ts, "commits": [], "messages": []})
        group["commits"].append(c)
    for m in messages:
        ts = m.get("ts", "")
        key = ts
        group = groups.setdefault(key, {"ts": ts, "commits": [], "messages": []})
        group["messages"].append(m)
    items = list(groups.values())
    items.sort(key=lambda g: _parse_ts(g.get("ts")) or datetime.min, reverse=True)
    return items


def git_available():
    try:
        subprocess.run(["git", "rev-parse", "--git-dir"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def get_git_head():
    try:
        out = subprocess.check_output(["git", "rev-parse", "--verify", "HEAD"]).decode("utf-8").strip()
        return out
    except Exception:
        return None


def get_git_commits(last_commit):
    if not git_available():
        return []
    head = get_git_head()
    if not head:
        return []
    cmd = ["git", "log", "--reverse", "--pretty=format:%H%x1f%an%x1f%ad%x1f%s", "--date=iso-strict"]
    if last_commit:
        cmd.insert(2, f"{last_commit}..HEAD")
    try:
        out = subprocess.check_output(cmd).decode("utf-8")
    except Exception:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        commits.append({"hash": parts[0], "author": parts[1], "date": parts[2], "message": parts[3]})
    return commits


def _detect_agent_name(command):
    if not command:
        return "agent"
    base = os.path.basename(command[0])
    if base in ("python", "python3") and len(command) > 1:
        return os.path.basename(command[1])
    return base


def run_session(command):
    session_start = now_iso()
    agent = _detect_agent_name(command)
    messages = []
    editor = SimpleLineRecorder()
    builder = OutputBuilder()

    pid, fd = pty.fork()
    if pid == 0:
        if "TERM" not in os.environ:
            os.environ["TERM"] = "xterm-256color"
        os.execvp(command[0], command)

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    old_tty = termios.tcgetattr(stdin_fd)

    def set_winsize():
        try:
            rows, cols, xp, yp = struct.unpack(
                "HHHH", fcntl.ioctl(stdout_fd, termios.TIOCGWINSZ, b"\x00" * 8)
            )
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, xp, yp))
        except Exception:
            pass

    set_winsize()
    old_handler = signal.signal(signal.SIGWINCH, lambda *_: set_winsize())
    try:
        tty.setraw(stdin_fd)
        while True:
            rlist, _, _ = select.select([stdin_fd, fd], [], [])
            if stdin_fd in rlist:
                data = os.read(stdin_fd, 1024)
                if not data:
                    break
                os.write(fd, data)
                lines = editor.feed(data)
                for line in lines:
                    clean = sanitize_text(line)
                    if clean:
                        messages.append({"role": "user", "text": clean, "ts": now_iso()})
            if fd in rlist:
                data = os.read(fd, 1024)
                if not data:
                    break
                os.write(stdout_fd, data)
                text = strip_ansi(data.decode("utf-8", errors="ignore"))
                builder.feed_text(text)
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty)
        signal.signal(signal.SIGWINCH, old_handler)

    builder.flush_tail()

    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass

    return session_start, now_iso(), agent, messages


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("usage: codey <command> [args...]")
        sys.exit(1)

    command = sys.argv[1:]
    session_start, session_end, agent, messages = run_session(command)

    html_path = Path(os.getcwd()) / "codey.html"
    state = load_state(html_path)
    for session in state.get("conversations", []):
        for m in session.get("messages", []):
            if m.get("role") == "user":
                m["text"] = sanitize_text(m.get("text", ""))

    commits = get_git_commits(state.get("last_commit"))
    head = get_git_head()
    if head:
        state["last_commit"] = head
    if commits:
        known = {c.get("hash") for c in state.get("timeline", [])}
        for c in commits:
            if c.get("hash") not in known:
                state.setdefault("timeline", []).append(c)

    if messages:
        state.setdefault("conversations", []).append({
            "session_start": session_start,
            "session_end": session_end,
            "agent": agent,
            "messages": messages,
        })

    flat_messages = []
    for session in state.get("conversations", []):
        agent = session.get("agent", "agent")
        for m in session.get("messages", []):
            if m.get("role") == "user":
                m["agent"] = agent
            flat_messages.append(m)
    render_state = dict(state)
    render_state["conversations"] = flat_messages
    project = os.path.basename(os.getcwd())
    title = f"codey - {project}" if project else "codey"
    state_json = json.dumps(state, ensure_ascii=True)
    html_path.write_text(render_html(render_state, state_json, title), encoding="utf-8")


if __name__ == "__main__":
    main()
