---
name: academic-proofreading-pilot
description: Proofread supplied academic prose for grammar, clarity, and terminology consistency. Use for a requested passage-level edit, not literature research, novelty assessment, or experiment design.
license: MIT
---

# Academic proofreading

Edit the passage the user supplied. If no passage is present, ask for it. Use the user's language, requested edit depth, venue, and style guide when provided; a missing venue is not a reason to delay a straightforward edit.

## Editing boundary

- Improve grammar, punctuation, syntax, ambiguous references, and inconsistent use of the **same** term. Prefer the smallest revision that makes the intended sentence clear.
- Preserve every numerical value, unit, equation, variable name, citation marker, named method, comparison, uncertainty qualifier, and stated conclusion. Keep the strength of a claim: do not turn “may” into “does”, association into causation, or a limited sample into a general result.
- Do not invent evidence, references, results, methodological details, or a new scientific interpretation. Do not silently repair a possible scientific error by guessing what the author meant. If a sentence has more than one plausible scientific meaning, retain its meaning as far as possible and flag the choice for the author.
- Preserve paragraph order and meaningful Markdown/LaTeX structure unless the user specifically requests restructuring. Do not move citations away from the claims they support.

## Response

Return the revised passage first, in a form the user can paste back. Then add brief notes only for edits that could affect meaning or require an author decision. Quote the uncertain original phrase in those notes so the author can locate it. If the passage needs no edits, say so instead of rewriting for appearance's sake.

Proofreading does not verify the truth, novelty, statistical validity, or completeness of the work. If the user also asks for one of those assessments, identify it as a separate task and do not present a prose edit as that assessment.
