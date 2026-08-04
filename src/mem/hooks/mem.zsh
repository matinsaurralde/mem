# mem shell hook for zsh — install with: eval "$(mem init zsh)"
#
# How it works:
# - _mem_preexec runs BEFORE each command, capturing the command text and start time
# - _mem_precmd runs AFTER each command, computing exit code and duration
# - mem _capture runs in the background (&!) so it NEVER blocks the prompt
#
# Why &! instead of &: In zsh, &! (or equivalently &|) disowns the process
# immediately, preventing "job completed" messages from appearing in the prompt.
#
# Why zsh/datetime instead of $SECONDS: $SECONDS is an integer, so every command
# faster than a second was recorded as 0ms and everything slower was rounded to
# a whole second. Two thirds of a real history ended up with duration_ms == 0,
# which makes "what is slow around here?" unanswerable. EPOCHREALTIME is a float
# with microsecond resolution and costs no subprocess. The module ships with
# every zsh build, but the load is guarded anyway: if it were missing we record
# 0 rather than a wrong number.

zmodload -F zsh/datetime p:EPOCHREALTIME 2>/dev/null

_mem_preexec() {
  # $1 is the line exactly as the user typed it, leading whitespace included.
  # $2 and $3 are normalised forms — using them would silently erase the
  # leading-space gesture below, which is the whole reason for reading $1.
  if [[ "$1" == [[:space:]]* ]]; then
    # A leading space is the universal "do not record this" gesture
    # (HIST_IGNORE_SPACE here, HISTCONTROL=ignorespace in bash). Someone who
    # types it has made an explicit decision about a secret, and a
    # privacy-first tool does not get to overrule it. mem honours it
    # unconditionally, whether or not the shell option is set, because mem's
    # store is searchable and fed to an AI layer: it is strictly more sensitive
    # than the shell's own history file.
    _mem_cmd=""
    return
  fi
  _mem_cmd="$1"
  _mem_start=${EPOCHREALTIME:-0}
}

_mem_precmd() {
  # Must be the very first statement: anything before it clobbers $?.
  local exit_code=$?
  if [[ -n "$_mem_cmd" ]]; then
    integer duration=0
    if [[ -n "$EPOCHREALTIME" ]]; then
      (( duration = (EPOCHREALTIME - _mem_start) * 1000 ))
    fi
    mem _capture "$_mem_cmd" "$PWD" "$exit_code" "$duration" 2>/dev/null &!
    _mem_cmd=""
  fi
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec _mem_preexec
add-zsh-hook precmd _mem_precmd

# --- Ctrl+R ------------------------------------------------------------------
#
# The finder prints the chosen command to stdout and draws its interface on
# /dev/tty, so the substitution below captures the choice and nothing else.
# The command is placed on the command line, never executed: you get to read
# it, edit it, and decide. A history search that runs things behind your back
# is how people delete the wrong branch.
#
# Set MEM_NO_KEYBINDING=1 before loading this hook to keep zsh's own Ctrl+R.

_mem_search_widget() {
  local selected
  selected=$(mem tui -- "$BUFFER" </dev/tty) || { zle reset-prompt; return 0; }
  if [[ -n "$selected" ]]; then
    BUFFER="$selected"
    CURSOR=${#BUFFER}
  fi
  zle reset-prompt
}

if [[ -z "$MEM_NO_KEYBINDING" ]]; then
  zle -N _mem_search_widget
  bindkey '^R' _mem_search_widget
fi
