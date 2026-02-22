"""
☠  Necromancer C3 Panel  ☠
In-car maintenance panel for the C3 compatibility patch.

Installed to: selfdrive/ui/layouts/settings/necromancer.py
"""
import subprocess
import threading
from pathlib import Path

import pyray as rl

from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets import Widget, DialogResult
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog
from openpilot.system.ui.widgets.list_view import button_item, text_item
from openpilot.system.ui.widgets.option_dialog import MultiOptionDialog
from openpilot.system.ui.widgets.scroller import Scroller

OP_DIR   = "/data/openpilot"
RUNNER   = f"{OP_DIR}/scripts/necromancer/necro_runner.py"
BACKUP   = Path("/data/c3_backup")
NO_AGNOS = Path("/data/no_agnos_update")
PANDA_FW = BACKUP / "panda/panda.bin.signed"


def _git(*args: str, timeout: int = 15) -> str:
  try:
    r = subprocess.run(
      ["git", "-C", OP_DIR, *args],
      capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout.strip()
  except Exception:
    return ""


def _launch_runner(op: str, param: str = "") -> None:
  """Kill the comma UI tmux session and launch the necromancer runner full-screen."""
  subprocess.run(["tmux", "kill-session", "-t", "comma"], capture_output=True)
  cmd = ["python3", RUNNER, op]
  if param:
    cmd.append(param)
  with open("/tmp/necro_runner.log", "w") as log_file:
    subprocess.Popen(cmd, start_new_session=True, stdout=log_file, stderr=log_file)


class NecromancerLayout(Widget):
  def __init__(self):
    super().__init__()

    self._current_commit = "reading…"
    self._current_branch = "reading…"
    self._patch_status   = "checking…"
    self._refresh_count  = 0

    self._verify_running        = False
    self._pending_verify_modal  = None
    self._pending_commit_action = None
    self._pending_branch_action = None
    self._commit_dialog: MultiOptionDialog | None = None
    self._branch_dialog: MultiOptionDialog | None = None

    self._commit_item = text_item(lambda: tr("Current Commit"), lambda: self._current_commit)
    self._branch_item = text_item(lambda: tr("Current Branch"), lambda: self._current_branch)
    self._patch_item  = text_item(lambda: tr("Patch Status"),   lambda: self._patch_status)

    self._verify_btn = button_item(
      lambda: tr("Verify Patch Wards"), lambda: tr("CHECK"),
      description="Inspect all 5 patch wards — firmware, service, and AGNOS block.",
      callback=self._on_verify,
    )
    self._update_btn = button_item(
      lambda: tr("Update to Latest"), lambda: tr("UPDATE"),
      description="Pull the latest commit from remote, reapply the curse, and reboot.",
      callback=self._on_update,
    )
    self._commit_btn = button_item(
      lambda: tr("Specific Commit…"), lambda: tr("SELECT"),
      description="Restore to a specific newer commit from the remote branch.",
      callback=self._on_specific_commit,
    )
    self._branch_btn = button_item(
      lambda: tr("Switch Branch…"), lambda: tr("SELECT"),
      description="Switch to a different remote branch, reapply the curse, and reboot.",
      callback=self._on_switch_branch,
    )

    self._scroller = Scroller([
      self._commit_item,
      self._branch_item,
      self._patch_item,
      self._verify_btn,
      self._update_btn,
      self._commit_btn,
      self._branch_btn,
    ], line_separator=True, spacing=0)

  # ── Lifecycle ──────────────────────────────────────────────────────────────

  def show_event(self):
    self._scroller.show_event()
    self._refresh_count = 300  # force immediate refresh on show

  def _render(self, rect: rl.Rectangle):
    # Purple accent bar across the top of the panel
    PURPLE = rl.Color(192, 132, 252, 255)
    rl.draw_rectangle_rec(rl.Rectangle(rect.x, rect.y, rect.width, 6), PURPLE)
    self._scroller.render(rl.Rectangle(rect.x, rect.y + 6, rect.width, rect.height - 6))

  def _update_state(self):
    # Refresh commit/branch/patch info every ~300 frames (~5 s at 60 fps)
    self._refresh_count += 1
    if self._refresh_count >= 300:
      self._refresh_count = 0
      threading.Thread(target=self._do_refresh, daemon=True).start()

    # Verify button state
    self._verify_btn.action_item.set_enabled(not self._verify_running)

    # Fire pending modals/actions queued from background threads
    if self._pending_verify_modal:
      content, self._pending_verify_modal = self._pending_verify_modal, None
      gui_app.set_modal_overlay(ConfirmDialog(content, "OK", cancel_text="", rich=True))

    if self._pending_commit_action:
      fn, self._pending_commit_action = self._pending_commit_action, None
      fn()

    if self._pending_branch_action:
      fn, self._pending_branch_action = self._pending_branch_action, None
      fn()

  # ── Data refresh ───────────────────────────────────────────────────────────

  def _do_refresh(self):
    self._current_commit = _git("log", "--format=%h %s", "-1") or "unknown"
    self._current_branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    self._patch_status   = "☠  Installed" if BACKUP.is_dir() else "✗  Not found"

  # ── Verify ─────────────────────────────────────────────────────────────────

  def _on_verify(self):
    if self._verify_running:
      return
    self._verify_running = True
    self._verify_btn.action_item.set_value("checking…")
    threading.Thread(target=self._do_verify, daemon=True).start()

  def _do_verify(self):
    try:
      enabled = subprocess.run(
        ["systemctl", "is-enabled", "c3_persist"],
        capture_output=True, text=True,
      ).stdout.strip() == "enabled"
      active = subprocess.run(
        ["systemctl", "is-active", "c3_persist"],
        capture_output=True, text=True,
      ).stdout.strip() == "active"

      wards = [
        ("c3_persist.service enabled", enabled),
        ("c3_persist.service active",  active),
        ("/data/c3_backup exists",     BACKUP.is_dir()),
        ("panda firmware backup",      PANDA_FW.exists()),
        ("AGNOS update blocked",       NO_AGNOS.exists()),
      ]
      ok_count = sum(1 for _, ok in wards if ok)
      lines = [f"{'✓' if ok else '✗'}  {label}" for label, ok in wards]

      if ok_count == 5:
        title = "☠  All 5 wards intact — the C3 is fully bound."
      else:
        title = f"⚠  {ok_count}/5 wards in place — some are missing."

      content = f"<h1>{title}</h1><br><p>" + "<br>".join(lines) + "</p>"
      self._pending_verify_modal = content
      self._verify_btn.action_item.set_value("CHECK")
    except Exception as e:
      self._pending_verify_modal = f"<h1>Error</h1><br><p>{e}</p>"
      self._verify_btn.action_item.set_value("CHECK")
    finally:
      self._verify_running = False

  # ── Update to latest ───────────────────────────────────────────────────────

  def _on_update(self):
    def confirm(result: int):
      if result == DialogResult.CONFIRM:
        _launch_runner("update")

    gui_app.set_modal_overlay(
      ConfirmDialog(
        "<h1>Update to Latest?</h1><br>"
        "<p>This will pull the latest commit from remote, reapply the C3 curse, "
        "and reboot the device when complete.</p>",
        "Update ☠", rich=True,
      ),
      callback=confirm,
    )

  # ── Specific commit ────────────────────────────────────────────────────────

  def _on_specific_commit(self):
    self._commit_btn.action_item.set_enabled(False)
    self._commit_btn.action_item.set_value("fetching…")
    threading.Thread(target=self._fetch_commits, daemon=True).start()

  def _fetch_commits(self):
    try:
      _git("fetch", "origin", "--quiet", timeout=20)
      remote = (
        _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", timeout=5)
        or "origin/nap-alpha"
      )
      newer = [
        l for l in _git("log", f"HEAD..{remote}", "--format=%h %s", timeout=10).splitlines()
        if l.strip()
      ]
    except Exception:
      newer = []
    self._pending_commit_action = lambda: self._show_commit_dialog(newer)

  def _show_commit_dialog(self, newer: list[str]):
    self._commit_btn.action_item.set_enabled(True)
    self._commit_btn.action_item.set_value("SELECT")
    if not newer:
      gui_app.set_modal_overlay(
        ConfirmDialog("Already on the latest commit — nothing to restore.", "OK", cancel_text=""),
      )
      return
    self._commit_dialog = MultiOptionDialog("☠  Restore to Commit", newer, newer[0])

    def on_select(result: int):
      if result == DialogResult.CONFIRM and self._commit_dialog and self._commit_dialog.selection:
        chosen_hash = self._commit_dialog.selection.split()[0]
        _launch_runner("checkout", chosen_hash)
      self._commit_dialog = None

    gui_app.set_modal_overlay(self._commit_dialog, callback=on_select)

  # ── Switch branch ──────────────────────────────────────────────────────────

  def _on_switch_branch(self):
    self._branch_btn.action_item.set_enabled(False)
    self._branch_btn.action_item.set_value("fetching…")
    threading.Thread(target=self._fetch_branches, daemon=True).start()

  def _fetch_branches(self):
    try:
      _git("fetch", "origin", "--quiet", timeout=20)
      current = _git("rev-parse", "--abbrev-ref", "HEAD", timeout=5) or ""
      raw     = _git("branch", "-r", "--format=%(refname:short)", timeout=10)
      branches: list[str] = []
      for b in raw.splitlines():
        b = b.strip().replace("origin/", "")
        if b and "HEAD" not in b and b not in branches:
          branches.append(b)
      if current in branches:
        branches.remove(current)
        branches.insert(0, current)
    except Exception:
      branches = []
      current  = ""
    self._pending_branch_action = lambda: self._show_branch_dialog(branches, current)

  def _show_branch_dialog(self, branches: list[str], current: str):
    self._branch_btn.action_item.set_enabled(True)
    self._branch_btn.action_item.set_value("SELECT")
    if not branches:
      gui_app.set_modal_overlay(
        ConfirmDialog("Could not fetch branch list — check network connection.", "OK", cancel_text=""),
      )
      return
    self._branch_dialog = MultiOptionDialog("☠  Switch Branch", branches, current)

    def on_select(result: int):
      if result == DialogResult.CONFIRM and self._branch_dialog and self._branch_dialog.selection:
        chosen = self._branch_dialog.selection
        if chosen != current:
          _launch_runner("branch", chosen)
      self._branch_dialog = None

    gui_app.set_modal_overlay(self._branch_dialog, callback=on_select)
