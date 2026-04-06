Generate a terminal statusline for a Claude Code session as a single shell prompt string for zsh.

The statusline must show these segments in order:
1. Model name — hardcoded as "sonnet-4-6" in green
2. Current directory — $PWD shortened, in blue  
3. Context usage — percentage of context window used, with a fill bar using block characters (░▒▓█), color shifts green→amber→red as it fills
4. Current operation — what Claude is doing right now (read/write/bash/search/thinking/idle), in matching color
5. Token drain — tokens consumed by last operation prefixed with +, color shifts blue→amber→red for heavy operations
6. Files written — count of files created/modified this session in purple
7. Git branch — current branch from $(git branch --show-current) in yellow

Rules:
- Single PROMPT= line for zsh, no multiline
- Use %F{color}...%f for zsh color codes only — no ANSI \033 escapes
- Use block chars ░▒▓█ for the context fill bar, exactly 8 chars wide
- No powerline glyphs — must work in any terminal font
- Context bar color: %F{green} under 50%, %F{yellow} 50-75%, %F{red} over 75%
- Operation colors: read=blue, write=green, bash=yellow, search=magenta, thinking=cyan, idle=white
- Drain colors: blue under 500 tokens, yellow 500-2000, red over 2000
- Must fit on one line at 120 char terminal width
- End with %F{yellow}❯%f as the prompt character

Also output the precmd() hook needed to update context percentage dynamically, reading from $CLAUDE_CTX_PCT env var with fallback to "??" if unset.

Output only the zsh code block, no explanation.
```

**Then in any Claude Code session just type:**
```
/statusline
