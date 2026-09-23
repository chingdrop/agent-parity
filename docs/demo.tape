# Terminal recording for the README. Render from the repo root:
#
#   vhs docs/demo.tape
#
# Needs vhs (https://github.com/charmbracelet/vhs) plus its ttyd and ffmpeg
# dependencies, and `uv sync` already run. The output is the real command's
# real output against sample_data/; nothing here is scripted text.
#
# vhs 0.12 with ffmpeg 9 exits 0 but silently writes no GIF (its own ffmpeg
# call fails). Workaround: point Output at a directory ("Output frames/") to
# get PNG frames, then assemble them yourself:
#
#   ffmpeg -framerate 50 -i frames/frame-text-%05d.png \
#     -framerate 50 -i frames/frame-cursor-%05d.png \
#     -filter_complex "[0][1]overlay,pad=1300:420:(ow-iw)/2:(oh-ih)/2:color=0x171717,fps=20,split[a][b];[a]palettegen[p];[b][p]paletteuse" \
#     docs/demo.gif

Output docs/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1300
Set Height 420
Set Padding 24
Set TypingSpeed 55ms

# Start from a clean state with a short prompt (not recorded).
Hide
Type "export PS1='$ ' && cd $(git rev-parse --show-toplevel) && rm -rf output agent_parity.db && clear"
Enter
Show

Type "uv run agent-parity run --csv"
Sleep 600ms
Enter

# The run takes a moment; then hold on the summary line long enough to read.
Sleep 3s
Sleep 6s
