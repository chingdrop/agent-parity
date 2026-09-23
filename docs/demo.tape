# Terminal recording for the README. Render from the repo root:
#
#   vhs docs/demo.tape
#
# Needs vhs (https://github.com/charmbracelet/vhs) plus its ttyd and ffmpeg
# dependencies, and `uv sync` already run. The output is the real command's
# real output against sample_data/; nothing here is scripted text.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1300
Set Height 420
Set Padding 24
Set TypingSpeed 55ms

# Start from a clean state with a short prompt (not recorded).
Hide
Type "export PS1='$ ' && cd $(git rev-parse --show-toplevel) && rm -rf output && clear"
Enter
Show

Type "uv run agent-parity run --csv"
Sleep 600ms
Enter

# The run takes a moment; then hold on the summary line long enough to read.
Sleep 3s
Sleep 6s
