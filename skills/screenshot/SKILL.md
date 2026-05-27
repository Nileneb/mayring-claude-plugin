---
name: mayring-coder:screenshot
description: Capture the user's screen so Claude can actually see it, then Read the PNG. Use when the user references something visible on their monitor, when iterating on UI/frontend work, or whenever there's a mismatch between what the user sees and what Claude assumes is on screen. Bridges the perception gap between the user's display and Claude.
---

# /screenshot — Capture the user's screen

Bridges the gap between what the user sees on their monitor and what Claude
perceives. Captures the X11 screen to a PNG that Claude then Reads.

## Steps (do exactly, in order)

1. **Capture.** Run this self-contained command (no plugin-path or env
   dependency — works wherever the session runs):

   ```bash
   python3 - <<'PY'
   import os, sys, subprocess
   from pathlib import Path
   out = Path(os.environ.get("CLAUDE_SCREENSHOT_OUT", "/tmp/claude_screenshot.png"))
   disp = os.environ.get("DISPLAY") or ":1"
   max_w = int(os.environ.get("CLAUDE_SCREENSHOT_MAX_W", "3840"))  # downscale ceiling: keeps the read token-cheap
   os.environ["DISPLAY"] = disp
   size = None
   try:
       from PIL import ImageGrab
       img = ImageGrab.grab(xdisplay=disp); img.save(out); size = img.size
   except Exception as e:
       print(f"# PIL grab failed ({e}); trying CLI tools", file=sys.stderr)
       for cmd in (["gnome-screenshot", "-f", str(out)], ["scrot", "-o", str(out)],
                   ["import", "-window", "root", str(out)]):
           if subprocess.run(["bash", "-lc", f"command -v {cmd[0]}"],
                             capture_output=True).returncode == 0:
               subprocess.run(cmd, check=True, env={**os.environ, "DISPLAY": disp}); break
       else:
           sys.exit("no screenshot backend (need PIL / gnome-screenshot / scrot / import)")
   try:  # downscale wide/multi-monitor grabs so the Read stays cheap but legible
       from PIL import Image
       im = Image.open(out)
       if im.width > max_w:
           im.resize((max_w, int(im.height * max_w / im.width))).save(out); size = im.size
   except Exception:
       pass
   print(out)                                  # stdout = path to Read
   print(f"# captured {size} -> {out}", file=sys.stderr)
   PY
   ```

2. **Read** the path printed on stdout with the Read tool — that is how you
   actually see the screen.

3. **Describe only what's relevant** to the current task. The screen may show
   private content (mail, messages, other windows); do not dump everything you
   see — surface just what matters for the work at hand.

## Notes

- X11 session expected (`DISPLAY`, default `:1`). PIL.ImageGrab is the primary
  path; gnome-screenshot / scrot / import are fallbacks.
- Multi-monitor: captures the **full virtual screen** (all monitors side by
  side). To inspect one monitor, ask the user which, then crop after Reading,
  or re-run with a region tool.
- Tunables: `CLAUDE_SCREENSHOT_OUT` (output path), `CLAUDE_SCREENSHOT_MAX_W`
  (downscale ceiling; raise it if you need to read fine UI text).
