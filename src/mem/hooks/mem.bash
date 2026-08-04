# mem shell hook for bash — install with: eval "$(mem init bash)"
#
# How it works:
# - A DEBUG trap fires before the first simple command of each line and stamps
#   the start time. That is all it does — it is a clock, not a recorder.
# - PROMPT_COMMAND runs after the line completes, reads the command *line* out
#   of bash's own history, and calls mem _capture in the background.
# - `& disown` backgrounds the capture and suppresses job notifications.
#
# Why `history 1` and not $BASH_COMMAND:
#   $BASH_COMMAND is the simple command bash is about to run, so for
#   `a | b | c` it yields `a`, and for `false || echo recovered` it yields
#   `false`. That is not a truncation the user can see in the store — it is a
#   different command that means something else. `history 1` is the line the
#   user actually typed.
#
# Why $BASH_COMMAND was chosen originally, and how that objection is answered:
#   `history 1` returns the last *persisted* entry, so with
#   HISTCONTROL=ignorespace, HISTIGNORE, or history disabled it silently
#   returns a stale earlier command. The fix is to read the history *number*
#   alongside the text and only capture when it advanced. If bash declined to
#   record the line the number does not move, and mem records nothing — which
#   is exactly right, because every one of those mechanisms is the user
#   telling their shell not to remember this.
#
#   The contract that falls out of it is worth stating plainly: mem remembers
#   exactly what your shell remembers, and nothing else. The one cost is
#   HISTCONTROL=ignoredups/erasedups, where a command repeated back-to-back
#   advances the number once and mem sees a single occurrence. Frequency
#   counts degrade slightly for those users; no command is lost.
#
# Why the history number is also read at install time:
#   the DEBUG trap is armed by this very file, so without a starting reference
#   the first prompt would capture the `source`/`eval` line that installed it.

# A millisecond clock with no subprocess where bash provides one. $SECONDS is
# an integer: it recorded every sub-second command as 0ms, which is most of
# them. EPOCHREALTIME arrived in bash 5.0; macOS still ships bash 3.2 as
# /bin/bash and has no process-free sub-second clock, so that path falls back
# to `date` and second resolution rather than reporting a wrong number. Either
# way the result is milliseconds, so mem's stored unit never depends on which
# bash you happen to run.
if [[ -n "${EPOCHREALTIME:-}" ]]; then
  _mem_clock() {
    local t=${EPOCHREALTIME/[.,]/}
    _mem_ms=$(( 10#$t / 1000 ))
  }
else
  _mem_clock() {
    _mem_ms=$(( $(date +%s) * 1000 ))
  }
fi

_mem_read_history() {
  # bash prints `history 1` as a right-aligned number, exactly two separator
  # spaces, then the command verbatim. Slicing past the number plus those two
  # spaces preserves a leading space in the command itself, which the check in
  # _mem_prompt_cmd depends on.
  #
  # HISTTIMEFORMAT is cleared inside the substitution — a subshell, so the
  # user's own setting is untouched — otherwise their timestamp format ends up
  # glued to the front of the command text.
  local entry
  entry=$(HISTTIMEFORMAT= history 1)
  entry=${entry#"${entry%%[![:space:]]*}"}
  _mem_hist_num=${entry%%[![:digit:]]*}
  if [[ -n "$_mem_hist_num" ]]; then
    _mem_hist_cmd=${entry:$(( ${#_mem_hist_num} + 2 ))}
  else
    # History is off entirely. There is nothing to read and nothing to guess.
    _mem_hist_cmd=""
  fi
}

_mem_debug_trap() {
  # DEBUG traps are not inherited by shell functions unless `set -T`, so this
  # fires only for top-level commands. The guard keeps the stamp on the first
  # simple command of the line: for `a | b | c` we want when `a` started, not
  # when `c` did.
  if [[ -z "$_mem_capturing" ]]; then
    _mem_capturing=1
    _mem_clock
    _mem_start=$_mem_ms
  fi
}

_mem_prompt_cmd() {
  # Must be the very first statement: anything before it clobbers $?.
  local exit_code=$?
  _mem_capturing=""
  local previous=$_mem_hist_num
  _mem_read_history
  if [[ -n "$_mem_hist_num" && "$_mem_hist_num" != "$previous" ]]; then
    # A leading space is the universal "do not record this" gesture. The
    # history-number check already covers HISTCONTROL=ignorespace; this covers
    # the user who never set the option but expects the gesture to work
    # anyway. mem's store is searchable and fed to an AI layer, so it errs
    # toward not remembering.
    if [[ "$_mem_hist_cmd" != [[:space:]]* ]]; then
      _mem_clock
      mem _capture "$_mem_hist_cmd" "$PWD" "$exit_code" \
        "$(( _mem_ms - _mem_start ))" 2>/dev/null &
      disown 2>/dev/null
    fi
  fi
}

_mem_capturing=""
_mem_start=0
_mem_ms=0
_mem_hist_num=""
_mem_hist_cmd=""
_mem_read_history

trap '_mem_debug_trap' DEBUG

# bash 5.1 made PROMPT_COMMAND an array, and prompt frameworks set it as one.
# Assigning a string to an array variable replaces element 0 and silently drops
# the rest, so a scalar assignment here would delete the user's prompt. mem
# goes first either way: it must read $? before anything else can overwrite it.
if [[ -n "${BASH_VERSINFO[0]:-}" ]] &&
   ((BASH_VERSINFO[0] > 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] >= 1))); then
  PROMPT_COMMAND=(_mem_prompt_cmd "${PROMPT_COMMAND[@]}")
else
  PROMPT_COMMAND="_mem_prompt_cmd${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
fi

# --- Ctrl+R ------------------------------------------------------------------
#
# The finder prints the chosen command to stdout and draws its interface on
# /dev/tty, so the substitution below captures the choice and nothing else.
# The command is placed on the command line, never executed: you get to read
# it, edit it, and decide. A history search that runs things behind your back
# is how people delete the wrong branch.
#
# Set MEM_NO_KEYBINDING=1 before loading this hook to keep bash's own Ctrl+R.

_mem_search_widget() {
  local selected
  selected=$(mem tui -- "$READLINE_LINE" </dev/tty) || return 0
  if [[ -n "$selected" ]]; then
    READLINE_LINE="$selected"
    READLINE_POINT=${#READLINE_LINE}
  fi
}

if [[ -z "${MEM_NO_KEYBINDING:-}" ]]; then
  # `bind -x` needs an interactive shell with readline; in anything else
  # (a script sourcing this file, a shell with `set +o emacs`) it fails
  # harmlessly and capture still works.
  bind -x '"\C-r": _mem_search_widget' 2>/dev/null
fi
