---
name: daily-digest
description: Compile a concise daily briefing for the owner — pull remembered facts and upcoming reminders, scan any explicitly-named sources (calendar files, repos, URLs the owner gave you), and deliver a short, skimmable digest. Use when the owner asks for a "digest", "morning briefing", "what's up today", "catch me up", or when sending a proactive scheduled summary.
---

# Daily digest

Produce a short briefing the owner can read on their phone in ~15 seconds.
This is a chat message, not a report: lead with what matters, cut filler.

## Gather

1. Call `recall_facts` to load what you've been told to remember. Surface only
   the items relevant to *today* (deadlines, people, ongoing tasks) — don't dump
   the whole list.
2. Note any reminders you've set for this chat that are due in the next ~24h.
3. If the owner pointed you at concrete sources (a repo path, a file, a URL),
   check them with the built-in tools (`Bash`, `Read`, `WebFetch`) for anything
   new worth flagging. Do **not** invent sources or go spelunking the web
   unprompted — only look where the owner has actually directed you.

## Format

Keep it to a handful of lines. Use this shape, dropping any empty section:

```
☀️ Daily digest — <weekday, short date>

🔭 Focus
• <the 1–3 things that actually matter today>

⏰ Coming up
• <time> — <reminder/deadline>

🧠 Worth knowing
• <fresh, relevant fact or change you found>
```

## Rules

- Short paragraphs and bullets only — no headings beyond the ones above, no
  code blocks, no walls of text.
- If there's genuinely nothing notable, say so in one line rather than padding.
- Never fabricate. If a source was unreachable, say it briefly and move on.
- End with one concrete, optional next step only if it's clearly useful
  (e.g. "Want me to set a reminder for the 3pm call?").
