#!/usr/bin/env python3
"""
☠  Necromancer Operation Runner  ☠

Full-screen git/maintenance operation runner with live output.
Launched as a detached process that kills the comma UI and takes over the screen.

Usage:
    python3 necro_runner.py <operation> [param]

Operations:
    update              — git pull --rebase + reapply C3 curse
    checkout <hash>     — git checkout <hash> + reapply C3 curse
    branch <name>       — git checkout <name> + git pull + reapply C3 curse
    verify              — check patch wards (no git, no reboot)
"""
import os
import sys
import subprocess
import threading
import queue

import pyray as rl

sys.path.insert(0, "/data/openpilot")

from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.scroll_panel import GuiScrollPanel
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets.button import Button, ButtonStyle
from openpilot.system.hardware import HARDWARE, PC

OP_DIR  = "/data/openpilot"
PERSIST = "/data/c3_persist.py"

MARGIN          = 50
TITLE_FONT_SIZE = 70
OUTPUT_FONT_SIZE = 35
LINE_HEIGHT     = 45
BUTTON_WIDTH    = 350
BUTTON_HEIGHT   = 110

BG_COLOR   = rl.Color(20, 20, 20, 255)
ACC_COLOR  = rl.Color(192, 132, 252, 255)   # lavender purple
OK_COLOR   = rl.Color(134, 239, 172, 255)   # green
ERR_COLOR  = rl.Color(248, 113, 113, 255)   # red
WARN_COLOR = rl.Color(251, 146, 60, 255)    # orange
DIM_COLOR  = rl.Color(180, 180, 180, 255)

OP_TITLES = {
  "update":   "☠  Update to Latest",
  "checkout": "☠  Restore to Commit",
  "branch":   "☠  Switch Branch",
  "verify":   "☠  Verify Patch Wards",
}

NEEDS_REBOOT = {"update", "checkout", "branch"}


class State:
  RUNNING   = 0
  COMPLETED = 1
  ERROR     = 2


class NecroRunnerApp:
  def __init__(self, op: str, param: str = ""):
    self._op    = op
    self._param = param
    self._title = OP_TITLES.get(op, f"☠  {op}")

    self._state         = State.RUNNING
    self._output_q:     queue.Queue[str] = queue.Queue()
    self._output_lines: list[str]        = []
    self._scroll_panel  = GuiScrollPanel()

    self._font       = None
    self._title_font = None
    self._exit_btn   = None

  def _init_ui(self):
    self._font       = gui_app.font(FontWeight.NORMAL)
    self._title_font = gui_app.font(FontWeight.BOLD)

    if self._op == "verify":
      label = "Close"
      style = ButtonStyle.TRANSPARENT_WHITE_BORDER
    else:
      label = "Reboot"
      style = ButtonStyle.DANGER

    self._exit_btn = Button(label, click_callback=self._on_exit,
                            button_style=style, font_size=45)

  def start(self):
    threading.Thread(target=self._run, daemon=True).start()

  # ── Output helper ──────────────────────────────────────────────────────────

  def _emit(self, line: str):
    self._output_q.put(line)

  # ── Operation runner (background thread) ───────────────────────────────────

  def _run(self):
    try:
      if self._op == "verify":
        self._run_verify()
      elif self._op == "update":
        self._git_op("Pulling latest commits",
                     ["pull", "--rebase", "--recurse-submodules", "--progress"])
        if self._state != State.ERROR:
          self._run_persist()
      elif self._op == "checkout":
        self._reset_submodules()
        self._git_op(f"Checking out {self._param}", ["checkout", self._param])
        if self._state != State.ERROR:
          self._submodule_update()
        if self._state != State.ERROR:
          self._run_persist()
      elif self._op == "branch":
        self._reset_submodules()
        self._git_op(f"Switching to branch {self._param}", ["checkout", self._param])
        if self._state != State.ERROR:
          self._git_op("Pulling branch", ["pull", "--rebase", "--recurse-submodules", "--progress"])
        if self._state != State.ERROR:
          self._run_persist()
      else:
        self._emit(f"✗  Unknown operation: {self._op}")
        self._state = State.ERROR
    except Exception as e:
      self._emit(f"\n✗  Unexpected error: {e}")
      self._state = State.ERROR

  def _reset_submodules(self):
    self._emit("\n[*]  Resetting submodule state to avoid conflicts…")
    subprocess.run(
      ["git", "-C", OP_DIR, "submodule", "foreach", "--recursive", "git", "reset", "--hard"],
      capture_output=True,
    )
    subprocess.run(
      ["git", "-C", OP_DIR, "submodule", "foreach", "--recursive", "git", "clean", "-fd"],
      capture_output=True,
    )

  def _git_op(self, label: str, git_args: list[str]):
    self._emit(f"\n[git]  {label}…\n")
    proc = subprocess.Popen(
      ["git", "-C", OP_DIR] + git_args,
      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
      text=True, bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
      self._emit("  " + line.rstrip())
    proc.wait()
    if proc.returncode != 0:
      self._emit(f"\n✗  git {git_args[0]} failed (exit {proc.returncode})")
      self._state = State.ERROR

  def _submodule_update(self):
    self._emit("\n[git]  Updating submodules…\n")
    proc = subprocess.Popen(
      ["git", "-C", OP_DIR, "submodule", "update",
       "--init", "--recursive", "--force", "--progress"],
      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
      text=True, bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
      self._emit("  " + line.rstrip())
    proc.wait()
    if proc.returncode != 0:
      self._emit(f"\n✗  submodule update failed (exit {proc.returncode})")
      self._state = State.ERROR

  def _run_persist(self):
    self._emit("\n[curse]  Reapplying C3 compatibility patch…\n")
    proc = subprocess.Popen(
      ["python3", PERSIST],
      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
      text=True, bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
      self._emit("  " + line.rstrip())
    proc.wait()
    if proc.returncode == 0:
      self._emit("\n☠" * 20)
      self._emit("\n  ✓  Curse reapplied — press Reboot to activate changes.")
      self._emit("☠" * 20)
      self._state = State.COMPLETED
    else:
      self._emit(f"\n✗  c3_persist.py failed (exit {proc.returncode})")
      self._state = State.ERROR

  def _run_verify(self):
    self._emit("\n[wards]  Consulting the spirits…\n")
    wards = [
      ("c3_persist.service enabled", subprocess.run(
        ["systemctl", "is-enabled", "c3_persist"], capture_output=True, text=True,
      ).stdout.strip() == "enabled"),
      ("c3_persist.service active",  subprocess.run(
        ["systemctl", "is-active", "c3_persist"], capture_output=True, text=True,
      ).stdout.strip() == "active"),
      ("/data/c3_backup exists",     os.path.isdir("/data/c3_backup")),
      ("panda firmware backup",      os.path.isfile("/data/c3_backup/panda/panda.bin.signed")),
      ("AGNOS update blocked",       os.path.isfile("/data/no_agnos_update")),
    ]
    for label, ok in wards:
      self._emit(f"  {'✓' if ok else '✗'}  {label}")
    ok_count = sum(1 for _, ok in wards if ok)
    self._emit("")
    if ok_count == 5:
      self._emit("  ☠  All 5 wards intact — the C3 is fully bound.")
      self._state = State.COMPLETED
    else:
      self._emit(f"  ⚠  {ok_count}/5 wards in place — some are missing.")
      self._emit("     Re-running the installation may help.")
      self._state = State.ERROR

  # ── Exit ───────────────────────────────────────────────────────────────────

  def _on_exit(self):
    gui_app.request_close()
    if self._op == "verify":
      # Just restart the comma service — no reboot needed
      subprocess.Popen(["sudo", "systemctl", "restart", "comma.service"],
                       start_new_session=True)
    else:
      if not PC:
        HARDWARE.reboot()

  # ── Render ─────────────────────────────────────────────────────────────────

  def _process_queue(self):
    while True:
      try:
        self._output_lines.append(self._output_q.get_nowait())
      except queue.Empty:
        break

  def render(self):
    rect = rl.Rectangle(0, 0, gui_app.width, gui_app.height)
    rl.draw_rectangle_rec(rect, BG_COLOR)
    self._process_queue()

    mx = rect.x + MARGIN
    w  = rect.width - MARGIN * 2
    y  = rect.y + MARGIN

    # Title
    title_sz = measure_text_cached(self._title_font, self._title, TITLE_FONT_SIZE)
    rl.draw_text_ex(self._title_font, self._title,
                    rl.Vector2(mx, y), TITLE_FONT_SIZE, 0, ACC_COLOR)
    y += title_sz.y + MARGIN // 2

    # Divider
    rl.draw_line_ex(rl.Vector2(mx, y), rl.Vector2(mx + w, y), 2,
                    rl.Color(80, 80, 80, 255))
    y += MARGIN

    # Status badge (top right)
    status_map = {
      State.RUNNING:   ("Running…",  WARN_COLOR),
      State.COMPLETED: ("Complete ✓", OK_COLOR),
      State.ERROR:     ("Error ✗",    ERR_COLOR),
    }
    s_text, s_col = status_map[self._state]
    s_sz = measure_text_cached(self._font, s_text, 45)
    rl.draw_text_ex(self._font, s_text,
                    rl.Vector2(rect.x + rect.width - MARGIN - s_sz.x,
                               rect.y + MARGIN + title_sz.y // 2 - 22),
                    45, 0, s_col)

    # Output area
    btn_area_h = BUTTON_HEIGHT + MARGIN * 2
    out_h      = rect.height - y - btn_area_h - rect.y
    out_rect   = rl.Rectangle(mx, y, w, out_h)
    content_h  = max(len(self._output_lines) * LINE_HEIGHT, out_h)
    content_r  = rl.Rectangle(0, 0, w, content_h)

    # Auto-scroll to bottom
    if len(self._output_lines) * LINE_HEIGHT > out_h:
      self._scroll_panel._offset_filter_y.x = -(len(self._output_lines) * LINE_HEIGHT - out_h)

    scroll = self._scroll_panel.update(out_rect, content_r)

    rl.begin_scissor_mode(int(out_rect.x), int(out_rect.y),
                          int(out_rect.width), int(out_rect.height))
    for i, line in enumerate(self._output_lines):
      ly = out_rect.y + scroll + i * LINE_HEIGHT
      if ly + LINE_HEIGHT < out_rect.y or ly > out_rect.y + out_rect.height:
        continue
      if "✓" in line or "intact" in line.lower() or "success" in line.lower():
        col = OK_COLOR
      elif "✗" in line or "failed" in line.lower() or "error" in line.lower():
        col = ERR_COLOR
      elif "⚠" in line or "warning" in line.lower():
        col = WARN_COLOR
      elif "☠" in line or line.strip().startswith("["):
        col = ACC_COLOR
      else:
        col = DIM_COLOR
      rl.draw_text_ex(self._font, line,
                      rl.Vector2(out_rect.x, ly), OUTPUT_FONT_SIZE, 0, col)
    rl.end_scissor_mode()

    # Exit/Reboot button — enabled only when not running
    btn_enabled = self._state != State.RUNNING
    btn_rect = rl.Rectangle(
      rect.x + rect.width - MARGIN - BUTTON_WIDTH,
      rect.y + rect.height - MARGIN - BUTTON_HEIGHT,
      BUTTON_WIDTH, BUTTON_HEIGHT,
    )
    self._exit_btn.set_enabled(btn_enabled)
    self._exit_btn.render(btn_rect)


def main():
  if len(sys.argv) < 2:
    print("Usage: necro_runner.py <operation> [param]")
    sys.exit(1)

  op    = sys.argv[1]
  param = sys.argv[2] if len(sys.argv) > 2 else ""

  # Kill the main comma UI to take over the screen
  subprocess.run(["tmux", "kill-session", "-t", "comma"], capture_output=True)

  gui_app.init_window("Necromancer")

  app = NecroRunnerApp(op, param)
  app._init_ui()
  app.start()

  for _ in gui_app.render():
    app.render()


if __name__ == "__main__":
  main()
